import unittest

from canonical_runtime_audit import evaluate_canonical_runtime_rows


class CanonicalRuntimeAuditTests(unittest.TestCase):
    def test_expected_provenance_passes(self):
        rows = []
        rows += [{"날짜":"2026-07-13","요금":8833,"data_source":"drive_ocr_tesseract"} for _ in range(11)]
        rows += [{"날짜":"2026-07-13","요금":8837,"data_source":"drive_ocr_tesseract"}]
        rows += [{"날짜":"2026-07-13","요금":9625,"data_source":"argos_reconstructed"} for _ in range(4)]
        # Adjust exact sums to expected.
        rows[0]["요금"] += 106000 - sum(r["요금"] for r in rows[:12])
        rows[12]["요금"] += 38500 - sum(r["요금"] for r in rows[12:16])

        start = len(rows)
        rows += [{"날짜":"2026-07-15","요금":9327,"data_source":"drive_ocr_tesseract"} for _ in range(10)]
        rows += [{"날짜":"2026-07-15","요금":9330,"data_source":"drive_ocr_tesseract"}]
        rows += [{"날짜":"2026-07-15","요금":5620,"data_source":"argos_reconstructed"} for _ in range(5)]
        rows += [{"날짜":"2026-07-15","요금":14700,"data_source":"app_ocr_individual"}]
        drive = rows[start:start+11]
        drive[0]["요금"] += 102600 - sum(r["요금"] for r in drive)

        result = evaluate_canonical_runtime_rows(rows)
        self.assertTrue(result["pass"])

    def test_wrong_winner_provenance_fails_even_if_total_matches(self):
        rows = [
            {"날짜":"2026-07-13","요금":144500,"data_source":"argos_reconstructed"},
            {"날짜":"2026-07-15","요금":145400,"data_source":"argos_reconstructed"},
        ]
        result = evaluate_canonical_runtime_rows(rows)
        self.assertFalse(result["pass"])


if __name__ == "__main__":
    unittest.main()
