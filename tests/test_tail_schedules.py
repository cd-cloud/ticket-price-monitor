from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from data_storage import PriceRepository
from tail_schedule_service import TailScheduleService, next_run_at, parse_utc
from web_ui import create_app
from test_web_ui_validation import write_config


TZ = "Asia/Shanghai"


def make_service(tmp_path, *, submit=None, jobs=None):
    repo = PriceRepository(Path(tmp_path) / "schedules.db")
    repo.initialize()
    jobs = jobs or {}
    config = SimpleNamespace(
        timezone=TZ,
        providers={
            "ctrip": SimpleNamespace(enabled=True),
            "feizhu": SimpleNamespace(enabled=True),
        },
    )
    calls = []

    def default_submit(payload):
        calls.append(payload)
        return {"job_id": f"job-{len(calls)}"}

    service = TailScheduleService(
        repository=repo,
        submit_tail_discovery=submit or default_submit,
        get_config=lambda: config,
        get_job=lambda job_id: jobs[job_id],
    )
    return service, repo, calls


def payload(**overrides):
    data = {
        "origin": "BJS",
        "transfer": "CTU",
        "departure_date": "2027-01-01",
        "provider": "ctrip",
        "cabin": "economy_plus",
        "passengers": 1,
        "candidates": ["CAN", "SHA"],
        "search_scope": "candidates",
        "filter_scope": "first_leg",
        "max_candidates": 6,
        "max_minutes": 10,
        "target_results": 0,
        "max_baseline_queries": 1,
        "max_provider_calls": 0,
        "max_followup_candidates": 0,
        "mode": "resume",
        "ignore_cache": True,
        "source_job_id": "old",
        "_checkpoint_job_id": "hidden",
    }
    data.update(overrides)
    return data


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def test_save_schedule_strips_internal_fields_and_computes_next_local_time(tmp_path):
    service, repo, _ = make_service(tmp_path)
    schedule = service.save_schedule(
        {"payload": payload(), "interval_days": 1, "run_time": "09:00"},
        now=at("2026-01-05T02:00:00"),  # 10:00 in Shanghai.
    )

    assert schedule["schedule_id"]
    assert schedule["next_run_at"] == "2026-01-06T01:00:00Z"
    assert schedule["route"] == {
        "origin": "BJS", "transfer": "CTU", "departure_date": "2027-01-01",
        "provider": "ctrip", "search_scope": "candidates", "candidates": 2,
    }
    stored = repo.fetch_tail_schedule(schedule["schedule_id"])["payload"]
    assert stored["mode"] == "explore"
    assert stored["ignore_cache"] is False
    assert "source_job_id" not in stored
    assert "_checkpoint_job_id" not in stored


def test_save_schedule_requires_boolean_enabled(tmp_path):
    service, _repo, _calls = make_service(tmp_path)
    with pytest.raises(ValueError, match="enabled must be a boolean"):
        service.save_schedule({
            "payload": payload(), "interval_days": 1, "run_time": "09:00", "enabled": "false",
        })


def test_save_dual_provider_schedule_keeps_feizhu_call_budget(tmp_path):
    service, repo, _ = make_service(tmp_path)
    dual = payload(
        provider="both",
        feizhu_search_scope="open",
        max_provider_calls=20,
        max_candidates=0,
        target_results=0,
    )

    schedule = service.save_schedule(
        {"payload": dual, "interval_days": 1, "run_time": "09:00"},
        now=at("2026-01-05T02:00:00"),
    )

    stored = repo.fetch_tail_schedule(schedule["schedule_id"])["payload"]
    assert stored["provider"] == "both"
    assert stored["feizhu_search_scope"] == "open"
    assert stored["max_provider_calls"] == 20


def test_next_run_uses_same_day_when_time_is_still_ahead():
    assert next_run_at(at("2026-01-05T00:30:00"), 1, "09:00", __import__("zoneinfo").ZoneInfo(TZ)) == "2026-01-05T01:00:00Z"


def test_process_due_submits_once_and_advances_next_run(tmp_path):
    service, repo, calls = make_service(tmp_path)
    schedule = service.save_schedule(
        {"payload": payload(), "interval_days": 2, "run_time": "09:00"},
        now=at("2026-01-05T02:00:00"),
    )
    due = parse_utc(schedule["next_run_at"])

    result = service.process_due(now=due)

    assert result["processed"][0]["status"] == "submitted"
    assert len(calls) == 1
    assert calls[0]["origin"] == "BJS"
    updated = repo.fetch_tail_schedule(schedule["schedule_id"])
    assert updated["last_job_id"] == "job-1"
    assert updated["last_run_at"] == "2026-01-06T01:00:00Z"
    assert updated["next_run_at"] == "2026-01-08T01:00:00Z"


def test_missed_multiple_cycles_only_catches_up_once(tmp_path):
    service, repo, _ = make_service(tmp_path)
    schedule = service.save_schedule(
        {"payload": payload(), "interval_days": 1, "run_time": "09:00"},
        now=at("2026-01-01T02:00:00"),
    )

    service.process_due(now=at("2026-01-05T05:00:00"))

    updated = repo.fetch_tail_schedule(schedule["schedule_id"])
    assert updated["last_run_at"] == "2026-01-05T05:00:00Z"
    assert updated["next_run_at"] == "2026-01-06T01:00:00Z"


