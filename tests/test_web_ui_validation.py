import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from web_ui import create_app


def write_config(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "storage": {
                    "runtime_dir": "./runtime",
                    "output_dir": "./output",
                    "database_path": "./runtime/test.db",
                    "encrypted_credentials_path": "./runtime/credentials.enc.json",
                    "salt_file": "./runtime/master.salt",
                    "log_file": "./runtime/test.log",
                },
                "defaults": {"locale": "zh-CN", "currency": "CNY"},
                "scheduler": {"timezone": "Asia/Shanghai"},
                "providers": {
                    "ctrip": {
                        "enabled": True,
                        "login_url": "https://example.test/login",
                        "search_url_template": "https://example.test/{origin}-{destination}",
                        "storage_state_file": "./runtime/ctrip_state.json",
                    }
                },
                "routes": [],
            }
        ),
        encoding="utf-8",
    )


class WebUiValidationTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("FLIGHT_TRACKER_UI_TOKEN", None)

    def test_health_endpoint_is_lightweight(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)

            with TestClient(create_app(str(config_path))) as client:
                response = client.get("/api/health")

        self.assertEqual(200, response.status_code)
        self.assertEqual({"ok": True}, response.json())

    def test_invalid_route_payload_returns_400(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)

            with TestClient(create_app(str(config_path))) as client:
                response = client.post(
                    "/api/routes",
                    json={
                        "origin": "SHA",
                        "destination": "SHA",
                        "departure_date": "2027-06-01",
                        "providers": ["ctrip"],
                    },
                )

        self.assertEqual(400, response.status_code)
        self.assertIn("cannot be the same", response.json()["detail"])

    def test_return_date_must_be_later_than_departure_date(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)

            with TestClient(create_app(str(config_path))) as client:
                response = client.post(
                    "/api/routes",
                    json={
                        "origin": "SHA",
                        "destination": "PEK",
                        "departure_date": "2027-07-01",
                        "return_date": "2027-07-01",
                        "providers": ["ctrip"],
                    },
                )

        self.assertEqual(400, response.status_code)
        self.assertIn("must be later", response.json()["detail"])

    def test_browser_session_diagnostics_endpoint(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)

            with TestClient(create_app(str(config_path))) as client:
                response = client.get("/api/browser-session/diagnostics")

        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual("chrome", data["default_backend"])
        self.assertEqual("ctrip", data["providers"][0]["provider"])
        self.assertIn("chrome", data["providers"][0]["backend_order"])

    def test_browser_profiles_endpoint_and_switch(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)

            with TestClient(create_app(str(config_path))) as client:
                response = client.get("/api/browser-profiles")
                switch_response = client.post("/api/browser-profiles/ctrip/switch", json={"profile": "recovery"})
                switched = client.get("/api/browser-profiles")

        self.assertEqual(200, response.status_code)
        self.assertEqual(200, switch_response.status_code)
        self.assertEqual("ctrip", response.json()["providers"][0]["provider"])
        self.assertEqual("recovery", switched.json()["providers"][0]["chrome_profile"]["forced_profile"])

    def test_browser_profile_clean_endpoint_can_switch_to_recovery(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)
            profile_dir = Path(tmp_dir) / "runtime" / "browser_profiles" / "chrome" / "ctrip-v2" / "Default"
            profile_dir.mkdir(parents=True)
            (profile_dir / "Cookies").write_text("cookies", encoding="utf-8")

            with TestClient(create_app(str(config_path))) as client:
                response = client.post(
                    "/api/browser-profiles/ctrip/clean",
                    json={"profile": "primary", "switch_to_recovery": True},
                )
                profiles = client.get("/api/browser-profiles")

        self.assertEqual(200, response.status_code)
        self.assertFalse((profile_dir / "Cookies").exists())
        self.assertEqual("recovery", profiles.json()["providers"][0]["chrome_profile"]["forced_profile"])

    def test_mutating_endpoint_rejects_non_local_origin(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)

            with TestClient(create_app(str(config_path))) as client:
                response = client.post(
                    "/api/routes",
                    headers={"Origin": "https://example.test"},
                    json={
                        "origin": "SHA",
                        "destination": "PEK",
                        "departure_date": "2027-06-01",
                        "providers": ["ctrip"],
                    },
                )

        self.assertEqual(403, response.status_code)

    def test_mutating_endpoint_accepts_configured_ui_token(self) -> None:
        os.environ["FLIGHT_TRACKER_UI_TOKEN"] = "test-token"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)

            with TestClient(create_app(str(config_path))) as client:
                missing = client.post(
                    "/api/routes",
                    json={
                        "origin": "SHA",
                        "destination": "PEK",
                        "departure_date": "2027-06-01",
                        "providers": ["ctrip"],
                    },
                )
                accepted = client.post(
                    "/api/routes",
                    headers={"X-Flight-Tracker-Token": "test-token"},
                    json={
                        "origin": "SHA",
                        "destination": "PEK",
                        "departure_date": "2027-06-01",
                        "providers": ["ctrip"],
                    },
                )

        self.assertEqual(401, missing.status_code)
        self.assertEqual(200, accepted.status_code)


