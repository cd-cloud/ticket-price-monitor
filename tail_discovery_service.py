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
    OPEN_DESTINATION,
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
from tail_history import history_context, route_observations, rank_candidates
from tail_call_budget import TailCallBudget, ProviderCallBudgetExceeded


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
        hybrid = request.search_scope == "hybrid"
        seed_candidates = list(candidates)
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
            filter_scope=request.filter_scope, validation_version=2,
            **({"search_scope": request.search_scope} if request.search_scope != "candidates" else {}),
            **({"max_followup_candidates": request.max_followup_candidates} if hybrid else {}),
        )
        restored = []
        prior_priority_trace = []
        if request.mode == "resume":
            previous = self.repository.fetch_tail_discovery_job(request.source_job_id)
            if not previous:
                raise ValueError("找不到原任务，不能继续。")
            previous_policy = ((previous.get("result") or {}).get("candidate_policy")
                               or (previous.get("progress") or {}).get("candidate_policy") or {})
            prior_priority_trace = list(previous_policy.get("priority_trace") or [])
            if str((previous.get("payload") or {}).get("search_scope") or "candidates") != request.search_scope:
                raise ValueError("目的地查询方式已改变，请重新探索而不是继续原任务。")
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
                raise ValueError("查询条件已改变，或校验规则已升级，请重新探索而不是继续原任务。")
            prior_fingerprint = (previous.get("progress") or {}).get("query_fingerprint")
            if prior_fingerprint and prior_fingerprint != fingerprint:
                raise ValueError("查询条件已改变，或校验规则已升级，请重新探索而不是继续原任务。")
        if hybrid:
            history_codes = self.repository.tail_history_destinations(origin=request.origin, transfer=request.transfer)
            candidates = [OPEN_DESTINATION] + list(dict.fromkeys(
                seed_candidates + [item["destination"] for item in restored if item["destination"] != OPEN_DESTINATION]
                + history_codes))[:500]
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
        learning_context = history_context(request) | {"execution_mode": execution_mode}
        call_budget = TailCallBudget(request.max_provider_calls)
        ordered = list(candidates)
        signatures = {code: negative_cache_signature(
            provider=provider, origin=request.origin, transfer=request.transfer, destination=code,
            departure_date=request.departure_date, cabin=request.cabin, passengers=request.passengers,
            preferred_airlines=request.preferred_airlines,
            filter_scope=request.filter_scope, validation_version=2,
        ) for code in candidates}
        cached = set()
        # CLI search coverage is not guaranteed. Old Feizhu "no route" entries
        # must not suppress the new server-side transfer-city queries either.
        if provider != "feizhu" and execution_mode not in {"refresh", "recheck"} and not request.ignore_cache:
            cached = self.repository.active_negative_cache_codes(signatures, now=datetime.now(timezone.utc).isoformat()) - done
        attempts = [{"destination": code, "status": "no_match", "phase": "negative_cache", "method": "negative_cache"}
                    for code in ordered if code in cached]
        attempts.extend(item["attempt"] for item in restored if item["destination"] in done)
        for attempt in attempts:
            self._apply_queue_attempt(queue, attempt["destination"], attempt)
        pending = [code for code in ordered if code not in done and code not in cached]
        dispatched = set()
        processed = set()
        warnings = (["开放目的地仅返回平台提供的部分方案，不代表全量；继续任务不会请求下一页，重新探索才会重新查询。"]
                    if request.search_scope in {"open", "hybrid"} else [])
        if hybrid:
            warnings.append("混合发现：先开放取样，再按历史证据定向补查；只在显式预算内扩展城市，不保证全量。")
        errors = []
        baseline = None
        stop_reason = ""
        if hybrid and OPEN_DESTINATION in done and len(done - {OPEN_DESTINATION}) >= request.max_followup_candidates:
            stop_reason = "followup_budget" if pending else ""
        policy = {"known_route_edges": len(edges), "cache_skipped": len(cached),
                  "negative_cache_ttl_hours": 6, "ignore_cache": request.ignore_cache,
                  "ranking": "contextual_history_v1", "exploration_fraction": .2}
        policy.update(search_scope=request.search_scope,
                      provider_calls_supported=provider == "feizhu",
                      progress_unit="开放批次＋定向城市" if hybrid else "开放查询批次" if request.search_scope == "open" else "目的地",
                      followup_limit=request.max_followup_candidates if hybrid else None,
                      priority_trace=prior_priority_trace, ranking_updates=0)

        def remaining_candidates():
            return [code for code in candidates if code not in done and code not in cached and code not in processed]

        def rank_pending():
            remaining = [code for code in remaining_candidates() if code != OPEN_DESTINATION]
            bundle = self.repository.tail_history_bundle(
                origin=request.origin, transfer=request.transfer,
                candidates=[code for code in candidates if code != OPEN_DESTINATION])
            ranked = rank_candidates(remaining, learning_context,
                **bundle,
                position=len({code for code in done | processed if code != OPEN_DESTINATION}))
            policy.update(next_candidates=ranked[:10], ranking_updates=policy["ranking_updates"] + 1,
                          known_route_edges=len({row["destination"] for row in bundle["edges"]}))
            return ranked

        def publish(stage="running", message="正在查询", **extra):
            policy["provider_calls"] = call_budget.snapshot()
            if progress_callback:
                progress_callback({**tail_progress_payload(stage, candidates, attempts, len(results), message,
                                                           queue=tail_queue_report(queue)),
                                   "checkpoint_job_id": root_job_id, "query_fingerprint": fingerprint,
                                   "candidate_policy": policy, "effective_candidates": candidates,
                                   "execution_mode": execution_mode, **extra})

        def should_stop():
            nonlocal stop_reason
            if stop_reason:
                return True  # A platform stop must not be replaced by a target/time budget.
            if cancel_checker and cancel_checker():
                stop_reason = "cancelled"
            elif monotonic() >= deadline:
                stop_reason = "time_budget"
            elif call_budget.exhausted:
                stop_reason = "provider_call_budget"
            elif request.target_results and len({first_leg_key(r) for r in results[restored_count:]
                                                if (r.get("raw_payload") or {}).get("validation", {}).get("status") == "valid"}) >= request.target_results:
                stop_reason = "target_reached"
            return bool(stop_reason)

        def prepare_results(candidate_results):
            kept, rejected = filter_tail_results(candidate_results, max_price=request.max_price,
                                                departure_time_start=request.departure_time_start,
                                                departure_time_end=request.departure_time_end)
            kept, invalid = validate_tail_results_for_run(
                kept, origin=request.origin, transfer=request.transfer, departure_date=request.departure_date,
                preferred_airlines=request.preferred_airlines, cabin=request.cabin,
                filter_scope=request.filter_scope,
            )
            if request.expected_itinerary_key:
                kept = [r for r in kept if itinerary_key(r) == request.expected_itinerary_key]
            if request.search_scope in {"open", "hybrid"}:
                # Preserve alternatives per actual destination, not only the first few cities.
                kept = [item for dest in dict.fromkeys(r["destination"] for r in kept)
                        for item in select_tail_alternatives([r for r in kept if r["destination"] == dest],
                                                            per_first_leg=request.alternatives_per_flight)]
            else:
                kept = select_tail_alternatives(kept, per_first_leg=request.alternatives_per_flight)
            return kept, rejected, invalid

        def checkpoint(attempt, candidate_results):
            nonlocal stop_reason
            code = str(attempt.get("destination") or "").upper()
            if not code or code in processed or code not in dispatched:
                return
            attempt = dict(attempt)
            provider_error = attempt.get("error")
            termination = attempt.get("stop_reason") or (
                attempt.get("phase") if attempt.get("phase") in
                {"rate_limited", "verification_timeout", "cancelled_before_query", "provider_call_budget"} else "")
            # Preserve control-plane failures independently of business filtering.
            if termination:
                attempt["stop_reason"] = termination
                if termination in {"rate_limited", "verification_timeout", "provider_call_budget"}:
                    stop_reason = termination
                    if termination == "rate_limited":
                        self.repository.set_tail_cooldown(
                            provider, (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat())
            kept, rejected, invalid = prepare_results(candidate_results)
            for result in kept:
                annotate_quote(result, ttl_minutes=request.quote_ttl_minutes, rechecked=execution_mode == "recheck")
                result["raw_payload"].update(query_fingerprint=fingerprint, request=asdict(request),
                                             job_id=job_id, filter_scope=request.filter_scope)
                result["scraped_at"] = self.repository.now_iso()
            apply_baselines(kept, baseline)
            observations = route_observations(candidate_results, request, kept)
            if kept:
                attempt.update(status="matched", phase="matched", price=min(r["price"] for r in kept))
            elif candidate_results:
                attempt.update(status="no_match", phase="validation_failed" if invalid else "filtered",
                               error="; ".join(str(a.get("error") or "") for a in rejected + invalid) or "未找到符合条件的原报价")
            if attempt.get("phase") == "parse_incomplete":
                attempt["status"] = "failed"  # Unknown is retryable, never a negative cache hit.
            if execution_mode == "recheck" and not kept and not termination:
                if attempt.get("phase") not in {"query_failed", "cancelled_before_query"}:
                    attempt.update(status="failed", phase="recheck_inconclusive",
                                   recheck_outcome="inconclusive",
                                   error="本次搜索未能确认原行程；结果覆盖不完整，不能据此判断下架，可重试。")
            if termination:
                attempt["phase"] = termination
                attempt["error"] = provider_error or attempt.get("error")
                if not kept:
                    attempt["status"] = "failed"
            savings = [r.get("savings_amount") for r in kept if r.get("savings_amount") is not None]
            attempt["savings_amount"] = max(savings) if savings else None
            entry = None
            if provider != "feizhu" and attempt.get("status") == "no_match" and attempt.get("phase") in {"route_absent", "no_via_transfer"}:
                now = datetime.now(timezone.utc)
                entry = {"signature": signatures[code], "provider": provider, "origin": request.origin,
                         "transfer": request.transfer, "destination": code, "departure_date": request.departure_date,
                         "cabin": request.cabin, "passengers": request.passengers,
                         "preferred_airlines": request.preferred_airlines, "phase": attempt["phase"],
                         "observed_at": now.isoformat(), "expires_at": (now + timedelta(hours=6)).isoformat()}
            attempt["learning_version"] = 1
            record = {**attempt, "job_id": job_id, "queue_key": fingerprint, "provider": provider,
                      "origin": request.origin, "transfer": request.transfer, "departure_date": request.departure_date,
                      "cabin": request.cabin, "raw_payload": dict(attempt)}
            ids = self.repository.checkpoint_tail_candidate(root_job_id=root_job_id, fingerprint=fingerprint,
                                                           attempt=record, results=kept, negative_entry=entry,
                                                           recheck_result_id=request.source_result_id if execution_mode == "recheck" else 0,
                                                           positive_signature=signatures.get(code),
                                                           route_observations=observations,
                                                           learning_context=learning_context)
            for result, result_id in zip(kept, ids):
                result["id"] = result_id
            processed.add(code)
            attempts.append(attempt)
            results.extend(kept)
            if hybrid and code == OPEN_DESTINATION:
                added = [row["destination"] for row in observations if row["quality"] == "valid"]
                for destination in dict.fromkeys(added):
                    if destination not in candidates and len(candidates) < 501:
                        candidates.append(destination)
                queue[:] = build_tail_request_queue(candidates, attempts)
            self._apply_queue_attempt(queue, code, attempt)
            publish(message=f"已完成 {code}，保存 {len(kept)} 条方案")

        def on_event(event):
            stage = str(event.get("stage") or "running")
            code = str(event.get("destination") or "")
            if stage == "candidate_started":
                self._mark_queue_item(queue, code, status="running")
                publish(message="正在进行开放目的地查询（非全量）" if code == OPEN_DESTINATION else f"正在查询 {code}")
                return
            if stage in {"needs_verification", "verification_continued"}:
                self._mark_queue_item(queue, code, status="needs_verification" if stage == "needs_verification" else "running")
                publish(stage, str(event.get("message") or "继续查询"))
                return
            attempt = event.get("attempt")
            if attempt and attempt.get("status") not in {"started", "needs_verification"}:
                checkpoint(attempt, [r for r in event.get("results") or []
                                     if code == OPEN_DESTINATION or r.get("destination") == code])

        async def bounded(coro, maximum=math.inf):
            remaining = min(maximum, deadline - monotonic())
            if remaining <= 0:
                coro.close()
                raise asyncio.TimeoutError()
            return await asyncio.wait_for(coro, timeout=remaining)

        rank_pending()
        publish("running", "任务已启动，已完成项以数据库检查点为准")
        try:
            if pending and not should_stop():
                credentials = self.config_manager.load_credentials(allow_missing=True)
                async def execute():
                    nonlocal baseline, stop_reason
                    recheck_flight = None
                    if execution_mode == "recheck" and request.source_result_id:
                        original = self.repository.fetch_tail_result(request.source_result_id)
                        legs = original.get("flight_details") or []
                        recheck_flight = legs[0].get("flight_no") if legs else None
                    async with self._tail_discovery_client(provider, credentials) as automation:
                        configure_budget = getattr(automation, "set_tail_call_budget", None)
                        if configure_budget:
                            configure_budget(call_budget)
                        baseline_query = getattr(automation, "query_tail_baseline", None)
                        if baseline_query and not (provider == "feizhu" and request.max_provider_calls == 1):
                            try:
                                baseline = await bounded(baseline_query(
                                    provider, origin=request.origin, transfer=request.transfer,
                                    departure_date=request.departure_date, cabin=request.cabin, passengers=request.passengers,
                                    departure_time_start=request.departure_time_start, departure_time_end=request.departure_time_end,
                                    preferred_airlines=request.preferred_airlines,
                                ), 150)
                            except Exception as exc:
                                warnings.append(f"基准价未获取：{exc}")
                                if isinstance(exc, ProviderCallBudgetExceeded):
                                    stop_reason = "provider_call_budget"
                                elif looks_like_rate_limit(exc):
                                    stop_reason = "rate_limited"
                                    self.repository.set_tail_cooldown(provider, (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat())
                        while remaining_candidates():
                            if should_stop():
                                break
                            remaining = remaining_candidates()
                            ranked = rank_pending()
                            if OPEN_DESTINATION in remaining:
                                batch = [OPEN_DESTINATION]
                            else:
                                completed_cities = len({code for code in done | processed if code != OPEN_DESTINATION})
                                slots = (request.max_followup_candidates - completed_cities if hybrid
                                         else request.max_candidates - len(processed) if request.max_candidates
                                         else len(remaining))
                                if slots <= 0:
                                    stop_reason = "followup_budget" if hybrid else "candidate_budget"
                                    break
                                batch = [row["destination"] for row in ranked[:min(slots, self.safe_batch_size)]]
                            dispatched.update(batch)
                            policy["priority_trace"].extend(
                                [{**row, "batch": policy["ranking_updates"], "job_id": job_id}
                                 for row in ranked if row["destination"] in batch])
                            before_processed = len(processed)
                            discovery = await bounded(automation.discover_tail_routes(
                                provider, origin=request.origin, transfer=request.transfer,
                                departure_date=request.departure_date, cabin=request.cabin, passengers=request.passengers,
                                candidates=batch, preferred_airlines=request.preferred_airlines,
                                filter_scope=request.filter_scope,
                                progress_callback=on_event, cancel_checker=should_stop,
                                verification_continue_checker=verification_continue_checker,
                                **({"search_scope": "open"} if batch == [OPEN_DESTINATION] else {}),
                                **({"result_filter": lambda rows: prepare_results(rows)[0],
                                    "max_baseline_queries": request.max_baseline_queries,
                                    "transport_no": recheck_flight} if provider == "feizhu" else {}),
                            ))
                            # Backward-compatible runners may only publish at batch completion.
                            for attempt in discovery.get("attempts") or []:
                                checkpoint(attempt, [r for r in discovery.get("results") or []
                                                     if attempt.get("destination") == OPEN_DESTINATION
                                                     or r.get("destination") == attempt.get("destination")])
                            errors.extend(discovery.get("errors") or [])
                            rank_pending()
                            if len(processed) == before_processed:
                                stop_reason = stop_reason or "runner_incomplete"
                                warnings.append("本批未产生完成检查点，停止以避免重复查询，可继续任务。")
                            if should_stop():
                                break
                            if remaining_candidates():
                                if OPEN_DESTINATION not in remaining_candidates():
                                    used_cities = len({c for c in done | processed if c != OPEN_DESTINATION})
                                    if hybrid and used_cities >= request.max_followup_candidates:
                                        stop_reason = "followup_budget"
                                        break
                                    if not hybrid and request.max_candidates and len(processed) >= request.max_candidates:
                                        stop_reason = "candidate_budget"
                                        break
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
        rank_pending()
        policy["provider_calls"] = call_budget.snapshot()
        elapsed = max(.001, monotonic() - started)
        summary = {
            "running": False, "saved": len(results), "results": sorted(results, key=lambda r: r["price"]),
            "newly_saved": len(results) - restored_count,
            "errors": warnings + errors, "cancelled": stop_reason == "cancelled", "stop_reason": stop_reason,
            "checkpoint_job_id": root_job_id, "query_fingerprint": fingerprint, "mode": request.mode,
            "effective_candidates": candidates,
            "execution_mode": execution_mode,
            "search_scope": request.search_scope,
            "filter_scope": request.filter_scope,
            "discovered_destinations": sorted({r["destination"] for r in results}),
            "report": tail_batch_report(candidates, attempts, len(results)), "candidate_policy": policy,
            "baseline": baseline, "preferred_airlines": request.preferred_airlines,
            "metrics": {"elapsed_seconds": round(elapsed, 2), "queried_candidates": len(processed),
                        "provider_calls": call_budget.snapshot() if provider == "feizhu" else None,
                        "valid_results_per_100_calls": (round(len({
                            itinerary_key(r) for r in results[restored_count:]
                            if (r.get("validation") or {}).get("status") == "valid"})
                            * 100 / call_budget.used, 2) if call_budget.used else None),
                        "effective_results_per_minute": round(sum(1 for r in results[restored_count:] if (r.get("savings_amount") or 0) > 0) * 60 / elapsed, 2)},
            "queue": {"requested": len(candidates), "completed": len(done) + len(processed),
                      "batch_size": self.safe_batch_size, "resume_key": root_job_id, "items": queue,
                      "report": tail_queue_report(queue)},
        }
        stop_message = {
            "followup_budget": "达到原任务定向补查城市上限；如需更多城市请重新探索",
            "provider_call_budget": "达到本轮平台调用预算，可继续未完成项",
            "runner_incomplete": "执行器未返回完成检查点，已停止，可继续任务",
        }.get(stop_reason, stop_reason)
        summary["progress"] = {**tail_progress_payload("finished", candidates, attempts, len(results),
                                  f"任务结束：{stop_message or ('开放查询完成（非全量）' if request.search_scope == 'open' else '扫描完成')}", queue=tail_queue_report(queue)),
                               "checkpoint_job_id": root_job_id, "query_fingerprint": fingerprint,
                               "effective_candidates": candidates, "execution_mode": execution_mode,
                               "candidate_policy": policy}
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

                def set_tail_call_budget(self, budget):
                    self._runner.call_budget = budget

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
        # Compatibility entry point; real runs supply the complete request context.
        from types import SimpleNamespace
        context = history_context(SimpleNamespace(
            provider=provider or "ctrip", origin=origin, transfer=transfer,
            departure_date=datetime.now(timezone.utc).date().isoformat(), cabin=cabin or "economy",
            passengers=1, filter_scope="first_leg", preferred_airlines=preferred_airlines,
            departure_time_start="", departure_time_end="", max_price=None))
        return [row["destination"] for row in rank_candidates(
            candidates, context, **self.repository.tail_history_bundle(
                origin=origin, transfer=transfer, candidates=candidates))]


def negative_cache_signature(**payload: Any) -> str:
    return tail_queue_key(**{"filter_scope": "first_leg", "validation_version": 2, **payload})


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
    filter_scope: str = "first_leg",
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
            filter_scope=filter_scope,
        )
        details = result.get("flight_details") or []
        if not isinstance(details, list):
            details = []
        selected_legs = details[:1] if filter_scope == "first_leg" else details
        if cabin and all(isinstance(leg, dict) for leg in selected_legs):
            if any(leg.get("cabin") and not cabin_matches(leg["cabin"], cabin) for leg in selected_legs):
                validation["status"] = "invalid"
                validation["reasons"].append("实际舱位不符合查询条件")
            elif any(not leg.get("cabin") for leg in selected_legs):
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
