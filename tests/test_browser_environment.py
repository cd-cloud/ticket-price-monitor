import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from airchina_query_gate import AirChinaQueryGate
from backends.chrome import SystemChromeBackend
from browser_environment import (
    BrowserEnvironmentError, environment_key, environment_summary, parse_browser_environment,
)
from browser_session_manager import AirChinaSessionUnavailable, BrowserSessionManager
from config_manager import ConfigManager, RouteQuery
from experimental_browser_session import ExperimentalBrowserSession
from local_session_state_store import LocalSessionStateStore, SessionStateError


def config(tmp_path, environment=None):
    raw = {"storage": {"runtime_dir": "./runtime"}, "defaults": {
        "browser_backend": "chrome", "browser_backend_fallbacks": []},
        "providers": {"airchina": {"browser_environment": environment,
            "extra_headers": {"User-Agent": "old", "Accept-Language": "en-US",
                              "Sec-CH-UA": "old", "X-Test": "ok"}}}, "routes": []}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    return ConfigManager(path)


def test_default_preserves_headers_profile_and_no_options(tmp_path):
    c = config(tmp_path).load()
    p = c.providers["airchina"]
    assert p.browser_environment is None
    assert p.extra_headers["User-Agent"] == "old"
    assert BrowserSessionManager.profile_name_for_backend(p, "chrome") == "airchina-v2"
    assert environment_summary(p)["configured"] is False


@pytest.mark.parametrize("raw", [
    [], False, {"args": ["--proxy-server=x"]}, {"viewport": {"width": True, "height": 900}},
    {"viewport": {"width": 100, "height": 900}}, {"device_scale_factor": float("nan")},
    {"screen": {"width": 1280, "height": 900}},
    {"screen": {"width": 1000, "height": 900}, "viewport": {"width": 1280, "height": 900}},
    {"timezone_id": "bad/timezone"}, {"locale": "../private"}, {"user_agent": "SECRET\r\n"},
    {"network": {"mode": "auto"}}, {"network": {"mode": "system", "server_env": "P"}},
    {"network": {"mode": "proxy", "server_env": "MISSING_ENV_PROXY"}},
])
def test_invalid_environment_fails_without_echoing_input(raw):
    with pytest.raises(BrowserEnvironmentError) as exc:
        parse_browser_environment(raw)
    assert "SECRET" not in str(exc.value)


@pytest.mark.parametrize("server", [
    "http://user:SECRET@localhost:8080", "file:///tmp/SECRET", "http://host",
    "http://host:0", "http://host:65536", "http://host:abc",
    "http://host:8080/path", "http://host:8080/?token=SECRET", "http://host:8080/#SECRET",
])
def test_invalid_proxy_never_leaks_server(monkeypatch, server):
    monkeypatch.setenv("TEST_PROXY", server)
    with pytest.raises(BrowserEnvironmentError) as exc:
        parse_browser_environment({"network": {"mode": "proxy", "server_env": "TEST_PROXY"}})
    assert "SECRET" not in str(exc.value)


def test_proxy_credentials_resolved_only_in_memory_and_summary_is_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PROXY", "http://localhost:8081")
    monkeypatch.setenv("TEST_PROXY_USER", "secret-user")
    monkeypatch.setenv("TEST_PROXY_PASS", "secret-pass")
    raw = {"locale": "zh-CN", "timezone_id": "Asia/Shanghai",
           "network": {"mode": "proxy", "server_env": "TEST_PROXY",
                       "username_env": "TEST_PROXY_USER", "password_env": "TEST_PROXY_PASS"}}
    manager = config(tmp_path, raw)
    provider = manager.load().providers["airchina"]
    assert provider.extra_headers == {"X-Test": "ok"}
    options = provider.browser_environment.context_options()
    assert options["proxy"]["password"] == "secret-pass"
    assert "secret-pass" not in repr(provider)
    assert "secret-user" not in json.dumps(environment_summary(provider))
    assert "localhost" not in json.dumps(environment_summary(provider))
    assert "secret-pass" not in manager.config_path.read_text()
    old_key = environment_key(provider)
    monkeypatch.setenv("TEST_PROXY_PASS", "changed")
    assert environment_key(ConfigManager(manager.config_path).load().providers["airchina"]) != old_key


