"""The Nifty-500 momentum adapter: real bootstrap, calendar, coverage, plans.

Everything here runs the ACTUAL checked-in source through the ACTUAL hosted
loader (``kite_algo_worker.hosted.load_strategy_main``) with a stub client, so
the loader contract and the adapter's own decisions are exercised rather than
assumed. No network, no database, no order.

The fixture is small (20 members) but keeps every structural property the
strategy depends on: a verified calendar that runs to the END OF THE MONTH while
the history stops at the as-of session, 253-session momentum windows, a
200-session breadth window, and an index series whose trend can be flipped
independently of the breadth gate.

All fixture dates are in the fixed past, so the scenarios do not change meaning
when the suite runs on a different day.
"""

from __future__ import annotations

import json
import sys
import time
import types
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SDK_ROOT = REPO / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker.hosted import load_strategy_main  # noqa: E402

SOURCE_PATH = REPO / "examples" / "hosted_platform" / "nifty500_momentum.py"

#: A true month end (2026-08-31 is a Monday), a mid-month session, and the
#: day-31 fallback month (2026-01-31 is a Saturday, so the last session is the
#: 30th).
MONTH_END = date(2026, 8, 31)
MID_MONTH = date(2026, 8, 18)
DAY31_EARLY = date(2026, 1, 29)
DAY31_LAST = date(2026, 1, 30)


def _weekdays(start: date, end: date) -> list:
    out = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


def _sessions(end: date, count: int) -> list:
    out = []
    cursor = end
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(out)


def _month_sessions(day: date) -> list:
    start = date(day.year, day.month, 1)
    end = date(day.year, day.month, 28)
    while True:
        try:
            end = end.replace(day=end.day + 1)
        except ValueError:
            break
    return _weekdays(start, end)


def _index_series(sessions, *, falling: bool = False):
    out = []
    for position, session in enumerate(sessions):
        if falling and position >= len(sessions) - 40:
            close = 20000.0 - (position - (len(sessions) - 40)) * 60.0
        else:
            close = 18000.0 + position * 10.0
        out.append((session, close))
    return out


def _member_series(sessions, *, above: bool):
    out = []
    for position, session in enumerate(sessions):
        if above:
            close = 500.0 + position * 0.5
        else:
            if position < len(sessions) - 30:
                close = 500.0 + position * 0.5
            else:
                close = (
                    500.0
                    + (len(sessions) - 30) * 0.5
                    - (position - (len(sessions) - 30)) * 8.0
                )
        out.append((session, close))
    return out


