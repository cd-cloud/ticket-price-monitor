"""Local experimental tasks sharing production admission, but not quote storage."""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from config_manager import RouteQuery
from providers.airchina import validate_inspection
from providers.airchina_form import validate_entry
from experimental_browser_session import ExperimentalSessionRunner, RetainedSessionUnavailable
from browser_session_manager import AirChinaSessionUnavailable


ACTIVE = {"queued", "running", "waiting_user", "cancelling"}
ACCESS_STATES = {"needs_verification", "needs_notice_confirmation", "blocked", "stale_results", "unexpected_page"}
STAGES = {
    "queued": "任务已接收。",
    "starting_browser": "正在打开项目浏览器（不是应用内浏览器）。",
    "navigating": "正在打开国航查询页。",
    "filling_form": "正在官网选择机场、日期与 1 成人；尚未提交查询。",
    "submitting_form": "正在提交一次官网查询；不重试、不跳转直达入口。",
    "reading_results": "正在校验航线并等待列表稳定。",
    "reading_products": "正在读取指定航班产品，不点击预订。",
    "reading_notices": "正在记录并处理查询通知，不操作验证码、登录、预订或支付。",
    "needs_verification": "请在项目浏览器中手动完成验证，再点击继续；程序不会处理验证码。",
    "needs_notice_confirmation": "请自行阅读并确认乘机提示，再点击继续。",
    "form_inspection": "表单未能确认，已保留项目 Chrome 窗口供检查（不是应用内浏览器）。请勿手动提交查询；检查后点击“结束检查并关闭”。不会自动重填或重提，原任务截止时间仍有效。",
}


def validate_payload(payload):
    allowed = {"origin", "destination", "departure_date", "cabin", "flight_no", "auto_confirm_notices", "entry_mode",
               "interaction_mode", "session_mode"}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValueError("只接受机场、出发日期、舱等和可选航班号；不支持往返、多程或凭据字段。")
    for key in ("origin", "destination", "departure_date"):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise ValueError("请填写出发机场、到达机场和出发日期。")
    cabin = payload.get("cabin", "economy")
    if cabin not in ("economy", "business"):
        raise ValueError("目前仅支持 economy / business。")
    flight_no = payload.get("flight_no")
    auto_confirm = payload.get("auto_confirm_notices", False)
    if not isinstance(auto_confirm, bool):
        raise ValueError("自动处理查询提示必须为布尔值。")
    if flight_no is not None and not isinstance(flight_no, str):
        raise ValueError("航班号必须为字符串。")
    if flight_no is not None:
        flight_no = flight_no.strip().upper() or None
    route = RouteQuery(
        origin=payload["origin"].strip().upper(), destination=payload["destination"].strip().upper(),
        departure_date=payload["departure_date"].strip(), cabin=cabin,
        passengers=1, providers=["airchina"],
    )
    validate_inspection(route, flight_no=flight_no)
    entry_mode = payload.get("entry_mode", "direct")
    validate_entry(route, entry_mode)
    # Legacy API clients retain manual continuation; the UI explicitly selects
    # stop for new unattended tasks. This is not authorization to solve a CAPTCHA.
    interaction_mode = payload.get("interaction_mode", "wait")
    if interaction_mode not in ("stop", "wait"):
        raise ValueError("遇阻处理只支持 stop（直接停止）/ wait（等待人工）。")
    session_mode = payload.get("session_mode", "fresh")
    if session_mode not in ("fresh", "reuse"):
        raise ValueError("浏览器会话只支持 fresh（每任务重启）/ reuse（短时复用）。")
    if entry_mode == "results" and (session_mode != "reuse" or interaction_mode != "stop"):
        raise ValueError("结果页续查仅支持短时复用、遇阻即停；请先通过官网表单取得同条件的参考结果。")
    normalized = {key: getattr(route, key) for key in ("origin", "destination", "departure_date", "cabin")}
    return route, flight_no, {**normalized, "flight_no": flight_no, "auto_confirm_notices": auto_confirm,
                             "entry_mode": entry_mode, "interaction_mode": interaction_mode, "session_mode": session_mode}


