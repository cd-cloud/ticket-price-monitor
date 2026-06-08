from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from config_manager import AppConfig, ConfigManager
from data_storage import PriceRepository
from tail_discovery import (
    AIRLINE_TAIL_DEFAULTS,
    ALL_DOMESTIC_TAIL_CODES,
    DEFAULT_TAIL_TEST_CODES,
    TAIL_DESTINATIONS,
)
from tail_discovery_service import TailDiscoveryService
from tail_jobs import TailDiscoveryJobManager


class TailDiscoveryManager:
    def __init__(
        self,
        *,
        config: AppConfig,
        config_manager: ConfigManager,
        repository: PriceRepository,
        run_lock: Any,
        safe_batch_size: int,
        on_finished: Callable[[int, list[str]], None] | None = None,
    ) -> None:
        self.config = config
        self.config_manager = config_manager
        self.repository = repository
        self.run_lock = run_lock
        self.safe_batch_size = safe_batch_size
        self.on_finished = on_finished
        self.tail_queue_file = self.config.runtime_dir / "tail_discovery_queue.json"
        self.job_manager = TailDiscoveryJobManager(self.run_tail_discovery_blocking, self.repository)

    def refresh_config(self, config: AppConfig) -> None:
        self.config = config
        self.tail_queue_file = self.config.runtime_dir / "tail_discovery_queue.json"

    async def run_tail_discovery(
        self,
        payload: dict[str, Any],
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        cancel_checker: Callable[[], bool] | None = None,
        verification_continue_checker: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        service = TailDiscoveryService(
            config=self.config,
            config_manager=self.config_manager,
            repository=self.repository,
            run_lock=self.run_lock,
            queue_file=self.tail_queue_file,
            safe_batch_size=self.safe_batch_size,
        )
        summary = await service.run(
            payload,
            progress_callback=progress_callback,
            cancel_checker=cancel_checker,
            verification_continue_checker=verification_continue_checker,
        )
        if not summary.get("running") and self.on_finished:
            self.on_finished(int(summary.get("saved", 0) or 0), list(summary.get("errors") or []))
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
        return self.job_manager.submit(payload)

    def tail_discovery_job(self, job_id: str) -> dict[str, Any]:
        return self.job_manager.get(job_id)

    def cancel_tail_discovery_job(self, job_id: str) -> dict[str, Any]:
        return self.job_manager.cancel(job_id)

    def continue_tail_discovery_job(self, job_id: str) -> dict[str, Any]:
        return self.job_manager.continue_after_verification(job_id)

    def retry_failed_tail_discovery_job(self, job_id: str) -> dict[str, Any]:
        job = self.job_manager.get(job_id)
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
        return self.job_manager.submit(payload)

    def latest_tail_discovery_job(self) -> dict[str, Any] | None:
        return self.job_manager.latest()

    def tail_discovery_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.job_manager.list_recent(limit=limit)

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
            "safe_batch_size": self.safe_batch_size,
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
            if isinstance(details, list) and details and self._tail_details_need_rebuild(details):
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
        suspicious_tokens = ["楼", "订票", "中转组合购买须知", "票", "购票须知"]
        for detail in details:
            if not isinstance(detail, dict):
                continue
            airport_text = " ".join(str(detail.get(key) or "") for key in ["departure_airport", "arrival_airport"])
            if any(token in airport_text for token in suspicious_tokens):
                return True
        return False
