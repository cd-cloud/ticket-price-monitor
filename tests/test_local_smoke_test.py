import json
import tempfile
import unittest
from pathlib import Path

from local_smoke_test import run_local_smoke_test


class LocalSmokeTestTests(unittest.TestCase):
    def test_local_smoke_test_completes_successfully(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            args = type(
                "Args",
                (),
                {
                    "output_dir": tmp_dir,
                    "timeout_seconds": 30,
                    "keep_temp": False,
                },
            )()

            exit_code = run_local_smoke_test(args)

            self.assertEqual(0, exit_code)
            artifacts = list(Path(tmp_dir).glob("local_smoke_*.json"))
            self.assertEqual(1, len(artifacts))
            payload = json.loads(artifacts[0].read_text(encoding="utf-8"))
            self.assertTrue(payload["ok"])
            self.assertEqual(
                ["cli_help", "serve_ui_health", "query_and_report"],
                [item["name"] for item in payload["checks"]],
            )


if __name__ == "__main__":
    unittest.main()
