#!/usr/bin/env python3
"""Phase 6 milestone 6C.3 — alerts evaluation capacity benchmark.

What this measures
------------------
The EVALUATION path: the part that has to scale to the documented
500-symbol / 5,000-rule workload. Workflows are authored, activated and
materialized exactly as production does, and observations enter through
``EvaluationWorker._dispatch`` — the same entry point a live tick or completed
candle uses. No component is called directly, so indexing, the ownership fence,
the publication transaction and the outbox are all on the measured path.

What this does NOT measure
--------------------------
Redis pub/sub fan-out (pass ``--redis-url`` to measure publish throughput
separately) and the delivery/notification path (deliveries are enqueued, not
sent). Both are reported as NOT MEASURED rather than assumed.

Honesty rule (spec §10.6)
-------------------------
If the documented target is not met, this reports the MEASURED supported
capacity and the bottleneck. It never extrapolates a smaller run up to the
target, and it never prints PASS for a run that did not actually cover the
target workload.

Usage
-----
    python scripts/alerts_capacity_benchmark.py \\
        --database-url postgresql+psycopg2://user:pw@host:5432/kite_bench \\
        --symbols 500 --rules 5000 --rate 200 --duration-s 120

    # find the ceiling instead of testing one rate
    python scripts/alerts_capacity_benchmark.py --database-url ... --ramp
"""

from __future__ import annotations

import argparse
import asyncio
import json
import resource
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, func, select, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.alerts.predicates import Observation  # noqa: E402
from backend.notifications.repository import Delivery  # noqa: E402
from backend.workflows import advanced_repository  # noqa: E402, F401 (registers tables)
from backend.workflows.compiler import compile_document  # noqa: E402
from backend.workflows.parser import parse_workflow_dict  # noqa: E402
from backend.workflows.repository import (  # noqa: E402
    Base,
    EvaluationCheckpoint,
    SignalEvent,
    SqlAlchemyWorkflowRepository,
)
from backend.workflows.runtime import EvaluationWorker  # noqa: E402
from backend.workflows.service import EvaluationService  # noqa: E402

# ---------------------------------------------------------------------------
# rule mix — documented, and configurable
#
# The plan requires the mix to be stated rather than implied, because the
# mix drives the result far more than the raw rule count does: an LTP edge rule
# is a checkpoint write, an indicator rule is a history read plus a feature
# computation, and a breadth rule takes a workflow-level advisory lock.
# ---------------------------------------------------------------------------

DEFAULT_MIX: Dict[str, float] = {
    "ltp_edge": 0.50,        # clock: ltp,      crosses_above         (cheap, hot path)
    "candle_edge": 0.30,     # clock: candle_close, crosses_above      (bar bookkeeping)
    "candle_indicator": 0.15,  # clock: candle_close + sma condition   (history + features)
    "breadth": 0.05,         # breadth stage                          (advisory lock + count)
}

#: Fixed so successive runs compare like with like.
TIMEFRAME = "5minute"
_REFERENCE_TS: Dict[str, datetime] = {"now": datetime.now(timezone.utc)}


@dataclass
class Options:
    database_url: str
    symbols: int
    rules: int
    rate: float
    duration_s: float
    concurrency: int
    owner: str
    target_lag_p95_ms: float
    ramp: bool
    lane_sweep: bool
    mix: Dict[str, float]
    shard_index: int
    shard_count: int
    redis_url: Optional[str]
    keep: bool
    json_out: Optional[str]


@dataclass
class RunResult:
    rate_achieved: float
    dispatched: int
    errors: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    duration_s: float
    db_writes: int
    rss_mb: float
    events: int = 0
    deliveries: int = 0
    cpu_cores: float = 0.0
    tick_rate_achieved: float = 0.0
    target_rate: float = 0.0
    concurrency: int = 1
    latencies: List[float] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


#: Lane counts swept by ``--lane-sweep``. A power-of-two ladder is enough to
#: see where throughput stops scaling, which is the signal that a serialised
#: resource has been reached.
LANE_LADDER = (1, 2, 4, 8, 16, 32)

#: Experiments that isolate one hypothesis at a time about what serialises.
#: `full` is the documented mix; the others remove one suspected bottleneck so
#: the latency difference attributes cost rather than guessing at it.
MIX_PRESETS: Dict[str, Dict[str, float]] = {
    "full": dict(DEFAULT_MIX),
    # Breadth takes a workflow-level advisory lock, so if locks are the
    # bottleneck, removing just this 5% should recover far more than 5%.
    "no_breadth": {k: v for k, v in DEFAULT_MIX.items() if k != "breadth"},
    # An LTP edge rule is checkpoint read + event + checkpoint CAS: pure
    # round-trip cost with no history read and no feature computation.
    "ltp_only": {"ltp_edge": 1.0},
    # A candle indicator rule adds a bounded history read and an SMA over the
    # window, i.e. the CPU/IO cost, with no lock.
    "indicator_only": {"candle_indicator": 1.0},
    # Candle edge: bar bookkeeping without the feature engine.
    "candle_only": {"candle_edge": 1.0},
}


