"""Offline probe tests. Synthetic page text is not a live fare sample."""
import asyncio
import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from airchina_probe import inspect_airchina_page
from browser_automation import BrowserAutomation
from config_manager import ConfigManager, RouteQuery
from main import build_parser
from providers.airchina_page import build_search_url, classify_page
from providers.registry import PROVIDERS
from providers.tail_registry import supports_tail_discovery


ROUTE = RouteQuery("PEK", "SHA", "2099-01-01")
TARGET = "https://www.airchina.com.cn/flight/oneway/pek-sha/2099-01-01"


def test_url_comes_from_new_public_entry():
    assert build_search_url(ROUTE) == TARGET
    assert build_search_url(replace(ROUTE, origin="pek", destination="sha")) == TARGET


@pytest.mark.parametrize("changes", [
    {"origin": "BJS"}, {"destination": "TYO"}, {"origin": "PEK?token=x"},
    {"destination": "PEK"}, {"departure_date": "2000-01-01"},
    {"departure_date": "2099-02-30"}, {"departure_date": "20990101"},
    {"departure_date": "2099-1-1"}, {"passengers": 2},
    {"return_date": "2099-01-02"}, {"route_type": "multi_city"},
    {"segments": [object()]},
])
def test_unsupported_conditions_are_not_silently_dropped(changes):
    with pytest.raises(ValueError):
        build_search_url(replace(ROUTE, **changes), today=date(2026, 10, 30))


@pytest.mark.parametrize(("url", "text", "status", "state"), [
    (TARGET, "选择航班 安全验证 请点击上图中的：", 200, "needs_verification"),
    (TARGET, "选择航班 11-10 ¥600", 200, "query_page_unverified"),
    (TARGET, "CA1234 经济舱 ¥600", 200, "query_page_unverified"),
    (TARGET, "访问过于频繁", 200, "blocked"),
    (TARGET, "安全验证", 403, "blocked"),
    (TARGET, "", 429, "blocked"),
    (TARGET, "", 432, "blocked"),
    (TARGET, "", 503, "http_error"),
    (TARGET, "您的停留时间过长，航班及价格信息可能发生变动", 200, "stale_results"),
    (TARGET, "确认，查看下一个提示", 200, "needs_notice_confirmation"),
    ("https://www.airchina.com.cn/zh-CN", "登录", 200, "unexpected_page"),
    ("https://www.airchina.com.cn.evil.test/flight", "", 200, "unexpected_page"),
    ("https://evil.test/?token=secret", "", 200, "unexpected_page"),
    (TARGET.replace("2099-01-01", "2099-01-02"), "选择航班", 200, "unexpected_page"),
])
def test_states_never_establish_a_quote(url, text, status, state):
    report = classify_page(url, text, TARGET, http_status=status)
    assert report["state"] == state
    assert report["parser_ready"] is False
    assert report["quote_saved"] is False
    assert "secret" not in json.dumps(report)
    assert "price" not in report


def fake_automation(tmp_path, *, status=200, text="选择航班 安全验证"):
    page = SimpleNamespace(
        url=TARGET, goto=AsyncMock(return_value=SimpleNamespace(status=status)),
        locator=Mock(return_value=SimpleNamespace(inner_text=AsyncMock(return_value=text))),
        wait_for_timeout=AsyncMock(), screenshot=AsyncMock(),
        evaluate=AsyncMock(return_value={}),
    )
    provider = SimpleNamespace(timeout_ms=1000, enabled=False)
    automation = SimpleNamespace(
        app_config=SimpleNamespace(runtime_dir=tmp_path, providers={"airchina": provider}),
        _new_page=AsyncMock(return_value=(page, "test-context")),
        _throttle=AsyncMock(), _close_context_quietly=AsyncMock(),
    )
    return automation, page


def test_probe_challenge_is_one_navigation_no_capture_or_quote(tmp_path):
    automation, page = fake_automation(tmp_path)
    path = asyncio.run(inspect_airchina_page(automation, ROUTE))
    report = json.loads(path.read_text())
    assert report["state"] == "needs_verification"
    assert report["quote_saved"] is False
    assert report["filters_applied"] is False
    assert set(path.parent.iterdir()) == {path}
    page.goto.assert_awaited_once_with(TARGET, wait_until="domcontentloaded", timeout=1000)
    page.screenshot.assert_not_awaited()
    automation._close_context_quietly.assert_awaited_once_with("test-context")
    assert not automation.app_config.providers["airchina"].enabled


def test_probe_waits_only_with_explicit_opt_in_and_rereads(tmp_path):
    automation, page = fake_automation(tmp_path)
    page.locator.return_value.inner_text.side_effect = ["安全验证", "选择航班 CA1234 ¥600"]
    path = asyncio.run(inspect_airchina_page(automation, ROUTE, manual_wait_seconds=30))
    report = json.loads(path.read_text())
    assert report["state"] == "invalid_snapshot"  # No supported DOM evidence after manual wait.
    assert not report["quote_saved"]
    assert [call.args[0] for call in page.wait_for_timeout.await_args_list] == [3000, 30000]
    page.goto.assert_awaited_once()


