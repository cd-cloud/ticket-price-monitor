"""Offline end-to-end experimental admission, lifecycle, browser and storage."""
import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from config_manager import ConfigManager
from data_storage import PriceRepository
from experimental_query_service import ACTIVE, ExperimentalQueryService
from query_service import QueryService
from test_airchina_adapter import snapshot, TARGET
from test_web_ui_validation import write_config
from web_ui import create_app


REQUEST = {"origin": "PEK", "destination": "SHA", "departure_date": "2099-01-01"}


def wait(service, job_id, predicate=None):
    end = time.monotonic() + 5
    while time.monotonic() < end:
        job = service.get(job_id)
        if predicate(job) if predicate else job["status"] not in ACTIVE:
            return job
        time.sleep(.01)
    raise AssertionError(f"did not settle: {job}")


def config_file(tmp_path):
    path = tmp_path / "config.json"
    write_config(path)
    data = json.loads(path.read_text())
    data["providers"]["airchina"] = {
        "enabled": False, "login_url": "https://www.airchina.com.cn/zh-CN",
        "search_url_template": "https://www.airchina.com.cn/flight/oneway/{origin}-{destination}/{departure_date}",
        "storage_state_file": "./runtime/airchina_state.json",
    }
    path.write_text(json.dumps(data))
    return path


@pytest.fixture
def env(tmp_path):
    manager = ConfigManager(config_file(tmp_path))
    manager.load_credentials = Mock(side_effect=AssertionError("must not read keys"))
    config = manager.load()
    repo = PriceRepository(config.database_path)
    repo.initialize()
    lock = threading.Lock()
    control = SimpleNamespace(challenge=False, error=False, close=False, entered=False, hang=False, config=None)
    page = SimpleNamespace(url=TARGET, evaluate=AsyncMock(return_value=snapshot()),
                           wait_for_timeout=AsyncMock())

    async def navigate(*args, **kwargs):
        control.entered = True
        if control.error:
            raise RuntimeError("secret-key-should-never-be-written")
        while control.hang:
            await asyncio.sleep(.01)
        return SimpleNamespace(status=200)

    page.goto = AsyncMock(side_effect=navigate)
    page.locator = Mock(return_value=SimpleNamespace(inner_text=AsyncMock(
        side_effect=lambda **kw: "安全验证" if control.challenge else "选择航班",
    )))
    automation = SimpleNamespace(_new_page=AsyncMock(return_value=(page, "context")),
                                 _throttle=AsyncMock(), _close_context_quietly=AsyncMock())

    @asynccontextmanager
    async def factory(cfg, credentials):
        assert credentials == {}
        control.config = cfg
        try:
            yield automation
        finally:
            control.close = True

    query = QueryService(config_manager=manager, repository=repo, run_lock=lock,
                         get_config=lambda: config, refresh_config=lambda: None, automation_factory=factory)
    service = ExperimentalQueryService(query_service=query, repository=repo, run_lock=lock, timeout_seconds=5)
    yield service, repo, control, page, automation, query
    control.hang = False
    service.close()


def test_success_shared_runtime_and_separate_storage(env):
    service, repo, control, page, automation, query = env
    job = service.submit(REQUEST)
    final = wait(service, job["job_id"])
    assert final["status"] == "completed_reference"
    assert final["result"]["reference_preview"]["offers"][0]["reference_fare"] == 600
    assert not final["quote_saved"]
    assert control.config.headless is False
    assert control.config.providers["airchina"].enabled is False
    assert repo.count_snapshots() == 0
    assert repo.fetch_tail_discovery_jobs() == []
    assert control.close
    automation._close_context_quietly.assert_awaited_once_with("context")
    page.goto.assert_awaited_once()
    assert "secret" not in json.dumps(service.list())


def test_manual_resume_keeps_page_and_lock(env):
    service, repo, control, page, automation, query = env
    control.challenge = True
    job = service.submit(REQUEST)
    wait(service, job["job_id"], lambda j: j["status"] == "waiting_user")
    assert query.run_lock.locked()
    with pytest.raises(RuntimeError):
        service.submit(REQUEST)
    assert asyncio.run(query.run_once())["running"]
    page.evaluate.assert_not_awaited()
    control.challenge = False
    service.control(job["job_id"], "continue")
    assert wait(service, job["job_id"])["status"] == "completed_reference"
    page.goto.assert_awaited_once()


def test_cancel_waiting_user_closes_context(env):
    service, repo, control, page, automation, query = env
    control.challenge = True
    job = service.submit(REQUEST)
    wait(service, job["job_id"], lambda j: j["status"] == "waiting_user")
    service.control(job["job_id"], "cancel")
    assert wait(service, job["job_id"])["status"] == "cancelled"
    assert control.close
    assert repo.count_snapshots() == 0
    automation._close_context_quietly.assert_awaited_once()


def test_total_timeout_includes_manual_wait(env):
    service, repo, control, page, automation, query = env
    service.timeout_seconds = .2
    control.challenge = True
    job = service.submit(REQUEST)
    assert wait(service, job["job_id"])["status"] == "timed_out"
    assert control.close
    assert repo.count_snapshots() == 0


