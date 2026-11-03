import asyncio
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backends.chrome import SystemChromeBackend
from browser_environment import (
    BrowserEnvironmentError, environment_summary, launch_options, parse_browser_environment,
)
from browser_fingerprint import (
    SCRIPT, SCRIPT_DIGEST, fingerprint_script, validate_fingerprint, default_fingerprint,
)
from browser_environment_audit import build_report, render_report
from config_manager import ConfigManager


FIXED = {"mode": "fixed-v1", "hardware_concurrency": 8, "device_memory": 8,
         "webgl": {"vendor": "PRIVATE_VENDOR", "renderer": "PRIVATE_RENDERER"}}


@pytest.mark.parametrize("settings,expected", [
    ({}, default_fingerprint()),
    ({"browser_environment": {}}, default_fingerprint()),
    ({"browser_environment": {"preset": "desktop-cn-v1"}}, default_fingerprint()),
    ({"browser_environment": None}, None),
    ({"browser_environment": {"fingerprint": None}}, None),
    ({"browser_environment": {"fingerprint": {"mode": "fixed-v1", "device_memory": 4}}},
     {"mode": "fixed-v1", "device_memory": 4}),
])
def test_airchina_defaults_and_explicit_opt_out(tmp_path, settings, expected):
    path = tmp_path / "config.json"
    raw = {"providers": {"airchina": settings, "ctrip": {}, "feizhu": {}}}
    path.write_text(json.dumps(raw))
    config = ConfigManager(path).load()
    env = config.providers["airchina"].browser_environment
    assert (env.options.get("fingerprint") if env else None) == expected
    assert config.providers["ctrip"].browser_environment is None
    assert config.providers["feizhu"].browser_environment is None
    assert json.loads(path.read_text()) == raw  # Loading never rewrites local files.
    if expected:
        assert "webgl" not in expected


def test_defaults_are_fresh_and_example_enables_same_values():
    from pathlib import Path
    first = default_fingerprint()
    first["device_memory"] = 1
    assert default_fingerprint()["device_memory"] == 8
    example = json.loads((Path(__file__).resolve().parents[1] / "config.example.json").read_text())
    provider = example["providers"]["airchina"]
    assert provider["browser_environment"]["fingerprint"] == default_fingerprint()
    assert provider["enabled"] is False  # Does not enable production flight queries.


@pytest.mark.parametrize("raw", [
    {}, [], False, {"mode": "random", "hardware_concurrency": 8}, {"mode": "fixed-v1"},
    {"mode": "fixed-v1", "script": "SECRET"},
    *({"mode": "fixed-v1", "hardware_concurrency": v} for v in [True, 0, 65, 8.0, "8", None]),
    *({"mode": "fixed-v1", "device_memory": v} for v in [True, 0, 16, "8", float("nan"), None]),
    *({"mode": "fixed-v1", "webgl": v} for v in [
        None, [], {}, {"vendor": "x"}, {"vendor": "x", "renderer": ""},
        {"vendor": "x", "renderer": "\x00SECRET"},
        {"vendor": "x", "renderer": "a" * 257},
        {"vendor": "x", "renderer": 8}, {"vendor": "x", "renderer": "y", "extra": 1},
    ]),
])
def test_validation_fail_closed_without_config_leak(raw):
    with pytest.raises(BrowserEnvironmentError) as exc:
        parse_browser_environment({"fingerprint": raw})
    assert "SECRET" not in str(exc.value)


def test_copy_serialization_and_opt_out():
    raw = deepcopy(FIXED)
    raw["webgl"]["renderer"] = '"); globalThis.INJECTED=true; // \u2028 中文 </script>'
    validated = validate_fingerprint(raw)
    assert validated == raw and validated is not raw
    script = fingerprint_script(raw)
    assert json.loads(script[len("(" + SCRIPT + ")("):-2]) == raw
    raw["webgl"]["vendor"] = "changed"
    assert validated["webgl"]["vendor"] == FIXED["webgl"]["vendor"]
    assert fingerprint_script(None) is None
    assert SCRIPT_DIGEST == hashlib.sha256(SCRIPT.encode()).hexdigest()[:16]
    a = parse_browser_environment({})
    assert a == parse_browser_environment({"fingerprint": None})
    assert a.key == hashlib.sha256(json.dumps(a.options, sort_keys=True).encode()).hexdigest()[:24]


