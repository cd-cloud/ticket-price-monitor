"""No website traffic: admission, persistence, spacing and failure boundaries."""
import asyncio
import json
import subprocess
import sys
import threading
import time
from unittest.mock import AsyncMock

import pytest

from airchina_query_gate import AirChinaQueryGate, retry_after_seconds
from config_manager import RouteQuery


def success():
    return {"state": "reference_fares_observed", "message": "reference",
            "reference_preview": {"offers": [{"reference_fare": 600}]},
            "notices": [{"body": "PRIVATE_NOTICE"}], "session_evidence": {"token": "PRIVATE_TOKEN"},
            "quote_saved": False, "coverage_complete": False}


async def run(gate, key="one", execute=None, **kwargs):
    return await gate.run(key, execute or AsyncMock(return_value=success()),
                          cancel_event=kwargs.pop("cancel_event", threading.Event()),
                          progress=kwargs.pop("progress", lambda _: None), **kwargs)


def test_cache_ttl_labels_and_no_sensitive_side_channels(tmp_path):
    now = [1000.]
    gate = AirChinaQueryGate(tmp_path, ttl=120, min_interval=0, clock=lambda: now[0])
    execute = AsyncMock(return_value=success())
    first = asyncio.run(run(gate, execute=execute))
    assert first["query_policy"]["source"] == "live"
    now[0] += 20
    cached = asyncio.run(run(gate, execute=execute))
    assert cached["query_policy"]["source"] == "cache"
    assert cached["query_policy"]["age_seconds"] == 20
    assert "session_evidence" not in cached and "notices" not in cached
    assert b"PRIVATE" not in gate.db.read_bytes()
    assert execute.await_count == 1
    now[0] += 101
    assert asyncio.run(run(gate, execute=execute))["query_policy"]["source"] == "live"
    assert execute.await_count == 2


def test_identical_concurrent_requests_share_one_success_across_instances(tmp_path):
    async def main():
        gate, other = AirChinaQueryGate(tmp_path, min_interval=0), AirChinaQueryGate(tmp_path, min_interval=0)
        entered = asyncio.Event()
        calls = []
        async def execute():
            calls.append(1)
            entered.set()
            await asyncio.sleep(.15)
            return success()
        first = asyncio.create_task(run(gate, execute=execute))
        await entered.wait()
        second = asyncio.create_task(run(other, execute=execute))
        results = await asyncio.gather(first, second)
        assert calls == [1]
        assert [r["query_policy"]["source"] for r in results] == ["live", "cache"]
        assert other.status()["cache_hits"] == 1
    asyncio.run(main())


def test_different_routes_serialized_and_minimum_start_spacing(tmp_path):
    gate = AirChinaQueryGate(tmp_path, min_interval=.15)
    starts = []
    async def execute():
        starts.append(time.monotonic())
        await asyncio.sleep(.03)
        return success()
    async def main():
        await asyncio.gather(run(gate, "a", execute), run(gate, "b", execute))
    asyncio.run(main())
    assert len(starts) == 2 and starts[1]-starts[0] >= .14


def test_pause_persistent_invalidates_cache_and_resume_does_not_submit(tmp_path):
    gate = AirChinaQueryGate(tmp_path, min_interval=0)
    asyncio.run(run(gate))
    gate.pause("needs_verification")
    other = AirChinaQueryGate(tmp_path, min_interval=0)
    execute = AsyncMock(return_value=success())
    assert asyncio.run(run(other, execute=execute))["state"] == "provider_paused"
    execute.assert_not_awaited()
    with pytest.raises(ValueError):
        other.resume(confirmed=False)
    assert other.resume(confirmed=True)["paused"] is False
    execute.assert_not_awaited()
    assert asyncio.run(run(other, execute=execute))["query_policy"]["source"] == "live"


def test_circuit_stays_open_after_manual_success(tmp_path):
    gate = AirChinaQueryGate(tmp_path, min_interval=0)
    async def execute():
        gate.pause("needs_verification")
        return success()
    result = asyncio.run(run(gate, execute=execute))
    assert result["state"] == "reference_fares_observed"
    assert result["query_policy"]["paused_after_query"] is True
    assert gate.status()["paused"]
    assert asyncio.run(run(gate))["state"] == "provider_paused"