def test_profile_retained_session_and_backup_are_isolated(tmp_path):
    manager = config(tmp_path)
    c1 = manager.load()
    s1 = LocalSessionStateStore(manager)
    manager = config(tmp_path, {"viewport": {"width": 1440, "height": 900}})
    c2 = manager.load()
    p1, p2 = c1.providers["airchina"], c2.providers["airchina"]
    assert BrowserSessionManager.profile_name_for_backend(p1, "chrome") != \
        BrowserSessionManager.profile_name_for_backend(p2, "chrome")
    assert ExperimentalBrowserSession._key(c1, p1) != ExperimentalBrowserSession._key(c2, p2)
    s2 = LocalSessionStateStore(manager)
    assert s1.path != s2.path
    s2.path.parent.mkdir(parents=True, exist_ok=True)
    s2.path.write_text(json.dumps({"version": 1, "provider": "airchina", "saved_at": "x",
                                 "ciphertext": "x", "environment_key": "wrong"}))
    with pytest.raises(SessionStateError):
        s2._read_envelope()


def test_environment_changes_cache_key_not_global_pause(tmp_path):
    route = RouteQuery("PEK", "SHA", "2026-11-10")
    kw = dict(flight_no=None, entry_mode="form", session_mode="persistent", manual=False, notices=True)
    k1 = AirChinaQueryGate.key(route, environment="one", **kw)
    k2 = AirChinaQueryGate.key(route, environment="two", **kw)
    assert k1 != k2
    gate = AirChinaQueryGate(tmp_path)
    gate.pause("needs_verification")
    execute = AsyncMock()
    result = asyncio.run(gate.run(k2, execute, cancel_event=threading.Event(), progress=lambda _: None))
    assert result["state"] == "provider_paused"
    execute.assert_not_awaited()


def test_actual_launch_options_and_no_fallback_on_proxy_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PROXY", "http://localhost:18081")
    manager = config(tmp_path, {"viewport": {"width": 1440, "height": 900},
                               "locale": "zh-CN", "user_agent": "TestBrowser/1",
                               "network": {"mode": "proxy", "server_env": "TEST_PROXY"}})
    c = manager.load()
    p = c.providers["airchina"]
    backend = SystemChromeBackend()
    launch = AsyncMock(side_effect=RuntimeError("SECRET-proxy-url-and-password"))
    backend._playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    backend._profile_root = tmp_path / "profiles"
    backend._chrome_path = "/test/chrome"
    sm = BrowserSessionManager(c)
    sm._backend_manager = SimpleNamespace(ensure_backend=AsyncMock(return_value=backend))
    with pytest.raises(AirChinaSessionUnavailable) as exc:
        asyncio.run(sm.new_page(p, backend_name="chrome"))
    assert "SECRET" not in str(exc.value)
    launch.assert_awaited_once()
    opts = launch.await_args.kwargs
    assert opts["viewport"] == {"width": 1440, "height": 900}
    assert opts["user_agent"] == "TestBrowser/1" and opts["locale"] == "zh-CN"
    assert opts["extra_http_headers"] == {"X-Test": "ok"}
    assert opts["proxy"] == {"server": "http://localhost:18081"}
    assert all(not lock.locked() for lock in backend._profile_locks.values())
    sm._backend_manager.ensure_backend.assert_awaited_once()


def test_unsupported_backend_rejected_without_silent_direct_fallback(tmp_path):
    c = config(tmp_path, {}).load()
    sm = BrowserSessionManager(c)
    sm._backend_manager = SimpleNamespace(ensure_backend=AsyncMock())
    with pytest.raises(AirChinaSessionUnavailable):
        asyncio.run(sm.new_page(c.providers["airchina"], backend_name="playwright"))
    sm._backend_manager.ensure_backend.assert_not_awaited()


def test_preparation_cannot_restore_backup_from_old_environment(tmp_path):
    from experimental_session_service import ExperimentalSessionService
    c = config(tmp_path, {"locale": "zh-CN"}).load()
    service = ExperimentalSessionService.__new__(ExperimentalSessionService)
    service.query_service = SimpleNamespace(get_config=lambda: c)
    service.store = SimpleNamespace(environment_key="", load=Mock())
    service.timeout_seconds = 1
    pool = SimpleNamespace(lease=Mock())
    with pytest.raises(SessionStateError, match="environment_changed_restart_required"):
        asyncio.run(service._prepare(pool, threading.Event(), threading.Event(), True))
    pool.lease.assert_not_called()
    service.store.load.assert_not_called()


def test_check_cli_does_not_print_proxy_or_launch_browser(tmp_path, monkeypatch, capsys):
    import main
    monkeypatch.setenv("TEST_PROXY", "http://private-proxy.example:8080")
    manager = config(tmp_path, {"network": {"mode": "proxy", "server_env": "TEST_PROXY"}})
    monkeypatch.setattr("sys.argv", ["main.py", "--config", str(manager.config_path),
                                    "airchina-environment-check"])
    app = Mock(side_effect=AssertionError("must not start app"))
    monkeypatch.setattr(main, "FlightPriceApplication", app)
    assert main.main() == 0
    output = capsys.readouterr().out
    assert json.loads(output)["network"] == "proxy"
    assert "private-proxy" not in output
    app.assert_not_called()
