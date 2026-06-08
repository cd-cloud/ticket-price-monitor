import unittest

from config_manager import RouteQuery, RouteSegment
from providers import ctrip_itinerary


class CtripItineraryTests(unittest.TestCase):
    def test_builds_multi_city_details_from_luggage_visa_key(self) -> None:
        price = {
            "adultPrice": 1200,
            "adultTax": 150,
            "priceUnitList": [
                {
                    "flightSeatList": [
                        {"segmentNo": 1, "sequenceNo": 1, "cabinClass": "Y", "seatClass": "M"},
                        {"segmentNo": 2, "sequenceNo": 1, "cabinClass": "C", "seatClass": "J"},
                    ]
                }
            ],
            "luggageVisaKey": (
                '{"criteria":{"FlightInfoList":['
                '{"SegmentNo":1,"SequenceNo":1,"MarketingCarrierCode":"CA","MarketingFlightNo":"123",'
                '"TakeOffDateTime":"2026-06-01 08:30:00","ArrivalDateTime":"2026-06-01 10:10:00",'
                '"DepartureAirportCode":"PEK","ArrivalAirportCode":"TFU","DepartureCityCode":"BJS","ArrivalCityCode":"CTU"},'
                '{"SegmentNo":2,"SequenceNo":1,"MarketingCarrierCode":"CA","MarketingFlightNo":"456",'
                '"TakeOffDateTime":"2026-06-02 09:00:00","ArrivalDateTime":"2026-06-02 11:45:00",'
                '"DepartureAirportCode":"TFU","ArrivalAirportCode":"SIN","DepartureCityCode":"CTU","ArrivalCityCode":"SIN"}'
                ']}}'
            ),
        }

        details = ctrip_itinerary.build_itinerary_details({}, price)

        self.assertEqual(1350, ctrip_itinerary.total_price(price))
        self.assertEqual(2, len(details))
        self.assertEqual("economy", details[0]["cabin"])
        self.assertEqual("business", details[1]["cabin"])
        self.assertEqual("08:30", details[0]["departure_time"])

    def test_detects_direct_multi_city_details(self) -> None:
        route = RouteQuery(
            origin="BJS",
            destination="SIN",
            departure_date="2026-06-01",
            route_type="multi_city",
            segments=[
                RouteSegment(origin="BJS", destination="CTU", departure_date="2026-06-01"),
                RouteSegment(origin="CTU", destination="SIN", departure_date="2026-06-02"),
            ],
        )
        details = [
            {"segment_index": 1, "origin": "BJS", "destination": "CTU", "departure_date": "2026-06-01"},
            {"segment_index": 2, "origin": "CTU", "destination": "SIN", "departure_date": "2026-06-02"},
        ]

        self.assertTrue(ctrip_itinerary.multi_city_details_are_direct(details, route))


if __name__ == "__main__":
    unittest.main()
