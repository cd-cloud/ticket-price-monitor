"""Feizhu tail-discovery runner — powered by flyai-cli.

Replaces the previous browser-automation approach with direct CLI calls.
No browser, no login, no CAPTCHA handling required.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Any, Callable

from providers import feizhu_flyai_adapter
from providers.ctrip_tail_discovery import details_match_tail_route
from providers.ctrip_tail_parser import details_match_airlines

LOGGER = logging.getLogger(__name__)

# Delay between candidates to avoid rate-limiting the MCP API.
_MIN_CANDIDATE_DELAY_SECONDS = 3.0
_MAX_CANDIDATE_DELAY_SECONDS = 7.0

# Timeout per candidate query (flyai-cli is usually sub-second).
_CANDIDATE_TIMEOUT_SECONDS = 30


def _format_error(exc: Exception) -> str:
    text = str(exc).strip()
    if text:
        return text
    if isinstance(exc, asyncio.TimeoutError):
        return "operation timed out"
    return exc.__class__.__name__


class FeizhuTailDiscoveryRunner:
    """Tail-discovery runner for Feizhu using flyai-cli.

    Queries the Fliggy MCP API for each candidate destination and filters
    results to itineraries that route via the requested transfer city.
    """

    def __init__(self, automation: Any) -> None:
        self.automation = automation
        self.app_config = getattr(automation, "app_config", automation)

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
        """Run tail discovery for all candidate destinations.

        Returns a dict with ``results``, ``errors``, and ``attempts`` keys
        in the same shape as the Ctrip tail-discovery runner.
        """
        normalized_origin = str(origin or "").strip().upper()
        normalized_transfer = str(transfer or "").strip().upper()
        unique_candidates = list(
            dict.fromkeys(
                code.upper()
                for code in candidates
                if code and code.upper() not in {normalized_origin, normalized_transfer}
            )
        )

        results: list[dict[str, object]] = []
        errors: list[str] = []
        attempts: list[dict[str, object]] = []

        for index, destination in enumerate(unique_candidates):
            if cancel_checker and cancel_checker():
                errors.append("Tail discovery was cancelled before the next destination.")
                break

            attempt: dict[str, object] = {
                "destination": destination,
                "status": "started",
                "method": "feizhu_flyai_cli",
                "phase": "querying",
                "error": None,
                "backend_attempts": ["flyai_cli: started"],
            }

            try:
                match, no_match_phase = await self._query_candidate_outcome(
                    origin=normalized_origin,
                    transfer=normalized_transfer,
                    destination=destination,
                    departure_date=departure_date,
                    cabin=cabin,
                    preferred_airlines=preferred_airlines,
                )

                if match is None:
                    attempt["status"] = "no_match"
                    attempt["phase"] = no_match_phase
                    attempt["error"] = (
                        f"No Feizhu itinerary from {normalized_origin} to {destination} "
                        f"via {normalized_transfer}: {no_match_phase}."
                    )
                    attempts.append(attempt)
                    errors.append(
                        f"{normalized_origin}->{destination}: {no_match_phase}."
                    )
                    continue

                observed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                attempt["status"] = "matched"
                attempt["phase"] = "matched"
                attempt["price"] = match["price"]
                attempt["detail_source"] = match.get("detail_source")
                attempt["detail_quality"] = match.get("detail_quality")
                attempts.append(attempt)

                results.append(
                    {
                        "provider": "feizhu",
                        "origin": normalized_origin,
                        "transfer": normalized_transfer,
                        "destination": destination,
                        "departure_date": departure_date,
                        "cabin": cabin,
                        "passengers": passengers,
                        "currency": getattr(self.app_config, "default_currency", "CNY"),
                        "price": match["price"],
                        "observed_at": observed_at,
                        "scraped_at": observed_at,
                        "flight_details": match["flight_details"],
                        "raw_payload": {
                            "method": "feizhu_flyai_cli",
                            "candidate": destination,
                            "preferred_airlines": preferred_airlines or [],
                            "detail_source": match.get("detail_source"),
                            "detail_quality": match.get("detail_quality"),
                            "search_url": match.get("review_url"),
                        },
                    }
                )

            except Exception as exc:
                error_text = _format_error(exc)
                message = f"{normalized_origin}->{destination} failed: {error_text}"
                attempt["status"] = "failed"
                attempt["phase"] = "query_failed"
                attempt["error"] = error_text
                attempts.append(attempt)
                errors.append(message)
                LOGGER.warning(message)

            if progress_callback:
                progress_callback(
                    {
                        "destination": destination,
                        "attempt": attempt,
                        "attempts": list(attempts),
                        "results": list(results),
                        "errors": list(errors),
                        "stage": "candidate_finished",
                    }
                )

            # Cooldown between candidates (except after the last one).
            if index < len(unique_candidates) - 1:
                cooldown_seconds = random.uniform(
                    _MIN_CANDIDATE_DELAY_SECONDS, _MAX_CANDIDATE_DELAY_SECONDS
                )
                await self._cancellable_wait(cooldown_seconds, cancel_checker=cancel_checker)

        results.sort(key=lambda item: float(item.get("price", 0) or 0))
        return {"results": results, "errors": errors, "attempts": attempts}

    async def query_baseline(
        self,
        *,
        origin: str,
        transfer: str,
        departure_date: str,
        cabin: str,
        passengers: int,
    ) -> dict[str, object] | None:
        offers = await feizhu_flyai_adapter.search_flights(
            origin=origin,
            destination=transfer,
            departure_date=departure_date,
            cabin=cabin,
            timeout_seconds=_CANDIDATE_TIMEOUT_SECONDS,
        )
        direct_offers = []
        for offer in offers:
            details = offer.get("flight_details") or []
            if len(details) != 1:
                continue
            detail = details[0]
            if str(detail.get("origin") or detail.get("departure_airport") or "").upper() != origin.upper():
                continue
            if str(detail.get("destination") or detail.get("arrival_airport") or "").upper() != transfer.upper():
                continue
            if float(offer.get("price") or 0) > 0:
                direct_offers.append(offer)
        if not direct_offers:
            return None
        offer = min(direct_offers, key=lambda item: float(item["price"]))
        return {
            "provider": "feizhu",
            "origin": origin,
            "destination": transfer,
            "price": float(offer["price"]),
            "observed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "review_url": offer.get("final_url"),
        }

    async def _query_candidate(self, **kwargs) -> dict[str, object] | None:
        match, _phase = await self._query_candidate_outcome(**kwargs)
        return match

    async def _query_candidate_outcome(
        self,
        *,
        origin: str,
        transfer: str,
        destination: str,
        departure_date: str,
        cabin: str,
        preferred_airlines: list[str] | None = None,
    ) -> tuple[dict[str, object] | None, str]:
        offers = await asyncio.wait_for(
            feizhu_flyai_adapter.search_flights(
                origin=origin,
                destination=destination,
                departure_date=departure_date,
                cabin=cabin,
                timeout_seconds=_CANDIDATE_TIMEOUT_SECONDS,
            ),
            timeout=_CANDIDATE_TIMEOUT_SECONDS + 5,
        )
        if not offers:
            return None, "route_absent"

        best_match: tuple[float, dict[str, Any]] | None = None
        via_transfer_found = False
        for offer in offers:
            flight_details = offer.get("flight_details") or []
            if len(flight_details) < 2:
                continue
            if not details_match_tail_route(flight_details, origin, transfer, destination):
                continue
            via_transfer_found = True
            if not details_match_airlines(flight_details, preferred_airlines):
                continue
            price = float(offer.get("price") or 0)
            if best_match is None or price < best_match[0]:
                best_match = (price, offer)

        if best_match is None:
            return None, "airline_filtered" if via_transfer_found else "no_via_transfer"

        _price, offer = best_match
        return {
            "price": offer["price"],
            "flight_details": offer["flight_details"],
            "detail_source": "flyai_mcp_api",
            "detail_quality": "complete",
            "review_url": offer.get("final_url"),
        }, ""

    async def _cancellable_wait(
        self,
        seconds: float,
        *,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> None:
        remaining = max(0, int(seconds * 1000))
        while remaining > 0:
            if cancel_checker and cancel_checker():
                return
            step = min(500, remaining)
            await asyncio.sleep(step / 1000)
            remaining -= step
