"""Local cross-process admission for Air China, not a CAPTCHA bypass.

Processes using the same runtime directory share an OS lock, short reference
cache, start spacing and a persistent manual-reset circuit breaker. No worker
is spawned: cancellation and browser cleanup finish before releasing the lock.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time


SUCCESS = {"reference_fares_observed", "fare_products_observed"}
BLOCKS = {"needs_verification", "blocked", "rate_limited"}


def retry_after_seconds(value, *, now=None):
    """Parse only Retry-After; never read/store other response header values."""
    if not isinstance(value, str) or len(value) > 128:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        return int(value)
    try:
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            return None
        return max(0, math.ceil(target.timestamp() - (time.time() if now is None else now)))
    except (ValueError, TypeError, OverflowError):
        return None


class AirChinaQueryGate:
    def __init__(self, runtime_dir, *, ttl=120, min_interval=30, clock=time.time):
        self.folder = Path(runtime_dir) / "airchina_admission"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.db = self.folder / "state.sqlite"
        self.lock_path = self.folder / "query.lock"
        self.ttl, self.min_interval, self.clock = ttl, min_interval, clock
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS control (
                    id INTEGER PRIMARY KEY CHECK(id=1), paused TEXT,
                    not_before REAL NOT NULL DEFAULT 0, last_start REAL NOT NULL DEFAULT 0,
                    running INTEGER NOT NULL DEFAULT 0, browser_runs INTEGER NOT NULL DEFAULT 0,
                    cache_hits INTEGER NOT NULL DEFAULT 0, pauses INTEGER NOT NULL DEFAULT 0);
                INSERT OR IGNORE INTO control(id) VALUES(1);
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY, observed REAL NOT NULL, report TEXT NOT NULL);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.db, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _try_lock(self):
        handle = self.lock_path.open("a+b")
        try:
            if __import__("os").name == "nt":
                import msvcrt
                if handle.seek(0, 2) == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except (BlockingIOError, OSError):
            handle.close()
            return None

    @staticmethod
    def _unlock(handle):
        try:
            if __import__("os").name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    @staticmethod
    def key(route, *, flight_no, entry_mode, session_mode, manual, notices, environment=""):
        # Full request/filter and interaction identity; no engine difference:
        # native UI and FlyClaw share the same underlying validated transport.
        payload = {"route": asdict(route), "flight_no": flight_no,
                   "entry": entry_mode, "session": session_mode,
                   "manual": manual, "notices": notices, "environment": environment, "schema": 2}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def status(self):
        with self.connection() as db:
            row = dict(db.execute("SELECT * FROM control WHERE id=1").fetchone())
        handle = self._try_lock()
        active = handle is None
        if handle is not None:
            try:
                # Recheck while holding the OS lock; an old running marker is
                # not a reason to leave the UI resume button disabled forever.
                with self.connection() as db:
                    row = dict(db.execute("SELECT * FROM control WHERE id=1").fetchone())
                    if row["running"] and not row["paused"]:
                        db.execute("UPDATE control SET paused='interrupted',pauses=pauses+1 WHERE id=1")
                        db.execute("DELETE FROM cache")
                        row["paused"] = "interrupted"
            finally:
                self._unlock(handle)
        row.pop("id")
        return {**row, "paused": bool(row["paused"]), "pause_reason": row["paused"],
                "active": active,
                "retry_after_seconds": max(0, math.ceil(row["not_before"] - self.clock())),
                "cache_ttl_seconds": self.ttl, "min_interval_seconds": self.min_interval}

    def pause(self, reason, *, retry_after=None):
        reason = reason if reason in BLOCKS | {"interrupted"} else "blocked"
        until = self.clock() + retry_after if retry_after is not None else 0
        with self.connection() as db:
            db.execute("""UPDATE control SET paused=CASE WHEN paused='rate_limited' THEN paused ELSE ? END,
                          not_before=MAX(not_before,?),
                          pauses=pauses+1 WHERE id=1""", (reason, until))
            db.execute("DELETE FROM cache")

    def resume(self, *, confirmed):
        if confirmed is not True:
            raise ValueError("请明确确认已人工检查国航访问状态。")
        handle = self._try_lock()
        if handle is None:
            raise RuntimeError("国航查询仍在运行，请先结束当前任务。")
        try:
            if self.status()["retry_after_seconds"] > 0:
                raise RuntimeError("服务端等待期限尚未结束，不能提前恢复。")
            with self.connection() as db:
                db.execute("UPDATE control SET paused=NULL, running=0 WHERE id=1")
                db.execute("DELETE FROM cache")
            return self.status()
        finally:
            self._unlock(handle)

    @staticmethod
    def paused_report(status):
        return {"state": "provider_paused", "quote_saved": False, "coverage_complete": False,
                "message": "国航自动查询已暂停；请人工检查后明确恢复。未打开页面或重新提交。",
                "query_policy": {"source": "paused", **status}}

    def _cached(self, key):
        now = self.clock()
        with self.connection() as db:
            db.execute("DELETE FROM cache WHERE observed<=? OR observed>?", (now-self.ttl, now))
            row = db.execute("SELECT * FROM cache WHERE key=?", (key,)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE control SET cache_hits=cache_hits+1 WHERE id=1")
        report = json.loads(row["report"])
        report["message"] = "使用短期缓存参考票价；未重新查价，非实时结果。"
        report["query_policy"] = {
            "source": "cache", "observed_at": datetime.fromtimestamp(row["observed"], timezone.utc).isoformat(),
            "age_seconds": round(now-row["observed"], 1), "ttl_seconds": self.ttl,
            "browser_started": False,
        }
        return report

    async def run(self, key, execute, *, cancel_event, progress, cache_allowed=True):
        queued = time.monotonic()
        announced = False
        while True:
            if cancel_event.is_set():
                raise asyncio.CancelledError
            state = self.status()
            if state["paused"]:
                return self.paused_report(state)
            handle = self._try_lock()
            if handle is None:
                if not announced:
                    progress("waiting_query_gate")
                    announced = True
                await asyncio.sleep(.1)
                continue
            try:
                state = self.status()
                if state["running"]:
                    # OS lock was released but no normal finalizer ran.
                    self.pause("interrupted")
                    return self.paused_report(self.status())
                if state["paused"]:
                    return self.paused_report(state)
                if cache_allowed:
                    cached = self._cached(key)
                    if cached:
                        cached["query_policy"]["waited_seconds"] = round(time.monotonic()-queued, 2)
                        return cached
                remaining = max(state["not_before"], state["last_start"] + self.min_interval) - self.clock()
                if remaining <= 0:
                    waited = round(time.monotonic()-queued, 2)
                    with self.connection() as db:
                        db.execute("UPDATE control SET running=1,last_start=?,browser_runs=browser_runs+1 WHERE id=1",
                                   (self.clock(),))
                    try:
                        report = await execute()
                        if report.get("state") in BLOCKS:
                            self.pause(report["state"])
                        # A 429 observed in the network audit takes precedence over
                        # a visible old results list; never cache it.
                        status = self.status()
                        if status["pause_reason"] == "rate_limited":
                            report = self.paused_report(status)
                        observed = self.clock()
                        report = deepcopy(report)
                        report["query_policy"] = {
                            **report.get("query_policy", {}),
                            "source": "live", "observed_at": datetime.fromtimestamp(observed, timezone.utc).isoformat(),
                            "age_seconds": 0, "browser_attempted": True,
                            "waited_seconds": waited,
                            "paused_after_query": status["paused"],
                        }
                        if cache_allowed and not status["paused"] and report.get("state") in SUCCESS:
                            # Only validated reference/product projections. No
                            # notice prose, network data or browser session state.
                            safe = {k: report[k] for k in (
                                "state", "message", "reference_preview", "product_preview",
                                "quote_saved", "coverage_complete", "final_quote_supported", "parser_ready",
                            ) if k in report}
                            with self.connection() as db:
                                db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?)",
                                           (key, observed, json.dumps(safe)))
                                db.execute("""DELETE FROM cache WHERE key NOT IN
                                    (SELECT key FROM cache ORDER BY observed DESC LIMIT 32)""")
                        return report
                    finally:
                        with self.connection() as db:
                            db.execute("UPDATE control SET running=0 WHERE id=1")
            finally:
                self._unlock(handle)
            if not announced:
                progress("waiting_query_gate")
                announced = True
            await asyncio.sleep(min(.1, max(0, remaining)))
