import unittest

from adaptive_selectors import SelectorCandidate, observe_selector_candidates, score_selector_observation


class FakeLocator:
    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.index = 0

    async def count(self) -> int:
        return len(self.texts)

    def nth(self, index: int) -> "FakeLocator":
        locator = FakeLocator(self.texts)
        locator.index = index
        return locator

    async def inner_text(self, timeout: int = 0) -> str:
        return self.texts[self.index]


class FakePage:
    def __init__(self, mapping: dict[str, list[str]]) -> None:
        self.mapping = mapping

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self.mapping.get(selector, []))


class AdaptiveSelectorTests(unittest.TestCase):
    def test_scores_matching_selector_above_plain_count(self) -> None:
        matching = score_selector_observation(
            count=3,
            text_preview="CA1234 transfer CTU ¥880",
            must_contain_any=("transfer",),
            nice_to_have_any=("¥", "CA"),
        )
        plain = score_selector_observation(
            count=8,
            text_preview="navigation header footer",
            must_contain_any=("transfer",),
            nice_to_have_any=("¥", "CA"),
        )

        self.assertGreater(matching, plain)

    def test_empty_selector_is_rejected(self) -> None:
        self.assertLess(score_selector_observation(count=0, text_preview="transfer"), 0)

    def test_observation_samples_more_than_first_node(self) -> None:
        observations = self._run(
            observe_selector_candidates(
                FakePage({".card": ["navigation", "CA1234 transfer CTU ¥880"]}),
                [
                    SelectorCandidate(
                        ".card",
                        "cards",
                        must_contain_any=("transfer",),
                        nice_to_have_any=("¥", "CA"),
                        sample_nodes=3,
                    )
                ],
            )
        )

        self.assertGreater(observations[0]["score"], 0)
        self.assertIn("transfer", observations[0]["text_preview"])

    @staticmethod
    def _run(coro):
        import asyncio

        return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
