"""Unit tests for feizhu_flyai_adapter."""

import json
import unittest

from providers import feizhu_flyai_adapter as adapter


class FlyaiResponseParsingTests(unittest.TestCase):
    def test_parse_direct_flight_item(self) -> None:
        item = {
            "journeys": [
                {
                    "journeyType": "直达",
                    "segments": [
                        {
                            "arrCityAbroad": True,
                            "arrCityCode": "SIN",
                            "arrCityName": "新加坡",
                            "arrDateTime": "2026-06-20 15:25:00",
                            "arrStationCode": "SIN",
                            "arrStationName": "樟宜机场",
                            "arrStationShortName": "樟宜",
                            "arrTerm": "T2",
                            "arrWeekAbbrName": "周六",
                            "depCityCode": "CTU",
                            "depCityName": "成都",
                            "depDateTime": "2026-06-20 10:40:00",
                            "depStationCode": "TFU",
                            "depStationName": "天府机场",
                            "depStationShortName": "天府",
                            "depTerm": "T1",
                            "depWeekAbbrName": "周六",
                            "duration": "285",
                            "marketingTransportName": "川航",
                            "marketingTransportNo": "3U3909",
                            "seatClassName": "经济舱",
                            "transportType": "飞机",
                        }
                    ],
                    "totalDuration": "285",
                    "transferDuration": "",
                }
            ],
            "jumpUrl": "https://a.feizhu.com/3hEanL",
            "tags": None,
            "ticketPrice": "2468.00",
            "totalDuration": "285",
        }

        offer = adapter._item_to_offer(item)
        self.assertEqual(offer["price"], 2468.0)
        self.assertEqual(offer["title"], "川航 3U3909")
        self.assertEqual(offer["departure_time"], "10:40:00")
        self.assertEqual(offer["final_url"], "https://a.feizhu.com/3hEanL")
        self.assertEqual(offer["journey_type"], "直达")
        self.assertEqual(len(offer["flight_details"]), 1)

        seg = offer["flight_details"][0]
        self.assertEqual(seg["segment_index"], 1)
        self.assertEqual(seg["flight_no"], "3U3909")
        self.assertEqual(seg["airline"], "川航")
        self.assertEqual(seg["origin"], "CTU")
        self.assertEqual(seg["destination"], "SIN")
        self.assertEqual(seg["departure_airport"], "TFU")
        self.assertEqual(seg["arrival_airport"], "SIN")
        self.assertEqual(seg["departure_terminal"], "T1")
        self.assertEqual(seg["arrival_terminal"], "T2")
        self.assertEqual(seg["departure_time"], "10:40:00")
        self.assertEqual(seg["arrival_time"], "15:25:00")
        self.assertEqual(seg["duration_minutes"], 285)
        self.assertEqual(seg["cabin"], "经济舱")

    def test_parse_transit_flight_item(self) -> None:
        item = {
            "journeys": [
                {
                    "journeyType": "中转",
                    "segments": [
                        {
                            "depCityCode": "CTU",
                            "arrCityCode": "HAK",
                            "depStationCode": "TFU",
                            "arrStationCode": "HAK",
                            "depDateTime": "2026-06-20 16:05:00",
                            "arrDateTime": "2026-06-20 18:20:00",
                            "marketingTransportName": "海航",
                            "marketingTransportNo": "HU7086",
                            "duration": "135",
                            "seatClassName": "经济舱",
                            "depTerm": "T2",
                            "arrTerm": "T2",
                        },
                        {
                            "depCityCode": "HAK",
                            "arrCityCode": "SIN",
                            "depStationCode": "HAK",
                            "arrStationCode": "SIN",
                            "depDateTime": "2026-06-21 07:35:00",
                            "arrDateTime": "2026-06-21 11:00:00",
                            "marketingTransportName": "海航",
                            "marketingTransportNo": "HU747",
                            "duration": "205",
                            "seatClassName": "经济舱",
                            "depTerm": "T2",
                            "arrTerm": "T4",
                        },
                    ],
                    "totalDuration": "960",
                    "transferDuration": "520",
                }
            ],
            "jumpUrl": "https://a.feizhu.com/0E0onG",
            "ticketPrice": "1448.00",
            "totalDuration": "960",
        }

        offer = adapter._item_to_offer(item)
        self.assertEqual(offer["price"], 1448.0)
        self.assertEqual(len(offer["flight_details"]), 2)

        seg1 = offer["flight_details"][0]
        self.assertEqual(seg1["flight_no"], "HU7086")
        self.assertEqual(seg1["origin"], "CTU")
        self.assertEqual(seg1["destination"], "HAK")

        seg2 = offer["flight_details"][1]
        self.assertEqual(seg2["flight_no"], "HU747")
        self.assertEqual(seg2["origin"], "HAK")
        self.assertEqual(seg2["destination"], "SIN")

    def test_parse_response_with_trailing_stderr(self) -> None:
        raw = '{"data":{"itemList":[]}}\nSome stderr noise'
        parsed = adapter._parse_flyai_response(raw)
        self.assertEqual(parsed, {"data": {"itemList": []}})

    def test_parse_response_single_line_json(self) -> None:
        raw = json.dumps({"data": {"itemList": [{"ticketPrice": "100"}]}})
        parsed = adapter._parse_flyai_response(raw)
        self.assertEqual(parsed["data"]["itemList"][0]["ticketPrice"], "100")

    def test_empty_response_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            adapter._parse_flyai_response("")

    def test_invalid_json_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            adapter._parse_flyai_response("not json at all")


class CityNameResolutionTests(unittest.TestCase):
    def test_known_iata_code(self) -> None:
        self.assertEqual(adapter._resolve_city_name("CTU"), "成都")
        self.assertEqual(adapter._resolve_city_name("SIN"), "新加坡")
        self.assertEqual(adapter._resolve_city_name("PEK"), "北京")

    def test_unknown_code_passthrough(self) -> None:
        self.assertEqual(adapter._resolve_city_name("XYZ"), "XYZ")

    def test_case_insensitive(self) -> None:
        self.assertEqual(adapter._resolve_city_name("ctu"), "成都")
        self.assertEqual(adapter._resolve_city_name("Ctu"), "成都")


class BuildCliArgsTests(unittest.TestCase):
    def test_basic_one_way(self) -> None:
        args = adapter._build_cli_args(
            origin="CTU",
            destination="SIN",
            departure_date="2026-06-20",
        )
        self.assertIn("search-flight", args)
        self.assertIn("--origin", args)
        self.assertIn("成都", args)
        self.assertIn("--destination", args)
        self.assertIn("新加坡", args)
        self.assertIn("--dep-date", args)
        self.assertIn("2026-06-20", args)
        self.assertIn("--sort-type", args)
        self.assertIn("3", args)

    def test_cabin_mapping(self) -> None:
        args = adapter._build_cli_args(
            origin="PEK",
            destination="SIN",
            departure_date="2026-06-20",
            cabin="business",
        )
        self.assertIn("--seat-class-name", args)
        self.assertIn("business", args)

    def test_journey_type(self) -> None:
        args = adapter._build_cli_args(
            origin="PEK",
            destination="SIN",
            departure_date="2026-06-20",
            journey_type="1",
        )
        self.assertIn("--journey-type", args)
        self.assertIn("1", args)


if __name__ == "__main__":
    unittest.main()
