"""Local experimental tasks sharing production admission, but not quote storage."""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from config_manager import RouteQuery
from providers.airchina import validate_inspection


ACTIVE = {"queued", "running", "waiting_user", "cancelling"}
STAGES = {
    "queued": "任务已接收。",
    "starting_browser": "正在打开项目浏览器（不是应用内浏览器）。",
    "navigating": "正在打开国航查询页。",
    "reading_results": "正在校验航线并等待列表稳定。",
    "reading_products": "正在读取指定航班产品，不点击预订。",
    "needs_verification": "请在项目浏览器中手动完成验证，再点击继续；程序不会处理验证码。",
    "needs_notice_confirmation": "请自行阅读并确认乘机提示，再点击继续。",
}


def validate_payload(payload):
    allowed = {"origin", "destination", "departure_date", "cabin", "flight_no"}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValueError("只接受机场、出发日期、舱等和可选航班号；不支持往返、多程或凭据字段。")
    for key in ("origin", "destination", "departure_date"):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise ValueError("请填写出发机场、到达机场和出发日期。")
    cabin = payload.get("cabin", "economy")
    if cabin not in ("economy", "business"):
        raise ValueError("目前仅支持 economy / business。")
    flight_no = payload.get("flight_no")
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
    normalized = {key: getattr(route, key) for key in ("origin", "destination", "departure_date", "cabin")}
    return route, flight_no, {**normalized, "flight_no": flight_no}


class ExperimentalQueryService:
    """One worker, explicit admission; no credentials, retries or rescheduling."""

    def __init__(self, *, query_service, repository, run_lock, timeout_seconds=240):
        self.query_service, self.repository, self.run_lock = query_service, repository, run_lock
        self.timeout_seconds = timeout_seconds
        self._guard = threading.RLock()
        self._thread = None
        self._control = None
        self._closing = False
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
                "quote_saved": False, "result": None,
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
                return self._update(job_id, status="running", message="正在重新读取同一页面，不重新提交查询。")
            if action == "cancel":
                cancel.set()
                return self._update(job_id, status="cancelling", message="正在取消并关闭浏览器上下文。")
            raise ValueError("未知任务操作。")

    def _run(self, job_id, route, flight_no, control):
        _, cancel, resume = control

        def progress(stage):
            with self._guard:
                if not cancel.is_set():
                    self._update(job_id, status="running", stage=stage, message=STAGES[stage])

        async def manual(state):
            with self._guard:
                if cancel.is_set():
                    raise asyncio.CancelledError()
                resume.clear()
                self._update(job_id, status="waiting_user", stage=state, message=STAGES[state])
            while not resume.is_set():
                if cancel.is_set():
                    raise asyncio.CancelledError()
                await asyncio.sleep(.1)

        try:
            report = asyncio.run(self.query_service.inspect_airchina_experiment(
                route, flight_no=flight_no, progress=progress, manual_handler=manual,
                cancel_event=cancel, timeout_seconds=self.timeout_seconds,
            ))
            with self._guard:
                if cancel.is_set():
                    raise asyncio.CancelledError()
                observed = report["state"] in {"reference_fares_observed", "fare_products_observed"}
                self._update(
                    job_id, status="completed_reference" if observed else "inconclusive",
                    stage=report["state"], message=report["message"], result=report,
                    finished_at=self.repository.now_iso(),
                )
        except asyncio.CancelledError:
            self._update(job_id, status="cancelled", stage="cancelled", message="已取消；未保存正式报价。",
                         finished_at=self.repository.now_iso(), result=None)
        except TimeoutError:
            self._update(job_id, status="timed_out", stage="timed_out", message="任务总时限已到；未自动重试。",
                         finished_at=self.repository.now_iso(), result=None)
        except Exception as exc:
            # Exception messages may contain tokenized URLs or session material.
            self._update(job_id, status="failed", stage="failed", message="实验查询失败；未自动重试。",
                         error_type=type(exc).__name__, finished_at=self.repository.now_iso(), result=None)
        finally:
            with self._guard:
                self._control = None
                self.run_lock.release()

    def close(self):
        with self._guard:
            self._closing = True
            if self._control:
                self._control[1].set()
            thread = self._thread
        if thread:
            thread.join(timeout=15)
