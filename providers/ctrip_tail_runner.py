"""Coordinator for Ctrip tail-discovery browser runs."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from config_manager import RouteQuery
from providers import ctrip
from providers.response_capture import attach_response_listeners


LOGGER = logging.getLogger(__name__)


class CtripTailDiscoveryRunner:
    """Runs the long Ctrip tail-discovery flow using BrowserAutomation primitives."""

    CONTEXT_REBUILD_INTERVAL = 8

    def __init__(self, automation: Any) -> None:
        self.automation = automation
        self.runtime = automation.provider_runtime

    def __getattr__(self, name: str) -> Any:
        return getattr(self.automation, name)

    async def _open_tail_context(
        self,
        provider: Any,
        *,
        backend_name: str | None = None,
    ) -> tuple[Any, Any, Any, list[dict]]:
        page, context = await self._new_page(provider, backend_name=backend_name)
        session_metadata = self._session_metadata_for_context(context)
        response_store: list[dict] = []
        await attach_response_listeners(page, response_store)
        return page, context, session_metadata, response_store

    def _backend_names_after(self, provider: Any, current_backend: str | None) -> list[str]:
        names = self._backend_names_for_provider(provider)
        current = (current_backend or "").strip().lower()
        if not current or current not in names:
            return names
        return names[names.index(current) + 1 :]

    async def _human_interlude(self, page: Any, provider: Any) -> None:
        """Simulate human browsing between two consecutive queries."""
        if random.random() < 0.35:
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(random.randint(2000, 5000))
                await page.evaluate("window.scrollTo(0, 0)")
                LOGGER.info("tail discovery human interlude: scrolled results page")
            except Exception:
                pass

    async def _retry_candidate_with_backend_fallback(
        self,
        *,
        provider: Any,
        route: RouteQuery,
        destination: str,
        page: Any,
        context: Any,
        response_store: list[dict],
        session_metadata: Any,
        trace_started: bool,
        origin: str,
        transfer: str,
        departure_date: str,
        attempt: dict[str, object],
        query_error: Exception,
    ) -> tuple[Exception | None, Any, Any, list[dict], Any, bool, bool]:
        backend_attempts = attempt.setdefault("backend_attempts", [])
        if isinstance(backend_attempts, list) and not backend_attempts:
            backend_attempts.append(f"{session_metadata.browser_backend}: {query_error}")

        last_error: Exception = query_error
        for next_backend in self._backend_names_after(provider, session_metadata.browser_backend):
            try:
                LOGGER.info(
                    "tail discovery retrying route=%s destination=%s with backend=%s",
                    route.route_key,
                    destination,
                    next_backend,
                )
                next_page, next_context, next_session_metadata, next_response_store = await self._open_tail_context(
                    provider,
                    backend_name=next_backend,
                )
            except Exception as exc:
                last_error = exc
                if isinstance(backend_attempts, list):
                    backend_attempts.append(f"{next_backend}: open failed: {exc}")
                LOGGER.info("tail discovery backend open failed backend=%s error=%s", next_backend, exc)
                continue

            await self._stop_ctrip_tail_trace(context, trace_started=trace_started)
            await self._close_context_quietly(context)
            page = next_page
            context = next_context
            session_metadata = next_session_metadata
            response_store = next_response_store
            trace_started = await self._start_ctrip_tail_trace(
                context,
                origin=origin,
                transfer=transfer,
                destination=destination,
                departure_date=departure_date,
            )
            try:
                await asyncio.wait_for(
                    ctrip.query_tail_candidate_offer(
                        self.runtime,
                        provider,
                        route,
                        page,
                        response_store,
                        origin_already_set=False,
                    ),
                    timeout=self.TAIL_DISCOVERY_CANDIDATE_TIMEOUT_SECONDS,
                )
                await self._ensure_ctrip_results_date(page, route, provider.timeout_ms)
                attempt["phase"] = "results_loaded"
                if isinstance(backend_attempts, list):
                    backend_attempts.append(f"{session_metadata.browser_backend}: ok")
                return None, page, context, response_store, session_metadata, trace_started, True
            except Exception as exc:
                last_error = exc
                if isinstance(exc, asyncio.TimeoutError):
                    last_error = RuntimeError(
                        f"native form search timed out after {self.TAIL_DISCOVERY_CANDIDATE_TIMEOUT_SECONDS}s"
                    )
                if isinstance(backend_attempts, list):
                    backend_attempts.append(f"{session_metadata.browser_backend}: {last_error}")
                LOGGER.info(
                    "tail discovery retry failed route=%s backend=%s error=%s",
                    route.route_key,
                    session_metadata.browser_backend,
                    last_error,
                )
                attempt["phase"] = "form_search_failed"
        return last_error, page, context, response_store, session_metadata, trace_started, False

    async def run(
        self,
        *,
        origin: str,
        transfer: str,
        departure_date: str,
        cabin: str,
        passengers: int,
        candidates: list[str],
        preferred_airlines: list[str] | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
        cancel_checker: Callable[[], bool] | None = None,
        verification_continue_checker: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        provider = self.app_config.providers["ctrip"]
        page, context, session_metadata, response_store = await self._open_tail_context(provider)
        results: list[dict[str, object]] = []
        errors: list[str] = []
        attempts: list[dict[str, object]] = []

        try:
            normalized_origin = origin.upper()
            normalized_transfer = transfer.upper()
            normalized_candidates = [
                code.upper()
                for code in candidates
                if code and code.upper() not in {normalized_origin, normalized_transfer}
            ]
            unique_candidates = list(dict.fromkeys(normalized_candidates))
            random.shuffle(unique_candidates)
            origin_form_ready = False
            context_had_error = False
            for index, destination in enumerate(unique_candidates):
                needs_rebuild = (
                    index > 0
                    and index % self.CONTEXT_REBUILD_INTERVAL == 0
                ) or (index > 0 and context_had_error)
                if needs_rebuild:
                    LOGGER.info(
                        "tail discovery rebuilding browser context after %s candidates (error=%s)",
                        index,
                        context_had_error,
                    )
                    await self._close_context_quietly(context)
                    page, context, session_metadata, response_store = await self._open_tail_context(
                        provider,
                        backend_name=session_metadata.browser_backend,
                    )
                    origin_form_ready = False
                    context_had_error = False

                if cancel_checker and cancel_checker():
                    errors.append("Tail discovery was cancelled before the next destination.")
                    break
                attempt: dict[str, object] = {
                    "destination": destination,
                    "status": "started",
                    "method": "native_form",
                    "phase": "preparing_form",
                    "error": None,
                }
                if await self._ctrip_verification_detected(page):
                    attempt["status"] = "needs_verification"
                    attempt["phase"] = "verification_detected"
                    attempt["error"] = "Ctrip verification is waiting for manual completion in the browser."
                    if progress_callback:
                        progress_callback(
                            {
                                "stage": "needs_verification",
                                "destination": destination,
                                "message": "Ctrip needs manual verification. Complete it in the browser, then continue from the local page.",
                                "attempts": [attempt],
                            }
                        )
                    continued = await self._wait_for_ctrip_verification_continue(
                        page,
                        cancel_checker=cancel_checker,
                        verification_continue_checker=verification_continue_checker,
                    )
                    if not continued:
                        attempt["status"] = "failed"
                        attempt["phase"] = "verification_timeout"
                        attempt["error"] = "Ctrip verification was not continued before timeout or task was cancelled."
                        attempts.append(attempt)
                        errors.append(f"{normalized_origin}->{destination}: Ctrip verification was not continued.")
                        break
                    try:
                        await self._save_storage_state_if_allowed(context, provider)
                    except Exception:
                        LOGGER.info("failed to save ctrip storage state after verification", exc_info=True)
                    attempt["status"] = "started"
                    attempt["phase"] = "verification_continued"
                    attempt["error"] = None
                route = RouteQuery(
                    origin=normalized_origin,
                    destination=destination,
                    departure_date=departure_date,
                    route_type="one_way",
                    cabin=cabin,
                    transfer_policy="any",
                    passengers=passengers,
                    providers=["ctrip"],
                )
                response_store.clear()
                trace_path: Path | None = None
                trace_started = await self._start_ctrip_tail_trace(
                    context,
                    origin=normalized_origin,
                    transfer=normalized_transfer,
                    destination=destination,
                    departure_date=departure_date,
                )
                try:
                    query_error: Exception | None = None
                    try:
                        await asyncio.wait_for(
                            ctrip.query_tail_candidate_offer(
                                self.runtime,
                                provider,
                                route,
                                page,
                                response_store,
                                origin_already_set=origin_form_ready,
                            ),
                            timeout=self.TAIL_DISCOVERY_CANDIDATE_TIMEOUT_SECONDS,
                        )
                        origin_form_ready = True
                        await self._ensure_ctrip_results_date(page, route, provider.timeout_ms)
                        attempt["phase"] = "results_loaded"
                        attempt["backend_attempts"] = [f"{session_metadata.browser_backend}: ok"]
                    except Exception as exc:
                        query_error = exc
                        if isinstance(exc, asyncio.TimeoutError):
                            query_error = RuntimeError(
                                f"native form search timed out after {self.TAIL_DISCOVERY_CANDIDATE_TIMEOUT_SECONDS}s"
                            )
                        LOGGER.info(
                            "tail discovery native form search skipped route=%s error=%s",
                            route.route_key,
                            exc,
                        )
                        attempt["phase"] = "form_search_failed"
                    if query_error is not None:
                        (
                            query_error,
                            page,
                            context,
                            response_store,
                            session_metadata,
                            trace_started,
                            origin_form_ready,
                        ) = await self._retry_candidate_with_backend_fallback(
                            provider=provider,
                            route=route,
                            destination=destination,
                            page=page,
                            context=context,
                            response_store=response_store,
                            session_metadata=session_metadata,
                            trace_started=trace_started,
                            origin=normalized_origin,
                            transfer=normalized_transfer,
                            departure_date=departure_date,
                            attempt=attempt,
                            query_error=query_error,
                        )
                    if query_error and "date mismatch" in str(query_error).lower():
                        context_had_error = True
                        attempt["date_evidence"] = await self._ctrip_results_date_evidence(page, route)
                        await self._browser_use_assist_page(
                            page,
                            "ctrip_tail_date_mismatch",
                            "Ctrip tail-discovery result page is showing a different date than requested. Diagnose the date state only; do not bypass verification.",
                            {
                                "route_key": route.route_key,
                                "expected_departure_date": departure_date,
                                "date_evidence": attempt.get("date_evidence"),
                                "error": str(query_error),
                            },
                        )
                        attempt["status"] = "failed"
                        attempt["phase"] = "date_mismatch"
                        attempt["error"] = str(query_error)
                        trace_path = await self._stop_ctrip_tail_trace(context, trace_started=trace_started)
                        if trace_path:
                            attempt["trace_path"] = str(trace_path)
                        attempts.append(attempt)
                        errors.append(
                            f"{normalized_origin}->{destination}: Ctrip result page did not switch to {departure_date}. "
                            f"{query_error}"
                        )
                        continue
                    if query_error and not ctrip.is_ctrip_results_url(page.url):
                        context_had_error = True
                        attempt["status"] = "failed"
                        attempt["error"] = str(query_error)
                        trace_path = await self._stop_ctrip_tail_trace(context, trace_started=trace_started)
                        if trace_path:
                            attempt["trace_path"] = str(trace_path)
                        attempts.append(attempt)
                        errors.append(
                            f"{normalized_origin}->{destination}: search did not reach a Ctrip results page. "
                            f"{query_error}"
                        )
                        continue
                    if await self._ctrip_verification_detected(page):
                        context_had_error = True
                        attempt["status"] = "failed"
                        attempt["error"] = "verification detected"
                        trace_path = await self._stop_ctrip_tail_trace(context, trace_started=trace_started)
                        if trace_path:
                            attempt["trace_path"] = str(trace_path)
                        attempts.append(attempt)
                        errors.append(
                            f"{normalized_origin}->{destination}: Ctrip verification was detected. "
                            "Stopped the remaining candidates."
                        )
                        break
                    await page.wait_for_timeout(1200)
                    await self._warm_ctrip_tail_result_data(page, response_store)
                    if not self._response_store_has_flight_itineraries(response_store):
                        attempt["phase"] = "warming_results"
                        await page.wait_for_timeout(1800)
                        await self._warm_ctrip_tail_result_data(page, response_store)
                    if await self._ctrip_verification_detected(page):
                        artifact = await self._dump_ctrip_tail_diagnostics(
                            page,
                            response_store=response_store,
                            origin=normalized_origin,
                            transfer=normalized_transfer,
                            destination=destination,
                            departure_date=departure_date,
                        )
                        attempt["status"] = "needs_verification"
                        attempt["phase"] = "verification_detected"
                        attempt["error"] = "Ctrip verification is waiting for manual completion in the browser."
                        if artifact:
                            attempt["diagnostic_artifact"] = artifact
                            attempt["error"] = f"{attempt['error']} Diagnostic artifact: {artifact.get('json_path')}"
                        if progress_callback:
                            progress_callback(
                                {
                                    "destination": destination,
                                    "attempts": [dict(attempt)],
                                    "results": [],
                                    "stage": "needs_verification",
                                    "message": "Ctrip needs manual verification. Complete it in the browser, then continue from the local page.",
                                }
                            )
                        continued = await self._wait_for_ctrip_verification_continue(
                            page,
                            cancel_checker=cancel_checker,
                            verification_continue_checker=verification_continue_checker,
                        )
                        if not continued:
                            attempt["status"] = "failed"
                            attempt["phase"] = "verification_timeout"
                            attempt["error"] = "Ctrip verification was not continued before timeout or task was cancelled."
                            trace_path = await self._stop_ctrip_tail_trace(context, trace_started=trace_started)
                            if trace_path:
                                attempt["trace_path"] = str(trace_path)
                            attempts.append(attempt)
                            errors.append(f"{normalized_origin}->{destination}: Ctrip verification was not continued.")
                            break
                        try:
                            await self._save_storage_state_if_allowed(context, provider)
                        except Exception:
                            LOGGER.info("failed to save ctrip storage state after verification", exc_info=True)
                        attempt["status"] = "started"
                        attempt["phase"] = "verification_continued"
                        attempt["error"] = None
                        response_store.clear()
                        await page.wait_for_timeout(1500)
                        await self._warm_ctrip_tail_result_data(page, response_store)
                    attempt["date_evidence"] = await self._ctrip_results_date_evidence(page, route)
                    match = self._extract_ctrip_tail_match_from_network(
                        response_store,
                        origin=normalized_origin,
                        transfer=normalized_transfer,
                        destination=destination,
                        preferred_airlines=preferred_airlines,
                    )
                    if match is None:
                        attempt["phase"] = "parsing_page"
                        match = await self._extract_ctrip_tail_match_from_page(
                            page,
                            origin=normalized_origin,
                            transfer=normalized_transfer,
                            destination=destination,
                            departure_date=departure_date,
                            cabin=cabin,
                            preferred_airlines=preferred_airlines,
                        )
                    if match is None:
                        artifact = await self._dump_ctrip_tail_diagnostics(
                            page,
                            response_store=response_store,
                            origin=normalized_origin,
                            transfer=normalized_transfer,
                            destination=destination,
                            departure_date=departure_date,
                        )
                        if artifact:
                            attempt["diagnostic_artifact"] = artifact
                        diagnostics = await self._extract_ctrip_tail_page_diagnostics(
                            page,
                            response_store=response_store,
                            origin=normalized_origin,
                            transfer=normalized_transfer,
                            destination=destination,
                            preferred_airlines=preferred_airlines,
                        )
                        attempt["status"] = "no_match"
                        attempt["phase"] = "no_transfer_match"
                        attempt["error"] = (
                            f"{diagnostics} Diagnostic artifact: {artifact.get('json_path')}"
                            if artifact
                            else diagnostics
                        )
                        trace_path = await self._stop_ctrip_tail_trace(context, trace_started=trace_started)
                        if trace_path:
                            attempt["trace_path"] = str(trace_path)
                            if artifact:
                                artifact["trace_path"] = str(trace_path)
                        attempts.append(attempt)
                        errors.append(
                            f"{normalized_origin}->{destination}: no itinerary via {normalized_transfer}. "
                            f"{diagnostics}"
                        )
                        continue
                    await self._try_enrich_ctrip_tail_match_details(page, match)
                    observed_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
                    attempt["status"] = "matched"
                    attempt["phase"] = "matched"
                    attempt["price"] = match["total_price"]
                    attempt["detail_source"] = match.get("detail_source")
                    attempt["detail_quality"] = match.get("detail_quality")
                    trace_path = await self._stop_ctrip_tail_trace(context, trace_started=trace_started)
                    if trace_path:
                        attempt["trace_path"] = str(trace_path)
                    attempts.append(attempt)
                    results.append(
                        {
                            "provider": "ctrip",
                            "origin": normalized_origin,
                            "transfer": normalized_transfer,
                            "destination": destination,
                            "departure_date": departure_date,
                            "cabin": cabin,
                            "passengers": passengers,
                            "currency": self.app_config.default_currency,
                            "price": match["total_price"],
                            "observed_at": observed_at,
                            "scraped_at": observed_at,
                            "flight_details": match["details"],
                            "raw_payload": {
                                "automation_backend": self.automation_backend,
                                **session_metadata.to_payload(),
                                "browser_use": {
                                    "available": self.browser_use_adapter.status.available,
                                    "reason": self.browser_use_adapter.status.reason,
                                    "config_dir": self.browser_use_adapter.status.config_dir,
                                },
                                "search_url": page.url,
                                "candidate": destination,
                                "preferred_airlines": preferred_airlines or [],
                                "dom_card": match.get("card"),
                                "detail_source": match.get("detail_source"),
                                "detail_quality": match.get("detail_quality"),
                                "text_cleaner": match.get("text_cleaner"),
                                "date_evidence": attempt.get("date_evidence"),
                                "backend_attempts": attempt.get("backend_attempts"),
                                "trace_path": str(trace_path) if trace_path else None,
                                "network_hits": [
                                    {"url": item.get("url"), "status": item.get("status")}
                                    for item in response_store[-10:]
                                ],
                            },
                        }
                    )
                except Exception as exc:
                    context_had_error = True
                    message = f"{normalized_origin}->{destination} failed: {exc}"
                    attempt["status"] = "failed"
                    attempt["error"] = str(exc)
                    trace_path = await self._stop_ctrip_tail_trace(context, trace_started=trace_started)
                    if trace_path:
                        attempt["trace_path"] = str(trace_path)
                    attempts.append(attempt)
                    errors.append(message)
                    LOGGER.exception(message)

                if progress_callback:
                    progress_callback(
                        {
                            "destination": destination,
                            "attempt": attempt,
                            "attempts": list(attempts),
                            "results": list(results),
                            "errors": list(errors),
                        }
                    )

                if index < len(unique_candidates) - 1:
                    base_min = 35000
                    base_max = 75000 + (index // 5) * 15000
                    wait_ms = random.randint(base_min, base_max)
                    LOGGER.info(
                        "tail discovery cool-down before next candidate: %.1fs (index=%s, range=%s-%s)",
                        wait_ms / 1000,
                        index,
                        base_min / 1000,
                        base_max / 1000,
                    )
                    await self._tail_cancellable_wait(page, wait_ms, cancel_checker=cancel_checker)
                    await self._human_interlude(page, provider)
        finally:
            await self._close_context_quietly(context)

        results.sort(key=lambda item: float(item.get("price", 0) or 0))
        return {"results": results, "errors": errors, "attempts": attempts}
