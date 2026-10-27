"""Feizhu discovery: enumerate offers; service owns filtering and ranking."""
from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Callable

from browser_retry_policy import looks_like_rate_limit
from providers import feizhu_flyai_adapter
from providers.ctrip_tail_discovery import details_match_tail_route, code_matches_target
from providers.ctrip_tail_parser import details_match_airlines
from tail_quotes import build_baseline_catalog, quote_terms, first_leg_key
from tail_call_budget import TailCallBudget, ProviderCallBudgetExceeded

LOGGER = logging.getLogger(__name__)
_MIN_CANDIDATE_DELAY_SECONDS = 3.0
_MAX_CANDIDATE_DELAY_SECONDS = 7.0
_CANDIDATE_TIMEOUT_SECONDS = 30


class FeizhuTailDiscoveryRunner:
    def __init__(self, automation: Any) -> None:
        self.automation = automation
        self.app_config = getattr(automation, "app_config", automation)
        self._baseline_catalog = None
        self._baseline_request = None
        self._baseline_attempted = set()
        self._baseline_queries = 0
        self.call_budget = TailCallBudget()

    async def _search_flights(self, kind, **kwargs):
        self.call_budget.consume(kind)
        return await feizhu_flyai_adapter.search_flights(**kwargs)

    async def run(
        self, *, origin: str, transfer: str, departure_date: str, cabin: str,
        passengers: int, candidates: list[str], preferred_airlines: list[str] | None = None,
        progress_callback: Callable | None = None, cancel_checker: Callable | None = None,
        verification_continue_checker: Callable | None = None,
        search_scope: str = "candidates",
        filter_scope: str = "first_leg",
        result_filter: Callable | None = None,
        max_baseline_queries: int = 3,
        transport_no: str | None = None,
    ) -> dict[str, object]:
        origin, transfer = origin.upper(), transfer.upper()
        from tail_service import OPEN_DESTINATION
        if search_scope == "open":
            candidates = [OPEN_DESTINATION]
        candidates = list(dict.fromkeys(code.upper() for code in candidates
                                       if code and code.upper() not in {origin, transfer}))
        results, attempts, errors = [], [], []
        for index, destination in enumerate(candidates):
            if cancel_checker and cancel_checker():
                errors.append("Tail discovery was cancelled before the next destination.")
                break
            started = monotonic()
            attempt = {"destination": destination, "status": "started", "method": "feizhu_flyai_cli",
                       "phase": "querying", "error": None, "backend_attempts": ["flyai_cli: started"]}
            if progress_callback:
                progress_callback({"stage": "candidate_started", "destination": destination})
            stop = False
            try:
                matches, phase = await self._query_candidates(
                    origin=origin, transfer=transfer,
                    destination=None if search_scope == "open" else destination,
                    departure_date=departure_date, cabin=cabin, preferred_airlines=preferred_airlines,
                    filter_scope=filter_scope, transport_no=transport_no,
                )
                attempt["search_metadata"] = dict(getattr(self, "_last_search_metadata", {}))
                if not matches:
                    attempt.update(status="failed" if phase == "search_incomplete" else "no_match", phase=phase,
                                   error=f"No Feizhu itinerary returned from {origin} to {destination} via {transfer}: {phase}. "
                                         "CLI coverage is not guaranteed; this does not prove no route exists.")
                    if attempt["search_metadata"].get("system_message"):
                        attempt["error"] += " " + attempt["search_metadata"]["system_message"][:800]
                else:
                    observed = datetime.now(timezone.utc).isoformat()
                    currency = getattr(self.app_config, "default_currency", "CNY")
                    for match in matches:
                        results.append({
                            "provider": "feizhu", "origin": origin, "transfer": transfer,
                            "destination": match.get("actual_destination") or destination, "departure_date": departure_date,
                            "cabin": cabin, "passengers": passengers, "currency": currency,
                            "price": match["price"], "observed_at": observed, "scraped_at": observed,
                            "flight_details": match["flight_details"],
                            "raw_payload": {
                                "method": "feizhu_flyai_cli", "candidate": destination,
                                "preferred_airlines": preferred_airlines or [],
                                "detail_source": "flyai_mcp_api", "detail_quality": "complete",
                                "search_url": match.get("review_url"),
                                "quote_terms": quote_terms("feizhu", passengers, currency,
                                                           price_scope="unknown",
                                                           source_price_field=match.get("source_price_field", "ticketPrice")),
                                "source_price_value": match.get("source_price_value"),
                                "search_metadata": match.get("search_metadata") or {},
                                "search_scope": search_scope,
                                "filter_scope": filter_scope,
                            },
                        })
                    attempt.update(status="matched", phase="matched", price=min(m["price"] for m in matches),
                                   detail_source="flyai_mcp_api", detail_quality="complete")
                    rows = [r for r in results if search_scope == "open" or r["destination"] == destination]
                    # Service supplies the exact same validation/dedup pipeline as checkpointing.
                    # Without it, skip enrichment rather than spend quota on unvalidated offers.
                    if result_filter:
                        eligible = result_filter(rows)
                        while self._baseline_queries < max_baseline_queries:
                            before = self._baseline_queries
                            stop = await self._fill_same_flight_baseline(
                                eligible, attempt=attempt, cancel_checker=cancel_checker)
                            if stop or self._baseline_queries == before:
                                break
                    attempt.update(baseline_queries=self._baseline_queries,
                                   baseline_query_budget=max_baseline_queries)
            except asyncio.CancelledError:
                attempt.update(status="failed", phase="cancelled_before_query", error="Task deadline or cancellation")
                raise
            except ProviderCallBudgetExceeded as exc:
                stop = True
                attempt.update(status="failed", phase="provider_call_budget",
                               stop_reason="provider_call_budget", error=str(exc))
            except Exception as exc:
                stop = looks_like_rate_limit(exc)
                attempt.update(status="failed", phase="rate_limited" if stop else "query_failed",
                               error=str(exc) or "query timed out")
                if stop:
                    attempt["stop_reason"] = "rate_limited"
                errors.append(f"{origin}->{destination}: {attempt['error']}")
                LOGGER.warning("Feizhu tail query failed: %s", attempt["error"])
            finally:
                attempt["elapsed_seconds"] = round(monotonic() - started, 3)
                attempts.append(attempt)
                if progress_callback:
                    progress_callback({"destination": destination, "attempt": dict(attempt),
                                       "attempts": list(attempts), "results": list(results),
                                       "errors": list(errors), "stage": "candidate_finished"})
            if stop:
                break  # No backend hopping after rate limiting.
            if index < len(candidates) - 1:
                await self._cancellable_wait(random.uniform(_MIN_CANDIDATE_DELAY_SECONDS,
                                                            _MAX_CANDIDATE_DELAY_SECONDS),
                                             cancel_checker=cancel_checker)
        return {"results": results, "errors": errors, "attempts": attempts}

    async def query_baseline(self, *, origin: str, transfer: str, departure_date: str,
                             cabin: str, passengers: int, departure_time_start: str = "",
                             departure_time_end: str = "", preferred_airlines=None,
                             transport_no: str | None = None) -> dict | None:
        offers = await self._search_flights("same_flight_baseline" if transport_no else "initial_baseline",
            origin=origin, destination=transfer, departure_date=departure_date,
            cabin=cabin, journey_type="1", transport_no=transport_no,
            timeout_seconds=_CANDIDATE_TIMEOUT_SECONDS,
        )
        direct = []
        for offer in offers or []:
            legs = offer.get("flight_details") or []
            if len(legs) == 1 and code_matches_target(legs[0].get("origin") or legs[0].get("departure_airport"), origin) \
                    and code_matches_target(legs[0].get("destination") or legs[0].get("arrival_airport"), transfer):
                direct.append(offer)
        catalog = build_baseline_catalog(
            direct, provider="feizhu", departure_date=departure_date, cabin=cabin,
            passengers=passengers, observed_at=datetime.now(timezone.utc).isoformat(),
            time_start=departure_time_start, time_end=departure_time_end, preferred_airlines=preferred_airlines,
        )
        if transport_no is None:
            self._baseline_request = dict(
                origin=origin, transfer=transfer, departure_date=departure_date, cabin=cabin,
                passengers=passengers, departure_time_start=departure_time_start,
                departure_time_end=departure_time_end, preferred_airlines=preferred_airlines,
            )
            self._baseline_catalog = catalog or {"market": None, "same_flights": {}}
            self._baseline_attempted.clear()
            self._baseline_queries = 0
            return self._baseline_catalog
        return catalog

    async def _fill_same_flight_baseline(self, results, *, attempt, cancel_checker):
        if self._baseline_request is None or self._baseline_catalog is None:
            return False
        same = self._baseline_catalog["same_flights"]
        missing = next((r for r in results if first_leg_key(r) not in same
                        and first_leg_key(r) not in self._baseline_attempted
                        and (r.get("flight_details") or [{}])[0].get("flight_no")), None)
        if missing is None:
            return False
        key = first_leg_key(missing)
        if cancel_checker and cancel_checker():
            return False
        self._baseline_attempted.add(key)
        await self._cancellable_wait(_MIN_CANDIDATE_DELAY_SECONDS, cancel_checker=cancel_checker)
        if cancel_checker and cancel_checker():
            return False
        self._baseline_queries += 1
        try:
            catalog = await self.query_baseline(
                **self._baseline_request, transport_no=missing["flight_details"][0]["flight_no"],
            )
            # Never accept a search that returned a different time, airport or cabin.
            item = ((catalog or {}).get("same_flights") or {}).get(key)
            if item:
                same[key] = item
        except Exception as exc:
            attempt["baseline_warning"] = str(exc)
            if isinstance(exc, ProviderCallBudgetExceeded):
                attempt.update(phase="provider_call_budget", stop_reason="provider_call_budget", error=str(exc))
                return True
            if looks_like_rate_limit(exc):
                attempt.update(phase="rate_limited", stop_reason="rate_limited", error=str(exc))
                return True
        return False

    async def _query_candidates(self, *, origin, transfer, destination, departure_date, cabin,
                                preferred_airlines=None, filter_scope="first_leg", transport_no=None):
        offers = await asyncio.wait_for(self._search_flights("open_search" if destination is None else "targeted_search",
            origin=origin, destination=destination, departure_date=departure_date,
            cabin="any" if filter_scope == "first_leg" else cabin,
            journey_type="2", transfer_city=transfer, transport_no=transport_no,
            timeout_seconds=_CANDIDATE_TIMEOUT_SECONDS,
        ), timeout=_CANDIDATE_TIMEOUT_SECONDS + 5)
        self._last_search_metadata = dict(getattr(offers, "search_metadata", {}))
        if not offers:
            return [], "search_incomplete"
        via = []
        for offer in offers:
            legs = offer.get("flight_details") or []
            actual = str((legs[-1].get("destination") or legs[-1].get("arrival_airport") or "") if legs else "").upper()
            target = destination or actual
            if not re.fullmatch(r"[A-Z]{3}", target) or target in {origin, transfer}:
                continue
            if details_match_tail_route(legs, origin, transfer, target):
                via.append({**offer, "actual_destination": target})
        # User airline/cabin filters belong to the service. Keeping structurally
        # returned routes here lets the history learn even when the quote is filtered.
        matches = [{**offer, "review_url": offer.get("final_url"), "detail_source": "flyai_mcp_api",
                    "detail_quality": "complete"} for offer in via]
        return matches, "search_incomplete"

    async def _query_candidate_outcome(self, **kwargs):
        matches, phase = await self._query_candidates(**kwargs)
        return (min(matches, key=lambda item: item["price"]), "") if matches else (None, phase)

    async def _query_candidate(self, **kwargs):
        return (await self._query_candidate_outcome(**kwargs))[0]

    async def _cancellable_wait(self, seconds: float, *, cancel_checker=None) -> None:
        remaining = seconds
        while remaining > 0:
            if cancel_checker and cancel_checker():
                return
            step = min(.5, remaining)
            await asyncio.sleep(step)
            remaining -= step
