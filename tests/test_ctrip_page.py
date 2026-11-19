import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from providers.ctrip_page import (
    city_candidate_matches,
    click_matching_city_candidate,
    city_search_terms,
    date_text_variants,
    date_value_matches,
    extract_page_departure_dates,
    extract_url_date_values,
)
from providers.ctrip_support import is_ctrip_domestic_code


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

    def test_new_tail_destinations_use_domestic_ctrip_search(self) -> None:
        self.assertTrue(is_ctrip_domestic_code("JHG"))
        self.assertTrue(is_ctrip_domestic_code("TCZ"))
        self.assertTrue(is_ctrip_domestic_code("CSX"))

    def test_city_candidate_match_rejects_atlanta_for_athens(self) -> None:
        self.assertTrue(city_candidate_matches("\u96c5\u5178 (ATH)", "", "ATH", "ATH"))
        self.assertTrue(city_candidate_matches("", "\u96c5\u5178|\u96c5\u5178\u56fd\u9645\u673a\u573a(ATH)", "ATH", "ATH"))
        self.assertFalse(city_candidate_matches("\u4e9a\u7279\u5170\u5927 (ATL)", "", "ATH", "ATH"))
        self.assertFalse(city_candidate_matches("", "\u4e9a\u7279\u5170\u5927|Hartsfield-Jackson Atlanta International Airport(ATL)", "ATH", "ATH"))


class CtripCityPickerTests(unittest.IsolatedAsyncioTestCase):
    async def test_click_matching_city_candidate_supports_new_poi_selector(self) -> None:
        class FakeCandidate:
            def __init__(self, selector: str) -> None:
                self.selector = selector
                self.clicked = False

            async def count(self) -> int:
                return int(self.selector == ".cflt-poi-selector-new .address")

            def nth(self, index: int):
                return self

            async def inner_text(self, timeout: int) -> str:
                return "城市 贵阳 KWE GY"

            async def get_attribute(self, name: str, timeout: int) -> str:
                return "点击POI选项[typeName:城市,text:贵阳(KWE),Code:KWE]"

            async def click(self, timeout: int, force: bool) -> None:
                self.clicked = True

        class FakeScope:
            def __init__(self) -> None:
                self.candidate = None

            def locator(self, selector: str) -> FakeCandidate:
                self.candidate = FakeCandidate(selector)
                return self.candidate

        scope = FakeScope()
        self.assertTrue(await click_matching_city_candidate(scope, "KWE", "KWE", 2000))
        self.assertTrue(scope.candidate.clicked)


class CtripLoginOverlayTests(unittest.IsolatedAsyncioTestCase):
    async def test_dismisses_stacked_notice_and_login_dialog(self) -> None:
        from providers.ctrip_page import dismiss_login_overlays

        class FakeItem:
            def __init__(self, page, selector: str) -> None:
                self.page = page
                self.selector = selector

            async def is_visible(self, timeout: int) -> bool:
                return self.page.visible.get(self.selector, False)

            async def click(self, timeout: int, force: bool) -> None:
                self.page.visible[self.selector] = False
                self.page.clicked.append(self.selector)

        class FakeLocator:
            def __init__(self, page, selector: str) -> None:
                self.item = FakeItem(page, selector)

            async def count(self) -> int:
                return int(await self.item.is_visible(0))

            def nth(self, index: int) -> FakeItem:
                return self.item

        class FakePage:
            def __init__(self) -> None:
                self.visible = {"button:has-text('我知道了')": True, ".modal-close": True}
                self.clicked = []
                self.keyboard = SimpleNamespace(press=AsyncMock())

            def locator(self, selector: str) -> FakeLocator:
                return FakeLocator(self, selector)

            async def wait_for_timeout(self, timeout: int) -> None:
                pass

        page = FakePage()
        await dismiss_login_overlays(page, 2000)
        self.assertEqual(["button:has-text('我知道了')", ".modal-close"], page.clicked)


class CtripDatepickerTests(unittest.IsolatedAsyncioTestCase):
    async def test_closes_lingering_calendar_with_viewport_outside_click(self) -> None:
        from unittest.mock import patch
        from providers.ctrip_page import settle_datepicker

        trigger = SimpleNamespace(evaluate=AsyncMock(), click=AsyncMock())
        page = SimpleNamespace(
            viewport_size={"width": 1000, "height": 700},
            keyboard=SimpleNamespace(press=AsyncMock()),
            mouse=SimpleNamespace(click=AsyncMock()),
            wait_for_timeout=AsyncMock(),
            locator=lambda selector: SimpleNamespace(click=AsyncMock()),
        )
        with patch("providers.ctrip_page.is_datepicker_overlay_visible", AsyncMock(side_effect=[True, False])):
            await settle_datepicker(page, trigger, 2000)
        page.mouse.click.assert_awaited_once_with(970, 30)
        trigger.click.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