def test_fixed_identity_stable_and_script_version_is_namespaced(monkeypatch):
    a = parse_browser_environment({"fingerprint": FIXED})
    assert a == parse_browser_environment({"fingerprint": deepcopy(FIXED)})
    assert a.key != parse_browser_environment({}).key
    assert a.key != parse_browser_environment({"fingerprint": {**FIXED, "device_memory": 4}}).key
    monkeypatch.setattr("browser_environment.SCRIPT_DIGEST", "next-version")
    assert a.key != parse_browser_environment({"fingerprint": FIXED}).key
    summary = environment_summary(SimpleNamespace(browser_environment=a))
    assert summary["fingerprint"]["enabled"]
    assert "PRIVATE_" not in json.dumps(summary)
    opts = {"fingerprint": FIXED, "viewport": {"width": 1440, "height": 900}}
    assert launch_options({"browser_environment_options": opts}) == {"viewport": opts["viewport"]}
    assert opts["fingerprint"] == FIXED


def backend_fixture(tmp_path, *, install_error=None, close_error=None):
    old_page = SimpleNamespace(close=AsyncMock())
    new_page = SimpleNamespace(close=AsyncMock())
    context = SimpleNamespace(
        add_init_script=AsyncMock(side_effect=install_error),
        close=AsyncMock(side_effect=close_error), pages=[old_page],
        new_page=AsyncMock(return_value=new_page),
    )
    backend = SystemChromeBackend()
    launch = AsyncMock(return_value=context)
    backend._playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=launch), stop=AsyncMock())
    backend._chrome_path = "/test/chrome"
    backend._profile_root = tmp_path / "profiles"
    return backend, context, launch, old_page, new_page


def test_installed_before_page_and_not_forwarded_to_playwright(tmp_path):
    async def run():
        backend, context, launch, old, new = backend_fixture(tmp_path)
        async def create_page():
            context.add_init_script.assert_awaited_once()
            return new
        context.new_page.side_effect = create_page
        assert await backend.new_context(browser_environment_options={"fingerprint": FIXED}) is context
        assert "fingerprint" not in launch.await_args.kwargs
        assert context.add_init_script.await_args.kwargs["script"] == fingerprint_script(FIXED)
        assert await backend.new_page(context) is new
        old.close.assert_awaited_once()
        assert not backend._fingerprint_new_pages
        await backend.close_context(context)
        assert not backend._contexts
        assert not backend._profile_locks["default"].locked()
    asyncio.run(run())


def test_disabled_preserves_default_page(tmp_path):
    async def run():
        backend, context, _, old, _ = backend_fixture(tmp_path)
        await backend.new_context()
        context.add_init_script.assert_not_awaited()
        assert await backend.new_page(context) is old
        context.new_page.assert_not_awaited()
        old.close.assert_not_awaited()
        await backend.stop()
    asyncio.run(run())


@pytest.mark.parametrize("error", [RuntimeError("install"), asyncio.CancelledError()])
@pytest.mark.parametrize("close_fails", [False, True])
def test_failed_install_never_returns_unpatched_context(tmp_path, error, close_fails):
    async def run():
        backend, context, _, _, _ = backend_fixture(
            tmp_path, install_error=error,
            close_error=RuntimeError("close") if close_fails else None)
        with pytest.raises((RuntimeError, asyncio.CancelledError)):
            await backend.new_context(browser_environment_options={"fingerprint": FIXED})
        context.close.assert_awaited_once()
        context.new_page.assert_not_awaited()
        assert backend._profile_locks["default"].locked() == close_fails
        assert bool(backend._contexts) == close_fails
        context.close.side_effect = None
        await backend.stop()
        assert not backend._contexts and not backend._profile_locks
    asyncio.run(run())


@pytest.mark.parametrize("value,status", [(True, "pass"), (False, "mismatch"), (None, "unavailable")])
def test_audit_fingerprint_matches_and_redacts(value, status):
    p = SimpleNamespace(locale="zh-CN", timezone="Asia/Shanghai",
        browser_environment=parse_browser_environment({"fingerprint": FIXED}))
    s = {"locale": "zh-CN", "timezone_id": "Asia/Shanghai",
         "fingerprint": {
             "hardware_concurrency": 8, "device_memory": 8,
             "webgl": {kind: None if value is None else
                       {"vendor_matches": value, "renderer_matches": value}
                       for kind in ("webgl", "webgl2")},
         }}
    r = build_report(p, [s, deepcopy(s)], [{}, {}], headless=True, configured_headless=False)
    assert next(row for row in r["rows"] if row["name"] == "固定 WEBGL 标识")["status"] == status
    assert next(row for row in r["rows"] if row["name"] == "固定 CPU 核数")["status"] == "pass"
    assert "PRIVATE_" not in json.dumps(r) + render_report(r)
