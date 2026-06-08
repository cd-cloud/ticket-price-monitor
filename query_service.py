from __future__ import annotations

import asyncio
import importlib
import logging
import random
import threading
from datetime import date, datetime, timezone
from typing import Any, Callable

from config_manager import AppConfig, ConfigManager, RouteQuery
from data_storage import PriceRepository, PriceSnapshot
from providers.registry import provider_definition
from run_summary import RunSummaryStore


AutomationFactory = Callable[[AppConfig, dict[str, dict[str, str]]], Any]
SummaryCallback = Callable[[dict[str, Any]], None]


class CliProviderRuntime:
    async def throttle(self, provider_cfg: Any) -> None:
        delay = random.uniform(
            float(getattr(provider_cfg, "min_delay_seconds", 0) or 0),
            float(getattr(provider_cfg, "max_delay_seconds", 0) or 0),
        )
        if delay > 0:
            await asyncio.sleep(delay)


class ProviderSnapshotResult:
    def __init__(
        self,
        *,
        provider: str,
        price: float,
        currency: str,
        observed_at: str,
        notes: list[str],
        raw_payload: dict[str, Any],
    ) -> None:
        self.provider = provider
        self.price = price
        self.currency = currency
        self.observed_at = observed_at
        self.notes = notes
        self.raw_payload = raw_payload


