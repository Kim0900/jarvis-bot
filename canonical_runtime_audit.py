"""Task #164 canonical runtime regression audit.

This is an explicit, opt-in operational diagnostic. It never mutates raw trip data.
It validates the real application read path (anon + X-MAGI-RPC-Secret) against
known production regression dates and expected canonical provenance.
"""

from collections import defaultdict

EXPECTED = {
    "2026-07-13": {
        "count": 16,
        "sum": 144500,
        "sources": {
            "drive_ocr_tesseract": (12, 106000),
            "argos_reconstructed": (4, 38500),
        },
    },
    "2026-07-15": {
        "count": 17,
        "sum": 145400,
        "sources": {
            "drive_ocr_tesseract": (11, 102600),
            "argos_reconstructed": (5, 28100),
            "app_ocr_individual": (1, 14700),
        },
    },
}


def evaluate_canonical_runtime_rows(rows):
    by_date = defaultdict(list)
    for row in rows or []:
        date_value = str(row.get("날짜") or "")
        if date_value in EXPECTED:
            by_date[date_value].append(row)

    results = {}
    overall = True
    for date_value, expected in EXPECTED.items():
        date_rows = by_date.get(date_value, [])
        count = len(date_rows)
        fare_sum = sum(int(r.get("요금") or 0) for r in date_rows)

        sources = defaultdict(lambda: [0, 0])
        for row in date_rows:
            src = str(row.get("data_source") or "(NULL)")
            sources[src][0] += 1
            sources[src][1] += int(row.get("요금") or 0)

        expected_sources = {
            src: [vals[0], vals[1]]
            for src, vals in expected["sources"].items()
        }
        actual_sources = dict(sources)

        passed = (
            count == expected["count"]
            and fare_sum == expected["sum"]
            and actual_sources == expected_sources
        )
        overall = overall and passed
        results[date_value] = {
            "pass": passed,
            "count": count,
            "sum": fare_sum,
            "sources": actual_sources,
            "expected_count": expected["count"],
            "expected_sum": expected["sum"],
            "expected_sources": expected_sources,
        }

    return {"pass": overall, "dates": results}
