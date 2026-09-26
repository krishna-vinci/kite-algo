"""Governed dependent release, driven through the REAL release pass.

The direct-helper test in ``tests/api/test_hosted_execution_requests.py`` proves
the authority *decision*. This file proves the wiring the correction bundle was
missing: the production ``LivePlanExecutor.release_sequence`` - the exact method
the live outcome consumer runs in production - is what releases (or refuses) a
withheld dependent leg, and a grant revocation or a mode change BETWEEN the two
legs prevents the second submission.

Order of operations (nothing is hand-inserted; the operator, supervisor and child
routes are the production ones):

1. an operator-prepared live attempt mints the real child credential;
2. a first plan opens an attributed INFY book (operator route);
3. the operator selects ``autonomous`` and issues a version/policy-bound grant;
4. the CHILD asks for execution of a two-leg rebalance over
   ``POST /api/algo-workers/worker/executions``;
5. the governed request is claimed and dispatched: leg 1 (SELL INFY) is sent to
   the FAKE BROKER and leg 2 (BUY RELIANCE) is materialized ``withheld``;
6. leg 1's fill is ingested, and then - depending on the scenario - the grant is
   revoked, the mode is switched back to approval-based, or nothing changes;
7. ``release_sequence`` runs. Only the un-modified autonomous grant may submit
   leg 2.

Only the broker intent handler and the quote/margin readers are faked. Every
other component (authorization service, execution request, dispatcher pipeline,
live adapter, sequence store, per-step claims, reservation ledger, barrier) is
production code. Disposable PostgreSQL 15433 only.

    RECONCILIATION_PG_ADMIN='postgresql://postgres:testonly@127.0.0.1:15433/postgres' \\
        .venv/bin/pytest tests/integration/test_hosted_execution_governed_release_postgres.py -q

Run this file in its own pytest process (a sibling suite installs a fake
``psycopg2`` at import, which breaks these PostgreSQL fixtures).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

# The suite drives its own clock in a disposable database with no imported NSE
# calendar, so the market session is supplied as EVIDENCE through the production
# seam rather than guessed (see tests/support/market_session_stub.py).
from tests.support.market_session_stub import open_market_session  # noqa: F401

# The disposable-database fixture, the environment switches and the fake broker
# are shared with the Phase 2A acceptance suite rather than copied, so the two
# cannot drift.
from tests.integration.test_hosted_live_phase2a_routes_postgres import (  # noqa: F401
    INFY,
    INFY_TOKEN,
    RELIANCE,
    _Env,
    _FakeBroker,
    _claim,
    _execution,
    _ingest_fill,
    _open_position,
    _operator_client,
    _prepare_live_attempt,
    _seed_catalog,
    _submit_proposal,
)
from tests.integration.test_hosted_live_phase2a_routes_postgres import (  # noqa: F401
    live_env as _phase2a_live_env,
)
from tests.integration.test_hosted_live_phase2a_routes_postgres import (  # noqa: F401
    pg as _phase2a_pg,
)


@pytest.fixture()
def pg(request):
    """The disposable-database fixture, reused from the Phase 2A suite."""
    return request.getfixturevalue("_phase2a_pg")


@pytest.fixture()
def live_env(request):
    """The live deployment switches/session fixture, reused from Phase 2A."""
    return request.getfixturevalue("_phase2a_live_env")


@pytest.fixture(autouse=True)
def _funds_boundary(monkeypatch):
    """Stand in for the broker margin/funds read, exactly as Phase 2A does."""
    from backend.api.routers import strategies as strategies_module

    monkeypatch.setattr(
        strategies_module,
        "_live_margin_evidence",
        lambda _scope, _plan: {
            "usable": 5_000_000.0,
            "as_of": datetime.now(timezone.utc).isoformat(),
        },
    )


def _include_execution_routes(app) -> None:
    """Mount the governed execution-request router on the shared app."""
    from backend.api.routers import worker_executions

    app.include_router(worker_executions.router, prefix="/api")


def _latest_version_id(factory, strategy_id: str) -> str:
    with factory() as session:
        return str(
            session.execute(
                text(
                    "SELECT id FROM public.hosted_strategy_versions "
                    "WHERE strategy_id = :sid ORDER BY version DESC LIMIT 1"
                ),
                {"sid": str(strategy_id)},
            ).scalar()
        )


def _governed_service(env):
    """The production request/dispatch service over the FAKE broker executor."""
    from backend.strategies.execution_requests import ExecutionRequestService
    from backend.strategies.plan_pipeline import PlanExecutionPipeline

    # The broker margin READ is the same fake the operator route uses; the
    # pipeline defaults to the real reader, which needs a live broker session.
    pipeline = PlanExecutionPipeline(
        env.factory,
        live_executor_factory=lambda: env.executor,
        margin_reader=lambda _account, _plan: {
            "usable": 5_000_000.0,
            "as_of": datetime.now(timezone.utc),
        },
    )
    return ExecutionRequestService(env.factory, pipeline=pipeline)


async def _open_autonomous_grant(client, env, attempt):
    """Operator: select autonomous mode and issue a bound grant."""
    version_id = _latest_version_id(env.factory, attempt["strategy_id"])
    mode = await client.put(
        f"/api/strategies/{attempt['strategy_id']}/authorization",
        json={"mode": "autonomous"},
    )
    assert mode.status_code < 400, mode.text
    grant = await client.post(
        f"/api/strategies/{attempt['strategy_id']}/authorization/grants",
        json={
            "idempotency_key": f"gov-release-{uuid.uuid4().hex[:8]}",
            "version_id": version_id,
            "execution_environment": "live",
        },
    )
    assert grant.status_code < 400, grant.text
    assert grant.json()["status"] == "active", grant.text
    return grant.json()


async def _freeze_rebalance(client, env, attempt, revision_id):
    """Freeze the two-leg rebalance plan WITHOUT executing it.

    The basket is NON-CNC (MIS product) on purpose: it never enters the C1.1
    staged funding lane, whose S1 gate (until S2 lands) withholds a staged
    dependent buy. This suite is about the GOVERNED authority on a dependent
    release, so it must exercise a release that S1 still performs.
    """
    response = await _submit_proposal(
        client,
        attempt,
        {
            "target_kind": "target_weights",
            "payload": {
                "universe_revision_id": revision_id,
                "product": "MIS",
                "target_weights": {RELIANCE: 0.04, INFY: 0.0},
                "reference_prices": {RELIANCE: 1500.0, INFY: 1500.0},
            },
        },
        account_scope=env.account_scope,
    )
    assert response.status_code < 400, response.text
    return str(response.json()["plan"]["plan_id"])


async def _child_request_execution(client, attempt, plan_id, *, key):
    response = await client.post(
        "/api/algo-workers/worker/executions",
        json={
            "strategy_run_id": attempt["run_id"],
            "plan_id": plan_id,
            "idempotency_key": key,
        },
        headers=attempt["child_headers"],
    )
    assert response.status_code < 400, response.text
    return response.json()


async def _materialize_governed_parent(client, env, attempt, revision_id):
    """Open the book, freeze the rebalance, and dispatch it under the grant."""
    await _open_position(client, env, attempt, revision_id, product="MIS")
    plan_id = await _freeze_rebalance(client, env, attempt, revision_id)
    await _open_autonomous_grant(client, env, attempt)
    request = await _child_request_execution(
        client, attempt, plan_id, key=f"gov-{uuid.uuid4().hex[:8]}"
    )
    assert request["status"] == "queued", request
    assert request["decision_kind"] == "automatic", request

    service = _governed_service(env)
    claims = service.claim_next(limit=5)
    assert [row["request_id"] for row in claims] == [request["request_id"]], claims
    outcome = await service.dispatch(request["request_id"])
    assert outcome["status"] == "executed", (
        outcome.get("refusal_code"),
        outcome.get("refusal_detail"),
    )

    parent = _execution(env.factory, plan_id)
    sell_spec = next(row for row in parent["step_spec"] if row["tradingsymbol"] == INFY)
    buy_spec = next(row for row in parent["step_spec"] if row["tradingsymbol"] == RELIANCE)
    assert _claim(env.factory, plan_id, buy_spec["step_no"])["state"] == "withheld"
    return plan_id, sell_spec, buy_spec


async def _resolve_leg_one_fill(env, attempt, *, quantity=66, trade_id="TR-GOV-LEG1"):
    """Ingest leg 1's fill WITHOUT running the release pass."""
    from backend.strategies.live_ingestion import LiveOutcomeConsumer

    _ingest_fill(
        env.factory,
        account_id=env.account_scope,
        run_id=attempt["run_id"],
        order_id="OID-INFY-EXIT",
        trade_id=trade_id,
        quantity=int(quantity),
        side="SELL",
        symbol=INFY,
        token=INFY_TOKEN,
        product="MIS",
    )
    # A BARE consumer: it records the outcome but does NOT release dependents, so
    # the release pass under test is the only thing that can submit leg 2.
    bare = LiveOutcomeConsumer(session_factory=env.factory, clock=env.clock)
    counts = await bare.poll_once()
    assert counts["filled"] == 1, counts


