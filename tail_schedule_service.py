from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import re
from typing import Any, Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from tail_service import normalize_tail_discovery_request, normalize_tail_time


ACTIVE_JOB_STATUSES = {"queued", "running", "needs_verification", "cancelling"}
SCHEDULE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
INTERNAL_PAYLOAD_KEYS = {"source_job_id", "expected_itinerary_key", "source_result_id"}


class TailScheduleService:
    """Persist and run recurring tail-discovery submissions through the existing job pipeline."""

    def __init__(
        self,
        *,
        repository: Any,
        submit_tail_discovery: Callable[[dict[str, Any]], dict[str, Any]],
        get_config: Callable[[], Any],
        get_job: Callable[[str], dict[str, Any]],
    ) -> None:
        self.repository = repository
        self.submit_tail_discovery = submit_tail_discovery
        self.get_config = get_config
        self.get_job = get_job

    def save_schedule(self, body: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else body
        payload = self._clean_payload(payload)
        moment = ensure_utc(now or utc_now())
        normalized = self._validate_payload(payload, moment)
        interval_days = self._interval_days(body.get("interval_days", 1))
        run_time = normalize_tail_time(body.get("run_time") or "09:00")
        if not run_time:
            raise ValueError("定时查询时间必须为有效的 HH:MM。")
        schedule_id = str(body.get("schedule_id") or "").strip() or uuid4().hex[:12]
        if not SCHEDULE_ID_PATTERN.fullmatch(schedule_id):
            raise ValueError("定时计划 ID 无效。")
        existing = self.repository.fetch_tail_schedule(schedule_id)
        enabled = bool(body.get("enabled", existing["enabled"] if existing else True))
        if enabled:
            enabled_count = sum(1 for item in self.repository.fetch_tail_schedules(enabled_only=True)
                                if item["schedule_id"] != schedule_id)
            if enabled_count >= 20:
                raise ValueError("最多启用 20 个定时查询计划。")
        stored_payload = self._stored_payload(payload, normalized)
        schedule = {
            "schedule_id": schedule_id,
            "enabled": enabled,
            "payload": stored_payload,
            "interval_days": interval_days,
            "run_time": run_time,
            "next_run_at": next_run_at(moment, interval_days, run_time, self.timezone()),
            "last_run_at": existing.get("last_run_at") if existing else None,
            "last_job_id": existing.get("last_job_id") if existing else None,
            "last_error": None,
            "created_at": existing.get("created_at") if existing else None,
        }
        self.repository.upsert_tail_schedule(schedule)
        return self.public_schedule(self.repository.fetch_tail_schedule(schedule_id))

    def list_schedules(self) -> list[dict[str, Any]]:
        return [self.public_schedule(item) for item in self.repository.fetch_tail_schedules()]

    def set_enabled(self, schedule_id: str, enabled: bool) -> dict[str, Any]:
        schedule = self._require_schedule(schedule_id)
        if enabled:
            self._validate_payload(schedule["payload"], utc_now())
            enabled_count = sum(1 for item in self.repository.fetch_tail_schedules(enabled_only=True)
                                if item["schedule_id"] != schedule_id)
            if enabled_count >= 20:
                raise ValueError("最多启用 20 个定时查询计划。")
        if not self.repository.set_tail_schedule_enabled(schedule_id, enabled):
            raise KeyError(schedule_id)
        return self.public_schedule(self.repository.fetch_tail_schedule(schedule_id))

    def delete_schedule(self, schedule_id: str) -> bool:
        self._require_schedule(schedule_id)
        return self.repository.delete_tail_schedule(schedule_id)

    def process_due(self, *, now: datetime | None = None) -> dict[str, Any]:
        moment = ensure_utc(now or utc_now())
        processed: list[dict[str, Any]] = []
        for schedule in self.repository.fetch_tail_schedules(enabled_only=True):
            due = parse_utc(schedule["next_run_at"])
            if due > moment:
                continue
            payload = schedule["payload"]
            if self._departure_date_passed(payload, moment):
                self.repository.set_tail_schedule_enabled(
                    schedule["schedule_id"], False, last_error="出发日期已过，定时查询已停用。"
                )
                processed.append({"schedule_id": schedule["schedule_id"], "status": "disabled_expired"})
                continue
            if self._last_job_active(schedule):
                next_at = advance_next_run(due, schedule["interval_days"], moment, self.timezone())
                self.repository.record_tail_schedule_run(
                    schedule["schedule_id"],
                    next_run_at=next_at,
                    last_error="上一次定时任务尚未完成，本次已跳过。",
                )
                processed.append({"schedule_id": schedule["schedule_id"], "status": "skipped_active_job"})
                continue
            try:
                job = self.submit_tail_discovery(dict(payload))
            except (ValueError, TypeError) as exc:
                self.repository.set_tail_schedule_enabled(
                    schedule["schedule_id"], False, last_error=str(exc)
                )
                processed.append({"schedule_id": schedule["schedule_id"], "status": "disabled_invalid", "error": str(exc)})
                continue
            except RuntimeError as exc:
                message = str(exc)
                if "已有查询" in message:
                    processed.append({"schedule_id": schedule["schedule_id"], "status": "deferred_busy"})
                    continue
                self.repository.record_tail_schedule_run(
                    schedule["schedule_id"],
                    next_run_at=iso_utc(moment + timedelta(minutes=30)),
                    last_error=message,
                )
                processed.append({"schedule_id": schedule["schedule_id"], "status": "retry_later", "error": message})
                continue
            next_at = advance_next_run(due, schedule["interval_days"], moment, self.timezone())
            self.repository.record_tail_schedule_run(
                schedule["schedule_id"],
                next_run_at=next_at,
                last_run_at=iso_utc(moment),
                last_job_id=str(job.get("job_id") or ""),
                last_error=None,
            )
            processed.append({
                "schedule_id": schedule["schedule_id"], "status": "submitted",
                "job_id": job.get("job_id"), "next_run_at": next_at,
            })
        return {"processed": processed}

    def public_schedule(self, schedule: dict[str, Any] | None) -> dict[str, Any]:
        if not schedule:
            raise KeyError("schedule")
        payload = schedule["payload"]
        last_job = None
        if schedule.get("last_job_id"):
            try:
                last_job = self.get_job(schedule["last_job_id"])
            except Exception:
                last_job = None
        progress = (last_job or {}).get("progress") or {}
        result = (last_job or {}).get("result") or {}
        last_saved = progress.get("saved")
        if last_saved is None and isinstance(result, dict):
            last_saved = result.get("saved")
        return {
            "schedule_id": schedule["schedule_id"],
            "enabled": schedule["enabled"],
            "interval_days": schedule["interval_days"],
            "run_time": schedule["run_time"],
            "next_run_at": schedule["next_run_at"],
            "last_run_at": schedule.get("last_run_at"),
            "last_job_id": schedule.get("last_job_id"),
            "last_error": schedule.get("last_error"),
            "created_at": schedule.get("created_at"),
            "updated_at": schedule.get("updated_at"),
            "route": {
                "origin": payload.get("origin"),
                "transfer": payload.get("transfer"),
                "departure_date": payload.get("departure_date"),
                "provider": payload.get("provider"),
                "search_scope": payload.get("search_scope"),
                "candidates": len(payload.get("candidates") or []),
            },
            "last_job_status": (last_job or {}).get("status"),
            "last_job_saved": last_saved if last_job else None,
        }

    def timezone(self) -> ZoneInfo:
        return ZoneInfo(str(getattr(self.get_config(), "timezone", "Asia/Shanghai") or "Asia/Shanghai"))

    def _clean_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        cleaned = {
            str(key): value for key, value in payload.items()
            if not str(key).startswith("_") and str(key) not in INTERNAL_PAYLOAD_KEYS
        }
        cleaned["mode"] = "explore"
        cleaned["ignore_cache"] = False
        return cleaned

    def _validate_payload(self, payload: dict[str, Any], moment: datetime):
        provider = str(payload.get("provider") or "ctrip").strip().lower()
        if provider == "both":
            ctrip_payload = {
                **payload,
                "provider": "ctrip",
                "search_scope": "candidates",
                "max_provider_calls": 0,
            }
            feizhu_scope = str(payload.get("feizhu_search_scope") or "open")
            feizhu_payload = {**payload, "provider": "feizhu", "search_scope": feizhu_scope}
            if feizhu_scope == "open":
                feizhu_payload.update(candidates=[], max_candidates=0, target_results=0)
            elif feizhu_scope == "hybrid":
                feizhu_payload.update(max_candidates=0, target_results=0)
            ctrip_request = normalize_tail_discovery_request(ctrip_payload)
            feizhu_request = normalize_tail_discovery_request(feizhu_payload)
            for request in (ctrip_request, feizhu_request):
                self._validate_provider(request.provider)
                self._validate_departure(request.departure_date, moment)
            return ctrip_request
        request = normalize_tail_discovery_request(payload)
        self._validate_provider(request.provider)
        self._validate_departure(request.departure_date, moment)
        return request

    def _stored_payload(self, payload: dict[str, Any], request: Any) -> dict[str, Any]:
        stored = dict(payload)
        stored.update(asdict(request))
        for key in INTERNAL_PAYLOAD_KEYS:
            stored.pop(key, None)
        stored["mode"] = "explore"
        stored["ignore_cache"] = False
        if str(payload.get("provider") or "").lower() == "both":
            stored["provider"] = "both"
            stored["feizhu_search_scope"] = str(payload.get("feizhu_search_scope") or "open")
            stored["max_provider_calls"] = int(payload.get("max_provider_calls") or 0)
        if stored.get("search_scope") == "open":
            stored["candidates"] = []
        return stored

    def _validate_provider(self, provider: str) -> None:
        config = self.get_config()
        provider_config = getattr(config, "providers", {}).get(provider)
        if provider_config is None or not provider_config.enabled:
            raise ValueError("所选平台未启用。")

    def _validate_departure(self, departure_date: str, moment: datetime) -> None:
        if date.fromisoformat(departure_date) < moment.astimezone(self.timezone()).date():
            raise ValueError("出发日期已过，不能保存定时查询。")

    def _departure_date_passed(self, payload: dict[str, Any], moment: datetime) -> bool:
        try:
            return date.fromisoformat(str(payload.get("departure_date"))) < moment.astimezone(self.timezone()).date()
        except (TypeError, ValueError):
            return True

    def _last_job_active(self, schedule: dict[str, Any]) -> bool:
        job_id = schedule.get("last_job_id")
        if not job_id:
            return False
        try:
            job = self.get_job(job_id)
        except Exception:
            return False
        return (job or {}).get("status") in ACTIVE_JOB_STATUSES

    def _require_schedule(self, schedule_id: str) -> dict[str, Any]:
        if not SCHEDULE_ID_PATTERN.fullmatch(schedule_id):
            raise KeyError(schedule_id)
        schedule = self.repository.fetch_tail_schedule(schedule_id)
        if schedule is None:
            raise KeyError(schedule_id)
        return schedule

    @staticmethod
    def _interval_days(value: Any) -> int:
        try:
            interval = int(value)
        except (TypeError, ValueError):
            raise ValueError("定时查询频率必须为 1 到 30 天。")
        if not 1 <= interval <= 30:
            raise ValueError("定时查询频率必须为 1 到 30 天。")
        return interval


def next_run_at(after: datetime, interval_days: int, run_time: str, tz: ZoneInfo) -> str:
    local = ensure_utc(after).astimezone(tz)
    hour, minute = (int(part) for part in run_time.split(":"))
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return iso_utc(candidate)


def advance_next_run(due: datetime, interval_days: int, now: datetime, tz: ZoneInfo) -> str:
    candidate = ensure_utc(due).astimezone(tz)
    moment = ensure_utc(now).astimezone(tz)
    step = timedelta(days=interval_days)
    candidate += step
    while candidate <= moment:
        candidate += step
    return iso_utc(candidate)


def parse_utc(value: str | None) -> datetime:
    if not value:
        return utc_now()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0)


def iso_utc(value: datetime) -> str:
    return ensure_utc(value).isoformat().replace("+00:00", "Z")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
