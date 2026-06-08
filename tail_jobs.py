import logging
import inspect
import threading
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable

from data_storage import PriceRepository


LOGGER = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _queue_counts(items: list[Any]) -> dict[str, int]:
    return dict(
        Counter(
            str(item.get("status") or "pending")
            for item in items
            if isinstance(item, dict) and "status" in item
        )
    )


def _normalize_terminal_queue_payload(value: Any) -> Any:
    if isinstance(value, list):
        return [_normalize_terminal_queue_payload(item) for item in value]
    if not isinstance(value, dict):
        return value

    normalized = {key: _normalize_terminal_queue_payload(item) for key, item in value.items()}
    if normalized.get("status") == "running":
        normalized["status"] = "cancelled_before_query"

    items = normalized.get("items")
    if isinstance(items, list):
        counts = _queue_counts(items)
        if counts:
            normalized["counts"] = counts
            normalized["running"] = counts.get("running", 0)
            normalized["pending"] = counts.get("pending", 0)
            for status, count in counts.items():
                normalized[status] = count
    return normalized


class TailDiscoveryJobManager:
    """Small in-process job manager for long-running tail discovery runs."""

    def __init__(
        self,
        runner: Callable[
            [
                dict[str, Any],
                Callable[[dict[str, Any]], None] | None,
                Callable[[], bool] | None,
                Callable[[], bool] | None,
            ],
            dict[str, Any],
        ],
        repository: PriceRepository,
    ) -> None:
        self._runner = runner
        self._repository = repository
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = uuid.uuid4().hex[:12]
        job = {
            "job_id": job_id,
            "status": "queued",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "payload": payload,
            "progress": {
                "stage": "queued",
                "message": "任务已创建，等待后台开始查询。",
                "attempted": 0,
                "matched": 0,
                "failed": 0,
                "no_match": 0,
                "pending": 0,
            },
            "result": None,
            "error": None,
            "cancel_requested": False,
            "continue_requested": False,
        }
        with self._lock:
            self._jobs[job_id] = job
        self._persist(job)

        worker = threading.Thread(target=self._run_job, args=(job_id, payload), daemon=True)
        worker.start()
        return self.get(job_id)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                persisted = self._repository.fetch_tail_discovery_job(job_id)
                if persisted:
                    return self._normalize_persisted_job(persisted) or persisted
                raise KeyError(job_id)
            return dict(job)

    def latest(self) -> dict[str, Any] | None:
        with self._lock:
            if not self._jobs:
                return self._normalize_persisted_job(self._repository.latest_tail_discovery_job())
            latest_job = max(self._jobs.values(), key=lambda item: str(item.get("created_at") or ""))
            return dict(latest_job)

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        persisted = [
            self._normalize_persisted_job(item) or item
            for item in self._repository.fetch_tail_discovery_jobs(limit=limit)
        ]
        with self._lock:
            merged = {str(item.get("job_id")): item for item in persisted if item.get("job_id")}
            for job in self._jobs.values():
                merged[str(job.get("job_id"))] = dict(job)
        return sorted(
            merged.values(),
            key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
            reverse=True,
        )[: max(1, min(int(limit), 100))]

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                persisted = self._repository.fetch_tail_discovery_job(job_id)
                if not persisted:
                    raise KeyError(job_id)
                return self._normalize_persisted_job(persisted) or persisted
            if job.get("status") in {"finished", "failed", "cancelled", "interrupted"}:
                return dict(job)
            progress = dict(job.get("progress") or {})
            progress["stage"] = "cancelling"
            progress["message"] = "已请求取消；当前目的地结束后会停止。"
            job["cancel_requested"] = True
            job["status"] = "cancelling"
            job["progress"] = progress
            job["updated_at"] = now_iso()
            self._persist(job)
            return dict(job)

    def continue_after_verification(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                persisted = self._repository.fetch_tail_discovery_job(job_id)
                if not persisted:
                    raise KeyError(job_id)
                raise ValueError("This task is no longer active; please retry the verification candidate.")
            if job.get("status") not in {"running", "needs_verification"}:
                return dict(job)
            progress = dict(job.get("progress") or {})
            progress["stage"] = "verification_continued"
            progress["message"] = "已收到继续指令，正在恢复携程甩尾查询。"
            job["continue_requested"] = True
            job["status"] = "running"
            job["progress"] = progress
            job["updated_at"] = now_iso()
            self._persist(job)
            return dict(job)

    @staticmethod
    def _normalize_persisted_job(job: dict[str, Any] | None) -> dict[str, Any] | None:
        if not job:
            return None
        if job.get("status") in {"queued", "running", "needs_verification"}:
            job = dict(job)
            job["status"] = "interrupted"
            progress = dict(job.get("progress") or {})
            progress["stage"] = "interrupted"
            progress["message"] = "服务曾重启，原来的自动化浏览器页面已经关闭；请重新发起甩尾查询。"
            job["progress"] = progress
        if job.get("status") in {"finished", "failed", "cancelled", "interrupted"}:
            job = dict(job)
            job = _normalize_terminal_queue_payload(job)
        return job

    def _update(self, job_id: str, **changes: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.update(changes)
            job["updated_at"] = now_iso()
            self._persist(job)

    def _persist(self, job: dict[str, Any]) -> None:
        try:
            self._repository.upsert_tail_discovery_job(job)
        except Exception:
            LOGGER.exception("failed to persist tail discovery job: %s", job.get("job_id"))

    def _progress_callback(self, job_id: str) -> Callable[[dict[str, Any]], None]:
        def callback(progress: dict[str, Any]) -> None:
            changes: dict[str, Any] = {"progress": progress}
            if progress.get("stage") == "needs_verification":
                changes["status"] = "needs_verification"
            elif progress.get("stage") == "verification_continued":
                changes["status"] = "running"
            elif progress.get("stage") in {"starting", "running"}:
                changes["status"] = "running"
            self._update(job_id, **changes)

        return callback

    def _is_cancel_requested(self, job_id: str) -> bool:
        with self._lock:
            return bool((self._jobs.get(job_id) or {}).get("cancel_requested"))

    def _consume_continue_requested(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or not job.get("continue_requested"):
                return False
            job["continue_requested"] = False
            self._persist(job)
            return True

    def _run_job(self, job_id: str, payload: dict[str, Any]) -> None:
        self._update(
            job_id,
            status="running",
            started_at=now_iso(),
            progress={
                "stage": "starting",
                "message": "后台查询已启动。",
                "attempted": 0,
                "matched": 0,
                "failed": 0,
                "no_match": 0,
                "pending": 0,
            },
        )
        try:
            runner_payload = dict(payload)
            runner_payload["_job_id"] = job_id
            runner_args = [
                runner_payload,
                self._progress_callback(job_id),
                lambda: self._is_cancel_requested(job_id),
                lambda: self._consume_continue_requested(job_id),
            ]
            try:
                parameters = inspect.signature(self._runner).parameters.values()
                accepts_varargs = any(item.kind == inspect.Parameter.VAR_POSITIONAL for item in parameters)
                max_args = 4 if accepts_varargs else len(inspect.signature(self._runner).parameters)
            except Exception:
                max_args = 4
            result = self._runner(*runner_args[:max_args])
            progress = result.get("progress") or result.get("report") or {
                "stage": "finished",
                "message": "任务结束。",
                "attempted": 0,
                "matched": 0,
                "failed": len(result.get("errors") or []),
                "no_match": 0,
                "pending": 0,
            }
            final_status = "cancelled" if result.get("cancelled") else "finished"
            if progress.get("stage") == "needs_verification" or result.get("needs_verification"):
                final_status = "needs_verification"
            if final_status == "cancelled":
                progress = dict(progress)
                progress["stage"] = "cancelled"
                progress["message"] = "任务已取消，已保留取消前完成的结果。"
            self._update(
                job_id,
                status=final_status,
                finished_at=None if final_status == "needs_verification" else now_iso(),
                result=result,
                progress=progress,
            )
        except Exception as exc:
            LOGGER.exception("tail discovery job failed: %s", job_id)
            self._update(
                job_id,
                status="failed",
                finished_at=now_iso(),
                error=str(exc),
                progress={
                    "stage": "failed",
                    "message": str(exc),
                },
            )