# ---------------------------------------------------------------------------
# synthetic candle history
# ---------------------------------------------------------------------------


class SyntheticHistory:
    """Deterministic bars for indicator rules, ending at the fed timestamp.

    Returned as real ``Observation`` objects (the same contract
    ``PgCandleHistory.recent_bars`` satisfies) so the feature engine does its
    real work instead of short-circuiting on an empty history.
    """

    def __init__(self, period: int) -> None:
        self.period = period

    def recent_bars(self, instrument_key: str, timeframe: str, limit: int) -> List[Observation]:
        end = _REFERENCE_TS["now"]
        bars: List[Observation] = []
        for offset in range(min(limit, self.period), 0, -1):
            ts = end - timedelta(minutes=5 * offset)
            close = 100.0 + (offset % 7)
            bars.append(
                Observation(
                    ts=ts, epoch_id="candle", ltp=close, open=close, high=close + 1,
                    low=close - 1, close=close, volume=1000.0, final=True,
                )
            )
        return bars

    def previous_session_levels(self, instrument_key: str, at: datetime) -> Optional[dict]:
        return None


class _FakeSource:
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def next_observation(self) -> None:
        return None


# ---------------------------------------------------------------------------
# workload authoring
# ---------------------------------------------------------------------------


def _instrument(index: int) -> str:
    return f"NSE:S{index:04d}"


def _condition(op: str, right: Any, field_name: str = "close") -> dict:
    return {"left": {"field": field_name}, "op": op, "right": right}


def _document(kind: str, symbols: Sequence[str], *, name: str) -> dict:
    """One workflow realizing a single rule kind over `symbols`."""
    common = {
        "version": 1,
        "name": name,
        "session": "nse_equity",
        "instruments": list(symbols),
    }

    if kind == "breadth":
        members = list(symbols)[:50]
        return {
            **common,
            "instruments": members,
            "stages": [
                {
                    "id": "breadth",
                    "type": "breadth",
                    "clock": "candle_close",
                    "timeframe": TIMEFRAME,
                    "breadth": {
                        "condition": {"all": [_condition("gt", {"value": 100})]},
                        "distinct_instruments": max(2, min(len(members), 10)),
                        "window": "30m",
                    },
                }
            ],
            "alerts": [
                {"id": "ba", "source": "breadth", "trigger": "on_transition", "channels": []}
            ],
        }

    if kind == "ltp_edge":
        clock, timeframe = "ltp", None
        stage = {
            "id": "px",
            "type": "signal",
            "clock": clock,
            "conditions": {"all": [_condition("crosses_above", {"value": 100})]},
        }
    elif kind == "candle_edge":
        clock, timeframe = "candle_close", TIMEFRAME
        stage = {
            "id": "px",
            "type": "signal",
            "clock": clock,
            "timeframe": timeframe,
            "conditions": {"all": [_condition("crosses_above", {"value": 100})]},
        }
    elif kind == "candle_indicator":
        clock, timeframe = "candle_close", TIMEFRAME
        stage = {
            "id": "px",
            "type": "signal",
            "clock": clock,
            "timeframe": timeframe,
            "conditions": {
                "all": [_condition("gt", {"indicator": "sma", "period": 20})]
            },
        }
    else:  # pragma: no cover - guarded by the caller
        raise ValueError(f"unknown rule kind {kind!r}")

    return {
        **common,
        "stages": [stage],
        # No channels, deliberately. A channel NAME that resolves to nothing
        # makes the delivery insert violate its FK, and the whole transaction —
        # event included — rolls back. Since the workload is about evaluation
        # cost, an empty channel list keeps the event/outbox path real while
        # leaving delivery fan-out out of the picture (which this harness
        # reports as NOT MEASURED anyway). Seed real channel rows and pass their
        # names here if delivery fan-out is what you want to measure.
        "alerts": [{"id": "a1", "source": "px", "trigger": "on_transition", "channels": []}],
    }


@dataclass
class WorkflowSpec:
    kind: str
    name: str
    revision: Any


def seed_workload(
    session_factory, *, owner: str, options: Options, mix: Dict[str, float]
) -> List[WorkflowSpec]:
    """Author → activate → materialize, exactly the production order.

    With ``--shard`` only this shard's share of the workflows is created. A
    single process can only use ~1 CPU core (see the GIL note in
    ``run_at_rate``), so scaling the offered rate requires several processes,
    and each must own a disjoint set of workflows or they collide on names.
    """
    repo = SqlAlchemyWorkflowRepository(session_factory)
    service = EvaluationService(repo, session_factory)
    symbols = [_instrument(i) for i in range(options.symbols)]

    # Build the full plan first, then take this shard's slice, so every shard
    # agrees on what the total workload is.
    plan: List[Tuple[str, int, List[str]]] = []
    for kind, share in mix.items():
        # A workflow over `symbols` instruments contributes ~`symbols`
        # subscriptions (one per instrument per alert), so the number of
        # workflows needed for this kind's share follows directly.
        target_rules = max(options.symbols, round(options.rules * share))
        workflows = max(1, round(target_rules / max(1, options.symbols)))
        covered = list(symbols)
        if kind == "breadth":
            covered = list(symbols)[:50]
        for index in range(workflows):
            plan.append((kind, index, covered))

    mine = [
        item
        for position, item in enumerate(plan)
        if position % options.shard_count == options.shard_index
    ]

    specs: List[WorkflowSpec] = []
    for kind, index, covered in mine:
        name = f"bench-{kind}-{index:03d}"
        document = _document(kind, covered, name=name)
        compiled = compile_document(parse_workflow_dict(document))
        workflow, revision = repo.create_workflow(
            owner, name, compiled.document.to_document_dict(), compiled.canonical_hash
        )
        repo.activate_revision(workflow.id, revision.id)
        active = repo.get_active_revision(workflow.id)
        service.ensure_subscriptions(active)
        specs.append(WorkflowSpec(kind=kind, name=name, revision=active))
    return specs


