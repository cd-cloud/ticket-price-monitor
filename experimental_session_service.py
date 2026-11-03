from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from urllib.parse import urlsplit

from local_session_state_store import LocalSessionStateStore, SessionStateError, restore_session_state
from providers.airchina_page import HOME_URL


class ExperimentalSessionService:
    def __init__(self, *, query_service, session_runner, run_lock, guard=None, timeout_seconds=900):
        self.query_service = query_service
        self.session_runner = session_runner
        self.run_lock = run_lock
        self._guard = guard or threading.RLock()
        self.timeout_seconds = timeout_seconds
        self.store = LocalSessionStateStore(query_service.config_manager)
        self._state = {"state": "closed", "reused": False}
        self._control = None
        self._thread = None
        self._backup_requested = False
        self._closing = False

    def status(self):
        with self._guard:
            active = self._control is not None
            state = dict(self._state)
            backup_override = state.pop("backup", None)
        if not active and state.get("state") != "error":
            try:
                state = self.session_runner.snapshot()
                if state.get("state") == "retained":
                    state["state"] = "ready"
            except Exception:
                state = {"state": "error", "error_code": "session_status_unavailable"}
        state["backup"] = backup_override or self.store.status()
        return state

    def mark_closed(self):
        self._set_state(state="closed", error_code=None, retention_reason=None)

    def prepare(self, *, restore=False):
        if not isinstance(restore, bool):
            raise ValueError("恢复参数必须为布尔值。")
        with self._guard:
            if self._closing or self._control is not None or not self.run_lock.acquire(blocking=False):
                raise RuntimeError("已有查询或会话准备正在运行，请稍后再试。")
            cancel, resume = threading.Event(), threading.Event()
            self._control = (cancel, resume)
            self._backup_requested = False
            self._state = {
                "state": "preparing",
                "restore_requested": restore,
                "reused": False,
                "error_code": None,
            }
            try:
                self._thread = threading.Thread(
                    target=self._run,
                    args=(cancel, resume, restore),
                    name="airchina-session-prepare",
                    daemon=True,
                )
                self._thread.start()
            except BaseException:
                self._control = None
                self._thread = None
                self._state = {"state": "error", "error_code": "prepare_start_failed"}
                self.run_lock.release()
                raise
        return self.status()

    def ready(self, *, backup=False):
        if not isinstance(backup, bool):
            raise ValueError("备份参数必须为布尔值。")
        with self._guard:
            if not self._control or self._state.get("state") not in {"waiting_user", "preparing"}:
                raise RuntimeError("当前没有等待确认的国航会话。")
            cancel, resume = self._control
            if cancel.is_set():
                raise RuntimeError("会话准备正在取消。")
            self._backup_requested = backup
            self._state.update(state="validating", error_code=None)
            resume.set()
            return dict(self._state)

    def cancel(self):
        with self._guard:
            if not self._control:
                raise RuntimeError("当前没有进行中的会话准备。")
            self._control[0].set()
            self._state["state"] = "cancelling"
            return dict(self._state)

    def delete_backup(self):
        with self._guard:
            if self._closing or self._control is not None or not self.run_lock.acquire(blocking=False):
                raise RuntimeError("已有查询或会话准备正在运行，请稍后再试。")
            try:
                return self.store.delete()
            finally:
                self.run_lock.release()

    def close(self):
        with self._guard:
            self._closing = True
            control = self._control
            thread = self._thread
            if control:
                control[0].set()
        if thread:
            thread.join(timeout=15)

    def is_idle(self):
        with self._guard:
            return self._control is None and (self._thread is None or not self._thread.is_alive())

    def _set_state(self, **changes):
        with self._guard:
            self._state.update(changes)

    def _run(self, cancel, resume, restore):
        try:
            self.session_runner.run(lambda pool: self._prepare(pool, cancel, resume, restore))
        except asyncio.CancelledError:
            self._close_after_terminal("closed", "cancelled")
        except TimeoutError:
            self._close_after_terminal("error", "prepare_timeout")
        except SessionStateError as exc:
            self._close_after_terminal("error", exc.code)
        except Exception:
            self._close_after_terminal("error", "prepare_failed")
        finally:
            with self._guard:
                self._control = None
                self._backup_requested = False
                self.run_lock.release()

    def _close_after_terminal(self, state, error_code):
        try:
            self.session_runner.close_idle()
        except Exception:
            state, error_code = "error", "cleanup_uncertain"
        self._set_state(state=state, error_code=error_code)

    async def _prepare(self, pool, cancel, resume, restore):
        async def execute():
            config = replace(
                self.query_service.get_config(),
                headless=False,
                browser_backend="chrome",
                browser_backend_fallbacks=[],
            )
            provider = config.providers.get("airchina")
            if provider is None:
                raise SessionStateError("provider_missing")
            from browser_environment import environment_key
            if environment_key(provider) != getattr(self.store, "environment_key", ""):
                # The store is bound at service creation. Never restore/save an
                # old environment's backup into a newly configured context.
                raise SessionStateError("environment_changed_restart_required")
            async with pool.lease(
                config,
                provider,
                self.query_service.automation_factory,
                mode="persistent",
                callback=lambda evidence: self._set_state(**evidence),
            ) as lease:
                if restore:
                    state = self.store.load()
                    counts = await restore_session_state(lease.context, state)
                    self._set_state(restored=counts)
                if restore or not self._is_home(lease.page.url):
                    response = await lease.page.goto(
                        HOME_URL,
                        wait_until="domcontentloaded",
                        timeout=provider.timeout_ms,
                    )
                    if getattr(response, "status", 0) >= 400:
                        raise SessionStateError("blocked")
                self._set_state(state="waiting_user", error_code=None)
                while True:
                    while not resume.is_set():
                        if cancel.is_set():
                            raise asyncio.CancelledError()
                        await asyncio.sleep(.1)
                    resume.clear()
                    if cancel.is_set():
                        raise asyncio.CancelledError()
                    access = await self._read_access(lease.page)
                    if access["ready"]:
                        break
                    self._set_state(state="waiting_user", error_code=access["error_code"])
                if self._backup_requested:
                    try:
                        state = await lease.context.storage_state()
                        backup = self.store.save(state)
                    except SessionStateError as exc:
                        backup = {"available": False, "error_code": exc.code}
                    except Exception:
                        backup = {"available": False, "error_code": "backup_failed"}
                    self._set_state(backup=backup)
                lease.keep = True
                lease.retention_reason = "manual_session_prepared"
                self._set_state(
                    state="ready",
                    retention_reason=lease.retention_reason,
                    error_code=None,
                )

        async def watch_cancel(task):
            while not cancel.is_set():
                await asyncio.sleep(.1)
            task.cancel()

        async with asyncio.timeout(self.timeout_seconds):
            task = asyncio.create_task(execute())
            watcher = asyncio.create_task(watch_cancel(task))
            try:
                if cancel.is_set():
                    task.cancel()
                await task
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)

    @staticmethod
    def _is_home(value):
        try:
            parsed = urlsplit(str(value or ""))
            return parsed.scheme == "https" and parsed.hostname == "www.airchina.com.cn" \
                and parsed.path.rstrip("/") == "/zh-CN"
        except (TypeError, ValueError):
            return False

    @staticmethod
    async def _read_access(page):
        try:
            parsed = urlsplit(str(page.url or ""))
            host = str(parsed.hostname or "").lower().rstrip(".")
            official = parsed.scheme == "https" and (
                host == "airchina.com.cn" or host.endswith(".airchina.com.cn")
            )
            if not official:
                return {"ready": False, "error_code": "unexpected_page"}
            text = await page.locator("body").inner_text(timeout=5000)
        except Exception:
            return {"ready": False, "error_code": "page_read_failed"}
        if any(marker in text for marker in ("安全验证", "请点击上图中的", "请完成验证", "滑动滑块")):
            return {"ready": False, "error_code": "needs_verification"}
        if any(marker in text for marker in ("访问过于频繁", "访问异常", "访问被拒绝")):
            return {"ready": False, "error_code": "blocked"}
        return {"ready": True, "error_code": None}
