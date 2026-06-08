import unittest

from route_expander import expand_route_payload, parse_city_list


class RouteExpanderTests(unittest.TestCase):
    def test_parse_city_list_accepts_common_separators(self) -> None:
        self.assertEqual(["SEL", "PUS", "CJU"], parse_city_list("SEL/PUS, CJU"))

    def test_parse_city_list_accepts_chinese_city_names(self) -> None:
        self.assertEqual(
            ["SEL", "PUS", "CJU"],
            parse_city_list("首尔/釜山/济州"),
        )
        self.assertEqual(
            ["ATH", "MIL", "ROM", "PAR", "MAD", "BCN"],
            parse_city_list("雅典、米兰、罗马、巴黎、马德里、巴塞罗那"),
        )

    def test_parse_city_list_accepts_common_international_chinese_names(self) -> None:
        examples = {
            "大阪/名古屋/福冈/札幌/冲绳": ["OSA", "NGO", "FUK", "SPK", "OKA"],
            "河内/胡志明/岘港/普吉/清迈/雅加达/巴厘岛": ["HAN", "SGN", "DAD", "HKT", "CNX", "JKT", "DPS"],
            "慕尼黑/苏黎世/维也纳/布拉格/哥本哈根/莫斯科": ["MUC", "ZRH", "VIE", "PRG", "CPH", "MOW"],
            "芝加哥/波士顿/西雅图/多伦多/墨西哥城/坎昆": ["CHI", "BOS", "SEA", "YTO", "MEX", "CUN"],
            "布里斯班/珀斯/奥克兰/多哈/伊斯坦布尔/开罗": ["BNE", "PER", "AKL", "DOH", "IST", "CAI"],
        }
        for text, expected in examples.items():
            with self.subTest(text=text):
                self.assertEqual(expected, parse_city_list(text))

    def test_round_trip_city_groups_expand_to_cartesian_routes(self) -> None:
        routes = expand_route_payload(
            {
                "route_type": "one_way",
                "origin_options": ["SEL", "CJU"],
                "destination_options": ["PAR", "BCN"],
                "departure_date": "2026-06-01",
                "return_date": "2026-06-12",
                "providers": ["ctrip"],
                "group_label": "Korea Europe return",
            },
            auto_query_interval_hours=12,
        )

        self.assertEqual(4, len(routes))
        self.assertEqual(
            {("SEL", "PAR"), ("SEL", "BCN"), ("CJU", "PAR"), ("CJU", "BCN")},
            {(route.origin, route.destination) for route in routes},
        )
        self.assertTrue(all(route.return_date == "2026-06-12" for route in routes))
        self.assertTrue(all(route.group_label == "Korea Europe return" for route in routes))

    def test_multi_city_segment_groups_expand_to_cartesian_routes(self) -> None:
        routes = expand_route_payload(
            {
                "route_type": "multi_city",
                "segments": [
                    {
                        "origin_options": ["SEL", "PUS", "CJU"],
                        "destination_options": ["BJS"],
                        "departure_date": "2026-06-01",
                    },
                    {
                        "origin_options": ["BJS"],
                        "destination_options": ["ATH", "MIL", "ROM", "PAR", "MAD"],
                        "departure_date": "2026-06-05",
                    },
                ],
                "providers": ["ctrip"],
                "group_label": "Korea Beijing Europe",
            },
            auto_query_interval_hours=12,
        )

        self.assertEqual(15, len(routes))
        self.assertTrue(all(route.route_type == "multi_city" for route in routes))
        self.assertEqual(["CJU", "PUS", "SEL"], sorted({route.segments[0].origin for route in routes}))
        self.assertEqual(["ATH", "MAD", "MIL", "PAR", "ROM"], sorted({route.segments[1].destination for route in routes}))

    def test_rejects_empty_single_route_group(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one origin"):
            expand_route_payload(
                {
                    "route_type": "one_way",
                    "origin_options": [],
                    "destination_options": ["PAR"],
                    "departure_date": "2026-06-01",
                    "providers": ["ctrip"],
                },
                auto_query_interval_hours=12,
            )

    def test_paired_multi_city_requires_equal_option_counts(self) -> None:
        with self.assertRaisesRegex(ValueError, "same option count"):
            expand_route_payload(
                {
                    "route_type": "multi_city",
                    "expansion_mode": "paired",
                    "segments": [
                        {
                            "origin_options": ["SEL", "PUS"],
                            "destination_options": ["BJS"],
                            "departure_date": "2026-06-01",
                        },
                        {
                            "origin_options": ["BJS"],
                            "destination_options": ["ATH", "MIL", "ROM"],
                            "departure_date": "2026-06-05",
                        },
                    ],
                    "providers": ["ctrip"],
                },
                auto_query_interval_hours=12,
            )


if __name__ == "__main__":
    unittest.main()
