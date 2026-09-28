import asyncio
import logging
import math
import os
import time
from datetime import date, datetime, timezone, timedelta
from math import floor
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set
from zoneinfo import ZoneInfo
import numpy as np

from backend.broker_api.instruments.instruments_repository import InstrumentsRepository
from backend.broker_api.orders.market_runtime_client import MarketDataRuntime
from backend.broker_api.options.options_greeks import (
    black76_greeks,
    black76_greeks_arrays,
    implied_vol_from_price_black76,
)
from backend.broker_api.core.redis_events import get_redis
from backend.options.market.redis_cache import (
    OPTION_SNAPSHOT_TTL_SECONDS,
    option_snapshot_v1_key,
    option_snapshot_v1_updates_channel,
    serialize_option_snapshot_v1,
)
from backend.options.market.analytics.max_pain import compute_bounded_max_pain
from backend.options.market.analytics.pcr import compute_put_call_ratio
from backend.options.market.snapshots import build_bounded_strike_window
from backend.platform.options_settings import AVAILABLE_OPTION_UNDERLYINGS


# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Vectorized Computation Flag ---
OPTIONS_SESSIONS_USE_VECTORIZED = True


# Underlyings whose option-chain sessions start automatically at boot and that an
# admission/read path is allowed to start on demand. The CSV env var keeps the
# set operator-controlled; names outside it are ignored rather than guessed.
DEFAULT_AUTOSTART_UNDERLYINGS = ("NIFTY",)


def autostart_underlyings() -> List[str]:
    """The configured auto-start (and known) underlyings, normalized and de-duplicated."""
    raw = os.environ.get("OPTIONS_AUTOSTART_UNDERLYINGS")
    if raw is None:
        raw = ",".join(DEFAULT_AUTOSTART_UNDERLYINGS)
    ordered: List[str] = []
    for part in raw.split(","):
        symbol = part.strip().upper()
        if symbol and symbol not in ordered:
            ordered.append(symbol)
    return ordered


# Constants
TOKEN_CAP = 2500
#: Far expiries widen their strike window to reach roughly this |delta| on both
#: sides (z of the normal CDF: N(-1.2816) ~= 0.10), so a delta-picked short on a
#: monthly is inside the tracked window.
FAR_WINDOW_DELTA_Z = 1.2816
FAR_WINDOW_MAX = 30
YEAR_IN_DAYS = 365.0
MIN_T = 1e-6  # Min time to expiry to avoid zero division
# NSE/NFO options expire at 15:30 IST (Asia/Kolkata), so time-to-expiry must be
# anchored to that wall clock rather than 15:30 UTC.
IST = ZoneInfo("Asia/Kolkata")
EXPIRY_CLOSE_HOUR_IST = 15
EXPIRY_CLOSE_MINUTE_IST = 30


def rank_tokens(ranks: Mapping[int, tuple], cap: int) -> tuple[List[int], List[int]]:
    """Keep the ``cap`` most important tokens: spot, then ATM outwards, near first.

    Deterministic (rank, then token) so a cap never drops spot or the ATM pair
    while a far wing survives.
    """
    ordered = [token for token, _rank in sorted(ranks.items(), key=lambda item: (item[1], item[0]))]
    return ordered[:cap], ordered[cap:]


def _snapshot_market_digest(snapshot: Mapping[str, Any]) -> tuple:
    """Return the cheap market-value subset that warrants a Redis publish."""
    expiries = []
    for expiry_key, expiry_data in sorted((snapshot.get("per_expiry") or {}).items()):
        rows = []
        for row in expiry_data.get("rows") or []:
            sides = []
            for option_type in ("CE", "PE"):
                contract = row.get(option_type) or row.get(option_type.lower()) or {}
                sides.append(
                    (
                        contract.get("ltp"),
                        contract.get("iv"),
                        contract.get("oi"),
                    )
                )
            rows.append((row.get("strike"), *sides))
        expiries.append((expiry_key, expiry_data.get("forward"), tuple(rows)))
    return tuple(expiries)


