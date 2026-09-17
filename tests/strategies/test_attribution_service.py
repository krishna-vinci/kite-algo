"""Unit tests for the durable strategy attribution domain, store and service.

The store/service tests run on SQLite with the shared ``Base`` metadata. The
platform tables the attribution store reads through Core ``text()`` SQL are
``public.``-qualified in the codebase, so the fixture attaches a ``public``
schema and creates the run table there — the same pattern as
``tests/api/test_algo_worker_api.py``. The new attribution tables come from
``Base.metadata.create_all`` (unqualified, portable).

Real-PostgreSQL enforcement (composite FKs, immutability trigger, RESTRICT,
concurrency, per-fact instrument identity across mapping eras) is verified by
the separate disposable-database suite.
"""

from __future__ import annotations

from datetime import datetime, timezone

from backend.strategies.attribution import AttributionFold, PositionKey, TradeFact


def _fact(
    source,
    run,
    qty,
    token=738561,
    product="CNC",
    buy=True,
    effective="2026-09-01T10:00:00+00:00",
    env="live",
    identity=("canonical", "uuid-a"),
):
    kind, key = identity
    return TradeFact(
        source_key=source,
        strategy_run_id=run,
        execution_environment=env,
        instrument_token=token,
        exchange="NSE",
        tradingsymbol="RELIANCE",
        product=product,
        signed_quantity=qty if buy else -qty,
        effective_at=datetime.fromisoformat(effective),
        pinned_generation=None,
    ), PositionKey(env, kind, key, product)


def test_fold_aggregates_across_runs_and_partial_fills():
    facts = [_fact("t:1", "run-sep", 60), _fact("t:2", "run-sep", 40), _fact("t:3", "run-oct", 100)]
    assert list(AttributionFold.fold(facts).values()) == [200]


def test_fold_exit_brings_key_to_absence():
    facts = [_fact("t:1", "run-a", 60), _fact("t:2", "run-a", 40), _fact("t:3", "run-a", 100, buy=False)]
    assert AttributionFold.fold(facts) == {}


def test_fold_products_distinct():
    facts = [_fact("t:1", "run-a", 100, product="CNC"), _fact("t:2", "run-a", 50, product="MIS")]
    assert sorted(AttributionFold.fold(facts).values()) == [50, 100]


def test_fold_deduplicates_identical_source_identity():
    facts = [_fact("t:1", "run-a", 60), _fact("t:1", "run-a", 60)]  # duplicate ingestion
    assert list(AttributionFold.fold(facts).values()) == [60]


def test_paper_and_live_are_separate_books():
    paper, paper_key = _fact("t:1", "run-p", 100, env="paper")
    live, live_key = _fact("t:2", "run-l", 20, env="live")
    positions = AttributionFold.fold([(paper, paper_key), (live, live_key)])
    assert positions == {paper_key: 100, live_key: 20}


def test_paper_buy_cannot_offset_live_sell():
    paper, paper_key = _fact("t:1", "run-p", 100, env="paper")
    live, live_key = _fact("t:2", "run-l", 100, buy=False, env="live")
    positions = AttributionFold.fold([(paper, paper_key), (live, live_key)])
    assert positions == {paper_key: 100, live_key: -100}  # NOT flat


def test_same_unresolved_era_nets_across_dates():
    # Same raw tuple, same catalog era (same generation identity in the era
    # segment): a Monday BUY and a Tuesday SELL net correctly.
    monday, monday_key = _fact("t:1", "run-a", 10, effective="2024-01-08T10:00:00+00:00",
                               identity=("raw", "kite:738561|NSE|RELIANCE|CNC|era=gen:gen-7"))
    tuesday, tuesday_key = _fact("t:2", "run-a", -10, effective="2024-01-09T10:00:00+00:00",
                                 identity=("raw", "kite:738561|NSE|RELIANCE|CNC|era=gen:gen-7"))
    positions = AttributionFold.fold([(monday, monday_key), (tuesday, tuesday_key)])
    assert positions == {}  # same era -> nets; flat keys disappear


def test_distinct_mapping_eras_of_unresolved_identity_never_merge():
    # Same raw tuple, two DISTINCT mapping eras (different generation identity
    # in the era segment): never merged, never netted.
    old, old_key = _fact("t:1", "run-a", 10, effective="2024-01-08T10:00:00+00:00",
                         identity=("raw", "kite:738561|NSE|RELIANCE|CNC|era=interval:gen-1..gen-6"))
    new, new_key = _fact("t:2", "run-a", -10, effective="2026-09-08T10:00:00+00:00",
                         identity=("raw", "kite:738561|NSE|RELIANCE|CNC|era=interval:gen-7.."))
    positions = AttributionFold.fold([(old, old_key), (new, new_key)])
    assert positions == {old_key: 10, new_key: -10}  # distinct eras never merge
