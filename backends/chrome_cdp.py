"""System Chrome CDP backend.

Launches ordinary Google Chrome with a remote-debugging port, then attaches via
CDP. This avoids Playwright's direct browser launch path while keeping a
persistent provider profile.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import subprocess
import urllib.request
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from .base import BrowserBackend
from .chrome import _find_system_chrome

LOGGER = logging.getLogger(__name__)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class CdpChromeBackend(BrowserBackend):
    """Attach to a normally launched Chrome process over CDP."""

    def __init__(self) -> None:
        self._playwright = None
        self._chrome_path: str | None = None
        self._headless = True
        self._profile_root: Path | None = None
        self._contexts: dict[int, BrowserContext] = {}
        self._browsers: dict[int, Browser] = {}
        self._processes: dict[int, subprocess.Popen] = {}

    async def start(self, headless: bool, browser_channel: str | None, **kwargs) -> None:
        chrome_path = _find_system_chrome()
        if chrome_path is None:
            raise RuntimeError("System Chrome not found.")
        self._playwright = await async_playwright().start()
        self._chrome_path = chrome_path
        self._headless = headless
        user_data_dir = kwargs.get("user_data_dir")
        self._profile_root = Path(user_data_dir) if user_data_dir else Path("runtime") / "browser_profiles" / "chrome_cdp"
        self._profile_root.mkdir(parents=True, exist_ok=True)
        LOGGER.info("System Chrome CDP backend using: %s", chrome_path)

    async def stop(self) -> None:
        for context in list(self._contexts.values()):
            try:
                await context.close()
            except Exception as error:
                LOGGER.debug("cdp context close skipped: %s", error)
        for browser in list(self._browsers.values()):
            try:
                await browser.close()
            except Exception as error:
                LOGGER.debug("cdp browser close skipped: %s", error)
        for proc in list(self._processes.values()):
            try:
                proc.terminate()
            except Exception:
                pass
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as error:
                LOGGER.debug("playwright stop skipped: %s", error)
        self._playwright = None
        self._chrome_path = None
        self._profile_root = None
        self._contexts.clear()
        self._browsers.clear()
        self._processes.clear()

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
        port = _free_port()
        args = [
            self._chrome_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            "about:blank",
        ]
        if self._headless:
            args.insert(1, "--headless=new")
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        endpoint = f"http://127.0.0.1:{port}"
        await self._wait_for_cdp(endpoint)
        browser = await self._playwright.chromium.connect_over_cdp(endpoint)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        if extra_http_headers:
            await context.set_extra_http_headers(extra_http_headers)
        self._contexts[id(context)] = context
        self._browsers[id(context)] = browser
        self._processes[id(context)] = proc
        return context

    async def new_page(self, context: BrowserContext) -> Page:
        pages = context.pages
        return pages[0] if pages else await context.new_page()

    async def close_context(self, context: BrowserContext) -> None:
        if context is None:
            return
        context_id = id(context)
        browser = self._browsers.pop(context_id, None)
        proc = self._processes.pop(context_id, None)
        try:
            if browser is not None:
                await browser.close()
            else:
                await context.close()
        except Exception as error:
            LOGGER.debug("cdp close skipped: %s", error)
        finally:
            self._contexts.pop(context_id, None)
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:
                    pass

    async def _wait_for_cdp(self, endpoint: str) -> None:
        last_error: Exception | None = None
        for _ in range(80):
            try:
                with urllib.request.urlopen(f"{endpoint}/json/version", timeout=0.25) as response:
                    if response.status == 200:
                        return
            except Exception as exc:
                last_error = exc
            await asyncio.sleep(0.25)
        raise RuntimeError(f"Chrome CDP endpoint did not become ready: {last_error}")
