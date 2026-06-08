import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from browser_automation import BrowserAutomation
from browser_session_policy import (
    BrowserSessionMetadata,
    build_session_metadata,
    credential_login_allowed_for_backend,
    saved_session_allowed_for_backend,
    session_artifact_for_backend,
)


class BrowserSessionPolicyTests(unittest.TestCase):
    def test_saved_session_is_allowed_only_for_playwright(self) -> None:
        self.assertTrue(saved_session_allowed_for_backend("playwright"))
        self.assertTrue(saved_session_allowed_for_backend(" Playwright "))
        self.assertFalse(saved_session_allowed_for_backend("rebrowser"))
        self.assertFalse(saved_session_allowed_for_backend("patchright"))
        self.assertFalse(saved_session_allowed_for_backend(""))

    def test_credential_login_is_allowed_for_stable_session_backends(self) -> None:
        self.assertTrue(credential_login_allowed_for_backend("playwright"))
        self.assertTrue(credential_login_allowed_for_backend(" Playwright "))
        self.assertTrue(credential_login_allowed_for_backend("chrome"))
        self.assertFalse(credential_login_allowed_for_backend("rebrowser"))
        self.assertFalse(credential_login_allowed_for_backend("patchright"))
        self.assertFalse(credential_login_allowed_for_backend(""))

    def test_storage_state_path_is_returned_only_for_playwright(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"cookies": [{"name": "sid", "value": "x"}]}), encoding="utf-8")
            automation = BrowserAutomation(
                SimpleNamespace(runtime_dir=Path(tmpdir), automation_backend="playwright"),
                {},
            )
            provider = SimpleNamespace(storage_state_file=state_path)

            self.assertEqual(str(state_path), automation._storage_state_path_for_backend(provider, "playwright"))
            self.assertIsNone(automation._storage_state_path_for_backend(provider, "rebrowser"))
            self.assertIsNone(automation._storage_state_path_for_backend(provider, "patchright"))

    def test_session_metadata_records_backend_and_storage_state_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"origins": [{"origin": "https://example.test"}]}), encoding="utf-8")

            playwright_metadata = build_session_metadata(
                backend_name="playwright",
                storage_state_file=state_path,
            )
            rebrowser_metadata = build_session_metadata(
                backend_name="rebrowser",
                storage_state_file=state_path,
            )

            self.assertEqual(
                BrowserSessionMetadata(
                    browser_backend="playwright",
                    storage_state_loaded=True,
                    credential_login_allowed=True,
                    storage_state_path=str(state_path),
                ),
                playwright_metadata,
            )
            self.assertEqual(
                {
                    "browser_backend": "playwright",
                    "storage_state_loaded": True,
                    "credential_login_allowed": True,
                    "persistent_profile_loaded": False,
                },
                playwright_metadata.to_payload(),
            )
            self.assertEqual("rebrowser", rebrowser_metadata.browser_backend)
            self.assertFalse(rebrowser_metadata.storage_state_loaded)
            self.assertFalse(rebrowser_metadata.credential_login_allowed)

    def test_chrome_metadata_uses_persistent_profile_not_storage_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"cookies": [{"name": "sid", "value": "x"}]}), encoding="utf-8")

            metadata = build_session_metadata(
                backend_name="chrome",
                storage_state_file=state_path,
            )

            self.assertEqual("chrome", metadata.browser_backend)
            self.assertFalse(metadata.storage_state_loaded)
            self.assertIsNone(metadata.storage_state_path)
            self.assertTrue(metadata.credential_login_allowed)
            self.assertTrue(metadata.persistent_profile_loaded)

    def test_session_artifact_uses_profile_for_chrome(self) -> None:
        storage_state = Path("runtime") / "ctrip_state.json"
        profile_dir = Path("runtime") / "browser_profiles" / "chrome" / "ctrip"

        artifact = session_artifact_for_backend(
            provider_name="ctrip",
            backend_name="chrome",
            storage_state_file=storage_state,
            persistent_profile_dir=profile_dir,
        )

        self.assertEqual("persistent_profile", artifact.kind)
        self.assertEqual(profile_dir, artifact.path)

    def test_session_artifact_uses_storage_state_for_playwright(self) -> None:
        storage_state = Path("runtime") / "ctrip_state.json"
        profile_dir = Path("runtime") / "browser_profiles" / "playwright" / "ctrip"

        artifact = session_artifact_for_backend(
            provider_name="ctrip",
            backend_name="playwright",
            storage_state_file=storage_state,
            persistent_profile_dir=profile_dir,
        )

        self.assertEqual("storage_state", artifact.kind)
        self.assertEqual(storage_state, artifact.path)

    def test_saved_session_can_be_disabled_for_playwright_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"cookies": [{"name": "sid", "value": "x"}]}), encoding="utf-8")

            metadata = build_session_metadata(
                backend_name="playwright",
                storage_state_file=state_path,
                allow_saved_session=False,
            )

            self.assertEqual("playwright", metadata.browser_backend)
            self.assertFalse(metadata.storage_state_loaded)
            self.assertIsNone(metadata.storage_state_path)

    def test_provider_backend_order_prefers_app_default_and_deduplicates(self) -> None:
        provider = SimpleNamespace(
            name="ctrip",
            browser_backend="rebrowser",
            browser_backend_fallbacks=["patchright", "playwright"],
        )
        automation = BrowserAutomation(
            SimpleNamespace(
                runtime_dir=Path("."),
                automation_backend="playwright",
                browser_backend="playwright",
                browser_backend_fallbacks=["rebrowser", "patchright"],
                providers={"ctrip": provider},
            ),
            {},
        )

        self.assertEqual(
            ["playwright", "rebrowser", "patchright"],
            automation._backend_names_for_provider(provider),
        )
        self.assertEqual(
            ["playwright", "rebrowser", "patchright"],
            automation._configured_backend_names(),
        )

    def test_provider_backend_order_prefers_chrome_default(self) -> None:
        provider = SimpleNamespace(
            name="ctrip",
            browser_backend="rebrowser",
            browser_backend_fallbacks=["patchright", "playwright"],
        )
        automation = BrowserAutomation(
            SimpleNamespace(
                runtime_dir=Path("."),
                automation_backend="playwright",
                browser_backend="chrome",
                browser_backend_fallbacks=["playwright"],
                providers={"ctrip": provider},
            ),
            {},
        )

        self.assertEqual(
            ["chrome", "playwright", "rebrowser", "patchright"],
            automation._backend_names_for_provider(provider),
        )

    def test_backend_override_replaces_provider_backend_order(self) -> None:
        provider = SimpleNamespace(
            name="ctrip",
            browser_backend="chrome",
            browser_backend_fallbacks=["playwright"],
        )
        automation = BrowserAutomation(
            SimpleNamespace(
                runtime_dir=Path("."),
                automation_backend="playwright",
                browser_backend="chrome",
                browser_backend_fallbacks=["playwright"],
                providers={"ctrip": provider},
            ),
            {},
            backend_override="camoufox",
        )

        self.assertEqual(["camoufox"], automation._backend_names_for_provider(provider))

    def test_ctrip_no_fare_is_confirmed_by_fallback_backend(self) -> None:
        route = SimpleNamespace()

        self.assertFalse(BrowserAutomation._should_stop_after_no_fare("ctrip", route, 0, 2))
        self.assertTrue(BrowserAutomation._should_stop_after_no_fare("ctrip", route, 1, 2))
        self.assertTrue(BrowserAutomation._should_stop_after_no_fare("feizhu", route, 0, 2))

    def test_ctrip_login_does_not_visit_login_url_for_chrome_profile(self) -> None:
        async def fail_if_called(*args, **kwargs):
            raise AssertionError("Ctrip login preflight should not open login_url")

        provider = SimpleNamespace(name="ctrip", login_url="https://accounts.ctrip.com/login", timeout_ms=1000)
        automation = BrowserAutomation(
            SimpleNamespace(
                runtime_dir=Path("."),
                automation_backend="playwright",
                providers={"ctrip": provider},
            ),
            {},
        )
        automation._context_backend_name = lambda context: "chrome"
        automation._session_metadata_for_context = lambda context: BrowserSessionMetadata(
            browser_backend="chrome",
            storage_state_loaded=False,
            credential_login_allowed=True,
            persistent_profile_loaded=True,
        )
        page = SimpleNamespace(goto=fail_if_called)

        asyncio.run(automation.login("ctrip", page=page, context=SimpleNamespace()))

if __name__ == "__main__":
    unittest.main()
