"""Task #180: SQL migration contract; not executed in production.

Preserve platform_date, business_date, and canonical identity.
Only uniquely evidenced Kakao page dates qualify for backfill.
"""
MIGRATION_SQL = "ALTER TABLE public.raw_calls ADD COLUMN IF NOT EXISTS ledger_date date;"

BACKFILL_SQL = """
WITH unique_pages AS (
  SELECT source_id, MIN(page_date) AS page_date
  FROM public.kakao_daily_page_evidence
  WHERE page_date IS NOT NULL
  GROUP BY source_id
  HAVING COUNT(DISTINCT page_date) = 1
)
UPDATE public.raw_calls AS r
SET ledger_date = p.page_date
FROM unique_pages AS p
WHERE r.source_id = p.source_id AND r.ledger_date IS NULL
  AND r."콜유형" = '카카오T'
  AND r.data_source IN ('drive_ocr_layout_v1', 'drive_ocr_tesseract');
"""

PREFLIGHT_SQL = """
SELECT COUNT(*) AS candidate_rows, COUNT(DISTINCT r.source_id) AS candidate_sources
FROM public.raw_calls AS r
JOIN (
  SELECT source_id FROM public.kakao_daily_page_evidence
  WHERE page_date IS NOT NULL
  GROUP BY source_id HAVING COUNT(DISTINCT page_date) = 1
) p ON r.source_id = p.source_id
WHERE r."콜유형" = '카카오T'
  AND r.data_source IN ('drive_ocr_layout_v1', 'drive_ocr_tesseract');
"""

def test_sql_contract():
    assert "COUNT(DISTINCT page_date) = 1" in BACKFILL_SQL
    assert "r.ledger_date IS NULL" in BACKFILL_SQL
    assert "drive_ocr_layout_v1" in BACKFILL_SQL
    assert "drive_ocr_tesseract" in BACKFILL_SQL
    assert "SET platform_date" not in BACKFILL_SQL
    assert "SET business_date" not in BACKFILL_SQL

if __name__ == "__main__":
    test_sql_contract()
    print("PASS")
