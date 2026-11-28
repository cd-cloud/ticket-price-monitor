"""One loop owns experimental browser objects across sequential user tasks.

No query scheduler, credential reader, CAPTCHA handler or profile rotation.
The retained page is only a transport/session resource, never a cached quote.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError as FutureCancelled
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
import threading
import time
from uuid import uuid4


class RetainedSessionUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("没有可复用的成功结果页（可能已过期或关闭）。请先用官网表单取得参考结果并保留会话；本次未新开浏览器或提交查询。")


@dataclass
class Lease:
    automation: object
    page: object
    context: object
    keep: bool = False
    retention_reason: str | None = None
    reused: bool = False


class ExperimentalBrowserSession:
    def __init__(self, *, idle_seconds=600, max_age_seconds=1800, clock=time.monotonic):
        self.idle_seconds, self.max_age_seconds, self.clock = idle_seconds, max_age_seconds, clock
        self._lock = asyncio.Lock()
        self._record = None
        self._timer = None
        self._poisoned = False

    @staticmethod
    def _key(config, provider):
        # Compare private configuration in memory; never serialize headers/paths
        # into task evidence. Profile preference changes invalidate reuse too.
        state = config.runtime_dir / "browser_profiles" / "profile_state.json"
        stamp = state.stat().st_mtime_ns if state.exists() else None
        return deepcopy((config.runtime_dir, config.browser_backend, config.browser_backend_fallbacks,
                         config.browser_channel, config.automation_backend, config.headless, provider, stamp))

    async def _dispose(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None
        record, self._record = self._record, None
        if not record:
            return
        try:
            # Always stop the automation even if page/context close fails.
            try:
                async with asyncio.timeout(5):
                    await record["lease"].automation._close_context_quietly(record["lease"].context)
            finally:
                async with asyncio.timeout(5):
                    await record["manager"].__aexit__(None, None, None)
        except BaseException:
            # Do not open another browser against an uncertain profile owner.
            self._poisoned = True
            raise

    async def close(self):
        async with self._lock:
            await self._dispose()
            if self._poisoned:
                raise RuntimeError("实验浏览器清理结果不确定，请重启应用后再试。")

    async def snapshot(self):
        # Runs on the owner loop; do not wait for an active lease's lock.
        if self._poisoned:
            return {"state": "error", "error_code": "cleanup_uncertain"}
        if not self._record:
            return {"state": "closed", "reused": False}
        record = self._record
        if record["lease"].page is not None and record["lease"].page.is_closed():
            return {"state": "closed", "error_code": "page_closed", "reused": False}
        return {
            "state": "in_use" if record.get("in_use") else "retained",
            "mode": record.get("mode"),
            "generation": record["id"],
            "use_number": record["uses"],
            "reused": record["uses"] > 1,
            "retention_reason": record.get("retention_reason"),
        }

    async def _expire(self, generation):
        async with self._lock:
            record = self._record
            if not record or record["id"] != generation:
                return
            if self.clock() < record["expires"]:
                return  # Another task already renewed this generation.
            try:
                await self._dispose()
            except Exception:
                # Poisoned state blocks further starts; never log raw browser errors.
                pass

    @asynccontextmanager
    async def lease(self, config, provider, factory, *, mode, callback=None, require_existing=False):
        if mode not in {"fresh", "reuse", "persistent"}:
            raise ValueError("Unsupported experimental browser session mode.")
        async with self._lock:
            if self._poisoned:
                raise RuntimeError("实验浏览器清理结果不确定，请重启应用后再试。")
            now = self.clock()
            key = self._key(config, provider)
            record = self._record
            reason = "first_use"
            if record:
                if mode == "fresh":
                    reason = "fresh_requested"
                elif key != record["key"]:
                    reason = "configuration_changed"
                elif record.get("mode") == "reuse" and (
                    now >= record["expires"] or now - record["born"] >= self.max_age_seconds
                ):
                    reason = "expired"
                elif record["lease"].page.is_closed():
                    reason = "page_closed"
                else:
                    reason = "same_page_context"
                if reason != "same_page_context":
                    await self._dispose()
                    record = None
            reused = record is not None
            if require_existing and (not reused or mode == "fresh"):
                raise RetainedSessionUnavailable()
            if self._timer:
                self._timer.cancel()
                self._timer = None
            evidence = {
                "mode": mode, "reused": reused, "reason": reason, "state": "opening",
                "idle_seconds": self.idle_seconds if mode == "reuse" else None,
                "max_age_seconds": self.max_age_seconds if mode == "reuse" else None,
            }

            def publish(**changes):
                evidence.update(changes)
                if callback:
                    callback(deepcopy(evidence))

            try:
                if record is None:
                    publish()
                    manager = factory(config, {})
                    try:
                        automation = await manager.__aenter__()
                    except BaseException:
                        # BrowserAutomation.start can fail after starting a driver.
                        try:
                            async with asyncio.timeout(5):
                                await manager.__aexit__(None, None, None)
                        except BaseException:
                            self._poisoned = True
                            raise
                        raise
                    # Register ownership before creating a page so failures close
                    # the automation instead of leaking its browser process.
                    record = {
                        "id": uuid4().hex, "born": self.clock(), "expires": self.clock(),
                        "key": key, "manager": manager, "uses": 0, "in_use": False,
                        "lease": Lease(automation, None, None),
                    }
                    self._record = record
                    page, context = await automation._new_page(provider)
                    record["lease"].page, record["lease"].context = page, context
                    # Browser startup may create/update profile metadata.
                    record["key"] = self._key(config, provider)
                lease = record["lease"]
                lease.keep = False
                lease.retention_reason = None
                lease.reused = reused
                record["mode"] = mode
                record["in_use"] = True
                record["uses"] += 1
                publish(state="in_use", generation=record["id"], use_number=record["uses"])
                yield lease
                record["in_use"] = False
                record["retention_reason"] = lease.retention_reason
                if mode in {"reuse", "persistent"} and lease.keep and not lease.page.is_closed():
                    if mode == "persistent":
                        publish(
                            state="retained",
                            retention_reason=lease.retention_reason,
                            idle_remaining_seconds=None,
                        )
                        return
                    record["expires"] = min(self.clock() + self.idle_seconds,
                                            record["born"] + self.max_age_seconds)
                    delay = record["expires"] - self.clock()
                    if delay > 0:
                        loop = asyncio.get_running_loop()
                        self._timer = loop.call_later(
                            delay, lambda: loop.create_task(self._expire(record["id"])),
                        )
                        publish(
                            state="retained",
                            retention_reason=lease.retention_reason,
                            idle_remaining_seconds=round(delay, 1),
                        )
                        return
                await self._dispose()
                publish(state="closed")
            except BaseException:
                if record:
                    record["in_use"] = False
                await self._dispose()
                # Audit cancellation must not prevent cleanup or replace the
                # original exception. The service records final disposition.
                raise


class ExperimentalSessionRunner:
    """Lazy dedicated thread; Playwright objects never cross event loops."""

    def __init__(self, *, idle_seconds=600, max_age_seconds=1800):
        self._guard = threading.Lock()
        self._thread = None
        self._loop = None
        self._pool = None
        self._closed = False
        self._limits = dict(idle_seconds=idle_seconds, max_age_seconds=max_age_seconds)

    def _start(self):
        with self._guard:
            if self._closed:
                raise RuntimeError("实验浏览器服务已关闭。")
            if self._thread is not None:
                return
            ready = threading.Event()

            def worker():
                with asyncio.Runner() as runner:
                    self._loop = runner.get_loop()
                    self._pool = ExperimentalBrowserSession(**self._limits)
                    ready.set()
                    self._loop.run_forever()

            self._thread = threading.Thread(target=worker, name="airchina-browser-owner", daemon=True)
            try:
                self._thread.start()
            except BaseException:
                self._thread = None
                raise
            ready.wait()

    def run(self, operation):
        self._start()

        async def execute():
            return await operation(self._pool)

        try:
            return asyncio.run_coroutine_threadsafe(execute(), self._loop).result()
        except FutureCancelled:
            raise asyncio.CancelledError() from None

    def snapshot(self):
        with self._guard:
            if self._loop is None or self._loop.is_closed():
                return {"state": "closed", "reused": False}
            loop, pool = self._loop, self._pool
        return asyncio.run_coroutine_threadsafe(pool.snapshot(), loop).result(timeout=5)

    def close_idle(self):
        # Caller holds the application admission lock (no active experiment).
        with self._guard:
            if self._loop is None or self._loop.is_closed():
                return
            loop, pool = self._loop, self._pool
        asyncio.run_coroutine_threadsafe(pool.close(), loop).result(timeout=12)

    def close(self):
        with self._guard:
            if self._closed:
                return
            self._closed = True
        if self._thread is not None:
            try:
                self.close_idle()
            finally:
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join(timeout=12)