# ---------------------------------------------------------------------------
# instrumentation
# ---------------------------------------------------------------------------


def _write_counts(session_factory) -> Tuple[int, int]:
    """(events, deliveries) — the proof that the write path was exercised."""
    with session_factory() as session:
        events = session.execute(select(func.count()).select_from(SignalEvent)).scalar_one()
        deliveries = session.execute(select(func.count()).select_from(Delivery)).scalar_one()
    return int(events), int(deliveries)


def _count_rows(session_factory) -> int:
    with session_factory() as session:
        events = session.execute(select(func.count()).select_from(SignalEvent)).scalar_one()
        checkpoints = session.execute(
            select(func.count()).select_from(EvaluationCheckpoint)
        ).scalar_one()
        deliveries = session.execute(select(func.count()).select_from(Delivery)).scalar_one()
    return int(events) + int(checkpoints) + int(deliveries)


def _rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB, macOS bytes.
    return rss / 1024.0 if sys.platform != "darwin" else rss / (1024.0 * 1024.0)


def _percentiles(samples: List[float]) -> Tuple[float, float, float]:
    if not samples:
        return (0.0, 0.0, 0.0)
    ordered = sorted(samples)
    return (
        statistics.median(ordered) * 1000.0,
        ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] * 1000.0,
        ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))] * 1000.0,
    )


def _clock_for(sub: Any) -> str:
    """The clock of the stage this subscription belongs to.

    ``ActiveSubscription`` carries the revision document, not a resolved clock,
    so the stage is looked up here the same way the runtime identifies it.
    """
    for stage in sub.document.get("stages", []) or []:
        if stage.get("id") == sub.stage_id:
            return str(stage.get("clock") or "candle_close")
    return "candle_close"


