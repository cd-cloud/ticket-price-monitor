"""Browser context and session lifecycle management."""

from __future__ import annotations

import logging
import json
import shutil
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

from playwright.async_api import BrowserContext, Page

from backends.fallback import BackendFallbackManager
from browser_session_policy import (
    BrowserSessionArtifact,
    BrowserSessionMetadata,
    build_session_metadata,
    saved_session_allowed_for_backend,
    session_artifact_for_backend,
)
from config_manager import AppConfig, ProviderConfig


LOGGER = logging.getLogger(__name__)
CHROME_PROFILE_VERSION = "v2"
CHROME_PROFILE_LOCK_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")
CHROME_PROFILE_RECOVERY_SUFFIXES = ("recovery", "recovery-2", "recovery-3")
PROFILE_STATE_FILENAME = "profile_state.json"
CHROME_PROFILE_SITE_DATA_DIRS = (
    "Cache",
    "Code Cache",
    "GPUCache",
    "DawnCache",
    "GrShaderCache",
    "ShaderCache",
    "Service Worker",
    "Session Storage",
    "Local Storage",
    "IndexedDB",
    "File System",
    "blob_storage",
)
CHROME_PROFILE_SITE_DATA_FILES = (
    "Cookies",
    "Cookies-journal",
    "Network/Cookies",
    "Network/Cookies-journal",
    "History",
    "History-journal",
    "Top Sites",
    "Top Sites-journal",
    "Visited Links",
)


