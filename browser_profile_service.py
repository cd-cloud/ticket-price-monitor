from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from typing import Any

from config_manager import AppConfig
from data_storage import PriceRepository


class BrowserProfileService:
    def __init__(self, *, config: AppConfig, repository: PriceRepository) -> None:
        self.config = config
        self.repository = repository

    def refresh_config(self, config: AppConfig) -> None:
        self.config = config

    def browser_session_hints(self) -> list[dict[str, Any]]:
        if self.config.browser_backend != "chrome":
            return []
        from browser_session_manager import BrowserSessionManager

        manager = BrowserSessionManager(self.config)
        hints: list[dict[str, Any]] = []
        for provider in self.config.providers.values():
            if not provider.enabled:
                continue
            health = manager.chrome_profile_health(provider, "chrome")
            if health.get("ok"):
                continue
            hints.append(
                {
                    "provider": provider.name,
                    "backend": "chrome",
                    "profile_path": health.get("profile_path"),
                    "reason": health.get("reason"),
                    "needs_login": True,
                    "message": f"{provider.name} 的 Chrome 登录档案异常，已切换 recovery profile；下次查询可能需要重新登录。",
                }
            )
        return hints

    def browser_session_diagnostics(self) -> dict[str, Any]:
        from browser_session_manager import BrowserSessionManager

        manager = BrowserSessionManager(self.config)
        providers: list[dict[str, Any]] = []
        for provider in self.config.providers.values():
            if not provider.enabled:
                continue
            backend_order = manager.backend_names_for_provider(provider)
            health = manager.chrome_profile_health(provider, "chrome") if "chrome" in backend_order else None
            providers.append(
                {
                    "provider": provider.name,
                    "backend_order": backend_order,
                    "primary_backend": backend_order[0] if backend_order else None,
                    "chrome_profile": health,
                    "needs_login": bool(health and not health.get("ok")),
                }
            )
        return {
            "default_backend": self.config.browser_backend,
            "fallbacks": list(self.config.browser_backend_fallbacks),
            "providers": providers,
            "hints": self.browser_session_hints(),
        }

    def browser_profiles(self) -> dict[str, Any]:
        from browser_session_manager import BrowserSessionManager

        manager = BrowserSessionManager(self.config)
        providers: list[dict[str, Any]] = []
        attempts = self.repository.fetch_tail_discovery_attempts(limit=500)
        snapshots = self.repository.fetch_snapshots()
        for provider in self.config.providers.values():
            if not provider.enabled:
                continue
            backend_order = manager.backend_names_for_provider(provider)
            chrome_enabled = "chrome" in backend_order
            health = manager.chrome_profile_health(provider, "chrome") if chrome_enabled else None
            providers.append(
                {
                    "provider": provider.name,
                    "backend_order": backend_order,
                    "chrome_enabled": chrome_enabled,
                    "storage_state_file": str(provider.storage_state_file),
                    "storage_state_exists": provider.storage_state_file.exists(),
                    "chrome_profile": health,
                    "activity": self._profile_activity(provider.name, attempts, snapshots),
                }
            )
        return {
            "default_backend": self.config.browser_backend,
            "fallbacks": list(self.config.browser_backend_fallbacks),
            "providers": providers,
        }

    def open_browser_profile(self, provider_name: str, *, profile: str | None = None) -> dict[str, Any]:
        from backends.chrome import _find_system_chrome
        from browser_session_manager import BrowserSessionManager

        provider = self.config.providers.get(provider_name)
        if not provider or not provider.enabled:
            raise KeyError(provider_name)
        manager = BrowserSessionManager(self.config)
        profile_name = self._resolve_profile_name(manager, provider, profile)
        profile_path = manager.persistent_profile_path_for_backend(provider, "chrome", profile_name)
        profile_path.mkdir(parents=True, exist_ok=True)
        chrome_path = _find_system_chrome()
        if not chrome_path:
            raise RuntimeError("System Chrome not found.")
        subprocess.Popen(
            [
                chrome_path,
                f"--user-data-dir={profile_path}",
                "--no-first-run",
                "--new-window",
                provider.login_url or "https://flights.ctrip.com",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        opened_at = self._now_iso()
        if profile_name.endswith("-recovery"):
            manager.set_chrome_profile_preference(provider.name, forced_profile="recovery", last_manual_opened_at=opened_at)
        else:
            manager.set_chrome_profile_preference(provider.name, forced_profile=None, last_manual_opened_at=opened_at)
        return {"opened": True, "provider": provider.name, "profile_name": profile_name, "profile_path": str(profile_path)}

    def switch_browser_profile(self, provider_name: str, *, profile: str) -> dict[str, Any]:
        from browser_session_manager import BrowserSessionManager

        provider = self.config.providers.get(provider_name)
        if not provider or not provider.enabled:
            raise KeyError(provider_name)
        manager = BrowserSessionManager(self.config)
        normalized = str(profile or "").strip().lower()
        if normalized not in {"primary", "recovery"}:
            raise ValueError("profile must be primary or recovery")
        manager.set_chrome_profile_preference(
            provider.name,
            forced_profile="recovery" if normalized == "recovery" else None,
        )
        return self.browser_profiles()

    def reset_recovery_profile(self, provider_name: str) -> dict[str, Any]:
        from browser_session_manager import BrowserSessionManager, CHROME_PROFILE_RECOVERY_SUFFIXES

        provider = self.config.providers.get(provider_name)
        if not provider or not provider.enabled:
            raise KeyError(provider_name)
        manager = BrowserSessionManager(self.config)
        base_name = manager.profile_name_for_backend(provider, "chrome")
        removed: list[str] = []
        for suffix in CHROME_PROFILE_RECOVERY_SUFFIXES:
            path = manager.persistent_profile_path_for_backend(provider, "chrome", f"{base_name}-{suffix}")
            if path.exists():
                shutil.rmtree(path)
                removed.append(str(path))
        manager.set_chrome_profile_preference(provider.name, forced_profile=None)
        return {"removed": removed, "profiles": self.browser_profiles()}

    def clean_browser_profile(
        self,
        provider_name: str,
        *,
        profile: str | None = None,
        switch_to_recovery: bool = False,
    ) -> dict[str, Any]:
        from browser_session_manager import BrowserSessionManager

        provider = self.config.providers.get(provider_name)
        if not provider or not provider.enabled:
            raise KeyError(provider_name)
        manager = BrowserSessionManager(self.config)
        profile_name = self._resolve_profile_name(manager, provider, profile or "selected")
        result = manager.clean_chrome_profile_site_data(
            provider,
            "chrome",
            profile_name=profile_name,
            switch_to_recovery=switch_to_recovery,
        )
        return {**result, "profiles": self.browser_profiles()}

    def _resolve_profile_name(self, manager: Any, provider: Any, profile: str | None) -> str:
        base_name = manager.profile_name_for_backend(provider, "chrome")
        normalized = str(profile or "selected").strip().lower()
        if normalized == "primary":
            return base_name
        if normalized == "recovery":
            return f"{base_name}-recovery"
        return manager.selected_chrome_profile_name(provider, "chrome")

    @staticmethod
    def _profile_activity(
        provider_name: str,
        attempts: list[dict[str, Any]],
        snapshots: list[dict[str, Any]],
    ) -> dict[str, Any]:
        provider_attempts = [item for item in attempts if item.get("provider") == provider_name]
        provider_snapshots = [item for item in snapshots if item.get("provider") == provider_name]
        latest_success = next((item for item in provider_attempts if item.get("status") == "matched"), None)
        latest_verification = next(
            (
                item
                for item in provider_attempts
                if "verification" in str(item.get("phase") or item.get("error") or "").lower()
                or item.get("status") == "needs_verification"
            ),
            None,
        )
        latest_failure = next((item for item in provider_attempts if item.get("status") == "failed"), None)
        latest_snapshot = provider_snapshots[-1] if provider_snapshots else None
        return {
            "last_success_at": (latest_success or latest_snapshot or {}).get("observed_at"),
            "last_verification_at": (latest_verification or {}).get("observed_at"),
            "last_failure_at": (latest_failure or {}).get("observed_at"),
            "last_failure_reason": (latest_failure or {}).get("error"),
            "recent_attempts": len(provider_attempts),
            "recent_failures": sum(1 for item in provider_attempts if item.get("status") == "failed"),
        }

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
