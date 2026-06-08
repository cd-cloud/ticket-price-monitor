"""Patchright backend — patched Playwright with anti-detection fixes.

Install:
    py -3.12 -m pip install patchright
    py -3.12 -m patchright install chromium

Usage in config.json:
    "defaults": { "browser_backend": "patchright" }

Note: Patchright only patches Chromium. Firefox/WebKit are unsupported.
"""

from patchright.async_api import async_playwright

from .playwright import PlaywrightBackend


class PatchrightBackend(PlaywrightBackend):
    """Patchright-based backend. A patched version of Playwright that modifies
    Chromium fingerprints to reduce bot detection.
    """

    async def start(self, headless: bool, browser_channel: str | None, **kwargs) -> None:
        self._playwright = await async_playwright().start()
        chromium = self._playwright.chromium
        self._browser = await chromium.launch(
            headless=headless,
            channel=browser_channel,
        )
