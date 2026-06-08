"""Runtime facade passed to provider query handlers."""

from __future__ import annotations

from typing import Any

from playwright.async_api import Page

from config_manager import ProviderConfig, RouteQuery, RouteSegment
from providers import ctrip_network_parser


class ProviderRuntime:
    """Narrow facade over BrowserAutomation capabilities used by providers.

    This keeps provider handlers from depending on the full BrowserAutomation
    object while preserving behavior during the refactor.
    """

    def __init__(self, automation: Any) -> None:
        self._automation = automation

    @property
    def app_config(self) -> Any:
        return self._automation.app_config

    @property
    def offer_details_type(self) -> Any:
        return self._automation.offer_details_type

    async def throttle(self, provider: ProviderConfig) -> None:
        await self._automation._throttle(provider)

    async def assist_page(
        self,
        page: Page,
        kind: str,
        instruction: str,
        context: dict[str, object] | None = None,
    ) -> None:
        await self._automation._browser_use_assist_page(page, kind, instruction, context)

    async def wait_for_results(self, provider: ProviderConfig, page: Page) -> None:
        await self._automation._wait_for_results(provider, page)

    async def warm_ctrip_result_data(self, page: Page, response_store: list[dict]) -> None:
        await self._automation._warm_ctrip_tail_result_data(page, response_store)

    async def extract_offer(
        self,
        provider: ProviderConfig,
        route: RouteQuery,
        page: Page,
        response_store: list[dict],
    ) -> Any:
        return await self._automation._extract_offer(provider, route, page, response_store)

    async def ensure_ctrip_results_date(self, page: Page, route: RouteQuery, timeout_ms: int) -> None:
        await self._automation._ensure_ctrip_results_date(page, route, timeout_ms)

    async def activate_ctrip_one_way(self, page: Page, timeout_ms: int) -> None:
        await self._automation._activate_ctrip_one_way(page, timeout_ms)

    async def activate_ctrip_round_trip(self, page: Page, timeout_ms: int) -> None:
        await self._automation._activate_ctrip_round_trip(page, timeout_ms)

    async def activate_ctrip_multi_city(self, page: Page, timeout_ms: int) -> None:
        await self._automation._activate_ctrip_multi_city(page, timeout_ms)

    async def dismiss_ctrip_login_overlays(self, page: Page, timeout_ms: int) -> None:
        await self._automation._dismiss_ctrip_login_overlays(page, timeout_ms)

    async def prepare_ctrip_one_way_form(
        self,
        page: Page,
        route: RouteQuery,
        timeout_ms: int,
        *,
        skip_origin: bool = False,
    ) -> None:
        await self._automation._prepare_ctrip_one_way_form(
            page,
            route,
            timeout_ms,
            skip_origin=skip_origin,
        )

    async def prepare_ctrip_multi_city_form(
        self,
        page: Page,
        segments: list[RouteSegment],
        timeout_ms: int,
    ) -> None:
        await self._automation._prepare_ctrip_multi_city_form(page, segments, timeout_ms)

    async def prepare_ctrip_round_trip_form(
        self,
        page: Page,
        route: RouteQuery,
        timeout_ms: int,
    ) -> None:
        await self._automation._prepare_ctrip_round_trip_form(page, route, timeout_ms)

    def extract_ctrip_multi_city_match_from_network(
        self,
        response_store: list[dict],
        route: RouteQuery,
        *,
        preferred_total: float | None = None,
    ) -> dict[str, object] | None:
        return ctrip_network_parser.extract_multi_city_match_from_network(
            response_store,
            route,
            preferred_total=preferred_total,
        )

    @staticmethod
    def build_itinerary_summary(route: RouteQuery) -> str:
        if not route.segments:
            return f"{route.origin}->{route.destination} {route.departure_date}"
        return " / ".join(
            f"{segment.origin}->{segment.destination} {segment.departure_date}"
            for segment in route.segments
        )
