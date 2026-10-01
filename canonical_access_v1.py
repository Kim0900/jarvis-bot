"""Canonical Supabase access contract v1.

Keeps the internal RPC secret requirement explicit and testable outside the
large bot module.
"""

INTERNAL_RPC_HEADER = "X-MAGI-RPC-Secret"


def build_canonical_headers(base_headers, secret):
    if not secret:
        raise RuntimeError("SUPABASE_INTERNAL_RPC_SECRET is required for canonical reads")
    return {**dict(base_headers or {}), INTERNAL_RPC_HEADER: secret}


def canonical_rpc_payload(start_date=None, end_date=None):
    return {
        "p_start_date": str(start_date) if start_date else None,
        "p_end_date": str(end_date) if end_date else None,
    }
