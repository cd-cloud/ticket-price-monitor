import asyncio
import random

from playwright.async_api import Page

from config_manager import ProviderConfig, RouteQuery


class BrowserPageOperations:
    async def _texts_for_selectors(self, page: Page, selectors: list[str], limit: int = 6) -> list[str]:
        texts: list[str] = []
        for selector in selectors:
            try:
                locator = page.locator(selector)
                count = min(await locator.count(), limit)
                for index in range(count):
                    text = (await locator.nth(index).inner_text()).strip()
                    if text:
                        texts.append(text)
            except Exception:
                continue
        return texts

    async def _collect_candidate_texts(self, provider: ProviderConfig, page: Page) -> dict[str, list[str]]:
        selector_sets = {
            "price_candidates": self._selector_list(provider, "lowest_price")
            or [
                "[class*='price']",
                "[class*='Price']",
                "[class*='fare']",
                "[class*='Fare']",
                "[data-testid*='price']",
            ],
            "button_candidates": ["button", "a", "[role='button']"],
        }
        result: dict[str, list[str]] = {}
        for key, selectors in selector_sets.items():
            result[key] = await self._texts_for_selectors(page, selectors, limit=10)
        return result

    async def _fill_first(self, page: Page, selectors: list[str], value: str) -> None:
        last_error: Exception | None = None
        for selector in selectors:
            try:
                await page.locator(selector).first.fill(value)
                return
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"Could not fill any selector from {selectors}") from last_error

    async def _click_first(self, page: Page, selectors: list[str]) -> None:
        last_error: Exception | None = None
        for selector in selectors:
            try:
                await page.locator(selector).first.click()
                return
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"Could not click any selector from {selectors}") from last_error

    async def _click_first_available(self, scope, selectors: list[str], timeout_ms: int) -> bool:
        click_timeout = max(500, min(timeout_ms, 1500))
        for selector in selectors:
            try:
                locator = scope.locator(selector)
                if await locator.count() == 0:
                    continue
                await locator.first.click(timeout=click_timeout, force=True)
                return True
            except Exception:
                continue
        return False

    async def _throttle(self, provider: ProviderConfig) -> None:
        wait_seconds = random.uniform(provider.min_delay_seconds, provider.max_delay_seconds)
        await asyncio.sleep(wait_seconds)

    @staticmethod
    def _selector_list(provider: ProviderConfig, key: str) -> list[str]:
        raw = provider.selectors.get(key)
        if raw is None:
            return []
        if isinstance(raw, str):
            return [raw] if raw.strip() else []
        return [item for item in raw if isinstance(item, str) and item.strip()]

    @staticmethod
    def _build_itinerary_summary(route: RouteQuery) -> str:
        if not route.segments:
            return f"{route.origin}->{route.destination} {route.departure_date}"
        return " / ".join(
            f"{segment.origin}->{segment.destination} {segment.departure_date}"
            for segment in route.segments
        )