class Fixture:
    def __init__(
        self,
        *,
        history_end: date = MONTH_END,
        members_above: int = 20,
        members_total: int = 20,
        index_falling: bool = False,
        late_member: bool = False,
        truncated_member: bool = False,
        gap_member: bool = False,
        fail_member: bool = False,
        duplicate_member: bool = False,
        nonfinite_member: bool = False,
        offcalendar_member: bool = False,
        unfinal_member: bool = False,
        unfinal_index: bool = False,
        index_missing_last: bool = False,
        index_finality_false: bool = False,
        member_finality_false: bool = False,
        unfinished_tail: bool = False,
    ) -> None:
        self.history_end = history_end
        self.history_sessions = _sessions(history_end, 320)
        # The calendar runs to the END OF THE MONTH even when the history stops
        # earlier: that is what makes a monthly schedule decidable.
        self.sessions = sorted(
            set(self.history_sessions) | set(_month_sessions(history_end))
        )
        self.index_token = 268041
        self.index_identity = {
            "instrument_token": self.index_token,
            "exchange": "NSE",
            "tradingsymbol": "NIFTY 500",
        }
        self.members = []
        self.bars = {}
        self.late_tokens = set()
        self.truncated_tokens = set()
        self.gap_tokens = set()
        self.fail_tokens = set()
        self.duplicate_tokens = set()
        self.nonfinite_tokens = set()
        self.offcalendar_tokens = set()
        for index in range(members_total):
            token = 200000 + index
            symbol = f"MEMBER{index:02d}"
            self.members.append(
                {
                    "instrument_token": token,
                    "exchange": "NSE",
                    "tradingsymbol": symbol,
                    "series": "EQ",
                }
            )
            series_sessions = self.history_sessions
            if late_member and index == members_total - 1:
                series_sessions = self.history_sessions[-100:]
                self.late_tokens.add(token)
            if truncated_member and index == members_total - 2:
                self.truncated_tokens.add(token)
            self.bars[token] = _member_series(
                series_sessions, above=index < members_above
            )
        self.index_bars = _index_series(self.history_sessions, falling=index_falling)
        if gap_member:
            self.gap_tokens.add(200001)
        if fail_member:
            self.fail_tokens.add(200000)
        if duplicate_member:
            self.duplicate_tokens.add(200002)
        if nonfinite_member:
            self.nonfinite_tokens.add(200003)
        if offcalendar_member:
            self.offcalendar_tokens.add(200004)
        self.unfinal_member = 200005 if unfinal_member else None
        self.unfinal_index = unfinal_index
        self.index_finality_false = index_finality_false
        self.member_finality_false = 200006 if member_finality_false else None
        # Sessions the calendar knows about but has NOT reported a verified close
        # for: the platform's shape for a session that has not finished yet (or
        # whose close was never verified). They stay in the verified session list,
        # so the monthly schedule is still decidable, while the "newest session
        # that is provably finished" stays at ``history_end``.
        self.unclosed_sessions = {
            session for session in self.sessions if session > self.history_end
        }
        self.tail_session: date | None = None
        if unfinal_index:
            self.unclosed_sessions.add(self.history_end)
        if unfinished_tail:
            # One session AFTER the fixture's own as-of, present in the calendar
            # (no close reported) AND in every series, verdict ``False`` - i.e. the
            # real "today is still open" tail the adapter must tolerate.
            tail = self.history_end + timedelta(days=1)
            while tail.weekday() >= 5:
                tail += timedelta(days=1)
            self.tail_session = tail
            self.sessions = sorted(set(self.sessions) | {tail})
            self.unclosed_sessions.add(tail)
            self.index_bars = self.index_bars + [(tail, self.index_bars[-1][1])]
            for token, series in self.bars.items():
                self.bars[token] = series + [(tail, series[-1][1])]
        if index_missing_last:
            # The provider's index history stops one session short: the newest
            # session the calendar proves finished has no bar.
            self.index_bars = self.index_bars[:-1]

    def _rows(self, rows, from_date: str, to_date: str):
        return [
            {
                "ts": f"{session.isoformat()}T00:00:00+05:30",
                "close": close,
                "is_complete": True,
            }
            for session, close in rows
            if from_date <= session.isoformat() <= to_date
        ]

    def history(self, token: int, from_date: str, to_date: str):
        truncated_window = from_date >= (
            self.history_end - timedelta(days=420)
        ).isoformat()

        if token == self.index_token:
            rows = self.index_bars
        else:
            rows = self.bars.get(token) or []
            if token in self.truncated_tokens and truncated_window:
                # A provider that silently clips the range: the wide probe below
                # returns data this read withheld.
                rows = rows[-100:]
            if token in self.gap_tokens and len(rows) > 40:
                rows = rows[:-20] + rows[-10:]

        candles = self._rows(rows, from_date, to_date)
        if token in self.duplicate_tokens and candles:
            candles.append(dict(candles[-1]))
        if token in self.nonfinite_tokens and candles:
            candles[-1]["close"] = float("nan")
        if token in self.offcalendar_tokens and candles:
            # A Saturday bar the verified calendar does not contain.
            candles.append(
                {
                    "ts": f"{(self.history_end - timedelta(days=1)).isoformat()}T00:00:00+05:30",
                    "close": 123.45,
                    "is_complete": True,
                }
            )

        payload = {
            "timeframe": "day",
            "candles": candles,
            "complete": True,
        }
        if token == self.index_token and self.unfinal_index:
            payload["last_candle_final"] = False
        elif self.unfinal_member is not None and token == self.unfinal_member:
            # NO finality evidence at all: not "not final", but "unknown".
            pass
        elif token == self.index_token and self.index_finality_false:
            # Explicit ``False`` on the newest index bar, which IS the as-of bar.
            payload["last_candle_final"] = False
        elif self.member_finality_false is not None and token == self.member_finality_false:
            # Explicit ``False`` on one member whose newest bar is the as-of bar.
            payload["last_candle_final"] = False
        elif self.tail_session in self.unclosed_sessions:
            # The newest bar in the response is a session the calendar has not
            # closed: the platform says so, and it belongs to a bar AFTER the
            # as-of session.
            payload["last_candle_final"] = False
        else:
            payload["last_candle_final"] = True
        return payload


class FakeClient:
    def __init__(self, fixture: Fixture) -> None:
        self.fixture = fixture
        self.history_calls = []

    def get_market_calendar(self, from_date, to_date, *, exchange="NSE", segment="CM"):
        rows = []
        for session in self.fixture.sessions:
            if not (from_date <= session.isoformat() <= to_date):
                continue
            row = {
                "session_date": session.isoformat(),
                "session_type": "REGULAR",
                "exchange": exchange,
                # The platform's calendar reports an IST wall-clock close; the
                # adapter combines it with the session date exactly like the
                # completeness assessment does.
                "closes_at": "15:30:00",
                "opens_at": "09:15:00",
            }
            if session in self.fixture.unclosed_sessions:
                # Known session, no verified close reported. For the as-of session
                # itself the only remaining "proof" would be a date inference,
                # which is refused; for a LATER session it is simply still open.
                row["closes_at"] = None
            rows.append(row)
        return {"exchange": exchange, "segment": segment, "sessions": rows}

    def resolve_ticker(self, symbol):
        if symbol != "NSE:NIFTY 500":
            raise AssertionError(f"unexpected index lookup {symbol}")
        return {"instrument": dict(self.fixture.index_identity)}

    def get_historical_candles(
        self, instrument, timeframe="day", from_date=None, to_date=None
    ):
        # Mirrors the real SDK signature: the instrument is POSITIONAL.
        token = int(instrument)
        self.history_calls.append(token)
        if token in self.fixture.fail_tokens:
            raise RuntimeError("provider unavailable")
        return self.fixture.history(token, str(from_date), str(to_date))

    def get_index_constituents(self, source_list, *, schema_version=1):
        assert source_list == "Nifty500"
        return {
            "complete": True,
            "source_list": source_list,
            "members": [dict(row) for row in self.fixture.members],
            "member_count": len(self.fixture.members),
        }


