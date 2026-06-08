"""Session reuse policy for browser automation contexts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from backends import backend_capabilities


@dataclass(frozen=True, slots=True)
class BrowserSessionMetadata:
    browser_backend: str
    storage_state_loaded: bool
    credential_login_allowed: bool
    storage_state_path: str | None = None
    persistent_profile_loaded: bool = False

    def to_payload(self) -> dict[str, object]:
        return {
            "browser_backend": self.browser_backend,
            "storage_state_loaded": self.storage_state_loaded,
            "credential_login_allowed": self.credential_login_allowed,
            "persistent_profile_loaded": self.persistent_profile_loaded,
        }


@dataclass(frozen=True, slots=True)
class BrowserSessionArtifact:
    provider: str
    browser_backend: str
    kind: str
    path: Path

    def to_payload(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "browser_backend": self.browser_backend,
            "kind": self.kind,
            "path": str(self.path),
        }


def normalize_backend_name(backend_name: str | None) -> str:
    return str(backend_name or "").strip().lower()


def saved_session_allowed_for_backend(backend_name: str | None) -> bool:
    """Return whether this backend is allowed to load saved storage_state."""
    return backend_capabilities(backend_name).supports_storage_state


def credential_login_allowed_for_backend(backend_name: str | None) -> bool:
    """Return whether this backend can submit configured credentials."""
    return backend_capabilities(backend_name).supports_credential_login


def persistent_profile_allowed_for_backend(backend_name: str | None) -> bool:
    """Return whether this backend reuses a persistent browser profile."""
    return backend_capabilities(backend_name).supports_persistent_profile


def session_artifact_for_backend(
    *,
    provider_name: str,
    backend_name: str | None,
    storage_state_file: Path,
    persistent_profile_dir: Path,
) -> BrowserSessionArtifact:
    normalized_backend = normalize_backend_name(backend_name)
    if persistent_profile_allowed_for_backend(normalized_backend):
        return BrowserSessionArtifact(
            provider=provider_name,
            browser_backend=normalized_backend,
            kind="persistent_profile",
            path=persistent_profile_dir,
        )
    return BrowserSessionArtifact(
        provider=provider_name,
        browser_backend=normalized_backend,
        kind="storage_state",
        path=storage_state_file,
    )


def storage_state_is_usable(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return bool(state.get("cookies") or state.get("origins"))


def build_session_metadata(
    *,
    backend_name: str | None,
    storage_state_file: Path,
    allow_saved_session: bool = True,
) -> BrowserSessionMetadata:
    normalized_backend = normalize_backend_name(backend_name)
    storage_state_path = None
    if (
        allow_saved_session
        and saved_session_allowed_for_backend(normalized_backend)
        and storage_state_is_usable(storage_state_file)
    ):
        storage_state_path = str(storage_state_file)
    return BrowserSessionMetadata(
        browser_backend=normalized_backend,
        storage_state_loaded=storage_state_path is not None,
        credential_login_allowed=credential_login_allowed_for_backend(normalized_backend),
        storage_state_path=storage_state_path,
        persistent_profile_loaded=persistent_profile_allowed_for_backend(normalized_backend),
    )
