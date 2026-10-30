"""Offline dual-provider orchestration, using real queues/checkpoints and fake I/O."""
import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from data_storage import PriceRepository
from tail_batch import ACTIVE, batch_children
from tail_discovery_manager import TailDiscoveryManager
from tail_discovery_service import TailDiscoveryService
from test_tail_improvements import quote, DATE
from test_web_ui_validation import write_config


def request(**changes):
    return {"provider": "both", "origin": "BJS", "transfer": "SHA", "departure_date": DATE,
            "cabin": "economy", "candidates": ["CAN"], "search_scope": "candidates",
            "feizhu_search_scope": "open", "max_candidates": 2, "target_results": 0,
            "max_provider_calls": 20, **changes}


def wait_for(manager, job_id, predicate=None):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        job = manager.tail_discovery_job(job_id)
        if (predicate(job) if predicate else job["status"] not in ACTIVE):
            return job
        time.sleep(.01)
    raise AssertionError(f"job did not reach expected state: {job['status']}, {job.get('error')}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = PriceRepository(tmp_path / "test.db")
    repo.initialize()
    config = SimpleNamespace(runtime_dir=tmp_path, default_currency="CNY",
                             providers={p: SimpleNamespace(enabled=True) for p in ("ctrip", "feizhu")})
    manager = TailDiscoveryManager(config=config, config_manager=SimpleNamespace(load_credentials=lambda **kw: {}),
                                   repository=repo, run_lock=threading.Lock(), safe_batch_size=3)
    control = SimpleNamespace(calls=[], entered=set(), hold=set(), fail=set(), report_failure=set(), crash_after={},
                              verification=False, continued=False, cancelled=set())

    class Client:
        async def query_tail_baseline(self, provider, **kw):
            return None

        async def discover_tail_routes(self, provider, **kw):
            control.calls.append((provider, deepcopy(kw["candidates"])))
            control.entered.add(provider)
            if provider in control.fail:
                raise RuntimeError(f"{provider} simulated failure")
            if provider in control.report_failure:
                attempts = [{"destination": code, "status":"failed", "phase":"query_failed",
                             "error":f"{provider} mock authorization unavailable"} for code in kw["candidates"]]
                return {"results":[], "attempts":attempts, "errors":["mock authorization unavailable"]}
            if provider == "ctrip" and control.verification:
                kw["progress_callback"]({"stage": "needs_verification", "destination": kw["candidates"][0],
                                         "message": "等待人工验证"})
                while not kw["verification_continue_checker"]():
                    if kw["cancel_checker"]():
                        break
                    await asyncio.sleep(.01)
                control.continued = True
                kw["progress_callback"]({"stage": "verification_continued", "destination": kw["candidates"][0]})
            while provider in control.hold and not kw["cancel_checker"]():
                await asyncio.sleep(.01)
            if kw["cancel_checker"]():
                control.cancelled.add(provider)
                return {"results": [], "attempts": [], "errors": []}
            rows, attempts = [], []
            for code in kw["candidates"]:
                item = quote("CAN" if code == "__OPEN__" else code, provider=provider)
                rows.append(item)
                attempt = {"destination": code, "status": "matched", "phase": "matched"}
                attempts.append(attempt)
                kw["progress_callback"]({"stage": "candidate_finished", "destination": code,
                                         "attempt": attempt, "results": [item]})
                if control.crash_after.get(provider) == code:
                    raise RuntimeError(f"{provider} failure after checkpoint")
            return {"results": rows, "attempts": attempts, "errors": []}

    @asynccontextmanager
    async def client(self, provider, credentials):
        yield Client()

    monkeypatch.setattr(TailDiscoveryService, "_tail_discovery_client", client)
    yield manager, repo, control
    # Ensure an assertion failure never leaves a worker writing after tmp cleanup.
    control.hold.clear()
    control.verification = False
    for job in manager.tail_discovery_jobs():
        if job["status"] in ACTIVE:
            manager.cancel_tail_discovery_job(job["job_id"])
            wait_for(manager, job["job_id"])


def test_both_enter_concurrently_and_global_admission_is_held(env):
    manager, repo, control = env
    control.hold = {"ctrip", "feizhu"}
    job = manager.submit_tail_discovery(request())
    wait_for(manager, job["job_id"], lambda j: control.entered == {"ctrip", "feizhu"})
    assert manager.run_lock.locked()
    with pytest.raises(RuntimeError):
        manager.submit_tail_discovery(request(provider="ctrip", max_provider_calls=0))
    control.hold.clear()
    final = wait_for(manager, job["job_id"])
    assert final["status"] == "finished"
    assert not manager.run_lock.locked()
    assert {r["provider"] for r in manager.tail_discovery_results(job_id=job["job_id"])} == {"ctrip", "feizhu"}
    children = batch_children(final)
    assert {c["payload"]["search_scope"] for c in children} == {"candidates", "open"}
    by_provider = {c["provider"]: c for c in children}
    assert by_provider["ctrip"]["payload"]["max_provider_calls"] == 0
    assert by_provider["ctrip"]["payload"]["max_candidates"] == 2
    assert by_provider["feizhu"]["payload"]["max_candidates"] == 0
    assert by_provider["feizhu"]["payload"]["max_provider_calls"] == 20
    assert len(repo.fetch_tail_discovery_jobs()) == 1  # internal children do not clutter history
    assert repo.latest_tail_discovery_job()["job_id"] == job["job_id"]


@pytest.mark.parametrize("provider", ["ctrip", "feizhu"])
@pytest.mark.parametrize("action", ["resume", "retry-failed"])
def test_provider_failure_isolated_and_only_failed_provider_reruns(env, provider, action):
    manager, repo, control = env
    control.fail.add(provider)
    first = wait_for(manager, manager.submit_tail_discovery(request())["job_id"])
    assert first["status"] == "partial"
    original_ids = {r["id"] for r in manager.tail_discovery_results(job_id=first["job_id"])}
    assert len(original_ids) == 1
    control.fail.clear()
    control.calls.clear()
    again = (manager.restart_job(first["job_id"], "resume") if action == "resume"
             else manager.retry_failed_tail_discovery_job(first["job_id"]))
    final = wait_for(manager, again["job_id"])
    assert final["status"] == "finished"
    assert {p for p, codes in control.calls} == {provider}
    rows = manager.tail_discovery_results(job_id=again["job_id"])
    assert len(rows) == 2 and original_ids <= {r["id"] for r in rows}
    reused = [c for c in batch_children(final) if c["reused"]]
    assert len(reused) == 1 and reused[0]["provider"] != provider


def test_crash_after_checkpoint_resume_preserves_both_roots_without_requerying_done_city(env):
    manager, repo, control = env
    control.crash_after["ctrip"] = "CAN"
    first = wait_for(manager, manager.submit_tail_discovery(request(candidates=["CAN", "SZX"], max_candidates=0))["job_id"])
    assert first["status"] == "partial"
    assert len(manager.tail_discovery_results(job_id=first["job_id"])) == 2
    control.crash_after.clear()
    control.calls.clear()
    final = wait_for(manager, manager.restart_job(first["job_id"], "resume")["job_id"])
    assert final["status"] == "finished"
    assert control.calls == [("ctrip", ["SZX"])]
    assert len(manager.tail_discovery_results(job_id=final["job_id"])) == 3
    ctrip = next(c for c in batch_children(final) if c["provider"] == "ctrip")
    previous = next(c for c in batch_children(first) if c["provider"] == "ctrip")
    assert ctrip["checkpoint_job_id"] == previous["checkpoint_job_id"]


def test_verification_wait_does_not_block_fliggy_results_and_continue_targets_ctrip(env):
    manager, repo, control = env
    control.verification = True
    job = manager.submit_tail_discovery(request())
    current = wait_for(manager, job["job_id"], lambda j: j["status"] == "needs_verification"
                       and any(c["provider"] == "feizhu" and c["status"] == "finished" for c in batch_children(j)))
    assert manager.run_lock.locked()
    assert {r["provider"] for r in manager.tail_discovery_results(job_id=current["job_id"])} == {"feizhu"}
    manager.continue_tail_discovery_job(job["job_id"])
    final = wait_for(manager, job["job_id"])
    assert final["status"] == "finished"
    assert control.continued


def test_cancel_reaches_both_and_releases_lock_only_after_both_exit(env):
    manager, repo, control = env
    control.hold = {"ctrip", "feizhu"}
    job = manager.submit_tail_discovery(request())
    wait_for(manager, job["job_id"], lambda j: control.entered == {"ctrip", "feizhu"})
    manager.cancel_tail_discovery_job(job["job_id"])
    final = wait_for(manager, job["job_id"])
    assert final["status"] == "cancelled"
    assert control.cancelled == {"ctrip", "feizhu"}
    assert not manager.run_lock.locked()


@pytest.mark.parametrize("cause", ["disabled", "cooldown"])
def test_unavailable_platform_does_not_prevent_other_platform(env, cause):
    manager, repo, control = env
    if cause == "disabled":
        manager.config.providers["feizhu"].enabled = False
    else:
        repo.set_tail_cooldown("feizhu", "2099-01-01T00:00:00+00:00")
    final = wait_for(manager, manager.submit_tail_discovery(request())["job_id"])
    assert final["status"] == "partial"
    assert control.entered == {"ctrip"}
    child = next(c for c in batch_children(final) if c["provider"] == "feizhu")
    assert child["status"] == "failed" and child["error"]
    assert len(manager.tail_discovery_results(job_id=final["job_id"])) == 1


def test_all_platforms_failed_is_not_reported_as_success(env):
    manager, repo, control = env
    control.fail = {"ctrip", "feizhu"}
    final = wait_for(manager, manager.submit_tail_discovery(request())["job_id"])
    assert final["status"] == "failed"
    assert {c["status"] for c in batch_children(final)} == {"failed"}
    assert not manager.run_lock.locked()

def test_reported_provider_errors_are_visible_and_retryable_not_successful_empty_results(env):
    manager, repo, control = env
    control.report_failure.add("feizhu")
    first = wait_for(manager, manager.submit_tail_discovery(request())["job_id"])
    assert first["status"] == "partial"
    f = next(c for c in batch_children(first) if c["provider"] == "feizhu")
    assert f["status"] == "failed"
    assert "authorization unavailable" in f["error"]
    control.report_failure.clear()
    control.calls.clear()
    final = wait_for(manager, manager.retry_failed_tail_discovery_job(first["job_id"])["job_id"])
    assert final["status"] == "finished"
    assert control.calls == [("feizhu", ["__OPEN__"])]

def test_latest_prefers_new_parent_even_when_created_in_same_second(env, monkeypatch):
    import tail_jobs
    manager, repo, control = env
    monkeypatch.setattr(tail_jobs, "now_iso", lambda: "2026-10-30T00:00:00Z")
    first = wait_for(manager, manager.submit_tail_discovery(request())["job_id"])
    control.hold = {"ctrip", "feizhu"}
    second = manager.submit_tail_discovery(request())
    assert first["created_at"] == second["created_at"]
    assert manager.latest_tail_discovery_job()["job_id"] == second["job_id"]
    control.hold.clear()
    wait_for(manager, second["job_id"])
    assert manager.latest_tail_discovery_job()["job_id"] == second["job_id"]
    # Equal-time persisted rows also have a deterministic newest-parent order.
    for job in (first, manager.tail_discovery_job(second["job_id"])):
        job["updated_at"] = "2026-10-30T00:00:00Z"
        repo.upsert_tail_discovery_job(job)
    assert repo.latest_tail_discovery_job()["job_id"] == second["job_id"]


@pytest.mark.parametrize("scope", ["candidates", "hybrid"])
def test_dual_supports_fliggy_directed_or_hybrid_without_changing_ctrip(env, scope):
    manager, repo, control = env
    # No followup for hybrid keeps the test independent of intentional cooldown.
    final = wait_for(manager, manager.submit_tail_discovery(request(
        feizhu_search_scope=scope, max_followup_candidates=0))["job_id"])
    by_provider = {c["provider"]: c for c in batch_children(final)}
    assert by_provider["ctrip"]["payload"]["search_scope"] == "candidates"
    assert by_provider["feizhu"]["payload"]["search_scope"] == scope
    assert ("ctrip", ["CAN"]) in control.calls
    assert ("feizhu", ["__OPEN__"] if scope == "hybrid" else ["CAN"]) in control.calls


def test_batch_scope_paginates_union_before_limit_and_never_falls_back_to_history(env):
    manager, repo, control = env
    final = wait_for(manager, manager.submit_tail_discovery(request())["job_id"])
    expected = {r["id"] for r in manager.tail_discovery_results(job_id=final["job_id"])}
    for index in range(205):
        repo.insert_tail_discovery_result(quote(price=900 + index))
    first = manager.tail_discovery_results(job_id=final["job_id"], limit=1)
    second = manager.tail_discovery_results(job_id=final["job_id"], limit=1, offset=1)
    assert {first[0]["id"], second[0]["id"]} == expected
    repo.upsert_tail_discovery_job({"job_id": "empty-batch", "status": "failed", "payload": {"provider": "both"}})
    assert manager.tail_discovery_results(job_id="empty-batch") == []


def test_restart_reconstructs_batch_and_retains_successful_provider(env):
    manager, repo, control = env
    control.fail.add("ctrip")
    first = wait_for(manager, manager.submit_tail_discovery(request())["job_id"])
    first["status"] = "running"
    first["result"] = None
    for child in first["progress"]["children"]:
        if child["provider"] == "ctrip":
            child["status"] = "running"
    repo.upsert_tail_discovery_job(first)
    restarted = TailDiscoveryManager(config=manager.config, config_manager=manager.config_manager,
                                    repository=repo, run_lock=threading.Lock(), safe_batch_size=3)
    restored = restarted.latest_tail_discovery_job()
    assert restored["status"] == "interrupted"
    assert next(c for c in batch_children(restored) if c["provider"] == "ctrip")["status"] == "interrupted"
    control.fail.clear()
    control.calls.clear()
    final = wait_for(restarted, restarted.restart_job(first["job_id"], "resume")["job_id"])
    assert final["status"] == "finished"
    assert control.calls == [("ctrip", ["CAN"])]
    assert len(restarted.tail_discovery_results(job_id=final["job_id"])) == 2

def test_restart_after_checkpoints_before_terminal_publish_does_not_repeat_provider_calls(env):
    manager, repo, control = env
    first = wait_for(manager, manager.submit_tail_discovery(request())["job_id"])
    expected = {r["id"] for r in manager.tail_discovery_results(job_id=first["job_id"])}
    for child in batch_children(first):
        row = repo.fetch_tail_discovery_job(child["job_id"])
        row.update(status="running", result=None)
        repo.upsert_tail_discovery_job(row)
        child["status"] = "running"
    first.update(status="running", result=None)
    for child in first["progress"]["children"]:
        child["status"] = "running"
    repo.upsert_tail_discovery_job(first)
    restarted = TailDiscoveryManager(config=manager.config, config_manager=manager.config_manager,
                                    repository=repo, run_lock=threading.Lock(), safe_batch_size=3)
    control.calls.clear()
    final = wait_for(restarted, restarted.restart_job(first["job_id"], "resume")["job_id"])
    assert final["status"] == "finished"
    assert control.calls == []
    assert {r["id"] for r in restarted.tail_discovery_results(job_id=final["job_id"])} == expected


def test_refresh_uses_known_routes_and_isolates_empty_provider(env):
    manager, repo, control = env
    control.fail.add("feizhu")
    first = wait_for(manager, manager.submit_tail_discovery(request())["job_id"])
    control.fail.clear()
    control.calls.clear()
    final = wait_for(manager, manager.restart_job(first["job_id"], "refresh")["job_id"])
    assert final["status"] == "partial"
    assert control.calls == [("ctrip", ["CAN"])]
    f = next(c for c in batch_children(final) if c["provider"] == "feizhu")
    assert "没有已发现" in f["error"]
    # Continuing this partial refresh must not reject the entire batch because
    # the empty provider has no candidates; successful refresh remains retained.
    again = wait_for(manager, manager.restart_job(final["job_id"], "resume")["job_id"])
    assert again["status"] == "partial"
    assert next(c for c in batch_children(again) if c["provider"] == "ctrip")["reused"]


def test_internal_child_plan_cannot_be_injected_and_resume_ignores_changed_form(env):
    manager, repo, control = env
    control.fail.add("ctrip")
    final = wait_for(manager, manager.submit_tail_discovery(request(
        _batch_children=[{"job_id": "unrelated"}], _checkpoint_job_id="unrelated"))["job_id"])
    assert all(c["job_id"] != "unrelated" for c in batch_children(final))
    control.fail.clear()
    resumed = wait_for(manager, manager.submit_tail_discovery(request(
        mode="resume", source_job_id=final["job_id"], origin="CAN", transfer="KMG"))["job_id"])
    assert resumed["payload"]["origin"] == "BJS"
    assert {c["payload"]["origin"] for c in batch_children(resumed)} == {"BJS"}


@pytest.mark.parametrize("changes", [
    {"candidates": []}, {"feizhu_search_scope": "invalid"}, {"mode": "recheck"},
    {"max_minutes": -1}, {"passengers": 0}, {"max_provider_calls": -1},
])
def test_invalid_batch_input_rejected_before_any_task_or_platform_call(env, changes):
    manager, repo, control = env
    with pytest.raises(ValueError):
        manager.submit_tail_discovery(request(**changes))
    assert not repo.fetch_tail_discovery_jobs()
    assert not control.calls
    assert not manager.run_lock.locked()

def test_unadmitted_batch_run_rejected_without_leaking_global_lock(env):
    manager, repo, control = env
    with pytest.raises(ValueError, match="submit_tail_discovery"):
        asyncio.run(manager.run_tail_discovery(request()))
    assert not manager.run_lock.locked()


def test_http_api_submits_one_parent_and_paginates_both_providers(env, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app_service
    from web_ui import create_app
    # Use the same fake transport but the real application/API wiring.
    path = tmp_path / "api-config.json"
    write_config(path)
    config = json.loads(path.read_text())
    config["providers"]["feizhu"] = {**config["providers"]["ctrip"]}
    path.write_text(json.dumps(config))
    captured = []
    def factory(**kwargs):
        manager = TailDiscoveryManager(**kwargs)
        captured.append(manager)
        return manager
    monkeypatch.setattr(app_service, "TailDiscoveryManager", factory)
    with TestClient(create_app(str(path))) as client:
        manager = captured[0]
        try:
            invalid = client.post("/api/tail-discovery/run", json=request(candidates=[]))
            assert invalid.status_code == 400
            response = client.post("/api/tail-discovery/run", json=request())
            assert response.status_code == 200
            job_id = response.json()["job_id"]
            final = wait_for(manager, job_id)
            assert final["status"] == "finished"
            first = client.get(f"/api/tail-discovery/results?job_id={job_id}&limit=1").json()
            assert len(first["results"]) == 1 and first["has_more"]
            second = client.get(f"/api/tail-discovery/results?job_id={job_id}&limit=1&offset={first['next_offset']}").json()
            assert not second["has_more"]
            assert {first["results"][0]["provider"], second["results"][0]["provider"]} == {"ctrip", "feizhu"}
            assert "raw_payload" not in first["results"][0]
            jobs = client.get("/api/tail-discovery/jobs").json()["jobs"]
            assert len(jobs) == 1 and jobs[0]["job_id"] == job_id
            assert client.get("/api/tail-discovery/jobs/latest").json()["job"]["job_id"] == job_id
            # A result-level recheck stays a single-provider job.
            quote_id = first["results"][0]["id"]
            recheck = client.post(f"/api/tail-discovery/results/{quote_id}/recheck")
            assert recheck.status_code == 200
            final_recheck = wait_for(manager, recheck.json()["job_id"])
            assert final_recheck["payload"]["provider"] in {"ctrip", "feizhu"}
            assert final_recheck["payload"]["mode"] == "recheck"
        finally:
            for job in manager.tail_discovery_jobs():
                if job["status"] in ACTIVE:
                    manager.cancel_tail_discovery_job(job["job_id"])
                    wait_for(manager, job["job_id"])
