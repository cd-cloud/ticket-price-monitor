import unittest

from providers import feizhu_parser


class FeizhuParserTests(unittest.TestCase):
    def test_extract_prices_supports_rmb_symbols(self) -> None:
        self.assertEqual([688.0, 1290.0], feizhu_parser.extract_prices("低价 ￥688 公务舱 ¥1290"))

    def test_extract_prices_from_network_finds_nested_price_keys(self) -> None:
        response_store = [
            {
                "payload": {
                    "data": {
                        "flights": [
                            {"adultPrice": 880, "airline": "CA"},
                            {"fareAmount": "￥760", "airline": "MU"},
                        ]
                    }
                }
            }
        ]

        self.assertEqual([760.0, 880.0], feizhu_parser.extract_prices_from_network(response_store))

    def test_parse_jsonp_payload(self) -> None:
        payload = feizhu_parser.parse_json_or_jsonp('mtopjsonp1({"data":{"adultPrice":620}})')

        self.assertEqual({"data": {"adultPrice": 620}}, payload)

    def test_extract_best_flight_match_skips_restricted_fares(self) -> None:
        response_store = [
            {
                "url": "https://sjipiao.fliggy.com/searchow/search.htm",
                "payload": {
                    "data": {
                        "flight": [
                            {
                                "airlineCode": "HO",
                                "flightNo": "HO1254",
                                "depAirport": "PKX",
                                "arrAirport": "PVG",
                                "depTime": "2026-06-20 21:25",
                                "arrTime": "2026-06-20 23:35",
                                "cabin": {
                                    "bestPrice": 398,
                                    "specialType": "经济舱",
                                    "notices": ["限19-24周岁、55周岁及以上乘客可订"],
                                },
                            },
                            {
                                "airlineCode": "CA",
                                "flightNo": "CA8321",
                                "depAirport": "PKX",
                                "arrAirport": "PVG",
                                "depTime": "2026-06-20 10:40",
                                "arrTime": "2026-06-20 12:50",
                                "flightType": "321",
                                "cabin": {"bestPrice": 450, "specialType": "经济舱", "cabin": "Y", "notices": []},
                            },
                        ]
                    }
                },
            }
        ]

        match = feizhu_parser.extract_best_flight_match_from_network(response_store)

        self.assertEqual(450, match["total_price"])
        self.assertEqual("CA8321", match["details"][0]["flight_no"])
        self.assertEqual("searchow_flight_list", match["source"])

    def test_extract_tail_match_from_nested_transfer_flights(self) -> None:
        response_store = [
            {
                "url": "https://sjipiao.fliggy.com/searchow/search.htm",
                "payload": {
                    "data": {
                        "flight": [
                            {
                                "cabin": {"bestPrice": 920, "specialType": "经济舱", "notices": []},
                                "flightSegments": [
                                    {
                                        "airlineCode": "CA",
                                        "flightNo": "CA1421",
                                        "depCity": "BJS",
                                        "arrCity": "CTU",
                                        "depAirport": "PEK",
                                        "arrAirport": "CTU",
                                        "depTime": "2026-06-20 08:30",
                                        "arrTime": "2026-06-20 11:25",
                                    },
                                    {
                                        "airlineCode": "CA",
                                        "flightNo": "CA403",
                                        "depCity": "CTU",
                                        "arrCity": "SIN",
                                        "depAirport": "CTU",
                                        "arrAirport": "SIN",
                                        "depTime": "2026-06-20 13:10",
                                        "arrTime": "2026-06-20 18:30",
                                    },
                                ],
                            }
                        ]
                    }
                },
            }
        ]

        match = feizhu_parser.extract_tail_match_from_network(
            response_store,
            origin="BJS",
            transfer="CTU",
            destination="SIN",
            preferred_airlines=["CA"],
        )

        self.assertIsNotNone(match)
        self.assertEqual(920, match["total_price"])
        self.assertEqual(["CA1421", "CA403"], [detail["flight_no"] for detail in match["details"]])

    def test_extract_prices_filters_unreasonable_values(self) -> None:
        self.assertEqual([520.0], feizhu_parser.extract_prices("税费 20 票价 520 超大数字 999999"))

    def test_extract_best_match_from_alternate_flight_list_shape(self) -> None:
        response_store = [
            {
                "url": "https://market.m.taobao.com/app/trip/flight_search_result",
                "payload": {
                    "data": {
                        "result": [
                            {
                                "flightNumber": "MU5101",
                                "airline": "MU",
                                "depAirportCode": "SHA",
                                "arrAirportCode": "PEK",
                                "departureTime": "2026-06-20 08:00",
                                "arrivalTime": "2026-06-20 10:20",
                                "adultPrice": 780,
                                "cabinList": [{"cabinCode": "Y"}],
                            }
                        ]
                    }
                },
            }
        ]

        match = feizhu_parser.extract_best_flight_match_from_network(response_store)

        self.assertIsNotNone(match)
        self.assertEqual(780, match["total_price"])
        self.assertEqual("MU5101", match["details"][0]["flight_no"])
        self.assertEqual("SHA", match["details"][0]["departure_airport"])

    def test_extract_tail_match_from_wrapped_segment_list(self) -> None:
        response_store = [
            {
                "url": "https://market.m.taobao.com/app/trip/flight_search_result",
                "payload": {
                    "data": {
                        "items": [
                            {
                                "totalPrice": 1680,
                                "segmentList": [
                                    {
                                        "flightInfo": {
                                            "flightNo": "CA1501",
                                            "airlineCode": "CA",
                                            "depCityCode": "BJS",
                                            "arrCityCode": "SHA",
                                            "depAirportCode": "PEK",
                                            "arrAirportCode": "SHA",
                                            "depTime": "2026-06-20 08:00",
                                            "arrTime": "2026-06-20 10:15",
                                        }
                                    },
                                    {
                                        "flightInfo": {
                                            "flightNo": "CA833",
                                            "airlineCode": "CA",
                                            "depCityCode": "SHA",
                                            "arrCityCode": "PAR",
                                            "depAirportCode": "PVG",
                                            "arrAirportCode": "CDG",
                                            "depTime": "2026-06-20 12:30",
                                            "arrTime": "2026-06-20 19:10",
                                        }
                                    },
                                ],
                            }
                        ]
                    }
                },
            }
        ]

        match = feizhu_parser.extract_tail_match_from_network(
            response_store,
            origin="BJS",
            transfer="SHA",
            destination="PAR",
            preferred_airlines=["CA"],
        )

        self.assertIsNotNone(match)
        self.assertEqual(1680, match["total_price"])
        self.assertEqual(["CA1501", "CA833"], [detail["flight_no"] for detail in match["details"]])

    def test_extract_tail_match_from_international_solution_shape(self) -> None:
        response_store = [
            {
                "url": "https://market.m.taobao.com/app/trip/flight_search_result",
                "payload": {
                    "data": {
                        "solutionList": [
                            {
                                "lowestPrice": "2380",
                                "journeyList": [
                                    {
                                        "segmentInfo": {
                                            "flightCode": "CA4112",
                                            "marketingAirlineCode": "CA",
                                            "departureCityCode": "BJS",
                                            "arrivalCityCode": "CTU",
                                            "departureAirportCode": "PEK",
                                            "arrivalAirportCode": "TFU",
                                            "departureDateTime": "2026-06-20 09:00",
                                            "arrivalDateTime": "2026-06-20 12:05",
                                        }
                                    },
                                    {
                                        "segmentInfo": {
                                            "flightCode": "CA403",
                                            "marketingAirlineCode": "CA",
                                            "departureCityCode": "CTU",
                                            "arrivalCityCode": "SIN",
                                            "departureAirportCode": "TFU",
                                            "arrivalAirportCode": "SIN",
                                            "departureDateTime": "2026-06-20 13:35",
                                            "arrivalDateTime": "2026-06-20 18:50",
                                        }
                                    },
                                ],
                            }
                        ]
                    }
                },
            }
        ]

        match = feizhu_parser.extract_tail_match_from_network(
            response_store,
            origin="BJS",
            transfer="CTU",
            destination="SIN",
            preferred_airlines=["CA"],
        )

        self.assertIsNotNone(match)
        self.assertEqual(2380, match["total_price"])
        self.assertEqual(["CA4112", "CA403"], [detail["flight_no"] for detail in match["details"]])

    def test_build_network_diagnostics_summarizes_routes_and_prices(self) -> None:
        response_store = [
            {
                "url": "https://market.m.taobao.com/app/trip/flight_search_result",
                "payload": {
                    "data": {
                        "items": [
                            {
                                "totalPrice": 1680,
                                "segmentList": [
                                    {
                                        "flightInfo": {
                                            "flightNo": "CA1501",
                                            "airlineCode": "CA",
                                            "depCityCode": "BJS",
                                            "arrCityCode": "SHA",
                                            "depTime": "2026-06-20 08:00",
                                            "arrTime": "2026-06-20 10:15",
                                        }
                                    },
                                    {
                                        "flightInfo": {
                                            "flightNo": "CA833",
                                            "airlineCode": "CA",
                                            "depCityCode": "SHA",
                                            "arrCityCode": "PAR",
                                            "depTime": "2026-06-20 12:30",
                                            "arrTime": "2026-06-20 19:10",
                                        }
                                    },
                                ],
                            }
                        ]
                    }
                },
            }
        ]

        diagnostics = feizhu_parser.build_network_diagnostics(response_store, transfer="SHA")
        text = feizhu_parser.format_network_diagnostics(response_store, transfer="SHA")

        self.assertEqual(1, diagnostics["itineraries"])
        self.assertEqual(1, diagnostics["via_transfer"])
        self.assertEqual([1680.0], diagnostics["lowest_prices"])
        self.assertIn("BJS->SHA->PAR", text)
        self.assertIn("via_SHA=1", text)

    def test_response_store_detects_baxia_verification(self) -> None:
        response_store = [
            {
                "url": "https://sijipiao.fliggy.com/ie/flight_search_result_poller.do/_____tmd_____/newslidecaptcha",
                "payload": {"code": 0, "data": {"encryptToken": "abc", "imageData": "..."}},
            }
        ]

        self.assertTrue(feizhu_parser.response_store_has_verification(response_store))
        self.assertIn("verification_payloads=yes", feizhu_parser.format_network_diagnostics(response_store, transfer="CTU"))


if __name__ == "__main__":
    unittest.main()
