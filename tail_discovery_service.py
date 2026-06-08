import asyncio
import logging
import random
import threading
from contextlib import asynccontextmanager
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

    async def run(
        self,
        payload: dict[str, Any],
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        cancel_checker: Callable[[], bool] | None = None,
        verification_continue_checker: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if not self.run_lock.acquire(blocking=False):
            return {"running": True, "saved": 0, "errors": ["A query run is already in progress."]}

        request = normalize_tail_discovery_request(payload)
        job_id = str(payload.get("_job_id") or "").strip() or None
        provider_name = request.provider
        origin = request.origin
        transfer = request.transfer
        departure_date = request.departure_date
        cabin = request.cabin
        passengers = request.passengers
        preferred_airlines = request.preferred_airlines
        candidate_profile = request.candidate_profile
        candidates = self._prioritize_candidates_with_history(
            request.candidates,
            origin=origin,
            transfer=transfer,
            preferred_airlines=preferred_airlines,
        )
        max_price = request.max_price
        departure_time_start = request.departure_time_start
        departure_time_end = request.departure_time_end

        queue_key = tail_queue_key(
            provider=provider_name,
            origin=origin,
            transfer=transfer,
            departure_date=departure_date,
            cabin=cabin,
            preferred_airlines=preferred_airlines,
            candidate_profile=candidate_profile,
            candidates=candidates,
            max_price=max_price,
            departure_time_start=departure_time_start,
            departure_time_end=departure_time_end,
        )
        queue_state = load_tail_queue(self.queue_file, queue_key)
        previous_attempts = [
            item for item in queue_state.get("attempts", []) if isinstance(item, dict) and item.get("destination")
        ]
        queue_items = build_tail_request_queue(
            candidates,
            previous_attempts,
            queue_state.get("queue_items") if isinstance(queue_state.get("queue_items"), list) else [],
        )
        matched_codes = {
            str(item.get("destination") or "").upper()
            for item in previous_attempts
            if item.get("status") == "matched"
        }
        failed_attempt_codes = {
            str(item.get("destination") or "").upper()
            for item in previous_attempts
            if item.get("status") == "failed"
        }
        failed = set(queue_state.get("failed", [])) | failed_attempt_codes
        completed = (set(queue_state.get("completed", [])) & matched_codes) - failed
        candidates_to_run = [item["destination"] for item in queue_items if item["destination"] not in completed]

        warnings: list[str] = []
        errors: list[str] = []
        if len(candidates_to_run) > self.safe_batch_size:
            warnings.append(
                f"Safe queue is enabled: {len(candidates_to_run)} remaining candidate(s) will be queried in batches of "
                f"{self.safe_batch_size}; candidates are no longer skipped."
            )
        if not origin or not transfer or not departure_date:
            errors.append("origin, transfer and departure_date are required.")
        if not candidates:
            errors.append("No valid candidate destinations selected.")
        if provider_name not in self.config.providers or not self.config.providers[provider_name].enabled:
            errors.append(f"{provider_name} provider is not enabled.")
        if errors:
            self.run_lock.release()
            return {"running": False, "saved": 0, "results": [], "errors": errors}

        if not candidates_to_run:
            attempts = list(queue_state.get("attempts", []))
            report = tail_batch_report(candidates, attempts, 0)
            if progress_callback:
                progress_callback(
                    tail_progress_payload(
                        "finished",
                        candidates,
                        attempts,
                        0,
                        "没有待查询的目的地。",
                        queue=tail_queue_report(queue_items),
                    )
                )
            self.run_lock.release()
            return {
                "running": False,
                "saved": 0,
                "results": [],
                "errors": warnings,
                "report": report,
                "queue": {
                    "requested": len(candidates),
                    "remaining_before_run": 0,
                    "completed": len(completed),
                    "batch_size": self.safe_batch_size,
                    "batches": 0,
                    "resume_key": queue_key,
                    "items": queue_items,
                    "report": tail_queue_report(queue_items),
                },
                "preferred_airlines": preferred_airlines,
            }

        try:
            credentials = self.config_manager.load_credentials(allow_missing=True)
            all_results: list[dict[str, Any]] = []
            all_errors: list[str] = []
            attempts: list[dict[str, Any]] = []
            batches = [
                candidates_to_run[index : index + self.safe_batch_size]
                for index in range(0, len(candidates_to_run), self.safe_batch_size)
            ]
            if progress_callback:
                progress_callback(
                    tail_progress_payload(
                        "running",
                        candidates,
                        attempts,
                        0,
                        f"准备查询 {len(candidates_to_run)} 个剩余目的地，共 {len(batches)} 批。",
                        current_batch=0,
                        total_batches=len(batches),
                        queue=tail_queue_report(queue_items),
                    )
                )

            async with self._tail_discovery_client(provider_name, credentials) as automation:
                for batch_index, batch in enumerate(batches, start=1):
                    if cancel_checker and cancel_checker():
                        all_errors.append("Tail discovery was cancelled before the next batch.")
                        break

                    def batch_progress(event: dict[str, object], *, batch_index: int = batch_index) -> None:
                        if not progress_callback:
                            return
                        in_flight_attempts = attempts + list(event.get("attempts") or [])
                        in_flight_saved = len(all_results) + len(list(event.get("results") or []))
                        destination = str(event.get("destination") or "")
                        stage = str(event.get("stage") or "running")
                        message = str(event.get("message") or "")
                        if destination:
                            self._mark_queue_item(
                                queue_items,
                                destination,
                                status="needs_verification" if stage == "needs_verification" else "running",
                                last_phase=stage,
                            )
                        if stage == "needs_verification":
                            progress_callback(
                                tail_progress_payload(
                                    "needs_verification",
                                    candidates,
                                    in_flight_attempts,
                                    in_flight_saved,
                                    message or "携程需要人工验证，请完成验证后点击继续查询。",
                                    current_batch=batch_index,
                                    total_batches=len(batches),
                                    active_candidates=batch,
                                    queue=tail_queue_report(queue_items),
                                )
                            )
                            return
                        progress_callback(
                            tail_progress_payload(
                                "running",
                                candidates,
                                in_flight_attempts,
                                in_flight_saved,
                                f"已完成目的地 {destination}，继续执行第 {batch_index}/{len(batches)} 批。",
                                current_batch=batch_index,
                                total_batches=len(batches),
                                active_candidates=batch,
                                queue=tail_queue_report(queue_items),
                            )
                        )

                    if progress_callback:
                        for code in batch:
                            self._mark_queue_item(queue_items, code, status="running", last_phase="batch_started")
                        progress_callback(
                            tail_progress_payload(
                                "running",
                                candidates,
                                attempts,
                                len(all_results),
                                f"正在查询第 {batch_index}/{len(batches)} 批：{', '.join(batch)}。",
                                current_batch=batch_index,
                                total_batches=len(batches),
                                active_candidates=batch,
                                queue=tail_queue_report(queue_items),
                            )
                        )

                    discovery_kwargs = {
                        "origin": origin,
                        "transfer": transfer,
                        "departure_date": departure_date,
                        "cabin": cabin,
                        "passengers": passengers,
                        "candidates": batch,
                        "preferred_airlines": preferred_airlines,
                        "progress_callback": batch_progress,
                        "cancel_checker": cancel_checker,
                        "verification_continue_checker": verification_continue_checker,
                    }
                    discovery = await automation.discover_tail_routes(provider_name, **discovery_kwargs)
                    filtered_results, filtered_attempts = filter_tail_results(
                        list(discovery.get("results") or []),
                        max_price=max_price,
                        departure_time_start=departure_time_start,
                        departure_time_end=departure_time_end,
                    )
                    validated_results, validation_attempts = validate_tail_results_for_run(
                        filtered_results,
                        origin=origin,
                        transfer=transfer,
                        departure_date=departure_date,
                        preferred_airlines=preferred_airlines,
                    )
                    filtered_results = validated_results
                    filtered_attempts.extend(validation_attempts)
                    filtered_by_destination = {
                        str(attempt.get("destination") or "").upper(): attempt
                        for attempt in filtered_attempts
                        if attempt.get("destination")
                    }
                    batch_attempts = []
                    replaced_destinations = set()
                    for attempt in list(discovery.get("attempts") or []):
                        code = str(attempt.get("destination") or "").upper()
                        if code in filtered_by_destination:
                            batch_attempts.append(filtered_by_destination[code])
                            replaced_destinations.add(code)
                            continue
                        batch_attempts.append(attempt)
                    for code, attempt in filtered_by_destination.items():
                        if code not in replaced_destinations:
                            batch_attempts.append(attempt)
                    attempts.extend(batch_attempts)
                    for attempt in batch_attempts:
                        code = str(attempt.get("destination") or "").upper()
                        if not code:
                            continue
                        self._apply_queue_attempt(queue_items, code, attempt)
                        self.repository.insert_tail_discovery_attempt(
                            TailDiscoveryAttempt(
                                job_id=job_id,
                                queue_key=queue_key,
                                provider=provider_name,
                                origin=origin,
                                transfer=transfer,
                                destination=code,
                                departure_date=departure_date,
                                cabin=cabin,
                                status=str(attempt.get("status") or "unknown"),
                                phase=attempt.get("phase"),
                                method=attempt.get("method"),
                                price=attempt.get("price"),
                                detail_source=attempt.get("detail_source"),
                                detail_quality=attempt.get("detail_quality"),
                                error=attempt.get("error"),
                                raw_payload=attempt,
                            )
                        )
                        if attempt.get("status") == "matched":
                            completed.add(code)
                            failed.discard(code)
                        elif attempt.get("status") in {"no_match", "failed"}:
                            if attempt.get("status") == "failed":
                                failed.add(code)
                            else:
                                failed.discard(code)

                    save_tail_queue(
                        self.queue_file,
                        queue_key,
                        {
                            "key": queue_key,
                            "origin": origin,
                            "transfer": transfer,
                            "provider": provider_name,
                            "departure_date": departure_date,
                            "cabin": cabin,
                            "preferred_airlines": preferred_airlines,
                            "candidate_profile": candidate_profile,
                            "max_price": max_price,
                            "departure_time_start": departure_time_start,
                            "departure_time_end": departure_time_end,
                            "candidates": candidates,
                            "completed": sorted(completed),
                            "failed": sorted(failed),
                            "attempts": (previous_attempts + attempts)[-300:],
                            "queue_items": queue_items,
                            "queue_report": tail_queue_report(queue_items),
                        },
                    )
                    all_results.extend(filtered_results)
                    all_errors.extend(
                        f"batch {batch_index}/{len(batches)}: {error}"
                        for error in list(discovery.get("errors") or [])
                    )
                    if any("verification" in error.lower() for error in all_errors):
                        break
                    if cancel_checker and cancel_checker():
                        all_errors.append("Tail discovery was cancelled after current batch.")
                        break
                    if progress_callback:
                        progress_callback(
                            tail_progress_payload(
                                "running",
                                candidates,
                                attempts,
                                len(all_results),
                                f"第 {batch_index}/{len(batches)} 批完成。",
                                current_batch=batch_index,
                                total_batches=len(batches),
                                queue=tail_queue_report(queue_items),
                            )
                        )

                    if batch_index < len(batches):
                        if provider_name == "feizhu":
                            batch_cooldown_min = 5
                            batch_cooldown_max = 10
                        else:
                            batch_cooldown_min = 90 if batch_index < 3 else 180
                            batch_cooldown_max = 180 if batch_index < 3 else 300
                        batch_cooldown = random.randint(batch_cooldown_min, batch_cooldown_max)
                        LOGGER.info(
                            "tail discovery batch cooldown: %.1fs before batch %s/%s",
                            batch_cooldown,
                            batch_index + 1,
                            len(batches),
                        )
                        if progress_callback:
                            progress_callback(
                                tail_progress_payload(
                                    "running",
                                    candidates,
                                    attempts,
                                    len(all_results),
                                    f"批次冷却中，{batch_cooldown} 秒后继续第 {batch_index + 1}/{len(batches)} 批...",
                                    current_batch=batch_index,
                                    total_batches=len(batches),
                                    queue=tail_queue_report(queue_items),
                                )
                            )
                        remaining_cooldown = batch_cooldown
                        while remaining_cooldown > 0:
                            if cancel_checker and cancel_checker():
                                all_errors.append("Tail discovery was cancelled during batch cooldown.")
                                break
                            await asyncio.sleep(min(1, remaining_cooldown))
                            remaining_cooldown -= 1
                        if cancel_checker and cancel_checker():
                            break

            saved = 0
            results = all_results
            results.sort(key=lambda item: float(item.get("price", 0) or 0))
            for result in results:
                result["scraped_at"] = self.repository.now_iso()
                self.repository.insert_tail_discovery_result(TailDiscoveryResult.from_mapping(result))
                saved += 1
            cancelled = bool(cancel_checker and cancel_checker())
            if cancelled:
                self._reset_running_queue_items(queue_items)

            summary = {
                "running": False,
                "saved": saved,
                "results": results,
                "errors": warnings + all_errors,
                "cancelled": cancelled,
                "report": tail_batch_report(candidates, attempts, saved),
                "queue": {
                    "requested": len(candidates),
                    "remaining_before_run": len(candidates_to_run),
                    "completed": len(completed),
                    "batch_size": self.safe_batch_size,
                    "batches": len(batches),
                    "resume_key": queue_key,
                    "items": queue_items,
                    "report": tail_queue_report(queue_items),
                },
                "preferred_airlines": preferred_airlines,
            }
            summary["progress"] = tail_progress_payload(
                "finished",
                candidates,
                attempts,
                saved,
                f"查询完成，本次保存 {saved} 条结果。",
                current_batch=len(batches),
                total_batches=len(batches),
                queue=tail_queue_report(queue_items),
            )
            return summary
        finally:
            self.run_lock.release()

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
    ) -> list[str]:
        stats = self.repository.fetch_tail_destination_history(
            origin=origin,
            transfer=transfer,
            candidates=candidates,
        )
        original_order = {code: index for index, code in enumerate(candidates)}
        preferred_bonus = 0.25 if preferred_airlines else 0.0

        def score(code: str) -> tuple[float, int, str]:
            history = stats.get(code, {})
            attempts = int(history.get("attempts") or 0)
            matched = int(history.get("matched") or 0)
            failed = int(history.get("failed") or 0)
            no_match = int(history.get("no_match") or 0)
            filtered = int(history.get("filtered") or 0)
            best_price = history.get("best_price")
            hit_rate = matched / attempts if attempts else 0
            reliability = hit_rate * 8 - failed * 1.5 - no_match * 0.35 - filtered * 0.2
            price_bonus = max(0, (3000 - float(best_price or 3000)) / 1000) if best_price else 0
            freshness_penalty = min(attempts, 6) * 0.05 if matched == 0 else 0
            return (-(reliability + price_bonus + preferred_bonus - freshness_penalty), original_order.get(code, 999), code)

        return sorted(candidates, key=score)


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
        filtered_attempts.append(
            {
                "destination": result.get("destination"),
                "status": "no_match",
                "phase": "filtered",
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
            raw_payload["detail_quality"] = raw_payload.get("detail_quality") or "needs_review"
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
