import asyncio
import logging
import random
import math
import uuid
from dataclasses import asdict
from time import monotonic
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from config_manager import AppConfig, ConfigManager
from data_storage import PriceRepository
from tail_service import (
    build_tail_request_queue,
    load_tail_queue,
    normalize_tail_discovery_request,
    save_tail_queue,
    tail_batch_report,
    tail_progress_payload,
    tail_queue_item_update_from_attempt,
    tail_queue_key,
    tail_queue_report,
    validate_tail_result,
)
from tail_models import TailDiscoveryAttempt, TailDiscoveryResult
from tail_quotes import annotate_quote, apply_baselines, select_tail_alternatives, first_leg_key, itinerary_key, cabin_matches
from browser_retry_policy import looks_like_rate_limit
from tail_structured_extractor import model_to_dict, pydantic_ai_status, tail_itinerary_from_result


LOGGER = logging.getLogger(__name__)


class TailDiscoveryService:
    """Coordinates Ctrip tail discovery runs, queue state, progress, and persistence."""

    def __init__(
        self,
        *,
        config: AppConfig,
        config_manager: ConfigManager,
        repository: PriceRepository,
        run_lock: threading.Lock,
        queue_file: Path,
        safe_batch_size: int,
    ) -> None:
        self.config = config
        self.config_manager = config_manager
        self.repository = repository
        self.run_lock = run_lock
        self.queue_file = queue_file
        self.safe_batch_size = safe_batch_size

    async def run(self, payload: dict[str, Any], progress_callback=None, cancel_checker=None,
                  verification_continue_checker=None) -> dict[str, Any]:
        # Validate before acquiring the lock; every acquired lock has a finally.
        request = normalize_tail_discovery_request(payload)
        if not self.run_lock.acquire(blocking=False):
            raise RuntimeError("A query run is already in progress.")
        try:
            return await self._run(request, payload, progress_callback, cancel_checker, verification_continue_checker)
        finally:
            self.run_lock.release()

    async def _run(self, request, payload, progress_callback, cancel_checker, verification_continue_checker):
        started = monotonic()
        deadline = started + request.max_minutes * 60 if request.max_minutes else math.inf
        job_id = str(payload.get("_job_id") or uuid.uuid4().hex)
        root_job_id = job_id
        execution_mode = request.mode
        provider = request.provider
        if provider not in self.config.providers or not self.config.providers[provider].enabled:
            raise ValueError(f"{provider} provider is not enabled.")
        cooldown = self.repository.tail_cooldown(provider)
        if cooldown:
            raise RuntimeError(f"平台限流冷却中，{cooldown} 后再试。")
        candidates = list(request.candidates)
        if request.mode == "refresh":
            known_codes = self.repository.tail_known_destinations(
                provider=provider, origin=request.origin, transfer=request.transfer,
                departure_date=request.departure_date, cabin=request.cabin, passengers=request.passengers,
            )
            candidates = [code for code in candidates if code in known_codes]
            if not candidates:
                raise ValueError("当前条件没有已发现的报价，请先重新探索。")
        fingerprint = tail_queue_key(
            provider=provider, origin=request.origin, transfer=request.transfer,
            departure_date=request.departure_date, cabin=request.cabin, passengers=request.passengers,
            preferred_airlines=request.preferred_airlines, candidates=candidates, max_price=request.max_price,
            departure_time_start=request.departure_time_start, departure_time_end=request.departure_time_end,
            alternatives_per_flight=request.alternatives_per_flight,
        )
        restored = []
        if request.mode == "resume":
            previous = self.repository.fetch_tail_discovery_job(request.source_job_id)
            if not previous:
                raise ValueError("找不到原任务，不能继续。")
            execution_mode = str((previous.get("result") or {}).get("execution_mode")
                                 or (previous.get("progress") or {}).get("execution_mode")
                                 or (previous.get("payload") or {}).get("_execution_mode")
                                 or (previous.get("payload") or {}).get("mode") or "explore")
            root_job_id = str((previous.get("result") or {}).get("checkpoint_job_id")
                              or (previous.get("progress") or {}).get("checkpoint_job_id")
                              or (previous.get("payload") or {}).get("_checkpoint_job_id")
                              or request.source_job_id)
            restored = self.repository.tail_checkpoints(root_job_id)
            if any(item["fingerprint"] != fingerprint for item in restored):
                raise ValueError("查询条件已改变，请重新探索而不是继续原任务。")
            prior_fingerprint = (previous.get("progress") or {}).get("query_fingerprint")
            if prior_fingerprint and prior_fingerprint != fingerprint:
                raise ValueError("查询条件已改变，请重新探索而不是继续原任务。")
        done = {item["destination"] for item in restored if item["status"] in {"matched", "no_match"}}
        results = [self.repository.fetch_tail_result(result_id) for item in restored
                   if item["destination"] in done for result_id in item["result_ids"]]
        restored_count = len(results)
        old_attempts = [item["attempt"] for item in restored]
        queue = build_tail_request_queue(candidates, old_attempts)
        edges = self.repository.fetch_tail_route_edge_codes(
            provider=provider, transfer=request.transfer, candidates=candidates,
            preferred_airlines=request.preferred_airlines,
        )
        ordered = self._prioritize_candidates_with_history(
            candidates, origin=request.origin, transfer=request.transfer,
            preferred_airlines=request.preferred_airlines, provider=provider, cabin=request.cabin,
            known_edge_codes=edges,
        )
        signatures = {code: negative_cache_signature(
            provider=provider, origin=request.origin, transfer=request.transfer, destination=code,
            departure_date=request.departure_date, cabin=request.cabin, passengers=request.passengers,
            preferred_airlines=request.preferred_airlines,
        ) for code in candidates}
        cached = set()
        if execution_mode not in {"refresh", "recheck"} and not request.ignore_cache:
            cached = self.repository.active_negative_cache_codes(signatures, now=datetime.now(timezone.utc).isoformat()) - done
        attempts = [{"destination": code, "status": "no_match", "phase": "negative_cache", "method": "negative_cache"}
                    for code in ordered if code in cached]
        for attempt in attempts:
            self._apply_queue_attempt(queue, attempt["destination"], attempt)
        pending = [code for code in ordered if code not in done and code not in cached]
        selected = pending[:request.max_candidates] if request.max_candidates else pending
        processed = set()
        warnings, errors = [], []
        baseline = None
        stop_reason = ""
        policy = {"known_route_edges": len(edges), "cache_skipped": len(cached),
                  "negative_cache_ttl_hours": 6, "ignore_cache": request.ignore_cache,
                  "ranking": "decayed_value_per_second", "exploration_fraction": .2}

        def publish(stage="running", message="正在查询", **extra):
            if progress_callback:
                progress_callback({**tail_progress_payload(stage, candidates, attempts, len(results), message,
                                                           queue=tail_queue_report(queue)),
                                   "checkpoint_job_id": root_job_id, "query_fingerprint": fingerprint,
                                   "candidate_policy": policy, "effective_candidates": candidates,
                                   "execution_mode": execution_mode, **extra})

        def should_stop():
            nonlocal stop_reason
            if cancel_checker and cancel_checker():
                stop_reason = "cancelled"
            elif monotonic() >= deadline:
                stop_reason = "time_budget"
            elif request.target_results and len({first_leg_key(r) for r in results[restored_count:]
                                                if (r.get("raw_payload") or {}).get("validation", {}).get("status") == "valid"}) >= request.target_results:
                stop_reason = "target_reached"
            return bool(stop_reason)

        def checkpoint(attempt, candidate_results):
            nonlocal stop_reason
            code = str(attempt.get("destination") or "").upper()
            if not code or code in processed or code not in selected:
                return
            attempt = dict(attempt)
            kept, rejected = filter_tail_results(candidate_results, max_price=request.max_price,
                                                departure_time_start=request.departure_time_start,
                                                departure_time_end=request.departure_time_end)
            kept, invalid = validate_tail_results_for_run(
                kept, origin=request.origin, transfer=request.transfer, departure_date=request.departure_date,
                preferred_airlines=request.preferred_airlines, cabin=request.cabin,
            )
            if request.expected_itinerary_key:
                kept = [r for r in kept if itinerary_key(r) == request.expected_itinerary_key]
            kept = select_tail_alternatives(kept, per_first_leg=request.alternatives_per_flight)
            for result in kept:
                annotate_quote(result, ttl_minutes=request.quote_ttl_minutes, rechecked=execution_mode == "recheck")
                result["raw_payload"].update(query_fingerprint=fingerprint, request=asdict(request), job_id=job_id)
                result["scraped_at"] = self.repository.now_iso()
            apply_baselines(kept, baseline)
            if kept:
                attempt.update(status="matched", phase="matched", price=min(r["price"] for r in kept))
            elif candidate_results:
                attempt.update(status="no_match", phase="validation_failed" if invalid else "filtered",
                               error="; ".join(str(a.get("error") or "") for a in rejected + invalid) or "未找到符合条件的原报价")
            if attempt.get("phase") == "parse_incomplete":
                attempt["status"] = "failed"  # Unknown is retryable, never a negative cache hit.
            savings = [r.get("savings_amount") for r in kept if r.get("savings_amount") is not None]
            attempt["savings_amount"] = max(savings) if savings else None
            entry = None
            if attempt.get("status") == "no_match" and attempt.get("phase") in {"route_absent", "no_via_transfer"}:
                now = datetime.now(timezone.utc)
                entry = {"signature": signatures[code], "provider": provider, "origin": request.origin,
                         "transfer": request.transfer, "destination": code, "departure_date": request.departure_date,
                         "cabin": request.cabin, "passengers": request.passengers,
                         "preferred_airlines": request.preferred_airlines, "phase": attempt["phase"],
                         "observed_at": now.isoformat(), "expires_at": (now + timedelta(hours=6)).isoformat()}
            record = {**attempt, "job_id": job_id, "queue_key": fingerprint, "provider": provider,
                      "origin": request.origin, "transfer": request.transfer, "departure_date": request.departure_date,
                      "cabin": request.cabin, "raw_payload": dict(attempt)}
            ids = self.repository.checkpoint_tail_candidate(root_job_id=root_job_id, fingerprint=fingerprint,
                                                           attempt=record, results=kept, negative_entry=entry,
                                                           recheck_result_id=request.source_result_id if execution_mode == "recheck" else 0,
                                                           positive_signature=signatures[code])
            for result, result_id in zip(kept, ids):
                result["id"] = result_id
            processed.add(code)
            attempts.append(attempt)
            results.extend(kept)
            self._apply_queue_attempt(queue, code, attempt)
            if attempt.get("phase") in {"rate_limited", "verification_timeout"}:
                stop_reason = str(attempt["phase"])
                if stop_reason == "rate_limited":
                    self.repository.set_tail_cooldown(provider, (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat())
            publish(message=f"已完成 {code}，保存 {len(kept)} 条方案")

        def on_event(event):
            stage = str(event.get("stage") or "running")
            code = str(event.get("destination") or "")
            if stage == "candidate_started":
                self._mark_queue_item(queue, code, status="running")
                publish(message=f"正在查询 {code}")
                return
            if stage in {"needs_verification", "verification_continued"}:
                self._mark_queue_item(queue, code, status="needs_verification" if stage == "needs_verification" else "running")
                publish(stage, str(event.get("message") or "继续查询"))
                return
            attempt = event.get("attempt")
            if attempt and attempt.get("status") not in {"started", "needs_verification"}:
                checkpoint(attempt, [r for r in event.get("results") or [] if r.get("destination") == code])

        async def bounded(coro, maximum=math.inf):
            remaining = min(maximum, deadline - monotonic())
            if remaining <= 0:
                coro.close()
                raise asyncio.TimeoutError()
            return await asyncio.wait_for(coro, timeout=remaining)

        publish("running", "任务已启动，已完成项以数据库检查点为准")
        try:
            if selected and not should_stop():
                credentials = self.config_manager.load_credentials(allow_missing=True)
                async def execute():
                    nonlocal baseline, stop_reason
                    async with self._tail_discovery_client(provider, credentials) as automation:
                        baseline_query = getattr(automation, "query_tail_baseline", None)
                        if baseline_query:
                            try:
                                baseline = await bounded(baseline_query(
                                    provider, origin=request.origin, transfer=request.transfer,
                                    departure_date=request.departure_date, cabin=request.cabin, passengers=request.passengers,
                                    departure_time_start=request.departure_time_start, departure_time_end=request.departure_time_end,
                                    preferred_airlines=request.preferred_airlines,
                                ), 150)
                            except Exception as exc:
                                warnings.append(f"基准价未获取：{exc}")
                                if looks_like_rate_limit(exc):
                                    stop_reason = "rate_limited"
                                    self.repository.set_tail_cooldown(provider, (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat())
                        for offset in range(0, len(selected), self.safe_batch_size):
                            if should_stop():
                                break
                            batch = selected[offset:offset + self.safe_batch_size]
                            discovery = await bounded(automation.discover_tail_routes(
                                provider, origin=request.origin, transfer=request.transfer,
                                departure_date=request.departure_date, cabin=request.cabin, passengers=request.passengers,
                                candidates=batch, preferred_airlines=request.preferred_airlines,
                                progress_callback=on_event, cancel_checker=should_stop,
                                verification_continue_checker=verification_continue_checker,
                            ))
                            # Backward-compatible runners may only publish at batch completion.
                            for attempt in discovery.get("attempts") or []:
                                checkpoint(attempt, [r for r in discovery.get("results") or []
                                                     if r.get("destination") == attempt.get("destination")])
                            errors.extend(discovery.get("errors") or [])
                            if should_stop():
                                break
                            if offset + self.safe_batch_size < len(selected):
                                cooldown = random.randint(5, 10) if provider == "feizhu" else random.randint(90, 180)
                                publish(message=f"批次冷却 {cooldown} 秒，已完成结果已保存")
                                while cooldown > 0 and not should_stop():
                                    await asyncio.sleep(min(1, cooldown))
                                    cooldown -= 1
                await bounded(execute())
        except asyncio.TimeoutError:
            stop_reason = "time_budget"
            warnings.append("达到运行时限，已保存完成的结果，可继续任务。")
        finally:
            self._reset_running_queue_items(queue)
        if not stop_reason and len(selected) < len(pending):
            stop_reason = "candidate_budget"
        elapsed = max(.001, monotonic() - started)
        summary = {
            "running": False, "saved": len(results), "results": sorted(results, key=lambda r: r["price"]),
            "newly_saved": len(results) - restored_count,
            "errors": warnings + errors, "cancelled": stop_reason == "cancelled", "stop_reason": stop_reason,
            "checkpoint_job_id": root_job_id, "query_fingerprint": fingerprint, "mode": request.mode,
            "effective_candidates": candidates,
            "execution_mode": execution_mode,
            "report": tail_batch_report(candidates, attempts, len(results)), "candidate_policy": policy,
            "baseline": baseline, "preferred_airlines": request.preferred_airlines,
            "metrics": {"elapsed_seconds": round(elapsed, 2), "queried_candidates": len(processed),
                        "effective_results_per_minute": round(sum(1 for r in results[restored_count:] if (r.get("savings_amount") or 0) > 0) * 60 / elapsed, 2)},
            "queue": {"requested": len(candidates), "completed": len(done) + len(processed),
                      "batch_size": self.safe_batch_size, "resume_key": root_job_id, "items": queue,
                      "report": tail_queue_report(queue)},
        }
        summary["progress"] = {**tail_progress_payload("finished", candidates, attempts, len(results),
                                  f"任务结束：{stop_reason or '扫描完成'}", queue=tail_queue_report(queue)),
                               "checkpoint_job_id": root_job_id, "query_fingerprint": fingerprint,
                               "effective_candidates": candidates, "execution_mode": execution_mode}
        # Diagnostic JSON is not used for recovery. SQLite is the source of truth.
        try:
            save_tail_queue(self.queue_file, root_job_id, {"attempts": attempts, "queue_items": queue})
        except OSError:
            LOGGER.warning("Failed to export diagnostic queue JSON", exc_info=True)
        return summary

    @asynccontextmanager
    async def _tail_discovery_client(
        self,
        provider_name: str,
        credentials: dict[str, dict[str, str]],
    ) -> AsyncIterator[Any]:
        if provider_name == "feizhu":
            from providers.tail_registry import create_tail_runner

            class DirectTailDiscoveryClient:
                def __init__(self, app_config: AppConfig) -> None:
                    self.app_config = app_config
                    self._runner = create_tail_runner(provider_name, app_config)

                async def discover_tail_routes(self, requested_provider: str, **kwargs: Any) -> dict[str, object]:
                    if requested_provider != provider_name:
                        raise RuntimeError(f"Unexpected provider {requested_provider!r}.")
                    return await self._runner.run(**kwargs)

                async def query_tail_baseline(self, requested_provider: str, **kwargs: Any) -> dict[str, object] | None:
                    if requested_provider != provider_name:
                        raise RuntimeError(f"Unexpected provider {requested_provider!r}.")
                    return await self._runner.query_baseline(**kwargs)

            yield DirectTailDiscoveryClient(self.config)
            return

        from browser_automation import BrowserAutomation

        async with BrowserAutomation(self.config, credentials) as automation:
            yield automation

    @staticmethod
    def _mark_queue_item(queue_items: list[dict[str, Any]], destination: str, **changes: Any) -> None:
        code = str(destination or "").upper()
        for item in queue_items:
            if item.get("destination") == code:
                item.update({key: value for key, value in changes.items() if value is not None})
                return

    @staticmethod
    def _apply_queue_attempt(queue_items: list[dict[str, Any]], destination: str, attempt: dict[str, Any]) -> None:
        code = str(destination or "").upper()
        for item in queue_items:
            if item.get("destination") != code:
                continue
            item.update(tail_queue_item_update_from_attempt(attempt))
            if attempt.get("status") == "failed" or attempt.get("phase") in {"validation_failed", "filtered"}:
                item["retry_count"] = int(item.get("retry_count") or 0) + 1
            return

    @staticmethod
    def _reset_running_queue_items(queue_items: list[dict[str, Any]]) -> None:
        for item in queue_items:
            if item.get("status") == "running":
                item["status"] = "pending"
                item["last_phase"] = "cancelled_before_query"

    def _prioritize_candidates_with_history(
        self,
        candidates: list[str],
        *,
        origin: str,
        transfer: str,
        preferred_airlines: list[str],
        provider: str | None = None,
        cabin: str | None = None,
        known_edge_codes: set[str] | None = None,
    ) -> list[str]:
        evidence = self.repository.tail_candidate_evidence(origin=origin, transfer=transfer, candidates=candidates)
        now = datetime.now(timezone.utc)
        stats = {code: {"yes": 0., "no": 0., "cost": [], "value": [], "seen": 0} for code in candidates}
        for item in evidence:
            code = item["destination"]
            if code not in stats:
                continue
            try:
                observed = datetime.fromisoformat(item["observed_at"].replace("Z", "+00:00"))
                age = max(0, (now - observed).total_seconds() / 86400)
            except (ValueError, TypeError):
                age = 30
            weight = .5 ** (age / 7)
            same_source = item["provider"] == provider and item["cabin"] == cabin
            if not same_source:
                # Other providers supply positive hints only, never negative exclusions.
                if item["status"] == "matched":
                    stats[code]["yes"] += weight * .15
                continue
            st = stats[code]
            st["seen"] += 1
            raw = item.get("raw_payload") or {}
            if item["status"] == "matched":
                st["yes"] += weight
            elif item["phase"] in {"route_absent", "no_via_transfer"}:
                st["no"] += weight
            if raw.get("elapsed_seconds"):
                st["cost"].append((float(raw["elapsed_seconds"]), weight))
            if raw.get("savings_amount") is not None:
                st["value"].append((max(0, float(raw["savings_amount"])), weight))
        def average(values, default):
            return sum(v*w for v,w in values) / sum(w for _,w in values) if values else default
        def score(code):
            st = stats[code]
            hit = (1 + st["yes"]) / (2 + st["yes"] + st["no"])
            edge_hint = 1.1 if code in (known_edge_codes or set()) else 1
            return hit * average(st["value"], 200) * edge_hint / max(1, average(st["cost"], 10 if provider == "feizhu" else 90))
        ranked = sorted(candidates, key=lambda c: (-score(c), candidates.index(c)))
        unseen = [c for c in candidates if not stats[c]["seen"]]
        # One exploration slot per five; second slot also explores for small budgets.
        if unseen and len(unseen) < len(candidates):
            ranked = [c for c in ranked if c not in unseen]
            for index, code in enumerate(unseen):
                ranked.insert(min(1 + index * 5, len(ranked)), code)
        return ranked


def negative_cache_signature(**payload: Any) -> str:
    return tail_queue_key(**payload)


def apply_tail_baseline(results: list[dict[str, Any]], baseline: dict[str, Any] | None) -> list[dict[str, Any]]:
    return apply_baselines(results, baseline)


def filter_tail_results(
    results: list[dict[str, Any]],
    *,
    max_price: float | None,
    departure_time_start: str,
    departure_time_end: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    filtered_attempts: list[dict[str, Any]] = []
    for result in results:
        reason = tail_result_filter_reason(
            result,
            max_price=max_price,
            departure_time_start=departure_time_start,
            departure_time_end=departure_time_end,
        )
        if not reason:
            kept.append(result)
            continue
        phase = "price_filtered" if reason.startswith("price ") else "departure_time_filtered"
        filtered_attempts.append(
            {
                "destination": result.get("destination"),
                "status": "no_match",
                "phase": phase,
                "method": "post_query_filter",
                "price": result.get("price"),
                "error": reason,
                "detail_source": (result.get("raw_payload") or {}).get("detail_source"),
                "detail_quality": (result.get("raw_payload") or {}).get("detail_quality"),
                "raw_payload": {
                    "filter_reason": reason,
                    "max_price": max_price,
                    "departure_time_start": departure_time_start,
                    "departure_time_end": departure_time_end,
                },
            }
        )
    return kept, filtered_attempts


def validate_tail_results_for_run(
    results: list[dict[str, Any]],
    *,
    origin: str,
    transfer: str,
    departure_date: str,
    preferred_airlines: list[str],
    cabin: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    for result in results:
        destination = str(result.get("destination") or "").upper()
        validation = validate_tail_result(
            result,
            origin=origin,
            transfer=transfer,
            destination=destination,
            departure_date=departure_date,
            preferred_airlines=preferred_airlines,
        )
        details = result.get("flight_details") or []
        if cabin:
            if any(leg.get("cabin") and not cabin_matches(leg["cabin"], cabin) for leg in details):
                validation["status"] = "invalid"
                validation["reasons"].append("实际舱位不符合查询条件")
            elif any(not leg.get("cabin") for leg in details):
                if validation["status"] != "invalid":
                    validation["status"] = "needs_review"
                validation["warnings"].append("实际舱位未确认")
        try:
            price_ok = math.isfinite(float(result.get("price") or 0)) and float(result.get("price") or 0) > 0
        except (ValueError, TypeError):
            price_ok = False
        if not price_ok:
            validation["status"] = "invalid"
            validation["reasons"].append("价格必须为有效正数")
        raw_payload = result.get("raw_payload")
        if not isinstance(raw_payload, dict):
            raw_payload = {}
            result["raw_payload"] = raw_payload
        raw_payload["validation"] = validation
        result["validation"] = validation
        raw_payload["pydantic_ai"] = pydantic_ai_status()
        try:
            structured = tail_itinerary_from_result(result)
            raw_payload["structured_itinerary"] = model_to_dict(structured)
            raw_payload.pop("structured_itinerary_error", None)
        except Exception as exc:
            raw_payload["structured_itinerary_error"] = str(exc)
        if validation.get("status") == "invalid":
            trace_path = raw_payload.get("trace_path")
            attempts.append(
                {
                    "destination": destination,
                    "status": "no_match",
                    "phase": "validation_failed",
                    "method": "post_query_validation",
                    "price": result.get("price"),
                    "error": (
                        "; ".join(validation.get("reasons") or ["航段硬校验未通过"])
                        + (f" Trace: {trace_path}" if trace_path else "")
                    ),
                    "detail_source": raw_payload.get("detail_source"),
                    "detail_quality": raw_payload.get("detail_quality"),
                    "trace_path": trace_path,
                    "raw_payload": {
                        "validation": validation,
                        "price": result.get("price"),
                        "trace_path": trace_path,
                    },
                }
            )
            continue
        if validation.get("status") == "needs_review":
            raw_payload["detail_quality"] = "needs_review"
            result["detail_quality"] = raw_payload["detail_quality"]
        kept.append(result)
    return kept, attempts


def tail_result_filter_reason(
    result: dict[str, Any],
    *,
    max_price: float | None,
    departure_time_start: str,
    departure_time_end: str,
) -> str:
    if max_price is not None:
        try:
            if float(result.get("price") or 0) > max_price:
                return f"price above max_price {max_price:.0f}"
        except Exception:
            return "price is not parseable"
    if departure_time_start or departure_time_end:
        first_time = first_tail_departure_time(result)
        if not first_time:
            return "departure time is missing"
        if departure_time_start and first_time < departure_time_start:
            return f"departure time {first_time} before {departure_time_start}"
        if departure_time_end and first_time > departure_time_end:
            return f"departure time {first_time} after {departure_time_end}"
    return ""


def first_tail_departure_time(result: dict[str, Any]) -> str:
    details = result.get("flight_details") or []
    if not isinstance(details, list) or not details:
        return ""
    first = details[0] if isinstance(details[0], dict) else {}
    value = str(first.get("departure_time") or "").strip()
    if not value:
        return ""
    # Accept "07:30", "2026-06-01 07:30", or ISO-like datetime strings.
    import re

    match = re.search(r"([01]?\d|2[0-3]):([0-5]\d)", value)
    if not match:
        return ""
    return f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"
