"""Offline end-to-end experimental admission, lifecycle, browser and storage."""
import asyncio
import json
import threading
import time
from datetime import date, timedelta
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
                           wait_for_timeout=AsyncMock(), is_closed=Mock(return_value=False))
    from test_airchina_network_diagnostics import Page as EventPage, Request, Response
    events = EventPage()
    page.on, page.remove_listener = events.on, events.remove_listener
    page.events = events

    async def navigate(*args, **kwargs):
        control.entered = True
        request = Request()
        events.emit("request", request)
        events.emit("response", Response(request, 200))
        events.emit("requestfinished", request)
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
    # These are browser lifecycle tests: execute every requested sample without
    # wall-clock throttling. Dedicated policy tests exercise real TTL/spacing.
    from airchina_query_gate import AirChinaQueryGate
    gate = AirChinaQueryGate(config.runtime_dir, min_interval=0, ttl=0)
    query.airchina_query_gate = lambda: gate
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
    assert final["diagnostics"]["last_observation"]["accepted_offers"] == 16
    assert final["diagnostics"] == final["result"]["wait_evidence"]


def test_policy_cache_returns_original_observation_without_browser(env):
    service, repo, control, page, automation, query = env
    gate = query.airchina_query_gate()
    gate.ttl = 120
    request = {**REQUEST, "session_mode": "reuse", "interaction_mode": "stop"}
    first = wait(service, service.submit(request)["job_id"])
    service._thread.join(1)
    second = wait(service, service.submit(request)["job_id"])
    assert second["status"] == "completed_reference"
    assert second["result"]["query_policy"]["source"] == "cache"
    assert second["result"]["query_policy"]["observed_at"] == first["result"]["query_policy"]["observed_at"]
    assert second["network_diagnostics"] is None
    assert second["session_evidence"] is None
    assert page.goto.await_count == 1
    assert not repo.count_snapshots()


def test_blocked_provider_applies_to_next_route_without_opening_browser(env):
    service, repo, control, page, automation, query = env
    control.challenge = True
    first = wait(service, service.submit({**REQUEST, "interaction_mode": "stop"})["job_id"])
    assert first["stage"] == "needs_verification"
    service._thread.join(1)
    second = wait(service, service.submit({**REQUEST, "destination": "PVG"})["job_id"])
    assert second["stage"] == "provider_paused"
    assert second["result"]["query_policy"]["source"] == "paused"
    assert page.goto.await_count == 1


def test_http_429_stops_current_wait_and_closes_browser(env):
    from test_airchina_network_diagnostics import Request, Response
    service, repo, control, page, automation, query = env
    async def rate_limit(*args, **kwargs):
        control.entered = True
        req = Request()
        response = Response(req, 429)
        response.header_value = AsyncMock(return_value="60")
        page.events.emit("request", req)
        page.events.emit("response", response)
        await asyncio.sleep(10)
    page.goto.side_effect = rate_limit
    final = wait(service, service.submit(REQUEST)["job_id"])
    assert final["stage"] == "provider_paused"
    assert query.airchina_query_gate().status()["retry_after_seconds"] > 0
    automation._close_context_quietly.assert_awaited_once()
    assert control.close


def test_policy_resume_api_requires_confirmation_and_respects_wait(tmp_path):
    application = create_app(str(config_file(tmp_path)))
    with TestClient(application) as client:
        from airchina_query_gate import AirChinaQueryGate
        cfg = ConfigManager(config_file(tmp_path)).load()
        gate = AirChinaQueryGate(cfg.runtime_dir)
        gate.pause("needs_verification")
        assert client.get("/api/experiments/airchina").json()["query_policy"]["paused"]
        url = "/api/experiments/airchina/policy/resume"
        assert client.post(url, json={"confirmed": False}).status_code == 400
        assert client.post(url, json={"confirmed": True}).json()["paused"] is False
        gate.pause("rate_limited", retry_after=120)
        assert client.post(url, json={"confirmed": True}).status_code == 409
        assert gate.status()["paused"]