class ExperimentalQueryService:
    """One worker, explicit admission; no credentials, retries or rescheduling."""

    def __init__(self, *, query_service, repository, run_lock, timeout_seconds=240):
        self.query_service, self.repository, self.run_lock = query_service, repository, run_lock
        self.timeout_seconds = timeout_seconds
        self._guard = threading.RLock()
        self._thread = None
        self._control = None
        self._closing = False
        self._session_runner = ExperimentalSessionRunner()
        self.repository.interrupt_experimental_queries()

    def submit(self, payload):
        route, flight_no, normalized = validate_payload(payload)
        if "airchina" not in self.query_service.get_config().providers:
            raise ValueError("配置中缺少 airchina；请参考示例配置，不需要启用正式查询。")
        with self._guard:
            if self._closing or not self.run_lock.acquire(blocking=False):
                raise RuntimeError("已有查询正在运行或应用正在关闭，请稍后再试。")
            now = datetime.now(timezone.utc)
            job = {
                "job_id": uuid4().hex, "provider": "airchina", "status": "queued", "stage": "queued",
                "message": STAGES["queued"], "request": normalized,
                "created_at": now.isoformat(), "updated_at": now.isoformat(), "finished_at": None,
                "deadline_at": (now + timedelta(seconds=self.timeout_seconds)).isoformat(),
                "quote_saved": False, "result": None, "notices": [], "diagnostics": None, "entry_evidence": None,
                "access_events": [], "session_evidence": None, "network_diagnostics": None,
            }
            try:
                self.repository.save_experimental_query(job)
                self._control = (job["job_id"], threading.Event(), threading.Event())
                self._thread = threading.Thread(
                    target=self._run, args=(job["job_id"], route, flight_no, self._control),
                    name="airchina-experiment", daemon=True,
                )
                self._thread.start()
            except BaseException:
                self._control = None
                self._thread = None
                self.run_lock.release()
                job.update(status="failed", stage="failed", message="无法启动实验任务。",
                           finished_at=self.repository.now_iso())
                self.repository.save_experimental_query(job)
                raise
            return job

    def get(self, job_id):
        job = self.repository.experimental_query(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def list(self, limit=20):
        return self.repository.experimental_queries(limit)

    def _update(self, job_id, **changes):
        with self._guard:
            job = self.get(job_id)
            job.update(changes, updated_at=self.repository.now_iso())
            self.repository.save_experimental_query(job)
            return job

    def control(self, job_id, action):
        with self._guard:
            job = self.get(job_id)
            if not self._control or self._control[0] != job_id or job["status"] not in ACTIVE:
                raise RuntimeError("任务已结束或会话不可恢复，请重新提交。")
            _, cancel, resume = self._control
            if action == "continue":
                if job["status"] != "waiting_user":
                    raise RuntimeError("当前任务不在等待人工处理状态。")
                resume.set()
                return self._update(job_id, status="running", message=(
                    "正在结束检查并关闭浏览器，不重新读取或提交查询。"
                    if job["stage"] == "form_inspection" else "正在重新读取同一页面，不重新提交查询。"))
            if action == "cancel":
                cancel.set()
                return self._update(job_id, status="cancelling", message="正在取消并关闭浏览器上下文。")
            raise ValueError("未知任务操作。")

    def _run(self, job_id, route, flight_no, control):
        _, cancel, resume = control
        auto_confirm = self.get(job_id)["request"].get("auto_confirm_notices", False)
        entry_mode = self.get(job_id)["request"].get("entry_mode", "direct")
        interaction_mode = self.get(job_id)["request"].get("interaction_mode", "wait")
        session_mode = self.get(job_id)["request"].get("session_mode", "fresh")

        def record_session(evidence):
            self._update(job_id, session_evidence=evidence)
            if cancel.is_set():
                raise asyncio.CancelledError()

        def record_access(state, action):
            if state not in ACCESS_STATES:
                return
            with self._guard:
                job = self.get(job_id)
                entry = job.get("entry_evidence") or {}
                phase = "home_form" if entry_mode == "form" and not entry.get("result_page_observed") else "result_page"
                if job["stage"] in {"reading_results", "reading_products"}:
                    phase = job["stage"]
                events = list(job.get("access_events") or [])
                event = {"state": state, "phase": phase, "action": action}
                if events and all(events[-1].get(k) == v for k, v in event.items()):
                    return
                events.append({**event, "observed_at": self.repository.now_iso()})
                self._update(job_id, access_events=events[-8:])

        def record_notices(entries):
            # Commit BEFORE a click; independent of result so errors, timeout,
            # cancellation and restart never discard previously recorded prose.
            with self._guard:
                self._update(job_id, notices=entries)
                if cancel.is_set():
                    raise asyncio.CancelledError()

        def progress(stage):
            with self._guard:
                if not cancel.is_set():
                    self._update(job_id, status="running", stage=stage, message=STAGES[stage])

        def record_diagnostics(evidence):
            # No page prose or prices: retained even if the overall job is
            # cancelled, times out or fails after list collection.
            self._update(job_id, diagnostics=evidence)
            if cancel.is_set():
                raise asyncio.CancelledError()

        def record_entry(evidence):
            self._update(job_id, entry_evidence=evidence)
            if cancel.is_set():
                raise asyncio.CancelledError()

        def record_network(evidence):
            # Final metadata also survives cancellation/timeout. No cancel check
            # here: this callback is part of the finally cleanup path.
            self._update(job_id, network_diagnostics=evidence)

        async def manual(state):
            with self._guard:
                if cancel.is_set():
                    raise asyncio.CancelledError()
                resume.clear()
                record_access(state, "wait")
                self._update(job_id, status="waiting_user", stage=state, message=STAGES[state])
            while not resume.is_set():
                if cancel.is_set():
                    raise asyncio.CancelledError()
                await asyncio.sleep(.1)

        terminal = {"status": "failed", "stage": "failed", "message": "实验任务意外结束。", "result": None}
        try:
            report = self._session_runner.run(lambda pool: self.query_service.inspect_airchina_experiment(
                route, flight_no=flight_no, progress=progress,
                manual_handler=manual if interaction_mode == "wait" else None,
                cancel_event=cancel, timeout_seconds=self.timeout_seconds,
                auto_confirm_notices=auto_confirm, notice_callback=record_notices,
                diagnostic_callback=record_diagnostics,
                entry_mode=entry_mode, entry_callback=record_entry,
                session_mode=session_mode, session_pool=pool, session_callback=record_session,
                network_callback=record_network,
            ))
            with self._guard:
                # Keep access failures separate from the final task status.
                # They remain available even when cancellation wins this race.
                record_access(report["state"], "stop")
                if cancel.is_set():
                    raise asyncio.CancelledError()
                observed = report["state"] in {"reference_fares_observed", "fare_products_observed"}
                message = report["message"]
                if interaction_mode == "stop" and report["state"] in ACCESS_STATES:
                    message += " 无人值守模式已停止本次任务；不等待人工、不自动重试，也不判定为无票。"
                terminal = dict(
                    status="completed_reference" if observed else "inconclusive",
                    stage=report["state"], message=message, result=report,
                    finished_at=self.repository.now_iso(),
                )
        except asyncio.CancelledError:
            terminal = dict(status="cancelled", stage="cancelled", message="已取消；未保存正式报价。",
                            finished_at=self.repository.now_iso(), result=None)
        except TimeoutError:
            terminal = dict(status="timed_out", stage="timed_out", message="任务总时限已到；未自动重试。",
                            finished_at=self.repository.now_iso(), result=None)
        except (AirChinaSessionUnavailable, RetainedSessionUnavailable) as exc:
            terminal = dict(status="failed", stage="failed", message=str(type(exc)()),
                            error_type=type(exc).__name__,
                            finished_at=self.repository.now_iso(), result=None)
        except Exception as exc:
            # Exception messages may contain tokenized URLs or session material.
            terminal = dict(status="failed", stage="failed", message="实验查询失败；未自动重试。",
                            error_type=type(exc).__name__, finished_at=self.repository.now_iso(), result=None)
        finally:
            # A cancellation can win immediately after a successful lease was
            # retained. Dispose it before releasing admission to another query.
            with self._guard:
                if cancel.is_set() and terminal["status"] == "completed_reference":
                    terminal = dict(status="cancelled", stage="cancelled", message="已取消；未保存正式报价。",
                                    finished_at=self.repository.now_iso(), result=None)
                try:
                    final = self.get(job_id)
                    if terminal["status"] != "completed_reference":
                        cleanup_state = "closed"
                        try:
                            self._session_runner.close_idle()
                        except Exception:
                            cleanup_state = "cleanup_uncertain"
                        evidence = final.get("session_evidence")
                        if evidence:
                            terminal["session_evidence"] = {**evidence, "state": cleanup_state}
                finally:
                    try:
                        # Do not expose terminal status while cleanup/audit are
                        # still pending; UI may stop polling as soon as it sees it.
                        self._update(job_id, **terminal)
                    finally:
                        self._control = None
                        self.run_lock.release()

    def without_browser_session(self, operation=lambda: {"closed": True}):
        """Explicit idle close / profile maintenance, serialized with queries."""
        with self._guard:
            if self._closing or not self.run_lock.acquire(blocking=False):
                raise RuntimeError("已有查询正在运行，请先结束任务。")
        try:
            self._session_runner.close_idle()
            return operation()
        finally:
            self.run_lock.release()

    def close(self):
        with self._guard:
            self._closing = True
            if self._control:
                self._control[1].set()
            thread = self._thread
        if thread:
            thread.join(timeout=15)
        if not thread or not thread.is_alive():
            self._session_runner.close()
