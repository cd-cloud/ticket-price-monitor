from playwright.async_api import Page

from browser_query_result import OfferDetails
from config_manager import ProviderConfig, RouteQuery, RouteSegment
from providers import ctrip
from providers import ctrip_itinerary
from providers import ctrip_network_parser
from providers import ctrip_page
from providers import ctrip_parser


class CtripBrowserOperations:
    def __init__(self, automation, runtime) -> None:
        self.automation = automation
        self.runtime = runtime

    async def _ctrip_results_date_evidence(self, page: Page, route: RouteQuery) -> dict[str, object]:
        try:
            return await ctrip_page.collect_results_date_evidence(page, route.departure_date)
        except Exception as exc:
            return {"expected": route.departure_date, "url": getattr(page, "url", ""), "error": str(exc)}

    async def _query_single_route_offer(
        self,
        provider: ProviderConfig,
        route: RouteQuery,
        page: Page,
        response_store: list[dict],
    ) -> tuple[OfferDetails, str, str]:
        result = await ctrip.query_single_route_offer(self.runtime, provider, route, page, response_store)
        await self.runtime.ensure_ctrip_results_date(page, route, provider.timeout_ms, offer=result[0])
        return result

    async def _query_ctrip_multi_city_offer(
        self,
        provider: ProviderConfig,
        route: RouteQuery,
        page: Page,
        response_store: list[dict],
    ) -> tuple[OfferDetails, str, str]:
        return await ctrip.query_multi_city_offer(self.runtime, provider, route, page, response_store)

    async def _prepare_ctrip_results_page(self, page: Page, route: RouteQuery, timeout_ms: int) -> None:
        await ctrip_page.prepare_results_page(self, page, route, timeout_ms)

    async def _ensure_ctrip_results_date(self, page: Page, route: RouteQuery, timeout_ms: int, *, offer=None) -> dict:
        return await ctrip_page.ensure_results_departure_date(page, route, timeout_ms, offer=offer)

    async def _activate_ctrip_multi_city(self, page: Page, timeout_ms: int) -> None:
        await ctrip_page.activate_multi_city(page, timeout_ms)

    async def _activate_ctrip_one_way(self, page: Page, timeout_ms: int) -> None:
        await ctrip_page.activate_one_way(page, timeout_ms)

    async def _activate_ctrip_round_trip(self, page: Page, timeout_ms: int) -> None:
        await ctrip_page.activate_round_trip(page, timeout_ms)

    async def _dismiss_ctrip_login_overlays(self, page: Page, timeout_ms: int) -> None:
        await ctrip_page.dismiss_login_overlays(page, timeout_ms)

    async def _prepare_ctrip_one_way_form(
        self,
        page: Page,
        route: RouteQuery,
        timeout_ms: int,
        *,
        skip_origin: bool = False,
    ) -> None:
        try:
            await ctrip_page.prepare_one_way_form(self, page, route, timeout_ms, skip_origin=skip_origin)
        except Exception as exc:
            await self.runtime.assist_page(
                page,
                "ctrip_one_way_form",
                "Ctrip one-way form preparation failed. Help diagnose city/date field state using normal UI only.",
                {"route_key": route.route_key, "origin": route.origin, "destination": route.destination, "error": str(exc)},
            )
            raise

    async def _prepare_ctrip_round_trip_form(
        self,
        page: Page,
        route: RouteQuery,
        timeout_ms: int,
    ) -> None:
        try:
            await ctrip_page.prepare_round_trip_form(self, page, route, timeout_ms)
        except Exception as exc:
            await self.runtime.assist_page(
                page,
                "ctrip_round_trip_form",
                "Ctrip round-trip form preparation failed. Help diagnose city/date field state using normal UI only.",
                {"route_key": route.route_key, "origin": route.origin, "destination": route.destination, "error": str(exc)},
            )
            raise

    async def _prepare_ctrip_multi_city_form(
        self,
        page: Page,
        segments: list[RouteSegment],
        timeout_ms: int,
    ) -> None:
        try:
            await ctrip_page.prepare_multi_city_form(self, page, segments, timeout_ms)
        except Exception as exc:
            await self.runtime.assist_page(
                page,
                "ctrip_multi_city_form",
                "Ctrip multi-city form preparation failed. Help diagnose city/date field state using normal UI only.",
                {"segments": [segment.to_dict() for segment in segments], "error": str(exc)},
            )
            raise

    async def _extract_ctrip_offer(self, page: Page, route: RouteQuery) -> OfferDetails | None:
        return await ctrip_parser.extract_offer(self.automation, page, route)

    def _extract_ctrip_multi_city_match_from_network(
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
    def _ctrip_total_price(price: dict) -> float | None:
        return ctrip_itinerary.total_price(price)

    @staticmethod
    def _multi_city_details_are_direct(details: list[dict[str, object]], route: RouteQuery) -> bool:
        return ctrip_itinerary.multi_city_details_are_direct(details, route)

    def _walk_ctrip_data_nodes(self, node):
        yield from ctrip_itinerary.walk_data_nodes(node)

    def _build_ctrip_itinerary_details(self, itinerary: dict, price: dict) -> list[dict[str, object]]:
        return ctrip_itinerary.build_itinerary_details(itinerary, price)

    def _build_multi_city_details(self, itinerary: dict, price: dict) -> list[dict[str, object]]:
        return ctrip_itinerary.build_multi_city_details(itinerary, price)

    def _build_domestic_online_details(self, itinerary: dict, price: dict) -> list[dict[str, object]]:
        return ctrip_itinerary.build_domestic_online_details(itinerary, price)

    def _extract_flight_info_list(self, price: dict) -> list[dict]:
        return ctrip_itinerary.extract_flight_info_list(price)

    @staticmethod
    def _normalize_cabin_from_seat(seat: dict[str, object]) -> str | None:
        return ctrip_itinerary.normalize_cabin_from_seat(seat)

    @staticmethod
    def _normalize_cabin_code(value: object) -> str | None:
        return ctrip_itinerary.normalize_cabin_code(value)

    @staticmethod
    def _format_ctrip_dt(value: object) -> str | None:
        return ctrip_itinerary.format_ctrip_dt(value)

    @staticmethod
    def _coerce_price(value) -> float | None:
        return ctrip_itinerary.coerce_price(value)
