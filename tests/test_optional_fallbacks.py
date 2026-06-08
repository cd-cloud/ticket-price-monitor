import unittest

from browser_use_adapter import classify_page_state
from html_text_cleaner import clean_html_text
from tail_structured_extractor import model_to_dict, tail_itinerary_from_result


class OptionalFallbackTests(unittest.TestCase):
    def test_clean_html_text_keeps_visible_route_text(self) -> None:
        cleaned = clean_html_text("<html><script>x()</script><body><h1>Flight</h1><p>BJS to CTU</p></body></html>")

        self.assertIn("BJS to CTU", cleaned["text"])
        self.assertNotIn("x()", cleaned["text"])

    def test_structured_tail_itinerary_from_result(self) -> None:
        itinerary = tail_itinerary_from_result(
            {
                "origin": "BJS",
                "transfer": "CTU",
                "destination": "CAN",
                "departure_date": "2026-06-01",
                "cabin": "economy_plus",
                "price": 880,
                "currency": "CNY",
                "validation": {"status": "valid"},
                "raw_payload": {"detail_source": "network_exact", "detail_quality": "complete"},
                "flight_details": [
                    {
                        "segment_index": 1,
                        "origin": "BJS",
                        "destination": "CTU",
                        "departure_date": "2026-06-01",
                        "departure_time": "07:30",
                        "flight_no": "CA1405",
                    },
                    {
                        "segment_index": 2,
                        "origin": "CTU",
                        "destination": "CAN",
                        "departure_date": "2026-06-01",
                        "departure_time": "11:30",
                        "flight_no": "CA4301",
                    },
                ],
            }
        )
        data = model_to_dict(itinerary)

        self.assertEqual(data["origin"], "BJS")
        self.assertEqual(data["validation_status"], "valid")
        self.assertEqual(len(data["legs"]), 2)
        self.assertGreater(data["legs"][0]["confidence"], 0.9)

    def test_browser_use_page_state_classifier(self) -> None:
        state = classify_page_state(text="city airport dropdown search", url="https://flights.ctrip.com", title="Ctrip")

        self.assertEqual(state["primary"], "city_picker")
        self.assertIn("city_picker", state["states"])


if __name__ == "__main__":
    unittest.main()