class OptionsSession:
    """
    Manages the state and computation for a single underlying's options session.
    """

    def __init__(
        self,
        underlying: str,
        manager: "OptionsSessionManager",
        window_size: int = 12,
        cadence_sec: int = 5,
    ):
        self.underlying = underlying
        self.manager = manager
        self.window_size = window_size
        self.cadence_sec = cadence_sec
        self.task: Optional[asyncio.Task] = None
        self.is_running = False

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._dirty: Optional[asyncio.Event] = None
        else:
            self._dirty = asyncio.Event()
        self._last_compute_monotonic = 0.0
        self.tick_driven = bool(getattr(manager, "tick_driven", True))
        self.min_interval_sec = float(
            getattr(
                manager,
                "min_interval_sec",
                max(0.25, float(os.getenv("OPTIONS_CHAIN_MIN_INTERVAL_S", "1.0"))),
            )
        )

        # Session state
        self.spot_token: Optional[int] = None
        self.expiries: List[date] = []
        self.strikes_by_expiry: Dict[date, List[float]] = {}
        self.sigma_by_expiry: Dict[date, float] = {}
        self.desired_tokens: Set[int] = set()
        self.token_ranks: Dict[int, tuple] = {}
        self.snapshot: Dict[str, Any] = {}
        self.last_spot_ltp: Optional[float] = None
        self.last_spot_live: bool = False
        self.last_spot_age_sec: Optional[float] = None
        self.last_expiry_refresh_ts: Optional[datetime] = None
        
        # Instrument cache
        self._instrument_cache: Dict[Any, Any] = {}
        self._cache_ts: Dict[Any, datetime] = {}
        self._cache_ttl = timedelta(seconds=60)
        self._max_pain_cache: Dict[str, tuple[float, Optional[float]]] = {}

    def _dirty_event(self) -> asyncio.Event:
        if self._dirty is None:
            self._dirty = asyncio.Event()
        return self._dirty

    def mark_dirty(self) -> None:
        self._dirty_event().set()

    async def start(self):
        """
        Initializes and starts the session's 5s computation task.
        Includes a priming step to ensure the first snapshot is valid.
        """
        if self.is_running:
            logger.warning(f"Session for {self.underlying} is already running.")
            return

        await self._initialize_instruments()

        # Prime the session by retrying the computation until a valid forward price is calculated.
        # This ensures we don't publish a bad initial snapshot.
        primed = False
        try:
            for i in range(5): # Try up to 5 times (e.g., 5 seconds)
                await self._compute_and_publish()
                # Check if the first expiry has a valid forward price.
                if self.snapshot and self.snapshot.get('per_expiry'):
                    first_expiry_key = next(iter(self.snapshot['per_expiry']), None)
                    if first_expiry_key and self.snapshot['per_expiry'][first_expiry_key].get('forward') is not None:
                        primed = True
                        logger.info(f"Session for {self.underlying} primed successfully on attempt {i+1}.")
                        break
                logger.warning(f"Priming attempt {i+1} for {self.underlying} failed. Retrying in 1s...")
                await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"[{self.underlying}] Failed to prime session due to an unhandled exception: {e}", exc_info=True)
            # We can still proceed, but the session will start with empty data.
            # The main cadence loop has its own error handling.

        if not primed:
            logger.error(f"Failed to prime session for {self.underlying} after multiple attempts. Proceeding with potentially incomplete data.")

        self.is_running = True
        self.task = asyncio.create_task(self._run_cadence())
        logger.info(f"Started options session for {self.underlying}.")

    async def stop(self):
        """
        Stops the session's computation task and clears desired tokens.
        """
        if not self.is_running or not self.task:
            return
        self.is_running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.desired_tokens.clear()
        logger.info(f"Stopped options session for {self.underlying}.")

    async def update_config(self, window_size: int, cadence_sec: int):
        """
        Updates the session's configuration and restarts the task if needed.
        """
        if self.window_size == window_size and self.cadence_sec == cadence_sec:
            return  # No change

        logger.info(
            f"Updating config for {self.underlying}: "
            f"window={self.window_size} -> {window_size}, "
            f"cadence={self.cadence_sec}s -> {cadence_sec}s"
        )
        self.window_size = window_size
        self.cadence_sec = cadence_sec

        if self.is_running and self.task:
            logger.info(f"Restarting task for {self.underlying} due to config change.")
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = asyncio.create_task(self._run_cadence())

    async def _initialize_instruments(self):
        """
        Fetches initial instrument data required for the session.
        """
        repo = self.manager.instrument_repo
        self.spot_token = repo.get_spot_token(self.underlying)
        if not self.spot_token:
            raise ValueError(f"Could not find spot token for {self.underlying}")

        # Initial expiry selection
        await self._refresh_expiries()

    def _get_cached_instruments(self, cache_key: Any, fetch_func) -> Any:
        """
        Retrieves data from the in-memory cache or executes the fetch function
        if the cache is stale or the key does not exist.
        """
        now = datetime.now(timezone.utc)
        if cache_key in self._instrument_cache and cache_key in self._cache_ts:
            if now - self._cache_ts[cache_key] < self._cache_ttl:
                return self._instrument_cache[cache_key]
        
        # Cache miss or stale, fetch and cache
        data = fetch_func()
        self._instrument_cache[cache_key] = data
        self._cache_ts[cache_key] = now
        return data

    async def _refresh_expiries(self):
        """
        Refreshes the target expiries, updates strikes, and prunes state.
        """
        repo = self.manager.instrument_repo
        today = date.today()
        new_expiries = repo.select_current_weeklies_plus_three_monthlies(
            self.underlying, today
        )

        if set(new_expiries) != set(self.expiries):
            logger.info(
                f"Expiries for {self.underlying} changed from {self.expiries} to {new_expiries}"
            )
            self.expiries = new_expiries
            # Prune strikes for removed expiries
            current_expiry_set = set(self.expiries)
            for expiry in list(self.strikes_by_expiry.keys()):
                if expiry not in current_expiry_set:
                    del self.strikes_by_expiry[expiry]
            # Fetch strikes for new expiries
            for expiry in self.expiries:
                if expiry not in self.strikes_by_expiry:
                    cache_key = f"strikes:{self.underlying}:{expiry.isoformat()}"
                    self.strikes_by_expiry[expiry] = self._get_cached_instruments(
                        cache_key,
                        lambda: repo.get_distinct_strikes(self.underlying, expiry)
                    )
        self.last_expiry_refresh_ts = datetime.now(timezone.utc)

    async def _run_cadence(self):
        """
        The main loop for computing Greeks and publishing updates.
        """
        while self.is_running:
            try:
                # Lightweight check to refresh expiries every 60 seconds
                if (
                    not self.last_expiry_refresh_ts
                    or (
                        datetime.now(timezone.utc) - self.last_expiry_refresh_ts
                    ).total_seconds()
                    >= 60
                ):
                    await self._refresh_expiries()

                if self.tick_driven:
                    try:
                        await asyncio.wait_for(
                            self._dirty_event().wait(), timeout=self.cadence_sec
                        )
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(self.cadence_sec)
                self._dirty_event().clear()
                min_gap = max(0.25, float(self.min_interval_sec))
                wait = min_gap - (time.monotonic() - self._last_compute_monotonic)
                if wait > 0:
                    await asyncio.sleep(wait)
                await self._compute_and_publish()
                self._last_compute_monotonic = time.monotonic()

            except asyncio.CancelledError:
                logger.info(f"Cadence task for {self.underlying} was cancelled.")
                break
            except Exception as e:
                logger.error(
                    f"Error in session cadence for {self.underlying}: {e}",
                    exc_info=True,
                )
                # Avoid rapid failure loops
                await asyncio.sleep(self.cadence_sec)

    async def _compute_and_publish(self):
        """
        Performs a single cycle of computation and publishing with the new
        row-based payload structure and strict computation rules.
        The main computation logic is offloaded to a separate thread to avoid
        blocking the asyncio event loop.
        """
        # The actual computation is now done in a separate thread
        per_expiry_data, new_desired_tokens, spot_ltp, token_ranks = await asyncio.to_thread(
            self._run_computation
        )

        self.desired_tokens = new_desired_tokens
        self.token_ranks = token_ranks

        # 3. Assemble and publish snapshot
        snapshot = {
            "underlying": self.underlying,
            "spot_token": self.spot_token,
            "spot_ltp": spot_ltp,
            "cadence_sec": self.cadence_sec,
            "expiries": [e.isoformat() for e in self.expiries],
            "per_expiry": per_expiry_data,
            "desired_token_count": len(self.desired_tokens),
            "health": {
                "spot_live": bool(self.last_spot_live),
                "spot_age_sec": self.last_spot_age_sec,
                "dropped_tokens": int(
                    getattr(self.manager, "dropped_tokens", {}).get(self.underlying, 0)
                ),
            },
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.snapshot = snapshot

        # 4. Notify manager to update subscriptions and publish
        await self.manager.on_session_update(self)

    def _run_computation(self) -> tuple[Dict[str, Any], Set[int], Optional[float], Dict[int, tuple]]:
        """
        The synchronous, CPU-bound part of the computation. This method is
        executed in a separate thread pool to avoid blocking the event loop.

        Compatibility/canonical ownership note:
        - This session computation remains the current source of truth for
          synthetic-forward and Black-76-derived option Greeks/IV snapshots.
        - Canonical `/api/options/*` market routes expose these computed
          snapshots via `OptionsMarketService`; they do not re-compute Greeks
          independently.
        """
        # 1. Get spot LTP. If unavailable, we can still proceed but all
        #    expiry-level calculations will be skipped.
        spot_tick = self.manager.market_data.latest_ticks.get(self.spot_token)
        spot_ltp = (
            spot_tick.get("last_price")
            if spot_tick and "last_price" in spot_tick
            else None
        )
        self.last_spot_age_sec = None
        if spot_ltp:
            self.last_spot_ltp = spot_ltp
            self.last_spot_live = True
            stamp = spot_tick.get("exchange_timestamp") if spot_tick else None
            if isinstance(stamp, datetime):
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                self.last_spot_age_sec = (datetime.now(timezone.utc) - stamp).total_seconds()
        else:
            # Keep computing from the last good value, but say so in health:
            # a reused spot is not a live spot.
            spot_ltp = self.last_spot_ltp
            self.last_spot_live = False
            logger.warning(
                f"No live spot LTP for {self.underlying}; using last known value: {spot_ltp}"
            )

        # 2. Iterate through expiries
        per_expiry_data = {}
        new_desired_tokens = {self.spot_token} if self.spot_token else set()
        token_ranks: Dict[int, tuple] = {int(self.spot_token): (0, 0, 0)} if self.spot_token else {}

        if OPTIONS_SESSIONS_USE_VECTORIZED:
            # --- Vectorized Path ---
            for expiry_index, expiry in enumerate(self.expiries):
                expiry_str = expiry.isoformat()
                strikes = self.strikes_by_expiry.get(expiry, [])
                if not strikes or not spot_ltp:
                    per_expiry_data[expiry_str] = {"forward": None, "sigma_expiry": None, "atm_strike": None, "strikes": [], "rows": []}
                    continue

                atm_strike = self.manager.instrument_repo.nearest_strike(strikes, spot_ltp)
                if not atm_strike:
                    continue

                T = self._time_to_expiry(expiry)
                # NOTE: Synthetic forward from multi-strike put-call parity
                # remains the session-level source feeding canonical market
                # snapshots and worker options views.
                forward, ce_atm_ltp, pe_atm_ltp = self._compute_forward(expiry, atm_strike, spot_ltp, strikes=strikes)
                sigma_expiry = self._compute_sigma(expiry, atm_strike, forward, T, ce_atm_ltp, pe_atm_ltp)

                window_strikes = build_bounded_strike_window(
                    strikes=strikes,
                    atm_strike=atm_strike,
                    window=self._expiry_window(
                        expiry_index=expiry_index,
                        strikes=strikes,
                        center=float(forward or spot_ltp),
                        sigma=sigma_expiry,
                        T=T,
                    ),
                )
                
                strikes_key = tuple(sorted(window_strikes))
                cache_key = f"instruments:{self.underlying}:{expiry.isoformat()}:{hash(strikes_key)}"
                option_instruments = self._get_cached_instruments(
                    cache_key,
                    lambda: self.manager.instrument_repo.get_option_instruments_for_strikes(self.underlying, expiry, window_strikes)
                )

                inst_by_strike = {s: {} for s in window_strikes}
                ordered_window = sorted(window_strikes)
                atm_index = min(
                    range(len(ordered_window)),
                    key=lambda idx: abs(ordered_window[idx] - float(atm_strike)),
                ) if ordered_window else 0
                distance = {strike: abs(idx - atm_index) for idx, strike in enumerate(ordered_window)}
                for inst in option_instruments:
                    inst_by_strike[inst["strike"]][inst["option_type"]] = inst
                    token = int(inst["instrument_token"])
                    new_desired_tokens.add(token)
                    token_ranks[token] = (1, distance.get(inst["strike"], FAR_WINDOW_MAX), expiry_index)

                sorted_strikes = sorted(window_strikes)

                # Per-strike smile: solve each strike's own IV from its OTM
                # option price (put below forward, call above forward, ATM
                # averaged across both sides), then compute that strike's
                # Greeks with its own IV. Strikes whose solve fails (missing
                # price, no convergence, or intrinsic violation) fall back to
                # the expiry-level ATM IV and are flagged accordingly.
                per_strike_sigma, iv_source_by_strike = self._solve_per_strike_iv(
                    sorted_strikes, atm_strike, forward, T, sigma_expiry, inst_by_strike
                )

                greeks_by_contract: Dict[tuple[float, str], Dict[str, float]] = {}
                greeks_contracts = [
                    (strike, option_type, float(per_strike_sigma[i]))
                    for i, strike in enumerate(sorted_strikes)
                    if forward
                    and T > MIN_T
                    and per_strike_sigma[i] is not None
                    and not np.isnan(per_strike_sigma[i])
                    for option_type in ("CE", "PE")
                    if inst_by_strike.get(strike, {}).get(option_type)
                ]
                if greeks_contracts:
                    try:
                        delta, gamma, theta, vega = black76_greeks_arrays(
                            np.array(
                                [option_type == "CE" for _, option_type, _ in greeks_contracts],
                                dtype=np.bool_,
                            ),
                            float(forward),
                            np.array([strike for strike, _, _ in greeks_contracts], dtype=np.float64),
                            T,
                            np.array([sigma for _, _, sigma in greeks_contracts], dtype=np.float64),
                        )
                        for index, (strike, option_type, _) in enumerate(greeks_contracts):
                            greeks_by_contract[(strike, option_type)] = {
                                "delta": float(delta[index]),
                                "gamma": float(gamma[index]),
                                "theta": float(theta[index]) / 365.0,
                                "vega": float(vega[index]) / 100.0,
                                "rho": 0.0,
                            }
                    except Exception as e:
                        logger.error(
                            f"[{self.underlying}] Expiry Greeks computation failed for "
                            f"{expiry_str}: {e}",
                            exc_info=True,
                        )

                rows = []
                for i, strike in enumerate(sorted_strikes):
                    row = {"strike": strike, "CE": None, "PE": None}
                    strike_sigma = per_strike_sigma[i]
                    iv_source = iv_source_by_strike[i]
                    for option_type in ("CE", "PE"):
                        inst = inst_by_strike.get(strike, {}).get(option_type)
                        if not inst:
                            continue

                        tick = self.manager.market_data.latest_ticks.get(inst["instrument_token"])
                        ltp = tick.get("last_price") if tick else None

                        greeks = greeks_by_contract.get((strike, option_type), {})

                        exchange_ts = tick.get("exchange_timestamp") if tick else None
                        stale_age_sec = None
                        if exchange_ts:
                            if exchange_ts.tzinfo is None:
                                exchange_ts = exchange_ts.replace(tzinfo=timezone.utc)
                            stale_age_sec = (datetime.now(timezone.utc) - exchange_ts).total_seconds()

                        row[option_type] = {
                            "token": inst["instrument_token"],
                            "tsym": inst["tradingsymbol"],
                            "lot_size": inst.get("lot_size"),
                            "ltp": ltp,
                            "iv": strike_sigma if strike_sigma is not None and not np.isnan(strike_sigma) else None,
                            "iv_source": iv_source,
                            "oi": tick.get("oi") if tick else None,
                            "delta": greeks.get("delta"),
                            "gamma": greeks.get("gamma"),
                            "theta": greeks.get("theta"),
                            "vega": greeks.get("vega"),
                            "rho": greeks.get("rho"),
                            "updated_at": exchange_ts.isoformat() if exchange_ts else None,
                            "stale_age_sec": stale_age_sec,
                        }
                    rows.append(row)

                pcr = compute_put_call_ratio(rows)
                max_pain = self._cached_max_pain(expiry_str, rows)

                per_expiry_data[expiry_str] = {
                    "forward": forward,
                    "sigma_expiry": sigma_expiry,
                    "atm_strike": atm_strike,
                    "strikes": window_strikes,
                    "rows": rows,
                    "pcr": pcr,
                    "max_pain": max_pain,
                }
        else:
            # --- Legacy Path ---
            for expiry in self.expiries:
                expiry_str = expiry.isoformat()
                strikes = self.strikes_by_expiry.get(expiry, [])
                if not strikes or not spot_ltp:
                    per_expiry_data[expiry_str] = {
                        "forward": None,
                        "sigma_expiry": None,
                        "atm_strike": None,
                        "strikes": [],
                        "rows": [],
                    }
                    continue

                # Determine ATM strike
                atm_strike = self.manager.instrument_repo.nearest_strike(strikes, spot_ltp)
                if not atm_strike:
                    continue

                # Compute time to expiry
                T = self._time_to_expiry(expiry)

                # Strictly compute synthetic forward and sigma.
                # This remains the canonical session math source consumed by
                # canonical options API snapshots.
                forward, ce_atm_ltp, pe_atm_ltp = self._compute_forward(
                    expiry, atm_strike, spot_ltp
                )
                sigma_expiry = self._compute_sigma(
                    expiry, atm_strike, forward, T, ce_atm_ltp, pe_atm_ltp
                )

                # Build window of strikes and fetch instruments
                window_strikes = build_bounded_strike_window(
                    strikes=strikes,
                    atm_strike=atm_strike,
                    window=self.window_size,
                )
                
                # Use a cache key for the instruments of the current expiry window
                strikes_key = tuple(sorted(window_strikes))
                cache_key = f"instruments:{self.underlying}:{expiry.isoformat()}:{hash(strikes_key)}"
                option_instruments = self._get_cached_instruments(
                    cache_key,
                    lambda: self.manager.instrument_repo.get_option_instruments_for_strikes(
                        self.underlying, expiry, window_strikes
                    )
                )

                # Group instruments by strike for row creation
                inst_by_strike = {}
                for inst in option_instruments:
                    strike = inst["strike"]
                    if strike not in inst_by_strike:
                        inst_by_strike[strike] = {}
                    inst_by_strike[strike][inst["option_type"]] = inst
                    new_desired_tokens.add(inst["instrument_token"])

                # Build rows
                rows = []
                for strike in sorted(window_strikes):
                    ce_inst = inst_by_strike.get(strike, {}).get("CE")
                    pe_inst = inst_by_strike.get(strike, {}).get("PE")

                    row = {"strike": strike, "CE": None, "PE": None}

                    for inst, option_type in [(ce_inst, "CE"), (pe_inst, "PE")]:
                        if not inst:
                            continue

                        tick = self.manager.market_data.latest_ticks.get(
                            inst["instrument_token"]
                        )
                        ltp = tick.get("last_price") if tick else None

                        greeks = {}
                        iv = None
                        if forward and T > MIN_T and sigma_expiry:
                            # Greeks are reported from Black-76 using the
                            # synthetic forward-derived expiry sigma.
                            iv = sigma_expiry
                            greeks_unit = black76_greeks(
                                option_type, forward, strike, T, sigma_expiry
                            )
                            # Align Greeks with Mibian conventions for reporting
                            # Vega: reported per 1% volatility change (hence / 100)
                            # Theta: reported per calendar day (hence / 365)
                            greeks = {
                                "delta": greeks_unit.get("delta"),
                                "gamma": greeks_unit.get("gamma"),
                                "theta": greeks_unit.get("theta", 0.0) / 365.0
                                if greeks_unit.get("theta") is not None
                                else None,
                                "vega": greeks_unit.get("vega", 0.0) / 100.0
                                if greeks_unit.get("vega") is not None
                                else None,
                                "rho": greeks_unit.get("rho"),
                            }

                        exchange_ts = tick.get("exchange_timestamp") if tick else None
                        stale_age_sec = None
                        if exchange_ts:
                            if exchange_ts.tzinfo is None:
                                exchange_ts = exchange_ts.replace(tzinfo=timezone.utc)
                            stale_age_sec = (datetime.now(timezone.utc) - exchange_ts).total_seconds()

                        row[option_type] = {
                            "token": inst["instrument_token"],
                            "tsym": inst["tradingsymbol"],
                            "lot_size": inst.get("lot_size"),
                            "ltp": ltp,
                            "iv": iv,
                            "iv_source": "expiry_fallback",
                            "oi": tick.get("oi") if tick else None,
                            "delta": greeks.get("delta"),
                            "gamma": greeks.get("gamma"),
                            "theta": greeks.get("theta"),
                            "vega": greeks.get("vega"),
                            "rho": greeks.get("rho"),
                            "updated_at": exchange_ts.isoformat() if exchange_ts else None,
                            "stale_age_sec": stale_age_sec,
                        }
                    rows.append(row)

                pcr = compute_put_call_ratio(rows)
                max_pain = self._cached_max_pain(expiry_str, rows)

                per_expiry_data[expiry_str] = {
                    "forward": forward,
                    "sigma_expiry": sigma_expiry,
                    "atm_strike": atm_strike,
                    "strikes": window_strikes,
                    "rows": rows,
                    "pcr": pcr,
                    "max_pain": max_pain,
                }

        if not OPTIONS_SESSIONS_USE_VECTORIZED:
            token_ranks = {int(t): (1, 0, 0) for t in new_desired_tokens}

        return per_expiry_data, new_desired_tokens, spot_ltp, token_ranks

    def _cached_max_pain(
        self, expiry_key: str, rows: Sequence[Mapping[str, Any]]
    ) -> Optional[float]:
        now = time.monotonic()
        refresh_seconds = max(
            0.0, float(os.getenv("OPTIONS_MAX_PAIN_REFRESH_S", "30"))
        )
        cached = self._max_pain_cache.get(expiry_key)
        if cached is not None and now - cached[0] < refresh_seconds:
            return cached[1]
        value = compute_bounded_max_pain(rows)
        self._max_pain_cache[expiry_key] = (now, value)
        return value

    def _expiry_window(
        self,
        *,
        expiry_index: int,
        strikes: Sequence[float],
        center: float,
        sigma: Optional[float],
        T: float,
    ) -> int:
        """Strikes each side of ATM to track for one expiry.

        The nearest expiry keeps ``window_size``. Later expiries widen until the
        window reaches roughly 10-delta on both sides (a monthly short picked by
        delta must be inside the tracked window), capped at ``FAR_WINDOW_MAX``.
        """
        base = int(self.window_size)
        if expiry_index == 0 or not sigma or sigma <= 0 or T <= MIN_T or not center:
            return base
        ordered = sorted({float(s) for s in strikes})
        steps = [b - a for a, b in zip(ordered, ordered[1:]) if b > a]
        if not steps:
            return base
        step = float(np.median(steps))
        half_range = float(center) * (math.exp(FAR_WINDOW_DELTA_Z * float(sigma) * math.sqrt(T)) - 1.0)
        needed = int(math.ceil(half_range / step))
        return max(base, min(needed, FAR_WINDOW_MAX))

    def _solve_per_strike_iv(
        self,
        sorted_strikes: List[float],
        atm_strike: float,
        forward: Optional[float],
        T: float,
        sigma_expiry: Optional[float],
        inst_by_strike: Dict[float, Dict[str, Any]],
    ) -> tuple[List[Optional[float]], List[str]]:
        """
        Solves a per-strike implied volatility "smile" instead of reusing a
        single expiry-level sigma for every strike.

        For each strike, the OTM side is used to back out the IV: puts below
        the synthetic forward, calls above it; the ATM strike averages both
        sides when available. A strike whose solve fails (no OTM price, no
        convergence, or an intrinsic-value violation) falls back to the
        expiry-level ATM IV (``sigma_expiry``) and is flagged with
        ``iv_source: "expiry_fallback"``; a successful per-strike solve is
        flagged ``iv_source: "per_strike"``.
        """
        n = len(sorted_strikes)
        fallback_sigma = sigma_expiry if sigma_expiry is not None else None
        per_strike_sigma: List[Optional[float]] = [fallback_sigma] * n
        iv_source: List[str] = ["expiry_fallback"] * n

        if not forward or T <= MIN_T or n == 0:
            return per_strike_sigma, iv_source

        k_array = np.array(sorted_strikes, dtype=np.float64)
        ce_ltp_arr = np.full(n, np.nan)
        pe_ltp_arr = np.full(n, np.nan)

        for i, strike in enumerate(sorted_strikes):
            ce_inst = inst_by_strike.get(strike, {}).get("CE")
            if ce_inst:
                tick = self.manager.market_data.latest_ticks.get(ce_inst["instrument_token"])
                ltp = tick.get("last_price") if tick else None
                if ltp is not None:
                    ce_ltp_arr[i] = ltp
            pe_inst = inst_by_strike.get(strike, {}).get("PE")
            if pe_inst:
                tick = self.manager.market_data.latest_ticks.get(pe_inst["instrument_token"])
                ltp = tick.get("last_price") if tick else None
                if ltp is not None:
                    pe_ltp_arr[i] = ltp

        ce_iv_arr = np.full(n, np.nan)
        pe_iv_arr = np.full(n, np.nan)

        try:
            ce_mask = ~np.isnan(ce_ltp_arr)
            if ce_mask.any():
                solved = implied_vol_from_price_black76(
                    "CE", forward, k_array[ce_mask], T, ce_ltp_arr[ce_mask]
                )
                ce_iv_arr[ce_mask] = solved
            pe_mask = ~np.isnan(pe_ltp_arr)
            if pe_mask.any():
                solved = implied_vol_from_price_black76(
                    "PE", forward, k_array[pe_mask], T, pe_ltp_arr[pe_mask]
                )
                pe_iv_arr[pe_mask] = solved
        except Exception as e:
            logger.error(f"[{self.underlying}] Per-strike IV solve failed: {e}", exc_info=True)
            return per_strike_sigma, iv_source

        for i, strike in enumerate(sorted_strikes):
            if strike == atm_strike:
                candidates = [v for v in (ce_iv_arr[i], pe_iv_arr[i]) if not np.isnan(v)]
                if candidates:
                    per_strike_sigma[i] = float(sum(candidates) / len(candidates))
                    iv_source[i] = "per_strike"
            elif strike < forward:
                # Put wing is OTM below the forward.
                if not np.isnan(pe_iv_arr[i]):
                    per_strike_sigma[i] = float(pe_iv_arr[i])
                    iv_source[i] = "per_strike"
            else:
                # Call wing is OTM above the forward.
                if not np.isnan(ce_iv_arr[i]):
                    per_strike_sigma[i] = float(ce_iv_arr[i])
                    iv_source[i] = "per_strike"

        return per_strike_sigma, iv_source

    def _compute_forward(
        self,
        expiry: date,
        atm_strike: float,
        spot_ltp: float,
        strikes: Optional[Sequence[float]] = None,
    ) -> tuple[Optional[float], Optional[float], Optional[float]]:
        """Synthetic forward from put-call parity: F = K + C_K - P_K (r = 0).

        Median over the (up to) three strikes nearest spot that have a positive
        CE and PE price, so one stale or zero quote cannot move the forward.
        This module's Black-76 is undiscounted, so parity is used undiscounted
        too (r = 0). Returns (forward, ATM CE ltp, ATM PE ltp); forward is None
        when no strike has both prices.
        """
        repo = self.manager.instrument_repo
        ticks = self.manager.market_data.latest_ticks
        candidates = sorted(
            {float(s) for s in (strikes or [atm_strike])},
            key=lambda s: (abs(s - float(spot_ltp or atm_strike)), s),
        )[:3]
        if float(atm_strike) not in candidates:
            candidates.append(float(atm_strike))
        cache_key = (self.underlying, expiry, tuple(candidates))
        instruments = self._get_cached_instruments(
            cache_key,
            lambda: repo.get_option_instruments_for_strikes(
                self.underlying, expiry, candidates
            ),
        )
        prices: Dict[float, Dict[str, Optional[float]]] = {}
        for inst in instruments:
            tick = ticks.get(inst["instrument_token"])
            price = tick.get("last_price") if tick else None
            prices.setdefault(float(inst["strike"]), {})[inst["option_type"]] = price

        atm = prices.get(float(atm_strike), {})
        ce_ltp, pe_ltp = atm.get("CE"), atm.get("PE")
        estimates = []
        for strike in candidates[:3]:
            call, put = prices.get(strike, {}).get("CE"), prices.get(strike, {}).get("PE")
            if call and put and call > 0 and put > 0:
                estimates.append(strike + float(call) - float(put))
        if not estimates:
            return None, ce_ltp, pe_ltp
        return float(np.median(estimates)), ce_ltp, pe_ltp

    def _time_to_expiry(self, expiry: date) -> float:
        """
        Calculates the time to expiry in year fractions.
        """
        now = datetime.now(timezone.utc)
        expiry_dt = datetime(
            expiry.year,
            expiry.month,
            expiry.day,
            EXPIRY_CLOSE_HOUR_IST,
            EXPIRY_CLOSE_MINUTE_IST,
            tzinfo=IST,
        )
        time_left = (expiry_dt - now).total_seconds()
        if time_left <= 0:
            return MIN_T
        return max(MIN_T, time_left / (YEAR_IN_DAYS * 24 * 60 * 60))

    def _compute_sigma(
        self,
        expiry: date,
        atm_strike: float,
        forward: float,
        T: float,
        ce_ltp: Optional[float],
        pe_ltp: Optional[float],
    ) -> Optional[float]:
        """
        Computes the implied volatility for the ATM strike. Returns None if inputs
        are missing or the solver fails, with no fallback.

        Sigma is solved from synthetic-forward context and then reused by
        Black-76 Greeks generation in this session cycle.
        """
        if (
            forward is None
            or ce_ltp is None
            or pe_ltp is None
            or T <= 0
            or atm_strike <= 0
        ):
            return None

        # Per hotfix: Invert IV from the ATM Call LTP to align with Mibian's
        # behavior of BS([F, K, 0, days], callPrice=CE_atm).
        price = ce_ltp
        sigma = implied_vol_from_price_black76(
            option_type="CE", F=forward, K=atm_strike, T=T, price=price
        )
        return sigma


class OptionsSessionManager:
    """
    A singleton-style manager for all active options sessions.
    """

    def __init__(
        self, market_data: MarketDataRuntime, instrument_repo: InstrumentsRepository
    ):
        self.market_data = market_data
        self.instrument_repo = instrument_repo
        self.sessions: Dict[str, OptionsSession] = {}
        self.client_queues: Dict[str, List[asyncio.Queue]] = {}
        self.owner_id = "backend:options-sessions"
        self.dropped_tokens: Dict[str, int] = {}
        self._token_sessions: Dict[int, set[str]] = {}
        self._last_publish_digest: Dict[str, tuple] = {}
        self._last_redis_set_monotonic: Dict[str, float] = {}
        self.always_on: set[str] = set()
        self.cadence_sec = 5
        self.tick_driven = True
        self.min_interval_sec = max(
            0.25, float(os.getenv("OPTIONS_CHAIN_MIN_INTERVAL_S", "1.0"))
        )
        self.idle_stop_minutes = 15
        self.last_used: Dict[str, float] = {}
        self._ensure_tasks: Dict[str, asyncio.Task] = {}
        self._reaper_task: Optional[asyncio.Task] = None
        self._unsubscribe_tick_listener: Optional[Callable[[], None]] = None
        if hasattr(self.market_data, "add_tick_listener"):
            self._unsubscribe_tick_listener = self.market_data.add_tick_listener(self._on_tick)
        self._ensure_reaper_started()

    def close(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            self._reaper_task = None
        for task in self._ensure_tasks.values():
            task.cancel()
        self._ensure_tasks.clear()
        if self._unsubscribe_tick_listener:
            self._unsubscribe_tick_listener()
            self._unsubscribe_tick_listener = None

    def _ensure_reaper_started(self) -> None:
        if self._reaper_task is not None and not self._reaper_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._reaper_task = loop.create_task(self._reaper_loop())

    async def _reaper_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(60)
                await self._reap_idle_sessions()
        except asyncio.CancelledError:
            return

    async def _reap_idle_sessions(
        self, *, now_monotonic: Optional[float] = None
    ) -> None:
        """Stop idle on-demand sessions, and all on-demand sessions after close."""
        from backend.strategies.market_session import session_state

        now_value = time.monotonic() if now_monotonic is None else now_monotonic
        market = session_state("NFO")
        local_time = datetime.now(IST).time().replace(tzinfo=None)
        after_market_close = (
            market.get("reason") == "after_close"
            and local_time.hour * 60 + local_time.minute >= 15 * 60 + 35
        )
        for underlying in list(self.sessions):
            if underlying in self.always_on:
                continue
            last_used = self.last_used.get(underlying, now_value)
            idle = (
                self.idle_stop_minutes > 0
                and now_value - last_used > self.idle_stop_minutes * 60
            )
            if after_market_close or idle:
                await self.stop_session(underlying)

    def _on_tick(self, token: int, tick: Dict[str, Any]) -> None:
        for underlying in tuple(self._token_sessions.get(int(token), ())):
            session = self.sessions.get(underlying)
            if session is not None:
                # A stopped session's tokens can linger until the next update.
                session.mark_dirty()

    async def start_sessions(
        self, items: List[Dict[str, Any]], replace: bool = False
    ):
        """
        Starts or updates sessions from a list of requests.
        """
        normalized_underlyings = {
            self.instrument_repo.normalize_underlying_symbol(item["underlying"])[0]
            for item in items
        }

        if replace:
            # Stop sessions not in the new list
            to_stop = set(self.sessions.keys()) - normalized_underlyings
            for underlying in to_stop:
                await self.stop_session(underlying)

        # Start or update sessions
        for item in items:
            underlying, _ = self.instrument_repo.normalize_underlying_symbol(
                item["underlying"]
            )
            await self.start_session(
                underlying,
                item.get("window", 12),
                item.get("cadence_sec", 5),
            )
        await self._converge_subscriptions()

    async def start_session(
        self, underlying: str, window_size: int = 12, cadence_sec: int = 5
    ):
        """
        Starts a session for a single underlying, or updates it if it exists.
        """
        if underlying in self.sessions:
            session = self.sessions[underlying]
            await session.update_config(window_size, cadence_sec)
            return

        session = OptionsSession(underlying, self, window_size, cadence_sec)
        self.sessions[underlying] = session
        self.last_used.setdefault(underlying, time.monotonic())
        self._ensure_reaper_started()
        await session.start()

    async def apply_settings(self, settings: Any) -> Dict[str, bool]:
        """Apply persisted settings to running sessions without a restart."""
        self.always_on = set(settings.always_on)
        self.cadence_sec = int(settings.cadence_sec)
        self.tick_driven = bool(settings.tick_driven)
        self.min_interval_sec = float(settings.min_interval_sec)
        self.idle_stop_minutes = int(settings.idle_stop_minutes)

        for session in list(self.sessions.values()):
            session.tick_driven = self.tick_driven
            session.min_interval_sec = self.min_interval_sec
            await session.update_config(session.window_size, self.cadence_sec)

        results: Dict[str, bool] = {}
        for underlying in settings.always_on:
            results[underlying] = await self.ensure_session(
                underlying, cadence_sec=self.cadence_sec
            )
        return results

    async def ensure_session(
        self,
        underlying: str,
        window_size: int = 12,
        cadence_sec: Optional[int] = None,
    ) -> bool:
        """Start ``underlying``'s session if it is missing; idempotent and bounded.

        Only a *known* underlying (one named in ``OPTIONS_AUTOSTART_UNDERLYINGS``)
        may be started this way, so a read or admission path cannot open an
        arbitrary session. Returns ``True`` when a session exists afterwards. A
        start failure is logged and reported as ``False`` rather than raised, so
        one bad underlying never blocks a caller or boot.
        """
        normalized, _ = self.instrument_repo.normalize_underlying_symbol(underlying)
        normalized = str(normalized or "").strip().upper()
        if not normalized or normalized not in AVAILABLE_OPTION_UNDERLYINGS:
            return False
        if normalized in self.sessions:
            return True
        try:
            await self.start_session(
                normalized,
                window_size,
                self.cadence_sec if cadence_sec is None else cadence_sec,
            )
            await self._converge_subscriptions()
        except Exception as exc:  # noqa: BLE001 - a failed start is a False, not a crash
            self.sessions.pop(normalized, None)
            logger.warning(
                "Unable to start option session for %s: %s", normalized, exc, exc_info=True
            )
            return False
        return True

    async def stop_session(self, underlying: str):
        """
        Stops a session for a single underlying.
        """
        session = self.sessions.pop(underlying, None)
        if session:
            await session.stop()
            await self._converge_subscriptions()

    def get_snapshot(self, underlying: str) -> Optional[Dict[str, Any]]:
        """
        Returns the latest snapshot for an underlying.
        """
        normalized, _ = self.instrument_repo.normalize_underlying_symbol(underlying)
        normalized = str(normalized or "").strip().upper()
        self.touch(normalized)
        session = self.sessions.get(normalized)
        if session is None:
            self._schedule_ensure(normalized)
        return session.snapshot if session else None

    def touch(self, underlying: str) -> None:
        normalized = str(underlying or "").strip().upper()
        if normalized:
            self.last_used[normalized] = time.monotonic()

    def _schedule_ensure(self, underlying: str) -> None:
        if underlying not in AVAILABLE_OPTION_UNDERLYINGS:
            return
        existing = self._ensure_tasks.get(underlying)
        if existing is not None and not existing.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self.ensure_session(underlying))
        self._ensure_tasks[underlying] = task

        def _finished(done: asyncio.Task, symbol: str = underlying) -> None:
            if self._ensure_tasks.get(symbol) is done:
                self._ensure_tasks.pop(symbol, None)

        task.add_done_callback(_finished)

    def get_watchlist(self) -> List[Dict[str, Any]]:
        """
        Returns a list of active underlyings and their status.
        """
        return [
            {
                "underlying": s.underlying,
                "is_running": s.is_running,
                "desired_tokens": len(s.desired_tokens),
            }
            for s in self.sessions.values()
        ]

    def get_session_status(self) -> List[Dict[str, Any]]:
        """Current session state for the owner settings response."""
        now_monotonic = time.monotonic()
        now_utc = datetime.now(timezone.utc)
        result: List[Dict[str, Any]] = []
        for underlying, session in sorted(self.sessions.items()):
            last_used = self.last_used.get(underlying)
            updated_age = None
            updated_at = (getattr(session, "snapshot", {}) or {}).get("updated_at")
            if updated_at:
                try:
                    stamp = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    updated_age = max(0.0, (now_utc - stamp).total_seconds())
                except (TypeError, ValueError):
                    updated_age = None
            result.append(
                {
                    "underlying": underlying,
                    "running": bool(session.is_running),
                    "always_on": underlying in self.always_on,
                    "last_used_age_s": (
                        max(0.0, now_monotonic - last_used)
                        if last_used is not None
                        else None
                    ),
                    "updated_age_s": updated_age,
                    "desired_tokens": len(session.desired_tokens),
                    "cadence_sec": int(session.cadence_sec),
                }
            )
        return result

    async def on_session_update(self, session: OptionsSession):
        """
        Callback from a session when it has a new snapshot.
        """
        # Publish to Redis (best-effort)
        try:
            redis_client = get_redis()
            v1_snapshot_key = option_snapshot_v1_key(session.underlying)
            v1_pub_channel = option_snapshot_v1_updates_channel(session.underlying)
            digest = _snapshot_market_digest(session.snapshot)
            previous_digest = self._last_publish_digest.get(session.underlying)
            changed = previous_digest != digest
            now = time.monotonic()
            last_set = self._last_redis_set_monotonic.get(session.underlying, 0.0)
            if changed or now - last_set >= 5.0:
                v1_payload_json = serialize_option_snapshot_v1(
                    session.snapshot, session.underlying
                )
                await redis_client.set(
                    v1_snapshot_key,
                    v1_payload_json,
                    ex=OPTION_SNAPSHOT_TTL_SECONDS,
                )
                self._last_redis_set_monotonic[session.underlying] = now
                if changed:
                    await redis_client.publish(v1_pub_channel, v1_payload_json)
                    self._last_publish_digest[session.underlying] = digest
        except Exception as e:
            logger.warning(f"Redis operation failed: {e}")

        # Fan-out to in-process WebSocket clients
        if session.underlying in self.client_queues:
            for queue in self.client_queues[session.underlying]:
                await queue.put(session.snapshot)

        for sessions in self._token_sessions.values():
            sessions.discard(session.underlying)
        self._token_sessions = {
            token: sessions
            for token, sessions in self._token_sessions.items()
            if sessions
        }
        for token in getattr(session, "desired_tokens", set()):
            self._token_sessions.setdefault(int(token), set()).add(session.underlying)

        # Converge subscriptions
        await self._converge_subscriptions()

    async def _converge_subscriptions(self):
        """Subscribe the union of desired tokens, truncating by rank at TOKEN_CAP."""
        ranks: Dict[int, tuple] = {}
        owner_of: Dict[int, str] = {}
        for underlying, session in self.sessions.items():
            session_ranks = dict(getattr(session, "token_ranks", {}) or {})
            for token in session.desired_tokens:
                rank = session_ranks.get(int(token), (1, FAR_WINDOW_MAX, 99))
                if int(token) not in ranks or rank < ranks[int(token)]:
                    ranks[int(token)] = rank
                    owner_of[int(token)] = underlying
        kept, dropped = rank_tokens(ranks, TOKEN_CAP)
        self.dropped_tokens = {underlying: 0 for underlying in self.sessions}
        for token in dropped:
            self.dropped_tokens[owner_of[token]] = self.dropped_tokens.get(owner_of[token], 0) + 1
        if dropped:
            logger.warning(
                "options token cap reached: kept %s, dropped %s (far wings first)",
                len(kept),
                len(dropped),
            )
        if kept:
            await self.market_data.set_owner_subscriptions(
                self.owner_id,
                {int(token): "full" for token in kept},
            )
        else:
            await self.market_data.delete_owner(self.owner_id)

    async def register_client(self, underlying: str) -> asyncio.Queue:
        """
        Registers a client queue for a given underlying.
        """
        queue = asyncio.Queue()
        if underlying not in self.client_queues:
            self.client_queues[underlying] = []
        self.client_queues[underlying].append(queue)
        return queue

    def deregister_client(self, underlying: str, queue: asyncio.Queue):
        """
        Deregisters a client queue.
        """
        if underlying in self.client_queues:
            self.client_queues[underlying].remove(queue)
            if not self.client_queues[underlying]:
                del self.client_queues[underlying]

    def on_ticks(self, ticks: List[Dict[str, Any]]):
        """
        Legacy no-op retained for compatibility with older call sites.
        """
        pass
