"""Coordinator for Ctrip tail-discovery browser runs."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from time import monotonic
from pathlib import Path
from typing import Any, Callable

from config_manager import RouteQuery
from providers import ctrip, ctrip_page, ctrip_itinerary, ctrip_parser
from providers.ctrip_tail_discovery import extract_tail_matches_from_network, extract_tail_matches_from_page, code_matches_target
from browser_retry_policy import looks_like_rate_limit
from tail_quotes import build_baseline_catalog, quote_terms
from providers.ctrip_tail_discovery import classify_tail_no_match, tail_network_route_stats
from providers.response_capture import attach_response_listeners, ResponseStore
from providers.ctrip_tail_candidates import prepare_network_candidates, merge_tail_candidates


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
        response_store: list[dict] = ResponseStore()
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

        if looks_like_rate_limit(query_error):
            return query_error, page, context, response_store, session_metadata, trace_started, False
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
                # The common collection stage validates dates after loading,
                # including exact network evidence when date controls are unknown.
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
                if looks_like_rate_limit(last_error):
                    break
                if await self._ctrip_verification_detected(page):
                    break
        return last_error, page, context, response_store, session_metadata, trace_started, False

    async def query_baseline(
        self, *, origin: str, transfer: str, departure_date: str, cabin: str,
        passengers: int, departure_time_start: str = "", departure_time_end: str = "",
        preferred_airlines=None,
    ) -> dict[str, object] | None:
        provider = self.app_config.providers["ctrip"]
        page, context, _metadata, responses = await self._open_tail_context(provider)
        try:
            route = RouteQuery(origin=origin, destination=transfer, departure_date=departure_date,
                               route_type="one_way", cabin=cabin, transfer_policy="direct_only",
                               passengers=passengers, providers=["ctrip"])
            offer, url, _title = await ctrip.query_single_route_offer(self.runtime, provider, route, page, responses)
            offers = []
            for response in responses:
                for node in ctrip_itinerary.walk_data_nodes(response.get("payload") or {}):
                    for itinerary in node.get("flightItineraryList") or []:
                        for price in itinerary.get("priceList") or []:
                            legs = ctrip_itinerary.build_itinerary_details(itinerary, price)
                            total = ctrip_itinerary.total_price(price)
                            if len(legs) == 1 and total and code_matches_target(legs[0].get("origin"), origin) and code_matches_target(legs[0].get("destination"), transfer):
                                offers.append({"price": total, "flight_details": legs, "final_url": url})
            if not offers and offer:
                offers.append({"price": offer.price, "flight_details": ctrip_parser.parse_single_flight_details(offer.row_text, route), "final_url": url})
            return build_baseline_catalog(
                offers, provider="ctrip", departure_date=departure_date, cabin=cabin, passengers=passengers,
                observed_at=datetime.now(timezone.utc).isoformat(), review_url=url,
                time_start=departure_time_start, time_end=departure_time_end, preferred_airlines=preferred_airlines,
            )
        finally:
            await self._close_context_quietly(context)

    async def _verification_gate(self, page, context, attempt, progress_callback, cancel_checker, continue_checker):
        if not await self._ctrip_verification_detected(page):
            return False
        attempt.update(status="needs_verification", phase="verification_detected")
        if progress_callback:
            progress_callback({"stage": "needs_verification", "destination": attempt["destination"],
                               "message": "请在浏览器中人工完成验证，再点击继续。", "attempts": [dict(attempt)]})
        if not await self._wait_for_ctrip_verification_continue(
            page, cancel_checker=cancel_checker, verification_continue_checker=continue_checker,
        ):
            raise RuntimeError("verification_timeout: manual verification was not completed")
        await self._save_storage_state_if_allowed(context, self.app_config.providers["ctrip"])
        attempt.update(status="started", phase="verification_continued")
        if progress_callback:
            progress_callback({"stage": "verification_continued", "destination": attempt["destination"]})
        return True

    async def _collect_candidate_matches(self, page, route, transfer, responses, attempt, cabin):
        network = extract_tail_matches_from_network(
            self.automation, responses, origin=route.origin, transfer=transfer,
            destination=route.destination, preferred_airlines=None,
        )
        ui_evidence = await self._ctrip_results_date_evidence(page, route)
        attempt["date_evidence"] = ui_evidence
        try:
            network, evidence = prepare_network_candidates(ui_evidence, network, route, transfer)
        except RuntimeError:
            await ctrip_page.dump_datepicker_debug(page, route.departure_date, "tail-results-date-check")
            raise
        attempt["date_evidence"] = evidence
        page_matches = []
        sources = {"network": len(network), "page": 0, "page_status": "skipped_unknown_date"}
        attempt["candidate_sources"] = sources
        # DOM parsers infer travel dates from the selected page date. A valid
        # network quote cannot lend its date proof to unrelated DOM cards.
        if evidence.get("ui_status") != "unknown":
            try:
                page_matches = await extract_tail_matches_from_page(
                    page, origin=route.origin, transfer=transfer, destination=route.destination,
                    departure_date=route.departure_date, cabin=cabin, preferred_airlines=None,
                )
                for match in page_matches:
                    match["date_evidence"] = dict(ui_evidence)
                sources.update(page=len(page_matches), page_status="parsed")
            except Exception as exc:
                if looks_like_rate_limit(exc) or not network:
                    raise
                # Optional DOM extraction must not discard valid network quotes.
                sources.update(page_status="failed", page_error=str(exc))
        return merge_tail_candidates(network, page_matches)

    async def run(
        self, *, origin: str, transfer: str, departure_date: str, cabin: str, passengers: int,
        candidates: list[str], preferred_airlines: list[str] | None = None,
        progress_callback: Callable | None = None, cancel_checker: Callable | None = None,
        verification_continue_checker: Callable | None = None,
        filter_scope: str = "first_leg",
    ) -> dict[str, object]:
        provider = self.app_config.providers["ctrip"]
        page, context, metadata, responses = await self._open_tail_context(provider)
        results, attempts, errors = [], [], []
        origin, transfer = origin.upper(), transfer.upper()
        candidates = list(dict.fromkeys(code.upper() for code in candidates if code and code.upper() not in {origin, transfer}))
        origin_ready, context_error = False, False
        try:
            for index, destination in enumerate(candidates):
                if cancel_checker and cancel_checker():
                    break
                if index and (context_error or index % self.CONTEXT_REBUILD_INTERVAL == 0):
                    await self._close_context_quietly(context)
                    page, context, metadata, responses = await self._open_tail_context(provider, backend_name=metadata.browser_backend)
                    origin_ready, context_error = False, False
                started = monotonic()
                attempt = {"destination": destination, "status": "started", "phase": "preparing_form", "method": "native_form", "error": None}
                if progress_callback:
                    progress_callback({"stage": "candidate_started", "destination": destination})
                trace_started, stop = False, False
                query_cabin = ({"economy": "economy_plus", "premium_economy": "economy_plus",
                                "business": "business_first", "first": "business_first"}.get(cabin, cabin)
                               if filter_scope == "first_leg" else cabin)
                route = RouteQuery(origin=origin, destination=destination, departure_date=departure_date,
                                   route_type="one_way", cabin=query_cabin, transfer_policy="any",
                                   passengers=passengers, providers=["ctrip"])
                try:
                    await self._verification_gate(page, context, attempt, progress_callback, cancel_checker, verification_continue_checker)
                    responses.clear()
                    trace_started = await self._start_ctrip_tail_trace(context, origin=origin, transfer=transfer,
                                                                      destination=destination, departure_date=departure_date)
                    try:
                        await asyncio.wait_for(ctrip.query_tail_candidate_offer(
                            self.runtime, provider, route, page, responses, origin_already_set=origin_ready,
                        ), timeout=self.TAIL_DISCOVERY_CANDIDATE_TIMEOUT_SECONDS)
                        origin_ready = True
                    except Exception as exc:
                        if looks_like_rate_limit(exc):
                            raise
                        if await self._verification_gate(page, context, attempt, progress_callback, cancel_checker, verification_continue_checker):
                            responses.clear()
                            await asyncio.wait_for(ctrip.query_tail_candidate_offer(
                                self.runtime, provider, route, page, responses, origin_already_set=False,
                            ), timeout=self.TAIL_DISCOVERY_CANDIDATE_TIMEOUT_SECONDS)
                        else:
                            error, page, context, responses, metadata, trace_started, origin_ready = await self._retry_candidate_with_backend_fallback(
                                provider=provider, route=route, destination=destination, page=page, context=context,
                                response_store=responses, session_metadata=metadata, trace_started=trace_started,
                                origin=origin, transfer=transfer, departure_date=departure_date, attempt=attempt, query_error=exc,
                            )
                            if error:
                                if looks_like_rate_limit(error):
                                    raise error
                                if await self._verification_gate(page, context, attempt, progress_callback, cancel_checker, verification_continue_checker):
                                    responses.clear()
                                    await asyncio.wait_for(ctrip.query_tail_candidate_offer(
                                        self.runtime, provider, route, page, responses, origin_already_set=False,
                                    ), timeout=self.TAIL_DISCOVERY_CANDIDATE_TIMEOUT_SECONDS)
                                else:
                                    raise error
                    await self._verification_gate(page, context, attempt, progress_callback, cancel_checker, verification_continue_checker)
                    await self._warm_ctrip_tail_result_data(page, responses)
                    if await self._verification_gate(page, context, attempt, progress_callback, cancel_checker, verification_continue_checker):
                        responses.clear()
                        await self._warm_ctrip_tail_result_data(page, responses)
                    attempt["loading_evidence"] = getattr(responses, "loading_evidence", None)
                    matches = await self._collect_candidate_matches(
                        page, route, transfer, responses, attempt, cabin,
                    )
                    if not matches:
                        attempt.update(status="no_match", phase=classify_tail_no_match(
                            tail_network_route_stats(responses, origin=origin, transfer=transfer, destination=destination,
                                                     preferred_airlines=preferred_airlines),
                            has_no_results_notice=await ctrip_page.has_no_results_notice(page),
                        ))
                        attempt["diagnostic_artifact"] = await self._dump_ctrip_tail_diagnostics(
                            page, response_store=responses, origin=origin, transfer=transfer,
                            destination=destination, departure_date=departure_date,
                        )
                    else:
                        observed = datetime.now(timezone.utc).isoformat()
                        for match in matches:
                            # Preserve all offers. Do not choose the cheapest before service-level filters.
                            results.append({
                                "provider": "ctrip", "origin": origin, "transfer": transfer, "destination": destination,
                                "departure_date": departure_date, "cabin": cabin, "passengers": passengers,
                                "currency": self.app_config.default_currency, "price": match["total_price"],
                                "observed_at": observed, "scraped_at": observed, "flight_details": match["details"],
                                "raw_payload": {**metadata.to_payload(), "search_url": page.url,
                                    "preferred_airlines": preferred_airlines or [], "dom_card": match.get("card"),
                                    "detail_source": match.get("detail_source"), "detail_quality": match.get("detail_quality"),
                                    "date_evidence": match.get("date_evidence"),
                                    "response_url": match.get("response_url"),
                                    "query_generation": match.get("query_generation"),
                                    "candidate_sources": attempt.get("candidate_sources"),
                                    "loading_evidence": attempt.get("loading_evidence"),
                                    "quote_terms": quote_terms("ctrip", passengers, self.app_config.default_currency),
                                    "backend_attempts": attempt.get("backend_attempts", [f"{metadata.browser_backend}: ok"])},
                            })
                        attempt.update(status="matched", phase="matched", price=min(m["total_price"] for m in matches))
                except asyncio.CancelledError:
                    attempt.update(status="failed", phase="cancelled_before_query", error="Task deadline or cancellation")
                    raise
                except Exception as exc:
                    context_error = True
                    limited = looks_like_rate_limit(exc)
                    verification = "verification" in str(exc).lower()
                    date_issue = ("date_mismatch" if "result date mismatch" in str(exc).lower()
                                  else "date_unconfirmed" if "result date unconfirmed" in str(exc).lower() else None)
                    stop = limited or verification
                    attempt.update(status="failed", phase="rate_limited" if limited else "verification_timeout" if verification else date_issue or "query_failed",
                                   error=str(exc) or type(exc).__name__)
                    if stop:
                        attempt["stop_reason"] = attempt["phase"]
                    errors.append(f"{origin}->{destination}: {attempt['error']}")
                finally:
                    trace = await self._stop_ctrip_tail_trace(context, trace_started=trace_started)
                    if trace:
                        attempt["trace_path"] = str(trace)
                        for result in results:
                            if result["destination"] == destination:
                                result["raw_payload"]["trace_path"] = str(trace)
                    attempt["elapsed_seconds"] = round(monotonic() - started, 3)
                    attempts.append(dict(attempt))
                    if progress_callback:
                        progress_callback({"stage": "candidate_finished", "destination": destination,
                                           "attempt": dict(attempt), "attempts": list(attempts),
                                           "results": list(results), "errors": list(errors)})
                if stop:
                    break
                if index < len(candidates) - 1:
                    await self._tail_cancellable_wait(page, random.randint(35000, 75000), cancel_checker=cancel_checker)
        finally:
            await self._close_context_quietly(context)
        return {"results": results, "errors": errors, "attempts": attempts}