def test_active_previous_job_skips_occurrence_without_second_submit(tmp_path):
    jobs = {"job-1": {"job_id": "job-1", "status": "running", "progress": {}}}
    service, repo, calls = make_service(tmp_path, jobs=jobs)
    schedule = service.save_schedule(
        {"payload": payload(), "interval_days": 1, "run_time": "09:00"},
        now=at("2026-01-05T02:00:00"),
    )
    first_due = parse_utc(schedule["next_run_at"])
    service.process_due(now=first_due)
    second_due = parse_utc(repo.fetch_tail_schedule(schedule["schedule_id"])["next_run_at"])

    result = service.process_due(now=second_due)

    assert result["processed"][0]["status"] == "skipped_active_job"
    assert len(calls) == 1
    updated = repo.fetch_tail_schedule(schedule["schedule_id"])
    assert updated["next_run_at"] == "2026-01-08T01:00:00Z"
    assert "尚未完成" in updated["last_error"]


def test_busy_run_lock_defers_without_losing_due_time(tmp_path):
    def busy(payload):
        raise RuntimeError("已有查询正在运行，请稍后再试。")

    service, repo, _ = make_service(tmp_path, submit=busy)
    schedule = service.save_schedule(
        {"payload": payload(), "interval_days": 1, "run_time": "09:00"},
        now=at("2026-01-05T02:00:00"),
    )

    result = service.process_due(now=parse_utc(schedule["next_run_at"]))

    assert result["processed"][0]["status"] == "deferred_busy"
    assert repo.fetch_tail_schedule(schedule["schedule_id"])["next_run_at"] == schedule["next_run_at"]


def test_temporary_runtime_error_backs_off_thirty_minutes(tmp_path):
    def cooldown(payload):
        raise RuntimeError("平台限流冷却中")

    service, repo, _ = make_service(tmp_path, submit=cooldown)
    schedule = service.save_schedule(
        {"payload": payload(), "interval_days": 1, "run_time": "09:00"},
        now=at("2026-01-05T02:00:00"),
    )
    due = parse_utc(schedule["next_run_at"])

    result = service.process_due(now=due)

    assert result["processed"][0]["status"] == "retry_later"
    updated = repo.fetch_tail_schedule(schedule["schedule_id"])
    assert updated["next_run_at"] == "2026-01-06T01:30:00Z"
    assert "冷却" in updated["last_error"]


def test_expired_departure_disables_schedule(tmp_path):
    service, repo, _ = make_service(tmp_path)
    schedule = service.save_schedule(
        {"payload": payload(departure_date="2026-01-02"), "interval_days": 1, "run_time": "09:00"},
        now=at("2026-01-01T02:00:00"),
    )

    result = service.process_due(now=at("2026-01-03T02:00:00"))

    assert result["processed"][0]["status"] == "disabled_expired"
    updated = repo.fetch_tail_schedule(schedule["schedule_id"])
    assert updated["enabled"] is False
    assert "出发日期已过" in updated["last_error"]


def test_schedule_crud_and_list_enrich_last_job(tmp_path):
    jobs = {"job-1": {"job_id": "job-1", "status": "finished", "progress": {"saved": 4}}}
    service, repo, _ = make_service(tmp_path, jobs=jobs)
    schedule = service.save_schedule({"payload": payload(), "interval_days": 7, "run_time": "10:30"})
    repo.record_tail_schedule_run(schedule["schedule_id"], last_job_id="job-1", last_run_at="2026-01-05T02:00:00Z")

    listed = service.list_schedules()[0]
    assert listed["last_job_status"] == "finished"
    assert listed["last_job_saved"] == 4

    disabled = service.set_enabled(schedule["schedule_id"], False)
    assert disabled["enabled"] is False
    assert service.delete_schedule(schedule["schedule_id"]) is True
    assert repo.fetch_tail_schedule(schedule["schedule_id"]) is None


def test_schedule_rejects_invalid_payload_and_past_departure(tmp_path):
    service, _, _ = make_service(tmp_path)
    with pytest.raises(ValueError, match="出发日期已过"):
        service.save_schedule(
            {"payload": payload(departure_date="2026-01-01")},
            now=at("2026-01-02T02:00:00"),
        )
    with pytest.raises(ValueError, match="1 到 30"):
        service.save_schedule({"payload": payload(), "interval_days": 0})


def test_schedule_api_crud(tmp_path):
    config = tmp_path / "config.json"
    write_config(config)
    departure = (date.today() + timedelta(days=30)).isoformat()
    with TestClient(create_app(str(config))) as client:
        created = client.post(
            "/api/tail-discovery/schedules",
            json={"payload": payload(departure_date=departure), "interval_days": 2, "run_time": "09:30"},
        )
        assert created.status_code == 200
        schedule = created.json()
        assert schedule["interval_days"] == 2
        assert schedule["run_time"] == "09:30"

        listed = client.get("/api/tail-discovery/schedules").json()["schedules"]
        assert [item["schedule_id"] for item in listed] == [schedule["schedule_id"]]

        disabled = client.post(
            f"/api/tail-discovery/schedules/{schedule['schedule_id']}/enabled",
            json={"enabled": False},
        )
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False

        deleted = client.delete(f"/api/tail-discovery/schedules/{schedule['schedule_id']}")
        assert deleted.status_code == 200
        assert client.get("/api/tail-discovery/schedules").json()["schedules"] == []
