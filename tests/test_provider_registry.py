import unittest

from main import build_parser
from providers.registry import CLI_PROVIDER_CHOICES, PROVIDERS


class ProviderRegistryTests(unittest.TestCase):
    def test_cli_choices_follow_provider_registry(self) -> None:
        parser = build_parser()
        choices_by_dest = {
            action.dest: tuple(action.choices)
            for action in parser._actions
            if getattr(action, "choices", None)
        }
        subparsers_action = next(action for action in parser._actions if action.dest == "command")
        for command in ["run-once", "run-cycle", "schedule"]:
            provider_action = next(
                action
                for action in subparsers_action.choices[command]._actions
                if action.dest == "provider"
            )
            self.assertEqual(CLI_PROVIDER_CHOICES, tuple(provider_action.choices))
        self.assertNotIn("provider", choices_by_dest)

    def test_registry_marks_airchina_as_generic(self) -> None:
        self.assertFalse(PROVIDERS["airchina"].has_handler)
        self.assertEqual("generic", PROVIDERS["airchina"].status)


if __name__ == "__main__":
    unittest.main()
