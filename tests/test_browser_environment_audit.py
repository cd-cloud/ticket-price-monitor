import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from browser_environment import PRESETS, parse_browser_environment, BrowserEnvironmentError
from browser_environment_audit import (
    build_report, render_report, write_report, audit_provider, AUDIT_URL, HEADERS, run_audit,
)
from config_manager import ConfigManager
from browser_fingerprint import default_fingerprint


def provider():
    return SimpleNamespace(
        name="airchina", locale="zh-CN", timezone="Asia/Shanghai", extra_headers={},
        browser_environment=parse_browser_environment({"preset": "desktop-cn-v1"}),
    )


def sample():
    return {**deepcopy(PRESETS["desktop-cn-v1"]),
            "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) SecretTestUA",
            "platform": "MacIntel", "ua_platform": "macOS"}


def header():
    return {"user-agent": sample()["user_agent"], "accept-language": "zh-CN,zh;q=0.9",
            "sec-ch-ua-platform": '"macOS"'}


def report(p=None, samples=None, headers=None):
    return build_report(p or provider(), samples or [sample(), sample()],
                        headers or [header(), header()], headless=True, configured_headless=False)


def test_preset_is_fixed_and_matches_explicit_configuration():
    a = parse_browser_environment({"preset": "desktop-cn-v1"})
    b = parse_browser_environment(PRESETS["desktop-cn-v1"])
    assert a == b
    a.context_options()["viewport"]["width"] = 2000
    assert PRESETS["desktop-cn-v1"]["viewport"]["width"] == 1440
    assert "user_agent" not in a.options
    assert parse_browser_environment({"preset": "desktop-gb-v1", "locale": "zh-CN"}).options["locale"] == "zh-CN"


@pytest.mark.parametrize("preset", ["random", "desktop-cn", None, [], True])
def test_unknown_presets_fail_closed(preset):
    with pytest.raises(BrowserEnvironmentError):
        parse_browser_environment({"preset": preset})


def test_preset_locale_wins_over_inherited_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"providers": {"airchina": {
        "locale": "en-US", "timezone": "UTC", "browser_environment": {"preset": "desktop-cn-v1"}
    }}}))
    p = ConfigManager(path).load().providers["airchina"]
    assert p.browser_environment.options["locale"] == "zh-CN"
    assert p.browser_environment.options["timezone_id"] == "Asia/Shanghai"
    assert parse_browser_environment({"preset": "desktop-cn-v1",
                                      "fingerprint": default_fingerprint()}) == p.browser_environment


def test_report_agreement_is_not_fingerprint_or_network_success():
    r = report()
    assert r["result"] == "no_observed_differences"
    assert r["launch_mode_matches_config"] is False
    assert not r["proxy_applied"] and not r["live_site_queried"]
    assert "SecretTestUA" not in json.dumps(r)
    assert "SecretTestUA" not in render_report(r)
    assert "非" in render_report(r) or "不是" in render_report(r)


@pytest.mark.parametrize("field,changed", [
    ("viewport", {"width": 1200, "height": 800}),
    ("locale", "en-US"), ("timezone_id", "UTC"), ("color_scheme", "dark"),
    ("device_scale_factor", 2), ("platform", "Win32"),
    ("has_touch", True),
])
def test_mismatches_are_visible(field, changed):
    s = sample()
    s[field] = changed
    assert report(samples=[s, s])["result"] == "differences_found"


def test_second_load_header_mismatch_and_unavailable_signals():
    h = header()
    h["user-agent"] = "SECRET_OVERRIDE"
    r = report(headers=[header(), h])
    assert r["result"] == "differences_found"
    assert "SECRET_OVERRIDE" not in json.dumps(r)
    r = report(headers=[{}, {}])
    assert next(x for x in r["rows"] if x["name"] == "请求 UA 与 JS UA")["status"] == "unavailable"
    s = sample()
    s["ua_platform"] = None
    r = report(samples=[s, s])
    assert next(x for x in r["rows"] if x["name"] == "平台类别一致性")["status"] == "unavailable"


def test_custom_ua_compared_without_persisting_raw_and_stability():
    p = provider()
    p.browser_environment = parse_browser_environment({"user_agent": "SECRET_CONFIG_UA"})
    r = report(p=p, samples=[sample(), {**sample(), "color_scheme": "dark"}])
    assert r["result"] == "differences_found"
    assert "SECRET_CONFIG_UA" not in json.dumps(r)
    assert next(x for x in r["rows"] if x["name"] == "同一上下文两次采样")["status"] == "mismatch"


