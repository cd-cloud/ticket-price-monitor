"""One admitted query, two isolated provider runs; no provider I/O lives here."""
import asyncio
import threading
from copy import deepcopy

from tail_jobs import now_iso, TailDiscoveryJobManager


PROVIDERS = ("ctrip", "feizhu")
ACTIVE = {"queued", "running", "needs_verification", "cancelling"}


def checkpoint_root(job):
    return str((job.get("result") or {}).get("checkpoint_job_id")
               or (job.get("progress") or {}).get("checkpoint_job_id")
               or (job.get("payload") or {}).get("_checkpoint_job_id") or job["job_id"])


def batch_children(job):
    return ((job.get("result") or {}).get("children")
            or (job.get("progress") or {}).get("children") or [])


class TailBatchRunner:
    """Holds the application's global admission lock for the entire batch.

    Child services receive private locks, never a user-controlled bypass flag.
    They have separate budgets, checkpoints, diagnostics and provider cooldowns.
    """

    def __init__(self, manager):
        self.manager = manager
        self.repo = manager.repository

    async def run(self, payload, progress_callback=None, cancel_checker=None,
                  verification_continue_checker=None):
        plans = payload.get("_batch_children")
        if not payload.get("_job_id") or not isinstance(plans, list) or len(plans) != 2:
            raise ValueError("联合查询需要通过 submit_tail_discovery 创建父任务。")
        if not self.manager.run_lock.acquire(blocking=False):
            raise RuntimeError("已有查询正在运行，请稍后再试。")
        children = {}

        def snapshot(child):
            result, progress = child.get("result") or {}, child.get("progress") or {}
            return {
                "provider": child["payload"]["provider"], "job_id": child["job_id"],
                "status": child["status"], "payload": child["payload"],
                "progress": progress, "error": child.get("error"),
                "checkpoint_job_id": checkpoint_root(child),
                "saved": result.get("saved", progress.get("saved", 0)),
                "reused": child.get("reused", False),
            }

        def aggregate():
            rows = [snapshot(children[p]) for p in PROVIDERS if p in children]
            verifying = any(c["status"] == "needs_verification" for c in rows)
            return {
                "stage": "needs_verification" if verifying else "running",
                "message": ("携程等待人工验证；飞猪独立运行，已保存结果可先查看。" if verifying
                            else "双平台独立查询；预算分别计算，结果不代表可出票或全量库存。"),
                "children": rows,
                "saved": sum(int(c["saved"] or 0) for c in rows),
                "attempted": sum(int(c["progress"].get("attempted") or 0) for c in rows),
                "failed": sum(c["status"] == "failed" for c in rows),
            }

        def publish():
            progress = aggregate()
            if progress_callback:
                progress_callback(deepcopy(progress))
            return progress

        def persist(child):
            child["updated_at"] = now_iso()
            self.repo.upsert_tail_discovery_job(child)
            publish()

        async def run_child(plan):
            provider = plan["provider"]
            child = children[provider]
            if plan.get("reuse"):
                return
            if cancel_checker and cancel_checker():
                child.update(status="cancelled", finished_at=now_iso())
                persist(child)
                return
            child.update(status="running", started_at=now_iso())
            persist(child)

            def progress_callback_child(progress):
                child["progress"] = progress
                child["status"] = "needs_verification" if progress.get("stage") == "needs_verification" else "running"
                if cancel_checker and cancel_checker():
                    child["status"] = "cancelling"
                persist(child)

            try:
                if plan.get("preflight_error"):
                    raise ValueError(plan["preflight_error"])
                service = self.manager.make_service(
                    run_lock=threading.Lock(),
                    queue_file=self.manager.config.runtime_dir / f"tail_discovery_queue_{provider}.json",
                )
                result = await service.run(
                    {**plan["payload"], "_job_id": child["job_id"]},
                    progress_callback=progress_callback_child,
                    cancel_checker=cancel_checker,
                    verification_continue_checker=verification_continue_checker if provider == "ctrip" else None,
                )
                report = result.get("report") or {}
                status = ("cancelled" if result.get("cancelled") else "paused" if result.get("stop_reason")
                          else "failed" if report.get("failed") else "finished")
                if report.get("failed"):
                    reasons = [str(a["error"]) for a in report.get("attempts", [])
                               if a.get("status") == "failed" and a.get("error")]
                    child["error"] = ("；".join(dict.fromkeys(reasons))
                                      or f"{report['failed']} 个查询单元失败，可继续未完成项。")[:1500]
                child.update(result=result, progress=result.get("progress") or {}, status=status,
                             finished_at=now_iso())
            except asyncio.CancelledError:
                child.update(status="interrupted", error="联合任务中断，已完成结果保留。", finished_at=now_iso())
                persist(child)
                raise
            except Exception as exc:
                # A provider failure must not cancel its sibling or lose progress.
                child.update(status="failed", error=str(exc), finished_at=now_iso())
            persist(child)

        tasks = []
        try:
            for plan in plans:
                if plan.get("reuse"):
                    child = self.repo.fetch_tail_discovery_job(plan["job_id"])
                    if not child:
                        raise ValueError("原平台子任务不存在，请重新查询。")
                    if child["status"] in ACTIVE:
                        child = TailDiscoveryJobManager._normalize_persisted_job(child)
                    child["reused"] = True
                else:
                    child = {
                        "job_id": plan["job_id"], "status": "queued",
                        "created_at": now_iso(), "updated_at": now_iso(),
                        "payload": {**plan["payload"], "_parent_job_id": payload["_job_id"]},
                        "progress": {}, "result": None, "error": None,
                    }
                    self.repo.upsert_tail_discovery_job(child)
                children[plan["provider"]] = child
            publish()
            tasks = [asyncio.create_task(run_child(plan)) for plan in plans]
            await asyncio.gather(*tasks)
            progress = aggregate()
            statuses = [c["status"] for c in children.values()]
            if cancel_checker and cancel_checker():
                status = "cancelled"
            elif all(s == "finished" for s in statuses):
                status = "finished"
            elif all(s == "failed" for s in statuses):
                status = "failed"
            elif all(s in {"paused", "interrupted", "cancelled"} for s in statuses):
                status = "paused"
            else:
                status = "partial"
            progress.update(stage=status, message={
                "finished": "双平台任务均已结束，请分别查看平台结果与待核实事项。",
                "partial": "部分平台未完成；另一平台的结果已保留，可继续未完成平台。",
                "failed": "两个平台均未完成，请查看各平台原因后重试。",
                "paused": "查询已暂停，可继续未完成平台。",
                "cancelled": "已停止双平台查询，已保存结果保留。",
            }[status])
            return {"running": False, "saved": progress["saved"], "children": progress["children"],
                    "progress": progress, "terminal_status": status, "cancelled": status == "cancelled",
                    "errors": [f"{p}: {c['error']}" for p, c in children.items() if c.get("error")]}
        finally:
            # Do not release global admission until both child coroutines have exited.
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self.manager.run_lock.release()
