from __future__ import annotations

import threading
from typing import Any


DEFAULT_RUN_SUMMARY = {
    "running": False,
    "saved": 0,
    "errors": [],
}


class RunSummaryStore:
    """Thread-safe holder for the dashboard's latest run summary."""

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self._lock = threading.Lock()
        self._summary = dict(initial or DEFAULT_RUN_SUMMARY)

    def get(self) -> dict[str, Any]:
        with self._lock:
            return self._copy_summary(self._summary)

    def set(self, summary: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._summary = self._copy_summary(summary)
            return self._copy_summary(self._summary)

    def idle(self) -> dict[str, Any]:
        return self.set(DEFAULT_RUN_SUMMARY)

    def running(self, **extra: Any) -> dict[str, Any]:
        return self.set({"running": True, "saved": 0, "errors": [], **extra})

    def finished(self, *, saved: int = 0, errors: list[str] | None = None, **extra: Any) -> dict[str, Any]:
        return self.set({"running": False, "saved": saved, "errors": errors or [], **extra})

    @staticmethod
    def _copy_summary(summary: dict[str, Any]) -> dict[str, Any]:
        copied = dict(summary)
        if isinstance(copied.get("errors"), list):
            copied["errors"] = list(copied["errors"])
        return copied
