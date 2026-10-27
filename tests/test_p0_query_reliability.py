"""Offline regressions for lock ownership and deferred automatic queries."""
import asyncio
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from query_service import QueryService
from scheduling.auto_worker import AutoQueryWorker, format_iso_datetime


ENTRY_POINTS = [
    ("run_once", ()),
    ("run_route_once", ("route",)),
    ("run_route_group_once", ("group",)),
]


def query_service():
    service = QueryService(
        config_manager=None,
        repository=None,
        run_lock=threading.Lock(),
        get_config=Mock(return_value=SimpleNamespace(
            routes=[SimpleNamespace(route_key="route", group_id="group")])),
        refresh_config=Mock(),
    )
    service._provider_has_handler = Mock(return_value=True)
    service._set_summary = Mock()
    service._query_routes = AsyncMock(return_value={"running": False, "saved": 1, "errors": []})
    return service


@pytest.mark.parametrize("entry,args", ENTRY_POINTS)
@pytest.mark.parametrize("phase", [
    "refresh_config", "get_config", "_provider_has_handler", "_set_summary", "_query_routes",
])
def test_exception_releases_lock_and_next_query_can_start(entry, args, phase):
    service = query_service()
    getattr(service, phase).side_effect = ValueError("simulated failure")
    with pytest.raises(ValueError, match="simulated failure"):
        asyncio.run(getattr(service, entry)(*args, provider_filter="ctrip"))
    assert not service.run_lock.locked()
    getattr(service, phase).side_effect = None
    assert asyncio.run(getattr(service, entry)(*args, provider_filter="ctrip"))["saved"] == 1
    assert not service.run_lock.locked()


@pytest.mark.parametrize("entry,args", ENTRY_POINTS)
def test_busy_query_does_not_release_another_queries_lock(entry, args):
    service = query_service()
    service.run_lock.acquire()
    try:
        assert asyncio.run(getattr(service, entry)(*args))["running"]
        assert service.run_lock.locked()
        service.refresh_config.assert_not_called()
    finally:
        service.run_lock.release()


@pytest.mark.parametrize("entry,args", ENTRY_POINTS)
def test_unknown_provider_releases_lock(entry, args):
    service = query_service()
    service._provider_has_handler.return_value = False
    result = asyncio.run(getattr(service, entry)(*args, provider_filter="missing"))
    assert result["errors"] == ["Unknown provider: missing"]
    assert not service.run_lock.locked()
    service._query_routes.assert_not_called()


@pytest.mark.parametrize("entry,args", ENTRY_POINTS[1:])
def test_missing_route_or_group_releases_lock(entry, args):
    service = query_service()
    service.get_config.return_value.routes = []
    result = asyncio.run(getattr(service, entry)(*args))
    assert result["errors"]
    assert not service.run_lock.locked()
    service._query_routes.assert_not_called()


@pytest.mark.parametrize("entry,args", ENTRY_POINTS)
def test_cancelled_query_releases_lock(entry, args):
    service = query_service()
    service._query_routes.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(getattr(service, entry)(*args))
    assert not service.run_lock.locked()


@pytest.mark.parametrize("has_previous_run", [False, True])
def test_busy_auto_query_keeps_due_time_and_retries_next_tick(monkeypatch, has_previous_run):
    now = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
    clock = [now]
    monkeypatch.setattr("scheduling.auto_worker.utc_now", lambda: clock[0])
    route = SimpleNamespace(
        route_key="route", auto_query_enabled=True, auto_query_interval_hours=12,
        auto_query_next_run_at=format_iso_datetime(now - timedelta(minutes=1)),
    )
    busy = {"running": True, "saved": 0, "errors": ["busy"]}
    finished = {"running": False, "saved": 1, "errors": []}
    service = SimpleNamespace(
        refresh_config=Mock(), config=SimpleNamespace(routes=[route]),
        config_manager=SimpleNamespace(save_routes=Mock()),
        run_route_once_blocking=Mock(side_effect=[busy, busy, finished]),
        report=Mock(),
    )
    worker = AutoQueryWorker(service, 12, 10)
    previous = now - timedelta(minutes=11) if has_previous_run else None
    worker._last_query_started_at = previous
    original_due = route.auto_query_next_run_at
    for _ in range(2):
        assert worker.process() == busy
        assert worker._last_query_started_at == previous
        assert route.auto_query_next_run_at == original_due
        assert not worker._cycle_lock.locked()
        service.config_manager.save_routes.assert_not_called()
        service.report.assert_not_called()
        clock[0] += timedelta(minutes=1)
    assert worker.process() == finished
    assert worker._last_query_started_at == clock[0]
    assert route.auto_query_next_run_at == format_iso_datetime(clock[0] + timedelta(hours=12))
    service.config_manager.save_routes.assert_called_once()
    service.report.assert_called_once()
    # A second due route still respects spacing after a real run.
    route.auto_query_next_run_at = original_due
    clock[0] += timedelta(minutes=1)
    assert "next_allowed_run_at" in worker.process()
    assert service.run_route_once_blocking.call_count == 3


def test_auto_query_exception_keeps_due_route_and_releases_cycle_lock():
    route = SimpleNamespace(
        route_key="route", auto_query_enabled=True,
        auto_query_next_run_at="2020-01-01T00:00:00Z",
    )
    service = SimpleNamespace(
        refresh_config=Mock(), config=SimpleNamespace(routes=[route]),
        run_route_once_blocking=Mock(side_effect=ValueError("query failed")), report=Mock(),
    )
    worker = AutoQueryWorker(service, 12, 10)
    with pytest.raises(ValueError, match="query failed"):
        worker.process()
    assert not worker._cycle_lock.locked()
    assert route.auto_query_next_run_at == "2020-01-01T00:00:00Z"
    service.report.assert_not_called()