class FakeRun:
    def __init__(
        self,
        *,
        positions=None,
        coverage="known",
        pending=None,
        refusal=None,
        request_status="executed",
    ) -> None:
        self.positions = list(positions or [])
        self.coverage = coverage
        self.pending = list(pending or [])
        self.refusal = refusal
        # A single status, or a SEQUENCE of statuses the platform is polled
        # through. The sequence form is how "the child waited while the owner
        # decided" is exercised: the durable row moves while the child is alive.
        self.request_statuses = (
            list(request_status) if isinstance(request_status, (list, tuple)) else None
        )
        self.request_status = request_status
        self.request_reads = 0
        self.proposals = []
        self.requests = []

    def _observe_status(self):
        self.request_reads += 1
        if self.request_statuses:
            index = min(self.request_reads - 1, len(self.request_statuses) - 1)
            return self.request_statuses[index]
        return self.request_status

    def attribution(self):
        return {
            "attributed": True,
            "strategy_id": "strat-1",
            "owner_id": "app:owner",
            "account_id": "kite:paper",
            "execution_environment": "paper",
            "binding_source": "hosted_job",
        }

    def owned_work(self):
        return {
            "strategy_run_id": "run-1",
            "strategy_id": "strat-1",
            "account_id": "kite:paper",
            "execution_environment": "paper",
            "coverage": self.coverage,
            "positions": [dict(row) for row in self.positions],
            "pending": [dict(row) for row in self.pending],
            "notes": [] if self.coverage == "known" else ["projection not published"],
        }

    def submit_proposal(self, payload):
        self.proposals.append(payload)
        if self.refusal is not None:
            return {
                "status": "refused",
                "plan": None,
                "refusal": {"rejection_reason": self.refusal},
            }
        return {"status": "validated", "plan": {"plan_id": f"plan-{len(self.proposals)}"}}

    def request_execution(self, plan_id, *, idempotency_key):
        self.requests.append({"plan_id": plan_id, "idempotency_key": idempotency_key})
        return {
            "request_id": "req-1",
            "status": self._observe_status(),
            "plan_id": plan_id,
        }

    def execution_request(self, request_id):
        return {"request_id": request_id, "status": self._observe_status()}


def _ctx(fixture, *, params=None, run=None):
    notes: list = []
    resolved = {
        "budget_inr": 500000,
        "regime_anchor_date": fixture.history_sessions[-6].isoformat(),
        "rebalance_kind": "MONTHLY_LAST_SESSION",
        # The tests do not sit on a bounded dispatch poll; the wait path itself
        # is asserted separately with an explicit deadline.
        "deadline_seconds": 0,
    }
    resolved.update(params or {})
    return (
        types.SimpleNamespace(
            params=resolved,
            run=run or FakeRun(),
            client=FakeClient(fixture),
            run_id="run-1",
            progress=lambda note=None: notes.append(str(note)),
        ),
        notes,
    )


def momentum_main():
    """The real entrypoint, loaded by the real hosted loader."""
    return load_strategy_main(str(SOURCE_PATH))


def momentum_module():
    """The loaded module, so the pure strategy math is testable directly."""
    load_strategy_main(str(SOURCE_PATH))
    return sys.modules["hosted_strategy"]


def _position(symbol, quantity, *, product="CNC", token=200000, exchange="NSE"):
    return {
        "identity_kind": "instrument",
        "identity_key": f"{exchange}:{symbol}",
        "product": product,
        "instrument_token": token,
        "exchange": exchange,
        "tradingsymbol": symbol,
        "net_quantity": quantity,
    }


def _legs(run):
    return {leg["tradingsymbol"]: leg for leg in run.proposals[0]["payload"]["legs"]}


# -- bootstrap ---------------------------------------------------------------

def test_source_boots_through_the_real_loader():
    assert callable(momentum_main())


# -- regime math -------------------------------------------------------------

def test_failed_breadth_is_defensive_even_when_the_index_trend_passes():
    module = momentum_module()
    sessions = _sessions(MONTH_END, 260)
    decision = module.evaluate_regime(
        previous_state=module.RegimeState.RISK_ON,
        index_history=[
            module.Candle(session=session, close=Decimal(str(close)))
            for session, close in _index_series(sessions)
        ],
        member_histories={
            f"M{index}": tuple(
                module.Candle(session=session, close=Decimal(str(close)))
                for session, close in _member_series(sessions, above=False)
            )
            for index in range(10)
        },
    )
    assert decision.state == module.RegimeState.DEFENSIVE
    assert decision.exposure == Decimal("0")
    assert decision.breadth_numerator == 0
    assert decision.breadth_denominator == 10


