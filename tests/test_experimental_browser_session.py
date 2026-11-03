"""No browser, network or site storage. Real loops test owner/lifetime semantics."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from experimental_browser_session import ExperimentalBrowserSession, ExperimentalSessionRunner
from experimental_browser_session import RetainedSessionUnavailable


@pytest.fixture
def fixture(tmp_path):
    cfg = SimpleNamespace(runtime_dir=tmp_path, browser_backend="chrome", browser_backend_fallbacks=[],
                          browser_channel=None, automation_backend="playwright", headless=False)
    provider = SimpleNamespace(name="airchina", extra_headers={"test": "private-header"})
    created, closed = [], []
    @asynccontextmanager
    async def factory(config, credentials):
        assert not credentials
        page = SimpleNamespace(closed=False)
        page.is_closed = lambda: page.closed
        context = object()
        automation = SimpleNamespace(_new_page=AsyncMock(return_value=(page, context)))
        async def close(_context):
            page.closed = True
        automation._close_context_quietly = AsyncMock(side_effect=close)
        item = (automation, page, context, asyncio.get_running_loop())
        created.append(item)
        try:
            yield automation
        finally:
            closed.append(item)
    return cfg, provider, factory, created, closed


def test_two_tasks_use_identical_page_context_and_live_loop(fixture):
    cfg, provider, factory, created, closed = fixture
    runner = ExperimentalSessionRunner()
    audits = []
    async def task(pool):
        async with pool.lease(cfg, provider, factory, mode="reuse", callback=audits.append) as lease:
            lease.keep = True
            return lease.page, lease.context, asyncio.get_running_loop()
    try:
        first, second = runner.run(task), runner.run(task)
        assert all(a is b for a, b in zip(first, second))
        assert len(created) == 1 and not closed
        assert audits[-1]["reused"] and audits[-1]["use_number"] == 2
        assert "private-header" not in str(audits) and str(cfg.runtime_dir) not in str(audits)
        runner.close_idle()
        assert len(closed) == 1
    finally:
        runner.close()
        runner.close()  # application/test teardown is idempotent
    assert not runner._thread.is_alive()


@pytest.mark.parametrize("mode", ["fresh", "reuse"])
def test_failed_read_is_never_kept(fixture, mode):
    cfg, provider, factory, created, closed = fixture
    async def run():
        pool = ExperimentalBrowserSession()
        async with pool.lease(cfg, provider, factory, mode=mode):
            pass  # no validated result, including CAPTCHA/no-results
        assert len(closed) == 1 and pool._record is None
    asyncio.run(run())


@pytest.mark.parametrize("state", ["absent", "expired", "closed", "config", "valid"])
def test_requery_never_opens_replacement_browser(fixture, state):
    cfg, provider, factory, created, closed = fixture
    async def run():
        now = [0]
        pool = ExperimentalBrowserSession(clock=lambda: now[0])
        if state != "absent":
            async with pool.lease(cfg, provider, factory, mode="reuse") as lease:
                lease.keep = True
            if state == "expired":
                now[0] = 9999
            elif state == "closed":
                created[0][1].closed = True
            elif state == "config":
                provider.extra_headers["test"] = "changed"
        before = len(created)
        async def take():
            async with pool.lease(cfg, provider, factory, mode="reuse", require_existing=True) as lease:
                assert lease.page is created[0][1]
                lease.keep = True
        if state == "valid":
            await take()
        else:
            with pytest.raises(RetainedSessionUnavailable):
                await take()
        assert len(created) == before
        await pool.close()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["fresh", "config", "page", "profile"])
def test_reuse_invalidation_closes_before_opening(fixture, change):
    cfg, provider, factory, created, closed = fixture
    async def run():
        pool = ExperimentalBrowserSession()
        audits = []
        async with pool.lease(cfg, provider, factory, mode="reuse") as lease:
            lease.keep = True
        if change == "config":
            provider.extra_headers["test"] = "changed"
        if change == "page":
            created[0][1].closed = True
        if change == "profile":
            path = cfg.runtime_dir / "browser_profiles" / "profile_state.json"
            path.parent.mkdir()
            path.write_text("{}")
        async with pool.lease(cfg, provider, factory, mode="fresh" if change == "fresh" else "reuse",
                              callback=audits.append) as lease:
            assert len(closed) == 1 and len(created) == 2
            lease.keep = True
        assert not audits[-1]["reused"]
        assert audits[-1]["reason"] == {
            "fresh": "fresh_requested", "config": "configuration_changed",
            "page": "page_closed", "profile": "configuration_changed",
        }[change]
        await pool.close()
    asyncio.run(run())


def test_idle_expiry_closes_without_query_or_another_task(fixture):
    cfg, provider, factory, created, closed = fixture
    async def run():
        pool = ExperimentalBrowserSession(idle_seconds=.03)
        async with pool.lease(cfg, provider, factory, mode="reuse") as lease:
            lease.keep = True
        await asyncio.sleep(.1)
        assert len(closed) == 1
        assert created[0][0]._new_page.await_count == 1
        assert pool._record is None
    asyncio.run(run())


def test_absolute_lifetime_cannot_be_extended_by_queries(fixture):
    cfg, provider, factory, created, closed = fixture
    async def run():
        now = [0]
        pool = ExperimentalBrowserSession(idle_seconds=10, max_age_seconds=15, clock=lambda: now[0])
        async with pool.lease(cfg, provider, factory, mode="reuse") as lease:
            lease.keep = True
        now[0] = 9
        async with pool.lease(cfg, provider, factory, mode="reuse") as lease:
            assert len(created) == 1
            lease.keep = True
        assert pool._record["expires"] == 15
        now[0] = 16
        async with pool.lease(cfg, provider, factory, mode="reuse") as lease:
            assert len(created) == 2 and len(closed) == 1
            lease.keep = True
            now[0] = 40  # don't close in flight, but cannot retain at task end
        assert len(closed) == 2 and pool._record is None
    asyncio.run(run())


@pytest.mark.parametrize("ending", ["error", "cancel", "audit"])
def test_exceptions_close_owner_and_never_retry(fixture, ending):
    cfg, provider, factory, created, closed = fixture
    async def run():
        pool = ExperimentalBrowserSession()
        def record(evidence):
            if ending == "audit" and evidence["state"] == "retained":
                raise OSError("audit unavailable")
        with pytest.raises((RuntimeError, asyncio.CancelledError, OSError)):
            async with pool.lease(cfg, provider, factory, mode="reuse", callback=record) as lease:
                lease.keep = True
                if ending == "cancel":
                    raise asyncio.CancelledError()
                if ending == "error":
                    raise RuntimeError("private-error")
        assert len(closed) == len(created) == 1
        assert pool._record is None
    asyncio.run(run())


def test_cleanup_failure_blocks_reopening(fixture):
    cfg, provider, factory, created, closed = fixture
    async def run():
        pool = ExperimentalBrowserSession()
        with pytest.raises(OSError):
            async with pool.lease(cfg, provider, factory, mode="reuse") as lease:
                lease.automation._close_context_quietly.side_effect = OSError("private-close-error")
        assert len(closed) == 1  # manager exit still ran
        with pytest.raises(RuntimeError, match="清理"):
            async with pool.lease(cfg, provider, factory, mode="reuse"):
                pytest.fail("must not reopen")
        assert len(created) == 1
    asyncio.run(run())


def test_new_page_failure_still_stops_automation(fixture):
    cfg, provider, factory, created, closed = fixture
    @asynccontextmanager
    async def broken(config, credentials):
        async with factory(config, credentials) as automation:
            automation._new_page.side_effect = RuntimeError("private-startup-error")
            yield automation
    async def run():
        pool = ExperimentalBrowserSession()
        with pytest.raises(RuntimeError):
            async with pool.lease(cfg, provider, broken, mode="reuse"):
                pytest.fail("no page")
        assert len(created) == len(closed) == 1
        assert pool._record is None
    asyncio.run(run())


def test_partial_startup_failure_still_calls_manager_exit(fixture):
    cfg, provider, _, _, _ = fixture
    manager = SimpleNamespace(__aenter__=AsyncMock(side_effect=RuntimeError("startup")),
                              __aexit__=AsyncMock())
    async def run():
        pool = ExperimentalBrowserSession()
        with pytest.raises(RuntimeError):
            async with pool.lease(cfg, provider, lambda *args: manager, mode="reuse"):
                pytest.fail("no automation")
        manager.__aexit__.assert_awaited_once()
    asyncio.run(run())


def test_persistent_ignores_short_expiry_and_resets_lease_retention(fixture):
    cfg, provider, factory, created, closed = fixture
    async def run():
        now = [0]
        pool = ExperimentalBrowserSession(idle_seconds=.01, max_age_seconds=.02, clock=lambda: now[0])
        audits = []
        async with pool.lease(cfg, provider, factory, mode="persistent", callback=audits.append) as lease:
            lease.keep = True
            lease.retention_reason = "manual_session_prepared"
        now[0] = 999
        async with pool.lease(cfg, provider, factory, mode="persistent", callback=audits.append) as lease:
            assert lease.reused
            assert lease.retention_reason is None
            lease.keep = True
            lease.retention_reason = "access_validated"
        assert len(created) == 1 and not closed
        assert audits[-1]["retention_reason"] == "access_validated"
        assert (await pool.snapshot())["state"] == "retained"
        async with pool.lease(cfg, provider, factory, mode="fresh"):
            assert len(created) == 2 and len(closed) == 1
        assert len(closed) == 2 and pool._record is None
    asyncio.run(run())


def test_snapshot_does_not_wait_for_active_lease(fixture):
    cfg, provider, factory, _, _ = fixture
    async def run():
        pool = ExperimentalBrowserSession()
        async with pool.lease(cfg, provider, factory, mode="persistent"):
            current = await asyncio.wait_for(pool.snapshot(), timeout=.1)
            assert current["state"] == "in_use"
            assert current["mode"] == "persistent"
        await pool.close()
    asyncio.run(run())
