from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict
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
from tail_service import normalize_tail_discovery_request
from tail_quotes import public_quote_metadata, itinerary_key
from tail_batch import TailBatchRunner, PROVIDERS, ACTIVE, batch_children, checkpoint_root


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

    def make_service(self, *, run_lock=None, queue_file=None):
        return TailDiscoveryService(
            config=self.config, config_manager=self.config_manager,
            repository=self.repository, run_lock=run_lock if run_lock is not None else self.run_lock,
            queue_file=queue_file or self.tail_queue_file, safe_batch_size=self.safe_batch_size,
        )

    async def run_tail_discovery(
        self,
        payload: dict[str, Any],
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        cancel_checker: Callable[[], bool] | None = None,
        verification_continue_checker: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        service = TailBatchRunner(self) if payload.get("provider") == "both" else self.make_service()
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
        payload = {key: value for key, value in payload.items() if not key.startswith("_")}
        if payload.get("provider") == "both":
            return self._submit_batch(payload)
        request = normalize_tail_discovery_request(payload)
        if not request.candidates and request.search_scope != "hybrid":
            raise ValueError("请选择至少一个有效的尾段目的地。")
        if request.provider not in self.config.providers or not self.config.providers[request.provider].enabled:
            raise ValueError("所选平台未启用。")
        if request.mode == "resume":
            source = self.job_manager.get(request.source_job_id)
            if source["status"] in {"queued", "running", "needs_verification", "cancelling"}:
                raise ValueError("任务仍在运行；人工验证请使用继续验证。")
            payload["_checkpoint_job_id"] = (
                (source.get("result") or {}).get("checkpoint_job_id")
                or (source.get("progress") or {}).get("checkpoint_job_id")
                or (source.get("payload") or {}).get("_checkpoint_job_id")
                or request.source_job_id
            )
            payload["_execution_mode"] = (
                (source.get("result") or {}).get("execution_mode")
                or (source.get("progress") or {}).get("execution_mode")
                or (source.get("payload") or {}).get("_execution_mode")
                or (source.get("payload") or {}).get("mode") or "explore"
            )
        if request.mode == "recheck":
            if not request.source_result_id or not request.expected_itinerary_key:
                raise ValueError("报价复核需要原报价 ID 和行程标识。")
            original = self.repository.fetch_tail_result(request.source_result_id)
            if itinerary_key(original) != request.expected_itinerary_key:
                raise ValueError("复核行程与原报价不一致。")
        if self.run_lock.locked():
            raise RuntimeError("已有查询正在运行，请稍后再试。")
        cooldown = self.repository.tail_cooldown(request.provider)
        if cooldown:
            raise RuntimeError(f"平台限流冷却中，{cooldown} 后再试。")
        return self.job_manager.submit(payload)

    def _submit_batch(self, payload, *, source=None, action=None):
        """Build child plans server-side. Never accept client checkpoint/child IDs."""
        mode = action or str(payload.get("mode") or "explore")
        if mode not in {"explore", "resume", "refresh", "retry-failed"}:
            raise ValueError("联合查询只支持发现、继续、刷新或重试；单条复核使用报价入口。")
        if source is None and mode == "resume":
            source = self.job_manager.get(str(payload.get("source_job_id") or ""))
        if source:
            if (source.get("payload") or {}).get("provider") != "both":
                raise ValueError("联合查询不能继续单平台任务。")
            if source["status"] in ACTIVE:
                raise ValueError("任务仍在运行，请等待结束或使用继续验证。")
            if mode != "explore":
                # Resume/refresh are strictly based on the original snapshot,
                # not a newly edited form or client-supplied nested metadata.
                payload = {k: v for k, v in source["payload"].items() if not k.startswith("_")}
        if mode in {"refresh", "retry-failed"} and not source:
            raise ValueError("此操作需要原联合任务。")
        if self.run_lock.locked():
            raise RuntimeError("已有查询正在运行，请稍后再试。")
        feizhu_scope = str(payload.get("feizhu_search_scope") or "open")
        if feizhu_scope not in {"candidates", "open", "hybrid"}:
            raise ValueError("不支持的飞猪目的地查询方式。")
        previous = {c["provider"]: c for c in batch_children(source or {})}
        # A service restart can happen before the parent publishes child status.
        for plan in (source or {}).get("payload", {}).get("_batch_children", []):
            previous.setdefault(plan["provider"], plan)
        plans = []
        for provider in PROVIDERS:
            old = self.repository.fetch_tail_discovery_job(previous.get(provider, {}).get("job_id", "")) if source else None
            if old and old["status"] in ACTIVE:
                old = self.job_manager._normalize_persisted_job(old)
            if old and mode in {"resume", "retry-failed"} and (
                    old["status"] == "finished" or mode == "retry-failed" and old["status"] != "failed"):
                plans.append({"provider": provider, "job_id": old["job_id"],
                              "payload": old["payload"], "reuse": True})
                continue
            child = {k: v for k, v in payload.items() if not k.startswith("_")}
            child.update(provider=provider, search_scope="candidates" if provider == "ctrip" else feizhu_scope,
                         mode="explore", source_job_id="", expected_itinerary_key="", source_result_id=0)
            if provider == "ctrip":
                child["max_provider_calls"] = 0
            elif feizhu_scope in {"open", "hybrid"}:
                child.update(max_candidates=0, target_results=0)
                if feizhu_scope == "open":
                    child["candidates"] = []
            if old and mode in {"resume", "retry-failed", "refresh"}:
                child = {k: v for k, v in old["payload"].items() if not k.startswith("_")}
                child.update(mode="refresh" if mode == "refresh" else "resume", source_job_id=old["job_id"])
                if mode == "refresh" and child.get("search_scope") in {"open", "hybrid"}:
                    child.update(search_scope="candidates", candidates=sorted(self.repository.tail_known_destinations(
                        provider=provider, origin=child["origin"], transfer=child["transfer"],
                        departure_date=child["departure_date"], cabin=child.get("cabin", "economy_plus"),
                        passengers=int(child.get("passengers") or 1),
                    )))
                effective = (old.get("result") or {}).get("effective_candidates") or (old.get("progress") or {}).get("effective_candidates")
                if mode != "refresh" and effective and child.get("search_scope") != "hybrid":
                    child["candidates"] = effective
            # Refresh with no known destinations is a per-provider error, not a
            # reason to suppress the other provider's refresh.
            empty_refresh = bool(source) and mode != "explore" and child.get("candidates") == []
            validation_input = dict(child)
            if empty_refresh:
                validation_input.pop("candidates")
            normalized = asdict(normalize_tail_discovery_request(validation_input))
            if empty_refresh:
                normalized["candidates"] = []
            elif not normalized["candidates"] and normalized["search_scope"] != "hybrid":
                raise ValueError("请选择至少一个有效的携程尾段城市。")
            if old and normalized["mode"] == "resume":
                normalized["_checkpoint_job_id"] = checkpoint_root(old)
                normalized["_execution_mode"] = (
                    (old.get("result") or {}).get("execution_mode")
                    or (old.get("progress") or {}).get("execution_mode")
                    or old["payload"].get("_execution_mode") or old["payload"].get("mode") or "explore")
            plans.append({"provider": provider, "job_id": uuid.uuid4().hex[:12], "payload": normalized,
                          **({"preflight_error": "此平台当前条件没有已发现的报价，请重新查询。"} if empty_refresh else {})})
        if mode == "retry-failed" and all(p.get("reuse") for p in plans):
            raise ValueError("没有失败的平台子任务。")
        payload = {k: v for k, v in payload.items() if not k.startswith("_")}
        payload.update(provider="both", mode="resume" if mode == "retry-failed" else mode,
                       feizhu_search_scope=feizhu_scope, search_scope="candidates",
                       source_job_id=(source or {}).get("job_id", ""), _batch_children=plans)
        return self.job_manager.submit(payload)

    def restart_job(self, job_id: str, mode: str) -> dict[str, Any]:
        if mode not in {"resume", "refresh", "explore"}:
            raise ValueError("Unknown task action.")
        job = self.job_manager.get(job_id)
        if (job.get("payload") or {}).get("provider") == "both":
            return self._submit_batch(job["payload"], source=job, action=mode)
        payload = {key: value for key, value in (job.get("payload") or {}).items() if not key.startswith("_")}
        payload.update(mode=mode, source_job_id=job_id)
        if mode in {"explore", "refresh"}:
            payload.update(expected_itinerary_key="", source_result_id=0)
        if mode == "refresh" and payload.get("search_scope") in {"open", "hybrid"}:
            payload.update(search_scope="candidates", candidates=sorted(self.repository.tail_known_destinations(
                provider=payload["provider"], origin=payload["origin"], transfer=payload["transfer"],
                departure_date=payload["departure_date"], cabin=payload.get("cabin", "economy_plus"),
                passengers=int(payload.get("passengers") or 1),
            )))
        # A resumed refresh must retain its actual candidate set, not the original broad form.
        effective = (job.get("result") or {}).get("effective_candidates") or (job.get("progress") or {}).get("effective_candidates")
        if mode == "resume" and effective and payload.get("search_scope") != "hybrid":
            payload["candidates"] = effective
        return self.submit_tail_discovery(payload)

    def recheck_result(self, result_id: int) -> dict[str, Any]:
        result = self.repository.fetch_tail_result(result_id)
        raw = result.get("raw_payload") or {}
        payload = dict(raw.get("request") or {})
        payload.update({key: result[key] for key in (
            "provider", "origin", "transfer", "departure_date", "cabin", "passengers",
        )})
        payload.update(mode="recheck", candidates=[result["destination"]], ignore_cache=True,
                       search_scope="candidates",
                       expected_itinerary_key=itinerary_key(result), source_job_id="",
                       source_result_id=result_id, max_candidates=1, target_results=0, max_price=None)
        return self.submit_tail_discovery(payload)

    def tail_discovery_job(self, job_id: str) -> dict[str, Any]:
        return self.job_manager.get(job_id)

    def cancel_tail_discovery_job(self, job_id: str) -> dict[str, Any]:
        return self.job_manager.cancel(job_id)

    def continue_tail_discovery_job(self, job_id: str) -> dict[str, Any]:
        return self.job_manager.continue_after_verification(job_id)

    def retry_failed_tail_discovery_job(self, job_id: str) -> dict[str, Any]:
        job = self.job_manager.get(job_id)
        if (job.get("payload") or {}).get("provider") == "both":
            return self._submit_batch(job["payload"], source=job, action="retry-failed")
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
        if payload.get("search_scope") == "hybrid":
            return self.restart_job(job_id, "resume")
        payload["candidates"] = failed_codes
        payload["retry_of"] = job_id
        payload.update(mode="explore", ignore_cache=True)
        return self.submit_tail_discovery(payload)

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

    def tail_discovery_results(self, limit: int = 100, *, job_id: str | None = None,
                               offset: int = 0) -> list[dict[str, Any]]:
        from providers import ctrip_tail_parser

        root = None
        roots = None
        if job_id:
            job = self.repository.fetch_tail_discovery_job(job_id)
            if not job:
                raise KeyError(job_id)
            root = str((job.get("result") or {}).get("checkpoint_job_id")
                       or (job.get("progress") or {}).get("checkpoint_job_id")
                       or (job.get("payload") or {}).get("_checkpoint_job_id") or job_id)
            if (job.get("payload") or {}).get("provider") == "both":
                refs = batch_children(job) or job["payload"].get("_batch_children", [])
                roots = []
                for ref in refs:
                    child = self.repository.fetch_tail_discovery_job(ref["job_id"])
                    roots.append(checkpoint_root(child) if child else ref["payload"].get("_checkpoint_job_id") or ref["job_id"])
                root = None
        results = self.repository.fetch_tail_discovery_results(
            limit=limit, checkpoint_job_id=root, checkpoint_job_ids=roots, offset=offset,
        )
        for item in results:
            raw_payload = item.get("raw_payload") if isinstance(item.get("raw_payload"), dict) else {}
            item.update(public_quote_metadata(item))
            if raw_payload:
                item["detail_source"] = raw_payload.get("detail_source") or item.get("detail_source")
                item["detail_quality"] = raw_payload.get("detail_quality") or item.get("detail_quality")
                item["review_url"] = raw_payload.get("search_url") or raw_payload.get("url")
                item["date_evidence"] = raw_payload.get("date_evidence") or {}
                item["validation"] = raw_payload.get("validation") or {}
                for key in (
                    "baseline_price", "baseline_observed_at", "baseline_review_url",
                    "savings_amount", "savings_percent",
                ):
                    item[key] = raw_payload.get(key)
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
