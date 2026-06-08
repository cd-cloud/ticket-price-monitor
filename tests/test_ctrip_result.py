import unittest
from types import SimpleNamespace

from config_manager import RouteQuery
from providers import ctrip_result


class CtripResultTests(unittest.TestCase):
    def test_round_trip_visible_single_leg_is_marked_partial(self) -> None:
        route = RouteQuery(
            origin="SEL",
            destination="PAR",
            departure_date="2026-06-20",
            return_date="2026-06-27",
            cabin="economy",
        )
        offer = SimpleNamespace(
            price=5561,
            departure_time="10:10",
            row_text=(
                "韩国德威航空 | TW401 空客330(大) | 10:10 | 仁川国际机场T1 | "
                "18:10 | 戴高乐机场T1 | 15小时航班详情 | ¥5561起"
            ),
        )

        outcome = ctrip_result.build_single_route_outcome(route, offer, "https://example.test", "title", [])

        self.assertEqual("partial", outcome.extra_payload["detail_quality"])
        self.assertEqual("visible_row", outcome.extra_payload["detail_source"])
        self.assertEqual("ctrip_single_visible", outcome.extra_payload["parser"])
        self.assertEqual("medium", outcome.extra_payload["parser_confidence"])

    def test_low_price_calendar_match_is_price_only(self) -> None:
        route = RouteQuery(
            origin="PUS",
            destination="BCN",
            departure_date="2026-11-14",
            return_date="2026-11-20",
            cabin="economy_plus",
        )
        offer = SimpleNamespace(price=4181, departure_time=None, row_text=None)
        response_store = [
            {
                "payload": {
                    "priceList": [
                        {
                            "departDate": "/Date(1794614400000+0800)/",
                            "returnDate": "/Date(1795132800000+0800)/",
                            "totalPrice": 4181,
                        }
                    ]
                }
            }
        ]

        outcome = ctrip_result.build_single_route_outcome(
            route,
            offer,
            "https://example.test",
            "title",
            response_store,
        )

        self.assertEqual("price_only", outcome.extra_payload["detail_quality"])
        self.assertEqual("network_low_price_calendar", outcome.extra_payload["detail_source"])
        self.assertEqual("low_price_calendar", outcome.extra_payload["network_match_source"])
        self.assertEqual("low", outcome.extra_payload["parser_confidence"])


if __name__ == "__main__":
    unittest.main()
