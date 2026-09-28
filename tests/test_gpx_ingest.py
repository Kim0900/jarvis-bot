import unittest
from gpx_ingest import GpxParseError, build_gpx_session_row, parse_gpx_text

SAMPLE = """<?xml version="1.0"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>
<trkpt lat="35.0000" lon="128.0000"><time>2026-09-28T15:00:00Z</time></trkpt>
<trkpt lat="35.0001" lon="128.0001"><time>2026-09-28T15:10:00Z</time></trkpt>
</trkseg></trk></gpx>"""

class TestGpxIngest(unittest.TestCase):
    def test_kst_service_date(self):
        r = parse_gpx_text(SAMPLE)
        self.assertEqual(r["service_date"], "2026-09-29")
        self.assertEqual(r["point_count"], 2)
        self.assertEqual(r["duration_h"], 0.17)
        self.assertGreater(r["distance_km"], 0)

    def test_row_has_provenance_and_stable_hash(self):
        a, _ = build_gpx_session_row(SAMPLE, "file-1", "a.gpx")
        b, _ = build_gpx_session_row(SAMPLE, "file-2", "copy.gpx")
        self.assertEqual(a["source_sha256"], b["source_sha256"])
        self.assertEqual(a["source_file_id"], "file-1")

    def test_invalid_xml_fails_closed(self):
        with self.assertRaises(GpxParseError):
            parse_gpx_text("<gpx>")

    def test_no_timestamp_fails_closed(self):
        with self.assertRaises(GpxParseError):
            parse_gpx_text('<gpx><trkpt lat="35" lon="128"/></gpx>')

    def test_source_id_required(self):
        with self.assertRaises(GpxParseError):
            build_gpx_session_row(SAMPLE, None, "a.gpx")

if __name__ == "__main__":
    unittest.main()
