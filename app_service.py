import asyncio
import logging
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from config_manager import ConfigManager
from dashboard_builder import build_dashboard_payload
from data_storage import PriceRepository
from legacy_run_service import LegacyRunService
from query_service import QueryService
from report_service import ReportService
from route_service import RouteService
from run_summary import RunSummaryStore
from scheduling.auto_worker import AutoQueryWorker
from scheduler import TaskScheduler
from tail_discovery import (
    AIRLINE_TAIL_DEFAULTS,
    ALL_DOMESTIC_TAIL_CODES,
    DEFAULT_TAIL_TEST_CODES,
    TAIL_DESTINATIONS,
)
from tail_discovery_service import TailDiscoveryService
from tail_jobs import TailDiscoveryJobManager


def configure_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    for handler in list(root_logger.handlers):
        if getattr(handler, "_flight_tracker_handler", False):
            root_logger.removeHandler(handler)
            handler.close()
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler._flight_tracker_handler = True  # type: ignore[attr-defined]
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler._flight_tracker_handler = True  # type: ignore[attr-defined]
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)


class FlightPriceApplication:
    AUTO_QUERY_INTERVAL_HOURS = 12
    AUTO_QUERY_MIN_GAP_MINUTES = 10
    TAIL_DISCOVERY_SAFE_BATCH_SIZE = 3

    def __init__(self, config_manager: ConfigManager) -> None:
        self.config_manager = config_manager
        self.config = config_manager.load()
        configure_logging(self.config.log_file)
        self.repository = PriceRepository(self.config.database_path)
        self.repository.initialize()
        self.report_service = ReportService(config=self.config, repository=self.repository)
        self._run_lock = threading.Lock()
        self.run_summary = RunSummaryStore()
        self.legacy_run_service = LegacyRunService(
            config=self.config,
            repository=self.repository,
            run_summary=self.run_summary,
        )
        self.query_service = QueryService(
            config_manager=self.config_manager,
            repository=self.repository,
            run_lock=self._run_lock,
            get_config=lambda: self.config,
            refresh_config=self.refresh_config,
            summary_store=self.run_summary,
        )
        self.tail_queue_file = self.config.runtime_dir / "tail_discovery_queue.json"
        self.tail_job_manager = TailDiscoveryJobManager(self.run_tail_discovery_blocking, self.repository)
        self.auto_query_worker = AutoQueryWorker(
            self,
            interval_hours=self.AUTO_QUERY_INTERVAL_HOURS,
            min_gap_minutes=self.AUTO_QUERY_MIN_GAP_MINUTES,
        )
        self.route_service = RouteService(
            config_manager=self.config_manager,
            get_config=lambda: self.config,
            refresh_config=self.refresh_config,
            rebalance_auto_query=self.auto_query_worker.rebalance,
            auto_query_interval_hours=self.AUTO_QUERY_INTERVAL_HOURS,
        )

    def refresh_config(self) -> None:
        self.config = self.config_manager.load()
        self.report_service.refresh_config(self.config)
        self.legacy_run_service.refresh_config(self.config)

    def save_credentials(self, provider: str, username: str, password: str) -> None:
        path = self.config_manager.save_credentials(provider, username, password)
        logging.info("encrypted credentials saved to %s", path)

    async def bootstrap_session(
        self,
        provider: str,
        manual: bool,
        backend_override: str | None = None,
    ) -> None:
        from browser_automation import BrowserAutomation

        credentials = self.config_manager.load_credentials(allow_missing=True)
        async with BrowserAutomation(self.config, credentials, backend_override=backend_override) as automation:
            artifact = await automation.bootstrap_session(provider, manual=manual)
        logging.info(
            "browser session saved provider=%s backend=%s kind=%s path=%s",
            artifact.provider,
            artifact.browser_backend,
            artifact.kind,
            artifact.path,
        )

    async def inspect_page(
        self,
        provider: str,
        route_index: int = 0,
        pause: bool = True,
        backend_override: str | None = None,
    ) -> Path:
        from browser_automation import BrowserAutomation

        route = self.config.routes[route_index]
        credentials = self.config_manager.load_credentials(allow_missing=True)
        async with BrowserAutomation(self.config, credentials, backend_override=backend_override) as automation:
            path = await automation.inspect_query_page(provider, route, pause=pause)
        logging.info("inspection saved to %s", path)
        return path

    async def run_once(
        self,
        provider_filter: str | None = None,
        backend_override: str | None = None,
    ) -> dict[str, Any]:
        return await self.query_service.run_once(
            provider_filter=provider_filter,
            backend_override=backend_override,
        )

    async def run_route_once(
        self,
        route_key: str,
        provider_filter: str | None = None,
        backend_override: str | None = None,
    ) -> dict[str, Any]:
        return await self.query_service.run_route_once(
            route_key=route_key,
            provider_filter=provider_filter,
            backend_override=backend_override,
        )

    async def run_route_group_once(
        self,
        group_id: str,
        provider_filter: str | None = None,
        backend_override: str | None = None,
    ) -> dict[str, Any]:
        return await self.query_service.run_route_group_once(
            group_id=group_id,
            provider_filter=provider_filter,
            backend_override=backend_override,
        )

    def run_once_blocking(
        self,
        provider_filter: str | None = None,
        backend_override: str | None = None,
    ) -> dict[str, Any]:
        return self.query_service.run_once_blocking(
            provider_filter=provider_filter,
            backend_override=backend_override,
        )

    def run_route_once_blocking(
        self,
        route_key: str,
        provider_filter: str | None = None,
        backend_override: str | None = None,
    ) -> dict[str, Any]:
        return self.query_service.run_route_once_blocking(
            route_key=route_key,
            provider_filter=provider_filter,
            backend_override=backend_override,
        )

    def run_route_group_once_blocking(
        self,
        group_id: str,
        provider_filter: str | None = None,
        backend_override: str | None = None,
    ) -> dict[str, Any]:
        return self.query_service.run_route_group_once_blocking(
            group_id=group_id,
            provider_filter=provider_filter,
            backend_override=backend_override,
        )

    async def run_tail_discovery(
        self,
        payload: dict[str, Any],
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        cancel_checker: Callable[[], bool] | None = None,
        verification_continue_checker: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        self.refresh_config()
        service = TailDiscoveryService(
            config=self.config,
            config_manager=self.config_manager,
            repository=self.repository,
            run_lock=self._run_lock,
            queue_file=self.tail_queue_file,
            safe_batch_size=self.TAIL_DISCOVERY_SAFE_BATCH_SIZE,
        )
        summary = await service.run(
            payload,
            progress_callback=progress_callback,
            cancel_checker=cancel_checker,
            verification_continue_checker=verification_continue_checker,
        )
        if not summary.get("running"):
            self.run_summary.finished(saved=summary.get("saved", 0), errors=summary["errors"])
        return summary

    def run_tail_discovery_blocking(
        self,
        payload: dict[str, Any],
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        cancel_checker: Callable[[], bool] | None = None,
        verification_continue_checker: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        return asyncio.run(
            self.run_tail_discovery(
                payload,
                progress_callback=progress_callback,
                cancel_checker=cancel_checker,
                verification_continue_checker=verification_continue_checker,
            )
        )

    def submit_tail_discovery(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.tail_job_manager.submit(payload)

    def tail_discovery_job(self, job_id: str) -> dict[str, Any]:
        return self.tail_job_manager.get(job_id)

    def cancel_tail_discovery_job(self, job_id: str) -> dict[str, Any]:
        return self.tail_job_manager.cancel(job_id)

    def continue_tail_discovery_job(self, job_id: str) -> dict[str, Any]:
        return self.tail_job_manager.continue_after_verification(job_id)

    def retry_failed_tail_discovery_job(self, job_id: str) -> dict[str, Any]:
        job = self.tail_job_manager.get(job_id)
        payload = dict(job.get("payload") or {})
        progress = job.get("progress") or {}
        result = job.get("result") or {}
        report = result.get("report") or progress
        failed_codes = [
            str(code).upper()
            for code in report.get("failed_codes", [])
            if str(code).strip()
        ]
        if not failed_codes:
            failed_codes = [
                str(item.get("destination") or "").upper()
                for item in report.get("attempts", [])
                if item.get("status") == "failed" and item.get("destination")
            ]
        failed_codes = list(dict.fromkeys([code for code in failed_codes if code]))
        if not failed_codes:
            raise ValueError("No failed destinations found for this job.")
        payload["candidates"] = failed_codes
        payload["retry_of"] = job_id
        return self.tail_job_manager.submit(payload)

    def latest_tail_discovery_job(self) -> dict[str, Any] | None:
        return self.tail_job_manager.latest()

    def tail_discovery_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.tail_job_manager.list_recent(limit=limit)

    def tail_discovery_attempts(self, job_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self.repository.fetch_tail_discovery_attempts(job_id=job_id, limit=limit)

    def tail_discovery_candidates(self) -> dict[str, Any]:
        return {
            "default_codes": list(DEFAULT_TAIL_TEST_CODES),
            "default_profiles": {
                "DEFAULT": list(DEFAULT_TAIL_TEST_CODES),
                "ALL_DOMESTIC": list(ALL_DOMESTIC_TAIL_CODES),
                **{key: list(value) for key, value in AIRLINE_TAIL_DEFAULTS.items()},
            },
            "safe_batch_size": self.TAIL_DISCOVERY_SAFE_BATCH_SIZE,
            "candidates": [item.to_dict() for item in TAIL_DESTINATIONS],
        }

    def tail_discovery_results(self, limit: int = 100) -> list[dict[str, Any]]:
        from providers import ctrip_tail_parser

        results = self.repository.fetch_tail_discovery_results(limit=limit)
        for item in results:
            raw_payload = item.get("raw_payload") if isinstance(item.get("raw_payload"), dict) else {}
            if raw_payload:
                item["detail_source"] = raw_payload.get("detail_source") or item.get("detail_source")
                item["detail_quality"] = raw_payload.get("detail_quality") or item.get("detail_quality")
                item["review_url"] = raw_payload.get("search_url") or raw_payload.get("url")
                item["date_evidence"] = raw_payload.get("date_evidence") or {}
                item["validation"] = raw_payload.get("validation") or {}
            details = item.get("flight_details")
            if (
                isinstance(details, list)
                and details
                and self._tail_details_need_rebuild(details)
            ):
                source_text = next(
                    (
                        str(detail.get("row_text") or detail.get("summary_text") or "")
                        for detail in details
                        if isinstance(detail, dict) and (detail.get("row_text") or detail.get("summary_text"))
                    ),
                    "",
                )
                item["flight_details"] = ctrip_tail_parser.build_tail_details_from_text_block(
                    source_text,
                    origin=str(item.get("origin") or ""),
                    transfer=str(item.get("transfer") or ""),
                    destination=str(item.get("destination") or ""),
                    departure_date=str(item.get("departure_date") or ""),
                    cabin=str(item.get("cabin") or "economy_plus"),
                )
            item.pop("raw_payload", None)
        return results

    @staticmethod
    def _tail_details_need_rebuild(details: list[Any]) -> bool:
        if len(details) == 1 and isinstance(details[0], dict) and details[0].get("row_text"):
            return True
        for detail in details:
            if not isinstance(detail, dict):
                continue
            airport_text = " ".join(
                str(detail.get(key) or "")
                for key in ["departure_airport", "arrival_airport"]
            )
            if any(token in airport_text for token in ["¥", "起", "订票", "中转组合购买须知"]):
                return True
        return False

    def run_once_via_cli(self, provider_filter: str | None = None) -> dict[str, Any]:
        return self.legacy_run_service.run_once_via_cli(provider_filter=provider_filter)

    def start_run_cycle_via_cli(self, provider_filter: str | None = None) -> dict[str, Any]:
        return self.legacy_run_service.start_run_cycle_via_cli(provider_filter=provider_filter)

    def schedule(self, provider_filter: str | None = None, interval_minutes: int | None = None) -> None:
        scheduler = TaskScheduler(
            timezone=self.config.timezone,
            interval_minutes=interval_minutes or self.config.scheduler_interval_minutes,
        )

        def job() -> None:
            asyncio.run(self.run_once(provider_filter=provider_filter))

        scheduler.add_price_job(job, interval_minutes=interval_minutes)
        job()
        scheduler.run_forever()

    def report(self, route_key: str | None = None, provider: str | None = None) -> dict[str, str]:
        return self.report_service.report(route_key=route_key, provider=provider)

    def export_csv(self, output_path: Path | None = None) -> Path:
        return self.report_service.export_csv(output_path)

    def list_routes(self) -> list[dict[str, Any]]:
        return self.route_service.list_routes()

    def upsert_route(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.route_service.upsert_route(payload)

    def delete_route(self, route_key: str) -> bool:
        return self.route_service.delete_route(route_key)

    def delete_route_group(self, group_id: str) -> bool:
        return self.route_service.delete_route_group(group_id)

    def update_route_auto_query(self, route_key: str, enabled: bool) -> dict[str, Any]:
        return self.route_service.update_auto_query(route_key, enabled)

    def start_auto_query_worker(self) -> None:
        self.auto_query_worker.start()

    def stop_auto_query_worker(self) -> None:
        self.auto_query_worker.stop()

    def process_auto_queries(self) -> dict[str, Any]:
        return self.auto_query_worker.process()

    def dashboard_payload(self) -> dict[str, Any]:
        snapshots = self.repository.fetch_snapshots()
        report_paths = self.report_service.artifact_paths()
        return build_dashboard_payload(
            config=self.config,
            routes=self.list_routes(),
            snapshots=snapshots,
            report_paths=report_paths,
            last_run_summary=self.run_summary.get(),
            auto_query_interval_hours=self.AUTO_QUERY_INTERVAL_HOURS,
            auto_query_min_gap_minutes=self.AUTO_QUERY_MIN_GAP_MINUTES,
            session_hints=self.browser_session_hints(),
        )

    def browser_session_hints(self) -> list[dict[str, Any]]:
        if self.config.browser_backend != "chrome":
            return []
        from browser_session_manager import BrowserSessionManager

        manager = BrowserSessionManager(self.config)
        hints: list[dict[str, Any]] = []
        for provider in self.config.providers.values():
            if not provider.enabled:
                continue
            health = manager.chrome_profile_health(provider, "chrome")
            if health.get("ok"):
                continue
            hints.append(
                {
                    "provider": provider.name,
                    "backend": "chrome",
                    "profile_path": health.get("profile_path"),
                    "reason": health.get("reason"),
                    "needs_login": True,
                    "message": f"{provider.name} 的 Chrome 登录档案异常，已切换 recovery profile；下次查询可能需要重新登录。",
                }
            )
        return hints

    def browser_session_diagnostics(self) -> dict[str, Any]:
        from browser_session_manager import BrowserSessionManager

        manager = BrowserSessionManager(self.config)
        providers: list[dict[str, Any]] = []
        for provider in self.config.providers.values():
            if not provider.enabled:
                continue
            backend_order = manager.backend_names_for_provider(provider)
            health = (
                manager.chrome_profile_health(provider, "chrome")
                if "chrome" in backend_order
                else None
            )
            providers.append(
                {
                    "provider": provider.name,
                    "backend_order": backend_order,
                    "primary_backend": backend_order[0] if backend_order else None,
                    "chrome_profile": health,
                    "needs_login": bool(health and not health.get("ok")),
                }
            )
        return {
            "default_backend": self.config.browser_backend,
            "fallbacks": list(self.config.browser_backend_fallbacks),
            "providers": providers,
            "hints": self.browser_session_hints(),
        }

    def browser_profiles(self) -> dict[str, Any]:
        from browser_session_manager import BrowserSessionManager

        manager = BrowserSessionManager(self.config)
        providers: list[dict[str, Any]] = []
        attempts = self.repository.fetch_tail_discovery_attempts(limit=500)
        snapshots = self.repository.fetch_snapshots()
        for provider in self.config.providers.values():
            if not provider.enabled:
                continue
            backend_order = manager.backend_names_for_provider(provider)
            chrome_enabled = "chrome" in backend_order
            health = manager.chrome_profile_health(provider, "chrome") if chrome_enabled else None
            providers.append(
                {
                    "provider": provider.name,
                    "backend_order": backend_order,
                    "chrome_enabled": chrome_enabled,
                    "storage_state_file": str(provider.storage_state_file),
                    "storage_state_exists": provider.storage_state_file.exists(),
                    "chrome_profile": health,
                    "activity": self._profile_activity(provider.name, attempts, snapshots),
                }
            )
        return {
            "default_backend": self.config.browser_backend,
            "fallbacks": list(self.config.browser_backend_fallbacks),
            "providers": providers,
        }

    def open_browser_profile(self, provider_name: str, *, profile: str | None = None) -> dict[str, Any]:
        from backends.chrome import _find_system_chrome
        from browser_session_manager import BrowserSessionManager

        provider = self.config.providers.get(provider_name)
        if not provider or not provider.enabled:
            raise KeyError(provider_name)
        manager = BrowserSessionManager(self.config)
        profile_name = self._resolve_profile_name(manager, provider, profile)
        profile_path = manager.persistent_profile_path_for_backend(provider, "chrome", profile_name)
        profile_path.mkdir(parents=True, exist_ok=True)
        chrome_path = _find_system_chrome()
        if not chrome_path:
            raise RuntimeError("System Chrome not found.")
        subprocess.Popen(
            [
                chrome_path,
                f"--user-data-dir={profile_path}",
                "--no-first-run",
                "--new-window",
                provider.login_url or "https://flights.ctrip.com",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        opened_at = self._now_iso()
        if profile_name.endswith("-recovery"):
            manager.set_chrome_profile_preference(provider.name, forced_profile="recovery", last_manual_opened_at=opened_at)
        else:
            manager.set_chrome_profile_preference(provider.name, forced_profile=None, last_manual_opened_at=opened_at)
        return {"opened": True, "provider": provider.name, "profile_name": profile_name, "profile_path": str(profile_path)}

    def switch_browser_profile(self, provider_name: str, *, profile: str) -> dict[str, Any]:
        from browser_session_manager import BrowserSessionManager

        provider = self.config.providers.get(provider_name)
        if not provider or not provider.enabled:
            raise KeyError(provider_name)
        manager = BrowserSessionManager(self.config)
        normalized = str(profile or "").strip().lower()
        if normalized not in {"primary", "recovery"}:
            raise ValueError("profile must be primary or recovery")
        manager.set_chrome_profile_preference(
            provider.name,
            forced_profile="recovery" if normalized == "recovery" else None,
        )
        return self.browser_profiles()

    def reset_recovery_profile(self, provider_name: str) -> dict[str, Any]:
        from browser_session_manager import BrowserSessionManager, CHROME_PROFILE_RECOVERY_SUFFIXES

        provider = self.config.providers.get(provider_name)
        if not provider or not provider.enabled:
            raise KeyError(provider_name)
        manager = BrowserSessionManager(self.config)
        base_name = manager.profile_name_for_backend(provider, "chrome")
        removed: list[str] = []
        for suffix in CHROME_PROFILE_RECOVERY_SUFFIXES:
            path = manager.persistent_profile_path_for_backend(provider, "chrome", f"{base_name}-{suffix}")
            if path.exists():
                shutil.rmtree(path)
                removed.append(str(path))
        manager.set_chrome_profile_preference(provider.name, forced_profile=None)
        return {"removed": removed, "profiles": self.browser_profiles()}

    def clean_browser_profile(self, provider_name: str, *, profile: str | None = None, switch_to_recovery: bool = False) -> dict[str, Any]:
        from browser_session_manager import BrowserSessionManager

        provider = self.config.providers.get(provider_name)
        if not provider or not provider.enabled:
            raise KeyError(provider_name)
        manager = BrowserSessionManager(self.config)
        profile_name = self._resolve_profile_name(manager, provider, profile or "selected")
        result = manager.clean_chrome_profile_site_data(
            provider,
            "chrome",
            profile_name=profile_name,
            switch_to_recovery=switch_to_recovery,
        )
        return {**result, "profiles": self.browser_profiles()}

    def _resolve_profile_name(self, manager: Any, provider: Any, profile: str | None) -> str:
        base_name = manager.profile_name_for_backend(provider, "chrome")
        normalized = str(profile or "selected").strip().lower()
        if normalized == "primary":
            return base_name
        if normalized == "recovery":
            return f"{base_name}-recovery"
        return manager.selected_chrome_profile_name(provider, "chrome")

    def _profile_activity(
        self,
        provider_name: str,
        attempts: list[dict[str, Any]],
        snapshots: list[dict[str, Any]],
    ) -> dict[str, Any]:
        provider_attempts = [item for item in attempts if item.get("provider") == provider_name]
        provider_snapshots = [item for item in snapshots if item.get("provider") == provider_name]
        latest_success = next((item for item in provider_attempts if item.get("status") == "matched"), None)
        latest_verification = next(
            (
                item
                for item in provider_attempts
                if "verification" in str(item.get("phase") or item.get("error") or "").lower()
                or item.get("status") == "needs_verification"
            ),
            None,
        )
        latest_failure = next((item for item in provider_attempts if item.get("status") == "failed"), None)
        latest_snapshot = provider_snapshots[-1] if provider_snapshots else None
        return {
            "last_success_at": (latest_success or latest_snapshot or {}).get("observed_at"),
            "last_verification_at": (latest_verification or {}).get("observed_at"),
            "last_failure_at": (latest_failure or {}).get("observed_at"),
            "last_failure_reason": (latest_failure or {}).get("error"),
            "recent_attempts": len(provider_attempts),
            "recent_failures": sum(1 for item in provider_attempts if item.get("status") == "failed"),
        }

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
