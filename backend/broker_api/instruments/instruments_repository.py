"""
Provides a repository for querying instrument data.

Instrument lookups go to the Go market-runtime via HTTP (same Docker bridge,
~0.76 ms) and fall back to PostgreSQL when Go is unreachable.
"""
import asyncio
import calendar
import logging
import time
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlencode

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app.database import SessionLocal

logger = logging.getLogger("instruments")

# ── Go HTTP client singletons ────────────────────────────────────────────────
_GO_BASE_URL = "http://market-runtime:8780"
_sync_client: Optional[httpx.Client] = None
_async_client: Optional[httpx.AsyncClient] = None


def _get_sync() -> httpx.Client:
    global _sync_client
    if _sync_client is None:
        _sync_client = httpx.Client(
            base_url=_GO_BASE_URL,
            timeout=httpx.Timeout(0.5, connect=0.3),
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )
    return _sync_client


def _get_async() -> httpx.AsyncClient:
    global _async_client
    if _async_client is None:
        _async_client = httpx.AsyncClient(
            base_url=_GO_BASE_URL,
            timeout=httpx.Timeout(0.5, connect=0.3),
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )
    return _async_client


# ── Circuit breaker ──────────────────────────────────────────────────────────

class _CircuitBreaker:
    """State machine protecting Go HTTP calls.

    404 (instrument not found) is NOT a failure — Go is healthy, just
    doesn't have the data.  Only 5xx and connection errors count.
    """

    def __init__(self):
        self._failures = 0
        self._opened_at = 0.0
        self._state = "closed"  # closed | open | half_open
        self._threshold = 3
        self._recovery = 10  # seconds

    def _should_try(self) -> bool:
        if self._state == "closed":
            return True
        if self._state == "open":
            if time.monotonic() - self._opened_at > self._recovery:
                self._state = "half_open"
                return True
            return False
        return True  # half_open

    def success(self) -> None:
        self._failures = 0
        self._state = "closed"

    def not_found(self) -> None:
        """404 — Go is healthy, just doesn't have the instrument."""
        self._failures = 0
        self._state = "closed"

    def failure(self) -> None:
        self._failures += 1
        if self._state in ("half_open", "closed") and self._failures >= self._threshold:
            self._state = "open"
            self._opened_at = time.monotonic()
            logger.warning("Go instrument HTTP circuit breaker OPEN (3 consecutive failures)")


_sync_breaker = _CircuitBreaker()
_async_breaker = _CircuitBreaker()


# ── Repository ───────────────────────────────────────────────────────────────

