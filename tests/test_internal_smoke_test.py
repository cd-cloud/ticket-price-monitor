import os
import unittest

from internal_smoke_test import cloudflare_access_headers, detect_verification, parse_viewport


class InternalSmokeTestTests(unittest.TestCase):
    def test_parse_viewport_bounds_values(self) -> None:
        self.assertEqual(parse_viewport("1440x900"), {"width": 1440, "height": 900})
        self.assertEqual(parse_viewport("bad"), {"width": 1365, "height": 900})

    def test_cloudflare_access_headers_from_env(self) -> None:
        os.environ["TEST_CF_ID"] = "id"
        os.environ["TEST_CF_SECRET"] = "secret"
        try:
            self.assertEqual(
                cloudflare_access_headers("TEST_CF_ID,TEST_CF_SECRET"),
                {
                    "CF-Access-Client-Id": "id",
                    "CF-Access-Client-Secret": "secret",
                },
            )
        finally:
            os.environ.pop("TEST_CF_ID", None)
            os.environ.pop("TEST_CF_SECRET", None)

    def test_detect_verification(self) -> None:
        self.assertTrue(detect_verification("Verify you are human before continuing"))
        self.assertFalse(detect_verification("Welcome to the internal dashboard"))


if __name__ == "__main__":
    unittest.main()
