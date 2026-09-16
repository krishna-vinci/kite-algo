"""Index scope adapter for fundamentals.

This module is the single place that decides which index universes the
fundamentals domain can serve. Routes, scope validation, and the nightly
scheduler all read it dynamically, so adding a new index later is a one-entry
change here — nothing else is hardcoded.

Membership itself always resolves against the platform's own constituent
store (``public.kite_ticker_tickers`` by ``source_list``), the same source the
0.7.6 constituent snapshot serves. The adapter only gates which keys are
exposed and normalizes their spelling.
"""
from __future__ import annotations

from dataclasses import dataclass

from backend.app.database import get_db_connection


@dataclass(frozen=True)
class IndexScopeAdapter:
    key: str  # canonical scope key as stored in the constituent store
    description: str


# Currently active universes. Add a new index by appending one entry here —
# the API routes, scope validation, and the nightly scheduler pick it up
# automatically with no further changes.
_INDEX_SCOPE_ADAPTERS: dict[str, IndexScopeAdapter] = {
    "Nifty50": IndexScopeAdapter(key="Nifty50", description="NSE Nifty 50 constituents"),
    "Nifty500": IndexScopeAdapter(key="Nifty500", description="NSE Nifty 500 constituents"),
}

_CASE_FOLD: dict[str, str] = {key.casefold(): key for key in _INDEX_SCOPE_ADAPTERS}


def supported_index_scopes() -> list[str]:
    """Canonical keys of every currently supported index scope."""
    return sorted(_INDEX_SCOPE_ADAPTERS)


def is_supported_index(index: str | None) -> bool:
    return _canonical_index_key(index) is not None


def canonical_index_key(index: str | None) -> str:
    """Map any spelling of a supported index to its canonical key."""
    key = _canonical_index_key(index)
    if key is None:
        raise ValueError(f"index must be one of {supported_index_scopes()}")
    return key


def _canonical_index_key(index: str | None) -> str | None:
    if not index:
        return None
    return _CASE_FOLD.get(str(index).strip().casefold())


def resolve_index_symbols(index: str) -> list[str]:
    """Resolve an index scope key to its constituent symbols."""
    canonical = canonical_index_key(index)
    sql = """
        SELECT DISTINCT tradingsymbol FROM public.kite_ticker_tickers
        WHERE source_list = %s AND tradingsymbol IS NOT NULL
        ORDER BY tradingsymbol
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (canonical,))
            rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        raise ValueError(f"no constituents found for index scope '{canonical}'")
    return [row[0] for row in rows]
