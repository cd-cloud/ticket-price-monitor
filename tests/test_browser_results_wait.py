import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace

from browser_automation import BrowserAutomation


class BackendTimeoutLikeError(Exception):
    pass


class FakeLocator:
    def __init__(self, selector: str, hits: set[str]) -> None:
        self.selector = selector
        self.hits = hits

    @property
    def first(self) -> "FakeLocator":
        return self

    async def wait_for(self, timeout: int = 0) -> None:
        if self.selector not in self.hits:
            raise BackendTimeoutLikeError(self.selector)


class FakePage:
    def __init__(self, hits: set[str]) -> None:
        self.hits = hits
        self.seen: list[str] = []

    def locator(self, selector: str) -> FakeLocator:
        self.seen.append(selector)
        return FakeLocator(selector, self.hits)

    async def wait_for_timeout(self, timeout: int) -> None:
        return None


class BrowserResultsWaitTests(unittest.TestCase):
    def test_ctrip_wait_continues_after_backend_specific_timeout(self) -> None:
        provider = SimpleNamespace(name="ctrip", timeout_ms=45000, selectors={})
        combined_selector = ".list-content-item-transit, .search-result-item, .box-row, [class*='flight'][class*='item']"
        page = FakePage({combined_selector})
        automation = BrowserAutomation(SimpleNamespace(runtime_dir=Path("."), automation_backend="playwright"), {})

        asyncio.run(automation._wait_for_results(provider, page))

        self.assertEqual([combined_selector], page.seen)


if __name__ == "__main__":
    unittest.main()