def test_passing_breadth_keeps_the_asymmetric_index_trend_logic():
    module = momentum_module()
    sessions = _sessions(MONTH_END, 260)
    members = {
        f"M{index}": tuple(
            module.Candle(session=session, close=Decimal(str(close)))
            for session, close in _member_series(sessions, above=True)
        )
        for index in range(10)
    }
    rising = [
        module.Candle(session=session, close=Decimal(str(close)))
        for session, close in _index_series(sessions)
    ]
    falling = [
        module.Candle(session=session, close=Decimal(str(close)))
        for session, close in _index_series(sessions, falling=True)
    ]

    strong = module.evaluate_regime(module.RegimeState.DEFENSIVE, rising, members)
    assert (strong.state, strong.exposure) == (module.RegimeState.RISK_ON, Decimal("1"))

    weak = module.evaluate_regime(module.RegimeState.DEFENSIVE, falling, members)
    assert (weak.state, weak.exposure) == (module.RegimeState.CAUTIOUS, Decimal("0.50"))


def test_ranking_needs_253_aligned_sessions():
    module = momentum_module()
    sessions = _sessions(MONTH_END, 260)
    short = sessions[-252:]
    long_history = tuple(
        module.Candle(session=session, close=Decimal(str(close)))
        for session, close in _member_series(sessions, above=True)
    )
    short_history = tuple(
        module.Candle(session=session, close=Decimal(str(close)))
        for session, close in _member_series(short, above=True)
    )
    ranked, excluded = module.rank_momentum_stocks(
        member_histories={"LONG": long_history, "SHORT": short_history},
        current_prices={"LONG": Decimal("600"), "SHORT": Decimal("600")},
    )
    assert [stock.symbol for stock in ranked] == ["LONG"]
    assert "requires 253 completed candles" in excluded["SHORT"]


def test_position_cap_rejects_a_single_expensive_share():
    module = momentum_module()
    ranked = (
        module.RankedStock("RICH", 1, Decimal("0.5"), Decimal("20000")),
        module.RankedStock("CHEAP", 2, Decimal("0.4"), Decimal("400")),
    )
    targets, _, equal_target, position_cap, _, excluded = module.allocate_equal_weight(
        ranked_stocks=ranked,
        strategy_fund=Decimal("150000"),
        exposure=Decimal("1"),
        target_count=2,
    )
    assert equal_target == Decimal("75000")
    assert position_cap == Decimal("15000")
    assert excluded["RICH"] == "single_share_exceeds_position_cap"
    assert [target.symbol for target in targets] == ["CHEAP"]
    assert targets[0].quantity == 37


# -- calendar and schedule ---------------------------------------------------

def test_a_true_month_end_session_is_due():
    fixture = Fixture(history_end=MONTH_END)
    run = FakeRun()
    ctx, _ = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    assert len(run.proposals) == 1
    assert run.proposals[0]["payload"]["as_of_session"] == MONTH_END.isoformat()


def test_mid_month_is_not_a_rebalance_session():
    """The calendar runs to the end of the month, so today is not the last day."""
    fixture = Fixture(history_end=MID_MONTH)
    run = FakeRun()
    ctx, notes = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    assert run.proposals == []
    assert "off the monthly rebalance session" in " ".join(notes)


def test_calendar_day_31_falls_back_to_the_last_session_and_not_earlier():
    early = Fixture(history_end=DAY31_EARLY)
    early_run = FakeRun()
    early_ctx, early_notes = _ctx(
        early,
        params={"rebalance_kind": "MONTHLY_CALENDAR_DAY", "rebalance_day_of_month": 31},
        run=early_run,
    )
    assert momentum_main()(early_ctx) == 0
    assert early_run.proposals == []
    assert "off the monthly rebalance session" in " ".join(early_notes)

    last = Fixture(history_end=DAY31_LAST)
    last_run = FakeRun()
    last_ctx, _ = _ctx(
        last,
        params={"rebalance_kind": "MONTHLY_CALENDAR_DAY", "rebalance_day_of_month": 31},
        run=last_run,
    )
    assert momentum_main()(last_ctx) == 0
    assert len(last_run.proposals) == 1
    assert last_run.proposals[0]["payload"]["as_of_session"] == DAY31_LAST.isoformat()


# -- strict data handling ----------------------------------------------------

def test_duplicate_dates_are_refused_not_overwritten():
    fixture = Fixture(duplicate_member=True)
    run = FakeRun()
    ctx, notes = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    assert run.proposals == []
    assert "CONSTITUENT_HISTORY_CONTRADICTORY" in " ".join(notes)


def test_non_finite_close_is_refused():
    fixture = Fixture(nonfinite_member=True)
    run = FakeRun()
    ctx, notes = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    assert run.proposals == []
    assert "CONSTITUENT_HISTORY_CONTRADICTORY" in " ".join(notes)


