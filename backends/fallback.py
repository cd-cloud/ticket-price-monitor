"""Backend fallback manager — automatically tries multiple browser engines.

Usage in config.json:
    "defaults": {
        "browser_backend": "playwright",
        "browser_backend_fallbacks": ["rebrowser", "patchright"]
    }

Priority:
1. provider.browser_backend (if set)
2. provider.browser_backend_fallbacks (if set)
3. app_config.browser_backend
4. app_config.browser_backend_fallbacks
"""

import logging
from pathlib import Path

from .base import BrowserBackend
from . import backend_definition, create_backend

LOGGER = logging.getLogger(__name__)


class BackendFallbackManager:
    """Manages one or more browser backends with automatic fallback."""

    def __init__(
        self,
        backend_names: list[str],
        headless: bool,
        browser_channel: str | None,
        profile_root: Path | None = None,
    ) -> None:
        self.backend_names = [n for n in backend_names if n]
        self.headless = headless
        self.browser_channel = browser_channel
        self.profile_root = profile_root
        self._backends: dict[str, BrowserBackend] = {}
        self._active_name: str | None = None

    @property
    def active_backend(self) -> BrowserBackend | None:
        if self._active_name is None:
            return None
        return self._backends.get(self._active_name)

    @property
    def active_name(self) -> str | None:
        return self._active_name

    async def start(self) -> str:
        """Try to start backends in priority order. Return the first successful one."""
        for name in self.backend_names:
            if name in self._backends:
                self._active_name = name
                return name
            backend: BrowserBackend | None = None
            try:
                backend = create_backend(name)
                await backend.start(
                    self.headless,
                    self.browser_channel,
                    user_data_dir=self._user_data_dir_for_backend(name),
                )
                self._backends[name] = backend
                self._active_name = name
                LOGGER.info("Backend started: %s", name)
                return name
            except Exception as exc:
                LOGGER.warning("Backend %s failed to start: %s", name, exc)
                if backend is not None:
                    try:
                        await backend.stop()
                    except Exception:
                        pass
        raise RuntimeError(
            f"No browser backend could be started from {self.backend_names}"
        )

    async def ensure_backend(self, name: str) -> BrowserBackend:
        """Return an already-started backend, or start it on demand."""
        if name in self._backends:
            return self._backends[name]
        backend = create_backend(name)
        await backend.start(
            self.headless,
            self.browser_channel,
            user_data_dir=self._user_data_dir_for_backend(name),
        )
        self._backends[name] = backend
        LOGGER.info("On-demand backend started: %s", name)
        return backend

    async def health_check(
        self,
        backend_names: list[str] | None = None,
        *,
        active_only: bool = False,
        deep: bool = False,
    ) -> list[dict[str, str | bool]]:
        """Report backend availability.

        Shallow checks avoid launching unstarted browsers. Deep checks start
        and stop unstarted backends and should be reserved for diagnostics.
        """
        names = backend_names or self.backend_names
        if active_only and self._active_name:
            names = [self._active_name]
        report: list[dict[str, str | bool]] = []
        for name in [item for item in names if item]:
            if name in self._backends:
                report.append({"backend": name, "ok": True, "active": name == self._active_name, "error": ""})
                continue
            if not deep:
                definition = backend_definition(name)
                report.append(
                    {
                        "backend": name,
                        "ok": bool(definition and definition.available),
                        "active": False,
                        "error": "" if definition and definition.available else (
                            definition.unavailable_reason if definition else "unknown backend"
                        ),
                    }
                )
                continue
            backend: BrowserBackend | None = None
            try:
                backend = create_backend(name)
                await backend.start(
                    self.headless,
                    self.browser_channel,
                    user_data_dir=self._user_data_dir_for_backend(name),
                )
                report.append({"backend": name, "ok": True, "active": False, "error": ""})
            except Exception as exc:
                report.append({"backend": name, "ok": False, "active": False, "error": str(exc)})
            finally:
                if backend is not None:
                    try:
                        await backend.stop()
                    except Exception:
                        pass
        return report

    async def try_fallback(self, failed_name: str) -> BrowserBackend | None:
        """After a backend fails, try the next one in the list."""
        seen_failed = False
        for name in self.backend_names:
            if name == failed_name:
                seen_failed = True
                continue
            if not seen_failed:
                continue
            if name in self._backends:
                self._active_name = name
                LOGGER.info("Fallback to already-started backend: %s", name)
                return self._backends[name]
            try:
                backend = create_backend(name)
                await backend.start(
                    self.headless,
                    self.browser_channel,
                    user_data_dir=self._user_data_dir_for_backend(name),
                )
                self._backends[name] = backend
                self._active_name = name
                LOGGER.info("Fallback backend started: %s", name)
                return backend
            except Exception as exc:
                LOGGER.warning("Fallback backend %s failed to start: %s", name, exc)
        return None

    def get_backend(self, name: str | None = None) -> BrowserBackend:
        """Get a specific backend, or the active one."""
        if name and name in self._backends:
            return self._backends[name]
        if self._active_name and self._active_name in self._backends:
            return self._backends[self._active_name]
        raise RuntimeError("No browser backend available")

    async def stop(self) -> None:
        """Stop all started backends."""
        for name, backend in list(self._backends.items()):
            try:
                await backend.stop()
                LOGGER.info("Backend stopped: %s", name)
            except Exception as exc:
                LOGGER.warning("Backend %s stop error: %s", name, exc)
        self._backends.clear()
        self._active_name = None

    def _user_data_dir_for_backend(self, name: str) -> Path | None:
        if self.profile_root is None:
            return None
        return self.profile_root / name
