import unittest

from canonical_identity_v1 import (
    evidence_rank,
    identity_strength,
    platform_key,
    select_canonical,
)


# Policy evidence doc synchronized.
class CanonicalIdentityV1Tests(unittest.TestCase):
    def test_full_interval_exact_match_is_strong(self):
        a = {
            "id": 1, "날짜": "2026-07-13", "콜유형": "카카오T",
            "배차시각": "19:08", "하차시각": "19:17", "요금": 6300,
            "출발지": "동구 신암2동", "도착지": "중구 성내2동",
        }
        b = {
            "id": 2, "날짜": "2026-07-13", "콜유형": "카카오T",
            "배차시각": "19:08", "하차시각": "19:17", "요금": 6300,
            "출발지": "대구 동구 신암2동", "도착지": "대구 중구 성내2동",
        }
        self.assertEqual(identity_strength(a, b), "STRONG_FULL_INTERVAL")

    def test_boundary_only_overlap_is_weak_not_auto_duplicate(self):
        a = {
            "id": 1, "날짜": "2026-08-23", "콜유형": "카카오T",
            "배차시각": "01:02", "하차시각": None, "요금": 7700,
            "출발지": None, "도착지": None,
        }
        b = {
            "id": 2, "날짜": "2026-08-23", "콜유형": "카카오T",
            "배차시각": "00:55", "하차시각": "01:02", "요금": 7700,
            "출발지": "대구 수성구 수성1가동", "도착지": "대구 중구 성내2동",
        }
        self.assertEqual(identity_strength(a, b), "WEAK_TIME_ONLY")

    def test_partial_start_plus_addresses_is_strong(self):
        a = {
            "id": 1, "날짜": "2026-08-16", "콜유형": "카카오T",
            "배차시각": "20:27", "하차시각": None, "요금": 6400,
            "출발지": "대구 수성구 범어3동", "도착지": "대구 동구 신암1동",
        }
        b = {
            "id": 2, "날짜": "2026-08-16", "콜유형": "카카오T",
            "배차시각": "20:27", "하차시각": "20:36", "요금": 6400,
            "출발지": "대구 수성구 범어3동", "도착지": "대구 동구 신암1동",
        }
        self.assertEqual(identity_strength(a, b), "STRONG_PARTIAL_ADDRESS")

    def test_blank_known_sources_infer_kakao(self):
        cases = [
            {"콜유형": "", "data_source": "app_ocr_individual"},
            {"콜유형": None, "data_source": "drive_ocr_tesseract"},
            {"콜유형": "   ", "data_source": "drive_ocr_layout_v1"},
        ]
        for row in cases:
            self.assertEqual(platform_key(row), "KAKAO")
        self.assertEqual(
            platform_key({"콜유형": "", "data_source": "argos_reconstructed"}),
            "UNKNOWN",
        )

    def test_mixed_platform_never_deduplicates(self):
        a = {
            "id": 1, "날짜": "2026-07-15", "콜유형": "카카오T",
            "배차시각": "20:00", "하차시각": "20:10", "요금": 5000,
        }
        b = {
            "id": 2, "날짜": "2026-07-15", "콜유형": "우버",
            "배차시각": "20:00", "하차시각": "20:10", "요금": 5000,
        }
        self.assertIsNone(identity_strength(a, b))

    def test_completed_drive_beats_verified_reconstruction(self):
        drive = {
            "id": 20, "data_source": "drive_ocr_tesseract",
            "source_id": "src-drive", "verify_status": "unverified",
            "status": "confirmed",
        }
        reconstructed = {
            "id": 10, "data_source": "argos_reconstructed",
            "source_id": None, "verify_status": "verified",
            "status": "confirmed",
        }
        completed = {"src-drive"}
        self.assertGreater(
            evidence_rank(drive, completed),
            evidence_rank(reconstructed, completed),
        )

    def test_select_canonical_preserves_unique_midnight_row(self):
        rows = [
            {
                "id": 100, "날짜": "2026-07-15", "콜유형": "카카오T",
                "배차시각": "19:59", "하차시각": "20:05", "요금": 5400,
                "출발지": "동구 신암2동", "도착지": "북구 복현1동",
                "data_source": "argos_reconstructed", "verify_status": "verified",
                "status": "confirmed", "source_id": None,
            },
            {
                "id": 200, "날짜": "2026-07-15", "콜유형": "카카오T",
                "배차시각": "19:59", "하차시각": "20:05", "요금": 5400,
                "출발지": "대구 동구 신암2동", "도착지": "대구 북구 복현1동",
                "data_source": "drive_ocr_tesseract", "verify_status": "unverified",
                "status": "confirmed", "source_id": "src-drive",
            },
            {
                "id": 201, "날짜": "2026-07-15", "콜유형": "카카오T",
                "배차시각": "00:13", "하차시각": "00:19", "요금": 7800,
                "출발지": "대구 중구 동인동", "도착지": "대구 수성구 만촌1동",
                "data_source": "drive_ocr_tesseract", "verify_status": "unverified",
                "status": "confirmed", "source_id": "src-drive",
            },
        ]
        result = select_canonical(rows, {"src-drive"})
        ids = {row["id"] for row in result["rows"]}
        self.assertEqual(ids, {200, 201})
        self.assertEqual(result["suppressed"][0]["row_id"], 100)
        self.assertEqual(platform_key(rows[2]), "KAKAO")


if __name__ == "__main__":
    unittest.main()