def test_governed_release_submits_the_second_leg_under_an_active_grant(pg, live_env):
    """The happy path: the production release pass sends leg 2 exactly once."""
    env = _Env(
        pg,
        live_env,
        broker=_FakeBroker(order_ids=("OID-INFY-ENTRY", "OID-INFY-EXIT", "OID-REL-BUY")),
    )
    _include_execution_routes(env.app)
    catalog = _seed_catalog(env.factory)
    revision_id = catalog["universe_revision_id"]

    async def _run():
        client = await _operator_client(env.app)
        try:
            attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            plan_id, _sell, buy_spec = await _materialize_governed_parent(
                client, env, attempt, revision_id
            )
            calls_before = len(env.broker.calls)
            await _resolve_leg_one_fill(env, attempt)

            counts = await env.executor.release_sequence()
            assert counts["released"] == 1, (
                counts,
                dict(_claim(env.factory, plan_id, buy_spec["step_no"])),
            )
            released = _claim(env.factory, plan_id, buy_spec["step_no"])
            assert released["state"] == "pending", dict(released)
            assert list(released["broker_order_ids"]) == ["OID-REL-BUY"]
            assert released["detail"]["released_by"] == "live-sequence"

            assert len(env.broker.calls) == calls_before + 1
            buy_intent, _context = env.broker.calls[-1]
            assert buy_intent.payload["order"]["transaction_type"] == "BUY"
            assert buy_intent.payload["order"]["tradingsymbol"] == RELIANCE

            # A second pass changes nothing: a released leg is never re-sent.
            again = await env.executor.release_sequence()
            assert again["released"] == 0, again
            assert len(env.broker.calls) == calls_before + 1
            return plan_id
        finally:
            await client.aclose()

    asyncio.run(_run())