def test_timeout_during_navigation_closes_context(env):
    service, repo, control, page, automation, query = env
    service.timeout_seconds = .15
    control.hang = True
    job = service.submit(REQUEST)
    assert wait(service, job["job_id"])["status"] == "timed_out"
    assert control.close
    page.goto.assert_awaited_once()


def test_error_redacted_and_lock_released(env):
    service, repo, control, page, automation, query = env
    control.error = True
    job = service.submit(REQUEST)
    final = wait(service, job["job_id"])
    service._thread.join(1)
    assert final["status"] == "failed" and final["error_type"] == "RuntimeError"
    assert "secret" not in json.dumps(service.list())
    assert not query.run_lock.locked()


def test_wrong_task_controls_and_terminal_resume_rejected(env):
    service, *_ = env
    with pytest.raises(KeyError):
        service.control("other", "continue")
    job = service.submit(REQUEST)
    wait(service, job["job_id"])
    with pytest.raises(RuntimeError):
        service.control(job["job_id"], "continue")


@pytest.mark.parametrize("changes", [
    {"return_date": "2099-01-03"}, {"passengers": 2}, {"api_key": "unused"},
    {"origin": "BJS"}, {"origin": "PEK?secret=x"}, {"destination": "PEK"},
    {"cabin": "first"}, {"flight_no": 123}, {"flight_no": "book"},
    {"departure_date": "2000-01-01"}, {"departure_date": None},
])
def test_invalid_input_never_opens_browser(env, changes):
    service, repo, control, page, *_ = env
    with pytest.raises(ValueError):
        service.submit({**REQUEST, **changes})
    page.goto.assert_not_awaited()
    assert not service.list()


def test_shutdown_cancels_worker(env):
    service, repo, control, page, *_ = env
    control.challenge = True
    job = service.submit(REQUEST)
    wait(service, job["job_id"], lambda j: j["status"] == "waiting_user")
    service.close()
    assert service.get(job["job_id"])["status"] == "cancelled"
    assert not service._thread.is_alive()
    with pytest.raises(RuntimeError):
        service.submit(REQUEST)


def test_start_failure_does_not_leave_queued_task_or_hold_lock(env, monkeypatch):
    service, repo, control, page, automation, query = env
    monkeypatch.setattr(threading.Thread, "start", Mock(side_effect=RuntimeError("failure")))
    with pytest.raises(RuntimeError):
        service.submit(REQUEST)
    assert service.list()[0]["status"] == "failed"
    assert not query.run_lock.locked()
    service._thread = None


def test_recovery_never_restarts_or_replays_query(env):
    service, repo, control, page, automation, query = env
    repo.save_experimental_query({
        "job_id": "orphan", "status": "waiting_user", "updated_at": repo.now_iso(),
    })
    recovered = ExperimentalQueryService(query_service=query, repository=repo, run_lock=query.run_lock)
    assert recovered.get("orphan")["status"] == "interrupted"
    page.goto.assert_not_awaited()


def test_header_and_session_configuration_matches_common_ctrip_mechanism():
    example = json.loads((Path(__file__).parents[1] / "config.example.json").read_text())
    ctrip, ca = [example["providers"][p] for p in ("ctrip", "airchina")]
    for key in ("extra_headers", "locale", "timezone", "min_delay_seconds", "max_delay_seconds", "timeout_ms"):
        assert ctrip[key] == ca[key]
    assert ca["extra_headers"] == {"Accept-Language": "zh-CN,zh;q=0.9"}
    assert ctrip["storage_state_file"] != ca["storage_state_file"]
    assert not ca["enabled"]


def test_api_and_web_entry_with_no_real_browser(tmp_path, monkeypatch):
    async def fake(self, route, **kwargs):
        kwargs["progress"]("reading_results")
        return {"state": "query_page_unverified", "message": "不确定，不是无票", "quote_saved": False}
    monkeypatch.setattr(QueryService, "inspect_airchina_experiment", fake)
    with TestClient(create_app(str(config_file(tmp_path)))) as client:
        html = client.get("/").text
        assert "airchinaExperimentForm" in html and "/static/airchina_experiment.js" in html
        assert client.post("/api/experiments/airchina", json={**REQUEST, "return_date": "2099-01-03"}).status_code == 400
        response = client.post("/api/experiments/airchina", json=REQUEST)
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        for _ in range(100):
            job = client.get(f"/api/experiments/airchina/{job_id}").json()
            if job["status"] not in ACTIVE:
                break
            time.sleep(.01)
        assert job["status"] == "inconclusive"
        assert len(client.get("/api/experiments/airchina").json()["jobs"]) == 1
        assert client.post(f"/api/experiments/airchina/{job_id}/continue").status_code == 409
        assert client.get("/api/experiments/airchina/unknown").status_code == 404
        assert client.post("/api/experiments/airchina", json=REQUEST, headers={"Origin": "https://evil.test"}).status_code == 403