def test_off_calendar_bar_is_not_used():
    fixture = Fixture(offcalendar_member=True)
    run = FakeRun()
    ctx, notes = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    # The off-calendar bar is reported, and the run still produces a plan from
    # the verified sessions only.
    assert len(run.proposals) == 1
    assert any("outside the verified calendar" in note or "read" in note for note in notes)


def test_member_without_finality_evidence_is_a_refusal():
    fixture = Fixture(unfinal_member=True)
    run = FakeRun()
    ctx, notes = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    assert run.proposals == []
    assert "CONSTITUENT_HISTORY_UNAVAILABLE" in " ".join(notes)


def test_a_missing_newest_index_bar_is_a_named_refusal():
    """The as-of date may not roll back to an older bar.

    Every member is fresh and the calendar still proves the month end finished,
    so a rollback would have looked completely healthy: the strategy would have
    re-dated the newest available index bar as "now", shrunk its replay window
    with it, and could trade a signal that is no longer current. Nothing is
    submitted instead.
    """
    fixture = Fixture(index_missing_last=True)
    run = FakeRun()
    ctx, notes = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    assert run.proposals == []
    assert run.requests == []
    joined = " ".join(notes)
    assert "INDEX_HISTORY_STALE" in joined
    assert '"latest_completed": "2026-08-31"' in joined


def test_index_finality_false_on_the_as_of_bar_is_a_refusal():
    """``False`` on the newest returned bar means THAT bar is unfinished.

    When the newest index bar is the as-of session, the verdict is about the bar
    the strategy would plan from, so there is no signal to use.
    """
    fixture = Fixture(index_finality_false=True)
    run = FakeRun()
    ctx, notes = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    assert run.proposals == []
    assert "INDEX_SESSION_NOT_FINAL" in " ".join(notes)


def test_member_finality_false_on_the_as_of_bar_is_a_refusal():
    """"Unfinished" is per series, not per range: one member is enough.

    A member whose newest returned bar is the as-of session and is explicitly not
    final makes the breadth denominator unknown, so the run does nothing at all:
    no entry and no exit.
    """
    fixture = Fixture(member_finality_false=True)
    run = FakeRun()
    ctx, notes = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    assert run.proposals == []
    joined = " ".join(notes)
    assert "CONSTITUENT_SESSION_NOT_FINAL" in joined
    # The platform caps a progress note at 200 characters, so the symbol list is
    # asserted through the refusal's own count rather than the truncated tail.
    assert '"count": 1' in joined


def test_a_not_yet_closed_tail_session_does_not_poison_the_as_of_bar():
    """An explicit ``False`` about a LATER session is not about the as-of bar.

    The calendar knows one more session after the fixture's as-of and reports no
    close for it, and every series carries that session's still-open bar with
    ``last_candle_final=False``. That verdict belongs to the later bar; the as-of
    session is proven finished by its own verified close and still has its own
    bar, so the run proceeds with the SAME as-of date.
    """
    fixture = Fixture(unfinished_tail=True)
    run = FakeRun()
    ctx, notes = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    assert len(run.proposals) == 1
    assert run.proposals[0]["payload"]["as_of_session"] == MONTH_END.isoformat()
    assert fixture.tail_session in fixture.unclosed_sessions
    assert "INDEX_SESSION_NOT_FINAL" not in " ".join(notes)
    assert "CONSTITUENT_SESSION_NOT_FINAL" not in " ".join(notes)


def test_a_session_without_a_verified_close_is_never_the_as_of_session():
    """A calendar session with no reported close is not provably finished.

    The only remaining "proof" would be that the date looks like the past, and
    the adapter refuses to infer completion from a date. The newest PROVABLY
    finished session therefore becomes the as-of date; the month's last session
    has not happened yet, so the run defers by name instead of trading an
    unverified session or reporting a state it cannot support.
    """
    fixture = Fixture(unfinal_index=True)
    run = FakeRun()
    ctx, notes = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    assert run.proposals == []
    joined = " ".join(notes)
    assert "off the monthly rebalance session; no entry" in joined
    # 2026-08-31 is the month end the fixture DELIBERATELY left unverified, so the
    # as-of session falls back to the last Friday with a verified close.
    assert '"as_of": "2026-08-28"' in joined


def test_unreadable_constituent_is_a_refusal_not_an_exclusion():
    fixture = Fixture(fail_member=True)
    run = FakeRun()
    ctx, notes = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    assert run.proposals == []
    joined = " ".join(notes)
    assert "CONSTITUENT_HISTORY_UNAVAILABLE" in joined
    assert "MEMBER00" in joined


def test_gapped_history_refuses_the_whole_run():
    fixture = Fixture(gap_member=True)
    run = FakeRun()
    ctx, notes = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    assert run.proposals == []
    assert "CONSTITUENT_HISTORY_GAP" in " ".join(notes)


