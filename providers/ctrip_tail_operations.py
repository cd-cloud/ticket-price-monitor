from datetime import datetime
from pathlib import Path
from typing import Callable

from playwright.async_api import BrowserContext, Page

from config_manager import ProviderConfig, RouteQuery
from providers import ctrip_itinerary
from providers import ctrip_network_parser
from providers import ctrip_tail_discovery
from providers import ctrip_tail_parser


CTRIP_VERIFICATION_KEYWORDS = (
    "\u9a8c\u8bc1\u7801",
    "\u5b89\u5168\u9a8c\u8bc1",
    "\u6ed1\u5757",
    "\u4eba\u673a\u9a8c\u8bc1",
    "\u8bf7\u5b8c\u6210\u9a8c\u8bc1",
    "\u8bbf\u95ee\u5f02\u5e38",
    "captcha",
    "verify",
    "verification",
    "robot",
)


class CtripTailOperations:
    def __init__(self, automation, runtime, browser_operations) -> None:
        self.automation = automation
        self.runtime = runtime
        self.browser_operations = browser_operations

    @property
    def app_config(self):
        return self.runtime.app_config

    async def _throttle(self, provider: ProviderConfig) -> None:
        await self.runtime.throttle(provider)

    def _build_search_url(self, provider: ProviderConfig, route: RouteQuery) -> str:
        return self.automation._build_search_url(provider, route)

    async def _prepare_results_page(self, provider: ProviderConfig, page: Page, route: RouteQuery) -> None:
        await self.automation._prepare_results_page(provider, page, route)

    async def _ensure_ctrip_results_date(self, page: Page, route: RouteQuery, timeout_ms: int, *, offer=None) -> dict:
        return await self.runtime.ensure_ctrip_results_date(page, route, timeout_ms, offer=offer)

    async def _wait_for_results(self, provider: ProviderConfig, page: Page) -> None:
        await self.runtime.wait_for_results(provider, page)

    async def _tail_cancellable_wait(
        self,
        page: Page,
        wait_ms: int,
        *,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> None:
        remaining = max(0, int(wait_ms))
        while remaining > 0:
            if cancel_checker and cancel_checker():
                return
            step = min(1000, remaining)
            await page.wait_for_timeout(step)
            remaining -= step

    async def _start_ctrip_tail_trace(
        self,
        context: BrowserContext,
        *,
        origin: str,
        transfer: str,
        destination: str,
        departure_date: str,
    ) -> bool:
        return await ctrip_tail_discovery.start_trace(
            context,
            origin=origin,
            transfer=transfer,
            destination=destination,
            departure_date=departure_date,
        )

    async def _stop_ctrip_tail_trace(
        self,
        context: BrowserContext,
        *,
        trace_started: bool,
    ) -> Path | None:
        return await ctrip_tail_discovery.stop_trace(self, context, trace_started=trace_started)

    async def _goto_ctrip_tail_results(self, provider: ProviderConfig, route: RouteQuery, page: Page) -> None:
        await ctrip_tail_discovery.goto_tail_results(self, provider, route, page)

    async def _warm_ctrip_tail_result_data(self, page: Page, response_store: list[dict]) -> None:
        await ctrip_tail_discovery.warm_tail_result_data(self, page, response_store)

    async def _wait_for_ctrip_verification_continue(
        self,
        page: Page,
        *,
        cancel_checker: Callable[[], bool] | None = None,
        verification_continue_checker: Callable[[], bool] | None = None,
    ) -> bool:
        await self.runtime.assist_page(
            page,
            "ctrip_verification_wait",
            "Ctrip is showing a security verification screen. Wait for the user to finish it manually, then continue.",
            {"provider": "ctrip", "safety": "manual_verification_only"},
        )
        deadline = datetime.utcnow().timestamp() + 15 * 60
        while datetime.utcnow().timestamp() < deadline:
            if cancel_checker and cancel_checker():
                return False
            if verification_continue_checker and verification_continue_checker():
                return True
            try:
                if not await self._ctrip_verification_detected(page):
                    return True
            except Exception:
                pass
            await page.wait_for_timeout(2000)
        return False

    def _response_store_has_flight_itineraries(self, response_store: list[dict]) -> bool:
        return ctrip_tail_discovery.response_store_has_flight_itineraries(
            response_store,
            data_walker=ctrip_itinerary.walk_data_nodes,
        )

    async def _try_enrich_ctrip_tail_match_details(self, page: Page, match: dict[str, object]) -> None:
        card = match.get("card")
        if not isinstance(card, dict):
            match.setdefault("detail_source", "network_exact")
            match.setdefault("detail_quality", "complete")
            return
        index = card.get("card_index")
        if not isinstance(index, int) or index < 0:
            return
        try:
            row = page.locator(".list-content-item-transit").nth(index)
            await row.scroll_into_view_if_needed(timeout=3000)
            for selector in [".detail", ".airline", ".box-row"]:
                try:
                    await row.locator(selector).first.click(timeout=1500, force=True)
                    await page.wait_for_timeout(900)
                    break
                except Exception:
                    continue
            details_text = await self._visible_ctrip_expanded_detail_text(page, row)
        except Exception:
            return
        if not details_text:
            return
        existing_details = match.get("details")
        if not isinstance(existing_details, list) or not existing_details:
            return
        first_detail = existing_details[0] if isinstance(existing_details[0], dict) else {}
        last_detail = existing_details[-1] if isinstance(existing_details[-1], dict) else {}
        enriched = ctrip_tail_parser.build_tail_details_from_text_block(
            details_text,
            origin=str(first_detail.get("origin") or ""),
            transfer=str(first_detail.get("destination") or first_detail.get("tail_transfer") or ""),
            destination=str(last_detail.get("destination") or ""),
            departure_date=str(first_detail.get("departure_date") or ""),
            cabin=str(first_detail.get("cabin") or "economy_plus"),
        )
        has_more_signal = any(item.get("flight_no") for item in enriched) or len(details_text) > len(str(card.get("text") or ""))
        if has_more_signal:
            match["details"] = enriched
            match["detail_source"] = "expanded_dom"
            match["detail_quality"] = "expanded"

    async def _visible_ctrip_expanded_detail_text(self, page: Page, row) -> str:
        candidates: list[str] = []
        for selector in [
            ".flight-detail",
            ".flight-detail-list",
            ".detail-content",
            ".drawer",
            ".modal",
            ".popup",
            ".ant-modal",
            ".el-dialog",
        ]:
            try:
                loc = page.locator(selector)
                count = min(await loc.count(), 5)
            except Exception:
                continue
            for index in range(count):
                try:
                    text = (await loc.nth(index).inner_text(timeout=800)).strip()
                except Exception:
                    continue
                if text and len(text) > 40:
                    candidates.append(text)
        try:
            row_text = (await row.inner_text(timeout=1000)).strip()
            if row_text:
                candidates.append(row_text)
        except Exception:
            pass
        candidates.sort(key=len, reverse=True)
        return candidates[0] if candidates else ""

    async def _ctrip_verification_detected(self, page: Page) -> bool:
        try:
            url = (page.url or "").lower()
            if any(token in url for token in ["captcha", "verify", "verification", "risk"]):
                return True
            body = (await page.locator("body").inner_text(timeout=2500)).lower()
        except Exception:
            return False
        return any(keyword.lower() in body for keyword in CTRIP_VERIFICATION_KEYWORDS)

    def _extract_ctrip_tail_match_from_network(
        self,
        response_store: list[dict],
        *,
        origin: str,
        transfer: str,
        destination: str,
        preferred_airlines: list[str] | None = None,
    ) -> dict[str, object] | None:
        return ctrip_tail_discovery.extract_tail_match_from_network(
            self,
            response_store,
            origin=origin,
            transfer=transfer,
            destination=destination,
            preferred_airlines=preferred_airlines,
        )

    async def _extract_ctrip_tail_match_from_page(
        self,
        page: Page,
        *,
        origin: str,
        transfer: str,
        destination: str,
        departure_date: str,
        cabin: str,
        preferred_airlines: list[str] | None = None,
    ) -> dict[str, object] | None:
        return await ctrip_tail_discovery.extract_tail_match_from_page(
            page,
            origin=origin,
            transfer=transfer,
            destination=destination,
            departure_date=departure_date,
            cabin=cabin,
            preferred_airlines=preferred_airlines,
        )

    async def _extract_ctrip_tail_page_diagnostics(
        self,
        page: Page,
        *,
        response_store: list[dict] | None = None,
        origin: str = "",
        transfer: str,
        destination: str = "",
        preferred_airlines: list[str] | None = None,
    ) -> str:
        return await ctrip_tail_discovery.extract_tail_page_diagnostics(
            self,
            page,
            response_store=response_store,
            origin=origin,
            transfer=transfer,
            destination=destination,
            preferred_airlines=preferred_airlines,
        )

    async def _dump_ctrip_tail_diagnostics(
        self,
        page: Page,
        *,
        response_store: list[dict],
        origin: str,
        transfer: str,
        destination: str,
        departure_date: str,
    ) -> dict[str, str] | None:
        return await ctrip_tail_discovery.dump_tail_diagnostics(
            self,
            page,
            response_store=response_store,
            origin=origin,
            transfer=transfer,
            destination=destination,
            departure_date=departure_date,
        )

    def _summarize_response_for_diagnostics(self, item: dict) -> dict[str, object]:
        return ctrip_network_parser.summarize_response_for_diagnostics(item)

    def _payload_shape(self, payload: dict) -> str:
        return ctrip_network_parser.payload_shape(payload)

    def _ctrip_tail_network_diagnostics(
        self,
        response_store: list[dict],
        *,
        origin: str,
        transfer: str,
        destination: str,
        preferred_airlines: list[str] | None = None,
    ) -> str:
        return ctrip_tail_discovery.network_diagnostics(
            self,
            response_store,
            origin=origin,
            transfer=transfer,
            destination=destination,
            preferred_airlines=preferred_airlines,
        )

    @staticmethod
    def _summarize_tail_details(details: list[dict[str, object]]) -> str:
        return ctrip_tail_discovery.summarize_tail_details(details)

    def _details_match_tail_route(
        self,
        details: list[dict[str, object]],
        origin: str,
        transfer: str,
        destination: str,
    ) -> bool:
        return ctrip_tail_discovery.details_match_tail_route(details, origin, transfer, destination)

    @staticmethod
    def _code_matches_target(value: object, target: str) -> bool:
        return ctrip_tail_discovery.code_matches_target(value, target)
