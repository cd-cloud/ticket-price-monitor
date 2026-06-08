import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace

import browser_automation
from browser_automation import BrowserAutomation
from config_manager import RouteQuery
from provider_runtime import ProviderRuntime
from providers.base import ParsedProviderResult, ProviderQueryOutcome


class ProviderRuntimeTests(unittest.TestCase):
    def test_runtime_delegates_common_provider_operations(self) -> None:
        calls: list[tuple] = []

        class FakeAutomation:
            offer_details_type = tuple

            async def _throttle(self, provider):
                calls.append(("throttle", provider.name))

            async def _browser_use_assist_page(self, page, kind, instruction, context):
                calls.append(("assist", kind, context))

        runtime = ProviderRuntime(FakeAutomation())

        asyncio.run(runtime.throttle(SimpleNamespace(name="ctrip")))
        asyncio.run(runtime.assist_page(None, "kind", "instruction", {"x": 1}))

        self.assertEqual(
            [("throttle", "ctrip"), ("assist", "kind", {"x": 1})],
            calls,
        )
        self.assertIs(tuple, runtime.offer_details_type)

    def test_parsed_provider_result_converts_to_outcome_with_parser_metadata(self) -> None:
        parsed = ParsedProviderResult(
            provider="ctrip",
            price=880,
            final_url="https://example.test/results",
            title="results",
            parser="network",
            confidence="high",
            flight_details=[{"origin": "BJS", "destination": "CTU"}],
        )

        outcome = parsed.to_outcome()

        self.assertEqual(880, outcome.price)
        self.assertEqual("network", outcome.extra_payload["parser"])
        self.assertEqual("high", outcome.extra_payload["parser_confidence"])
        self.assertEqual("CTU", outcome.flight_details[0]["destination"])

    def test_browser_automation_passes_provider_runtime_to_handler(self) -> None:
        seen: list[object] = []

        class FakeHandler:
            async def query(self, runtime, provider, route, page, response_store):
                seen.append(runtime)
                return ProviderQueryOutcome(price=100, final_url="https://example.test", title="ok")

        original = dict(browser_automation.PROVIDER_HANDLERS)
        browser_automation.PROVIDER_HANDLERS["fake"] = FakeHandler()
        try:
            automation = BrowserAutomation(
                SimpleNamespace(
                    runtime_dir=Path("."),
                    automation_backend="playwright",
                    browser_backend="playwright",
                    browser_backend_fallbacks=[],
                    providers={},
                ),
                {},
            )
            provider = SimpleNamespace(name="fake")
            route = RouteQuery(origin="SHA", destination="PEK", departure_date="2026-06-01")

            result = asyncio.run(
                automation._query_provider_with_handler(provider, route, page=None, response_store=[])
            )
        finally:
            browser_automation.PROVIDER_HANDLERS.clear()
            browser_automation.PROVIDER_HANDLERS.update(original)

        self.assertEqual(100, result.price)
        self.assertEqual([automation.provider_runtime], seen)

    def test_runtime_builds_itinerary_summary_without_automation_delegate(self) -> None:
        route = RouteQuery(
            origin="BJS",
            destination="SIN",
            departure_date="2026-06-01",
            route_type="multi_city",
            segments=[
                SimpleNamespace(origin="BJS", destination="CTU", departure_date="2026-06-01"),
                SimpleNamespace(origin="CTU", destination="SIN", departure_date="2026-06-02"),
            ],
        )

        self.assertEqual(
            "BJS->CTU 2026-06-01 / CTU->SIN 2026-06-02",
            ProviderRuntime.build_itinerary_summary(route),
        )


if __name__ == "__main__":
    unittest.main()
