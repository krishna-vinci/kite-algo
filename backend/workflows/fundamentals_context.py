"""Production fundamentals context for the alerts worker.

Bridges the alerts predicate layer to the existing fundamentals pipeline:
``public.fundamentals_features`` (populated by the ``fundamentals`` package's
Screener.in sync — nightly scheduler in the FastAPI app, on-demand via
``POST /algo-workers/worker/fundamentals/sync``).

Honest semantics (spec §5.8): values are the LATEST stored snapshot carrying
acquisition metadata (``scraped_at`` / ``as_of_date`` / ``statement_scope``).
There is no point-in-time fundamentals history; a replay evaluates the same
current snapshot. Missing rows leave fields unknown (never false).

Coverage: the source is NSE-centric bare-symbol keyed, so only
``NSE:SYMBOL`` keys resolve; other exchanges report unavailable.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from backend.workflows.registry import (
    FUNDAMENTALS_COLUMN_MAP,
    FUNDAMENTALS_FIELDS,
)

logger = logging.getLogger(__name__)

__all__ = ["FundamentalsLoader", "FUNDAMENTALS_COLUMN_MAP"]

_SELECT_COLUMNS = list(FUNDAMENTALS_COLUMN_MAP.values()) + [
    "scraped_at",
    "as_of_date",
    "statement_scope",
]

# Context keys carrying acquisition metadata alongside the values.
ACQUIRED_AT_KEY = "fundamentals.acquired_at"
AS_OF_KEY = "fundamentals.as_of_date"
SCOPE_KEY = "fundamentals.statement_scope"


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def bare_symbol(instrument_key: str) -> Optional[str]:
    """``NSE:INFY`` -> ``INFY``; only NSE keys map to the source.

    The fundamentals pipeline stores bare symbols (Screener.in is
    NSE-listing oriented); a BSE/NFO/MCX key must not silently match a
    same-ticker NSE row.
    """
    exchange, sep, symbol = instrument_key.partition(":")
    if not sep:
        return None
    if exchange.upper() != "NSE":
        return None
    return symbol if symbol else None


class FundamentalsLoader:
    """Caches the latest ``fundamentals_features`` row per bare symbol.

    Thread-safe; per-symbol TTL memoization keeps the per-tick dispatch path
    off the database. A failed or absent lookup is not cached as a value —
    the next dispatch retries after ``error_ttl_seconds``.
    """

    def __init__(
        self,
        session_factory: Any,
        *,
        ttl_seconds: float = 6 * 3600,
        error_ttl_seconds: float = 300,
        statement_scope: str = "consolidated",
    ) -> None:
        self._session_factory = session_factory
        self._ttl = float(ttl_seconds)
        self._error_ttl = float(error_ttl_seconds)
        self._statement_scope = statement_scope
        self._lock = threading.Lock()
        # symbol -> (monotonic_ts, context_dict_or_None)
        self._cache: dict[str, tuple[float, Optional[dict]]] = {}

    # ------------------------------------------------------------------
    def context_for(self, instrument_key: str, *, now: Optional[float] = None) -> Optional[dict]:
        """Latest-snapshot context dict for one instrument, or ``None``.

        The returned mapping (when not None) always contains the numeric
        fields present in the row plus acquisition metadata keys; predicate
        resolution reads ``context[field_name]`` and treats absence as
        unknown.
        """
        symbol = bare_symbol(instrument_key)
        if symbol is None:
            return None
        current = time.monotonic() if now is None else now
        with self._lock:
            cached = self._cache.get(symbol)
            if cached is not None:
                ts, payload = cached
                ttl = self._ttl if payload is not None else self._error_ttl
                if current - ts < ttl:
                    return dict(payload) if payload is not None else None
        payload = self._fetch(symbol)
        with self._lock:
            self._cache[symbol] = (current, payload)
        return dict(payload) if payload is not None else None

    # ------------------------------------------------------------------
    def freshness(self, instrument_key: str) -> Optional[dict]:
        """Acquisition metadata only (for health/evidence), or ``None``."""
        ctx = self.context_for(instrument_key)
        if ctx is None:
            return None
        return {
            "acquired_at": ctx.get(ACQUIRED_AT_KEY),
            "as_of_date": ctx.get(AS_OF_KEY),
            "statement_scope": ctx.get(SCOPE_KEY),
        }

    def invalidate(self, symbol: Optional[str] = None) -> None:
        with self._lock:
            if symbol is None:
                self._cache.clear()
            else:
                self._cache.pop(symbol, None)

    # ------------------------------------------------------------------
    def _fetch(self, symbol: str) -> Optional[dict]:
        query = (
            "SELECT " + ", ".join(_SELECT_COLUMNS) +
            " FROM public.fundamentals_features "
            "WHERE symbol = :symbol AND statement_scope = :scope"
        )
        try:
            session = self._session_factory()
        except Exception:
            logger.warning("fundamentals session unavailable", exc_info=True)
            return None
        try:
            row = session.execute(
                query, {"symbol": symbol, "scope": self._statement_scope}
            ).mappings().first()
        except Exception:
            logger.warning(
                "fundamentals lookup failed for %s", symbol, exc_info=True
            )
            return None
        finally:
            try:
                session.close()
            except Exception:
                pass
        if row is None:
            return None
        context: dict = {}
        for field in FUNDAMENTALS_FIELDS:
            column = FUNDAMENTALS_COLUMN_MAP.get(field)
            if column is None:
                continue
            value = _finite(row.get(column))
            if value is not None:
                context[field] = value
        scraped_at = row.get("scraped_at")
        if isinstance(scraped_at, datetime):
            if scraped_at.tzinfo is None:
                scraped_at = scraped_at.replace(tzinfo=timezone.utc)
            context[ACQUIRED_AT_KEY] = scraped_at.astimezone(timezone.utc).isoformat()
        as_of = row.get("as_of_date")
        if as_of is not None:
            context[AS_OF_KEY] = str(as_of)
        scope = row.get("statement_scope")
        if scope:
            context[SCOPE_KEY] = str(scope)
        return context
