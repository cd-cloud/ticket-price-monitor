"""Runtime facade passed to provider query handlers."""

from __future__ import annotations

from typing import Any

from playwright.async_api import Page

from config_manager import ProviderConfig, RouteQuery


class ProviderRuntime:
    """Provider-neutral facade over shared browser capabilities."""

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

    async def extract_offer(
        self,
        provider: ProviderConfig,
        route: RouteQuery,
        page: Page,
        response_store: list[dict],
    ) -> Any:
        return await self._automation._extract_offer(provider, route, page, response_store)

    @staticmethod
    def build_itinerary_summary(route: RouteQuery) -> str:
        if not route.segments:
            return f"{route.origin}->{route.destination} {route.departure_date}"
        return " / ".join(
            f"{segment.origin}->{segment.destination} {segment.departure_date}"
            for segment in route.segments
        )