if __name__ == "__main__":
    unittest.main()

class TailApiValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "config.json"
        write_config(path)
        self.client = TestClient(create_app(str(path)))
        self.addCleanup(self.client.close)

    def test_invalid_tail_request_returns_400_without_starting_job(self):
        response = self.client.post("/api/tail-discovery/run", json={"origin": "BJS", "transfer": "BJS"})
        self.assertEqual(400, response.status_code)
        self.assertEqual([], self.client.get("/api/tail-discovery/jobs").json()["jobs"])

    def test_unknown_recheck_returns_404(self):
        self.assertEqual(404, self.client.post("/api/tail-discovery/results/999/recheck").status_code)

    def test_unknown_resume_returns_404(self):
        self.assertEqual(404, self.client.post("/api/tail-discovery/jobs/unknown/restart", json={"mode": "resume"}).status_code)

    def test_recheck_dispatch_and_busy_error(self):
        from unittest.mock import patch
        with patch("app_service.FlightPriceApplication.recheck_tail_result", return_value={"job_id": "j", "status": "queued"}) as fn:
            response = self.client.post("/api/tail-discovery/results/5/recheck")
            self.assertEqual(200, response.status_code)
            fn.assert_called_once_with(5)
        with patch("app_service.FlightPriceApplication.recheck_tail_result", side_effect=RuntimeError("busy")):
            self.assertEqual(409, self.client.post("/api/tail-discovery/results/5/recheck").status_code)

    def test_open_destination_validation_and_dispatch(self):
        from unittest.mock import patch
        from tail_service import OPEN_DESTINATION, normalize_tail_discovery_request
        request = {"provider": "ctrip", "origin": "BJS", "transfer": "CGO",
                   "departure_date": "2027-06-20", "search_scope": "open", "candidates": []}
        self.assertEqual(400, self.client.post("/api/tail-discovery/run", json=request).status_code)
        # Enable Feizhu in the isolated app config; mock only job submission, never validation.
        path = Path(self.temp.name) / "config.json"
        config = json.loads(path.read_text())
        config["providers"]["feizhu"] = {**config["providers"]["ctrip"],
                                        "storage_state_file": "./runtime/feizhu_state.json"}
        path.write_text(json.dumps(config))
        with TestClient(create_app(str(path))) as client, \
             patch("tail_jobs.TailDiscoveryJobManager.submit",
                   return_value={"job_id": "open-test", "status": "queued"}) as submit:
            request["provider"] = "feizhu"
            response = client.post("/api/tail-discovery/run", json=request)
            self.assertEqual(200, response.status_code, response.text)
            submitted = submit.call_args.args[0]
            self.assertEqual(normalize_tail_discovery_request(submitted).candidates, [OPEN_DESTINATION])
