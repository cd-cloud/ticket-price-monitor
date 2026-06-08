import json
import tempfile
import unittest
from pathlib import Path

from config_manager import ConfigManager
from route_service import RouteService


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


class RouteServiceTests(unittest.TestCase):
    def make_service(self, config_path: Path):
        manager = ConfigManager(config_path=config_path)
        config = manager.load()
        rebalanced: list[list[str]] = []

        def refresh() -> None:
            nonlocal config
            config = manager.load()

        def rebalance(routes) -> None:
            rebalanced.append([route.route_key for route in routes])

        service = RouteService(
            config_manager=manager,
            get_config=lambda: config,
            refresh_config=refresh,
            rebalance_auto_query=rebalance,
            auto_query_interval_hours=12,
        )
        return service, rebalanced

    def test_upsert_route_normalizes_and_persists_one_way_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)
            service, rebalanced = self.make_service(config_path)

            result = service.upsert_route(
                {
                    "origin": " sha ",
                    "destination": "pek",
                    "departure_date": "2026-06-01",
                    "cabin": "Business",
                    "passengers": 2,
                    "providers": ["ctrip"],
                    "preferred_airlines": [" ca "],
                }
            )

            route = result["route"]
            self.assertTrue(result["saved"])
            self.assertEqual(route["origin"], "SHA")
            self.assertEqual(route["destination"], "PEK")
            self.assertEqual(route["cabin"], "business")
            self.assertEqual(route["preferred_airlines"], ["CA"])
            self.assertEqual(len(service.list_routes()), 1)
            self.assertEqual(len(rebalanced[-1]), 1)

    def test_update_auto_query_preserves_route_and_clears_next_run_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)
            service, _rebalanced = self.make_service(config_path)
            route = service.upsert_route(
                {
                    "origin": "SHA",
                    "destination": "PEK",
                    "departure_date": "2026-06-01",
                    "providers": ["ctrip"],
                    "auto_query_enabled": True,
                    "auto_query_next_run_at": "2026-06-01T00:00:00Z",
                }
            )["route"]

            disabled = service.update_auto_query(route["route_key"], False)["route"]

            self.assertFalse(disabled["auto_query_enabled"])
            self.assertIsNone(disabled["auto_query_next_run_at"])
            self.assertEqual(disabled["origin"], "SHA")

    def test_upsert_route_rejects_same_origin_and_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)
            service, _rebalanced = self.make_service(config_path)

            with self.assertRaisesRegex(ValueError, "cannot be the same"):
                service.upsert_route(
                    {
                        "origin": "SHA",
                        "destination": "SHA",
                        "departure_date": "2026-06-01",
                        "providers": ["ctrip"],
                    }
                )

    def test_upsert_route_rejects_disabled_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            payload["providers"]["priceline"] = {
                "enabled": False,
                "login_url": "https://example.test/login",
                "search_url_template": "https://example.test/search",
                "storage_state_file": "./runtime/priceline_state.json",
            }
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            service, _rebalanced = self.make_service(config_path)

            with self.assertRaisesRegex(ValueError, "disabled"):
                service.upsert_route(
                    {
                        "origin": "SHA",
                        "destination": "PEK",
                        "departure_date": "2026-06-01",
                        "providers": ["priceline"],
                    }
                )

    def test_upsert_route_rejects_provider_without_handler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            payload["providers"]["airchina"] = {
                "enabled": True,
                "login_url": "https://example.test/login",
                "search_url_template": "https://example.test/search",
                "storage_state_file": "./runtime/airchina_state.json",
            }
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            service, _rebalanced = self.make_service(config_path)

            with self.assertRaisesRegex(ValueError, "not available"):
                service.upsert_route(
                    {
                        "origin": "SHA",
                        "destination": "PEK",
                        "departure_date": "2026-06-01",
                        "providers": ["airchina"],
                    }
                )

    def test_upsert_route_accepts_direct_only_transfer_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)
            service, _rebalanced = self.make_service(config_path)

            result = service.upsert_route(
                {
                    "origin": "SHA",
                    "destination": "PEK",
                    "departure_date": "2026-06-01",
                    "transfer_policy": "direct_only",
                    "providers": ["ctrip"],
                }
            )

            self.assertEqual("direct_only", result["route"]["transfer_policy"])

    def test_upsert_route_maps_legacy_direct_transfer_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)
            service, _rebalanced = self.make_service(config_path)

            result = service.upsert_route(
                {
                    "origin": "SHA",
                    "destination": "PEK",
                    "departure_date": "2026-06-01",
                    "transfer_policy": "direct",
                    "providers": ["ctrip"],
                }
            )

            self.assertEqual("direct_only", result["route"]["transfer_policy"])

    def test_upsert_multi_city_rejects_decreasing_segment_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)
            service, _rebalanced = self.make_service(config_path)

            with self.assertRaisesRegex(ValueError, "non-decreasing"):
                service.upsert_route(
                    {
                        "route_type": "multi_city",
                        "cabin": "economy",
                        "providers": ["ctrip"],
                        "segments": [
                            {"origin": "SHA", "destination": "PEK", "departure_date": "2026-06-02"},
                            {"origin": "PEK", "destination": "SIN", "departure_date": "2026-06-01"},
                        ],
                    }
                )

    def test_upsert_route_batch_expands_and_persists_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)
            service, rebalanced = self.make_service(config_path)

            result = service.upsert_route(
                {
                    "route_batch": True,
                    "route_type": "one_way",
                    "origin_options": ["SEL", "CJU"],
                    "destination_options": ["PAR", "BCN"],
                    "departure_date": "2026-06-01",
                    "return_date": "2026-06-12",
                    "providers": ["ctrip"],
                    "group_label": "Korea Europe return",
                }
            )

            self.assertTrue(result["saved"])
            self.assertEqual(4, result["created"])
            routes = service.list_routes()
            self.assertEqual(4, len(routes))
            self.assertEqual({"Korea Europe return"}, {route["group_label"] for route in routes})
            self.assertEqual(4, len(rebalanced[-1]))

    def test_upsert_route_batch_expands_multi_city_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)
            service, rebalanced = self.make_service(config_path)

            result = service.upsert_route(
                {
                    "route_batch": True,
                    "route_type": "multi_city",
                    "segments": [
                        {
                            "origin_options": ["SEL", "PUS"],
                            "destination_options": ["BJS"],
                            "departure_date": "2026-06-01",
                        },
                        {
                            "origin_options": ["BJS"],
                            "destination_options": ["ATH", "MIL"],
                            "departure_date": "2026-06-05",
                        },
                    ],
                    "providers": ["ctrip"],
                    "group_label": "Korea Beijing Europe",
                }
            )

            self.assertTrue(result["saved"])
            self.assertEqual(4, result["created"])
            routes = service.list_routes()
            self.assertEqual(4, len(routes))
            self.assertEqual({"multi_city"}, {route["route_type"] for route in routes})
            self.assertEqual({"Korea Beijing Europe"}, {route["group_label"] for route in routes})
            self.assertEqual({2}, {len(route["segments"]) for route in routes})
            self.assertEqual({"SEL", "PUS"}, {route["segments"][0]["origin"] for route in routes})
            self.assertEqual({"ATH", "MIL"}, {route["segments"][1]["destination"] for route in routes})
            self.assertEqual(4, len(rebalanced[-1]))

    def test_upsert_route_batch_replaces_previous_multi_city_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)
            service, _rebalanced = self.make_service(config_path)

            first = service.upsert_route(
                {
                    "route_batch": True,
                    "route_type": "multi_city",
                    "segments": [
                        {"origin_options": ["SEL", "PUS"], "destination_options": ["BJS"], "departure_date": "2026-06-01"},
                        {"origin_options": ["BJS"], "destination_options": ["ATH", "MIL"], "departure_date": "2026-06-05"},
                    ],
                    "providers": ["ctrip"],
                    "group_label": "Korea Beijing Europe",
                }
            )
            second = service.upsert_route(
                {
                    "previous_group_id": first["group_id"],
                    "route_batch": True,
                    "route_type": "multi_city",
                    "segments": [
                        {"origin_options": ["CJU"], "destination_options": ["BJS"], "departure_date": "2026-06-01"},
                        {"origin_options": ["BJS"], "destination_options": ["PAR"], "departure_date": "2026-06-05"},
                    ],
                    "providers": ["ctrip"],
                    "group_label": "Updated group",
                }
            )

            self.assertTrue(second["saved"])
            routes = service.list_routes()
            self.assertEqual(1, len(routes))
            self.assertEqual("CJU", routes[0]["segments"][0]["origin"])
            self.assertEqual("PAR", routes[0]["segments"][1]["destination"])
            self.assertEqual("Updated group", routes[0]["group_label"])

    def test_upsert_route_batch_accepts_single_city_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)
            service, _rebalanced = self.make_service(config_path)

            result = service.upsert_route(
                {
                    "route_type": "one_way",
                    "origin_options": ["BJS"],
                    "destination_options": ["SHA"],
                    "departure_date": "2026-06-01",
                    "providers": ["ctrip"],
                    "group_label": "Single option group",
                }
            )

            self.assertTrue(result["saved"])
            self.assertEqual(1, result["created"])
            self.assertEqual("Single option group", result["group_label"])
            self.assertEqual("BJS", result["routes"][0]["origin"])
            self.assertEqual("SHA", result["routes"][0]["destination"])

    def test_delete_route_group_removes_all_expanded_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            write_config(config_path)
            service, _rebalanced = self.make_service(config_path)

            result = service.upsert_route(
                {
                    "route_batch": True,
                    "route_type": "one_way",
                    "origin_options": ["SEL", "CJU"],
                    "destination_options": ["PAR", "BCN"],
                    "departure_date": "2026-06-01",
                    "providers": ["ctrip"],
                    "group_label": "Korea Europe",
                }
            )

            self.assertTrue(service.delete_route_group(result["group_id"]))
            self.assertEqual([], service.list_routes())


if __name__ == "__main__":
    unittest.main()