def test_profile_unavailable_is_actionable_without_query_or_retry(env):
    from browser_session_manager import AirChinaSessionUnavailable
    service, repo, control, page, automation, query = env
    automation._new_page.side_effect = AirChinaSessionUnavailable()
    final = wait(service, service.submit(REQUEST)["job_id"])
    assert final["status"] == "failed"
    assert final["error_type"] == "AirChinaSessionUnavailable"
    assert "不会自动切换" in final["message"]
    assert final["network_diagnostics"]["counts"] == {}
    assert final["network_diagnostics"]["finished"]
    page.goto.assert_not_awaited()
    automation._new_page.assert_awaited_once()
    assert repo.count_snapshots() == 0


@pytest.mark.parametrize("ending", ["timeout", "cancel", "error"])
def test_last_diagnostic_survives_total_timeout_cancel_and_failure(env, ending):
    service, repo, control, page, automation, query = env
    # First real parser sample fails; the next I/O hangs or raises. This exercises
    # the actual callback chain, rather than returning a fabricated final report.
    data = {**snapshot(), "date_text": None}
    entered = threading.Event()
    async def evaluate(_):
        if not entered.is_set():
            entered.set()
            return data
        if ending == "error":
            raise RuntimeError("secret-page-material")
        await asyncio.sleep(10)
    page.evaluate.side_effect = evaluate
    service.timeout_seconds = .3 if ending == "timeout" else 5
    job = service.submit(REQUEST)
    if ending == "cancel":
        wait(service, job["job_id"], lambda j: j.get("diagnostics") is not None)
        service.control(job["job_id"], "cancel")
    final = wait(service, job["job_id"])
    assert final["status"] == {"timeout": "timed_out", "cancel": "cancelled", "error": "failed"}[ending]
    assert final["result"] is None
    assert final["diagnostics"]["last_observation"]["parser_reason"] == "visible_date_mismatch"
    audit = final["network_diagnostics"]
    assert audit["attached"] and audit["finished"]
    assert audit["counts"]["responses"] == 1
    assert audit["outcome"] == ("error" if ending == "error" else "cancelled")
    assert all(not handlers for handlers in page.events.handlers.values())
    assert "secret-page-material" not in json.dumps(final)
    assert repo.count_snapshots() == 0


def test_manual_resume_keeps_page_and_lock(env):
    service, repo, control, page, automation, query = env
    control.challenge = True
    job = service.submit(REQUEST)
    wait(service, job["job_id"], lambda j: j["status"] == "waiting_user")
    assert query.run_lock.locked()
    assert service.submit(REQUEST)["job_id"] == job["job_id"]
    assert asyncio.run(query.run_once())["running"]
    page.evaluate.assert_not_awaited()
    control.challenge = False
    service.control(job["job_id"], "continue")
    assert wait(service, job["job_id"])["status"] == "completed_reference"
    page.goto.assert_awaited_once()


@pytest.mark.parametrize("ending", ["continue", "cancel", "timeout", "stop"])
def test_failed_form_manual_inspection_holds_then_disposes_without_retry(env, monkeypatch, ending):
    from providers.airchina_form import FormEntry
    service, repo, control, page, automation, query = env
    calls = []
    async def failed(self, _timeout):
        calls.append(1)
        self.publish("stopped", "form_wait_timeout", failed_stage="selecting_origin")
        return {"state": "form_entry_unverified", "entry_evidence": self.evidence}
    monkeypatch.setattr(FormEntry, "run", failed)
    if ending == "timeout":
        service.timeout_seconds = .4
    job = service.submit({**REQUEST, "entry_mode": "form", "session_mode": "reuse",
                          "interaction_mode": "stop" if ending == "stop" else "wait",
                          "departure_date": (date.today() + timedelta(days=10)).isoformat()})
    if ending != "stop":
        waiting = wait(service, job["job_id"], lambda j: j["status"] == "waiting_user")
        assert waiting["stage"] == "form_inspection"
        assert waiting["entry_evidence"]["reason"] == "form_wait_timeout"
        assert query.run_lock.locked() and not control.close
        automation._close_context_quietly.assert_not_awaited()
        if ending in {"continue", "cancel"}:
            service.control(job["job_id"], ending)
    final = wait(service, job["job_id"])
    assert final["status"] == {"continue":"inconclusive", "cancel":"cancelled",
                               "timeout":"timed_out", "stop":"inconclusive"}[ending]
    assert final["session_evidence"]["state"] == "closed"
    assert control.close and not query.run_lock.locked()
    assert len(calls) == 1 and repo.count_snapshots() == 0
    page.goto.assert_not_awaited()
    page.evaluate.assert_not_awaited()