def test_http_block_does_not_wait_or_retry(tmp_path):
    automation, page = fake_automation(tmp_path, status=429)
    path = asyncio.run(inspect_airchina_page(automation, ROUTE, manual_wait_seconds=30))
    assert json.loads(path.read_text())["state"] == "blocked"
    page.wait_for_timeout.assert_not_awaited()
    page.goto.assert_awaited_once()


def test_probe_error_is_redacted_and_context_closed(tmp_path):
    automation, page = fake_automation(tmp_path)
    page.goto.side_effect = RuntimeError("secret-token=https://example.test?api_key=secret")
    path = asyncio.run(inspect_airchina_page(automation, ROUTE))
    assert json.loads(path.read_text())["state"] == "probe_error"
    assert "secret" not in path.read_text()
    automation._close_context_quietly.assert_awaited_once()


def test_cancellation_closes_context_and_propagates(tmp_path):
    automation, page = fake_automation(tmp_path)
    page.goto.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(inspect_airchina_page(automation, ROUTE))
    automation._close_context_quietly.assert_awaited_once()


def test_captures_require_explicit_opt_in(tmp_path):
    automation, page = fake_automation(tmp_path)
    path = asyncio.run(inspect_airchina_page(automation, ROUTE, capture_visible_page=True))
    report = json.loads(path.read_text())
    assert (path.parent / "visible-page.txt").read_text() == "选择航班 安全验证"
    page.screenshot.assert_awaited_once()
    assert report["screenshot_path"] == str(path.parent / "page.png")


@pytest.mark.parametrize("kwargs", [{"manual_wait_seconds": -1}, {"manual_wait_seconds": 301}])
def test_invalid_wait_does_not_open_page(tmp_path, kwargs):
    automation, page = fake_automation(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(inspect_airchina_page(automation, ROUTE, **kwargs))
    automation._new_page.assert_not_awaited()


def test_invalid_route_does_not_open_page(tmp_path):
    automation, page = fake_automation(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(inspect_airchina_page(automation, replace(ROUTE, passengers=2)))
    automation._new_page.assert_not_awaited()


def test_existing_inspect_entry_uses_dedicated_probe(tmp_path):
    automation, page = fake_automation(tmp_path)
    path = asyncio.run(BrowserAutomation.inspect_query_page(automation, "airchina", ROUTE, pause=False))
    assert json.loads(path.read_text())["state"] == "needs_verification"
    page.goto.assert_awaited_once()


def test_no_generic_fare_fallback_even_if_invoked_directly():
    provider = SimpleNamespace(name="airchina")
    for coroutine in [
        BrowserAutomation.query_provider(None, "airchina", ROUTE),
        BrowserAutomation._query_provider_without_backend_retry(None, "airchina", ROUTE),
        BrowserAutomation._extract_offer(None, provider, ROUTE, None, []),
    ]:
        with pytest.raises(RuntimeError, match="尚未完成"):
            asyncio.run(coroutine)
    assert not PROVIDERS["airchina"].has_handler
    assert not supports_tail_discovery("airchina")


def test_cli_explicit_and_never_reads_credentials_or_initializes_app(tmp_path):
    import main

    config_path = tmp_path / "config.json"
    example = json.loads((Path(__file__).parents[1] / "config.example.json").read_text())
    example["routes"] = []
    config_path.write_text(json.dumps(example))
    manager = ConfigManager(config_path=config_path)
    argv = ["main.py", "--config", str(config_path), "airchina-probe", "--origin", "PEK",
            "--destination", "SHA", "--date", "2099-01-01"]
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"state": "needs_verification", "message": "manual"}))
    context = AsyncMock()
    with patch("sys.argv", argv), patch("main.ConfigManager", return_value=manager), \
         patch.object(manager, "load_credentials", side_effect=AssertionError("do not read keys")), \
         patch("browser_automation.BrowserAutomation", return_value=context) as factory, \
         patch("airchina_probe.inspect_airchina_page", new=AsyncMock(return_value=report)), \
         patch("main.FlightPriceApplication", side_effect=AssertionError("no application DB")):
        assert main.main() == 2
        factory.assert_called_once()
        assert factory.call_args.args[1] == {}
        assert factory.call_args.args[0].headless is False


@pytest.mark.parametrize("wait", ["-1", "301", "not-a-number"])
def test_cli_rejects_invalid_manual_wait(wait):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["airchina-probe", "--origin", "PEK", "--destination", "SHA",
                                   "--date", "2099-01-01", "--manual-wait-seconds", wait])
