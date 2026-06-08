import asyncio
import unittest
from unittest.mock import patch

from backends import (
    backend_capabilities,
    backend_definition,
    backend_registry_payload,
    create_backend,
    known_backend_names,
)
from backends.base import BrowserBackend
from backends.fallback import BackendFallbackManager
from backends.playwright import PlaywrightBackend


class BackendFactoryTests(unittest.TestCase):
    def test_create_playwright_returns_expected_type(self) -> None:
        backend = create_backend("playwright")
        self.assertIsInstance(backend, PlaywrightBackend)

    def test_create_rebrowser_when_installed(self) -> None:
        try:
            backend = create_backend("rebrowser")
            self.assertIsInstance(backend, BrowserBackend)
        except ValueError:
            self.skipTest("rebrowser-playwright not installed")

    def test_create_patchright_when_installed(self) -> None:
        try:
            backend = create_backend("patchright")
            self.assertIsInstance(backend, BrowserBackend)
        except ValueError:
            self.skipTest("patchright not installed")

    def test_create_unknown_raises_value_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            create_backend("nonexistent_backend_xyz")
        self.assertIn("nonexistent_backend_xyz", str(ctx.exception))

    def test_registry_exposes_known_backend_even_when_unavailable(self) -> None:
        self.assertIn("camoufox", known_backend_names())
        definition = backend_definition("camoufox")
        self.assertIsNotNone(definition)

    def test_playwright_capabilities_allow_session_reuse(self) -> None:
        capabilities = backend_capabilities("playwright")
        self.assertTrue(capabilities.supports_storage_state)
        self.assertTrue(capabilities.supports_credential_login)

    def test_chrome_capabilities_use_persistent_profile(self) -> None:
        capabilities = backend_capabilities("chrome")
        self.assertFalse(capabilities.supports_storage_state)
        self.assertTrue(capabilities.supports_credential_login)
        self.assertTrue(capabilities.supports_persistent_profile)

    def test_experimental_capabilities_disable_session_reuse(self) -> None:
        capabilities = backend_capabilities("rebrowser")
        self.assertFalse(capabilities.supports_storage_state)
        self.assertFalse(capabilities.supports_credential_login)

    def test_registry_payload_contains_diagnostic_fields(self) -> None:
        payload = backend_registry_payload()
        playwright = next(item for item in payload if item["name"] == "playwright")
        self.assertTrue(playwright["available"])
        self.assertIn("capabilities", playwright)


class BackendFallbackManagerTests(unittest.TestCase):
    def test_priority_list_order_is_respected(self) -> None:
        mgr = BackendFallbackManager(
            backend_names=["playwright"],
            headless=True,
            browser_channel=None,
        )
        self.assertEqual(mgr.backend_names, ["playwright"])

    def test_empty_names_are_filtered(self) -> None:
        mgr = BackendFallbackManager(
            backend_names=["", "playwright", ""],
            headless=True,
            browser_channel=None,
        )
        self.assertEqual(mgr.backend_names, ["playwright"])

    def test_shallow_health_check_does_not_start_unstarted_backends(self) -> None:
        mgr = BackendFallbackManager(
            backend_names=["playwright"],
            headless=True,
            browser_channel=None,
        )
        with patch("backends.fallback.create_backend", side_effect=AssertionError("should not start")):
            report = asyncio.run(mgr.health_check(["playwright"], deep=False))
        self.assertEqual([{"backend": "playwright", "ok": True, "active": False, "error": ""}], report)


if __name__ == "__main__":
    unittest.main()