class BrowserSessionManager:
    """Owns browser backend startup, context creation, and session metadata."""

    def __init__(self, app_config: AppConfig) -> None:
        self.app_config = app_config
        self._backend_manager: BackendFallbackManager | None = None
        self._context_session_metadata: dict[int, BrowserSessionMetadata] = {}
        self._context_profile_names: dict[int, str] = {}

    @property
    def active_backend(self):
        if self._backend_manager is None:
            return None
        return self._backend_manager.active_backend

    async def start(self, backend_names: list[str] | None = None) -> str:
        names = backend_names or [self.app_config.browser_backend, *list(self.app_config.browser_backend_fallbacks)]
        self._backend_manager = BackendFallbackManager(
            backend_names=names,
            headless=self.app_config.headless,
            browser_channel=self.app_config.browser_channel,
            profile_root=self.app_config.runtime_dir / "browser_profiles",
        )
        return await self._backend_manager.start()

    async def stop(self) -> None:
        if self._backend_manager is not None:
            await self._backend_manager.stop()
        self._backend_manager = None
        self._context_session_metadata.clear()
        self._context_profile_names.clear()

    async def health_check(
        self,
        *,
        active_only: bool = True,
        deep: bool = False,
    ) -> list[dict[str, str | bool]]:
        if self._backend_manager is None:
            raise RuntimeError("BrowserSessionManager is not started")
        return await self._backend_manager.health_check(
            self.configured_backend_names(),
            active_only=active_only,
            deep=deep,
        )

    async def new_page(
        self,
        provider: ProviderConfig,
        *,
        allow_saved_session: bool = True,
        backend_name: str | None = None,
    ) -> tuple[Page, BrowserContext]:
        if self._backend_manager is None:
            raise RuntimeError("BrowserSessionManager is not started")

        errors: list[str] = []
        candidates = [backend_name] if backend_name else self.backend_names_for_provider(provider)
        for candidate_name in candidates:
            if not candidate_name:
                continue
            try:
                backend = await self._backend_manager.ensure_backend(candidate_name)
                profile_name = self.profile_name_for_backend(provider, candidate_name)
                if self._is_chrome_backend(candidate_name):
                    profile_name = self._select_healthy_chrome_profile(provider, candidate_name)
                session_metadata = build_session_metadata(
                    backend_name=candidate_name,
                    storage_state_file=provider.storage_state_file,
                    allow_saved_session=allow_saved_session,
                )
                context = await backend.new_context(
                    storage_state=session_metadata.storage_state_path,
                    extra_http_headers=provider.extra_headers or None,
                    locale=provider.locale,
                    timezone_id=provider.timezone,
                    profile_name=profile_name,
                )
                self._context_session_metadata[id(context)] = session_metadata
                self._context_profile_names[id(context)] = profile_name
                page = await backend.new_page(context)
                return page, context
            except Exception as exc:
                errors.append(f"{candidate_name}: {exc}")
                LOGGER.warning("new page failed provider=%s backend=%s error=%s", provider.name, candidate_name, exc)
        raise RuntimeError(f"No browser backend available for provider={provider.name}: {' | '.join(errors)}")

    async def close_context_quietly(self, context: BrowserContext | None) -> None:
        if context is None or self._backend_manager is None:
            return
        try:
            session_metadata = self.session_metadata_for_context(context)
            backend = (
                self._backend_manager.get_backend(session_metadata.browser_backend)
                if session_metadata.browser_backend
                else self._backend_manager.active_backend
            )
            if backend:
                await backend.close_context(context)
        except Exception as error:
            LOGGER.debug("context close skipped: %s", error)
        finally:
            self._context_session_metadata.pop(id(context), None)
            self._context_profile_names.pop(id(context), None)

    def configured_backend_names(self) -> list[str]:
        names = [self.app_config.browser_backend, *self.app_config.browser_backend_fallbacks]
        for provider in self.app_config.providers.values():
            if provider.browser_backend:
                names.append(provider.browser_backend)
            names.extend(provider.browser_backend_fallbacks or [])
        return self._dedupe_backend_names(names)

    def backend_names_for_provider(self, provider: ProviderConfig) -> list[str]:
        names: list[str] = [self.app_config.browser_backend, *self.app_config.browser_backend_fallbacks]
        if provider.browser_backend:
            names.append(provider.browser_backend)
        names.extend(provider.browser_backend_fallbacks or [])
        return self._dedupe_backend_names(names)

    def context_backend_name(self, context: BrowserContext | None) -> str:
        return self.session_metadata_for_context(context).browser_backend

    def storage_state_path_for_backend(self, provider: ProviderConfig, backend_name: str | None) -> str | None:
        return build_session_metadata(
            backend_name=backend_name,
            storage_state_file=provider.storage_state_file,
        ).storage_state_path

    def persistent_profile_path_for_backend(
        self,
        provider: ProviderConfig,
        backend_name: str | None,
        profile_name: str | None = None,
    ) -> Path:
        backend = str(backend_name or "").strip().lower() or self.app_config.browser_backend
        profile = profile_name or self.profile_name_for_backend(provider, backend)
        return self.app_config.runtime_dir / "browser_profiles" / backend / profile

    @staticmethod
    def profile_name_for_backend(provider: ProviderConfig, backend_name: str | None) -> str:
        backend = str(backend_name or "").strip().lower()
        if backend == "chrome":
            return f"{provider.name}-{CHROME_PROFILE_VERSION}"
        return provider.name

    def session_artifact_for_context(
        self,
        context: BrowserContext | None,
        provider: ProviderConfig,
    ) -> BrowserSessionArtifact:
        backend_name = self.context_backend_name(context)
        profile_name = self._context_profile_names.get(id(context)) if context is not None else None
        return session_artifact_for_backend(
            provider_name=provider.name,
            backend_name=backend_name,
            storage_state_file=provider.storage_state_file,
            persistent_profile_dir=self.persistent_profile_path_for_backend(provider, backend_name, profile_name),
        )

    def profile_name_for_context(self, context: BrowserContext | None) -> str | None:
        if context is None:
            return None
        return self._context_profile_names.get(id(context))

    def session_metadata_for_context(self, context: BrowserContext | None) -> BrowserSessionMetadata:
        if context is None:
            return BrowserSessionMetadata(browser_backend="", storage_state_loaded=False, credential_login_allowed=False)
        return self._context_session_metadata.get(
            id(context),
            BrowserSessionMetadata(browser_backend="", storage_state_loaded=False, credential_login_allowed=False),
        )

    async def save_storage_state_if_allowed(self, context: BrowserContext, provider: ProviderConfig) -> None:
        backend_name = self.context_backend_name(context)
        if not saved_session_allowed_for_backend(backend_name):
            LOGGER.info(
                "skipping storage_state save for provider=%s backend=%s",
                provider.name,
                backend_name or "unknown",
            )
            return
        await context.storage_state(path=str(provider.storage_state_file))

    def chrome_profile_health(self, provider: ProviderConfig, backend_name: str | None = "chrome") -> dict[str, object]:
        profile_name = self.profile_name_for_backend(provider, backend_name)
        profile_dir = self.persistent_profile_path_for_backend(provider, backend_name, profile_name)
        ok, reason = self._chrome_profile_is_healthy(profile_dir)
        selected_profile = self.selected_chrome_profile_name(provider, backend_name)
        state = self._profile_state().get(provider.name, {}).get("chrome", {})
        polluted_profiles = state.get("polluted_profiles") if isinstance(state.get("polluted_profiles"), dict) else {}
        return {
            "backend": str(backend_name or "").strip().lower() or self.app_config.browser_backend,
            "provider": provider.name,
            "profile_name": profile_name,
            "profile_path": str(profile_dir),
            "selected_profile_name": selected_profile,
            "selected_profile_path": str(self.persistent_profile_path_for_backend(provider, backend_name, selected_profile)),
            "forced_profile": state.get("forced_profile"),
            "polluted_profiles": polluted_profiles,
            "last_manual_opened_at": state.get("last_manual_opened_at"),
            "ok": ok,
            "reason": reason,
            "profiles": self.chrome_profile_entries(provider, backend_name),
        }

    def chrome_profile_entries(self, provider: ProviderConfig, backend_name: str | None = "chrome") -> list[dict[str, object]]:
        base_name = self.profile_name_for_backend(provider, backend_name)
        names = [base_name, *[f"{base_name}-{suffix}" for suffix in CHROME_PROFILE_RECOVERY_SUFFIXES]]
        state = self._profile_state().get(provider.name, {}).get("chrome", {})
        polluted_profiles = state.get("polluted_profiles") if isinstance(state.get("polluted_profiles"), dict) else {}
        entries: list[dict[str, object]] = []
        for name in names:
            path = self.persistent_profile_path_for_backend(provider, backend_name, name)
            ok, reason = self._chrome_profile_is_healthy(path)
            pollution = polluted_profiles.get(name) if isinstance(polluted_profiles.get(name), dict) else None
            entries.append(
                {
                    "name": name,
                    "path": str(path),
                    "exists": path.exists(),
                    "ok": ok,
                    "reason": reason,
                    "is_recovery": name != base_name,
                    "polluted": pollution is not None,
                    "pollution": pollution,
                }
            )
        return entries

    def selected_chrome_profile_name(self, provider: ProviderConfig, backend_name: str | None = "chrome") -> str:
        return self._select_healthy_chrome_profile(provider, backend_name, log=False)

    def set_chrome_profile_preference(
        self,
        provider_name: str,
        *,
        forced_profile: str | None,
        last_manual_opened_at: str | None = None,
    ) -> dict[str, object]:
        state = self._profile_state()
        provider_state = dict(state.get(provider_name) or {})
        chrome_state = dict(provider_state.get("chrome") or {})
        if forced_profile:
            chrome_state["forced_profile"] = forced_profile
        else:
            chrome_state.pop("forced_profile", None)
        if last_manual_opened_at:
            chrome_state["last_manual_opened_at"] = last_manual_opened_at
        provider_state["chrome"] = chrome_state
        state[provider_name] = provider_state
        self._write_profile_state(state)
        return chrome_state

    def mark_chrome_profile_polluted(
        self,
        provider: ProviderConfig,
        backend_name: str | None = "chrome",
        *,
        profile_name: str | None = None,
        reason: str = "",
    ) -> dict[str, object]:
        selected = profile_name or self.selected_chrome_profile_name(provider, backend_name)
        state = self._profile_state()
        provider_state = dict(state.get(provider.name) or {})
        chrome_state = dict(provider_state.get("chrome") or {})
        polluted_profiles = dict(chrome_state.get("polluted_profiles") or {})
        polluted_profiles[selected] = {
            "marked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "reason": reason,
        }
        chrome_state["polluted_profiles"] = polluted_profiles
        provider_state["chrome"] = chrome_state
        state[provider.name] = provider_state
        self._write_profile_state(state)

        next_profile = self.selected_chrome_profile_name(provider, backend_name)
        chrome_state = dict(self._profile_state().get(provider.name, {}).get("chrome", {}))
        if next_profile and next_profile != selected:
            chrome_state["forced_profile"] = next_profile
            provider_state = dict(state.get(provider.name) or {})
            provider_state["chrome"] = chrome_state
            state[provider.name] = provider_state
            self._write_profile_state(state)

        LOGGER.warning(
            "provider=%s backend=chrome quarantined profile=%s; next_profile=%s reason=%s",
            provider.name,
            selected,
            next_profile,
            reason,
        )
        return {
            "provider": provider.name,
            "profile_name": selected,
            "next_profile_name": next_profile,
            "chrome_profile": self.chrome_profile_health(provider, backend_name),
        }

    def clean_chrome_profile_site_data(
        self,
        provider: ProviderConfig,
        backend_name: str | None = "chrome",
        *,
        profile_name: str | None = None,
        switch_to_recovery: bool = False,
    ) -> dict[str, object]:
        selected = profile_name or self.selected_chrome_profile_name(provider, backend_name)
        profile_dir = self.persistent_profile_path_for_backend(provider, backend_name, selected)
        default_dir = profile_dir / "Default"
        removed: list[str] = []
        skipped: list[str] = []

        targets = [profile_dir / name for name in CHROME_PROFILE_LOCK_FILES]
        targets.extend(default_dir / name for name in CHROME_PROFILE_SITE_DATA_DIRS)
        targets.extend(default_dir / name for name in CHROME_PROFILE_SITE_DATA_FILES)

        for target in targets:
            if not target.exists():
                continue
            try:
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
                removed.append(str(target))
            except Exception as exc:
                skipped.append(f"{target}: {exc}")

        state = self._profile_state()
        provider_state = dict(state.get(provider.name) or {})
        chrome_state = dict(provider_state.get("chrome") or {})
        chrome_state["last_cleanup_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        chrome_state["last_cleanup_profile"] = selected
        chrome_state["last_cleanup_removed"] = len(removed)
        polluted_profiles = dict(chrome_state.get("polluted_profiles") or {})
        polluted_profiles.pop(selected, None)
        if polluted_profiles:
            chrome_state["polluted_profiles"] = polluted_profiles
        else:
            chrome_state.pop("polluted_profiles", None)
        if switch_to_recovery:
            chrome_state["forced_profile"] = "recovery"
        provider_state["chrome"] = chrome_state
        state[provider.name] = provider_state
        self._write_profile_state(state)

        return {
            "provider": provider.name,
            "profile_name": selected,
            "profile_path": str(profile_dir),
            "removed": removed,
            "skipped": skipped,
            "switched_to_recovery": switch_to_recovery,
            "chrome_profile": self.chrome_profile_health(provider, backend_name),
        }

    def _select_healthy_chrome_profile(self, provider: ProviderConfig, backend_name: str | None) -> str:
        return self._select_healthy_chrome_profile(provider, backend_name, log=True)

    def _select_healthy_chrome_profile(
        self,
        provider: ProviderConfig,
        backend_name: str | None,
        *,
        log: bool = True,
    ) -> str:
        base_name = self.profile_name_for_backend(provider, backend_name)
        recovery_names = [f"{base_name}-{suffix}" for suffix in CHROME_PROFILE_RECOVERY_SUFFIXES]
        chrome_state = self._profile_state().get(provider.name, {}).get("chrome", {})
        forced_profile = chrome_state.get("forced_profile")
        polluted_profiles = chrome_state.get("polluted_profiles") if isinstance(chrome_state.get("polluted_profiles"), dict) else {}
        known_profiles = [base_name, *recovery_names]
        if forced_profile in known_profiles:
            candidates = [forced_profile, *[name for name in known_profiles if name != forced_profile]]
        elif forced_profile == "recovery":
            candidates = [*recovery_names, base_name]
        else:
            candidates = [base_name, *recovery_names]
        last_reason = ""
        polluted_fallback: str | None = None
        for profile_name in candidates:
            profile_dir = self.persistent_profile_path_for_backend(provider, backend_name, profile_name)
            ok, reason = self._chrome_profile_is_healthy(profile_dir)
            polluted = profile_name in polluted_profiles
            if ok and not polluted:
                if log and profile_name != base_name:
                    LOGGER.warning(
                        "provider=%s backend=chrome using recovery profile=%s because primary profile is unhealthy: %s",
                        provider.name,
                        profile_name,
                        last_reason or "unknown",
                    )
                return profile_name
            if ok and polluted and polluted_fallback is None:
                polluted_fallback = profile_name
            last_reason = reason
            if log:
                if polluted:
                    reason = f"profile quarantined: {polluted_profiles.get(profile_name, {}).get('reason', 'unknown')}"
                LOGGER.warning(
                    "provider=%s backend=chrome profile=%s unhealthy: %s",
                    provider.name,
                    profile_name,
                    reason,
                )
        return polluted_fallback or candidates[-1]

    def _profile_state_path(self) -> Path:
        return self.app_config.runtime_dir / "browser_profiles" / PROFILE_STATE_FILENAME

    def _profile_state(self) -> dict[str, Any]:
        path = self._profile_state_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _write_profile_state(self, state: dict[str, object]) -> None:
        path = self._profile_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            **state,
            "_updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _chrome_profile_is_healthy(profile_dir: Path) -> tuple[bool, str]:
        if not profile_dir.exists():
            return True, "profile does not exist yet"
        for lock_file in CHROME_PROFILE_LOCK_FILES:
            if (profile_dir / lock_file).exists():
                return False, f"stale lock file: {lock_file}"
        preferences = profile_dir / "Default" / "Preferences"
        if preferences.exists():
            try:
                json.loads(preferences.read_text(encoding="utf-8"))
            except Exception as exc:
                return False, f"invalid Preferences JSON: {exc}"
        return True, "ok"

    @staticmethod
    def _is_chrome_backend(backend_name: str | None) -> bool:
        return str(backend_name or "").strip().lower() == "chrome"

    @staticmethod
    def _dedupe_backend_names(names: list[str | None]) -> list[str]:
        return list(dict.fromkeys([str(name).strip().lower() for name in names if str(name or "").strip()]))
