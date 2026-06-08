import unittest

from tail_discovery import AIRLINE_TAIL_DEFAULTS, DEFAULT_TAIL_TEST_CODES, TAIL_DESTINATIONS, default_tail_codes_for_airlines
from tail_models import TailQueueItem
from tail_service import build_tail_request_queue, normalize_tail_discovery_request, tail_queue_report, validate_tail_result


class TailServiceTests(unittest.TestCase):
    def test_normalizes_airlines_and_excludes_origin_transfer(self) -> None:
        request = normalize_tail_discovery_request(
            {
                "origin": "BJS",
                "transfer": "CTU",
                "departure_date": "2026-06-01",
                "preferred_airlines": "CA, ZH",
                "candidates": ["BJS", "CTU", "CAN", "SHA"],
            }
        )

        self.assertEqual(request.origin, "BJS")
        self.assertEqual(request.transfer, "CTU")
        self.assertEqual(request.preferred_airlines, ["CA", "ZH"])
        self.assertNotIn("BJS", request.candidates)
        self.assertNotIn("CTU", request.candidates)
        self.assertIn("CAN", request.candidates)
        self.assertIn("SHA", request.candidates)

    def test_all_domestic_profile_uses_broad_candidate_list(self) -> None:
        request = normalize_tail_discovery_request(
            {
                "origin": "SHA",
                "transfer": "BJS",
                "departure_date": "2026-06-01",
                "candidate_profile": "ALL_DOMESTIC",
            }
        )

        self.assertGreater(len(request.candidates), 40)
        self.assertNotIn("SHA", request.candidates)
        self.assertNotIn("BJS", request.candidates)

    def test_tail_candidates_keep_only_domestic_plus_hong_kong_macau(self) -> None:
        codes = {item.code for item in TAIL_DESTINATIONS}

        self.assertIn("HKG", codes)
        self.assertIn("MFM", codes)
        self.assertNotIn("HKG", DEFAULT_TAIL_TEST_CODES)
        self.assertNotIn("MFM", DEFAULT_TAIL_TEST_CODES)
        for code in ["TYO", "OSA", "SEL", "SIN", "BKK", "KUL", "SGN", "HAN", "MNL"]:
            self.assertNotIn(code, codes)
            self.assertNotIn(code, DEFAULT_TAIL_TEST_CODES)

    def test_air_china_defaults_include_expanded_domestic_points(self) -> None:
        codes = {item.code for item in TAIL_DESTINATIONS}
        for code in ["CGQ", "BAV", "JZH", "MIG", "YBP", "WMT", "WEH"]:
            self.assertIn(code, codes)
            self.assertIn(code, AIRLINE_TAIL_DEFAULTS["AIR_CHINA_GROUP"])

        ca_defaults = default_tail_codes_for_airlines(["CA"])
        for code in ["CGQ", "BAV", "JZH", "MIG", "YBP", "WMT"]:
            self.assertIn(code, ca_defaults)

    def test_explicit_international_tail_candidate_is_allowed_when_known(self) -> None:
        request = normalize_tail_discovery_request(
            {
                "provider": "feizhu",
                "origin": "BJS",
                "transfer": "CTU",
                "departure_date": "2026-06-01",
                "candidates": ["SIN"],
            }
        )

        self.assertEqual("feizhu", request.provider)
        self.assertIn("SIN", request.candidates)

    def test_tail_request_queue_rebuilds_status_from_attempts(self) -> None:
        queue = build_tail_request_queue(
            ["CAN", "SZX", "CKG"],
            [
                {"destination": "CAN", "status": "matched", "price": 800},
                {"destination": "SZX", "status": "failed", "phase": "opening_results", "error": "timeout"},
                {"destination": "CKG", "status": "no_match", "phase": "validation_failed", "error": "bad legs"},
            ],
        )
        by_code = {item["destination"]: item for item in queue}

        self.assertEqual(by_code["CAN"]["status"], "saved")
        self.assertEqual(by_code["SZX"]["status"], "retryable_failed")
        self.assertEqual(by_code["CKG"]["status"], "validation_failed")
        self.assertEqual(by_code["SZX"]["retry_count"], 1)

    def test_tail_queue_item_model_normalizes_report_items(self) -> None:
        queue = [
            TailQueueItem(destination="can", order=1, status="running"),
            {"destination": "szx", "order": "2", "status": "retryable_failed", "retry_count": "1"},
        ]

        report = tail_queue_report(queue)

        self.assertEqual(report["total"], 2)
        self.assertEqual(report["running"], 1)
        self.assertEqual(report["retryable_failed"], 1)
        self.assertEqual(report["items"][0]["destination"], "CAN")
        self.assertEqual(report["items"][1]["retry_count"], 1)

    def test_tail_request_queue_resets_stale_running_status(self) -> None:
        queue = build_tail_request_queue(
            ["can"],
            [],
            previous_items=[{"destination": "can", "order": 4, "status": "running"}],
        )

        self.assertEqual(queue[0]["destination"], "CAN")
        self.assertEqual(queue[0]["order"], 0)
        self.assertEqual(queue[0]["status"], "pending")

    def test_validate_tail_result_accepts_structured_a_b_x(self) -> None:
        result = {
            "flight_details": [
                {
                    "origin": "BJS",
                    "destination": "CTU",
                    "departure_airport": "PEK",
                    "arrival_airport": "CTU",
                    "departure_date": "2026-06-01",
                    "flight_no": "CA1405",
                    "airline": "CA",
                    "row_text": "北京 PEK 至 成都 CTU 国航 CA1405",
                },
                {
                    "origin": "CTU",
                    "destination": "CAN",
                    "departure_airport": "CTU",
                    "arrival_airport": "CAN",
                    "departure_date": "2026-06-01",
                    "flight_no": "CA4301",
                    "airline": "CA",
                    "row_text": "成都 CTU 至 广州 CAN 国航 CA4301",
                },
            ],
            "raw_payload": {"detail_source": "network_exact"},
        }

        validation = validate_tail_result(
            result,
            origin="BJS",
            transfer="CTU",
            destination="CAN",
            departure_date="2026-06-01",
            preferred_airlines=["CA"],
        )

        self.assertEqual(validation["status"], "valid")

    def test_validate_tail_result_accepts_next_day_second_leg(self) -> None:
        result = {
            "flight_details": [
                {
                    "origin": "BJS",
                    "destination": "CTU",
                    "departure_airport": "PEK",
                    "arrival_airport": "CTU",
                    "departure_date": "2026-06-01",
                    "flight_no": "CA1405",
                    "airline": "CA",
                },
                {
                    "origin": "CTU",
                    "destination": "CAN",
                    "departure_airport": "CTU",
                    "arrival_airport": "CAN",
                    "departure_date": "2026-06-02",
                    "flight_no": "CA4301",
                    "airline": "CA",
                },
            ],
            "raw_payload": {"detail_source": "network_exact"},
        }

        validation = validate_tail_result(
            result,
            origin="BJS",
            transfer="CTU",
            destination="CAN",
            departure_date="2026-06-01",
            preferred_airlines=["CA"],
        )

        self.assertEqual(validation["status"], "valid")

    def test_validate_tail_result_rejects_unsupported_text_fallback(self) -> None:
        result = {
            "flight_details": [
                {
                    "origin": "BJS",
                    "destination": "CTU",
                    "departure_airport": "BJS",
                    "arrival_airport": "CTU",
                    "departure_date": "2026-06-01",
                    "flight_no": "CA1405",
                    "airline": "CA",
                    "summary_text": "北京 到 成都 中转 天津 低价 800",
                },
                {
                    "origin": "CTU",
                    "destination": "CAN",
                    "departure_airport": "CTU",
                    "arrival_airport": "CAN",
                    "departure_date": "2026-06-01",
                    "flight_no": "CA4301",
                    "airline": "CA",
                    "summary_text": "北京 到 成都 中转 天津 低价 800",
                },
            ],
            "raw_payload": {"detail_source": "text_fallback"},
        }

        validation = validate_tail_result(
            result,
            origin="BJS",
            transfer="CTU",
            destination="CAN",
            departure_date="2026-06-01",
            preferred_airlines=["CA"],
        )

        self.assertEqual(validation["status"], "invalid")
        self.assertIn("evidence_mentions_destination", validation["checks"])


if __name__ == "__main__":
    unittest.main()