def test_genuine_late_listing_is_excluded_but_a_truncated_read_is_refused():
    late = Fixture(late_member=True)
    late_run = FakeRun()
    late_ctx, late_notes = _ctx(late, run=late_run)
    assert momentum_main()(late_ctx) == 0
    assert len(late_run.proposals) == 1
    assert any("late-listed" in note for note in late_notes)

    truncated = Fixture(truncated_member=True)
    truncated_run = FakeRun()
    truncated_ctx, truncated_notes = _ctx(truncated, run=truncated_run)
    assert momentum_main()(truncated_ctx) == 0
    assert truncated_run.proposals == []
    assert "CONSTITUENT_HISTORY_UNAVAILABLE" in " ".join(truncated_notes)


def test_progress_is_reported_while_the_fan_out_runs():
    fixture = Fixture(members_total=60)
    run = FakeRun()
    ctx, notes = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    joined = " ".join(notes)
    assert "reading daily history for 60 constituents" in joined
    assert "constituent history 25/60 read" in joined
    assert "constituent history 60/60 read" in joined


# -- the hosted adapter ------------------------------------------------------

def test_unknown_book_is_a_named_no_action_with_a_preview_only():
    fixture = Fixture()
    run = FakeRun(coverage="unknown")
    ctx, notes = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    assert run.proposals == []
    joined = " ".join(notes)
    assert "OWNED_WORK_COVERAGE_UNKNOWN" in joined
    assert "preview" in joined


def test_fractional_position_is_refused_not_truncated():
    fixture = Fixture()
    run = FakeRun(positions=[_position("MEMBER00", 10.5, token=200000)])
    ctx, notes = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    assert run.proposals == []
    assert "OWNED_WORK_POSITION_NOT_INTEGRAL" in " ".join(notes)


def test_negative_position_is_refused():
    fixture = Fixture()
    run = FakeRun(positions=[_position("MEMBER00", -5, token=200000)])
    ctx, notes = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    assert run.proposals == []
    assert "OWNED_WORK_POSITION_NEGATIVE" in " ".join(notes)


def test_non_nse_position_is_refused():
    fixture = Fixture()
    run = FakeRun(positions=[_position("MEMBER00", 5, token=200000, exchange="BSE")])
    ctx, notes = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    assert run.proposals == []
    assert "OWNED_WORK_POSITION_UNSUPPORTED" in " ".join(notes)


def test_two_instruments_sharing_a_symbol_are_refused():
    fixture = Fixture()
    run = FakeRun(
        positions=[
            _position("MEMBER00", 5, token=200000),
            _position("MEMBER00", 7, token=299999),
        ]
    )
    ctx, notes = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    assert run.proposals == []
    assert "OWNED_WORK_IDENTITY_AMBIGUOUS" in " ".join(notes)


def test_non_cnc_holding_is_refused_by_name():
    fixture = Fixture()
    run = FakeRun(positions=[_position("MEMBER00", 10, product="MIS", token=200000)])
    ctx, notes = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    assert run.proposals == []
    assert "OWNED_WORK_POSITION_UNSUPPORTED" in " ".join(notes)


def test_pending_work_defers_the_rebalance():
    fixture = Fixture()
    run = FakeRun(pending=[{"plan_id": "plan-9", "coverage": "known"}])
    ctx, notes = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    assert run.proposals == []
    assert "OWNED_WORK_PENDING" in " ".join(notes)


@pytest.mark.parametrize("value", [0, -1, "nan", "inf", None])
def test_finite_positive_capital_is_required(value):
    fixture = Fixture()
    run = FakeRun()
    ctx, notes = _ctx(fixture, params={"budget_inr": value}, run=run)
    assert momentum_main()(ctx) == 0
    assert run.proposals == []
    assert "CAPITAL_BASIS_INVALID" in " ".join(notes)


def test_regime_anchor_is_required_for_the_replay():
    fixture = Fixture()
    run = FakeRun()
    ctx, notes = _ctx(fixture, params={"regime_anchor_date": ""}, run=run)
    assert momentum_main()(ctx) == 0
    assert run.proposals == []
    assert "REGIME_ANCHOR_MISSING" in " ".join(notes)


def test_replay_bound_refuses_an_anchor_that_is_too_far_back():
    fixture = Fixture()
    run = FakeRun()
    ctx, notes = _ctx(
        fixture,
        params={
            "regime_anchor_date": fixture.history_sessions[10].isoformat(),
            "regime_replay_max_sessions": 20,
        },
        run=run,
    )
    assert momentum_main()(ctx) == 0
    assert run.proposals == []
    assert "REGIME_REPLAY_TOO_LONG" in " ".join(notes)


def test_monthly_rebalance_submits_exact_whole_share_targets():
    fixture = Fixture()
    run = FakeRun()
    ctx, notes = _ctx(fixture, run=run)

    assert momentum_main()(ctx) == 0
    assert len(run.proposals) == 1

    proposal = run.proposals[0]
    assert proposal["target_kind"] == "intent_bundle"
    assert "target_weights" not in proposal["payload"]
    for leg in proposal["payload"]["legs"]:
        assert isinstance(leg["target_quantity"], int)
        assert leg["target_quantity"] > 0
        assert leg["product"] == "CNC"
        assert leg["reference_price"] > 0

    assert len(run.requests) == 1
    assert run.requests[0]["idempotency_key"].startswith("n500mom-monthly_rebalance-")
    assert any("monthly rebalance" in note for note in notes)


