import unittest

from canonical_access_v1 import (
    INTERNAL_RPC_HEADER,
    build_canonical_headers,
    canonical_rpc_payload,
)


class CanonicalAccessV1Tests(unittest.TestCase):
    def test_missing_secret_is_rejected(self):
        with self.assertRaises(RuntimeError):
            build_canonical_headers({"apikey": "anon"}, "")

    def test_internal_secret_header_is_added(self):
        headers = build_canonical_headers(
            {"apikey": "anon", "Authorization": "Bearer anon"},
            "secret-value",
        )
        self.assertEqual(headers[INTERNAL_RPC_HEADER], "secret-value")
        self.assertEqual(headers["apikey"], "anon")

    def test_rpc_payload_is_date_scoped(self):
        self.assertEqual(
            canonical_rpc_payload("2026-07-13", "2026-07-15"),
            {"p_start_date": "2026-07-13", "p_end_date": "2026-07-15"},
        )


if __name__ == "__main__":
    unittest.main()
