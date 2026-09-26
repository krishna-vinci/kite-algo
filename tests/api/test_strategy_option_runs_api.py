"""B2.6a: the read-only owner API for a strategy's option runs.

Bounded ``httpx`` ASGI transport over a minimal app that mounts the strategies
router and the option-runs router (no lifespan, no background tasks), in the
style of ``tests/api/test_strategies_api.py``.

Pinned properties:

- every route requires an app session, and a worker bearer token does not open it;
- a foreign strategy, or a run bound to a foreign strategy, is 404 - never a
  403 that leaks existence and never a body;
- an incomplete run discovery is reported as ``coverage: "unknown"`` with a
  named reason, never as an empty list under ``coverage: "known"``;
- the list and detail shapes are the contract the options UI is built against;
- refusals are option-structure plans only, newest first, capped at 20.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy import create_engine, event, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from backend.api.routers import strategies as strategies_router  # noqa: E402
from backend.api.routers import strategy_option_runs as option_runs_router  # noqa: E402
from backend.app.auth import AppUser  # noqa: E402
from backend.options.execution.models import OptionRunState  # noqa: E402
from backend.strategies import models  # noqa: F401,E402  (table registration)
from backend.strategies.attribution_models import (  # noqa: E402
    StrategyPlan,
    StrategyPlanOptionRun,
    StrategyProposal,
)
from backend.strategies.models import HostedExecutionRequest  # noqa: E402
from backend.workflows.repository import Base  # noqa: E402

BASE = "/api/strategies"
ACCOUNT = "kite:paper"

SHORT = "NIFTY26NOV22500CE"
HEDGE = "NIFTY26NOV21500PE"
SHORT_ID = "NSE:NIFTY26NOV22500CE"
HEDGE_ID = "NSE:NIFTY26NOV21500PE"
HELD_DIGEST = "sha-held-digest"


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach_public(dbapi_connection, connection_record):
        _ = connection_record
        cursor = dbapi_connection.cursor()
        cursor.execute("ATTACH DATABASE ':memory:' AS public")
        # The protection-owner read is ``public.``-qualified in production code
        # (``OptionProtectionOwnerStore``), so it resolves here through the same
        # ATTACH the other platform tables use.
        cursor.execute(
            """
            CREATE TABLE public.option_protection_owners (
                option_run_id TEXT PRIMARY KEY,
                strategy_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                execution_environment TEXT NOT NULL,
                owner_run_id TEXT,
                owner_epoch INTEGER NOT NULL DEFAULT 1,
                policy_version TEXT NOT NULL,
                policy TEXT NOT NULL DEFAULT '{}',
                action_state TEXT NOT NULL DEFAULT 'none',
                stage_digest TEXT,
                state TEXT NOT NULL DEFAULT 'active',
                released_at TEXT
            )
            """
        )

    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture(autouse=True)
def account_policy(monkeypatch):
    """Authorize exactly the account these fixtures use (default-deny)."""
    monkeypatch.setenv("HOSTED_STRATEGY_ACCOUNT_SCOPES", ACCOUNT)
    yield


class _FakeSnapshot:
    """The scope-derived run discovery, with a caller-chosen coverage verdict."""

    def __init__(self, rows, coverage):
        self._rows = [dict(row) for row in rows]
        self._coverage = dict(coverage)
        self.calls = []

    def option_runs_for_scope(self, **kwargs):
        self.calls.append(dict(kwargs))
        return [dict(row) for row in self._rows], dict(self._coverage)


class _FakeRunStore:
    """The durable run store surface, in memory. A missing id raises, i.e. the
    read is UNREADABLE rather than "the run is empty"."""

    def __init__(self, runs=None):
        self._runs = dict(runs or {})

    def get_run(self, option_run_id):
        if option_run_id not in self._runs:
            raise KeyError(option_run_id)
        return self._runs[option_run_id]


def _app(
    session_factory,
    monkeypatch,
    user,
    *,
    snapshot=None,
    run_store=None,
    owner_store=None,
    options_session_manager=None,
):
    from backend.app import auth as auth_module

    monkeypatch.setattr(auth_module, "get_optional_app_user", lambda _request: user)
    app = FastAPI()
    app.include_router(strategies_router.router, prefix="/api")
    app.include_router(option_runs_router.router, prefix="/api")
    app.dependency_overrides[strategies_router._strategies_db] = lambda: session_factory
    app.dependency_overrides[option_runs_router._option_runs_db] = lambda: (
        session_factory
    )
    if snapshot is not None:
        app.state.owned_work_snapshot_service = snapshot
    if run_store is not None:
        app.state.option_run_store = run_store
    if owner_store is not None:
        app.state.option_protection_owner_store = owner_store
    if options_session_manager is not None:
        app.state.options_session_manager = options_session_manager
    return app


def _client(session_factory, monkeypatch, username="admin", **kwargs):
    user = AppUser(username=username, role="admin") if username else None
    app = _app(session_factory, monkeypatch, user, **kwargs)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _create(client, name="options-ui"):
    response = await client.post(
        BASE,
        json={
            "name": name,
            "execution_mode": "paper",
            "job_kind": "finite",
            "account_scope": ACCOUNT,
            "max_duration_s": 21600,
            "progress_deadline_s": 600,
            "stale_exit_policy": "exit_on_worker_stale",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["strategy_id"]


def _frozen_leg(identity, symbol, side, role, quantity):
    return {
        "instrument_id": identity,
        "tradingsymbol": symbol,
        "broker_symbol": symbol,
        "side": side,
        "role": role,
        "ratio": 1,
        "quantity": quantity,
        "signed_quantity": quantity if side == "BUY" else -quantity,
        "product": "NRML",
        "expiry": "2026-11-26",
    }


def _resolved_plan(*, structure_digest=HELD_DIGEST, expiry_policy="exit_before_cutoff"):
    return {
        "target_kind": "option_structure",
        "underlying": "NIFTY",
        "expiry": "2026-11-26",
        "product": "NRML",
        "structure_digest": structure_digest,
        "expiry_policy": expiry_policy,
        "protection_policy": {"short_roll": {"enabled": True}},
        "max_loss": {"amount": 5000},
        "option_run": {"phase": "entry", "option_run_id": None},
        "legs": [
            _frozen_leg(SHORT_ID, SHORT, "SELL", "short", 150),
            _frozen_leg(HEDGE_ID, HEDGE, "BUY", "hedge", 150),
        ],
    }


def _seed_plan(
    session_factory,
    *,
    strategy_id,
    plan_id,
    option_run_id,
    phase,
    created_at,
    environment="paper",
    resolved=None,
):
    session = session_factory()
    try:
        session.add(
            StrategyProposal(
                proposal_id=f"prop-{plan_id}",
                strategy_id=strategy_id,
                account_id=ACCOUNT,
                evaluation_id=f"eval-{plan_id}",
                evaluation_kind="run_now",
                strategy_run_id="run-1",
                target_kind="option_structure",
                payload={},
                payload_sha256="payload-sha",
                status="validated",
            )
        )
        session.add(
            StrategyPlan(
                plan_id=plan_id,
                proposal_id=f"prop-{plan_id}",
                strategy_id=strategy_id,
                account_id=ACCOUNT,
                plan_kind="option_structure",
                plan_hash=f"hash-{plan_id}",
                logical_plan={},
                resolved_plan=dict(
                    resolved if resolved is not None else _resolved_plan()
                ),
                pinned_catalog_generation="gen-1",
                created_at=created_at,
            )
        )
        session.add(
            StrategyPlanOptionRun(
                plan_id=plan_id,
                option_run_id=option_run_id,
                worker_run_id="worker-1",
                strategy_id=strategy_id,
                account_id=ACCOUNT,
                execution_environment=environment,
                phase=phase,
                created_at=created_at,
            )
        )
        session.commit()
    finally:
        session.close()


def _seed_request(
    session_factory,
    *,
    strategy_id,
    plan_id,
    request_id,
    created_at,
    refusal_code="OPTION_ADJUSTMENT_STALE_BASIS",
    stage="request",
):
    session = session_factory()
    try:
        session.add(
            HostedExecutionRequest(
                request_id=request_id,
                owner_id="app:admin",
                strategy_id=strategy_id,
                canonical_strategy_id=strategy_id,
                account_id=ACCOUNT,
                execution_environment="paper",
                strategy_run_id="run-1",
                version_id="version-1",
                source_sha256="source-sha",
                policy_hash="policy-hash",
                plan_id=plan_id,
                plan_hash=f"hash-{plan_id}",
                authorization_mode="approval_based",
                status="refused",
                refusal_code=refusal_code,
                refusal_detail={"stage": stage, "checked_at": created_at.isoformat()},
                idempotency_key=f"key-{request_id}",
                request_hash=f"request-hash-{request_id}",
                created_at=created_at,
                updated_at=created_at,
            )
        )
        session.commit()
    finally:
        session.close()


def _durable_run(*, metrics=None):
    """The durable run with its OWN confirmed fills (both legs entered flat)."""
    trades = [
        {
            "leg_id": "plan-entry:1",
            "tradingsymbol": SHORT,
            "transaction_type": "SELL",
            "quantity": 150,
        },
        {
            "leg_id": "plan-entry:2",
            "tradingsymbol": HEDGE,
            "transaction_type": "BUY",
            "quantity": 150,
        },
    ]
    metadata = {"worker_run_id": "worker-1", "account_id": ACCOUNT}
    if metrics is not None:
        metadata["protection_metrics"] = dict(metrics)
    return OptionRunState(
        strategy_run_id="opt_run_1",
        strategy_name="iron_condor",
        product="NRML",
        status="entered",
        legs=[
            {
                "leg_id": "plan-entry:1",
                "tradingsymbol": SHORT,
                "transaction_type": "SELL",
                "quantity": 150,
                "metadata": {"instrument_id": SHORT_ID, "role": "entry", "ratio": 1},
            },
            {
                "leg_id": "plan-entry:2",
                "tradingsymbol": HEDGE,
                "transaction_type": "BUY",
                "quantity": 150,
                "metadata": {"instrument_id": HEDGE_ID, "role": "entry", "ratio": 1},
            },
        ],
        trades=trades,
        metadata=metadata,
    )


def _run_row(*, status="entered", coverage="known", structure_digest=HELD_DIGEST):
    return {
        "option_run_id": "opt_run_1",
        "plan_ids": ["plan-entry", "plan-exit"],
        "originating_plan_id": "plan-entry",
        "originating_phase": "entry",
        "phase": "entry",
        "worker_run_id": "worker-1",
        "underlying": "NIFTY",
        "expiry": "2026-11-26",
        "structure_id": "iron-condor-1",
        "structure_digest": structure_digest,
        "structure_generation": 2,
        "expiry_policy": "exit_before_cutoff",
        "product": "NRML",
        "status": status,
        "legs": [
            {
                "leg_id": "plan-entry:1",
                "tradingsymbol": SHORT,
                "transaction_type": "SELL",
                "quantity": 150,
                "metadata": {"instrument_id": SHORT_ID},
            },
            {
                "leg_id": "plan-entry:2",
                "tradingsymbol": HEDGE,
                "transaction_type": "BUY",
                "quantity": 150,
                "metadata": {"instrument_id": HEDGE_ID},
            },
        ],
        "completed_legs": ["plan-entry:1", "plan-entry:2"],
        "pending_legs": [],
        "failed_legs": [],
        "protective_exit_unresolved": False,
        "coverage": coverage,
    }


def _seed_edges(session_factory, strategy_id):
    """An entry edge, then a later exit edge on the same run."""
    _seed_plan(
        session_factory,
        strategy_id=strategy_id,
        plan_id="plan-entry",
        option_run_id="opt_run_1",
        phase="entry",
        created_at=datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc),
    )
    _seed_plan(
        session_factory,
        strategy_id=strategy_id,
        plan_id="plan-exit",
        option_run_id="opt_run_1",
        phase="exit",
        created_at=datetime(2026, 9, 25, 11, 0, tzinfo=timezone.utc),
        resolved=_resolved_plan(structure_digest="sha-exit-digest"),
    )


class _FakeOwnerStore:
    """The protection-owner store surface, with a caller-chosen failure mode."""

    def __init__(self, row=None, error=None):
        self._row = dict(row) if row else None
        self._error = error
        self.reads = []

    def read(self, option_run_id):
        self.reads.append(str(option_run_id))
        if self._error is not None:
            raise self._error
        return dict(self._row) if self._row else None


def _seed_owner_row(
    session_factory,
    *,
    option_run_id="opt_run_1",
    owner_run_id="worker-1",
    state="active",
    action_state="none",
    policy_version="policy-v1",
    owner_epoch=3,
):
    session = session_factory()
    try:
        session.execute(
            text(
                "INSERT OR REPLACE INTO public.option_protection_owners "
                "(option_run_id, strategy_id, account_id, execution_environment, "
                " owner_run_id, owner_epoch, policy_version, policy, action_state, "
                " state) VALUES (:run, 'strategy', :account, 'paper', :owner, "
                " :epoch, :policy_version, '{}', :action_state, :state)"
            ),
            {
                "run": option_run_id,
                "account": ACCOUNT,
                "owner": owner_run_id,
                "epoch": owner_epoch,
                "policy_version": policy_version,
                "action_state": action_state,
                "state": state,
            },
        )
        session.commit()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# authentication / authorization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_route_requires_a_session(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch, username=None) as client:
        assert (await client.get(f"{BASE}/stg_1/option-runs")).status_code == 401
        assert (
            await client.get(f"{BASE}/stg_1/option-runs/opt_run_1")
        ).status_code == 401


@pytest.mark.asyncio
async def test_a_worker_bearer_token_does_not_open_the_surface(
    session_factory, monkeypatch
):
    async with _client(session_factory, monkeypatch, username=None) as client:
        response = await client.get(
            f"{BASE}/stg_1/option-runs", headers={"Authorization": "Bearer kwa_x"}
        )
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_a_foreign_strategy_or_run_is_404(session_factory, monkeypatch):
    snapshot = _FakeSnapshot([], {"coverage": "known", "reason": ""})
    async with _client(session_factory, monkeypatch, snapshot=snapshot) as client:
        strategy_id = await _create(client)
    _seed_edges(session_factory, strategy_id)

    async with _client(
        session_factory, monkeypatch, username="someone-else", snapshot=snapshot
    ) as client:
        assert (
            await client.get(f"{BASE}/{strategy_id}/option-runs")
        ).status_code == 404
        assert (
            await client.get(f"{BASE}/{strategy_id}/option-runs/opt_run_1")
        ).status_code == 404

    async with _client(session_factory, monkeypatch, snapshot=snapshot) as client:
        # Owned strategy, but a run it has no binding to: still a 404, so a
        # caller cannot probe which option run ids exist.
        response = await client.get(f"{BASE}/{strategy_id}/option-runs/opt_run_other")
        assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_coverage_is_unknown_and_never_a_known_empty_list(
    session_factory, monkeypatch
):
    unknown = _FakeSnapshot(
        [],
        {
            "coverage": "unknown",
            "reason": "option_run_state_read_failed",
            "count": 0,
            "truncated": False,
        },
    )
    async with _client(session_factory, monkeypatch, snapshot=unknown) as client:
        strategy_id = await _create(client)
        body = (await client.get(f"{BASE}/{strategy_id}/option-runs")).json()

    assert body == {
        "strategy_id": strategy_id,
        "coverage": "unknown",
        "coverage_reason": "option_run_state_read_failed",
        "runs": [],
    }
    # The scope the route asked for is the strategy's OWN account + environment,
    # derived server-side from the strategy the owner just created.
    assert unknown.calls == [
        {"account_id": ACCOUNT, "strategy_id": strategy_id, "environment": "paper"}
    ]


@pytest.mark.asyncio
async def test_a_complete_read_of_no_runs_is_known_and_empty(
    session_factory, monkeypatch
):
    known = _FakeSnapshot([], {"coverage": "known", "reason": "", "count": 0})
    async with _client(session_factory, monkeypatch, snapshot=known) as client:
        strategy_id = await _create(client)
        body = (await client.get(f"{BASE}/{strategy_id}/option-runs")).json()
    assert body["coverage"] == "known"
    assert body["coverage_reason"] == ""
    assert body["runs"] == []


@pytest.mark.asyncio
async def test_an_owned_run_with_unreadable_state_is_unknown_not_404(
    session_factory, monkeypatch
):
    empty = _FakeSnapshot(
        [], {"coverage": "unknown", "reason": "option_run_state_unavailable"}
    )
    async with _client(session_factory, monkeypatch, snapshot=empty) as client:
        strategy_id = await _create(client)
        _seed_edges(session_factory, strategy_id)
        response = await client.get(f"{BASE}/{strategy_id}/option-runs/opt_run_1")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run"]["option_run_id"] == "opt_run_1"
    assert body["run"]["status"] == "unknown"
    assert body["run"]["coverage"] == "unknown"
    # The binding edge proves ownership and the frozen plan still describes the
    # INTENDED structure, but no own quantity is claimed: unreadable evidence
    # never becomes a zero, and no leg reads "flat".
    assert [
        (leg["leg_id"], leg["tradingsymbol"], leg["own_open_quantity"], leg["state"])
        for leg in body["run"]["legs"]
    ] == [
        ("plan-entry:1", SHORT, None, "pending"),
        ("plan-entry:2", HEDGE, None, "pending"),
    ]
    assert body["pnl"] == {
        "available": False,
        "reason": "option_run_unreadable",
        "premium": None,
        "mtm": None,
    }
    assert body["greeks"]["available"] is False
    assert body["greeks"]["reason"] == "option_run_unreadable"


# ---------------------------------------------------------------------------
# list shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_shape_matches_the_contract(session_factory, monkeypatch):
    snapshot = _FakeSnapshot([_run_row()], {"coverage": "known", "reason": ""})
    runs = {
        "opt_run_1": _durable_run(
            metrics={"combined_premium": 4321.5, "strategy_mtm": -120.25}
        )
    }
    async with _client(
        session_factory, monkeypatch, snapshot=snapshot, run_store=_FakeRunStore(runs)
    ) as client:
        strategy_id = await _create(client)
        _seed_edges(session_factory, strategy_id)
        body = (await client.get(f"{BASE}/{strategy_id}/option-runs")).json()

    assert set(body) == {"strategy_id", "coverage", "coverage_reason", "runs"}
    assert body["coverage"] == "known"
    run = body["runs"][0]
    assert run == {
        "option_run_id": "opt_run_1",
        "status": "entered",
        "structure_generation": 2,
        "structure_digest": HELD_DIGEST,
        "underlying": "NIFTY",
        "expiry": "2026-11-26",
        "product": "NRML",
        "protective_exit_unresolved": False,
        "coverage": "known",
        "legs": [
            {
                "leg_id": "plan-entry:1",
                "tradingsymbol": SHORT,
                "side": "SELL",
                "role": "short",
                "ratio": 1,
                "quantity": 150,
                "own_open_quantity": -150,
                "state": "open",
            },
            {
                "leg_id": "plan-entry:2",
                "tradingsymbol": HEDGE,
                "side": "BUY",
                "role": "hedge",
                "ratio": 1,
                "quantity": 150,
                "own_open_quantity": 150,
                "state": "open",
            },
        ],
        "repairable": False,
        "protection_owner": None,
    }


@pytest.mark.asyncio
async def test_repairable_is_true_for_a_partial_run_and_own_open_is_null_when_unreadable(
    session_factory, monkeypatch
):
    snapshot = _FakeSnapshot(
        [_run_row(status="partial_entry")], {"coverage": "known", "reason": ""}
    )
    # No run store at all: the durable run cannot be read, so every own quantity
    # is null and the leg state never claims "flat".
    async with _client(
        session_factory, monkeypatch, snapshot=snapshot, run_store=_FakeRunStore()
    ) as client:
        strategy_id = await _create(client)
        _seed_edges(session_factory, strategy_id)
        body = (await client.get(f"{BASE}/{strategy_id}/option-runs")).json()

    run = body["runs"][0]
    assert run["repairable"] is True
    assert [leg["own_open_quantity"] for leg in run["legs"]] == [None, None]
    assert [leg["state"] for leg in run["legs"]] == ["open", "open"]


# ---------------------------------------------------------------------------
# detail shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detail_shape_matches_the_contract(session_factory, monkeypatch):
    snapshot = _FakeSnapshot([_run_row()], {"coverage": "known", "reason": ""})
    runs = {
        "opt_run_1": _durable_run(
            metrics={"combined_premium": 4321.5, "strategy_mtm": -120.25}
        )
    }
    async with _client(
        session_factory, monkeypatch, snapshot=snapshot, run_store=_FakeRunStore(runs)
    ) as client:
        strategy_id = await _create(client)
        _seed_edges(session_factory, strategy_id)
        # One refusal on this strategy's option-structure entry plan, and one on
        # a non-option plan that must never leak into the window.
        _seed_request(
            session_factory,
            strategy_id=strategy_id,
            plan_id="plan-entry",
            request_id="req-refused",
            created_at=datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc),
        )
        body = (await client.get(f"{BASE}/{strategy_id}/option-runs/opt_run_1")).json()

    assert set(body) == {"run", "edges", "frozen", "refusals", "greeks", "pnl"}
    assert body["run"]["option_run_id"] == "opt_run_1"
    assert body["run"]["structure_digest"] == HELD_DIGEST
    # Edges are oldest first.
    assert [(edge["plan_id"], edge["phase"]) for edge in body["edges"]] == [
        ("plan-entry", "entry"),
        ("plan-exit", "exit"),
    ]
    assert all(edge["created_at"] for edge in body["edges"])
    # Frozen policies come from the plans; values that were never frozen are null.
    assert body["frozen"] == {
        "protection_policy": {"short_roll": {"enabled": True}},
        "max_loss": {"amount": 5000},
        "expiry_policy": "exit_before_cutoff",
    }
    assert len(body["refusals"]) == 1
    refusal = body["refusals"][0]
    # SQLite drops the offset on the round trip, so the timestamp is compared by
    # instant rather than by its exact rendering.
    assert refusal["at"].startswith("2026-09-25T12:00:00")
    assert {key: value for key, value in refusal.items() if key != "at"} == {
        "request_id": "req-refused",
        "plan_id": "plan-entry",
        "refusal_code": "OPTION_ADJUSTMENT_STALE_BASIS",
        "stage": "request",
        "detail": {"stage": "request", "checked_at": "2026-09-25T12:00:00+00:00"},
    }
    # No live options session is wired into this test app, so greeks stay a
    # named absence rather than a guess.
    assert body["greeks"] == {
        "available": False,
        "reason": "no_option_session",
        "delta": None,
        "gamma": None,
        "theta": None,
        "vega": None,
    }
    # Premium / MTM are the run's OWN recorded protection metrics, verbatim.
    assert body["pnl"] == {
        "available": True,
        "reason": "",
        "premium": 4321.5,
        "mtm": -120.25,
    }


class _FakeOptionsSessionManager:
    """A minimal ``OptionsSessionManager`` surface: one fixed snapshot."""

    def __init__(self, snapshot):
        self._snapshot = snapshot

    def get_snapshot(self, _underlying):
        return self._snapshot


@pytest.mark.asyncio
async def test_greeks_are_the_signed_sum_of_the_open_legs_contracts(
    session_factory, monkeypatch
):
    snapshot = _FakeSnapshot([_run_row()], {"coverage": "known", "reason": ""})
    runs = {"opt_run_1": _durable_run()}
    now_iso = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc).isoformat()
    manager = _FakeOptionsSessionManager(
        {
            "expiries": ["2026-11-26"],
            "per_expiry": {
                "2026-11-26": {
                    "rows": [
                        {
                            "strike": 22500,
                            "ce": {
                                "token": 1,
                                "tsym": SHORT,
                                "delta": -0.6,
                                "gamma": 0.001,
                                "theta": -2.0,
                                "vega": 8.0,
                                "updated_at": now_iso,
                            },
                        },
                        {
                            "strike": 21500,
                            "pe": {
                                "token": 2,
                                "tsym": HEDGE,
                                "delta": 0.35,
                                "gamma": 0.0009,
                                "theta": -1.4,
                                "vega": 6.0,
                                "updated_at": now_iso,
                            },
                        },
                    ],
                },
            },
        }
    )
    async with _client(
        session_factory,
        monkeypatch,
        snapshot=snapshot,
        run_store=_FakeRunStore(runs),
        options_session_manager=manager,
    ) as client:
        strategy_id = await _create(client)
        _seed_edges(session_factory, strategy_id)
        body = (await client.get(f"{BASE}/{strategy_id}/option-runs/opt_run_1")).json()

    # SHORT is own_open -150 (sold), HEDGE is own_open +150 (bought).
    assert body["greeks"] == {
        "available": True,
        "reason": "",
        "delta": -150.0 * -0.6 + 150.0 * 0.35,
        "gamma": -150.0 * 0.001 + 150.0 * 0.0009,
        "theta": -150.0 * -2.0 + 150.0 * -1.4,
        "vega": -150.0 * 8.0 + 150.0 * 6.0,
    }


@pytest.mark.asyncio
async def test_premium_and_mtm_are_a_named_absence_without_metrics(
    session_factory, monkeypatch
):
    snapshot = _FakeSnapshot([_run_row()], {"coverage": "known", "reason": ""})
    runs = {"opt_run_1": _durable_run()}
    async with _client(
        session_factory, monkeypatch, snapshot=snapshot, run_store=_FakeRunStore(runs)
    ) as client:
        strategy_id = await _create(client)
        _seed_edges(session_factory, strategy_id)
        body = (await client.get(f"{BASE}/{strategy_id}/option-runs/opt_run_1")).json()
    assert body["pnl"] == {
        "available": False,
        "reason": "no_reusable_read",
        "premium": None,
        "mtm": None,
    }


@pytest.mark.asyncio
async def test_frozen_policies_are_null_when_the_plan_froze_none(
    session_factory, monkeypatch
):
    resolved = _resolved_plan()
    resolved.pop("protection_policy")
    resolved.pop("max_loss")
    resolved["expiry_policy"] = None
    snapshot = _FakeSnapshot([_run_row()], {"coverage": "known", "reason": ""})
    async with _client(
        session_factory, monkeypatch, snapshot=snapshot, run_store=_FakeRunStore()
    ) as client:
        strategy_id = await _create(client)
        _seed_plan(
            session_factory,
            strategy_id=strategy_id,
            plan_id="plan-entry",
            option_run_id="opt_run_1",
            phase="entry",
            created_at=datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc),
            resolved=resolved,
        )
        body = (await client.get(f"{BASE}/{strategy_id}/option-runs/opt_run_1")).json()
    assert body["frozen"] == {
        "protection_policy": None,
        "max_loss": None,
        "expiry_policy": None,
    }


@pytest.mark.asyncio
async def test_refusals_are_option_structure_only_newest_first_and_capped(
    session_factory, monkeypatch
):
    snapshot = _FakeSnapshot([_run_row()], {"coverage": "known", "reason": ""})
    async with _client(
        session_factory,
        monkeypatch,
        snapshot=snapshot,
        run_store=_FakeRunStore({"opt_run_1": _durable_run()}),
    ) as client:
        strategy_id = await _create(client)
        _seed_edges(session_factory, strategy_id)
        # A plan that is NOT an option structure: its refusal must never appear.
        session = session_factory()
        try:
            session.add(
                StrategyProposal(
                    proposal_id="prop-fut",
                    strategy_id=strategy_id,
                    account_id=ACCOUNT,
                    evaluation_id="eval-fut",
                    evaluation_kind="run_now",
                    strategy_run_id="run-2",
                    target_kind="target_futures",
                    payload={},
                    payload_sha256="fut-sha",
                    status="validated",
                )
            )
            session.add(
                StrategyPlan(
                    plan_id="plan-fut",
                    proposal_id="prop-fut",
                    strategy_id=strategy_id,
                    account_id=ACCOUNT,
                    plan_kind="target_futures",
                    plan_hash="fut-hash",
                    logical_plan={},
                    resolved_plan={"target_kind": "target_futures"},
                    pinned_catalog_generation="gen-1",
                )
            )
            session.commit()
        finally:
            session.close()
        _seed_request(
            session_factory,
            strategy_id=strategy_id,
            plan_id="plan-fut",
            request_id="req-futures",
            created_at=datetime(2026, 9, 25, 23, 0, tzinfo=timezone.utc),
        )
        # 25 option-structure refusals, one per minute.
        for minute in range(25):
            _seed_request(
                session_factory,
                strategy_id=strategy_id,
                plan_id="plan-entry",
                request_id=f"req-{minute:02d}",
                created_at=datetime(2026, 9, 25, 13, minute, tzinfo=timezone.utc),
                refusal_code=f"REFUSAL_{minute:02d}",
                stage="dispatch_boundary",
            )
        body = (await client.get(f"{BASE}/{strategy_id}/option-runs/opt_run_1")).json()

    assert len(body["refusals"]) == 20
    codes = [row["refusal_code"] for row in body["refusals"]]
    # Newest first, and the 5 oldest are dropped by the cap.
    assert codes == [f"REFUSAL_{minute:02d}" for minute in range(24, 4, -1)]
    assert all(row["plan_id"] == "plan-entry" for row in body["refusals"])
    assert all(row["stage"] == "dispatch_boundary" for row in body["refusals"])


@pytest.mark.asyncio
async def test_an_explicit_environment_query_cannot_widen_the_scope(
    session_factory, monkeypatch
):
    snapshot = _FakeSnapshot([], {"coverage": "known", "reason": ""})
    async with _client(session_factory, monkeypatch, snapshot=snapshot) as client:
        strategy_id = await _create(client)
        assert (
            await client.get(f"{BASE}/{strategy_id}/option-runs?environment=nonsense")
        ).status_code == 422
        assert (
            await client.get(f"{BASE}/{strategy_id}/option-runs?environment=live")
        ).status_code == 200
    assert snapshot.calls[-1]["environment"] == "live"
    assert snapshot.calls[-1]["account_id"] == ACCOUNT


# ---------------------------------------------------------------------------
# protection owner (B2.4 read, surfaced by B2.6b S3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_protection_owner_is_reported_on_the_list_and_the_detail(
    session_factory, monkeypatch
):
    """The read the Options UI needs to name who owns a structure's protection."""
    snapshot = _FakeSnapshot([_run_row()], {"coverage": "known", "reason": ""})
    store = _FakeRunStore({"opt_run_1": _durable_run()})
    async with _client(
        session_factory, monkeypatch, snapshot=snapshot, run_store=store
    ) as client:
        strategy_id = await _create(client)
        _seed_edges(session_factory, strategy_id)
        _seed_owner_row(session_factory)

        listed = (await client.get(f"{BASE}/{strategy_id}/option-runs")).json()
        detail = (
            await client.get(f"{BASE}/{strategy_id}/option-runs/opt_run_1")
        ).json()

    expected = {
        "owner_run_id": "worker-1",
        "owner_epoch": 3,
        "state": "active",
        "policy_version": "policy-v1",
        "action_state": "none",
    }
    assert listed["runs"][0]["protection_owner"] == expected
    assert detail["run"]["protection_owner"] == expected


@pytest.mark.asyncio
async def test_an_unreadable_protection_owner_row_is_unknown_never_null(
    session_factory, monkeypatch
):
    """An unreadable row is a warning, not "this run has no owner"."""
    snapshot = _FakeSnapshot([_run_row()], {"coverage": "known", "reason": ""})
    store = _FakeRunStore({"opt_run_1": _durable_run()})
    owner_store = _FakeOwnerStore(error=RuntimeError("owner read failed"))
    async with _client(
        session_factory,
        monkeypatch,
        snapshot=snapshot,
        run_store=store,
        owner_store=owner_store,
    ) as client:
        strategy_id = await _create(client)
        _seed_edges(session_factory, strategy_id)
        listed = (await client.get(f"{BASE}/{strategy_id}/option-runs")).json()
        detail = (
            await client.get(f"{BASE}/{strategy_id}/option-runs/opt_run_1")
        ).json()

    assert listed["runs"][0]["protection_owner"] == {"state": "unknown"}
    assert detail["run"]["protection_owner"] == {"state": "unknown"}
    assert owner_store.reads == ["opt_run_1", "opt_run_1"]