@pytest.mark.parametrize("manual", [False, True])
def test_form_entry_is_forwarded_once_and_manual_continue_never_resubmits(env, monkeypatch, manual):
    from providers.airchina_form import FormEntry, validate_entry
    service, repo, control, page, automation, query = env
    departure = date.today() + timedelta(days=10)
    request = {**REQUEST, "departure_date": departure.isoformat(), "entry_mode": "form"}
    calls = []
    async def entered(self, _timeout):
        calls.append(self.route)
        page.url = validate_entry(self.route, "form")
        data = snapshot()
        data.update(url=page.url, date_text=f"{departure.year}年{departure.month}月{departure.day}日")
        page.evaluate.return_value = data
        self.publish("result_page", "result_page_observed", submit_attempted=True, result_page_observed=True)
        return {"state": "query_submitted", "entry_evidence": self.evidence}
    monkeypatch.setattr(FormEntry, "run", entered)
    control.challenge = manual
    job = service.submit(request)
    if manual:
        wait(service, job["job_id"], lambda j: j["status"] == "waiting_user")
        control.challenge = False
        service.control(job["job_id"], "continue")
    final = wait(service, job["job_id"])
    assert final["status"] == "completed_reference"
    assert final["request"]["entry_mode"] == "form"
    assert final["entry_evidence"]["result_page_observed"]
    assert len(calls) == 1
    page.goto.assert_not_awaited()  # No second goto from the adapter.
    assert repo.count_snapshots() == 0


@pytest.mark.parametrize("ending", ["error", "timeout", "cancel"])
def test_form_entry_audit_survives_failure_and_no_result(env, monkeypatch, ending):
    from providers.airchina_form import FormEntry
    service, repo, control, page, automation, query = env
    async def entered(self, _timeout):
        self.publish("submitting_form", "submit_attempted", submit_attempted=True)
        if ending == "error":
            raise RuntimeError("private-form-token")
        await asyncio.sleep(10)
    monkeypatch.setattr(FormEntry, "run", entered)
    service.timeout_seconds = .2 if ending == "timeout" else 5
    job = service.submit({**REQUEST, "departure_date": (date.today() + timedelta(days=10)).isoformat(),
                          "entry_mode": "form"})
    if ending == "cancel":
        wait(service, job["job_id"], lambda j: j.get("entry_evidence") is not None)
        service.control(job["job_id"], "cancel")
    final = wait(service, job["job_id"])
    assert final["status"] == {"error": "failed", "timeout": "timed_out", "cancel": "cancelled"}[ending]
    assert final["entry_evidence"]["submit_attempted"]
    assert not final["entry_evidence"]["result_page_observed"]
    assert "private-form-token" not in json.dumps(final)
    assert final["result"] is None
    assert repo.count_snapshots() == 0


def test_cancel_waiting_user_closes_context(env):
    service, repo, control, page, automation, query = env
    control.challenge = True
    job = service.submit(REQUEST)
    wait(service, job["job_id"], lambda j: j["status"] == "waiting_user")
    service.control(job["job_id"], "cancel")
    final = wait(service, job["job_id"])
    assert final["status"] == "cancelled"
    assert final["access_events"][0]["state"] == "needs_verification"
    assert final["access_events"][0]["action"] == "wait"
    assert control.close
    assert repo.count_snapshots() == 0
    automation._close_context_quietly.assert_awaited_once()


