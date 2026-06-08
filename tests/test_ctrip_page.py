import unittest

from providers.ctrip_page import (
    city_candidate_matches,
    city_search_terms,
    date_text_variants,
    date_value_matches,
    extract_page_departure_dates,
    extract_url_date_values,
)


class CtripPageTests(unittest.TestCase):
    def test_date_text_variants_include_chinese_display(self) -> None:
        variants = date_text_variants("2026-06-01")

        self.assertIn("2026-06-01", variants)
        self.assertIn("06月01日", variants)
        self.assertIn("6月1日", variants)

    def test_extract_url_date_values_reads_common_ctrip_params(self) -> None:
        values = extract_url_date_values(
            "https://flights.ctrip.com/online/list/oneway-bjs-hkg?depdate=2026-06-26&cabin=y_s"
        )

        self.assertEqual(values["depdate"], "2026-06-26")

    def test_date_value_matches_supported_displays(self) -> None:
        self.assertTrue(date_value_matches("2026/06/01", "2026-06-01"))
        self.assertTrue(date_value_matches("周一 6月1日", "2026-06-01"))
        self.assertFalse(date_value_matches("2026-06-02", "2026-06-01"))

    def test_extract_page_departure_dates_reads_no_result_notice(self) -> None:
        dates = extract_page_departure_dates("出发日期：2026-05-11)的机票可能因无航班")

        self.assertEqual(dates, ["2026-05-11"])

    def test_extract_page_departure_dates_reads_domestic_month_day_header(self) -> None:
        dates = extract_page_departure_dates("单程 ：北京广州05月11日周一 厦门航空MF8120", reference_year="2026")

        self.assertEqual(dates, ["2026-05-11"])

    def test_city_search_terms_include_common_international_group_codes(self) -> None:
        self.assertIn("釜山", city_search_terms("PUS", "PUS"))
        self.assertIn("巴塞罗那", city_search_terms("BCN", "BCN"))
        self.assertIn("米兰", city_search_terms("MIL", "MIL"))
        self.assertIn("雅典", city_search_terms("ATH", "ATH"))

    def test_city_candidate_match_rejects_atlanta_for_athens(self) -> None:
        self.assertTrue(city_candidate_matches("\u96c5\u5178 (ATH)", "", "ATH", "ATH"))
        self.assertTrue(city_candidate_matches("", "\u96c5\u5178|\u96c5\u5178\u56fd\u9645\u673a\u573a(ATH)", "ATH", "ATH"))
        self.assertFalse(city_candidate_matches("\u4e9a\u7279\u5170\u5927 (ATL)", "", "ATH", "ATH"))
        self.assertFalse(city_candidate_matches("", "\u4e9a\u7279\u5170\u5927|Hartsfield-Jackson Atlanta International Airport(ATL)", "ATH", "ATH"))


if __name__ == "__main__":
    unittest.main()
