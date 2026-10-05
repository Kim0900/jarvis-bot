import unittest

from magi_review_policy import classify_magi_review_lane, external_review_enabled


class MagiReviewPolicyTests(unittest.TestCase):
    def test_cassandra_final_is_never_external_magi_queue(self):
        task = {
            "status": "VERIFICATION",
            "verified_by": None,
            "verification_required": True,
            "verification_status": "PENDING_CASSANDRA_FINAL",
        }
        self.assertEqual(classify_magi_review_lane(task), "cassandra")

    def test_cassandra_hold_is_cassandra_lane(self):
        task = {
            "status": "VERIFICATION",
            "verified_by": None,
            "verification_required": False,
            "verification_status": "CASSANDRA_HOLD",
        }
        self.assertEqual(classify_magi_review_lane(task), "cassandra")

    def test_required_verification_routes_to_cassandra_even_without_status(self):
        task = {
            "status": "VERIFICATION",
            "verified_by": None,
            "verification_required": True,
            "verification_status": None,
        }
        self.assertEqual(classify_magi_review_lane(task), "cassandra")

    def test_plain_verification_routes_to_magi(self):
        task = {
            "status": "VERIFICATION",
            "verified_by": None,
            "verification_required": False,
            "verification_status": None,
        }
        self.assertEqual(classify_magi_review_lane(task), "magi")

    def test_verified_or_non_verification_is_skipped(self):
        self.assertEqual(
            classify_magi_review_lane({"status": "VERIFICATION", "verified_by": "CASSANDRA"}),
            "skip",
        )
        self.assertEqual(
            classify_magi_review_lane({"status": "IN_PROGRESS", "verified_by": None}),
            "skip",
        )

    def test_external_review_requires_explicit_opt_in(self):
        for value in (None, "", "0", "false", "off", "no", "garbage"):
            self.assertFalse(external_review_enabled(value))
        for value in ("1", "true", "TRUE", "yes", "on"):
            self.assertTrue(external_review_enabled(value))


if __name__ == "__main__":
    unittest.main()
