import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor" / "flyclaw"
if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

from config_manager import RouteQuery
from providers.airchina_direct import flyclaw_records_from_report
from sources.airchina import AirChinaSource
from sources.fliggy_mcp import FliggyMCPSource
import flyclaw as flyclaw_module


def test_cli_help_loads_complete_project():
    result = subprocess.run(
        [sys.executable, str(ROOT / "flyclaw_cli.py"), "search", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "--source" in result.stdout
    assert "--from" in result.stdout and "--to" in result.stdout


def test_fliggy_does_not_use_embedded_credentials(monkeypatch):
    monkeypatch.delenv("FLYCLAW_FLIGGY_API_KEY", raising=False)
    monkeypatch.delenv("FLYCLAW_FLIGGY_SIGN_SECRET", raising=False)
    source = FliggyMCPSource()
    assert source.query_by_route("北京", "上海", "2026-12-13") == []


def test_skiplagged_uses_plain_http_without_impersonation(monkeypatch):
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {"flights": {}}

    class Session:
        def get(self, *args, **kwargs):
            calls.append((args, kwargs))
            return Response()

    monkeypatch.setattr("sources.skiplagged.requests.Session", Session)
    from sources.skiplagged import SkiplaggedSource

    assert SkiplaggedSource()._fetch("PEK", "SHA", "2026-12-13", 1) == {"flights": {}}
    assert "impersonate" not in calls[0][1]


def test_merge_marks_incomparable_quote_terms():
    records = [
        {
            "flight_number": "CA1549", "source": "other", "price": 500,
            "currency": "USD", "price_kind": "final", "price_unit": "per_adult",
            "tax_included": True,
        },
        {
            "flight_number": "CA1549", "source": "airchina", "price": 600,
            "currency": "CNY", "price_kind": "starting_fare", "price_unit": "per_adult",
            "tax_included": False, "reference_only": True,
        },
    ]
    merged = flyclaw_module._merge_records(records)
    assert merged[0]["price_comparison"] == "not_comparable"
    assert set(merged[0]["source"].split(",")) == {"airchina", "other"}
    assert merged[0]["price"] == 600


def native_report():
    return {
        "state": "reference_fares_observed",
        "message": "参考票价",
        "reference_preview": {
            "offers": [{
                "flight_no": "CA1549", "marketing_airline": "CA",
                "departure_date": "2026-12-13", "arrival_date": "2026-12-13",
                "departure_time": "16:30", "arrival_time": "18:45",
                "cabin": "economy", "reference_fare": 600.0,
            }],
        },
    }


def test_airchina_record_mapping_preserves_reference_terms():
    route = RouteQuery("PEK", "SHA", "2026-12-13", preferred_airlines=["CA"])
    records = flyclaw_records_from_report(native_report(), route)
    assert len(records) == 1
    record = records[0]
    assert record["flight_number"] == "CA1549"
    assert record["scheduled_departure"] == "2026-12-13T16:30"
    assert record["scheduled_arrival"] == "2026-12-13T18:45"
    assert record["duration_minutes"] == 135
    assert record["segments"][0]["origin_iata"] == "PEK"
    assert record["price"] == 600
    assert record["price_kind"] == "starting_fare"
    assert record["tax_included"] is False
    assert record["bookable_confirmed"] is False
    assert record["reference_only"] is True
    assert record["source"] == "airchina"


def test_airchina_source_rejects_unsupported_query():
    source = AirChinaSource(query_service=object())
    with pytest.raises(ValueError, match="one-way"):
        asyncio.run(source.query_by_route_async("PEK", "SHA", "2026-12-13", return_date="2026-12-20"))
    with pytest.raises(ValueError, match="Stops"):
        asyncio.run(source.query_by_route_async("PEK", "SHA", "2026-12-13", stops=3))
    with pytest.raises(ValueError, match="one adult"):
        asyncio.run(source.query_by_route_async("PEK", "SHA", "2026-12-13", adults=2))


def test_airchina_source_uses_shared_query_service():
    query = SimpleNamespace()
    query.inspect_airchina_experiment = AsyncMock(return_value=native_report())
    source = AirChinaSource(query_service=query, entry_mode="form")
    records = asyncio.run(source.query_by_route_async("PEK", "SHA", "2026-12-13"))
    assert len(records) == 1
    assert source.last_report["state"] == "reference_fares_observed"
    assert query.inspect_airchina_experiment.await_args.kwargs["entry_mode"] == "form"


def test_search_source_limits_tasks_to_airchina(monkeypatch):
    captured = {}

    def execute(tasks, *_args, **_kwargs):
        pytest.fail("Air China-only search must not leave a background query thread.")

    def query(origin, destination, date, timeout, **_kwargs):
        captured["query"] = (origin, destination, date, timeout)
        return []

    monkeypatch.setattr(flyclaw_module, "_execute_concurrent_queries", execute)
    monkeypatch.setattr(flyclaw_module, "_query_airchina_route", query)
    args = flyclaw_module.build_parser().parse_args([
        "search", "--from", "PEK", "--to", "SHA", "--date", "2026-12-13", "--source", "airchina",
    ])
    flyclaw_module.cmd_search(args)
    assert captured["query"] == ("PEK", "SHA", "2026-12-13", 240)


@pytest.mark.parametrize("extra", [
    ["--return", "2026-12-20"], ["--children", "1"], ["--infants", "1"], ["--adults", "2"],
])
def test_airchina_cli_never_silently_drops_trip_or_passengers(monkeypatch, extra):
    query = Mock()
    monkeypatch.setattr(flyclaw_module, "_query_airchina_route", query)
    args = flyclaw_module.build_parser().parse_args([
        "search", "--from", "PEK", "--to", "SHA", "--date", "2026-12-13", "--source", "airchina",
        *extra,
    ])
    with pytest.raises(ValueError):
        flyclaw_module.cmd_search(args)
    query.assert_not_called()


def test_airchina_cli_passes_city_input_and_explicit_timeout(monkeypatch):
    query = Mock(return_value=[])
    monkeypatch.setattr(flyclaw_module, "_query_airchina_route", query)
    monkeypatch.setattr(flyclaw_module.route_cache, "save", lambda: None)
    args = flyclaw_module.build_parser().parse_args([
        "search", "--from", "北京", "--to", "上海", "--date", "2026-12-13",
        "--source", "airchina", "--timeout", "35", "--cabin", "premium", "--stops", "any",
    ])
    flyclaw_module.cmd_search(args)
    assert query.call_args.args == ("北京", "上海", "2026-12-13", 35)
    assert query.call_args.kwargs["cabin"] == "premium"
    assert query.call_args.kwargs["stops"] == "any"
    assert query.call_args.kwargs["return_date"] is None


def test_reference_table_displays_actual_currency():
    text = flyclaw_module._format_table([
        {"flight_number": "CA1549", "price": 600, "currency": "CNY", "reference_only": True},
    ])
    assert "CNY 600" in text
    assert "$600" not in text


def test_airchina_cli_preserves_distinct_airport_pairs_and_reports_uncertainty(monkeypatch, capsys):
    records = [
        {"flight_number": "CA1549", "origin_iata": "PEK", "destination_iata": end,
         "price": 600, "currency": "CNY", "reference_only": True, "source": "airchina"}
        for end in ("SHA", "PVG")
    ]
    monkeypatch.setattr(flyclaw_module, "_query_airchina_route", lambda *a, **kw: records)
    monkeypatch.setattr(flyclaw_module.route_cache, "save", lambda: None)
    monkeypatch.setattr(flyclaw_module.route_cache, "update_from_records", lambda r: None)
    args = flyclaw_module.build_parser().parse_args([
        "search", "--from", "PEK", "--to", "上海", "--date", "2026-12-13",
        "--source", "airchina", "--output", "json", "--currency", "original",
    ])
    flyclaw_module.cmd_search(args)
    assert len(json.loads(capsys.readouterr().out)) == 2
