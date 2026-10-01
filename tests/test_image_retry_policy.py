import unittest

from image_retry_policy import (
    classify_result_failure,
    is_terminal_last_error,
    mark_retryable,
    mark_terminal,
)


class ImageRetryPolicyTests(unittest.TestCase):
    def test_daily_history_validation_is_terminal(self):
        self.assertEqual(
            classify_result_failure({
                "format": "daily_history",
                "error_stage": "daily_history_validation",
                "error_code": "DAILY_HISTORY_COUNT_MISMATCH",
            }),
            "terminal",
        )

    def test_unknown_format_is_terminal(self):
        self.assertEqual(
            classify_result_failure({"error": "형식판별실패"}),
            "terminal",
        )

    def test_unstructured_runtime_failure_is_retryable(self):
        self.assertEqual(
            classify_result_failure({"error": "provider transport failed"}),
            "retryable",
        )

    def test_prefix_helpers(self):
        t = mark_terminal("bad content")
        r = mark_retryable("timeout")
        self.assertTrue(is_terminal_last_error(t))
        self.assertFalse(is_terminal_last_error(r))
        self.assertTrue(t.startswith("TERMINAL:"))
        self.assertTrue(r.startswith("RETRYABLE:"))


if __name__ == "__main__":
    unittest.main()
