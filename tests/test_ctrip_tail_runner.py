import unittest
from types import SimpleNamespace

from providers.ctrip_tail_runner import CtripTailDiscoveryRunner


class FakePage:
    def __init__(self) -> None:
        self.handlers = {}

    def on(self, event: str, handler) -> None:
        self.handlers[event] = handler


class CtripTailRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_tail_context_does_not_previsit_login_page(self) -> None:
        page = FakePage()
        context = object()

        class Automation:
            provider_runtime = object()

            async def _new_page(self, provider, backend_name=None):
                return page, context

            def _session_metadata_for_context(self, received_context):
                self.received_context = received_context
                return SimpleNamespace(browser_backend="chrome")

            async def login(self, *_args, **_kwargs):
                raise AssertionError("Ctrip tail discovery should not pre-login before opening results.")

        runner = CtripTailDiscoveryRunner(Automation())

        result_page, result_context, metadata, response_store = await runner._open_tail_context(
            SimpleNamespace(name="ctrip")
        )

        self.assertIs(result_page, page)
        self.assertIs(result_context, context)
        self.assertEqual("chrome", metadata.browser_backend)
        self.assertEqual([], response_store)
        self.assertIn("response", page.handlers)


if __name__ == "__main__":
    unittest.main()
