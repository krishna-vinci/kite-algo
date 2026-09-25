"""C1.2 S1 live option evidence, through production routes and a fake broker.

This suite owns its scratch PostgreSQL database and must run one file per pytest
process, like the other hosted-live route suites.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tests.integration.test_hosted_live_phase2b_routes_postgres import (
    APP_ADMIN_PASSWORD,
    APP_JWT_SECRET,
    SUPERVISOR_CREDENTIAL,
    _CATALOG,
    _build_app,
    _claims,
    _create_db,
    _drop_db,
    _execute,
    _ingest_fill,
    _option_payload,
    _operator_client,
    _prepare_live_attempt,
    _seed_catalog,
    _submit_proposal,
)


def _clock():
    class _Clock:
        def __init__(self):
            self.now = datetime.now(timezone.utc)

        def __call__(self):
            return self.now

    return _Clock()


def _declare_version_risk_policy(factory, strategy_id, risk_policy):
    import json

    from sqlalchemy import text

    with factory() as session:
        session.execute(
            text(
                "UPDATE public.hosted_strategy_versions "
                "SET risk_policy = CAST(:policy AS jsonb) WHERE strategy_id = :sid"
            ),
            {"policy": json.dumps(risk_policy), "sid": strategy_id},
        )
        session.commit()


@pytest.fixture
def pg():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    _CATALOG.clear()
    name, dsn = _create_db()
    try:
        os.environ["DATABASE_URL"] = dsn
        cfg = Config("backend/alembic.ini")
        cfg.set_main_option("sqlalchemy.url", dsn)
        cfg.set_main_option("script_location", "backend/alembic")
        command.upgrade(cfg, "head")
        engine = create_engine(dsn, poolclass=NullPool)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        try:
            yield {"dsn": dsn, "factory": factory, "engine": engine}
        finally:
            engine.dispose()
    finally:
        _drop_db(name)


@pytest.fixture
def live_env(pg):
    broker_user_id = f"c12s1{uuid.uuid4().hex[:6]}"
    account_scope = f"kite:{broker_user_id}"
    saved = {}
    for key in (
        "APP_ADMIN_PASSWORD_HASH", "APP_ADMIN_PASSWORD_HASH_B64",
        "APP_ADMIN_PASSWORD_HASH_FILE",
    ):
        saved[key] = os.environ.pop(key, None)
    os.environ.update(
        {
            "DATABASE_URL": pg["dsn"], "APP_ENV": "development",
            "APP_ALLOW_INSECURE_DEV_AUTH": "true", "APP_ADMIN_USERNAME": "admin",
            "APP_ADMIN_PASSWORD": APP_ADMIN_PASSWORD,
            "APP_JWT_SECRET": APP_JWT_SECRET, "JWT_SECRET": APP_JWT_SECRET,
            "HOSTED_SUPERVISOR_CREDENTIAL": SUPERVISOR_CREDENTIAL,
            "HOSTED_STRATEGY_ACCOUNT_SCOPES": account_scope,
            "HOSTED_LIVE_ENABLED": "true",
        }
    )
    from sqlalchemy import text

    with pg["factory"]() as session:
        session.execute(
            text(
                "INSERT INTO public.kite_sessions (session_id, access_token, broker_user_id, created_at) "
                "VALUES ('system', 'c1-2-access-token', :uid, NOW())"
            ),
            {"uid": broker_user_id},
        )
        session.commit()
    try:
        yield {"account_scope": account_scope}
    finally:
        for key in (
            "APP_ADMIN_PASSWORD", "APP_JWT_SECRET", "JWT_SECRET",
            "HOSTED_STRATEGY_ACCOUNT_SCOPES", "HOSTED_LIVE_ENABLED",
            "HOSTED_SUPERVISOR_CREDENTIAL",
        ):
            os.environ.pop(key, None)
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


@pytest.fixture(autouse=True)
def _margin_boundary(monkeypatch):
    from backend.api.routers import strategies as strategies_module

    evidence = {"required_margin_inr": 1_000_000.0}
    monkeypatch.setattr(
        strategies_module,
        "_live_margin_evidence",
        lambda _scope, _plan: {
            "usable": 5_000_000.0,
            "required_margin_inr": evidence["required_margin_inr"],
            "as_of": datetime.now(timezone.utc).isoformat(),
        },
    )
    return evidence


@pytest.mark.asyncio
async def test_fresh_evidence_dispatches_and_a_stale_release_records_a_blocker(pg, live_env, _margin_boundary):
    """A fresh chain admits the first leg; age at the short release blocks it."""
    _seed_catalog(pg["factory"])
    clock = _clock()
    broker_calls = []

    class _Broker:
        async def handle(self, intent, *, context=None):
            broker_calls.append(intent)
            return {"result": {"order_id": f"O-{len(broker_calls)}"}}

    app, executor = _build_app(pg["factory"], _Broker(), clock)
    client = await _operator_client(app)
    try:
        attempt = await _prepare_live_attempt(
            client,
            account_scope=live_env["account_scope"],
            lease_until=clock() + timedelta(hours=12),
        )
        _declare_version_risk_policy(
            pg["factory"],
            attempt["strategy_id"],
            {"allowed_structure_families": ["vertical_spread"]},
        )
        proposed = await _submit_proposal(
            client,
            attempt,
            _option_payload(phase="entry"),
            account_scope=live_env["account_scope"],
        )
        assert proposed.status_code < 400, proposed.text
        plan = proposed.json()["plan"]
        evidence = plan["resolved_plan"]["option_chain_evidence"]
        assert evidence["underlying"] == "NIFTY"
        assert evidence["snapshot_digest"]
        assert set(evidence["legs"]) == {
            _CATALOG["instruments"][name]
            for name in ("NIFTY26OCT25000CE", "NIFTY26OCT30000CE")
        }

        executed, _reservation = await _execute(client, attempt["strategy_id"], plan["plan_id"])
        assert executed.status_code < 400, executed.text
        assert len(broker_calls) == 1, executed.text
        hedge_order = executed.json()["broker_order_ids"][0]

        _ingest_fill(
            pg["factory"],
            account_id=live_env["account_scope"],
            run_id=attempt["run_id"],
            order_id=hedge_order,
            trade_id="TR-C12-HEDGE",
            quantity=75,
            side="BUY",
            symbol="NIFTY26OCT30000CE",
            token=900002,
        )
        from backend.strategies.live_ingestion import LiveOutcomeConsumer

        consumer = LiveOutcomeConsumer(
            session_factory=pg["factory"],
            clock=clock,
            sequence_releaser=executor.release_sequence,
        )
        clock.now = clock.now + timedelta(seconds=6)
        counts = await consumer.poll_once()
        claims = _claims(pg["factory"], plan["plan_id"])
        assert counts["sequence_blocked"] == 1, (counts, claims)
        assert claims[0]["state"] == "withheld", claims
        assert claims[0]["detail"]["release_blocked"] == (
            "OPTION_CHAIN_SNAPSHOT_STALE"
        ), claims[1]
        assert len(broker_calls) == 1, "stale evidence released the short"

        clock.now = clock.now - timedelta(seconds=6)
        counts = await consumer.poll_once()
        claims = _claims(pg["factory"], plan["plan_id"])
        assert counts["sequence_released"] == 1, (counts, claims)
        assert claims[0]["state"] == "pending", claims
        assert len(broker_calls) == 2
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_margin_limit_enforces_required_margin_inr_on_live_option_entry(pg, live_env, _margin_boundary):
    """B2.5 reads the new key: over the limit refuses; the twin dispatches."""
    _seed_catalog(pg["factory"])
    clock = _clock()
    broker_calls = []

    class _Broker:
        async def handle(self, intent, *, context=None):
            broker_calls.append(intent)
            return {"result": {"order_id": f"O-{len(broker_calls)}"}}

    app, executor = _build_app(pg["factory"], _Broker(), clock)
    evidence = _margin_boundary
    executor._margin_reader = lambda _account, _plan: {
        "usable": 5_000_000.0,
        "required_margin_inr": evidence["required_margin_inr"],
        "as_of": clock().isoformat(),
    }
    client = await _operator_client(app)
    try:
        attempt = await _prepare_live_attempt(
            client,
            account_scope=live_env["account_scope"],
            lease_until=clock() + timedelta(hours=12),
        )
        _declare_version_risk_policy(
            pg["factory"],
            attempt["strategy_id"],
            {
                "allowed_structure_families": ["vertical_spread"],
                "margin_limit_inr": 2_000.0,
            },
        )
        policy = await client.put(
            f"/api/strategies/{attempt['strategy_id']}/admission-policy",
            json={"allocation_inr": 50_000_000.0},
        )
        assert policy.status_code < 400, policy.text

        _margin_boundary["required_margin_inr"] = 2_001.0
        proposed = await _submit_proposal(
            client,
            attempt,
            _option_payload(phase="entry"),
            account_scope=live_env["account_scope"],
        )
        assert proposed.status_code < 400, proposed.text
        over_plan = proposed.json()["plan"]
        reserved = await client.post(
            f"/api/strategies/{attempt['strategy_id']}/plans/{over_plan['plan_id']}/reserve"
        )
        assert reserved.status_code >= 400, reserved.text
        assert reserved.json()["detail"]["rejection_reason"] == "ADMISSION_REFUSED"
        assert reserved.json()["detail"]["admission"]["rejection_reason"] == (
            "MARGIN_INSUFFICIENT"
        ), reserved.text
        assert broker_calls == []

        _margin_boundary["required_margin_inr"] = 1_000.0
        proposed = await _submit_proposal(
            client,
            attempt,
            _option_payload(phase="entry"),
            account_scope=live_env["account_scope"],
        )
        assert proposed.status_code < 400, proposed.text
        under_plan = proposed.json()["plan"]
        admitted, _reservation = await _execute(
            client, attempt["strategy_id"], under_plan["plan_id"]
        )
        assert admitted.status_code < 400, admitted.text
        assert len(broker_calls) == 1
    finally:
        await client.aclose()
