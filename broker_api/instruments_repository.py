"""
Provides a repository for querying instrument data from the database.
"""
import calendar
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from database import SessionLocal


class InstrumentsRepository:
    """
    A schema-aware repository for efficient options lookups.
    """

    def __init__(self, db: Optional[Session | Callable[[], Session]] = None):
        self.db = db

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

    def normalize_underlying_symbol(self, input_symbol: str) -> tuple[str, str]:
        """
        Normalizes common index symbols to their database representations.
        Returns (options_underlying_key, spot_tradingsymbol).
        """
        symbol_map = {
            "NIFTY": ("NIFTY", "NIFTY 50"),
            "BANKNIFTY": ("BANKNIFTY", "NIFTY BANK"),
        }
        return symbol_map.get(input_symbol.upper(), (input_symbol, input_symbol))

    def get_spot_token(self, underlying_symbol: str) -> Optional[int]:
        """
        Retrieves the instrument token for a given spot or index symbol.
        """
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

    def get_expiries(self, underlying: str, today: date) -> List[date]:
        """
        Fetches all available option expiries for an underlying on or after a given date.
        """
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
        """
        Classifies a list of expiry dates into weekly and monthly options.
        Monthly expiries are the last Thursday of the month.
        """
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
        """
        Selects target expiries based on an aggressive profile.
        - Weeklies: Next 4 weekly + next 2 monthly.
        - No weeklies: Next 3 monthly.
        """
        weeklies, monthlies = self.classify_weekly_monthly(expiries)
        if weeklies:
            target = sorted(list(set(weeklies[:4] + monthlies[:2])))
        else:
            target = monthlies[:3]
        return target

    def get_expiries_grouped(
        self, underlying: str, today: date
    ) -> Dict[date, List[date]]:
        """
        Returns a dict keyed by the first day of each month with a sorted list
        of distinct expiries for that month, for expiries >= today.
        """
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
        """
        Identifies the monthly expiry (max date) for each month in the grouped dict.
        """
        return {ym: max(expiries) for ym, expiries in grouped.items()}

    def select_current_weeklies_plus_three_monthlies(
        self, underlying: str, today: date
    ) -> List[date]:
        """
        Selects a rolling window of 4 weekly and 3 monthly expiries.
        """
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

        target_weeklies = weeklies[:4]
        target_monthlies = monthlies[:3]

        target_expiries = sorted(list(set(target_weeklies + target_monthlies)))
        return target_expiries

    def get_distinct_strikes(self, underlying: str, expiry: date) -> List[float]:
        """
        Retrieves all distinct strikes for a given underlying and expiry.
        """
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
        """
        Fetches option instrument details for a list of strikes.
        """
        if not strikes:
            return []
        query = text(
            """
            SELECT instrument_token, tradingsymbol, strike, option_type
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
        """
        Computes the modal difference between neighboring strikes.
        """
        if len(strikes) < 2:
            return None
        diffs = [strikes[i] - strikes[i - 1] for i in range(1, len(strikes))]
        if not diffs:
            return None
        return max(set(diffs), key=diffs.count)

    def nearest_strike(self, strikes: List[float], ref: float) -> Optional[float]:
        """
        Finds the nearest strike to a reference price.
        """
        if not strikes:
            return None
        return min(strikes, key=lambda k: abs(k - ref))

    def window_strikes(self, strikes: List[float], atm: float, k: int) -> List[float]:
        """
        Returns a window of 2k+1 strikes centered around the ATM strike.
        """
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
    
    # ═══════════════════════════════════════════════════════════════════════════
    # PHASE 3: DELTA-BASED STRIKE SELECTION
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_atm_strike(self, underlying: str, current_spot: float, expiry: date) -> Optional[float]:
        """
        Calculate ATM strike closest to spot price.
        
        Args:
            underlying: Index symbol (e.g., 'NIFTY')
            current_spot: Current spot/index price
            expiry: Target expiry date
        
        Returns:
            ATM strike price, or None if no strikes available
        """
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
        """
        Get N strikes centered around ATM.
        
        Args:
            atm_strike: The ATM strike
            all_strikes: All available strikes (sorted)
            count: Total number of strikes to return (default: 5)
        
        Returns:
            List of strikes centered around ATM
        """
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
    
    def get_lot_size(self, instrument_token: int) -> Optional[int]:
        """
        Get lot size for an instrument token.
        
        Args:
            instrument_token: Instrument token
        
        Returns:
            Lot size, or None if not found
        """
        query = text(
            """
            SELECT lot_size FROM kite_instruments
            WHERE instrument_token = :token LIMIT 1
            """
        )
        with self._session_scope() as db:
            result = db.execute(query, {"token": instrument_token}).scalar_one_or_none()
            return result

    def get_instrument_by_exchange_symbol(self, exchange: str, tradingsymbol: str) -> Optional[Dict[str, object]]:
        """
        Look up a single instrument by exchange and tradingsymbol.

        Returns a minimal normalized mapping used by execution paths.
        """
        query = text(
            """
            SELECT instrument_token, exchange, tradingsymbol, lot_size, instrument_type, last_price, tick_size
            FROM kite_instruments
            WHERE exchange = :exchange AND tradingsymbol = :tradingsymbol
            LIMIT 1
            """
        )
        with self._session_scope() as db:
            row = db.execute(
                query,
                {
                    "exchange": str(exchange or "").strip().upper(),
                    "tradingsymbol": str(tradingsymbol or "").strip().upper(),
                },
            ).mappings().first()
            return dict(row) if row else None