def test_total_timeout_includes_manual_wait(env):
    service, repo, control, page, automation, query = env
    service.timeout_seconds = .2
    control.challenge = True
    job = service.submit(REQUEST)
    final = wait(service, job["job_id"])
    assert final["status"] == "timed_out"
    assert final["access_events"][0]["state"] == "needs_verification"
    assert control.close
    assert repo.count_snapshots() == 0


@pytest.mark.parametrize(("text", "state"), [
    ("安全验证", "needs_verification"),
    ("我知道了", "needs_notice_confirmation"),
    ("访问过于频繁", "blocked"),
    ("停留时间过长，请重新查询价格", "stale_results"),
])
def test_unattended_access_stop_releases_lock_and_preserves_reason(env, text, state):
    service, repo, control, page, automation, query = env
    page.locator.return_value.inner_text.side_effect = None
    page.locator.return_value.inner_text.return_value = text
    job = service.submit({**REQUEST, "interaction_mode": "stop"})
    final = wait(service, job["job_id"])
    service._thread.join(1)
    assert final["status"] == "inconclusive"
    assert final["stage"] == state
    assert "无人值守模式已停止" in final["message"]
    assert final["access_events"] == [{
        "state": state, "phase": "result_page", "action": "stop",
        "observed_at": final["access_events"][0]["observed_at"],
    }]
    assert not query.run_lock.locked()
    automation._new_page.assert_awaited_once()
    automation._close_context_quietly.assert_awaited_once()
    page.goto.assert_awaited_once()
    page.evaluate.assert_not_awaited()
    assert repo.count_snapshots() == 0
    with pytest.raises(RuntimeError):
        service.control(job["job_id"], "continue")


def test_unattended_does_not_block_valid_reference_reading(env):
    service, repo, *_ = env
    job = service.submit({**REQUEST, "interaction_mode": "stop"})
    final = wait(service, job["job_id"])
    assert final["status"] == "completed_reference"
    assert final["access_events"] == []
    assert repo.count_snapshots() == 0


def test_two_explicit_tasks_reuse_session_but_query_and_validate_again(env):
    service, repo, control, page, automation, query = env
    request = {**REQUEST, "session_mode": "reuse", "interaction_mode": "stop"}
    first = wait(service, service.submit(request)["job_id"])
    service._thread.join(1)
    assert not control.close
    assert not query.run_lock.locked()  # idle is not a running query
    second = wait(service, service.submit(request)["job_id"])
    service._thread.join(1)
    assert first["status"] == second["status"] == "completed_reference"
    assert not first["session_evidence"]["reused"] and second["session_evidence"]["reused"]
    assert first["session_evidence"]["generation"] == second["session_evidence"]["generation"]
    assert second["session_evidence"]["use_number"] == 2
    for job in (first, second):
        audit = job["network_diagnostics"]
        assert audit["attached"] and audit["finished"]
        assert audit["counts"]["requests"] == audit["counts"]["responses"] == 1
        assert audit["outcome"] == "reference_fares_observed"
    assert all(not handlers for handlers in page.events.handlers.values())
    automation._new_page.assert_awaited_once()
    automation._close_context_quietly.assert_not_awaited()
    assert page.goto.await_count == 2  # no cached-price shortcut
    assert page.evaluate.await_count >= 4  # both tasks independently revalidate
    assert repo.count_snapshots() == 0
    def maintenance():
        assert control.close and query.run_lock.locked()
        return "maintenance"
    assert service.without_browser_session(maintenance) == "maintenance"
    assert control.close
    automation._close_context_quietly.assert_awaited_once()


@pytest.mark.parametrize("ending", ["challenge", "cancel", "timeout", "error"])
def test_reuse_is_disposed_after_failure(env, ending):
    service, repo, control, page, automation, query = env
    request = {**REQUEST, "session_mode": "reuse"}
    wait(service, service.submit(request)["job_id"])
    service._thread.join(1)
    control.challenge = True
    if ending == "error":
        control.error = True
    if ending == "timeout":
        service.timeout_seconds = .2
    if ending == "challenge":
        request["interaction_mode"] = "stop"
    job = service.submit(request)
    if ending == "cancel":
        wait(service, job["job_id"], lambda j: j["status"] == "waiting_user")
        with pytest.raises(RuntimeError):
            service.without_browser_session()
        service.control(job["job_id"], "cancel")
    final = wait(service, job["job_id"])
    service._thread.join(1)
    assert final["status"] == {"challenge":"inconclusive", "cancel":"cancelled",
                               "timeout":"timed_out", "error":"failed"}[ending]
    assert final["session_evidence"]["state"] == "closed"
    assert control.close and not query.run_lock.locked()
    automation._new_page.assert_awaited_once()
    assert page.goto.await_count == 2  # no automatic third query