def test_retry_after_cannot_be_reset_early_or_shortened(tmp_path):
    now = [1000.]
    gate = AirChinaQueryGate(tmp_path, clock=lambda: now[0])
    gate.pause("rate_limited", retry_after=80)
    gate.pause("blocked")
    with pytest.raises(RuntimeError, match="等待"):
        gate.resume(confirmed=True)
    assert gate.status()["retry_after_seconds"] == 80
    now[0] += 81
    assert gate.status()["paused"]
    assert gate.resume(confirmed=True)["paused"] is False


@pytest.mark.parametrize("value,expected", [
    ("60", 60), (" 12 ", 12), ("-1", None), ("abc", None), (None, None),
    ("Wed, 21 Oct 2015 07:28:00 GMT", 1445412480),
    ("9"*200, None), ("０１", None),
])
def test_retry_after_parser(value, expected):
    assert retry_after_seconds(value, now=0) == expected


def test_waiter_cancel_does_not_cancel_owner(tmp_path):
    async def main():
        gate = AirChinaQueryGate(tmp_path, min_interval=0)
        entered, release = asyncio.Event(), asyncio.Event()
        async def execute():
            entered.set()
            await release.wait()
            return success()
        owner = asyncio.create_task(run(gate, execute=execute))
        await entered.wait()
        waiter = asyncio.create_task(run(gate))
        await asyncio.sleep(.02)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not owner.done()
        release.set()
        assert (await owner)["state"] == "reference_fares_observed"
    asyncio.run(main())


def test_cancelled_owner_cleans_up_before_unlock(tmp_path):
    async def main():
        gate = AirChinaQueryGate(tmp_path, min_interval=0)
        entered, cleaned = asyncio.Event(), []
        async def execute():
            try:
                entered.set()
                await asyncio.sleep(10)
            finally:
                await asyncio.sleep(.02)
                cleaned.append(True)
        owner = asyncio.create_task(run(gate, execute=execute))
        await entered.wait()
        with pytest.raises(RuntimeError, match="运行"):
            gate.resume(confirmed=True)
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert cleaned and gate.status()["running"] == 0
        assert (await run(gate))["state"] == "reference_fares_observed"
    asyncio.run(main())


def test_failed_result_is_not_cached_and_fresh_skips_cache(tmp_path):
    gate = AirChinaQueryGate(tmp_path, min_interval=0)
    asyncio.run(run(gate, execute=AsyncMock(return_value={"state": "query_page_unverified"})))
    assert asyncio.run(run(gate))["query_policy"]["source"] == "live"
    assert asyncio.run(run(gate, cache_allowed=False))["query_policy"]["source"] == "live"


def test_orphaned_run_fails_closed(tmp_path):
    gate = AirChinaQueryGate(tmp_path, min_interval=0)
    with gate.connection() as db:
        db.execute("UPDATE control SET running=1 WHERE id=1")
    result = asyncio.run(run(gate))
    assert result["state"] == "provider_paused"
    assert gate.status()["pause_reason"] == "interrupted"


def test_rate_limit_overrides_visible_results(tmp_path):
    gate = AirChinaQueryGate(tmp_path, min_interval=0)
    async def execute():
        gate.pause("rate_limited", retry_after=10)
        return success()
    assert asyncio.run(run(gate, execute=execute))["state"] == "provider_paused"


def test_full_query_identity():
    route = RouteQuery("PEK", "SHA", "2099-01-01")
    kwargs = dict(flight_no=None, entry_mode="form", session_mode="persistent", manual=False, notices=False)
    first = AirChinaQueryGate.key(route, **kwargs)
    route.cabin = "business"
    assert AirChinaQueryGate.key(route, **kwargs) != first
    route.cabin = "economy"
    assert AirChinaQueryGate.key(route, **{**kwargs, "flight_no": "CA1549"}) != first
    assert AirChinaQueryGate.key(route, **{**kwargs, "manual": True}) != first


def test_os_lock_shared_with_another_process(tmp_path):
    gate = AirChinaQueryGate(tmp_path)
    handle = gate._try_lock()
    try:
        code = """import sys
from airchina_query_gate import AirChinaQueryGate
g=AirChinaQueryGate(sys.argv[1])
h=g._try_lock()
print('locked' if h is None else 'acquired')
if h: g._unlock(h)
"""
        result = subprocess.run([sys.executable, "-c", code, str(tmp_path)],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "locked"
    finally:
        gate._unlock(handle)
