import asyncio
import logging
from datetime import date, datetime, timezone
from math import floor
from typing import Any, Dict, List, Optional, Set

from broker_api.instruments_repository import InstrumentsRepository
from broker_api.options_greeks import (
    black76_greeks,
    implied_vol_from_price_black76,
)
from broker_api.redis_events import get_redis, publish_event
from broker_api.websocket_manager import WebSocketManager


# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants
TOKEN_CAP = 2500
YEAR_IN_DAYS = 365.0
MIN_T = 1e-6  # Min time to expiry to avoid zero division


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

        # Session state
        self.spot_token: Optional[int] = None
        self.expiries: List[date] = []
        self.strikes_by_expiry: Dict[date, List[float]] = {}
        self.sigma_by_expiry: Dict[date, float] = {}
        self.desired_tokens: Set[int] = set()
        self.snapshot: Dict[str, Any] = {}
        self.last_spot_ltp: Optional[float] = None
        self.last_expiry_refresh_ts: Optional[datetime] = None

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
                    self.strikes_by_expiry[expiry] = repo.get_distinct_strikes(
                        self.underlying, expiry
                    )
        self.last_expiry_refresh_ts = datetime.now(timezone.utc)

    async def _run_cadence(self):
        """
        The main loop for computing Greeks and publishing updates.
        """
        while self.is_running:
            try:
                start_time = asyncio.get_event_loop().time()

                # Lightweight check to refresh expiries every 60 seconds
                if (
                    not self.last_expiry_refresh_ts
                    or (
                        datetime.now(timezone.utc) - self.last_expiry_refresh_ts
                    ).total_seconds()
                    >= 60
                ):
                    await self._refresh_expiries()

                await self._compute_and_publish()

                # Dynamic sleep to maintain cadence
                elapsed = asyncio.get_event_loop().time() - start_time
                sleep_duration = max(0, self.cadence_sec - elapsed)
                logger.info(f"[{self.underlying}] Computation took {elapsed:.2f}s, sleeping for {sleep_duration:.2f}s")
                await asyncio.sleep(sleep_duration)

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
        """
        # 1. Get spot LTP. If unavailable, we can still proceed but all
        #    expiry-level calculations will be skipped.
        spot_tick = self.manager.ws_manager.latest_ticks.get(self.spot_token)
        spot_ltp = (
            spot_tick.get("last_price")
            if spot_tick and "last_price" in spot_tick
            else None
        )
        if spot_ltp:
            self.last_spot_ltp = spot_ltp
        else:
            # Use last known good value if current is missing, but don't block processing
            spot_ltp = self.last_spot_ltp
            logger.warning(
                f"No live spot LTP for {self.underlying}; using last known value: {spot_ltp}"
            )

        # 2. Iterate through expiries
        per_expiry_data = {}
        new_desired_tokens = {self.spot_token} if self.spot_token else set()

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

            # Strictly compute synthetic forward and sigma
            forward, ce_atm_ltp, pe_atm_ltp = self._compute_forward(
                expiry, atm_strike, spot_ltp
            )
            sigma_expiry = self._compute_sigma(
                expiry, atm_strike, forward, T, ce_atm_ltp, pe_atm_ltp
            )

            # Build window of strikes and fetch instruments
            window_strikes = self.manager.instrument_repo.window_strikes(
                strikes, atm_strike, self.window_size
            )
            option_instruments = (
                self.manager.instrument_repo.get_option_instruments_for_strikes(
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

                    tick = self.manager.ws_manager.latest_ticks.get(
                        inst["instrument_token"]
                    )
                    ltp = tick.get("last_price") if tick else None

                    greeks = {}
                    iv = None
                    if forward and T > MIN_T and sigma_expiry:
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
                        "ltp": ltp,
                        "iv": iv,
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

            per_expiry_data[expiry_str] = {
                "forward": forward,
                "sigma_expiry": sigma_expiry,
                "atm_strike": atm_strike,
                "strikes": window_strikes,
                "rows": rows,
            }

        self.desired_tokens = new_desired_tokens

        # 3. Assemble and publish snapshot
        snapshot = {
            "underlying": self.underlying,
            "spot_token": self.spot_token,
            "spot_ltp": spot_ltp,
            "cadence_sec": self.cadence_sec,
            "expiries": [e.isoformat() for e in self.expiries],
            "per_expiry": per_expiry_data,
            "desired_token_count": len(self.desired_tokens),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.snapshot = snapshot

        # 4. Notify manager to update subscriptions and publish
        await self.manager.on_session_update(self)

    def _compute_forward(
        self, expiry: date, atm_strike: float, spot_ltp: float
    ) -> tuple[Optional[float], Optional[float], Optional[float]]:
        """
        Computes the synthetic forward price: F = Spot + Call_ATM - Put_ATM.
        Returns None for the forward if either ATM option LTP is missing.
        """
        repo = self.manager.instrument_repo
        atm_insts = repo.get_option_instruments_for_strikes(
            self.underlying, expiry, [atm_strike]
        )
        ce_inst = next((i for i in atm_insts if i["option_type"] == "CE"), None)
        pe_inst = next((i for i in atm_insts if i["option_type"] == "PE"), None)

        ce_ltp, pe_ltp = None, None
        if ce_inst:
            tick = self.manager.ws_manager.latest_ticks.get(ce_inst["instrument_token"])
            if tick:
                ce_ltp = tick.get("last_price")
        if pe_inst:
            tick = self.manager.ws_manager.latest_ticks.get(pe_inst["instrument_token"])
            if tick:
                pe_ltp = tick.get("last_price")

        if spot_ltp is not None and ce_ltp is not None and pe_ltp is not None:
            return spot_ltp + ce_ltp - pe_ltp, ce_ltp, pe_ltp
        return None, ce_ltp, pe_ltp

    def _time_to_expiry(self, expiry: date) -> float:
        """
        Calculates the time to expiry in year fractions.
        """
        now = datetime.now(timezone.utc)
        expiry_dt = datetime(
            expiry.year, expiry.month, expiry.day, 15, 30, tzinfo=timezone.utc
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
        self, ws_manager: WebSocketManager, instrument_repo: InstrumentsRepository
    ):
        self.ws_manager = ws_manager
        self.instrument_repo = instrument_repo
        self.sessions: Dict[str, OptionsSession] = {}
        self.client_queues: Dict[str, List[asyncio.Queue]] = {}

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
        await session.start()

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
        session = self.sessions.get(underlying)
        return session.snapshot if session else None

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

    async def on_session_update(self, session: OptionsSession):
        """
        Callback from a session when it has a new snapshot.
        """
        # Publish to Redis (best-effort)
        try:
            redis_client = get_redis()
            snapshot_key = f"options:snapshot:{session.underlying}"
            pub_channel = f"options:updates:{session.underlying}"
            await redis_client.set(snapshot_key, str(session.snapshot), ex=120)
            await publish_event(pub_channel, session.snapshot)
        except Exception as e:
            logger.warning(f"Redis operation failed: {e}")

        # Fan-out to in-process WebSocket clients
        if session.underlying in self.client_queues:
            for queue in self.client_queues[session.underlying]:
                await queue.put(session.snapshot)

        # Converge subscriptions
        await self._converge_subscriptions()

    async def _converge_subscriptions(self):
        """
        Computes the union of all desired tokens and updates the WebSocketManager.
        """
        global_union: Set[int] = set()
        for session in self.sessions.values():
            global_union.update(session.desired_tokens)

        # Enforce token cap
        if len(global_union) > TOKEN_CAP:
            # TODO: Implement degradation logic
            logger.warning(
                f"Token cap exceeded: {len(global_union)} > {TOKEN_CAP}. "
                "Degradation not yet implemented."
            )
            # For now, just truncate
            global_union = set(list(global_union)[:TOKEN_CAP])

        # This method needs to be added to WebSocketManager
        if hasattr(self.ws_manager, "set_desired_tokens_union"):
            await self.ws_manager.set_desired_tokens_union(global_union)
        else:
            logger.error(
                "WebSocketManager is missing 'set_desired_tokens_union' method."
            )

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
        Callback from WebSocketManager to receive ticks.
        """
        for session in self.sessions.values():
            if session.is_running:
                asyncio.run_coroutine_threadsafe(session._compute_and_publish(), self.ws_manager.main_event_loop)