def test_application_shutdown_closes_retained_browser(env):
    service, repo, control, page, automation, query = env
    wait(service, service.submit({**REQUEST, "session_mode": "reuse"})["job_id"])
    service._thread.join(1)
    assert not control.close
    service.close()
    assert control.close
    assert not service._session_runner._thread.is_alive()


def test_results_entry_without_retained_page_fails_before_opening_browser(env):
    service, repo, control, page, automation, query = env
    job = service.submit({**REQUEST, "entry_mode": "results", "session_mode": "reuse",
                          "interaction_mode": "stop",
                          "departure_date": (date.today() + timedelta(days=10)).isoformat()})
    final = wait(service, job["job_id"])
    assert final["error_type"] == "RetainedSessionUnavailable"
    assert "未新开浏览器" in final["message"]
    automation._new_page.assert_not_awaited()
    page.goto.assert_not_awaited()
    assert not query.run_lock.locked() and repo.count_snapshots() == 0


def test_results_entry_uses_retained_page_and_never_falls_back(env, monkeypatch):
    from providers.airchina_requery import ResultsRequery
    service, repo, control, page, automation, query = env
    first = wait(service, service.submit({**REQUEST, "session_mode": "reuse"})["job_id"])
    async def no_refresh(self, _timeout):
        assert self.page is page
        self.publish("stopped", "refresh_not_observed", submit_attempted=True, baseline_validated=True)
        return {"state": "form_entry_unverified", "entry_evidence": self.evidence}
    monkeypatch.setattr(ResultsRequery, "run", no_refresh)
    job = service.submit({**REQUEST, "entry_mode": "results", "session_mode": "reuse",
                          "interaction_mode": "stop",
                          "departure_date": (date.today() + timedelta(days=10)).isoformat()})
    final = wait(service, job["job_id"])
    assert final["status"] == "inconclusive"
    assert final["session_evidence"]["generation"] == first["session_evidence"]["generation"]
    assert final["session_evidence"]["reused"] and final["session_evidence"]["state"] == "closed"
    assert final["entry_evidence"]["reason"] == "refresh_not_observed"
    automation._new_page.assert_awaited_once()
    page.goto.assert_awaited_once()  # first task only
    assert not query.run_lock.locked() and repo.count_snapshots() == 0


def test_verification_appearing_during_list_reading_stops_without_retry(env):
    service, repo, control, page, automation, query = env
    page.evaluate.return_value = {**snapshot(), "verification_visible": True}
    job = service.submit({**REQUEST, "interaction_mode": "stop"})
    final = wait(service, job["job_id"])
    service._thread.join(1)
    assert final["status"] == "inconclusive"
    assert final["access_events"][0]["state"] == "needs_verification"
    assert final["access_events"][0]["phase"] == "reading_results"
    page.goto.assert_awaited_once()
    assert not query.run_lock.locked()
    assert repo.count_snapshots() == 0


def test_home_verification_preserves_pre_submit_stop(env, monkeypatch):
    from providers.airchina_form import FormEntry
    service, repo, control, page, automation, query = env
    async def stopped(self, _timeout):
        self.publish("stopped", "verification", failed_stage="opening_home")
        return {"state": "needs_verification", "entry_evidence": self.evidence}
    monkeypatch.setattr(FormEntry, "run", stopped)
    job = service.submit({**REQUEST, "interaction_mode": "stop", "entry_mode": "form",
                          "departure_date": (date.today() + timedelta(days=10)).isoformat()})
    final = wait(service, job["job_id"])
    assert final["status"] == "inconclusive"
    assert final["access_events"][0]["phase"] == "home_form"
    assert not final["entry_evidence"]["submit_attempted"]
    page.goto.assert_not_awaited()
    page.evaluate.assert_not_awaited()


