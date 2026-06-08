import logging

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from .base import BrowserBackend

LOGGER = logging.getLogger(__name__)


class PlaywrightBackend(BrowserBackend):
    """Standard Playwright backend (default)."""

    def __init__(self) -> None:
        self._playwright = None
        self._browser: Browser | None = None

    async def start(self, headless: bool, browser_channel: str | None, **kwargs) -> None:
        self._playwright = await async_playwright().start()
        chromium = self._playwright.chromium
        self._browser = await chromium.launch(
            headless=headless,
            channel=browser_channel,
        )

    async def stop(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as error:
                LOGGER.debug("browser close skipped: %s", error)
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as error:
                LOGGER.debug("playwright stop skipped: %s", error)
        self._browser = None
        self._playwright = None

    async def new_context(
        self,
        *,
        storage_state: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        locale: str | None = None,
        timezone_id: str | None = None,
        **kwargs,
    ) -> BrowserContext:
        if self._browser is None:
            raise RuntimeError("BrowserBackend is not started")
        return await self._browser.new_context(
            storage_state=storage_state,
            extra_http_headers=extra_http_headers,
            locale=locale,
            timezone_id=timezone_id,
        )

    async def new_page(self, context: BrowserContext) -> Page:
        return await context.new_page()

    async def close_context(self, context: BrowserContext) -> None:
        if context is None:
            return
        try:
            await context.close()
        except Exception as error:
            LOGGER.debug("context close skipped: %s", error)
