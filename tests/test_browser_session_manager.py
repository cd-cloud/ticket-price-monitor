import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from browser_session_manager import BrowserSessionManager
from browser_session_policy import BrowserSessionMetadata


class FakeContext:
    def __init__(self) -> None:
        self.storage_state_path: str | None = None

    async def storage_state(self, *, path: str) -> None:
        self.storage_state_path = path


class BrowserSessionManagerTests(unittest.TestCase):
    def test_backend_order_prefers_app_default_and_deduplicates(self) -> None:
        provider = SimpleNamespace(
            name="ctrip",
            browser_backend="rebrowser",
            browser_backend_fallbacks=["patchright", "playwright"],
        )
        manager = BrowserSessionManager(
            SimpleNamespace(
                browser_backend="playwright",
                browser_backend_fallbacks=["rebrowser", "patchright"],
                providers={"ctrip": provider},
                headless=True,
                browser_channel=None,
            )
        )

        self.assertEqual(
            ["playwright", "rebrowser", "patchright"],
            manager.backend_names_for_provider(provider),
        )
        self.assertEqual(
            ["playwright", "rebrowser", "patchright"],
            manager.configured_backend_names(),
        )

    def test_backend_order_prefers_chrome_when_configured_as_default(self) -> None:
        provider = SimpleNamespace(
            name="ctrip",
            browser_backend="rebrowser",
            browser_backend_fallbacks=["patchright", "playwright"],
        )
        manager = BrowserSessionManager(
            SimpleNamespace(
                browser_backend="chrome",
                browser_backend_fallbacks=["playwright"],
                providers={"ctrip": provider},
                headless=True,
                browser_channel=None,
            )
        )

        self.assertEqual(
            ["chrome", "playwright", "rebrowser", "patchright"],
            manager.backend_names_for_provider(provider),
        )

    def test_session_metadata_defaults_to_empty_policy(self) -> None:
        manager = BrowserSessionManager(
            SimpleNamespace(
                browser_backend="playwright",
                browser_backend_fallbacks=[],
                providers={},
                headless=True,
                browser_channel=None,
            )
        )

        self.assertEqual(
            BrowserSessionMetadata(browser_backend="", storage_state_loaded=False, credential_login_allowed=False),
            manager.session_metadata_for_context(None),
        )

    def test_save_storage_state_only_for_allowed_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            provider = SimpleNamespace(name="ctrip", storage_state_file=state_path)
            manager = BrowserSessionManager(
                SimpleNamespace(
                    browser_backend="playwright",
                    browser_backend_fallbacks=[],
                    providers={},
                    headless=True,
                    browser_channel=None,
                )
            )

            playwright_context = FakeContext()
            manager._context_session_metadata[id(playwright_context)] = BrowserSessionMetadata(
                browser_backend="playwright",
                storage_state_loaded=False,
                credential_login_allowed=True,
            )
            asyncio.run(manager.save_storage_state_if_allowed(playwright_context, provider))
            self.assertEqual(str(state_path), playwright_context.storage_state_path)

            rebrowser_context = FakeContext()
            manager._context_session_metadata[id(rebrowser_context)] = BrowserSessionMetadata(
                browser_backend="rebrowser",
                storage_state_loaded=False,
                credential_login_allowed=False,
            )
            asyncio.run(manager.save_storage_state_if_allowed(rebrowser_context, provider))
            self.assertIsNone(rebrowser_context.storage_state_path)

    def test_session_artifact_points_to_chrome_provider_profile(self) -> None:
        provider = SimpleNamespace(name="ctrip", storage_state_file=Path("runtime") / "ctrip_state.json")
        manager = BrowserSessionManager(
            SimpleNamespace(
                runtime_dir=Path("runtime"),
                browser_backend="chrome",
                browser_backend_fallbacks=["playwright"],
                providers={},
                headless=True,
                browser_channel=None,
            )
        )
        context = FakeContext()
        manager._context_session_metadata[id(context)] = BrowserSessionMetadata(
            browser_backend="chrome",
            storage_state_loaded=False,
            credential_login_allowed=True,
            persistent_profile_loaded=True,
        )

        artifact = manager.session_artifact_for_context(context, provider)

        self.assertEqual("persistent_profile", artifact.kind)
        self.assertEqual(Path("runtime") / "browser_profiles" / "chrome" / "ctrip-v2", artifact.path)

    def test_chrome_profile_health_detects_stale_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = SimpleNamespace(name="ctrip", storage_state_file=Path(tmpdir) / "state.json")
            manager = BrowserSessionManager(
                SimpleNamespace(
                    runtime_dir=Path(tmpdir),
                    browser_backend="chrome",
                    browser_backend_fallbacks=["playwright"],
                    providers={},
                    headless=True,
                    browser_channel=None,
                )
            )
            profile_dir = Path(tmpdir) / "browser_profiles" / "chrome" / "ctrip-v2"
            profile_dir.mkdir(parents=True)
            (profile_dir / "SingletonLock").write_text("", encoding="utf-8")

            health = manager.chrome_profile_health(provider)

            self.assertFalse(health["ok"])
            self.assertIn("stale lock file", str(health["reason"]))

    def test_unhealthy_chrome_profile_switches_to_recovery_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = SimpleNamespace(name="ctrip", storage_state_file=Path(tmpdir) / "state.json")
            manager = BrowserSessionManager(
                SimpleNamespace(
                    runtime_dir=Path(tmpdir),
                    browser_backend="chrome",
                    browser_backend_fallbacks=["playwright"],
                    providers={},
                    headless=True,
                    browser_channel=None,
                )
            )
            profile_dir = Path(tmpdir) / "browser_profiles" / "chrome" / "ctrip-v2"
            profile_dir.mkdir(parents=True)
            (profile_dir / "SingletonCookie").write_text("", encoding="utf-8")

            self.assertEqual("ctrip-v2-recovery", manager._select_healthy_chrome_profile(provider, "chrome"))

    def test_chrome_profile_preference_can_force_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = SimpleNamespace(name="ctrip", storage_state_file=Path(tmpdir) / "state.json")
            manager = BrowserSessionManager(
                SimpleNamespace(
                    runtime_dir=Path(tmpdir),
                    browser_backend="chrome",
                    browser_backend_fallbacks=["playwright"],
                    providers={},
                    headless=True,
                    browser_channel=None,
                )
            )

            manager.set_chrome_profile_preference(provider.name, forced_profile="recovery")

            self.assertEqual("ctrip-v2-recovery", manager.selected_chrome_profile_name(provider, "chrome"))
            health = manager.chrome_profile_health(provider)
            self.assertEqual("recovery", health["forced_profile"])

    def test_polluted_chrome_profile_switches_to_clean_recovery_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = SimpleNamespace(name="ctrip", storage_state_file=Path(tmpdir) / "state.json")
            manager = BrowserSessionManager(
                SimpleNamespace(
                    runtime_dir=Path(tmpdir),
                    browser_backend="chrome",
                    browser_backend_fallbacks=["playwright"],
                    providers={},
                    headless=True,
                    browser_channel=None,
                )
            )

            result = manager.mark_chrome_profile_polluted(
                provider,
                "chrome",
                profile_name="ctrip-v2",
                reason="login overlay blocked",
            )

            self.assertEqual("ctrip-v2", result["profile_name"])
            self.assertEqual("ctrip-v2-recovery", manager.selected_chrome_profile_name(provider, "chrome"))
            health = manager.chrome_profile_health(provider)
            self.assertEqual("ctrip-v2-recovery", health["forced_profile"])
            self.assertIn("ctrip-v2", health["polluted_profiles"])

    def test_clean_chrome_profile_site_data_clears_pollution_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = SimpleNamespace(name="ctrip", storage_state_file=Path(tmpdir) / "state.json")
            manager = BrowserSessionManager(
                SimpleNamespace(
                    runtime_dir=Path(tmpdir),
                    browser_backend="chrome",
                    browser_backend_fallbacks=["playwright"],
                    providers={},
                    headless=True,
                    browser_channel=None,
                )
            )
            manager.mark_chrome_profile_polluted(
                provider,
                "chrome",
                profile_name="ctrip-v2",
                reason="login overlay blocked",
            )

            manager.clean_chrome_profile_site_data(provider, "chrome", profile_name="ctrip-v2")

            health = manager.chrome_profile_health(provider)
            self.assertNotIn("ctrip-v2", health["polluted_profiles"])

    def test_session_artifact_uses_selected_recovery_profile(self) -> None:
        provider = SimpleNamespace(name="ctrip", storage_state_file=Path("runtime") / "ctrip_state.json")
        manager = BrowserSessionManager(
            SimpleNamespace(
                runtime_dir=Path("runtime"),
                browser_backend="chrome",
                browser_backend_fallbacks=["playwright"],
                providers={},
                headless=True,
                browser_channel=None,
            )
        )
        context = FakeContext()
        manager._context_session_metadata[id(context)] = BrowserSessionMetadata(
            browser_backend="chrome",
            storage_state_loaded=False,
            credential_login_allowed=True,
            persistent_profile_loaded=True,
        )
        manager._context_profile_names[id(context)] = "ctrip-v2-recovery"

        artifact = manager.session_artifact_for_context(context, provider)

        self.assertEqual(Path("runtime") / "browser_profiles" / "chrome" / "ctrip-v2-recovery", artifact.path)

    def test_clean_chrome_profile_site_data_removes_pollution_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = SimpleNamespace(name="ctrip", storage_state_file=Path(tmpdir) / "state.json")
            manager = BrowserSessionManager(
                SimpleNamespace(
                    runtime_dir=Path(tmpdir),
                    browser_backend="chrome",
                    browser_backend_fallbacks=["playwright"],
                    providers={},
                    headless=True,
                    browser_channel=None,
                )
            )
            profile_dir = Path(tmpdir) / "browser_profiles" / "chrome" / "ctrip-v2"
            default_dir = profile_dir / "Default"
            (default_dir / "Cache").mkdir(parents=True)
            (default_dir / "Cache" / "entry").write_text("cache", encoding="utf-8")
            (default_dir / "Service Worker").mkdir()
            (default_dir / "Cookies").write_text("cookies", encoding="utf-8")
            (profile_dir / "SingletonCookie").write_text("", encoding="utf-8")

            result = manager.clean_chrome_profile_site_data(provider, "chrome", profile_name="ctrip-v2")

            self.assertFalse((default_dir / "Cache").exists())
            self.assertFalse((default_dir / "Service Worker").exists())
            self.assertFalse((default_dir / "Cookies").exists())
            self.assertFalse((profile_dir / "SingletonCookie").exists())
            self.assertEqual("ctrip-v2", result["profile_name"])
            self.assertGreaterEqual(len(result["removed"]), 4)


if __name__ == "__main__":
    unittest.main()