def test_the_plan_targets_exactly_fifteen_names():
    fixture = Fixture(members_total=25)
    run = FakeRun()
    ctx, _ = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    assert len(run.proposals[0]["payload"]["legs"]) == 15


def test_no_change_produces_no_proposal():
    fixture = Fixture()
    first_run = FakeRun()
    ctx, _ = _ctx(fixture, run=first_run)
    assert momentum_main()(ctx) == 0
    wanted = {
        leg["tradingsymbol"]: leg["target_quantity"]
        for leg in first_run.proposals[0]["payload"]["legs"]
    }
    symbol_tokens = {
        row["tradingsymbol"]: row["instrument_token"] for row in fixture.members
    }
    book = [
        _position(symbol, quantity, token=symbol_tokens[symbol])
        for symbol, quantity in wanted.items()
    ]
    run = FakeRun(positions=book)
    ctx, notes = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    assert run.proposals == []
    assert "already matches" in " ".join(notes)


def test_failed_breadth_exits_own_holdings_and_never_enters():
    fixture = Fixture(members_above=0)
    run = FakeRun(positions=[_position("MEMBER00", 25, token=200000)])
    ctx, notes = _ctx(fixture, run=run)

    assert momentum_main()(ctx) == 0
    assert len(run.proposals) == 1
    legs = _legs(run)
    assert legs["MEMBER00"]["target_quantity"] == 0
    assert run.proposals[0]["payload"]["intent"] == "breadth_exit"
    assert "regime DEFENSIVE" in " ".join(notes)
    assert "reference_price" not in legs["MEMBER00"]


def test_the_breadth_exit_waits_for_the_owner_like_every_other_request(monkeypatch):
    """The exit path has no shortcut around the owner's decision.

    A failed-breadth run is a governed request like the monthly rebalance: in
    review-first mode the child stays with the attempt until the decision lands,
    so an exit is never reported as done while it is still parked.
    """
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    fixture = Fixture(members_above=0)
    run = FakeRun(
        positions=[_position("MEMBER00", 25, token=200000)],
        request_status=["awaiting_approval", "queued", "executed"],
    )
    ctx, notes = _ctx(fixture, params={"deadline_seconds": 30}, run=run)
    assert momentum_main()(ctx) == 0
    joined = " ".join(notes)
    assert "waiting on execution request" in joined
    assert "breadth_exit dispatch status=executed" in joined
    assert "no action" not in joined


def test_breadth_failure_with_no_holdings_places_nothing():
    fixture = Fixture(members_above=0)
    run = FakeRun()
    ctx, notes = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    assert run.proposals == []
    assert "holds nothing" in " ".join(notes)


def test_existing_holdings_keep_their_absolute_target():
    fixture = Fixture()
    symbol_tokens = {
        row["tradingsymbol"]: row["instrument_token"] for row in fixture.members
    }
    run = FakeRun(positions=[_position("MEMBER00", 5, token=symbol_tokens["MEMBER00"])])
    ctx, _ = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    legs = _legs(run)
    assert legs["MEMBER00"]["target_quantity"] > 5
    assert legs["MEMBER00"]["reference_price"] > 0


# -- execution-request reporting ---------------------------------------------


def test_a_parked_request_is_not_a_terminal_state():
    """The invariant the review-first hold rests on.

    ``awaiting_approval`` is a decision that has NOT happened: if the adapter
    listed it as terminal, the child would stop waiting, the attempt would end,
    and the approval the owner later issues would be refused by name
    (``HOSTED_ATTEMPT_FENCED``) against a lease nobody holds.
    """
    module = momentum_module()
    assert "awaiting_approval" not in module._TERMINAL_REQUEST_STATES
    assert {"executed", "refused", "rejected", "dispatch_unresolved"} <= (
        module._TERMINAL_REQUEST_STATES
    )


def test_platform_refusal_is_reported_by_name_without_orders():
    fixture = Fixture()
    run = FakeRun(refusal="ALLOCATION_EXCEEDED")
    ctx, notes = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    assert len(run.proposals) == 1
    assert run.requests == []
    joined = " ".join(notes)
    assert "refused the plan" in joined
    assert "ALLOCATION_EXCEEDED" in joined


def test_the_child_holds_while_the_owner_decides(monkeypatch):
    """Review-first: a parked request is NOT an outcome.

    The child keeps the attempt alive (and keeps reporting progress) until the
    platform resolves the request, then exits 0 on the decision it observed. A
    child that returned as soon as it saw ``awaiting_approval`` would end the
    attempt, and the claim that later carries the owner's approval would refuse
    it as ``HOSTED_ATTEMPT_FENCED`` - the parked request would be unactionable.
    """
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    fixture = Fixture()
    run = FakeRun(
        request_status=["awaiting_approval", "awaiting_approval", "queued", "executed"]
    )
    ctx, notes = _ctx(fixture, params={"deadline_seconds": 30}, run=run)
    assert momentum_main()(ctx) == 0
    joined = " ".join(notes)
    # It waited (more than the single mandatory read) ...
    assert "waiting on execution request" in joined
    # ... and it exited on the terminal status it observed, not on the park.
    assert "monthly_rebalance dispatch status=executed" in joined
    assert "no action" not in joined


