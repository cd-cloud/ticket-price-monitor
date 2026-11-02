"""Manual preparation uses the same project profile, never a personal browser."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from browser_automation import BrowserAutomation
from browser_profile_service import BrowserProfileService
from browser_session_manager import BrowserSessionManager, AirChinaSessionUnavailable
from browser_session_policy import BrowserSessionMetadata
from config_manager import ConfigManager
from test_experimental_queries import config_file


@pytest.mark.parametrize("profile", ["airchina-v2", "airchina-v2-recovery-2"])
def test_manual_open_and_next_query_select_same_airchina_profile(tmp_path, monkeypatch, profile):
    config = replace(ConfigManager(config_file(tmp_path)).load(), browser_backend="chrome",
                     browser_backend_fallbacks=[])
    provider = config.providers["airchina"]
    assert not provider.enabled
    manager = BrowserSessionManager(config)
    manager.set_chrome_profile_preference("airchina", forced_profile=profile)
    service = BrowserProfileService(config=config, repository=SimpleNamespace(
        fetch_tail_discovery_attempts=Mock(return_value=[]), fetch_snapshots=Mock(return_value=[])))
    popen = Mock()
    monkeypatch.setattr("backends.chrome._find_system_chrome", lambda: "/mock/chrome")
    monkeypatch.setattr("browser_profile_service.subprocess.Popen", popen)
    rows = service.browser_profiles()["providers"]
    assert any(row["provider"] == "airchina" for row in rows)
    opened = service.open_browser_profile("airchina")
    expected = manager.persistent_profile_path_for_backend(provider, "chrome", profile)
    assert f"--user-data-dir={expected}" in popen.call_args.args[0]
    assert popen.call_args.args[0][-1] == provider.login_url
    assert opened["profile_name"] == profile
    backend = SimpleNamespace(new_context=AsyncMock(return_value=object()), new_page=AsyncMock(return_value=object()))
    manager._backend_manager = SimpleNamespace(ensure_backend=AsyncMock(return_value=backend))
    asyncio.run(manager.new_page(provider, backend_name="chrome"))
    assert backend.new_context.call_args.kwargs["profile_name"] == profile
    assert backend.new_context.call_args.kwargs["storage_state"] is None
    assert not provider.enabled  # Preparation did not enable production querying.


@pytest.mark.parametrize("backend,kind", [("chrome", "persistent_profile"), ("playwright", "storage_state")])
def test_manual_bootstrap_keeps_existing_backend_save_policy(tmp_path, backend, kind):
    config = ConfigManager(config_file(tmp_path)).load()
    provider = config.providers["airchina"]
    manager = BrowserSessionManager(config)
    page = SimpleNamespace(goto=AsyncMock(), wait_for_timeout=AsyncMock())
    context = SimpleNamespace(storage_state=AsyncMock())
    manager._context_session_metadata[id(context)] = BrowserSessionMetadata(
        browser_backend=backend, storage_state_loaded=False, credential_login_allowed=False,
        persistent_profile_loaded=backend == "chrome")
    manager._context_profile_names[id(context)] = "airchina-v2" if backend == "chrome" else "airchina"
    automation = BrowserAutomation.__new__(BrowserAutomation)
    automation.app_config, automation.session_manager = config, manager
    automation._new_page = AsyncMock(return_value=(page, context))
    automation._close_context_quietly = AsyncMock()
    automation.login = AsyncMock(side_effect=AssertionError("manual means no credential submission"))
    artifact = asyncio.run(automation.bootstrap_session("airchina", manual=True))
    assert artifact.kind == kind
    page.goto.assert_awaited_once_with(provider.login_url, wait_until="domcontentloaded", timeout=provider.timeout_ms)
    page.wait_for_timeout.assert_awaited_once_with(180000)
    automation.login.assert_not_awaited()
    if backend == "chrome":
        context.storage_state.assert_not_awaited()
    else:
        context.storage_state.assert_awaited_once_with(path=str(provider.storage_state_file))
    automation._close_context_quietly.assert_awaited_once_with(context)


@pytest.mark.parametrize("fault", ["socket_lock", "dangling_lock", "invalid_preferences", "quarantined"])
def test_unavailable_manual_profile_never_selects_recovery_or_fallback(tmp_path, fault):
    config = replace(ConfigManager(config_file(tmp_path)).load(), browser_backend="chrome",
                     browser_backend_fallbacks=["playwright"])
    provider = config.providers["airchina"]
    manager = BrowserSessionManager(config)
    path = manager.persistent_profile_path_for_backend(provider, "chrome", "airchina-v2")
    path.mkdir(parents=True)
    if fault == "socket_lock":
        (path / "SingletonSocket").write_text("owned")
    elif fault == "dangling_lock":
        (path / "SingletonLock").symlink_to(path / "nonexistent-owner")
    elif fault == "invalid_preferences":
        (path / "Default").mkdir()
        (path / "Default" / "Preferences").write_text("malformed")
    else:
        manager._write_profile_state({"airchina": {"chrome": {"polluted_profiles": {"airchina-v2": {}}}}})
    backend = SimpleNamespace(ensure_backend=AsyncMock())
    manager._backend_manager = backend
    assert manager.selected_chrome_profile_name(provider, "chrome") == "airchina-v2"
    with pytest.raises(AirChinaSessionUnavailable, match="不会自动切换"):
        asyncio.run(manager.new_page(provider))
    backend.ensure_backend.assert_not_awaited()
    assert not (path.parent / "airchina-v2-recovery").exists()
    if fault == "socket_lock":
        assert (path / "SingletonSocket").read_text() == "owned"
    elif fault == "dangling_lock":
        assert (path / "SingletonLock").is_symlink()


def test_backend_launch_error_never_falls_through_after_profile_precheck(tmp_path):
    config = replace(ConfigManager(config_file(tmp_path)).load(), browser_backend="chrome",
                     browser_backend_fallbacks=["playwright"])
    manager = BrowserSessionManager(config)
    backend = SimpleNamespace(new_context=AsyncMock(side_effect=RuntimeError("SECRET_BACKEND_DETAIL")))
    manager._backend_manager = SimpleNamespace(ensure_backend=AsyncMock(return_value=backend))
    with pytest.raises(AirChinaSessionUnavailable) as caught:
        asyncio.run(manager.new_page(config.providers["airchina"]))
    manager._backend_manager.ensure_backend.assert_awaited_once_with("chrome")
    assert "SECRET" not in str(caught.value)
