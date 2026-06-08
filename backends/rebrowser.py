"""Rebrowser backend — drop-in Playwright replacement with stealth patches.

Install:
    py -3.12 -m pip install rebrowser-playwright
    py -3.12 -m rebrowser_playwright install chromium

Usage in config.json:
    "defaults": { "browser_backend": "rebrowser" }
"""

from rebrowser_playwright.async_api import async_playwright

from .playwright import PlaywrightBackend


class RebrowserBackend(PlaywrightBackend):
    """Rebrowser-based backend. API-compatible with Playwright but patches
    Chromium source-level fingerprints (navigator.webdriver, Runtime.enable
    leak, etc.) for better bot evasion.
    """

    async def start(self, headless: bool, browser_channel: str | None, **kwargs) -> None:
        self._playwright = await async_playwright().start()
        chromium = self._playwright.chromium
        self._browser = await chromium.launch(
            headless=headless,
            channel=browser_channel,
        )