def test_access_history_survives_error_after_manual_resume(env):
    service, repo, control, page, automation, query = env
    control.challenge = True
    job = service.submit(REQUEST)
    waiting = wait(service, job["job_id"], lambda j: j["status"] == "waiting_user")
    assert waiting["request"]["interaction_mode"] == "wait"  # legacy API default
    page.locator.return_value.inner_text.side_effect = RuntimeError("private-session-token")
    service.control(job["job_id"], "continue")
    final = wait(service, job["job_id"])
    assert final["status"] == "failed"
    assert final["access_events"] == waiting["access_events"]
    assert "private-session-token" not in json.dumps(final)


def test_access_history_survives_restart_without_replay(env):
    service, repo, control, page, automation, query = env
    events = [{"state": "needs_verification", "phase": "result_page", "action": "wait",
               "observed_at": repo.now_iso()}]
    repo.save_experimental_query({
        "job_id": "orphan-access", "status": "waiting_user", "updated_at": repo.now_iso(),
        "access_events": events,
    })
    recovered = ExperimentalQueryService(query_service=query, repository=repo, run_lock=query.run_lock)
    assert recovered.get("orphan-access")["status"] == "interrupted"
    assert recovered.get("orphan-access")["access_events"] == events
    page.goto.assert_not_awaited()


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
    {"interaction_mode": None}, {"interaction_mode": True}, {"interaction_mode": []},
    {"interaction_mode": {}}, {"interaction_mode": "solve"},
    {"session_mode": None}, {"session_mode": True}, {"session_mode": []}, {"session_mode": "rotate"},
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
        assert client.post("/api/experiments/airchina/session/close").json() == {"closed": True}
        assert client.post("/api/experiments/airchina/session/close",
                           headers={"Origin": "https://evil.test"}).status_code == 403


def wait_session(service, expected):
    end = time.monotonic() + 5
    while time.monotonic() < end:
        current = service.session_status()
        if current["state"] == expected:
            return current
        time.sleep(.01)
    raise AssertionError(f"session did not reach {expected}: {current}")


def test_prepare_ready_and_close_persistent_session(env):
    service, repo, control, page, automation, query = env
    service.prepare_browser_session()
    waiting = wait_session(service, "waiting_user")
    assert waiting["mode"] == "persistent"
    assert query.run_lock.locked()
    service.browser_session_ready(backup=False)
    ready = wait_session(service, "ready")
    assert ready["retention_reason"] == "manual_session_prepared"
    assert not query.run_lock.locked()
    automation._new_page.assert_awaited_once()
    automation._close_context_quietly.assert_not_awaited()
    assert repo.count_snapshots() == 0
    service.without_browser_session()
    assert wait_session(service, "closed")
    automation._close_context_quietly.assert_awaited_once_with("context")


def test_restore_failure_remains_visible_until_explicit_close(env, monkeypatch):
    from local_session_state_store import SessionStateError

    service, *_ = env
    monkeypatch.setattr(
        service.experimental_sessions.store,
        "load",
        Mock(side_effect=SessionStateError("invalid_backup")),
    )
    service.prepare_browser_session(restore=True)
    failure = wait_session(service, "error")
    assert failure["error_code"] == "invalid_backup"
    service.without_browser_session()
    assert wait_session(service, "closed")


