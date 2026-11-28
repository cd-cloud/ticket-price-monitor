import json
import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from pathlib import Path

from core.config_manager import ProviderConfig, RouteQuery
from web.dashboard_builder import build_dashboard_payload, build_task_groups


class DashboardBuilderTests(unittest.TestCase):
    def test_dashboard_includes_price_change_direction_for_latest_snapshot(self) -> None:
        route = RouteQuery(
            origin="PEK",
            destination="SHA",
            departure_date="2099-01-01",
            providers=["ctrip"],
        )
        config = SimpleNamespace(
            routes=[route],
            providers={
                "ctrip": ProviderConfig(
                    name="ctrip",
                    enabled=True,
                    login_url="https://example.test/login",
                    search_url_template="https://example.test/search",
                    storage_state_file=Path("state.json"),
                    selectors={},
                    timeout_ms=1000,
                    extra_headers={},
                    min_delay_seconds=0,
                    max_delay_seconds=0,
                    locale="zh-CN",
                    timezone="Asia/Shanghai",
                )
            },
            default_currency="CNY",
        )

        payload = build_dashboard_payload(
            config=config,
            routes=[],
            snapshots=[
                {
                    "provider": "ctrip",
                    "route_key": route.route_key,
                    "price": 1200,
                    "currency": "CNY",
                    "observed_at": "2026-05-16T10:00:00",
                    "scraped_at": "2026-05-16T10:00:00",
                    "raw_payload": {"search_url": "https://flights.ctrip.com/online/list/oneway-pek0-sha0"},
                },
                {
                    "provider": "ctrip",
                    "route_key": route.route_key,
                    "price": 980,
                    "currency": "CNY",
                    "observed_at": "2026-05-17T10:00:00",
                    "scraped_at": "2026-05-17T10:00:00",
                    "raw_payload": {"search_url": "https://flights.ctrip.com/online/list/oneway-pek0-sha0"},
                },
            ],
            report_paths={},
            last_run_summary={"errors": []},
            auto_query_interval_hours=12,
            auto_query_min_gap_minutes=30,
        )

        task = payload["tasks"][0]
        self.assertEqual(980, task["latest_price"])
        self.assertEqual(1200, task["previous_price"])
        self.assertEqual(-220, task["latest_price_delta_amount"])
        self.assertEqual("down", task["latest_price_delta_direction"])

    def test_error_status_hides_stale_latest_price(self) -> None:
        departure = date.today() + timedelta(days=30)
        route = RouteQuery(
            origin="PUS",
            destination="BCN",
            departure_date=departure.isoformat(),
            return_date=(departure + timedelta(days=6)).isoformat(),
            providers=["ctrip"],
        )
        config = SimpleNamespace(
            routes=[route],
            providers={
                "ctrip": ProviderConfig(
                    name="ctrip",
                    enabled=True,
                    login_url="https://example.test/login",
                    search_url_template="https://example.test/search",
                    storage_state_file=Path("state.json"),
                    selectors={},
                    timeout_ms=1000,
                    extra_headers={},
                    min_delay_seconds=0,
                    max_delay_seconds=0,
                    locale="zh-CN",
                    timezone="Asia/Shanghai",
                )
            },
            default_currency="CNY",
        )

        payload = build_dashboard_payload(
            config=config,
            routes=[],
            snapshots=[
                {
                    "provider": "ctrip",
                    "route_key": route.route_key,
                    "price": 236,
                    "currency": "CNY",
                    "observed_at": "2026-05-16T10:00:00",
                    "scraped_at": "2026-05-16T10:00:00",
                    "raw_payload": {"search_url": "https://flights.ctrip.com/online/list/round-pus0-bcn0"},
                }
            ],
            report_paths={},
            last_run_summary={
                "errors": [
                    f"provider=ctrip route={route.route_key} failed: Ctrip returned no flights."
                ]
            },
            auto_query_interval_hours=12,
            auto_query_min_gap_minutes=30,
            session_hints=[{"provider": "ctrip", "needs_login": True}],
        )

        task = payload["tasks"][0]
        self.assertEqual("error", task["status"])
        self.assertIsNone(task["latest_price"])
        self.assertEqual(236, task["stale_latest_price"])
        self.assertEqual([{"provider": "ctrip", "needs_login": True}], payload["session_hints"])

    def test_dashboard_excludes_providers_without_handlers_and_marks_expired_routes(self) -> None:
        route = RouteQuery(
            origin="PEK",
            destination="SHA",
            departure_date="2026-05-01",
            providers=["ctrip", "airchina"],
        )
        provider = ProviderConfig(
            name="ctrip",
            enabled=True,
            login_url="https://example.test/login",
            search_url_template="https://example.test/search",
            storage_state_file=Path("state.json"),
            selectors={},
            timeout_ms=1000,
            extra_headers={},
            min_delay_seconds=0,
            max_delay_seconds=0,
            locale="zh-CN",
            timezone="Asia/Shanghai",
        )
        config = SimpleNamespace(
            routes=[route],
            providers={
                "ctrip": provider,
                "airchina": ProviderConfig(
                    name="airchina",
                    enabled=True,
                    login_url="https://example.test/login",
                    search_url_template="https://example.test/search",
                    storage_state_file=Path("airchina.json"),
                    selectors={},
                    timeout_ms=1000,
                    extra_headers={},
                    min_delay_seconds=0,
                    max_delay_seconds=0,
                    locale="zh-CN",
                    timezone="Asia/Shanghai",
                ),
            },
            default_currency="CNY",
        )

        payload = build_dashboard_payload(
            config=config,
            routes=[],
            snapshots=[],
            report_paths={},
            last_run_summary={"errors": []},
            auto_query_interval_hours=12,
            auto_query_min_gap_minutes=30,
        )

        self.assertEqual(["ctrip"], payload["provider_options"])
        self.assertEqual(1, len(payload["tasks"]))
        self.assertEqual("ctrip", payload["tasks"][0]["provider"])
        self.assertEqual("expired", payload["tasks"][0]["status"])
        self.assertTrue(payload["tasks"][0]["is_expired"])

    def test_provider_error_does_not_hide_other_provider_snapshot(self) -> None:
        route = RouteQuery(
            origin="SHA", destination="PEK", departure_date="2099-01-01",
            providers=["ctrip", "feizhu"],
        )
        config = SimpleNamespace(
            routes=[route],
            providers={
                "ctrip": SimpleNamespace(enabled=True),
                "feizhu": SimpleNamespace(enabled=True),
            },
            default_currency="CNY", timezone="Asia/Shanghai",
        )
        snapshots = [{
            "provider": provider, "route_key": route.route_key, "price": 500,
            "currency": "CNY", "observed_at": "2099-01-01T00:00:00Z",
            "scraped_at": "2099-01-01T00:00:00Z",
            "raw_payload": {"search_url": "https://flights.ctrip.com/online/list/x"},
        } for provider in ("ctrip", "feizhu")]
        payload = build_dashboard_payload(
            config=config, routes=[], snapshots=snapshots, report_paths={},
            last_run_summary={"errors": [
                f"provider=feizhu route={route.route_key} failed: boom"
            ]},
            auto_query_interval_hours=12, auto_query_min_gap_minutes=10,
        )
        tasks = {task["provider"]: task for task in payload["tasks"]}

        self.assertEqual("ok", tasks["ctrip"]["status"])
        self.assertEqual(500, tasks["ctrip"]["latest_price"])
        self.assertEqual("error", tasks["feizhu"]["status"])
        self.assertIsNone(tasks["feizhu"]["latest_price"])

    def test_task_group_does_not_compare_different_currencies(self) -> None:
        groups = build_task_groups([
            {
                "group_id": "g", "group_label": "G", "route_display": "A",
                "origin": "AAA", "destination": "BBB", "latest_price": 100,
                "currency": "USD", "passengers": 1, "cabin": "economy",
            },
            {
                "group_id": "g", "group_label": "G", "route_display": "B",
                "origin": "CCC", "destination": "DDD", "latest_price": 1000,
                "currency": "CNY", "passengers": 1, "cabin": "economy",
            },
        ])

        self.assertIsNone(groups[0]["lowest_price"])
        self.assertFalse(groups[0]["lowest_price_comparable"])
        self.assertEqual(["CNY", "USD"], groups[0]["currencies"])

    def test_task_group_comparison_requires_dates_filters_and_quote_terms(self) -> None:
        base = {
            "group_id": "g", "group_label": "G", "route_display": "A",
            "origin": "AAA", "destination": "BBB", "latest_price": 100,
            "currency": "CNY", "passengers": 1, "cabin": "economy",
            "departure_date": "2099-01-01", "return_date": None,
            "route_type": "one_way", "transfer_policy": "any", "preferred_airlines": [],
            "quote_terms": {"price_basis": "per_person", "tax_included": True, "priced_passengers": 1},
        }
        different_date = build_task_groups([
            base,
            {**base, "route_display": "B", "latest_price": 90, "departure_date": "2099-01-02"},
        ])[0]
        self.assertFalse(different_date["lowest_price_comparable"])

        missing_terms = build_task_groups([
            base,
            {**base, "route_display": "B", "latest_price": 90, "quote_terms": None},
        ])[0]
        self.assertFalse(missing_terms["lowest_price_comparable"])

        comparable = build_task_groups([
            base,
            {**base, "route_display": "B", "latest_price": 90},
        ])[0]
        self.assertTrue(comparable["lowest_price_comparable"])
        self.assertEqual(90, comparable["lowest_price"])

    def test_dashboard_sanitizes_derived_tasks_and_run_feedback(self) -> None:
        route = RouteQuery("AAA", "BBB", "2099-01-01", providers=["ctrip"])
        config = SimpleNamespace(
            routes=[route],
            providers={"ctrip": SimpleNamespace(enabled=True)},
            default_currency="CNY",
            timezone="Asia/Shanghai",
        )
        payload = build_dashboard_payload(
            config=config,
            routes=[],
            snapshots=[{
                "provider": "ctrip", "route_key": route.route_key, "price": 100,
                "currency": "CNY", "observed_at": "2099-01-01T00:00:00Z",
                "scraped_at": "2099-01-01T00:00:00Z",
                "raw_payload": {
                    "itinerary_summary": "Bearer TASK_SECRET",
                    "segments": [{
                        "origin": "https://x.test/?token=SEGMENT_SECRET",
                        "destination": "BBB",
                        "departure_date": "2099-01-01",
                    }],
                },
            }],
            report_paths={},
            last_run_summary={"errors": ["https://x.test/?token=RUN_SECRET"]},
            auto_query_interval_hours=12,
            auto_query_min_gap_minutes=10,
        )
        serialized = json.dumps(payload)
        self.assertNotIn("TASK_SECRET", serialized)
        self.assertNotIn("SEGMENT_SECRET", serialized)
        self.assertNotIn("RUN_SECRET", serialized)


if __name__ == "__main__":
    unittest.main()
