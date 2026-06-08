"""Camoufox backend — hardened Firefox with built-in anti-detection.

Camoufox is a modified Firefox that patches the browser at the C++ level to
mimic a real user fingerprint. It returns standard Playwright-compatible
``BrowserContext`` objects, so the rest of the codebase works unchanged.

Docs: https://camoufox.com/python/
"""

import logging

from .base import BrowserBackend

LOGGER = logging.getLogger(__name__)


class CamoufoxBackend(BrowserBackend):
    """Backend that launches Camoufox (anti-detect Firefox)."""

    def __init__(self) -> None:
        self._camoufox = None
        self._context = None

    async def start(self, headless: bool, browser_channel: str | None, **kwargs) -> None:
        try:
            from camoufox.async_api import AsyncCamoufox
        except Exception as exc:
            raise RuntimeError(
                "Camoufox is not installed. Install it with:\n"
                "  pip install camoufox"
            ) from exc

        LOGGER.info("Starting Camoufox backend (headless=%s)", headless)
        self._camoufox = AsyncCamoufox(headless=headless)
        await self._camoufox.__aenter__()
        # AsyncCamoufox.browser holds the actual Playwright Browser instance.

    async def stop(self) -> None:
        if self._camoufox is not None:
            try:
                await self._camoufox.__aexit__(None, None, None)
            except Exception as error:
                LOGGER.debug("camoufox close skipped: %s", error)
        self._camoufox = None
        self._context = None

    async def new_context(
        self,
        *,
        storage_state: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        locale: str | None = None,
        timezone_id: str | None = None,
        **kwargs,
    ) -> "BrowserContext":
        if self._camoufox is None:
            raise RuntimeError("BrowserBackend is not started")

        options: dict = {}
        if extra_http_headers:
            options["extra_http_headers"] = extra_http_headers
        if locale:
            options["locale"] = locale
        if timezone_id:
            options["timezone_id"] = timezone_id
        # Camoufox does not natively support Playwright's storage_state JSON;
        # loading it would silently fail or cause mismatched fingerprints.
        # We intentionally ignore storage_state here — session restoration is
        # handled at a higher level if needed.

        self._context = await self._camoufox.browser.new_context(**options)
        return self._context

    async def new_page(self, context: "BrowserContext") -> "Page":
        return await context.new_page()

    async def close_context(self, context: "BrowserContext") -> None:
        if context is None:
            return
        try:
            await context.close()
        except Exception as error:
            LOGGER.debug("context close skipped: %s", error)