def test_governed_release_refuses_the_second_leg_after_revocation(pg, live_env):
    """A grant revoked between the legs prevents the second submission."""
    env = _Env(
        pg,
        live_env,
        broker=_FakeBroker(order_ids=("OID-INFY-ENTRY", "OID-INFY-EXIT", "OID-REL-BUY")),
    )
    _include_execution_routes(env.app)
    catalog = _seed_catalog(env.factory)
    revision_id = catalog["universe_revision_id"]

    async def _run():
        client = await _operator_client(env.app)
        try:
            attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            plan_id, _sell, buy_spec = await _materialize_governed_parent(
                client, env, attempt, revision_id
            )
            await _resolve_leg_one_fill(env, attempt)
            calls_before = len(env.broker.calls)

            revoked = await client.post(
                f"/api/strategies/{attempt['strategy_id']}/authorization/grants/revoke",
                json={"reason": "owner stop before the second leg"},
            )
            assert revoked.status_code < 400, revoked.text
            assert revoked.json()["grant"]["status"] == "revoked", revoked.text

            counts = await env.executor.release_sequence()
            assert counts["released"] == 0, counts
            assert counts["blocked"] >= 1, counts
            # Nothing was placed and the leg is still withheld, named.
            assert len(env.broker.calls) == calls_before, [
                call[0].payload for call in env.broker.calls
            ]
            held = _claim(env.factory, plan_id, buy_spec["step_no"])
            assert held["state"] == "withheld", dict(held)
            assert held["detail"]["release_blocked"] == "GRANT_REVOKED", dict(held)
            return plan_id
        finally:
            await client.aclose()

    asyncio.run(_run())


def test_governed_release_refuses_the_second_leg_after_a_mode_change(pg, live_env):
    """Switching back to approval-based revokes the standing autonomous mandate."""
    env = _Env(
        pg,
        live_env,
        broker=_FakeBroker(order_ids=("OID-INFY-ENTRY", "OID-INFY-EXIT", "OID-REL-BUY")),
    )
    _include_execution_routes(env.app)
    catalog = _seed_catalog(env.factory)
    revision_id = catalog["universe_revision_id"]

    async def _run():
        client = await _operator_client(env.app)
        try:
            attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            plan_id, _sell, buy_spec = await _materialize_governed_parent(
                client, env, attempt, revision_id
            )
            await _resolve_leg_one_fill(env, attempt)
            calls_before = len(env.broker.calls)

            mode = await client.put(
                f"/api/strategies/{attempt['strategy_id']}/authorization",
                json={"mode": "approval_based", "reason": "review before leg two"},
            )
            assert mode.status_code < 400, mode.text

            counts = await env.executor.release_sequence()
            assert counts["released"] == 0, counts
            assert counts["blocked"] >= 1, counts
            assert len(env.broker.calls) == calls_before, [
                call[0].payload for call in env.broker.calls
            ]
            held = _claim(env.factory, plan_id, buy_spec["step_no"])
            assert held["state"] == "withheld", dict(held)
            assert held["detail"]["release_blocked"] == "AUTHORIZATION_MODE_NOT_AUTONOMOUS", dict(held)
            return plan_id
        finally:
            await client.aclose()

    asyncio.run(_run())
