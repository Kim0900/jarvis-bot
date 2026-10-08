import unittest

from ingestion_notify import format_ingestion_message


class IngestionNotifyTests(unittest.TestCase):
    def test_received(self):
        msg = format_ingestion_message(
            stage="RECEIVED",
            kind="gpx",
            source_id="1234567890abcdefgh",
            file_name="drive.gpx",
        )
        self.assertIn("📥 데이터 인입 확인", msg)
        self.assertIn("GPX 운행경로", msg)
        self.assertIn("처리 시작", msg)

    def test_daily_completed(self):
        msg = format_ingestion_message(
            stage="COMPLETED",
            kind="call_image",
            source_id="abc",
            result={
                "format": "daily_history",
                "saved_count": 4,
                "duplicate_skipped_count": 2,
                "page_date": "2026-10-07",
                "displayed_count": 6,
                "displayed_amount": 31300,
            },
        )
        self.assertIn("카카오 일별 운행이력", msg)
        self.assertIn("신규 4건 / 중복 2건", msg)
        self.assertIn("31,300원", msg)

    def test_s700_completed(self):
        msg = format_ingestion_message(
            stage="COMPLETED",
            kind="s700",
            result={
                "inserted": 7,
                "duplicates_existing": 1,
                "conflicts_existing": [],
                "insert_failed": [],
                "match": {"MATCHED": 4, "UNMATCHED": 3},
            },
        )
        self.assertIn("신규 7건 / 중복 1건 / 충돌 0건 / 실패 0건", msg)
        self.assertIn("MATCHED 4 / UNMATCHED 3", msg)

    def test_gpx_duplicate(self):
        msg = format_ingestion_message(
            stage="COMPLETED",
            kind="gpx",
            result={
                "inserted": 0,
                "duplicate": True,
                "session": {"service_date": "2026-10-07", "point_count": 7148},
            },
        )
        self.assertIn("중복 파일", msg)
        self.assertIn("7,148", msg)

    def test_error_is_bounded_and_reports_retry(self):
        msg = format_ingestion_message(
            stage="ERROR",
            kind="call_image",
            result={"format": "uber_trip_detail"},
            error="X" * 2000,
            retryable=False,
        )
        self.assertIn("❌ 데이터 처리 오류", msg)
        self.assertIn("Uber 운행 상세", msg)
        self.assertIn("재시도: 불가", msg)
        self.assertLess(len(msg), 1300)


if __name__ == "__main__":
    unittest.main()
