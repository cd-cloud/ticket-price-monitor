"""System Chrome backend with persistent provider profiles."""

import asyncio
import logging
import platform
import shutil
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

from .base import BrowserBackend

LOGGER = logging.getLogger(__name__)


def _find_system_chrome() -> str | None:
    """Locate the system Google Chrome executable."""
    system = platform.system()

    if system == "Windows":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        for path in candidates:
            if Path(path).exists():
                return path

    elif system == "Darwin":
        path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if Path(path).exists():
            return path

    else:
        for binary_name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            found = shutil.which(binary_name)
            if found:
                return found

    return None


class SystemChromeBackend(BrowserBackend):
    """Backend that launches real system Chrome with persistent profiles."""

    def __init__(self) -> None:
        self._playwright = None
        self._chrome_path: str | None = None
        self._headless = True
        self._profile_root: Path | None = None
        self._contexts: dict[int, BrowserContext] = {}
        self._profile_locks: dict[str, asyncio.Lock] = {}
        self._context_profiles: dict[int, str] = {}

    async def start(self, headless: bool, browser_channel: str | None, **kwargs) -> None:
        chrome_path = _find_system_chrome()
        if chrome_path is None:
            raise RuntimeError(
                "System Chrome not found. Please install Google Chrome or use 'playwright' backend."
            )
        LOGGER.info("System Chrome backend using: %s", chrome_path)

        self._playwright = await async_playwright().start()
        self._chrome_path = chrome_path
        self._headless = headless
        user_data_dir = kwargs.get("user_data_dir")
        self._profile_root = Path(user_data_dir) if user_data_dir else Path("runtime") / "browser_profiles" / "chrome"
        self._profile_root.mkdir(parents=True, exist_ok=True)

    async def stop(self) -> None:
        for context in list(self._contexts.values()):
            try:
                await context.close()
            except Exception as error:
                LOGGER.debug("persistent context close skipped: %s", error)
        for lock in self._profile_locks.values():
            if lock.locked():
                lock.release()
        self._contexts.clear()
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as error:
                LOGGER.debug("playwright stop skipped: %s", error)
        self._playwright = None
        self._chrome_path = None
        self._profile_root = None
        self._context_profiles.clear()
        self._profile_locks.clear()

    async def new_context(
        self,
        *,
        storage_state: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        locale: str | None = None,
        timezone_id: str | None = None,
        **kwargs,
    ) -> BrowserContext:
        if self._playwright is None or self._chrome_path is None or self._profile_root is None:
            raise RuntimeError("BrowserBackend is not started")

        profile_name = str(kwargs.get("profile_name") or "default").strip().lower() or "default"
        profile_dir = self._profile_root / profile_name
        profile_dir.mkdir(parents=True, exist_ok=True)
        lock = self._profile_locks.setdefault(profile_name, asyncio.Lock())
        await lock.acquire()

        # Persistent contexts store cookies/localStorage in user_data_dir, so
        # Playwright storage_state JSON is intentionally ignored here.
        try:
            context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=self._headless,
                executable_path=self._chrome_path,
                extra_http_headers=extra_http_headers,
                locale=locale,
                timezone_id=timezone_id,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-default-browser-check",
                    "--no-first-run",
                ],
            )
        except Exception:
            lock.release()
            raise
        self._contexts[id(context)] = context
        self._context_profiles[id(context)] = profile_name
        return context

    async def new_page(self, context: BrowserContext) -> Page:
        pages = context.pages
        if pages:
            page = pages[0]
            for extra_page in pages[1:]:
                try:
                    await extra_page.close()
                except Exception as error:
                    LOGGER.debug("extra persistent page close skipped: %s", error)
            return page
        return await context.new_page()

    async def close_context(self, context: BrowserContext) -> None:
        if context is None:
            return
        try:
            await context.close()
        except Exception as error:
            LOGGER.debug("context close skipped: %s", error)
        finally:
            context_id = id(context)
            self._contexts.pop(context_id, None)
            profile_name = self._context_profiles.pop(context_id, None)
            if profile_name:
                lock = self._profile_locks.get(profile_name)
                if lock and lock.locked():
                    lock.release()
