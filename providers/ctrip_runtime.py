from playwright.async_api import Page

from config_manager import ProviderConfig, RouteQuery, RouteSegment
from provider_runtime import ProviderRuntime


class CtripRuntime(ProviderRuntime):
    async def warm_ctrip_result_data(self, page: Page, response_store: list[dict]) -> None:
        await self._automation.ctrip_tail._warm_ctrip_tail_result_data(page, response_store)

    async def ensure_ctrip_results_date(self, page: Page, route: RouteQuery, timeout_ms: int, *, offer=None) -> dict:
        return await self._automation.ctrip_browser._ensure_ctrip_results_date(page, route, timeout_ms, offer=offer)

    async def activate_ctrip_one_way(self, page: Page, timeout_ms: int) -> None:
        await self._automation.ctrip_browser._activate_ctrip_one_way(page, timeout_ms)

    async def activate_ctrip_round_trip(self, page: Page, timeout_ms: int) -> None:
        await self._automation.ctrip_browser._activate_ctrip_round_trip(page, timeout_ms)

    async def activate_ctrip_multi_city(self, page: Page, timeout_ms: int) -> None:
        await self._automation.ctrip_browser._activate_ctrip_multi_city(page, timeout_ms)

    async def dismiss_ctrip_login_overlays(self, page: Page, timeout_ms: int) -> None:
        await self._automation.ctrip_browser._dismiss_ctrip_login_overlays(page, timeout_ms)

    async def prepare_ctrip_one_way_form(
        self,
        page: Page,
        route: RouteQuery,
        timeout_ms: int,
        *,
        skip_origin: bool = False,
    ) -> None:
        await self._automation.ctrip_browser._prepare_ctrip_one_way_form(
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
        await self._automation.ctrip_browser._prepare_ctrip_multi_city_form(page, segments, timeout_ms)

    async def prepare_ctrip_round_trip_form(
        self,
        page: Page,
        route: RouteQuery,
        timeout_ms: int,
    ) -> None:
        await self._automation.ctrip_browser._prepare_ctrip_round_trip_form(page, route, timeout_ms)

    def extract_ctrip_multi_city_match_from_network(
        self,
        response_store: list[dict],
        route: RouteQuery,
        *,
        preferred_total: float | None = None,
    ) -> dict[str, object] | None:
        return self._automation.ctrip_browser._extract_ctrip_multi_city_match_from_network(
            response_store,
            route,
            preferred_total=preferred_total,
        )