class QueryService:
    """Runs configured price queries and persists successful snapshots."""

    DEFAULT_PROVIDER_QUERY_TIMEOUT_SECONDS = 150.0

    def __init__(
        self,
        *,
        config_manager: ConfigManager,
        repository: PriceRepository,
        run_lock: threading.Lock,
        get_config: Callable[[], AppConfig],
        refresh_config: Callable[[], None],
        summary_store: RunSummaryStore | None = None,
        update_summary: SummaryCallback | None = None,
        automation_factory: AutomationFactory | None = None,
        provider_query_timeout_seconds: float | None = None,
    ) -> None:
        self.config_manager = config_manager
        self.repository = repository
        self.run_lock = run_lock
        self.get_config = get_config
        self.refresh_config = refresh_config
        self.summary_store = summary_store or RunSummaryStore()
        self.update_summary = update_summary
        self.automation_factory = automation_factory or self._default_automation_factory
        self.provider_query_timeout_seconds = provider_query_timeout_seconds
        self.last_run_summary: dict[str, Any] = self.summary_store.get()

    async def run_once(
        self,
        provider_filter: str | None = None,
        backend_override: str | None = None,
    ) -> dict[str, Any]:
        if not self.run_lock.acquire(blocking=False):
            return {"running": True, "saved": 0, "errors": ["A query run is already in progress."]}

        self.refresh_config()
        config = self.get_config()
        if provider_filter and not self._provider_has_handler(provider_filter):
            self.run_lock.release()
            return {"running": False, "saved": 0, "errors": [f"Unknown provider: {provider_filter}"]}
        self._set_summary({"running": True, "saved": 0, "errors": []})
        try:
            return await self._query_routes(
                config.routes,
                provider_filter=provider_filter,
                backend_override=backend_override,
            )
        finally:
            self.run_lock.release()

    async def run_route_once(
        self,
        route_key: str,
        provider_filter: str | None = None,
        backend_override: str | None = None,
    ) -> dict[str, Any]:
        if not self.run_lock.acquire(blocking=False):
            return {"running": True, "saved": 0, "errors": ["A query run is already in progress."]}

        self.refresh_config()
        config = self.get_config()
        if provider_filter and not self._provider_has_handler(provider_filter):
            self.run_lock.release()
            return {"running": False, "saved": 0, "errors": [f"Unknown provider: {provider_filter}"]}
        route = next((item for item in self.get_config().routes if item.route_key == route_key), None)
        if route is None:
            self.run_lock.release()
            return {"running": False, "saved": 0, "errors": [f"Route not found: {route_key}"]}

        self._set_summary({"running": True, "saved": 0, "errors": []})
        try:
            return await self._query_routes(
                [route],
                provider_filter=provider_filter,
                backend_override=backend_override,
            )
        finally:
            self.run_lock.release()

    async def run_route_group_once(
        self,
        group_id: str,
        provider_filter: str | None = None,
        backend_override: str | None = None,
    ) -> dict[str, Any]:
        if not self.run_lock.acquire(blocking=False):
            return {"running": True, "saved": 0, "errors": ["A query run is already in progress."]}

        self.refresh_config()
        config = self.get_config()
        if provider_filter and not self._provider_has_handler(provider_filter):
            self.run_lock.release()
            return {"running": False, "saved": 0, "errors": [f"Unknown provider: {provider_filter}"]}
        routes = [route for route in config.routes if route.group_id == group_id]
        if not routes:
            self.run_lock.release()
            return {"running": False, "saved": 0, "errors": [f"Route group not found: {group_id}"]}

        self._set_summary({"running": True, "saved": 0, "errors": []})
        try:
            return await self._query_routes(
                routes,
                provider_filter=provider_filter,
                backend_override=backend_override,
            )
        finally:
            self.run_lock.release()

    def run_once_blocking(
        self,
        provider_filter: str | None = None,
        backend_override: str | None = None,
    ) -> dict[str, Any]:
        return asyncio.run(self.run_once(provider_filter=provider_filter, backend_override=backend_override))

    def run_route_once_blocking(
        self,
        route_key: str,
        provider_filter: str | None = None,
        backend_override: str | None = None,
    ) -> dict[str, Any]:
        return asyncio.run(
            self.run_route_once(
                route_key=route_key,
                provider_filter=provider_filter,
                backend_override=backend_override,
            )
        )

    def run_route_group_once_blocking(
        self,
        group_id: str,
        provider_filter: str | None = None,
        backend_override: str | None = None,
    ) -> dict[str, Any]:
        return asyncio.run(
            self.run_route_group_once(
                group_id=group_id,
                provider_filter=provider_filter,
                backend_override=backend_override,
            )
        )

    async def _query_routes(
        self,
        routes: list[RouteQuery],
        provider_filter: str | None = None,
        backend_override: str | None = None,
    ) -> dict[str, Any]:
        config = self.get_config()
        credentials = self.config_manager.load_credentials(allow_missing=True)
        saved = 0
        errors: list[str] = []
        targets = list(self._iter_query_targets(config, routes, provider_filter=provider_filter))
        total_targets = len(targets)
        completed_targets = 0
        saved_pairs: list[dict[str, Any]] = []
        recent_successes: list[dict[str, Any]] = []
        recent_failures: list[dict[str, Any]] = []
        started_at = self._now_iso()
        self._set_summary(
            {
                "running": True,
                "saved": 0,
                "errors": [],
                "completed_targets": 0,
                "total_targets": total_targets,
                "current_target": None,
                "started_at": started_at,
                "finished_at": None,
                "saved_pairs": [],
                "recent_successes": [],
                "recent_failures": [],
            }
        )
        if provider_filter == "feizhu":
            (
                saved,
                completed_targets,
                saved_pairs,
                recent_successes,
                recent_failures,
            ) = await self._query_cli_provider_routes(
                config,
                targets,
                errors,
                provider_filter,
                total_targets=total_targets,
            )
        else:
            try:
                automation_context = self.automation_factory(config, credentials)
                if backend_override and hasattr(automation_context, "backend_override"):
                    automation_context.backend_override = backend_override
                async with automation_context as automation:
                    for route, provider_name, provider_cfg in targets:
                        self._set_summary(
                            {
                                **self.last_run_summary,
                                "running": True,
                                "current_target": self._target_payload(route, provider_name),
                                "completed_targets": completed_targets,
                                "total_targets": total_targets,
                                "saved": saved,
                                "errors": list(errors),
                                "saved_pairs": list(saved_pairs),
                                "recent_successes": list(recent_successes),
                                "recent_failures": list(recent_failures),
                            }
                        )
                        try:
                            timeout_seconds = self._provider_query_timeout_seconds(
                                config,
                                provider_cfg,
                                backend_override=backend_override,
                            )
                            result = await asyncio.wait_for(
                                automation.query_provider(provider_name, route),
                                timeout=timeout_seconds,
                            )
                            self._insert_snapshot(route, result)
                            saved += 1
                            saved_pair = {
                                "provider": provider_name,
                                "route_key": route.route_key,
                                "price": result.price,
                                "observed_at": result.observed_at,
                            }
                            saved_pairs.append(saved_pair)
                            recent_successes.append(
                                {
                                    "provider": provider_name,
                                    "route_key": route.route_key,
                                    "route_display": getattr(route, "display_label", route.route_key),
                                    "price": result.price,
                                    "observed_at": result.observed_at,
                                }
                            )
                            recent_successes[:] = recent_successes[-8:]
                            logging.info(
                                "saved snapshot provider=%s route=%s price=%s",
                                provider_name,
                                route.route_key,
                                result.price,
                            )
                        except asyncio.TimeoutError:
                            message = (
                                f"provider={provider_name} route={route.route_key} failed: "
                                f"query timed out after {timeout_seconds:.1f}s"
                            )
                            errors.append(message)
                            recent_failures.append(
                                {
                                    "provider": provider_name,
                                    "route_key": route.route_key,
                                    "route_display": getattr(route, "display_label", route.route_key),
                                    "error": "query timed out",
                                }
                            )
                            recent_failures[:] = recent_failures[-8:]
                            logging.error(message)
                        except Exception as exc:
                            message = f"provider={provider_name} route={route.route_key} failed: {exc}"
                            errors.append(message)
                            recent_failures.append(
                                {
                                    "provider": provider_name,
                                    "route_key": route.route_key,
                                    "route_display": getattr(route, "display_label", route.route_key),
                                    "error": str(exc),
                                }
                            )
                            recent_failures[:] = recent_failures[-8:]
                            logging.exception(message)
                        completed_targets += 1
                        self._set_summary(
                            {
                                **self.last_run_summary,
                                "running": True,
                                "current_target": self._target_payload(route, provider_name),
                                "completed_targets": completed_targets,
                                "total_targets": total_targets,
                                "saved": saved,
                                "errors": list(errors),
                                "saved_pairs": list(saved_pairs),
                                "recent_successes": list(recent_successes),
                                "recent_failures": list(recent_failures),
                            }
                        )
            except Exception as exc:
                target = provider_filter or "browser providers"
                message = f"provider={target} failed before querying routes: {exc}"
                errors.append(message)
                recent_failures.append(
                    {"provider": target, "route_key": "", "route_display": target, "error": str(exc)}
                )
                logging.exception(message)

        summary = {
            "running": False,
            "saved": saved,
            "errors": errors,
            "completed_targets": completed_targets,
            "total_targets": total_targets,
            "current_target": None,
            "started_at": started_at,
            "finished_at": self._now_iso(),
            "saved_pairs": saved_pairs,
            "recent_successes": recent_successes,
            "recent_failures": recent_failures,
        }
        self._set_summary(summary)
        return summary

    async def _query_cli_provider_routes(
        self,
        config: AppConfig,
        targets: list[tuple[RouteQuery, str, Any]],
        errors: list[str],
        provider_filter: str,
        *,
        total_targets: int,
    ) -> tuple[int, int, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        saved = 0
        completed_targets = 0
        saved_pairs: list[dict[str, Any]] = []
        recent_successes: list[dict[str, Any]] = []
        recent_failures: list[dict[str, Any]] = []
        runtime = CliProviderRuntime()
        provider_module = importlib.import_module(f"providers.{provider_filter}")
        for route, provider_name, provider_cfg in targets:
            self._set_summary(
                {
                    **self.last_run_summary,
                    "running": True,
                    "current_target": self._target_payload(route, provider_name),
                    "completed_targets": completed_targets,
                    "total_targets": total_targets,
                    "saved": saved,
                    "errors": list(errors),
                    "saved_pairs": list(saved_pairs),
                    "recent_successes": list(recent_successes),
                    "recent_failures": list(recent_failures),
                }
            )
            try:
                timeout_seconds = self._provider_query_timeout_seconds(config, provider_cfg)
                result = await asyncio.wait_for(
                    provider_module.query(runtime, provider_cfg, route, None, []),
                    timeout=timeout_seconds,
                )
                raw_payload = dict(result.extra_payload)
                raw_payload.setdefault("final_url", result.final_url)
                raw_payload.setdefault("title", result.title)
                raw_payload.setdefault("departure_time", result.departure_time)
                raw_payload.setdefault("row_text", result.row_text)
                raw_payload.setdefault("flight_details", result.flight_details)
                snapshot_result = ProviderSnapshotResult(
                    provider=provider_name,
                    price=result.price,
                    currency=config.default_currency,
                    observed_at=self.repository.now_iso(),
                    notes=[result.title] if result.title else [],
                    raw_payload=raw_payload,
                )
                self._insert_snapshot(route, snapshot_result)
                saved += 1
                saved_pair = {
                    "provider": provider_name,
                    "route_key": route.route_key,
                    "price": result.price,
                    "observed_at": snapshot_result.observed_at,
                }
                saved_pairs.append(saved_pair)
                recent_successes.append(
                    {
                        "provider": provider_name,
                        "route_key": route.route_key,
                        "route_display": getattr(route, "display_label", route.route_key),
                        "price": result.price,
                        "observed_at": snapshot_result.observed_at,
                    }
                )
                recent_successes[:] = recent_successes[-8:]
                logging.info(
                    "saved CLI snapshot provider=%s route=%s price=%s",
                    provider_name,
                    route.route_key,
                    result.price,
                )
            except asyncio.TimeoutError:
                message = (
                    f"provider={provider_name} route={route.route_key} failed: "
                    f"query timed out after {timeout_seconds:.1f}s"
                )
                errors.append(message)
                recent_failures.append(
                    {
                        "provider": provider_name,
                        "route_key": route.route_key,
                        "route_display": getattr(route, "display_label", route.route_key),
                        "error": "query timed out",
                    }
                )
                recent_failures[:] = recent_failures[-8:]
                logging.error(message)
            except Exception as exc:
                message = f"provider={provider_name} route={route.route_key} failed: {exc}"
                errors.append(message)
                recent_failures.append(
                    {
                        "provider": provider_name,
                        "route_key": route.route_key,
                        "route_display": getattr(route, "display_label", route.route_key),
                        "error": str(exc),
                    }
                )
                recent_failures[:] = recent_failures[-8:]
                logging.exception(message)
            completed_targets += 1
            self._set_summary(
                {
                    **self.last_run_summary,
                    "running": True,
                    "current_target": self._target_payload(route, provider_name),
                    "completed_targets": completed_targets,
                    "total_targets": total_targets,
                    "saved": saved,
                    "errors": list(errors),
                    "saved_pairs": list(saved_pairs),
                    "recent_successes": list(recent_successes),
                    "recent_failures": list(recent_failures),
                }
            )
        return saved, completed_targets, saved_pairs, recent_successes, recent_failures

    def _iter_query_targets(
        self,
        config: AppConfig,
        routes: list[RouteQuery],
        *,
        provider_filter: str | None = None,
    ):
        for route in routes:
            if self._route_is_expired(route):
                logging.info("skipping expired route=%s", route.route_key)
                continue
            providers = route.providers or list(config.providers.keys())
            for provider_name in providers:
                provider_cfg = config.providers.get(provider_name)
                if not provider_cfg or not provider_cfg.enabled:
                    continue
                if not self._provider_has_handler(provider_name):
                    logging.info("skipping provider without handler: %s", provider_name)
                    continue
                if provider_filter and provider_name != provider_filter:
                    continue
                yield route, provider_name, provider_cfg

    def _insert_snapshot(self, route: RouteQuery, result: Any) -> None:
        raw_payload = dict(result.raw_payload or {})
        snapshot = PriceSnapshot(
            provider=result.provider,
            route_key=route.route_key,
            origin=route.origin,
            destination=route.destination,
            departure_date=route.departure_date,
            return_date=route.return_date,
            cabin=route.cabin,
            passengers=route.passengers,
            currency=result.currency,
            price=result.price,
            observed_at=result.observed_at,
            scraped_at=self.repository.now_iso(),
            notes=list(result.notes or []),
            raw_payload=raw_payload,
            parser=raw_payload.get("parser"),
            parser_confidence=raw_payload.get("parser_confidence"),
            detail_quality=raw_payload.get("detail_quality"),
            detail_source=raw_payload.get("detail_source"),
            browser_backend=raw_payload.get("browser_backend"),
        )
        self.repository.insert_snapshot(snapshot)

    def _set_summary(self, summary: dict[str, Any]) -> None:
        self.last_run_summary = self.summary_store.set(summary)
        if self.update_summary:
            self.update_summary(self.last_run_summary)

    @staticmethod
    def _target_payload(route: RouteQuery, provider_name: str) -> dict[str, Any]:
        return {
            "provider": provider_name,
            "route_key": route.route_key,
            "route_display": getattr(route, "display_label", route.route_key),
            "route_type": getattr(route, "route_type", "one_way"),
        }

    def _provider_query_timeout_seconds(
        self,
        config: AppConfig,
        provider_cfg: Any,
        *,
        backend_override: str | None = None,
    ) -> float:
        if self.provider_query_timeout_seconds is not None:
            return self.provider_query_timeout_seconds
        timeout_ms = float(getattr(provider_cfg, "timeout_ms", 0) or 0)
        if timeout_ms <= 0:
            return self.DEFAULT_PROVIDER_QUERY_TIMEOUT_SECONDS
        backend_count = self._provider_backend_count(config, provider_cfg, backend_override=backend_override)
        per_backend_seconds = (timeout_ms / 1000.0) * 3
        cooldown_budget_seconds = 60 * max(0, backend_count - 1)
        return max(
            self.DEFAULT_PROVIDER_QUERY_TIMEOUT_SECONDS,
            per_backend_seconds * backend_count + cooldown_budget_seconds,
        )

    @staticmethod
    def _provider_backend_count(
        config: AppConfig,
        provider_cfg: Any,
        *,
        backend_override: str | None = None,
    ) -> int:
        if backend_override:
            return 1
        names = [
            getattr(config, "browser_backend", ""),
            *list(getattr(config, "browser_backend_fallbacks", []) or []),
        ]
        provider_backend = getattr(provider_cfg, "browser_backend", None)
        if provider_backend:
            names.append(provider_backend)
        names.extend(getattr(provider_cfg, "browser_backend_fallbacks", None) or [])
        normalized = []
        for name in names:
            text = str(name or "").strip().lower()
            if text and text not in normalized:
                normalized.append(text)
        return max(1, len(normalized))

    @staticmethod
    def _default_automation_factory(config: AppConfig, credentials: dict[str, dict[str, str]]) -> Any:
        from browser_automation import BrowserAutomation

        return BrowserAutomation(config, credentials)

    @staticmethod
    def _provider_has_handler(provider_name: str) -> bool:
        definition = provider_definition(provider_name)
        return bool(definition and definition.has_handler)

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _route_is_expired(route: RouteQuery) -> bool:
        completion = QueryService._route_completion_date(route)
        return bool(completion and completion < date.today())

    @staticmethod
    def _route_completion_date(route: RouteQuery) -> date | None:
        candidates: list[str] = []
        if route.return_date:
            candidates.append(route.return_date)
        if route.segments:
            candidates.extend(segment.departure_date for segment in route.segments)
        candidates.append(route.departure_date)
        parsed: list[date] = []
        for value in candidates:
            try:
                parsed.append(datetime.strptime(value, "%Y-%m-%d").date())
            except ValueError:
                continue
        return max(parsed) if parsed else None