@pytest.mark.parametrize("mode", ["reuse", "persistent"])
def test_verification_cleared_without_fares_retains_and_reuses_session(env, monkeypatch, mode):
    service, repo, control, page, automation, query = env

    async def empty_wait(*_args, **_kwargs):
        return {
            "state": "query_page_unverified",
            "wait_evidence": {},
            "reference_preview": {
                "status": "unverified", "reason": "page_reports_no_results", "offers": [],
                "quote_saved": False, "parser_ready": False, "coverage_complete": False,
            },
        }

    monkeypatch.setattr("providers.airchina.wait_for_results", empty_wait)
    request = {**REQUEST, "session_mode": mode, "interaction_mode": "wait"}
    control.challenge = True
    first_id = service.submit(request)["job_id"]
    wait(service, first_id, lambda job: job["status"] == "waiting_user")
    control.challenge = False
    service.control(first_id, "continue")
    first = wait(service, first_id)
    service._thread.join(1)
    assert first["status"] == "inconclusive"
    assert first["session_evidence"]["state"] == "retained"
    assert first["session_evidence"]["retention_reason"] == "manual_verification_cleared"
    assert not control.close

    query.airchina_query_gate().resume(confirmed=True)
    second = wait(service, service.submit(request)["job_id"])
    service._thread.join(1)
    assert second["session_evidence"]["reused"]
    assert second["session_evidence"]["generation"] == first["session_evidence"]["generation"]
    automation._new_page.assert_awaited_once()
    assert repo.count_snapshots() == 0


def test_session_api_prepare_restore_ready_cancel_and_delete(tmp_path, monkeypatch):
    from experimental_session_service import ExperimentalSessionService

    calls = []

    def prepare(self, *, restore=False):
        calls.append(("restore" if restore else "prepare", restore))
        return {"state": "preparing", "restore_requested": restore, "backup": {}}

    def ready(self, *, backup=False):
        calls.append(("ready", backup))
        return {"state": "validating", "backup": {}}

    monkeypatch.setattr(ExperimentalSessionService, "prepare", prepare)
    monkeypatch.setattr(ExperimentalSessionService, "ready", ready)
    monkeypatch.setattr(ExperimentalSessionService, "status", lambda self: {"state": "closed", "backup": {}})
    monkeypatch.setattr(ExperimentalSessionService, "cancel", lambda self: calls.append(("cancel", None)) or {"state": "cancelling"})
    monkeypatch.setattr(ExperimentalSessionService, "delete_backup", lambda self: calls.append(("delete", None)) or {"available": False})

    with TestClient(create_app(str(config_file(tmp_path)))) as client:
        assert client.get("/api/experiments/airchina/session").status_code == 200
        assert client.post("/api/experiments/airchina/session/prepare").status_code == 202
        assert client.post("/api/experiments/airchina/session/restore").status_code == 202
        assert client.post("/api/experiments/airchina/session/ready", json={"backup": True}).status_code == 200
        assert client.post("/api/experiments/airchina/session/cancel-prepare").status_code == 200
        assert client.delete("/api/experiments/airchina/session/backup").json() == {"available": False}
        assert client.post("/api/experiments/airchina/session/prepare",
                           headers={"Origin": "https://evil.test"}).status_code == 403
    assert calls == [("prepare", False), ("restore", True), ("ready", True), ("cancel", None), ("delete", None)]


def test_flyclaw_query_engine_uses_source_with_existing_task_lifecycle(env, monkeypatch):
    service, repo, control, page, automation, query = env
    captured = {}

    class Source:
        def __init__(self, query_service, **options):
            captured.update(options)
            captured["query_service"] = query_service
            self.last_report = None

        async def query_by_route_async(self, *_args, **_kwargs):
            self.last_report = {
                "state": "reference_fares_observed",
                "message": "FlyClaw 国航参考票价",
                "reference_preview": {"offers": []},
            }
            return []

    monkeypatch.setattr("providers.airchina_direct.create_airchina_source", Source)
    job_id = service.submit({
        **REQUEST,
        "departure_date": (date.today() + timedelta(days=10)).isoformat(),
        "query_engine": "flyclaw",
        "entry_mode": "form",
    })["job_id"]
    final = wait(service, job_id)
    assert final["status"] == "completed_reference"
    assert final["request"]["query_engine"] == "flyclaw"
    assert final["result"]["message"] == "FlyClaw 国航参考票价"
    assert captured["query_service"] is query
    assert captured["entry_mode"] == "form"
    assert captured["session_mode"] == "fresh"
    assert repo.count_snapshots() == 0
    page.goto.assert_not_awaited()
