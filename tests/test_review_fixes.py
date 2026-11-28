import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from browser.browser_query_result import OfferDetails
from core.config_manager import RouteQuery
from web.dashboard_builder import build_task_groups
from core.data_storage import PriceRepository, PriceSnapshot
from providers import ctrip_network_parser, ctrip_result
from providers.response_capture import parse_json_or_jsonp
from web.web_ui import create_app


def _write_config(path: Path) -> None:
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
                    },
                    "feizhu": {
                        "enabled": True,
                        "timeout_ms": 5000,
                        "min_delay_seconds": 0,
                        "max_delay_seconds": 0,
                        "storage_state_file": "./runtime/feizhu_state.json",
                    },
                },
                "routes": [],
            }
        ),
        encoding="utf-8",
    )


class ArchitectureAndUiReviewFixTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("FLIGHT_TRACKER_UI_TOKEN", None)

    def test_ctrip_ordinary_query_populates_quote_terms_for_group_comparison(self) -> None:
        route = RouteQuery(origin="PEK", destination="SHA", departure_date="2099-05-01", passengers=1, providers=["ctrip"])
        response_store = [
            {
                "url": "https://flights.ctrip.com/international/search/api/search/batchSearch",
                "status": 200,
                "payload": {
                    "data": {
                        "flightItineraryList": [
                            {
                                "flightSegments": [
                                    {
                                        "flightList": [
                                            {
                                                "departureCityCode": "BJS",
                                                "departureAirportCode": "PEK",
                                                "arrivalCityCode": "SHA",
                                                "arrivalAirportCode": "SHA",
                                                "departureDateTime": "2099-05-01 08:00:00",
                                                "arrivalDateTime": "2099-05-01 10:15:00",
                                                "flightNo": "CA1501",
                                                "marketAirlineName": "中国国航",
                                            }
                                        ]
                                    }
                                ],
                                "priceList": [
                                    {
                                        "adultPrice": 620,
                                        "adultTax": 100,
                                        "cabin": "Y",
                                    }
                                ],
                            }
                        ]
                    }
                },
            }
        ]
        match = ctrip_network_parser.extract_single_route_match_from_network(response_store, route)
        self.assertIsNotNone(match)
        outcome = ctrip_result.build_single_route_outcome(
            route,
            OfferDetails(price=match["total_price"], matched_quote=match),
            "https://flights.ctrip.com/online/list/oneway-pek-sha",
            "Ctrip",
        )
        terms = outcome.extra_payload.get("quote_terms")
        self.assertIsInstance(terms, dict)
        self.assertEqual("per_person", terms["price_basis"])
        self.assertTrue(terms["tax_included"])
        self.assertEqual(1, terms["priced_passengers"])

        groups = build_task_groups([
            {
                "group_id": "g1", "group_label": "京沪比价", "route_display": "PEK->SHA",
                "origin": "PEK", "destination": "SHA", "latest_price": 720,
                "currency": "CNY", "passengers": 1, "cabin": "economy",
                "departure_date": "2099-05-01", "return_date": None,
                "route_type": "one_way", "transfer_policy": "any", "preferred_airlines": [],
                "quote_terms": terms,
            },
            {
                "group_id": "g1", "group_label": "京沪比价", "route_display": "PKX->PVG",
                "origin": "PKX", "destination": "PVG", "latest_price": 680,
                "currency": "CNY", "passengers": 1, "cabin": "economy",
                "departure_date": "2099-05-01", "return_date": None,
                "route_type": "one_way", "transfer_policy": "any", "preferred_airlines": [],
                "quote_terms": terms,
            },
        ])
        self.assertTrue(groups[0]["lowest_price_comparable"])
        self.assertEqual(680, groups[0]["lowest_price"])

    def test_run_once_preserves_404_and_returns_409_when_busy(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            _write_config(config_path)

            with TestClient(create_app(str(config_path))) as client:
                missing_route = client.post("/api/routes/NO_SUCH_ROUTE/run-once")
                self.assertEqual(404, missing_route.status_code)

                missing_provider = client.post("/api/run-once?provider=nonexistent")
                self.assertEqual(404, missing_provider.status_code)

                created = client.post(
                    "/api/routes",
                    json={
                        "origin": "SHA",
                        "destination": "PEK",
                        "departure_date": "2099-07-01",
                        "providers": ["ctrip"],
                    },
                )
                self.assertEqual(200, created.status_code)
                route_key = created.json()["route"]["route_key"]

                with patch("services.app_service.FlightPriceApplication.is_query_busy", return_value=True):
                    busy_all = client.post("/api/run-once")
                    busy_route = client.post(f"/api/routes/{route_key}/run-once")
                self.assertEqual(409, busy_all.status_code)
                self.assertEqual(409, busy_route.status_code)

    def test_session_logout_clears_ui_cookie(self) -> None:
        os.environ["FLIGHT_TRACKER_UI_TOKEN"] = "strong-logout-test-token"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            _write_config(config_path)

            with TestClient(create_app(str(config_path))) as client:
                login = client.post("/api/session", json={"token": "strong-logout-test-token"})
                self.assertEqual(200, login.status_code)
                self.assertEqual(200, client.get("/api/dashboard").status_code)

                logout = client.post("/api/session/logout")
                self.assertEqual(200, logout.status_code)
                self.assertEqual({"authenticated": False}, logout.json())
                self.assertEqual(401, client.get("/api/dashboard").status_code)

    def test_route_price_history_target_price_and_group_auto_query(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            _write_config(config_path)

            with TestClient(create_app(str(config_path))) as client:
                group_resp = client.post(
                    "/api/routes",
                    json={
                        "route_batch": True,
                        "group_label": "华东去北京",
                        "origin_options": ["SHA", "HGH"],
                        "destination_options": ["PEK"],
                        "departure_date": "2099-08-01",
                        "cabin": "economy",
                        "passengers": 1,
                        "target_price": 800,
                        "providers": ["ctrip"],
                    },
                )
                self.assertEqual(200, group_resp.status_code)
                body = group_resp.json()
                group_id = body["group_id"]
                route_key = body["routes"][0]["route_key"]
                self.assertEqual(800.0, body["routes"][0]["target_price"])

                toggle = client.post(f"/api/route-groups/{group_id}/auto-query", json={"enabled": True})
                self.assertEqual(200, toggle.status_code)
                self.assertEqual(2, toggle.json()["updated"])
                self.assertTrue(all(item["auto_query_enabled"] for item in toggle.json()["routes"]))

                repo = PriceRepository(Path(tmp_dir) / "runtime" / "test.db")
                repo.insert_snapshot(
                    PriceSnapshot(
                        provider="ctrip", route_key=route_key, origin="SHA", destination="PEK",
                        departure_date="2099-08-01", return_date=None, cabin="economy",
                        passengers=1, currency="CNY", price=950,
                        observed_at="2099-07-01T10:00:00Z", scraped_at="2099-07-01T10:00:00Z",
                        notes=[],
                        raw_payload={"search_url": "https://flights.ctrip.com/online/list/oneway-sha-pek"},
                    )
                )
                repo.insert_snapshot(
                    PriceSnapshot(
                        provider="ctrip", route_key=route_key, origin="SHA", destination="PEK",
                        departure_date="2099-08-01", return_date=None, cabin="economy",
                        passengers=1, currency="CNY", price=760,
                        observed_at="2099-07-02T10:00:00Z", scraped_at="2099-07-02T10:00:00Z",
                        notes=[],
                        raw_payload={"search_url": "https://flights.ctrip.com/online/list/oneway-sha-pek"},
                    )
                )

                history = client.get(f"/api/routes/{route_key}/history?provider=ctrip")
                self.assertEqual(200, history.status_code)
                hdata = history.json()
                self.assertEqual(2, hdata["stats"]["count"])
                self.assertEqual(760.0, hdata["stats"]["min_price"])
                self.assertEqual(950.0, hdata["stats"]["max_price"])
                self.assertEqual(800.0, hdata["target_price"])

                dashboard = client.get("/api/dashboard").json()
                task = next(item for item in dashboard["tasks"] if item["route_key"] == route_key)
                self.assertTrue(task["target_price_hit"])

    def test_parse_json_or_jsonp_helper_in_response_capture(self) -> None:
        self.assertEqual({"ok": True}, parse_json_or_jsonp('{"ok": true}'))
        self.assertEqual(
            {"data": {"adultPrice": 620}},
            parse_json_or_jsonp('mtopjsonp1({"data":{"adultPrice":620}});'),
        )
        self.assertIsNone(parse_json_or_jsonp("not-json"))
