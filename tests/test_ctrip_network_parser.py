import asyncio
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from browser_automation import BrowserAutomation
from config_manager import RouteQuery, RouteSegment
from providers import ctrip
from providers import ctrip_itinerary
from providers import ctrip_network_parser


class CtripNetworkParserTests(unittest.TestCase):
    def test_builds_ctrip_online_results_url_for_one_way_and_round_trip(self) -> None:
        one_way = RouteQuery(
            origin="BJS",
            destination="SHA",
            departure_date="2026-06-20",
            cabin="economy",
            passengers=2,
        )
        round_trip = RouteQuery(
            origin="BJS",
            destination="SHA",
            departure_date="2026-06-20",
            return_date="2026-06-23",
            cabin="economy_plus",
        )

        one_way_url = ctrip.build_online_results_url(one_way)
        round_trip_url = ctrip.build_online_results_url(round_trip)

        self.assertIn("/online/list/oneway-bjs-sha", one_way_url)
        self.assertIn("depdate=2026-06-20", one_way_url)
        self.assertIn("cabin=y", one_way_url)
        self.assertIn("adult=2", one_way_url)
        self.assertIn("/online/list/round-bjs-sha", round_trip_url)
        self.assertIn("depdate=2026-06-20_2026-06-23", round_trip_url)
        self.assertIn("cabin=y_s", round_trip_url)

    def test_direct_no_results_falls_back_to_search_form(self) -> None:
        route = RouteQuery(
            origin="IKT",
            destination="SYD",
            departure_date="2027-02-06",
            return_date="2027-02-21",
            cabin="business_first",
        )
        runtime = SimpleNamespace(
            wait_for_results=AsyncMock(),
            dismiss_ctrip_login_overlays=AsyncMock(),
            warm_ctrip_result_data=AsyncMock(),
            extract_offer=AsyncMock(side_effect=AssertionError("no direct offer should be extracted")),
        )
        provider = SimpleNamespace(timeout_ms=1000)
        page = SimpleNamespace(url="https://flights.ctrip.com/online/list/round-ikt-syd")

        with (
            patch("providers.ctrip.open_direct_results_page", AsyncMock(return_value=True)),
            patch("providers.ctrip.ctrip_page.has_no_results_notice", AsyncMock(return_value=True)),
        ):
            result = asyncio.run(ctrip.load_direct_route_offer(runtime, provider, route, page, []))

        self.assertIsNone(result)
        runtime.dismiss_ctrip_login_overlays.assert_awaited_once()
        runtime.extract_offer.assert_awaited_once()

    def test_corrects_cabin_reset_by_ctrip_search_form(self) -> None:
        route = RouteQuery(
            origin="IKT",
            destination="SYD",
            departure_date="2027-02-06",
            return_date="2027-02-21",
            cabin="business_first",
        )
        page = SimpleNamespace(
            url=(
                "https://flights.ctrip.com/online/list/round-ikt0-syd0"
                "?depdate=2027-02-06_2027-02-21&cabin=y_s&adult=1&child=0&infant=0"
            ),
            goto=AsyncMock(),
        )

        asyncio.run(ctrip.ensure_results_cabin_url(page, route, 1000))

        corrected_url = page.goto.await_args.args[0]
        self.assertIn("/online/list/round-ikt0-syd0", corrected_url)
        self.assertIn("depdate=2027-02-06_2027-02-21", corrected_url)
        self.assertIn("cabin=c_f", corrected_url)
        page.goto.assert_awaited_once_with(corrected_url, wait_until="domcontentloaded", timeout=1000)

    def test_builds_ctrip_online_results_url_for_multi_city(self) -> None:
        route = RouteQuery(
            origin="BJS",
            destination="SIN",
            departure_date="2026-06-20",
            route_type="multi_city",
            segments=[
                RouteSegment(origin="BJS", destination="CTU", departure_date="2026-06-20"),
                RouteSegment(origin="CTU", destination="SIN", departure_date="2026-06-27"),
            ],
        )

        url = ctrip.build_multi_city_results_url(route)

        self.assertIn("/online/list/multi-bjs-ctu-ctu-sin", url)
        self.assertIn("depdate=2026-06-20_2026-06-27", url)
        self.assertIn("cabin=y_s", url)

    def test_symmetric_two_segment_multi_city_uses_native_round_trip(self) -> None:
        route = RouteQuery(
            origin="SEL",
            destination="ATH",
            departure_date="2027-03-31",
            route_type="multi_city",
            cabin="economy_plus",
            segments=[
                RouteSegment(origin="SEL", destination="ATH", departure_date="2027-03-31"),
                RouteSegment(origin="ATH", destination="SEL", departure_date="2027-04-10"),
            ],
        )

        native_route = ctrip.as_native_round_trip_route(route)

        self.assertIsNotNone(native_route)
        self.assertEqual("SEL", native_route.origin)
        self.assertEqual("ATH", native_route.destination)
        self.assertEqual("2027-04-10", native_route.return_date)
        self.assertFalse(native_route.is_multi_city)

    def test_asymmetric_two_segment_multi_city_stays_multi_city(self) -> None:
        route = RouteQuery(
            origin="SEL",
            destination="ATH",
            departure_date="2027-03-31",
            route_type="multi_city",
            segments=[
                RouteSegment(origin="SEL", destination="ATH", departure_date="2027-03-31"),
                RouteSegment(origin="ATH", destination="PUS", departure_date="2027-04-10"),
            ],
        )

        self.assertIsNone(ctrip.as_native_round_trip_route(route))

    def test_tail_discovery_reads_domestic_online_flight_segments(self) -> None:
        automation = BrowserAutomation.__new__(BrowserAutomation)
        itinerary = {
            "flightSegments": [
                {
                    "segmentNo": 1,
                    "flightList": [
                        {
                            "sequenceNo": 1,
                            "marketAirlineCode": "CA",
                            "flightNo": "CA1421",
                            "departureCityCode": "BJS",
                            "departureAirportCode": "PEK",
                            "departureDateTime": "2026-06-27 08:30:00",
                            "arrivalCityCode": "CTU",
                            "arrivalAirportCode": "CTU",
                            "arrivalDateTime": "2026-06-27 11:25:00",
                        },
                        {
                            "sequenceNo": 2,
                            "marketAirlineCode": "CA",
                            "flightNo": "CA4301",
                            "departureCityCode": "CTU",
                            "departureAirportCode": "CTU",
                            "departureDateTime": "2026-06-27 13:00:00",
                            "arrivalCityCode": "CAN",
                            "arrivalAirportCode": "CAN",
                            "arrivalDateTime": "2026-06-27 15:20:00",
                        },
                    ],
                }
            ],
            "priceList": [
                {
                    "adultPrice": 700,
                    "adultTax": 0,
                    "cabin": "@Y-Y",
                    "priceUnitList": [
                        {
                            "flightSeatList": [
                                {"segmentNo": 1, "sequenceNo": 1, "cabinClass": "Y", "seatClass": "T"},
                                {"segmentNo": 1, "sequenceNo": 2, "cabinClass": "Y", "seatClass": "S"},
                            ]
                        }
                    ],
                }
            ],
        }
        response_store = [{"payload": {"data": {"flightItineraryList": [itinerary]}}}]

        match = automation._extract_ctrip_tail_match_from_network(
            response_store,
            origin="BJS",
            transfer="CTU",
            destination="CAN",
            preferred_airlines=["AIR_CHINA_GROUP"],
        )

        self.assertIsNotNone(match)
        self.assertEqual(match["total_price"], 700)
        self.assertEqual([detail["flight_no"] for detail in match["details"]], ["CA1421", "CA4301"])
        self.assertEqual(match["details"][0]["departure_airport"], "PEK")
        self.assertEqual(match["details"][1]["arrival_airport"], "CAN")

    def test_storage_state_requires_cookie_or_origin_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "state.json"
            path.write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
            self.assertFalse(BrowserAutomation._storage_state_is_usable(path))

            path.write_text('{"cookies":[{"name":"sid","value":"1"}],"origins":[]}', encoding="utf-8")
            self.assertTrue(BrowserAutomation._storage_state_is_usable(path))

    def test_extracts_generic_price_candidates_from_network(self) -> None:
        response_store = [
            {"payload": {"data": {"fare": 150, "adultPrice": 860, "sortPrice": 920}}},
            {"payload": {"data": {"ticketPrice": 4800}}},
        ]

        self.assertEqual(860, ctrip_network_parser.extract_price_from_network(response_store))

    def test_extracts_single_route_low_price_by_departure_and_return_date(self) -> None:
        route = RouteQuery(
            origin="PUS",
            destination="BCN",
            departure_date="2026-11-14",
            return_date="2026-11-20",
            cabin="economy_plus",
        )
        response_store = [
            {
                "payload": {
                    "priceList": [
                        {
                            "departDate": "/Date(1794614400000+0800)/",
                            "returnDate": "/Date(1795132800000+0800)/",
                            "price": 4181,
                            "totalPrice": 4181,
                            "transportPrice": 2340,
                        },
                        {
                            "departDate": "/Date(1794614400000+0800)/",
                            "returnDate": "/Date(1795219200000+0800)/",
                            "price": 5293,
                            "totalPrice": 5293,
                            "transportPrice": 3500,
                        },
                    ]
                }
            }
        ]

        self.assertEqual(4181, ctrip_network_parser.extract_single_route_price_from_network(response_store, route))

    def test_extracts_single_route_exact_itinerary_details_from_network(self) -> None:
        route = RouteQuery(
            origin="BJS",
            destination="SHA",
            departure_date="2026-06-20",
            cabin="economy",
        )
        price = {
            "adultPrice": 610,
            "adultTax": 50,
            "priceUnitList": [
                {
                    "flightSeatList": [
                        {"segmentNo": 1, "sequenceNo": 1, "cabinClass": "Y", "seatClass": "Q"}
                    ]
                }
            ],
        }
        itinerary = {
            "flightSegments": [
                {
                    "segmentNo": 1,
                    "flightList": [
                        {
                            "sequenceNo": 1,
                            "marketAirlineCode": "MU",
                            "flightNo": "MU5123",
                            "departureCityCode": "BJS",
                            "departureAirportCode": "PEK",
                            "departureDateTime": "2026-06-20 08:30:00",
                            "arrivalCityCode": "SHA",
                            "arrivalAirportCode": "SHA",
                            "arrivalDateTime": "2026-06-20 10:45:00",
                        }
                    ],
                }
            ],
            "priceList": [price],
        }
        response_store = [{"payload": {"data": {"flightItineraryList": [itinerary]}}}]

        match = ctrip_network_parser.extract_single_route_match_from_network(response_store, route)

        self.assertIsNotNone(match)
        self.assertEqual(660, match["total_price"])
        self.assertEqual("flight_itinerary_list", match["source"])
        self.assertEqual("MU5123", match["details"][0]["flight_no"])

    def test_extracts_round_trip_exact_itinerary_details_from_network(self) -> None:
        route = RouteQuery(
            origin="SEL",
            destination="PAR",
            departure_date="2026-06-20",
            return_date="2026-06-27",
            cabin="economy",
        )
        price = {
            "adultPrice": 6400,
            "adultTax": 599,
            "priceUnitList": [
                {
                    "flightSeatList": [
                        {"segmentNo": 1, "sequenceNo": 1, "cabinClass": "Y", "seatClass": "Q"},
                        {"segmentNo": 2, "sequenceNo": 1, "cabinClass": "Y", "seatClass": "S"},
                    ]
                }
            ],
            "luggageVisaKey": (
                '{"criteria":{"FlightInfoList":['
                '{"SegmentNo":1,"SequenceNo":1,"MarketingCarrierCode":"TW","MarketingFlightNo":"401",'
                '"TakeOffDateTime":"2026-06-20 10:10:00","ArrivalDateTime":"2026-06-20 18:10:00",'
                '"DepartureAirportCode":"ICN","ArrivalAirportCode":"CDG",'
                '"DepartureCityCode":"SEL","ArrivalCityCode":"PAR"},'
                '{"SegmentNo":2,"SequenceNo":1,"MarketingCarrierCode":"TW","MarketingFlightNo":"402",'
                '"TakeOffDateTime":"2026-06-27 20:10:00","ArrivalDateTime":"2026-06-28 16:10:00",'
                '"DepartureAirportCode":"CDG","ArrivalAirportCode":"ICN",'
                '"DepartureCityCode":"PAR","ArrivalCityCode":"SEL"}'
                ']}}'
            ),
        }
        response_store = [{"payload": {"data": {"flightItineraryList": [{"priceList": [price]}]}}}]

        match = ctrip_network_parser.extract_single_route_match_from_network(response_store, route)

        self.assertIsNotNone(match)
        self.assertEqual(6999, match["total_price"])
        self.assertEqual("flight_itinerary_list", match["source"])
        self.assertEqual(["SEL", "PAR"], [detail["origin"] for detail in match["details"]])
        self.assertEqual(["PAR", "SEL"], [detail["destination"] for detail in match["details"]])

    def test_single_route_low_price_ignores_wrong_dates_and_transport_price(self) -> None:
        route = RouteQuery(
            origin="PUS",
            destination="BCN",
            departure_date="2026-11-14",
            return_date="2026-11-20",
            cabin="economy_plus",
        )
        response_store = [
            {
                "payload": {
                    "priceList": [
                        {
                            "departDate": "/Date(1779120000000+0800)/",
                            "returnDate": "/Date(1779724800000+0800)/",
                            "price": 4181,
                            "totalPrice": 4181,
                            "transportPrice": 517,
                        }
                    ]
                }
            }
        ]

        self.assertIsNone(ctrip_network_parser.extract_single_route_price_from_network(response_store, route))

    def test_extracts_multi_city_match_from_network(self) -> None:
        route = RouteQuery(
            origin="BJS",
            destination="SIN",
            departure_date="2026-06-01",
            route_type="multi_city",
            transfer_policy="direct_only",
            segments=[
                RouteSegment(origin="BJS", destination="CTU", departure_date="2026-06-01"),
                RouteSegment(origin="CTU", destination="SIN", departure_date="2026-06-02"),
            ],
        )
        price = {
            "adultPrice": 1000,
            "adultTax": 120,
            "priceUnitList": [{"flightSeatList": []}],
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
        response_store = [{"payload": {"data": {"flightItineraryList": [{"priceList": [price]}]}}}]

        match = ctrip_network_parser.extract_multi_city_match_from_network(response_store, route)

        self.assertIsNotNone(match)
        self.assertEqual(1120, match["total_price"])
        self.assertEqual(["BJS", "CTU"], [detail["origin"] for detail in match["details"]])

    def test_summarizes_response_for_diagnostics(self) -> None:
        summary = ctrip_network_parser.summarize_response_for_diagnostics(
            {"url": "https://example.test", "status": 200, "payload": {"data": {"flightItineraryList": []}}}
        )

        self.assertTrue(summary["has_flight_itinerary_list"])
        self.assertIn("data:dict", summary["shape"])

    def test_multi_city_route_match_accepts_seoul_airport_aliases(self) -> None:
        route = RouteQuery(
            origin="SEL",
            destination="ATH",
            departure_date="2027-03-31",
            route_type="multi_city",
            segments=[
                RouteSegment(origin="SEL", destination="ATH", departure_date="2027-03-31"),
                RouteSegment(origin="ATH", destination="SEL", departure_date="2027-04-10"),
            ],
        )
        details = [
            {"segment_index": 1, "origin": "ICN", "destination": "ATH", "departure_date": "2027-03-31"},
            {"segment_index": 2, "origin": "ATH", "destination": "GMP", "departure_date": "2027-04-10"},
        ]

        self.assertTrue(ctrip_itinerary.multi_city_details_match_route(details, route))

    def test_multi_city_route_match_rejects_unrelated_destination(self) -> None:
        route = RouteQuery(
            origin="SEL",
            destination="ATH",
            departure_date="2027-03-31",
            route_type="multi_city",
            segments=[
                RouteSegment(origin="SEL", destination="ATH", departure_date="2027-03-31"),
                RouteSegment(origin="ATH", destination="SEL", departure_date="2027-04-10"),
            ],
        )
        details = [
            {"segment_index": 1, "origin": "ICN", "destination": "ATL", "departure_date": "2027-03-31"},
            {"segment_index": 2, "origin": "ATL", "destination": "GMP", "departure_date": "2027-04-10"},
        ]

        self.assertFalse(ctrip_itinerary.multi_city_details_match_route(details, route))


if __name__ == "__main__":
    unittest.main()
