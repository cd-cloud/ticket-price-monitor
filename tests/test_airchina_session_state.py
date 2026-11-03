import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config_manager import ConfigManager
from local_session_state_store import (
    LocalSessionStateStore,
    SessionStateError,
    restore_session_state,
)


def make_store(tmp_path, monkeypatch, key="test-master-key"):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "storage": {"runtime_dir": "./runtime"},
        "providers": {},
        "routes": [],
    }))
    monkeypatch.setenv("FLIGHT_TRACKER_MASTER_KEY", key)
    return LocalSessionStateStore(ConfigManager(path))


def state():
    return {
        "cookies": [
            {"name": "session", "value": "dummy-session-secret", "domain": ".airchina.com.cn", "path": "/"},
            {"name": "third", "value": "dummy-third-party", "domain": ".example.test", "path": "/"},
        ],
        "origins": [
            {"origin": "https://www.airchina.com.cn", "localStorage": [
                {"name": "login", "value": "dummy-local-secret"},
            ]},
            {"origin": "https://example.test", "localStorage": [
                {"name": "third", "value": "dummy-third-party"},
            ]},
        ],
    }


def test_encrypted_backup_round_trip_filters_domains_and_hides_plaintext(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    saved = store.save(state())
    assert saved["available"] and saved["saved_at"]
    raw = store.path.read_text()
    assert "dummy-session-secret" not in raw
    assert "dummy-local-secret" not in raw
    assert "dummy-third-party" not in raw
    if os.name != "nt":
        assert store.path.stat().st_mode & 0o777 == 0o600

    loaded = store.load()
    assert len(loaded["cookies"]) == 1
    assert loaded["cookies"][0]["value"] == "dummy-session-secret"
    assert len(loaded["origins"]) == 1
    context = SimpleNamespace(add_cookies=AsyncMock(), add_init_script=AsyncMock())
    counts = asyncio.run(restore_session_state(context, loaded))
    assert counts == {"cookies": 1, "origins": 1}
    context.add_cookies.assert_awaited_once_with(loaded["cookies"])
    script = context.add_init_script.await_args.kwargs["script"]
    assert "https://www.airchina.com.cn" in script
    assert "dummy-local-secret" in script
    assert store.status()["available"]
    assert store.delete()["available"] is False
    assert not store.path.exists()


def test_missing_key_never_writes_plaintext(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    monkeypatch.delenv("FLIGHT_TRACKER_MASTER_KEY")
    with pytest.raises(SessionStateError, match="encryption_unavailable"):
        store.save(state())
    assert not store.path.exists()
    assert store.status()["encryption_available"] is False


def test_wrong_key_and_corruption_fail_closed(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    store.save(state())
    monkeypatch.setenv("FLIGHT_TRACKER_MASTER_KEY", "different-key")
    with pytest.raises(SessionStateError, match="invalid_backup"):
        store.load()
    store.path.write_text("not-json")
    with pytest.raises(SessionStateError, match="invalid_backup"):
        store.load()
    assert store.status()["error_code"] == "invalid_backup"


def test_restore_rejects_cross_origin_before_browser_write(tmp_path, monkeypatch):
    make_store(tmp_path, monkeypatch)
    context = SimpleNamespace(add_cookies=AsyncMock(), add_init_script=AsyncMock())
    with pytest.raises(SessionStateError, match="invalid_origin"):
        asyncio.run(restore_session_state(context, {
            "cookies": [{"name": "session", "value": "dummy", "domain": ".example.test"}],
            "origins": [],
        }))
    context.add_cookies.assert_not_awaited()
    context.add_init_script.assert_not_awaited()
