import unittest

from magi_conditional_preflight import (
    unique_page_date, close_decision, legacy_terminal_candidate
)


class ConditionalDesignTests(unittest.TestCase):
    def test_180_unique_date(self):
        self.assertEqual(unique_page_date(["2026-10-07", "2026-10-07"]), "2026-10-07")
        for values in ([], [None], ["2026-10-07", None],
                       ["2026-10-07", "2026-10-08"], ["invalid"]):
            self.assertIsNone(unique_page_date(values))

    def test_185_complete_requires_all_evidence(self):
        good = dict(kakao_pages_final=True, s700_all_classified=True,
                    gpx_coverage_ok=True, unresolved_evidence=False)
        self.assertEqual(close_decision(**good), "COMPLETE")
        self.assertEqual(close_decision(**{**good, "unresolved_evidence": True}),
                         "NEEDS_CONFIRM")
        for field in ("kakao_pages_final", "s700_all_classified", "gpx_coverage_ok"):
            self.assertEqual(close_decision(**{**good, field: False}), "NEEDS_CONFIRM")

    def test_185_approval_distinct_and_attributed(self):
        bad = dict(kakao_pages_final=False, s700_all_classified=False,
                   gpx_coverage_ok=False, unresolved_evidence=True)
        self.assertEqual(close_decision(**bad, exception_approval={"approver": "owner"}),
                         "NEEDS_CONFIRM")
        approval = dict(approver="owner", approved_at="2026-10-09T12:00:00+09:00",
                        reason="documented exception", evidence_id="evidence-1")
        self.assertEqual(close_decision(**bad, exception_approval=approval),
                         "COMPLETE_EXCEPTION")

    def test_186_exact_tuple_only(self):
        frame = dict(decoder_version="0.4", primary_state="1C",
                     secondary_state="08", aux_flag="01")
        self.assertTrue(legacy_terminal_candidate(frame))
        for key, value in (("primary_state", "1D"), ("secondary_state", "09"),
                           ("aux_flag", "00"), ("decoder_version", "0.5")):
            self.assertFalse(legacy_terminal_candidate({**frame, key: value}))


if __name__ == "__main__":
    unittest.main()