def test_an_owner_wait_that_outlives_the_bound_is_unresolved(monkeypatch):
    """A wait the bound expires on is UNRESOLVED, never "waiting for the owner".

    The durable request stays valid and may still be approved later, but this
    child observed no decision, so it must not report a finished run.
    """
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    fixture = Fixture()
    run = FakeRun(request_status="awaiting_approval")
    # The bound is real wall-clock time: keep it tiny and skip the poll delay.
    ctx, notes = _ctx(fixture, params={"deadline_seconds": 0.05}, run=run)
    assert momentum_main()(ctx) == 2
    joined = " ".join(notes)
    assert (
        "monthly_rebalance was still waiting for the owner's decision "
        "at the attempt's deadline" in joined
    )
    assert '"bound_seconds": 0.05' in joined
    assert "no action" not in joined


def test_queued_autonomous_dispatch_is_unresolved_not_an_owner_wait():
    fixture = Fixture()
    run = FakeRun(request_status="dispatching")
    ctx, notes = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 2
    joined = " ".join(notes)
    assert "not finished and is not an owner wait" in joined
    assert "waiting for the owner" not in joined


def test_a_bounded_wait_polls_then_reports_the_durable_request():
    """A short deadline still polls, then reports the durable row honestly."""
    fixture = Fixture()
    run = FakeRun(request_status="queued")
    ctx, notes = _ctx(fixture, params={"deadline_seconds": 0.2}, run=run)
    assert momentum_main()(ctx) == 2
    joined = " ".join(notes)
    assert "waiting on execution request" in joined
    # The progress note is capped at 200 characters by the platform; the wording
    # is asserted on the part that survives that cap. The note must not promise
    # that the parked row will be dispatched later: the claim re-reads the attempt,
    # so a fresh attempt is what acts on the decision.
    assert "this attempt's authority ends with the child" in joined
    assert "dispatching after the child exits" not in joined


def test_dispatch_unresolved_is_unresolved():
    fixture = Fixture()
    run = FakeRun(request_status="dispatch_unresolved")
    ctx, notes = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 2
    assert "outcome is unknown" in " ".join(notes)


def test_an_unreadable_index_is_a_named_no_action():
    fixture = Fixture()
    fixture.fail_tokens.add(fixture.index_token)
    run = FakeRun()
    ctx, notes = _ctx(fixture, run=run)
    assert momentum_main()(ctx) == 0
    assert run.proposals == []
    assert "INDEX_HISTORY_UNAVAILABLE" in " ".join(notes)


def test_a_source_that_cannot_run_returns_unresolved():
    fixture = Fixture()
    ctx, notes = _ctx(fixture)

    def boom(symbol):
        raise RuntimeError("boom")

    ctx.client.resolve_ticker = boom
    assert momentum_main()(ctx) == 2
    assert "unresolved" in " ".join(notes)


def test_schema_documents_the_contract():
    schema = json.loads(
        (REPO / "examples" / "hosted_platform" / "nifty500_momentum.schema.json").read_text()
    )
    assert schema["required"] == ["budget_inr", "regime_anchor_date"]
    assert schema["additionalProperties"] is False
    assert "coverage_policy" not in schema["properties"]
    assert schema["properties"]["initial_regime"]["default"] == "DEFENSIVE"
    assert schema["properties"]["budget_inr"]["exclusiveMinimum"] == 0
    assert schema["properties"]["regime_replay_max_sessions"]["maximum"] == 750


def test_every_schema_parameter_is_one_the_adapter_actually_reads():
    """The composer offers nothing the adapter ignores, with matching defaults.

    The composer validates a run against this schema and the child then resolves
    ``ctx.params`` itself; a parameter offered by the schema but unread by the
    adapter would be silently ignored, which is the mismatch worth refusing.
    """
    module = momentum_module()
    schema = json.loads(
        (REPO / "examples" / "hosted_platform" / "nifty500_momentum.schema.json").read_text()
    )
    exposed = set(schema["properties"])
    assert exposed.isdisjoint({"product", "regime_anchor", "initial_state", "schedule"})
    # Nothing the composer offers may be ignored by the adapter ...
    assert exposed <= set(module._PARAM_DEFAULTS) | {"budget_inr", "regime_anchor_date"}
    # ... and a parameter the adapter keeps internal (``product``) is a named
    # refusal, never a silent acceptance of another product.
    assert module._PARAM_DEFAULTS["product"] == module.PRODUCT_CNC
    for name, spec in schema["properties"].items():
        if "default" not in spec:
            continue
        assert spec["default"] == module._PARAM_DEFAULTS[name], name
