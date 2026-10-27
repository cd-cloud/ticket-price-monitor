from copy import deepcopy
import unittest

from tools.replay_tail_history import replay
from test_tail_improvements import payload


class ReplayTests(unittest.TestCase):
    def case(self, stamp, candidates=("CAN", "SZX")):
        return {"observed_at": stamp, "request": payload(),
                "candidates": list(candidates),
                "gold": {"CAN": {"outcome": "matched", "elapsed_seconds": 10},
                         "SZX": {"outcome": "filtered", "elapsed_seconds": 10}}}

    def test_prior_complete_history_can_reorder_future_case(self):
        result = replay([self.case("2026-10-26T12:00:00+00:00"),
                         self.case("2026-10-27T12:00:00+00:00", ("SZX", "CAN"))], budget=1)
        self.assertEqual(result["cases"][1]["ranked"], ["CAN"])
        self.assertEqual(result["cases"][1]["fixed"], ["SZX"])
        self.assertEqual(result["candidate_queries_per_strategy"], 2)

    def test_same_case_or_same_timestamp_labels_cannot_leak(self):
        cases = [self.case("2026-10-27T12:00:00+00:00", ("SZX", "CAN")),
                 self.case("2026-10-27T12:00:00+00:00", ("SZX", "CAN"))]
        result = replay(cases, budget=1)
        self.assertTrue(all(row["ranked"] == ["SZX"] for row in result["cases"]))

    def test_missing_counterfactual_is_skipped_not_assumed_failure(self):
        case = self.case("2026-10-27T12:00:00+00:00")
        del case["gold"]["SZX"]
        result = replay([case], budget=1)
        self.assertEqual(result["evaluated_cases"], 0)
        self.assertEqual(result["skipped_incomplete_cases"], 1)
        self.assertIsNone(result["ranked_valid_per_100_candidate_queries"])

    def test_inputs_are_not_mutated(self):
        case = self.case("2026-10-27T12:00:00+00:00")
        before = deepcopy(case)
        replay([case])
        self.assertEqual(before, case)
