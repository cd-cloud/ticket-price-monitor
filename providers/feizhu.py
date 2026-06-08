"""Feizhu (Fliggy) provider — powered by flyai-cli (Fliggy MCP API).

This module replaces the previous browser-automation approach with direct
CLI calls to @fly-ai/flyai-cli, which accesses Fliggy's MCP API under the hood.

Benefits:
- No login required (Taobao/Alibaba account not needed)
- No browser automation (Playwright page not used)
- No anti-bot / CAPTCHA / risk-control issues
- Sub-second response times
- Structured JSON data with full flight details

Prerequisite:
    npm install -g @fly-ai/flyai-cli
"""

from __future__ import annotations

from config_manager import ProviderConfig, RouteQuery
from providers.base import ParsedProviderResult, ProviderQueryOutcome
from providers import feizhu_flyai_adapter


name = "feizhu"
supports_multi_city = False
# Tail discovery is supported via the same flyai-cli infrastructure.
supports_tail_discovery = True


async def query(
    runtime,
    provider: ProviderConfig,
    route: RouteQuery,
    page,
    response_store: list[dict],
) -> ProviderQueryOutcome:
    """Query Feizhu via flyai-cli and return the cheapest flight offer."""
    if route.is_multi_city or route.return_date:
        raise RuntimeError("Feizhu provider currently supports one-way queries only.")

    # Rate-limiting is still applied even though we're using a CLI.
    await runtime.throttle(provider)

    offers = await feizhu_flyai_adapter.search_flights(
        origin=route.origin,
        destination=route.destination,
        departure_date=route.departure_date,
        cabin=route.cabin or "economy",
        timeout_seconds=max(10, provider.timeout_ms // 1000),
    )

    if not offers:
        raise RuntimeError(
            f"No Feizhu flights found for {route.origin}→{route.destination} on {route.departure_date}."
        )

    best = offers[0]
    return ParsedProviderResult(
        provider="feizhu",
        price=best["price"],
        final_url=best["final_url"],
        title=best["title"],
        departure_time=best["departure_time"],
        row_text=best["row_text"],
        flight_details=best["flight_details"],
        parser="feizhu_flyai_cli",
        confidence="high",
        extra_payload={
            "detail_quality": "complete",
            "detail_source": "flyai_mcp_api",
            "journey_type": best.get("journey_type"),
            "total_duration": best.get("total_duration"),
            "alternatives_count": len(offers),
        },
    ).to_outcome()
