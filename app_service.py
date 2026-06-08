from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any

from browser_profile_service import BrowserProfileService
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
from tail_discovery_manager import TailDiscoveryManager


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
        self.browser_profile_service = BrowserProfileService(config=self.config, repository=self.repository)
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
        self.tail_discovery_manager = TailDiscoveryManager(
            config=self.config,
            config_manager=self.config_manager,
            repository=self.repository,
            run_lock=self._run_lock,
            safe_batch_size=self.TAIL_DISCOVERY_SAFE_BATCH_SIZE,
            on_finished=self._mark_run_summary_finished,
        )

    def refresh_config(self) -> None:
        self.config = self.config_manager.load()
        self.report_service.refresh_config(self.config)
        self.browser_profile_service.refresh_config(self.config)
        self.tail_discovery_manager.refresh_config(self.config)
        self.legacy_run_service.refresh_config(self.config)

    def _mark_run_summary_finished(self, saved: int, errors: list[str]) -> None:
        self.run_summary.finished(saved=saved, errors=errors)

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
        progress_callback=None,
        cancel_checker=None,
        verification_continue_checker=None,
    ) -> dict[str, Any]:
        self.refresh_config()
        return await self.tail_discovery_manager.run_tail_discovery(
            payload,
            progress_callback=progress_callback,
            cancel_checker=cancel_checker,
            verification_continue_checker=verification_continue_checker,
        )

    def run_tail_discovery_blocking(
        self,
        payload: dict[str, Any],
        progress_callback=None,
        cancel_checker=None,
        verification_continue_checker=None,
    ) -> dict[str, Any]:
        self.refresh_config()
        return self.tail_discovery_manager.run_tail_discovery_blocking(
            payload,
            progress_callback=progress_callback,
            cancel_checker=cancel_checker,
            verification_continue_checker=verification_continue_checker,
        )

    def submit_tail_discovery(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.refresh_config()
        return self.tail_discovery_manager.submit_tail_discovery(payload)

    def tail_discovery_job(self, job_id: str) -> dict[str, Any]:
        return self.tail_discovery_manager.tail_discovery_job(job_id)

    def cancel_tail_discovery_job(self, job_id: str) -> dict[str, Any]:
        return self.tail_discovery_manager.cancel_tail_discovery_job(job_id)

    def continue_tail_discovery_job(self, job_id: str) -> dict[str, Any]:
        return self.tail_discovery_manager.continue_tail_discovery_job(job_id)

    def retry_failed_tail_discovery_job(self, job_id: str) -> dict[str, Any]:
        return self.tail_discovery_manager.retry_failed_tail_discovery_job(job_id)

    def latest_tail_discovery_job(self) -> dict[str, Any] | None:
        return self.tail_discovery_manager.latest_tail_discovery_job()

    def tail_discovery_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.tail_discovery_manager.tail_discovery_jobs(limit=limit)

    def tail_discovery_attempts(self, job_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self.tail_discovery_manager.tail_discovery_attempts(job_id=job_id, limit=limit)

    def tail_discovery_candidates(self) -> dict[str, Any]:
        return self.tail_discovery_manager.tail_discovery_candidates()

    def tail_discovery_results(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.tail_discovery_manager.tail_discovery_results(limit=limit)

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
            session_hints=self.browser_profile_service.browser_session_hints(),
        )

    def browser_session_hints(self) -> list[dict[str, Any]]:
        return self.browser_profile_service.browser_session_hints()

    def browser_session_diagnostics(self) -> dict[str, Any]:
        return self.browser_profile_service.browser_session_diagnostics()

    def browser_profiles(self) -> dict[str, Any]:
        return self.browser_profile_service.browser_profiles()

    def open_browser_profile(self, provider_name: str, *, profile: str | None = None) -> dict[str, Any]:
        return self.browser_profile_service.open_browser_profile(provider_name, profile=profile)

    def switch_browser_profile(self, provider_name: str, *, profile: str) -> dict[str, Any]:
        return self.browser_profile_service.switch_browser_profile(provider_name, profile=profile)

    def reset_recovery_profile(self, provider_name: str) -> dict[str, Any]:
        return self.browser_profile_service.reset_recovery_profile(provider_name)

    def clean_browser_profile(
        self,
        provider_name: str,
        *,
        profile: str | None = None,
        switch_to_recovery: bool = False,
    ) -> dict[str, Any]:
        return self.browser_profile_service.clean_browser_profile(
            provider_name,
            profile=profile,
            switch_to_recovery=switch_to_recovery,
        )
