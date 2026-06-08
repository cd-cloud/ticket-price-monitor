import unittest

from config_manager import RouteQuery
from dashboard_builder import extract_flight_details, extract_flight_number
from providers import ctrip_parser


ROW_TEXT = "Y87595\u00a0波音737(中) | 当日低价 | 15:35 | 浦东国际机场T2 | 17:55 | 首都国际机场T2 | ¥400起"


class CtripSingleDetailsTests(unittest.TestCase):
    def test_parses_one_letter_one_digit_flight_number(self) -> None:
        self.assertEqual("Y87595", extract_flight_number(ROW_TEXT))

    def test_dashboard_details_fall_back_to_one_way_route_fields(self) -> None:
        details = extract_flight_details(
            ROW_TEXT,
            segments=[],
            cabin="economy",
            origin="SHA",
            destination="PEK",
            departure_date="2026-05-20",
        )

        self.assertEqual(1, len(details))
        self.assertEqual("SHA", details[0]["origin"])
        self.assertEqual("PEK", details[0]["destination"])
        self.assertEqual("2026-05-20", details[0]["departure_date"])
        self.assertEqual("Y87595", details[0]["flight_no"])
        self.assertEqual("15:35", details[0]["departure_time"])
        self.assertEqual("浦东国际机场T2", details[0]["departure_airport"])
        self.assertEqual("17:55", details[0]["arrival_time"])
        self.assertEqual("首都国际机场T2", details[0]["arrival_airport"])

    def test_provider_details_are_written_to_raw_payload(self) -> None:
        route = RouteQuery(
            origin="SHA",
            destination="PEK",
            departure_date="2026-05-20",
            cabin="economy",
        )

        details = ctrip_parser.parse_single_flight_details(ROW_TEXT, route)

        self.assertEqual("Y87595", details[0]["flight_no"])
        self.assertEqual("SHA", details[0]["origin"])
        self.assertEqual("PEK", details[0]["destination"])


if __name__ == "__main__":
    unittest.main()
