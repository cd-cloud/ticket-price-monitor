import unittest

from run_summary import RunSummaryStore


class RunSummaryStoreTests(unittest.TestCase):
    def test_get_returns_copy_of_summary(self) -> None:
        store = RunSummaryStore()

        summary = store.finished(saved=1, errors=["first"])
        summary["errors"].append("mutated")

        self.assertEqual(store.get()["errors"], ["first"])

    def test_running_and_finished_preserve_extra_fields(self) -> None:
        store = RunSummaryStore()

        running = store.running(pid=123)
        finished = store.finished(saved=2, errors=[], stdout="ok")

        self.assertTrue(running["running"])
        self.assertEqual(running["pid"], 123)
        self.assertFalse(finished["running"])
        self.assertEqual(finished["stdout"], "ok")


if __name__ == "__main__":
    unittest.main()
