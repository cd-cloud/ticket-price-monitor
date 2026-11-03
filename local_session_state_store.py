from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


class SessionStateError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _airchina_host(hostname: str | None) -> bool:
    host = str(hostname or "").strip().lower().rstrip(".")
    return host == "airchina.com.cn" or host.endswith(".airchina.com.cn")


def _https_airchina_origin(value) -> bool:
    try:
        parsed = urlsplit(str(value or ""))
    except (TypeError, ValueError):
        return False
    return parsed.scheme == "https" and _airchina_host(parsed.hostname) and not parsed.username and not parsed.password


def _normalize_state(state, *, strict: bool) -> dict:
    if not isinstance(state, dict):
        raise SessionStateError("invalid_backup")
    cookies = state.get("cookies") or []
    origins = state.get("origins") or []
    if not isinstance(cookies, list) or not isinstance(origins, list):
        raise SessionStateError("invalid_backup")

    allowed_cookies = []
    for cookie in cookies:
        if not isinstance(cookie, dict) or not isinstance(cookie.get("name"), str) \
                or not isinstance(cookie.get("value"), str) or not _airchina_host(cookie.get("domain")):
            if strict:
                raise SessionStateError("invalid_origin")
            continue
        allowed_cookies.append(dict(cookie))

    allowed_origins = []
    for entry in origins:
        origin = entry.get("origin") if isinstance(entry, dict) else None
        items = entry.get("localStorage") if isinstance(entry, dict) else None
        if not _https_airchina_origin(origin) or not isinstance(items, list):
            if strict:
                raise SessionStateError("invalid_origin")
            continue
        normalized_items = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) \
                    or not isinstance(item.get("value"), str):
                raise SessionStateError("invalid_backup")
            normalized_items.append({"name": item["name"], "value": item["value"]})
        allowed_origins.append({"origin": origin, "localStorage": normalized_items})
    return {"cookies": allowed_cookies, "origins": allowed_origins}


class LocalSessionStateStore:
    def __init__(self, config_manager, provider: str = "airchina"):
        self.config_manager = config_manager
        self.provider = provider
        from browser_environment import environment_key
        config = config_manager.load()
        self.environment_key = environment_key(getattr(config, "providers", {}).get(provider))
        suffix = f"-env-{self.environment_key}" if self.environment_key else ""
        self.path = config.runtime_dir / "browser_sessions" / f"{provider}{suffix}.enc.json"

    def status(self) -> dict:
        status = {
            "available": self.path.exists(),
            "saved_at": None,
            "encryption_available": bool(os.getenv("FLIGHT_TRACKER_MASTER_KEY")),
            "error_code": None,
        }
        if not status["available"]:
            return status
        try:
            envelope = self._read_envelope()
            status["saved_at"] = envelope["saved_at"]
        except SessionStateError as exc:
            status["error_code"] = exc.code
        return status

    def save(self, state) -> dict:
        if not os.getenv("FLIGHT_TRACKER_MASTER_KEY"):
            raise SessionStateError("encryption_unavailable")
        normalized = _normalize_state(state, strict=False)
        saved_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        plaintext = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        token = self.config_manager._build_fernet(self.config_manager.load()).encrypt(plaintext)
        envelope = {
            "version": 1,
            "provider": self.provider,
            "saved_at": saved_at,
            "ciphertext": token.decode("ascii"),
        }
        if self.environment_key:
            envelope["environment_key"] = self.environment_key
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.provider}-", suffix=".tmp", dir=self.path.parent)
        try:
            try:
                os.fchmod(descriptor, 0o600)
            except (AttributeError, OSError):
                pass
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(envelope, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {"available": True, "saved_at": saved_at, "error_code": None}

    def load(self) -> dict:
        if not os.getenv("FLIGHT_TRACKER_MASTER_KEY"):
            raise SessionStateError("encryption_unavailable")
        envelope = self._read_envelope()
        try:
            plaintext = self.config_manager._build_fernet(self.config_manager.load()).decrypt(
                envelope["ciphertext"].encode("ascii")
            )
            state = json.loads(plaintext.decode("utf-8"))
        except Exception:
            raise SessionStateError("invalid_backup") from None
        return _normalize_state(state, strict=True)

    def delete(self) -> dict:
        self.path.unlink(missing_ok=True)
        return {"available": False, "saved_at": None, "error_code": None}

    def _read_envelope(self) -> dict:
        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
            if envelope.get("version") != 1:
                raise SessionStateError("invalid_version")
            if envelope.get("provider") != self.provider \
                    or envelope.get("environment_key", "") != self.environment_key \
                    or not isinstance(envelope.get("saved_at"), str) \
                    or not isinstance(envelope.get("ciphertext"), str):
                raise SessionStateError("invalid_backup")
            return envelope
        except SessionStateError:
            raise
        except Exception:
            raise SessionStateError("invalid_backup") from None


async def restore_session_state(context, state) -> dict:
    normalized = _normalize_state(state, strict=True)
    await context.add_cookies(normalized["cookies"])
    origins = normalized["origins"]
    if origins:
        script = f"""
        const entries = {json.dumps(origins, ensure_ascii=False)};
        for (const entry of entries) {{
          if (location.origin !== entry.origin) continue;
          for (const item of entry.localStorage) {{
            if (localStorage.getItem(item.name) === null) {{
              localStorage.setItem(item.name, item.value);
            }}
          }}
        }}
        """
        await context.add_init_script(script=script)
    return {"cookies": len(normalized["cookies"]), "origins": len(origins)}
