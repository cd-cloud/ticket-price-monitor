"""Feizhu discovery: enumerate offers; service owns filtering and ranking."""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Callable

from browser_retry_policy import looks_like_rate_limit
from providers import feizhu_flyai_adapter
from providers.ctrip_tail_discovery import details_match_tail_route, code_matches_target
from providers.ctrip_tail_parser import details_match_airlines
from tail_quotes import build_baseline_catalog, quote_terms

LOGGER = logging.getLogger(__name__)
_MIN_CANDIDATE_DELAY_SECONDS = 3.0
_MAX_CANDIDATE_DELAY_SECONDS = 7.0
_CANDIDATE_TIMEOUT_SECONDS = 30


class FeizhuTailDiscoveryRunner:
    def __init__(self, automation: Any) -> None:
        self.automation = automation
        self.app_config = getattr(automation, "app_config", automation)

    async def run(
        self, *, origin: str, transfer: str, departure_date: str, cabin: str,
        passengers: int, candidates: list[str], preferred_airlines: list[str] | None = None,
        progress_callback: Callable | None = None, cancel_checker: Callable | None = None,
        verification_continue_checker: Callable | None = None,
    ) -> dict[str, object]:
        origin, transfer = origin.upper(), transfer.upper()
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
                    origin=origin, transfer=transfer, destination=destination,
                    departure_date=departure_date, cabin=cabin, preferred_airlines=preferred_airlines,
                )
                if not matches:
                    attempt.update(status="no_match", phase=phase,
                                   error=f"No Feizhu itinerary from {origin} to {destination} via {transfer}: {phase}.")
                else:
                    observed = datetime.now(timezone.utc).isoformat()
                    currency = getattr(self.app_config, "default_currency", "CNY")
                    for match in matches:
                        results.append({
                            "provider": "feizhu", "origin": origin, "transfer": transfer,
                            "destination": destination, "departure_date": departure_date,
                            "cabin": cabin, "passengers": passengers, "currency": currency,
                            "price": match["price"], "observed_at": observed, "scraped_at": observed,
                            "flight_details": match["flight_details"],
                            "raw_payload": {
                                "method": "feizhu_flyai_cli", "candidate": destination,
                                "preferred_airlines": preferred_airlines or [],
                                "detail_source": "flyai_mcp_api", "detail_quality": "complete",
                                "search_url": match.get("review_url"),
                                "quote_terms": quote_terms("feizhu", passengers, currency),
                            },
                        })
                    attempt.update(status="matched", phase="matched", price=min(m["price"] for m in matches),
                                   detail_source="flyai_mcp_api", detail_quality="complete")
            except asyncio.CancelledError:
                attempt.update(status="failed", phase="cancelled_before_query", error="Task deadline or cancellation")
                raise
            except Exception as exc:
                stop = looks_like_rate_limit(exc)
                attempt.update(status="failed", phase="rate_limited" if stop else "query_failed",
                               error=str(exc) or "query timed out")
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
                             departure_time_end: str = "", preferred_airlines=None) -> dict | None:
        offers = await feizhu_flyai_adapter.search_flights(
            origin=origin, destination=transfer, departure_date=departure_date,
            cabin=cabin, timeout_seconds=_CANDIDATE_TIMEOUT_SECONDS,
        )
        direct = []
        for offer in offers or []:
            legs = offer.get("flight_details") or []
            if len(legs) == 1 and code_matches_target(legs[0].get("origin") or legs[0].get("departure_airport"), origin) \
                    and code_matches_target(legs[0].get("destination") or legs[0].get("arrival_airport"), transfer):
                direct.append(offer)
        return build_baseline_catalog(
            direct, provider="feizhu", departure_date=departure_date, cabin=cabin,
            passengers=passengers, observed_at=datetime.now(timezone.utc).isoformat(),
            time_start=departure_time_start, time_end=departure_time_end, preferred_airlines=preferred_airlines,
        )

    async def _query_candidates(self, *, origin, transfer, destination, departure_date, cabin,
                                preferred_airlines=None):
        offers = await asyncio.wait_for(feizhu_flyai_adapter.search_flights(
            origin=origin, destination=destination, departure_date=departure_date,
            cabin=cabin, timeout_seconds=_CANDIDATE_TIMEOUT_SECONDS,
        ), timeout=_CANDIDATE_TIMEOUT_SECONDS + 5)
        if not offers:
            return [], "route_absent"
        via = [offer for offer in offers if details_match_tail_route(
            offer.get("flight_details") or [], origin, transfer, destination)]
        matches = [{**offer, "review_url": offer.get("final_url"), "detail_source": "flyai_mcp_api",
                    "detail_quality": "complete"} for offer in via
                   if details_match_airlines(offer.get("flight_details") or [], preferred_airlines)]
        return matches, "airline_filtered" if via and not matches else "no_via_transfer"

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