def test_report_html_escapes_and_unique_local_files(tmp_path):
    r = report()
    r["rows"][0]["note"] = '<script>alert("secret")</script>'
    assert '<script>' not in render_report(r)
    a, b = write_report(r, tmp_path), write_report(r, tmp_path)
    assert a != b
    assert json.loads((a / "report.json").read_text()) == r
    assert (a / "report.json").stat().st_mode & 0o777 == 0o600


def test_audit_uses_temp_profile_strips_proxy_and_sensitive_headers(monkeypatch):
    p = provider()
    monkeypatch.setenv("AUDIT_PROXY", "http://private.example:8080")
    p.browser_environment = parse_browser_environment({
        "preset": "desktop-cn-v1", "network": {"mode": "proxy", "server_env": "AUDIT_PROXY"}})
    p.extra_headers = {"Authorization": "SECRET", "Cookie": "SECRET",
                       "X-Private": "SECRET", "User-Agent": "SyntheticTest"}
    backend = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), new_context=AsyncMock())
    context = SimpleNamespace(route=AsyncMock())
    backend.new_context.return_value = context
    page = SimpleNamespace(evaluate=AsyncMock(side_effect=[sample(), sample()]))
    backend.new_page = AsyncMock(return_value=page)
    routes = []
    async def goto(url, **_):
        request = SimpleNamespace(url=url, header_value=AsyncMock(side_effect=lambda key: header().get(key)))
        route = SimpleNamespace(request=request, abort=AsyncMock(), fulfill=AsyncMock())
        routes.append(route)
        await context.route.await_args.args[1](route)
        # Unexpected requests must abort, without reading headers.
        unwanted = SimpleNamespace(request=SimpleNamespace(url="https://www.airchina.com.cn/"),
                                   abort=AsyncMock())
        await context.route.await_args.args[1](unwanted)
        unwanted.abort.assert_awaited_once()
    page.goto = AsyncMock(side_effect=goto)
    monkeypatch.setattr("browser_environment_audit.SystemChromeBackend", lambda: backend)
    r = asyncio.run(audit_provider(p, headless=True))
    assert r["environment"]["network"] == "proxy" and not r["proxy_applied"]
    opts = backend.new_context.await_args.kwargs
    assert "proxy" not in opts["browser_environment_options"]
    assert opts["extra_http_headers"] == {"User-Agent": "SyntheticTest"}
    assert len(routes) == 2
    for route in routes:
        assert set(c.args[0] for c in route.request.header_value.await_args_list) == set(HEADERS)
        route.fulfill.assert_awaited_once()
    backend.stop.assert_awaited_once()
    assert not __import__("pathlib").Path(backend.start.await_args.kwargs["user_data_dir"]).exists()
    assert "SECRET" not in json.dumps(r) and "private.example" not in json.dumps(r)


def test_failure_cleanup_and_sanitized_output(monkeypatch, tmp_path, capsys):
    backend = SimpleNamespace(start=AsyncMock(side_effect=RuntimeError("SECRET")), stop=AsyncMock())
    monkeypatch.setattr("browser_environment_audit.SystemChromeBackend", lambda: backend)
    cfg = SimpleNamespace(providers={"airchina": provider()}, runtime_dir=tmp_path, headless=False)
    assert asyncio.run(run_audit(cfg)) == 2
    assert "SECRET" not in capsys.readouterr().out
    backend.stop.assert_awaited_once()
    assert not list(tmp_path.iterdir())


def test_cli_does_not_start_application(monkeypatch, tmp_path):
    import main
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"providers": {"airchina": {}}}))
    monkeypatch.setattr("sys.argv", ["main.py", "--config", str(path), "airchina-environment-audit", "--headless"])
    runner = AsyncMock(return_value=0)
    monkeypatch.setattr("browser_environment_audit.run_audit", runner)
    monkeypatch.setattr(main, "FlightPriceApplication", Mock(side_effect=AssertionError("no live service")))
    assert main.main() == 0
    assert runner.await_args.kwargs["headless"] is True


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_touch_setting_requires_boolean(value):
    with pytest.raises(BrowserEnvironmentError):
        parse_browser_environment({"has_touch": value})