class InstrumentsRepository:
    """Instrument lookups via Go HTTP, with PostgreSQL fallback."""

    def __init__(self, db: Optional[Session | Callable[[], Session]] = None):
        self.db = db

    # ── session helpers ──────────────────────────────────────────────────

    @contextmanager
    def _session_scope(self) -> Iterator[Session]:
        if callable(self.db):
            session = self.db()
            try:
                yield session
            finally:
                session.close()
            return
        if self.db is not None:
            yield self.db
            return
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    # ── symbol normalisation ─────────────────────────────────────────────

    def normalize_underlying_symbol(self, input_symbol: str) -> tuple[str, str]:
        symbol_map = {
            "NIFTY": ("NIFTY", "NIFTY 50"),
            "BANKNIFTY": ("BANKNIFTY", "NIFTY BANK"),
        }
        return symbol_map.get(input_symbol.upper(), (input_symbol, input_symbol))

    def get_spot_token(self, underlying_symbol: str) -> Optional[int]:
        _, spot_tradingsymbol = self.normalize_underlying_symbol(underlying_symbol)
        if "NIFTY" in spot_tradingsymbol:
            query = text(
                "SELECT instrument_token FROM kite_instruments WHERE segment='INDICES' AND tradingsymbol=:ts LIMIT 1"
            )
        else:
            query = text(
                "SELECT instrument_token FROM kite_instruments WHERE exchange='NSE' AND instrument_type='EQ' AND tradingsymbol=:ts LIMIT 1"
            )
        with self._session_scope() as db:
            result = db.execute(query, {"ts": spot_tradingsymbol}).scalar_one_or_none()
            return result

    # ── expiry helpers ───────────────────────────────────────────────────

    def get_expiries(self, underlying: str, today: date) -> List[date]:
        query = text(
            """
            SELECT DISTINCT expiry FROM kite_instruments
            WHERE exchange='NFO' AND underlying=:underlying AND instrument_type IN ('CE','PE')
            AND expiry >= :today ORDER BY expiry ASC
            """
        )
        with self._session_scope() as db:
            result = db.execute(
                query, {"underlying": underlying, "today": today}
            ).fetchall()
            return [row[0] for row in result]

    def classify_weekly_monthly(
        self, expiries: List[date]
    ) -> tuple[List[date], List[date]]:
        weeklies, monthlies = [], []
        for expiry in expiries:
            last_day = calendar.monthrange(expiry.year, expiry.month)[1]
            last_thursday = max(
                [
                    d
                    for d in range(last_day, 0, -1)
                    if date(expiry.year, expiry.month, d).weekday() == 3
                ]
            )
            if expiry.day == last_thursday:
                monthlies.append(expiry)
            else:
                weeklies.append(expiry)
        return sorted(weeklies), sorted(monthlies)

    def select_target_expiries(self, expiries: List[date]) -> List[date]:
        weeklies, monthlies = self.classify_weekly_monthly(expiries)
        if weeklies:
            target = sorted(list(set(weeklies[:4] + monthlies[:2])))
        else:
            target = monthlies[:3]
        return target

    def get_expiries_grouped(
        self, underlying: str, today: date
    ) -> Dict[date, List[date]]:
        query = text(
            """
            SELECT date_trunc('month', expiry)::date AS ym,
                   array_agg(DISTINCT expiry ORDER BY expiry) AS expiries
            FROM kite_instruments
            WHERE exchange='NFO' AND instrument_type IN ('CE','PE')
            AND underlying=:underlying AND expiry >= :today
            GROUP BY 1 ORDER BY 1 ASC
            """
        )
        with self._session_scope() as db:
            result = db.execute(
                query, {"underlying": underlying, "today": today}
            ).mappings()
            return {row["ym"]: row["expiries"] for row in result}

    def pick_monthly_per_month(
        self, grouped: Dict[date, List[date]]
    ) -> Dict[date, date]:
        return {ym: max(expiries) for ym, expiries in grouped.items()}

    def select_current_weeklies_plus_three_monthlies(
        self, underlying: str, today: date
    ) -> List[date]:
        all_expiries = self.get_expiries(underlying, today)
        if not all_expiries:
            return []
        grouped = self.get_expiries_grouped(underlying, today)
        monthly_expiries_map = self.pick_monthly_per_month(grouped)
        all_monthly_expiries = set(monthly_expiries_map.values())
        weeklies = sorted(
            [exp for exp in all_expiries if exp not in all_monthly_expiries]
        )
        monthlies = sorted(list(all_monthly_expiries))
        normalized_underlying = (underlying or "").strip().upper()
        if normalized_underlying == "NIFTY":
            target_weeklies = weeklies[:3]
            target_monthlies = monthlies[:2]
        else:
            target_weeklies = weeklies[:4]
            target_monthlies = monthlies[:3]
        target_expiries = sorted(list(set(target_weeklies + target_monthlies)))
        return target_expiries

    # ── strike helpers ───────────────────────────────────────────────────

    def get_distinct_strikes(self, underlying: str, expiry: date) -> List[float]:
        query = text(
            """
            SELECT DISTINCT strike FROM kite_instruments
            WHERE exchange='NFO' AND underlying=:underlying AND expiry=:expiry
            AND instrument_type IN ('CE','PE') ORDER BY strike ASC
            """
        )
        with self._session_scope() as db:
            result = db.execute(
                query, {"underlying": underlying, "expiry": expiry}
            ).fetchall()
            return [row[0] for row in result]

    def get_option_instruments_for_strikes(
        self, underlying: str, expiry: date, strikes: List[float]
    ) -> List[Dict]:
        if not strikes:
            return []
        query = text(
            """
            SELECT instrument_token, tradingsymbol, strike, option_type, lot_size
            FROM kite_instruments
            WHERE exchange='NFO' AND underlying=:underlying AND expiry=:expiry
            AND strike IN :strikes AND instrument_type IN ('CE', 'PE')
            """
        )
        with self._session_scope() as db:
            result = db.execute(
                query,
                {"underlying": underlying, "expiry": expiry, "strikes": tuple(strikes)},
            ).mappings().all()
            return [dict(row) for row in result]

    def derive_strike_step(self, strikes: List[float]) -> Optional[float]:
        if len(strikes) < 2:
            return None
        diffs = [strikes[i] - strikes[i - 1] for i in range(1, len(strikes))]
        if not diffs:
            return None
        return max(set(diffs), key=diffs.count)

    def nearest_strike(self, strikes: List[float], ref: float) -> Optional[float]:
        if not strikes:
            return None
        return min(strikes, key=lambda k: abs(k - ref))

    def window_strikes(self, strikes: List[float], atm: float, k: int) -> List[float]:
        if not strikes:
            return []
        atm_strike = self.nearest_strike(strikes, atm)
        if atm_strike is None:
            return []
        try:
            atm_idx = strikes.index(atm_strike)
            start = max(0, atm_idx - k)
            end = min(len(strikes), atm_idx + k + 1)
            return strikes[start:end]
        except ValueError:
            return []

    # ═══════════════════════════════════════════════════════════════════════
    # PHASE 3: DELTA-BASED STRIKE SELECTION
    # ═══════════════════════════════════════════════════════════════════════

    def get_atm_strike(self, underlying: str, current_spot: float, expiry: date) -> Optional[float]:
        strikes = self.get_distinct_strikes(underlying, expiry)
        if not strikes:
            return None
        return self.nearest_strike(strikes, current_spot)

    def get_strikes_around_atm(
        self,
        atm_strike: float,
        all_strikes: List[float],
        count: int = 5
    ) -> List[float]:
        if not all_strikes or atm_strike not in all_strikes:
            return []
        try:
            atm_index = all_strikes.index(atm_strike)
            half = count // 2
            start = max(0, atm_index - half)
            end = min(len(all_strikes), atm_index + half + 1)
            return all_strikes[start:end]
        except ValueError:
            return []

    # ── HTTP lookups (with circuit breaker + SQL fallback) ───────────────

    def _lookup_token_via_http(self, token: int) -> Optional[Dict[str, Any]]:
        """Synchronous HTTP lookup via Go.  Returns None if not found."""
        if not _sync_breaker._should_try():
            return None  # circuit open → let caller use SQL
        try:
            resp = _get_sync().get(f"/instruments/{token}")
        except Exception:
            _sync_breaker.failure()
            return None

        if resp.status_code == 200:
            _sync_breaker.success()
            return resp.json()
        elif resp.status_code == 404:
            _sync_breaker.not_found()
            return None
        else:
            _sync_breaker.failure()
            return None

    async def _lookup_token_via_http_async(self, token: int) -> Optional[Dict[str, Any]]:
        """Async HTTP lookup via Go."""
        if not _async_breaker._should_try():
            return None
        try:
            resp = await _get_async().get(f"/instruments/{token}")
        except Exception:
            _async_breaker.failure()
            return None

        if resp.status_code == 200:
            _async_breaker.success()
            return resp.json()
        elif resp.status_code == 404:
            _async_breaker.not_found()
            return None
        else:
            _async_breaker.failure()
            return None

    def _lookup_symbol_via_http(self, exchange: str, symbol: str) -> Optional[Dict[str, Any]]:
        """Synchronous symbol lookup via Go."""
        if not _sync_breaker._should_try():
            return None
        try:
            params = urlencode({"exchange": exchange, "symbol": symbol})
            resp = _get_sync().get(f"/instruments/by-symbol?{params}")
        except Exception:
            _sync_breaker.failure()
            return None

        if resp.status_code == 200:
            _sync_breaker.success()
            return resp.json()
        elif resp.status_code == 404:
            _sync_breaker.not_found()
            return None
        else:
            _sync_breaker.failure()
            return None

    # ── public lookup API ────────────────────────────────────────────────

    def get_instrument_by_token(self, instrument_token: int) -> Optional[Dict[str, object]]:
        """HTTP-first (Go), SQL fallback."""
        try:
            token = int(instrument_token)
        except (TypeError, ValueError):
            return None

        result = self._lookup_token_via_http(token)
        if result is not None:
            return result  # type: ignore[return-value]
        return self._get_instrument_by_token_sql(token)

    def get_instrument_by_exchange_symbol(self, exchange: str, tradingsymbol: str) -> Optional[Dict[str, object]]:
        """HTTP-first (Go), SQL fallback."""
        ex = str(exchange or "").strip().upper()
        sym = str(tradingsymbol or "").strip().upper()
        if not ex or not sym:
            return None

        result = self._lookup_symbol_via_http(ex, sym)
        if result is not None:
            return result  # type: ignore[return-value]
        return self._get_instrument_by_exchange_symbol_sql(ex, sym)

    def resolve_market_symbol(self, symbol: str) -> Optional[Dict[str, object]]:
        raw = str(symbol or "").strip().upper()
        if not raw:
            return None
        if raw.isdigit():
            if len(raw) > 10:
                return None
            return self.get_instrument_by_token(int(raw))
        if ":" not in raw:
            return None
        exchange, tradingsymbol = raw.split(":", 1)
        exchange = exchange.strip()
        tradingsymbol = tradingsymbol.strip()
        if not exchange or not tradingsymbol:
            return None
        return self.get_instrument_by_exchange_symbol(exchange, tradingsymbol)

    def get_lot_size(self, instrument_token: int) -> Optional[int]:
        instrument = self.get_instrument_by_token(instrument_token)
        if instrument is not None:
            ls = instrument.get("lot_size")
            if ls is not None:
                return int(ls)
        return self._get_lot_size_sql(instrument_token)

    def search_market_instruments(self, query: str, *, exchange: Optional[str] = None, limit: int = 20) -> List[Dict[str, object]]:
        """Full-text ILIKE search — always hits PostgreSQL."""
        normalized_text = str(query or "").strip().upper()
        if not normalized_text:
            return []
        normalized_exchange = str(exchange or "").strip().upper() or None
        safe_limit = max(1, min(int(limit or 20), 50))
        sql = text(
            """
            SELECT instrument_token, exchange, tradingsymbol, name, instrument_type,
                   segment, tick_size, lot_size, expiry, strike
            FROM kite_instruments
            WHERE (:exchange IS NULL OR exchange = :exchange)
              AND (
                upper(tradingsymbol) LIKE :query
                OR upper(coalesce(name, '')) LIKE :query
              )
            ORDER BY
              CASE WHEN upper(tradingsymbol) = :exact_query THEN 0 ELSE 1 END,
              tradingsymbol ASC
            LIMIT :limit
            """
        )
        with self._session_scope() as db:
            rows = db.execute(
                sql,
                {
                    "query": f"%{normalized_text}%",
                    "exact_query": normalized_text,
                    "exchange": normalized_exchange,
                    "limit": safe_limit,
                },
            ).mappings().all()
            return [dict(row) for row in rows]

    # ── private SQL fallbacks ────────────────────────────────────────────

    def _get_instrument_by_token_sql(self, instrument_token: int) -> Optional[Dict[str, object]]:
        try:
            normalized_token = int(instrument_token)
        except (TypeError, ValueError):
            return None
        if normalized_token <= 0 or normalized_token > 9_999_999_999:
            return None
        query = text(
            """
            SELECT instrument_token, exchange, tradingsymbol, name, instrument_type,
                   segment, tick_size, lot_size, expiry, strike, COALESCE(underlying, '') AS underlying
            FROM kite_instruments
            WHERE instrument_token = :instrument_token
            LIMIT 1
            """
        )
        with self._session_scope() as db:
            row = db.execute(query, {"instrument_token": normalized_token}).mappings().first()
            return dict(row) if row else None

    def _get_instrument_by_exchange_symbol_sql(self, exchange: str, tradingsymbol: str) -> Optional[Dict[str, object]]:
        query = text(
            """
            SELECT instrument_token, exchange, tradingsymbol, lot_size, instrument_type,
                   COALESCE(tick_size, 0) AS tick_size, COALESCE(name, '') AS name,
                   COALESCE(strike, 0) AS strike, COALESCE(underlying, '') AS underlying
            FROM kite_instruments
            WHERE exchange = :exchange AND tradingsymbol = :tradingsymbol
            LIMIT 1
            """
        )
        with self._session_scope() as db:
            row = db.execute(
                query,
                {
                    "exchange": exchange,
                    "tradingsymbol": tradingsymbol,
                },
            ).mappings().first()
            return dict(row) if row else None

    def _get_lot_size_sql(self, instrument_token: int) -> Optional[int]:
        query = text(
            """
            SELECT lot_size FROM kite_instruments
            WHERE instrument_token = :token LIMIT 1
            """
        )
        with self._session_scope() as db:
            result = db.execute(query, {"token": instrument_token}).scalar_one_or_none()
            return result