def _dispatch_loop(
    streams: List[Tuple[Tuple[str, str], List[Any]]],
    *,
    rate: float,
    deadline: float,
    start_ts: datetime,
    step: Dict[str, timedelta],
    latencies: List[float],
    errors: List[str],
) -> Tuple[int, int]:
    """One dispatch lane: a disjoint set of STREAMS, paced to ``rate``.

    Each lane owns its streams outright so two threads never feed the same
    instrument concurrently — that mirrors production, where one source loop
    drives one instrument's ticks and a subscription is never fed twice at once.

    Returns ``(observations_dispatched, stream_ticks)``. The two differ by the
    fan-out factor, which is the point: one tick per instrument is delivered to
    every rule on that instrument.
    """
    # Keyed by STREAM, never by instrument. Two streams can share a symbol (an
    # LTP rule and a candle rule on NSE:TCS advance time by different steps);
    # sharing one clock per symbol made timestamps non-monotonic, so the runtime
    # read them as replays and dropped them.
    clocks: Dict[Tuple[str, str], datetime] = {key: start_ts for key, _ in streams}
    ticks: Dict[Tuple[str, str], int] = {}
    dispatched = 0
    stream_ticks = 0
    next_at = time.perf_counter()

    while time.perf_counter() < deadline:
        next_at += 1.0 / rate
        sleep_for = next_at - time.perf_counter()
        if sleep_for > 0:
            time.sleep(sleep_for)

        key, members = streams[stream_ticks % len(streams)]
        stream_ticks += 1
        instrument_key, clock = key
        ts = clocks[key] + step.get(clock, step["candle_close"])
        clocks[key] = ts
        _REFERENCE_TS["now"] = ts

        # Alternate either side of the 100 threshold every few ticks, so the
        # crossing rules genuinely fire. A series that stays above the threshold
        # never CROSSES it, and would leave the event + outbox write path
        # completely unexercised — an optimistic measurement that looks fine
        # because nothing ever fires.
        tick = ticks.get(key, 0)
        ticks[key] = tick + 1
        close = 98.0 if (tick // 2) % 2 == 0 else 104.0

        observation = Observation(
            ts=ts,
            # A constant epoch per clock: a fresh epoch on every tick would make
            # the benchmark measure epoch resets instead of evaluation.
            epoch_id="candle" if clock == "candle_close" else "ltp",
            ltp=close,
            open=close,
            high=close + 1.0,
            low=close - 1.0,
            close=close,
            volume=1000.0,
            final=True,
            # Event time == receipt time, so the 6A.0 skew bounds see a perfectly
            # fresh tick and the measurement is about capacity, not staleness.
            received_at=ts,
        )

        # Fan out: one observation, every rule on this stream. Each rule's
        # evaluation time is measured separately, because that is the number
        # that scales with the rule count.
        for sub in members:
            began = time.perf_counter()
            try:
                _DISPATCH(sub, observation)
            except Exception as exc:  # noqa: BLE001 - counted, never swallowed silently
                if len(errors) < 5:
                    errors.append(f"dispatch error: {type(exc).__name__}: {exc}")
            latencies.append(time.perf_counter() - began)
            dispatched += 1

    return dispatched, stream_ticks


#: Set by ``run_at_rate`` so the lane function can reach the worker without
#: threading it through every call signature.
_DISPATCH: Any = None


def _streams(subscriptions: List[Any]) -> Dict[Tuple[str, str], List[Any]]:
    """Group subscriptions into the STREAMS production actually feeds.

    A stream is one ``(instrument, clock)`` pair: an LTP tick feed or a
    completed-candle feed for one symbol. Every rule on that stream receives the
    same observation — that is what a real feed does, and it is why 5,000 rules
    over 500 symbols is 1,000 streams rather than 5,000 independent inputs.

    Dispatching per SUBSCRIPTION instead (the first version of this harness)
    under-samples every rule by its fan-out factor: with 10 rules per symbol each
    rule saw a tenth of the ticks, so almost nothing ever crossed and the
    workload was not the documented one.
    """
    streams: Dict[Tuple[str, str], List[Any]] = {}
    for sub in subscriptions:
        streams.setdefault((sub.instrument_key, _clock_for(sub)), []).append(sub)
    return streams


def run_at_rate(
    worker: EvaluationWorker,
    session_factory,
    *,
    rate: float,
    duration_s: float,
    concurrency: int,
) -> RunResult:
    """Feed `rate` stream-ticks per second across `concurrency` lanes.

    `rate` counts STREAM TICKS (one per (instrument, clock)), not rule
    evaluations: one tick fans out to every rule on that stream, so the rule
    evaluation rate is `rate x fan-out`. That is the honest way to state the
    input, because it is what a market feed actually delivers.

    Concurrency matters too: dispatching from one lane measures the harness, not
    the service. With one lane the ceiling is roughly `1 / per-evaluation
    latency`, far below the documented input rate.
    """
    global _DISPATCH
    subscriptions = list(worker._subscriptions)
    if not subscriptions:
        raise RuntimeError("no subscriptions were materialized — seeding failed")

    grouped = _streams(subscriptions)
    stream_items = list(grouped.items())
    lanes = max(1, min(concurrency, len(stream_items)))
    # Interleave rather than slice contiguously, so every lane gets a mix of
    # stream kinds (LTP, candle) instead of one lane owning all the breadth work.
    partitions: List[List[Tuple[Tuple[str, str], List[Any]]]] = [[] for _ in range(lanes)]
    for index, item in enumerate(stream_items):
        partitions[index % lanes].append(item)

    _DISPATCH = worker._dispatch

    start_ts = datetime.now(timezone.utc)
    step = {"ltp": timedelta(seconds=1), "candle_close": timedelta(minutes=5)}
    writes_before = _count_rows(session_factory)
    events_before, deliveries_before = _write_counts(session_factory)

    started = time.perf_counter()
    deadline = started + duration_s

    import threading

    latencies: List[List[float]] = [[] for _ in range(lanes)]
    errors: List[str] = []
    counts = [0] * lanes
    tick_counts = [0] * lanes
    # Process CPU time across ALL threads. Divided by wall time this gives the
    # effective core count, which is the decisive diagnostic: if it pins at
    # ~1.0 the driver is GIL-bound and the ceiling is the harness, not the
    # service. Without this the two are indistinguishable.
    cpu_before = time.process_time()

    def lane(index: int) -> None:
        counts[index], tick_counts[index] = _dispatch_loop(
            partitions[index],
            rate=rate / lanes,
            deadline=deadline,
            start_ts=start_ts,
            step=step,
            latencies=latencies[index],
            errors=errors,
        )

    threads = [threading.Thread(target=lane, args=(index,), daemon=True) for index in range(lanes)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    elapsed = time.perf_counter() - started
    cpu_cores = (time.process_time() - cpu_before) / elapsed if elapsed else 0.0
    writes_after = _count_rows(session_factory)
    events_after, deliveries_after = _write_counts(session_factory)
    flat = [value for chunk in latencies for value in chunk]
    p50, p95, p99 = _percentiles(flat)

    return RunResult(
        rate_achieved=sum(counts) / elapsed if elapsed else 0.0,
        tick_rate_achieved=sum(tick_counts) / elapsed if elapsed else 0.0,
        dispatched=sum(counts),
        errors=len(errors),
        p50_ms=p50,
        p95_ms=p95,
        p99_ms=p99,
        duration_s=elapsed,
        db_writes=writes_after - writes_before,
        events=events_after - events_before,
        deliveries=deliveries_after - deliveries_before,
        rss_mb=_rss_mb(),
        cpu_cores=cpu_cores,
        target_rate=rate,
        concurrency=lanes,
        latencies=flat,
        notes=list(errors),
    )


# ---------------------------------------------------------------------------
# building the worker
# ---------------------------------------------------------------------------


async def build_worker(session_factory, *, owner: str, period: int) -> EvaluationWorker:
    repo = SqlAlchemyWorkflowRepository(session_factory)
    worker = EvaluationWorker(
        repo,
        session_factory,
        channel_resolver=None,
        tick_source_factory=lambda key: _FakeSource(),
        candle_source_factory=lambda key, tf: _FakeSource(),
        candle_history=SyntheticHistory(period),
        poll_interval_s=0.01,
        instrument_resolver=lambda keys: ({key: 1 for key in keys}, set()),
        renewal=None,
        owner_id=owner,
    )
    await worker.start()
    return worker


async def measure(
    session_factory, options: Options, rate: float, *, concurrency: Optional[int] = None
) -> RunResult:
    lanes = concurrency if concurrency is not None else options.concurrency
    worker = await build_worker(session_factory, owner=options.owner, period=30)
    try:
        result = run_at_rate(
            worker,
            session_factory,
            rate=rate,
            duration_s=options.duration_s,
            concurrency=lanes,
        )
        result.concurrency = lanes
        return result
    finally:
        try:
            await worker.stop()
        except Exception:  # noqa: BLE001 - teardown must not mask the result
            pass


def preflight(engine) -> List[str]:
    """Report what the database is missing, rather than failing confusingly.

    The benchmark needs a database built by the real migration chain
    (``alembic upgrade head``). ``backend/schema.sql`` is the *evolving*
    reference DDL and assumes the frozen baseline, so it cannot build a database
    from zero on its own — a run against a half-built database produces
    misleading failures deep in the write path.
    """
    notes: List[str] = []
    with engine.connect() as connection:
        missing = [
            name
            for name in ("signal_events", "evaluation_checkpoints", "alert_subscriptions")
            if connection.execute(
                text(f"SELECT to_regclass('public.{name}')")
            ).scalar()
            is None
        ]
        catalog = connection.execute(
            text("SELECT to_regclass('public.kite_ticker_tickers')")
        ).scalar()
    if missing:
        notes.append(
            f"core tables missing ({', '.join(missing)}) — migrate the database first: "
            "DATABASE_URL=<url> python -m alembic -c backend/alembic.ini upgrade head"
        )
    if catalog is None:
        notes.append(
            "instrument catalog tables absent — subscriptions will carry no binding. "
            "Harmless for this benchmark (a synthetic history adapter is used), but it "
            "means the run does not exercise provenance writes."
        )
    return notes


def _cleanup(session_factory, owner: str) -> None:
    """Remove this benchmark's own workflows. Scoped to its owner only.

    Every statement is scoped by an owner subquery rather than an id array:
    passing a Python list against a ``uuid`` column needs an explicit cast, and
    getting that wrong fails at the very end of a long run — the worst possible
    moment to discover a typing mistake. Children are removed before parents so
    the foreign keys are satisfied without relying on cascade behaviour.

    Note the casts: ``workflows.id`` is text but ``signal_events.workflow_id``
    and the Phase 4 tables are uuid, so an uncast comparison raises
    "operator does not exist: uuid = text". That mix is pre-existing in the
    schema; the casts accommodate it rather than papering over it.
    """
    workflows = "SELECT id FROM workflows WHERE owner_id = :owner"
    revisions = f"SELECT id FROM workflow_revisions WHERE workflow_id IN ({workflows})"
    subscriptions = f"SELECT id FROM alert_subscriptions WHERE revision_id IN ({revisions})"

    statements = [
        # deliveries -> signal_events -> subscriptions -> revisions -> workflows
        f"DELETE FROM deliveries WHERE event_id IN "
        f"(SELECT id FROM signal_events WHERE workflow_id::text IN ({workflows}))",
        f"DELETE FROM signal_events WHERE workflow_id::text IN ({workflows})",
        f"DELETE FROM alert_breadth_triggers WHERE workflow_id::text IN ({workflows})",
        f"DELETE FROM alert_breadth_state WHERE workflow_id::text IN ({workflows})",
        f"DELETE FROM alert_session_counters WHERE workflow_id::text IN ({workflows})",
        f"DELETE FROM alert_suppression_counters WHERE workflow_id::text IN ({workflows})",
        f"DELETE FROM evaluation_checkpoints WHERE subscription_id IN ({subscriptions})",
        f"DELETE FROM alert_subscriptions WHERE revision_id IN ({revisions})",
        f"DELETE FROM workflow_canvas_layout WHERE workflow_id IN ({workflows})",
        f"DELETE FROM workflow_revisions WHERE workflow_id IN ({workflows})",
        "DELETE FROM workflows WHERE owner_id = :owner",
    ]

    from backend.workflows.repository import Workflow  # noqa: F401 - table registration

    with session_factory() as session:
        for statement in statements:
            session.execute(text(statement), {"owner": owner})
        session.commit()


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def report(options: Options, results: List[RunResult], *, certified: bool) -> None:
    print()
    print("=" * 72)
    print("alerts evaluation capacity benchmark")
    print("=" * 72)
    print(f"workload      : {options.symbols} symbols, {options.rules} rules")
    print(f"rule mix      : {json.dumps(options.mix)}")
    print(f"lanes/shard   : {options.concurrency}")
    print(f"offered ticks : {options.rate:.1f}/s per shard (total {options.rate * max(1, options.shard_count):.1f}/s)")
    if options.shard_count > 1:
        print(f"shards        : {options.shard_count} x {options.concurrency} lanes")
    print(f"database      : {options.database_url.split('@')[-1]}")
    print(f"target p95    : {options.target_lag_p95_ms:.0f} ms")
    print()
    print(f"{'lanes':>5} {'ticks/s':>8} {'evals/s':>8} {'p50 ms':>8} {'p95 ms':>8} "
          f"{'p99 ms':>8} {'cores':>6} {'events':>8} {'errors':>7} {'rss MB':>8}")
    for result in results:
        print(
            f"{result.concurrency:>5} "
            f"{result.tick_rate_achieved:>8.1f} "
            f"{result.rate_achieved:>8.1f} "
            f"{result.p50_ms:>8.1f} {result.p95_ms:>8.1f} {result.p99_ms:>8.1f} "
            f"{result.cpu_cores:>6.2f} {result.events:>8} "
            f"{result.errors:>7} {result.rss_mb:>8.1f}"
        )
    print()
    tick_rate = results[-1].tick_rate_achieved
    if tick_rate:
        print(f"fan-out       : {results[-1].rate_achieved / tick_rate:.1f} rule evaluations per stream tick")

    best = results[-1]

    # Fidelity guard, BEFORE any verdict. A run where nothing ever fired has
    # not measured the event + outbox + delivery write path at all, and its
    # latency is optimistic. Reporting a capacity number from it — even a
    # "NOT MET" one — would be misleading, so this refuses rather than
    # qualifying.
    total_events = sum(result.events for result in results)
    total_deliveries = sum(result.deliveries for result in results)
    if total_events == 0:
        print("FIDELITY FAILURE — the workload produced NO signal events.")
        print(
            "  Nothing crossed, so the event, outbox and delivery write path was never\n"
            "  exercised and the latencies above exclude it. They are optimistic and must\n"
            "  not be reported as capacity. Fix the synthetic series (it must CROSS the\n"
            "  rule threshold) or the rule mix before re-running."
        )
        for result in results:
            for note in result.notes:
                print(f"  note: {note}")
        return

    verdict = "PASS" if certified and best.p95_ms <= options.target_lag_p95_ms and not best.errors else "NOT MET"
    print(f"verdict       : {verdict}")
    print(f"events fired  : {total_events}, deliveries enqueued: {total_deliveries}")
    if verdict == "NOT MET":
        print(
            "MEASURED SUPPORTED CAPACITY: "
            f"{best.rate_achieved:.1f} observations/s, p95 {best.p95_ms:.1f} ms "
            f"({options.symbols} symbols, {options.rules} rules)."
        )
        print(
            "This is the supported figure. It is NOT an extrapolation to the "
            "documented target and must be reported as such (spec §10.6)."
        )
        if best.errors:
            print(f"first errors  : {best.notes[:3]}")
        diagnose(results)
    else:
        print("Redis pub/sub and delivery fan-out: NOT MEASURED by this harness.")

    for result in results:
        for note in result.notes:
            print(f"  note: {note}")


def diagnose(results: List[RunResult]) -> None:
    """Say what the numbers can and cannot support.

    The core count decides whether a figure describes the service or the
    driver. Reporting a latency number without it invites exactly the wrong
    conclusion — which is what happened on this harness's first outing, where a
    ~1-core ceiling was nearly read as a service limit.
    """
    peak_cores = max((result.cpu_cores for result in results), default=0.0)
    if peak_cores < 1.5:
        print()
        print(
            f"DIAGNOSIS — DRIVER-BOUND, NOT SERVICE-BOUND.\n"
            f"  The driver consumed only {peak_cores:.2f} CPU cores across all lanes, so it ran\n"
            f"  into the GIL: the measured ceiling is this harness's, not the service's. Threads\n"
            f"  in one Python process cannot use more than ~1 core of interpreter work.\n"
            f"  ACTION: re-run with --shard to spread the driver over several processes and\n"
            f"  re-take the measurement. Do NOT report the number above as service capacity, and\n"
            f"  do NOT tune the service based on it."
        )
    else:
        print()
        print(
            f"DIAGNOSIS — the driver used {peak_cores:.2f} cores, so the ceiling is not simply\n"
            f"  the interpreter. Attribute the remaining cost by comparing mixes:\n"
            f"    --mix no_breadth   (tests the breadth advisory lock)\n"
            f"    --mix ltp_only     (tests pure DB round-trip cost)\n"
            f"    --mix indicator_only (tests history read + feature computation)\n"
            f"  and by watching whether p99 tracks p95 (uniform cost) or departs from it\n"
            f"  (lock contention / pool exhaustion)."
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--database-url",
        default=None,
        help="PostgreSQL URL (required unless --aggregate is used)",
    )
    parser.add_argument("--symbols", type=int, default=500)
    parser.add_argument("--rules", type=int, default=5000)
    parser.add_argument("--rate", type=float, default=200.0, help="observations per second")
    parser.add_argument("--duration-s", type=float, default=120.0)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="parallel dispatch lanes; the service is measured through them",
    )
    parser.add_argument("--owner", default="capacity-bench")
    parser.add_argument("--target-lag-p95-ms", type=float, default=250.0)
    parser.add_argument("--ramp", action="store_true", help="step the rate up to find the ceiling")
    parser.add_argument(
        "--lane-sweep",
        action="store_true",
        help="hold the rate and sweep lanes, to see where throughput stops scaling",
    )
    parser.add_argument(
        "--mix",
        default="full",
        help=f"rule mix preset or JSON: {', '.join(MIX_PRESETS)} or '{{\"ltp_edge\": 1.0}}'",
    )
    parser.add_argument("--redis-url", default=None)
    parser.add_argument(
        "--shard",
        default="0/1",
        help=(
            "I/N — run only this shard's workflows. Use N processes to escape the "
            "single-interpreter GIL ceiling; aggregate the outputs with --aggregate."
        ),
    )
    parser.add_argument(
        "--aggregate",
        nargs="+",
        default=None,
        metavar="JSON",
        help="merge shard JSON outputs into one report instead of running a workload",
    )
    parser.add_argument("--keep", action="store_true", help="do not delete seeded workflows")
    parser.add_argument("--json-out", default=None)
    return parser


def resolve_mix(spec: str) -> Dict[str, float]:
    """A named preset or an inline JSON mix.

    Shares are normalised to sum to 1. That matters for the comparison presets:
    `no_breadth` drops one kind, and without normalising it would also drop ~5%
    of the rule count, so a latency difference could be attributed to the mix
    when it was really just less work.
    """
    if spec in MIX_PRESETS:
        parsed = dict(MIX_PRESETS[spec])
    else:
        try:
            parsed = json.loads(spec)
        except json.JSONDecodeError as exc:
            raise SystemExit(
                f"--mix must be a preset ({', '.join(MIX_PRESETS)}) or JSON: {exc}"
            ) from exc
        if not isinstance(parsed, dict) or not parsed:
            raise SystemExit("--mix JSON must be a non-empty object of kind -> share")
        unknown = sorted(set(parsed) - set(DEFAULT_MIX))
        if unknown:
            raise SystemExit(f"unknown rule kind(s) {unknown}; known: {sorted(DEFAULT_MIX)}")

    total = sum(float(value) for value in parsed.values())
    if total <= 0:
        raise SystemExit("--mix shares must sum to a positive number")
    return {kind: float(value) / total for kind, value in parsed.items()}


def parse_shard(spec: str) -> Tuple[int, int]:
    try:
        index_text, count_text = spec.split("/", 1)
        index, count = int(index_text), int(count_text)
    except (ValueError, AttributeError) as exc:
        raise SystemExit(f"--shard must be I/N (e.g. 0/4): {exc}") from exc
    if count < 1 or not 0 <= index < count:
        raise SystemExit(f"--shard index must satisfy 0 <= I < N, got {spec!r}")
    return index, count


def aggregate(paths: Sequence[str]) -> int:
    """Merge shard outputs and report the combined measurement.

    Percentiles are recomputed over the union of every shard's raw latencies
    rather than averaged across shards: averaging p95s would hide a slow shard,
    which is precisely the case worth seeing.
    """
    payloads = [json.loads(Path(path).read_text(encoding="utf-8")) for path in paths]
    if not payloads:
        raise SystemExit("--aggregate needs at least one JSON file")

    merged: List[float] = []
    dispatched = writes = errors = 0
    cpu_seconds = 0.0
    offered = 0.0
    ticks = 0.0
    lanes = 0
    rss = 0.0
    duration = 0.0
    events = deliveries = 0
    for payload in payloads:
        for result in payload["results"]:
            merged.extend(result.get("latencies", []))
            dispatched += result["dispatched"]
            writes += result["db_writes"]
            errors += result["errors"]
            events += result.get("events", 0)
            deliveries += result.get("deliveries", 0)
            cpu_seconds += result["cpu_cores"] * result["duration_s"]
            offered += result["target_rate"]
            ticks += result.get("tick_rate_achieved", 0.0) * result["duration_s"]
            lanes += result["concurrency"]
            rss += result["rss_mb"]
            duration = max(duration, result["duration_s"])

    p50, p95, p99 = _percentiles(merged)
    combined = RunResult(
        rate_achieved=dispatched / duration if duration else 0.0,
        tick_rate_achieved=ticks / duration if duration else 0.0,
        dispatched=dispatched,
        errors=errors,
        p50_ms=p50,
        p95_ms=p95,
        p99_ms=p99,
        duration_s=duration,
        db_writes=writes,
        events=events,
        deliveries=deliveries,
        rss_mb=rss,
        cpu_cores=cpu_seconds / duration if duration else 0.0,
        target_rate=offered,
        concurrency=lanes,
        latencies=merged,
    )

    options = Options(**payloads[0]["options"])
    print()
    print(f"aggregated {len(payloads)} shard(s)")
    report(options, [combined], certified=False)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.aggregate:
        return aggregate(args.aggregate)
    if not args.database_url:
        raise SystemExit("--database-url is required unless --aggregate is used")
    mix = resolve_mix(args.mix)
    shard_index, shard_count = parse_shard(args.shard)
    options = Options(
        database_url=args.database_url,
        symbols=args.symbols,
        rules=args.rules,
        rate=args.rate,
        duration_s=args.duration_s,
        concurrency=args.concurrency,
        owner=args.owner,
        target_lag_p95_ms=args.target_lag_p95_ms,
        ramp=args.ramp,
        lane_sweep=args.lane_sweep,
        mix=mix,
        shard_index=shard_index,
        shard_count=shard_count,
        redis_url=args.redis_url,
        keep=args.keep,
        json_out=args.json_out,
    )
    if shard_count > 1:
        # A shard MUST have its own database. EvaluationWorker.start() claims
        # ownership of EVERY active subscription it can see, so shards sharing a
        # database fight over the same fence rows: the losers get fenced out
        # (measuring fencing rather than capacity) and concurrent first claims
        # collide. Requiring the placeholder makes that impossible to get wrong.
        if "{shard}" not in options.database_url:
            raise SystemExit(
                "sharding needs a per-shard database: put '{shard}' in --database-url, "
                "e.g. postgresql+psycopg2://user:pw@host:5432/kite_bench_{shard}. "
                "A worker claims ownership of every active subscription in the database "
                "it connects to, so shards sharing one database measure the ownership "
                "fence (and its races) instead of evaluation capacity."
            )
        options.database_url = options.database_url.format(shard=shard_index)
        # Each shard needs its own owner namespace too, for the same reason.
        options.owner = f"{options.owner}-s{shard_index}"
        # `--rate` is the TOTAL offered rate across all shards, so the aggregate
        # reads as one workload rather than as N independent ones.
        options.rate = options.rate / shard_count

    if options.database_url.startswith("sqlite"):
        print(
            "refusing to run: this benchmark measures write behaviour, and SQLite "
            "would certify a capacity the production database does not have.",
            file=sys.stderr,
        )
        return 2

    engine = create_engine(
        options.database_url,
        future=True,
        # Each dispatch lane takes a connection for the length of a dispatch, so
        # the pool must exceed the lane count or the benchmark measures pool
        # starvation instead of evaluation capacity.
        pool_size=options.concurrency + 4,
        max_overflow=options.concurrency + 4,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    notes = preflight(engine)
    blocking = [note for note in notes if "core tables missing" in note]
    for note in notes:
        print(f"preflight: {note}")
    if blocking:
        print("refusing to run against an unmigrated database.", file=sys.stderr)
        return 2

    try:
        if not options.keep:
            # Clear this owner's previous run first, so a re-run is reproducible
            # instead of colliding on workflow names. `--keep` skips this, which
            # is what makes a kept run inspectable afterwards.
            _cleanup(session_factory, options.owner)
        specs = seed_workload(session_factory, owner=options.owner, options=options, mix=mix)
        print(f"seeded {len(specs)} workflows for owner {options.owner!r}")

        results: List[RunResult] = []
        if options.lane_sweep:
            # Hold the offered rate and vary the lanes. If throughput stops
            # rising while latency does rise, the bottleneck is serialised
            # somewhere; if both scale, it is not yet saturated.
            for lanes in LANE_LADDER:
                result = asyncio.run(
                    measure(session_factory, options, options.rate, concurrency=lanes)
                )
                results.append(result)
                print(
                    f"  {lanes:>3} lanes -> {result.rate_achieved:>7.1f}/s, "
                    f"p50 {result.p50_ms:>7.1f} ms, p95 {result.p95_ms:>7.1f} ms, "
                    f"{result.errors} errors"
                )
            certified = False
        elif options.ramp:
            rate = max(1.0, options.rate / 8.0)
            while rate <= options.rate * 8:
                result = asyncio.run(measure(session_factory, options, rate))
                results.append(result)
                print(f"  rate {rate:>8.1f}/s -> p95 {result.p95_ms:>7.1f} ms, "
                      f"{result.errors} errors")
                if result.p95_ms > options.target_lag_p95_ms or result.errors:
                    break
                rate *= 2
            certified = False
        else:
            results.append(asyncio.run(measure(session_factory, options, options.rate)))
            # Certified only when the run actually covered the documented
            # workload — not when it covered a smaller one successfully.
            certified = options.symbols >= 500 and options.rules >= 5000
        report(options, results, certified=certified)

        if options.json_out:
            Path(options.json_out).write_text(
                json.dumps(
                    {
                        "options": vars(options),
                        "results": [vars(result) for result in results],
                        "certified": certified,
                    },
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )
        return 0
    finally:
        if not options.keep:
            try:
                _cleanup(session_factory, options.owner)
            except Exception as exc:  # noqa: BLE001
                print(f"cleanup warning: {exc}", file=sys.stderr)
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
