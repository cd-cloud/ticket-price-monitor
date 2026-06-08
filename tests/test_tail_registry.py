import unittest
from types import SimpleNamespace

from providers.ctrip_tail_runner import CtripTailDiscoveryRunner
from providers.feizhu_tail_runner import FeizhuTailDiscoveryRunner
from providers.tail_registry import create_tail_runner, supports_tail_discovery


class TailRegistryTests(unittest.TestCase):
    def test_create_registered_tail_runners(self) -> None:
        automation = SimpleNamespace(provider_runtime=None)

        self.assertIsInstance(create_tail_runner("ctrip", automation), CtripTailDiscoveryRunner)
        self.assertIsInstance(create_tail_runner("feizhu", automation), FeizhuTailDiscoveryRunner)

    def test_rejects_provider_without_tail_discovery(self) -> None:
        self.assertTrue(supports_tail_discovery("ctrip"))
        self.assertFalse(supports_tail_discovery("priceline"))
        with self.assertRaises(RuntimeError):
            create_tail_runner("priceline", object())


if __name__ == "__main__":
    unittest.main()
