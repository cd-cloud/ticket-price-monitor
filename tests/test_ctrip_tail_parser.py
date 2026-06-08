import unittest

from providers import ctrip_tail_parser


class CtripTailParserTest(unittest.TestCase):
    def test_dom_card_match_prefers_requested_transfer(self) -> None:
        cards = [
            {
                "price": 580,
                "transfer_name": "\u8679\u6865",
                "transfer_text": "\u8f6c \u8679\u6865 2h20m",
                "transfer_layover": "2h20m",
                "transfer_duration": "\u4e2d\u8f6c 2\u5c0f\u65f620\u5206",
                "departure_time": "07:00",
                "departure_airport": "\u5927\u5174\u56fd\u9645\u673a\u573a",
                "arrival_time": "13:40",
                "arrival_airport": "\u767d\u4e91\u56fd\u9645\u673a\u573a T3",
                "airline_items": [
                    {"airline": "\u4e1c\u65b9\u822a\u7a7a", "flight_no": "MU5138", "plane_text": "MU5138 A321"},
                    {"airline": "\u4e1c\u65b9\u822a\u7a7a", "flight_no": "MU5303", "plane_text": "MU5303 A320"},
                ],
                "cabin_texts": ["\u7ecf\u6d4e\u8231", "\u7ecf\u6d4e\u8231"],
                "text": "\u4e1c\u65b9\u822a\u7a7aMU5138 \u8f6c \u8679\u6865 2h20m \u00a5580\u8d77",
            },
            {
                "price": 560,
                "transfer_name": "\u6d66\u4e1c",
                "transfer_text": "\u8f6c \u6d66\u4e1c 2h5m",
                "text": "\u8f6c \u6d66\u4e1c \u00a5560\u8d77",
            },
        ]

        match = ctrip_tail_parser.find_tail_match_from_cards(
            cards,
            origin="BJS",
            transfer="SHA",
            destination="CAN",
            departure_date="2026-06-14",
            cabin="economy_plus",
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["total_price"], 560)
        self.assertEqual(match["details"][0]["destination"], "SHA")
        self.assertEqual(match["details"][1]["destination"], "CAN")

    def test_text_block_fallback_extracts_transfer_price(self) -> None:
        blocks = ctrip_tail_parser.split_result_blocks(
            "\u4e1c\u65b9\u822a\u7a7aMU5138\n"
            "\u4e1c\u65b9\u822a\u7a7aMU5303\n"
            "07:00\n"
            "\u5927\u5174\u56fd\u9645\u673a\u573a\n"
            "\u4e2d\u8f6c2\u5c0f\u65f620\u5206\u8f6c1\u6b21\n"
            "\u8f6c\u8679\u68652h20m\n"
            "13:40\n"
            "\u767d\u4e91\u56fd\u9645\u673a\u573aT3\n"
            "\u00a5580\u8d77\n"
            "\u8ba2\u7968"
        )

        match = ctrip_tail_parser.find_tail_match_from_text_blocks(
            blocks,
            origin="BJS",
            transfer="SHA",
            destination="CAN",
            departure_date="2026-06-14",
            cabin="economy_plus",
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["total_price"], 580)
        self.assertEqual(match["details"][0]["departure_time"], "07:00")
        self.assertEqual(match["details"][0]["destination"], "SHA")
        self.assertEqual(match["details"][1]["arrival_time"], "13:40")
        self.assertEqual(match["details"][1]["destination"], "CAN")

    def test_tail_match_can_filter_by_airline_code_or_name(self) -> None:
        blocks = ctrip_tail_parser.split_result_blocks(
            "\u6df1\u5733\u822a\u7a7aZH9101\n"
            "\u6df1\u5733\u822a\u7a7aZH9102\n"
            "08:45\n"
            "\u9996\u90fd\u56fd\u9645\u673a\u573aT3\n"
            "\u4e2d\u8f6c2\u5c0f\u65f620\u5206\u8f6c1\u6b21\n"
            "\u8f6c\u6210\u90fd2h20m\n"
            "14:20\n"
            "\u767d\u4e91\u56fd\u9645\u673a\u573aT3\n"
            "\u00a5500\u8d77\n"
            "\u8ba2\u7968"
        )

        by_code = ctrip_tail_parser.find_tail_match_from_text_blocks(
            blocks,
            origin="BJS",
            transfer="CTU",
            destination="CAN",
            departure_date="2026-06-14",
            cabin="economy_plus",
            preferred_airlines=["ZH"],
        )
        by_name = ctrip_tail_parser.find_tail_match_from_text_blocks(
            blocks,
            origin="BJS",
            transfer="CTU",
            destination="CAN",
            departure_date="2026-06-14",
            cabin="economy_plus",
            preferred_airlines=["\u6df1\u822a"],
        )
        by_air_china_group = ctrip_tail_parser.find_tail_match_from_text_blocks(
            blocks,
            origin="BJS",
            transfer="CTU",
            destination="CAN",
            departure_date="2026-06-14",
            cabin="economy_plus",
            preferred_airlines=["\u56fd\u822a\u7cfb\u822a\u53f8"],
        )
        by_air_china_group_codes = ctrip_tail_parser.find_tail_match_from_text_blocks(
            blocks,
            origin="BJS",
            transfer="CTU",
            destination="CAN",
            departure_date="2026-06-14",
            cabin="economy_plus",
            preferred_airlines=["CA", "ZH", "TV", "SC", "KY", "NX"],
        )
        by_other_airline = ctrip_tail_parser.find_tail_match_from_text_blocks(
            blocks,
            origin="BJS",
            transfer="CTU",
            destination="CAN",
            departure_date="2026-06-14",
            cabin="economy_plus",
            preferred_airlines=["CA"],
        )

        self.assertIsNotNone(by_code)
        self.assertIsNotNone(by_name)
        self.assertIsNotNone(by_air_china_group)
        self.assertIsNotNone(by_air_china_group_codes)
        self.assertIsNone(by_other_airline)

    def test_text_block_fallback_rejects_footer_noise(self) -> None:
        blocks = [
            "您查询的北京(BJS)至天津(TSN)（出发日期：2026-05-11)的机票可能因无航班或航班座位已售完导致暂时无法查询到对应价格。"
            "现在注册携程会员即可获得1200积分和1300元消费券! 热门机票 北京到成都机票 天津特价机票 95010"
        ]

        match = ctrip_tail_parser.find_tail_match_from_text_blocks(
            blocks,
            origin="BJS",
            transfer="CTU",
            destination="TSN",
            departure_date="2026-06-27",
            cabin="economy_plus",
            preferred_airlines=["CA", "ZH", "TV", "SC", "KY", "NX"],
        )

        self.assertIsNone(match)

    def test_diagnostics_lists_visible_transfer_mentions(self) -> None:
        message = ctrip_tail_parser.build_diagnostics(
            cards=[{"price": 580, "transfer_name": "\u8679\u6865"}],
            blocks=[],
            transfer="CTU",
        )

        self.assertIn("\u8679\u6865", message)
        self.assertIn("CTU", message)


if __name__ == "__main__":
    unittest.main()
