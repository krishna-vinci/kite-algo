"""C1.2 S1 live option evidence, through production routes and a fake broker.

This suite owns its scratch PostgreSQL database and must run one file per pytest
process, like the other hosted-live route suites.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tests.integration.test_hosted_live_phase2b_routes_postgres import (
    _FakeOptionsManager,
    APP_ADMIN_PASSWORD,
    APP_JWT_SECRET,
    SUPERVISOR_CREDENTIAL,
    _CATALOG,
    OPT_HEDGE,
    OPT_HEDGE_TOKEN,
    OPT_SHORT,
    OPT_SHORT_TOKEN,
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
from tests.integration import test_hosted_live_phase2b_routes_postgres as phase2b_routes
from tests.integration.test_hosted_live_phase2b_routes_postgres import LOT


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


def _adjust_payload(
    *,
    option_run_id: str,
    based_on_generation: int,
    structure_units: int = 2,
    expiry: str = "2026-10-29",
    short_symbol: str = OPT_SHORT,
    short_token: int = OPT_SHORT_TOKEN,
    hedge_symbol: str = OPT_HEDGE,
    hedge_token: int = OPT_HEDGE_TOKEN,
) -> dict:
    legs = [
        {
            "instrument_token": short_token,
            "exchange": "NFO",
            "tradingsymbol": short_symbol,
            "side": "SELL",
            "ratio": 1,
            "reference_price": 100.0,
        },
        {
            "instrument_token": hedge_token,
            "exchange": "NFO",
            "tradingsymbol": hedge_symbol,
            "side": "BUY",
            "ratio": 1,
            "reference_price": 40.0,
        },
    ]
    return {
        "target_kind": "option_structure",
        "payload": {
            "legs": legs,
            "product": "NRML",
            "underlying": "NIFTY",
            "expiry": expiry,
            "expiry_policy": "exit_before_cutoff",
            "phase": "adjust",
            "structure_units": structure_units,
            "option_run_id": option_run_id,
            "based_on_generation": based_on_generation,
        },
    }


def _option_run(factory, plan_id: str) -> dict:
    import json

    from sqlalchemy import text

    with factory() as session:
        row = session.execute(
            text(
                """
                SELECT r.strategy_run_id, r.status, r.metadata, r.trades
                FROM public.strategy_plan_option_runs b
                JOIN public.option_run_states r ON r.strategy_run_id = b.option_run_id
                WHERE b.plan_id = :plan_id
                """
            ),
            {"plan_id": plan_id},
        ).mappings().one()
    return {
        "id": str(row["strategy_run_id"]),
        "status": str(row["status"]),
        "metadata": (
            row["metadata"]
            if isinstance(row["metadata"], dict)
            else json.loads(row["metadata"])
        ),
        "trades": row["trades"] if isinstance(row["trades"], list) else json.loads(row["trades"]),
    }


def _seed_roll_catalog(factory) -> None:
    """Add the replacement generation to the already-pinned catalog."""
    base = _seed_catalog(factory)
    if "NIFTY26NOV25000CE" in base["instruments"]:
        return
    import uuid

    from sqlalchemy import text

    specs = [
        ("NIFTY26NOV25000CE", 900003, 25000.0),
        ("NIFTY26NOV30000CE", 900004, 30000.0),
    ]
    with factory() as session:
        for symbol, token, strike in specs:
            instrument_id = str(uuid.uuid4())
            base["instruments"][symbol] = instrument_id
            session.execute(
                text(
                    "INSERT INTO public.instrument_catalog_records "
                    "(instrument_id, identity_key, public_key, exchange, tradingsymbol, "
                    " lifecycle_status, instrument_type, lot_size, tick_size, expiry, "
                    " strike, option_type, underlying, current_generation_id) "
                    "VALUES (:iid, :key, :key, 'NFO', :symbol, 'active', 'CE', "
                    " :lot, 0.05, '2026-11-26', :strike, 'CE', 'NIFTY', :gen)"
                ),
                {
                    "iid": instrument_id,
                    "key": f"NFO:{symbol}",
                    "symbol": symbol,
                    "lot": LOT,
                    "strike": strike,
                    "gen": base["generation"],
                },
            )
            session.execute(
                text(
                    "INSERT INTO public.instrument_broker_mappings "
                    "(mapping_id, instrument_id, broker, broker_exchange, broker_symbol, "
                    " broker_token, valid_from_generation, is_current) "
                    "VALUES (:mid, :iid, 'kite', 'NFO', :symbol, :token, :gen, TRUE)"
                ),
                {
                    "mid": str(uuid.uuid4()),
                    "iid": instrument_id,
                    "symbol": symbol,
                    "token": token,
                    "gen": base["generation"],
                },
            )
        session.commit()


class _RollOptionsManager(_FakeOptionsManager):
    """Fresh chain evidence for both the held and replacement expiries."""

    def __init__(self, clock):
        super().__init__(clock)
        self.packets[900003] = {
            "token": 900003, "tsym": "NIFTY26NOV25000CE", "ltp": 110.0,
            "iv": 0.15, "delta": 0.41, "updated_at": clock(),
        }
        self.packets[900004] = {
            "token": 900004, "tsym": "NIFTY26NOV30000CE", "ltp": 45.0,
            "iv": 0.17, "delta": 0.62, "updated_at": clock(),
        }

    def get_snapshot(self, _underlying):
        old_short = self.packets[OPT_SHORT_TOKEN]
        old_hedge = self.packets[OPT_HEDGE_TOKEN]
        new_short = self.packets[900003]
        new_hedge = self.packets[900004]
        rows = [
            {"strike": 25000.0, "ce": old_short, "pe": None},
            {"strike": 30000.0, "ce": None, "pe": old_hedge},
        ]
        new_rows = [
            {"strike": 25000.0, "ce": new_short, "pe": None},
            {"strike": 30000.0, "ce": None, "pe": new_hedge},
        ]
        return {
            "underlying": "NIFTY",
            "expiries": ["2026-10-29", "2026-11-26"],
            "per_expiry": {
                "2026-10-29": {"rows": rows},
                "2026-11-26": {"rows": new_rows},
            },
            "updated_at": self.clock(),
        }


def _entry_payload(*, structure_units: int = 1) -> dict:
    """The frozen ENTRY shape, sized to ``structure_units`` lots per leg."""
    body = _option_payload(phase="entry")
    body["payload"]["structure_units"] = int(structure_units)
    return body


def _delete_owner_row(factory, option_run_id: str) -> None:
    """Remove the protection-owner row so the run reads as UNKNOWN ownership."""
    from sqlalchemy import text

    with factory() as session:
        session.execute(
            text("DELETE FROM public.option_protection_owners WHERE option_run_id = :run"),
            {"run": option_run_id},
        )
        session.commit()


def _owner_row(factory, option_run_id: str) -> dict:
    """The run's protection-owner row, keyed for the freeze assertions."""
    import json

    from sqlalchemy import text

    with factory() as session:
        row = session.execute(
            text(
                "SELECT state, owner_run_id, owner_epoch, policy, policy_version "
                "FROM public.option_protection_owners WHERE option_run_id = :run"
            ),
            {"run": option_run_id},
        ).mappings().one()
    return {
        "state": str(row["state"]),
        "owner_run_id": row["owner_run_id"],
        "owner_epoch": int(row["owner_epoch"]),
        "policy": row["policy"] if isinstance(row["policy"], dict) else json.loads(row["policy"]),
        "policy_version": str(row["policy_version"]),
    }


def _ingest_rejection(factory, *, account_id, run_id, order_id, symbol, token) -> None:
    """One broker artifact: an owned order that came back REJECTED with no fill."""
    import uuid

    from sqlalchemy import text

    with factory() as session:
        session.execute(
            text(
                "INSERT INTO live_order_intents (intent_id, client_order_ref, account_id, "
                " strategy_run_id, strategy_family, strategy_name, entry_surface, "
                " broker_order_id, execution_mode, status) "
                "VALUES (:iid, :ref, :account, :run, 'options_strategy', :run, "
                " 'hosted_plan', :oid, 'live', 'placed')"
            ),
            {
                "iid": f"lint_{uuid.uuid4().hex[:8]}",
                "ref": f"KA-REJ-{uuid.uuid4().hex[:6]}",
                "account": account_id,
                "run": run_id,
                "oid": order_id,
            },
        )
        session.execute(
            text(
                "INSERT INTO order_state_projection (account_id, order_id, latest_status, "
                " latest_event_timestamp, last_seen_filled_quantity, dirty_for_trade_sync, "
                " needs_reconcile, terminal, exchange, tradingsymbol, instrument_token, "
                " product, transaction_type, updated_at) "
                "VALUES (:account, :oid, 'REJECTED', NOW(), 0, false, false, true, "
                " 'NFO', :symbol, :token, 'NRML', 'BUY', NOW()) "
                "ON CONFLICT (account_id, order_id) DO UPDATE SET latest_status = 'REJECTED', "
                " last_seen_filled_quantity = 0, terminal = true"
            ),
            {
                "account": account_id,
                "oid": order_id,
                "symbol": symbol,
                "token": token,
            },
        )
        session.commit()


async def _prepare_and_enter_option(
    pg, live_env, broker, clock, *, broker_calls, entry_units: int = 1
):
    _seed_catalog(pg["factory"])
    app, executor = _build_app(pg["factory"], broker, clock)
    client = await _operator_client(app)
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
        _entry_payload(structure_units=entry_units),
        account_scope=live_env["account_scope"],
    )
    assert proposed.status_code < 400, proposed.text
    plan = proposed.json()["plan"]
    executed, _reservation = await _execute(
        client, attempt["strategy_id"], plan["plan_id"]
    )
    assert executed.status_code < 400, executed.text
    hedge_order = executed.json()["broker_order_ids"][0]
    filled = LOT * int(entry_units)
    _ingest_fill(
        pg["factory"],
        account_id=live_env["account_scope"],
        run_id=attempt["run_id"],
        order_id=hedge_order,
        trade_id="TR-ENTRY-HEDGE",
        quantity=filled,
        side="BUY",
        symbol=OPT_HEDGE,
        token=OPT_HEDGE_TOKEN,
    )
    from backend.strategies.live_ingestion import LiveOutcomeConsumer

    consumer = LiveOutcomeConsumer(
        session_factory=pg["factory"],
        clock=clock,
        sequence_releaser=executor.release_sequence,
    )
    counts = await consumer.poll_once()
    assert counts["filled"] == 1, counts
    assert counts["sequence_released"] == 1, counts
    _ingest_fill(
        pg["factory"],
        account_id=live_env["account_scope"],
        run_id=attempt["run_id"],
        order_id=str(broker.calls[-1][1]["result"]["order_id"]),
        trade_id="TR-ENTRY-SHORT",
        quantity=filled,
        side="SELL",
        symbol=OPT_SHORT,
        token=OPT_SHORT_TOKEN,
    )
    counts = await consumer.poll_once()
    assert counts["filled"] == 1, counts
    assert _option_run(pg["factory"], plan["plan_id"])["status"] == "entered"
    return client, executor, attempt, plan


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
        assert plan is not None, proposed.text
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


@pytest.mark.asyncio
async def test_live_option_resize_partial_hedge_releases_nothing_full_hedge_releases_short(pg, live_env):
    _seed_catalog(pg["factory"])
    clock = _clock()
    broker_calls = []

    class _Broker:
        calls = []

        async def handle(self, intent, *, context=None):
            broker_calls.append(intent)
            response = {"result": {"order_id": f"O-RESIZE-{len(broker_calls)}"}}
            self.calls.append((intent, response))
            return response

    client, executor, attempt, entry_plan = await _prepare_and_enter_option(
        pg, live_env, _Broker(), clock, broker_calls=broker_calls
    )
    try:
        run = _option_run(pg["factory"], entry_plan["plan_id"])
        proposed = await _submit_proposal(
            client,
            attempt,
            _adjust_payload(
                option_run_id=run["id"],
                based_on_generation=int(
                    run["metadata"].get("structure_generation") or 1
                ),
            ),
            account_scope=live_env["account_scope"],
        )
        assert proposed.status_code < 400, proposed.text
        plan = proposed.json()["plan"]
        # A resize stays inside the run's HELD generation: same expiry, larger size.
        assert plan["resolved_plan"]["expiry"] == "2026-10-29", plan["resolved_plan"]
        executed, _reservation = await _execute(
            client, attempt["strategy_id"], plan["plan_id"]
        )
        assert executed.status_code < 400, executed.text
        hedge_order = executed.json()["broker_order_ids"][0]
        assert len(broker_calls) == 3, "resize did not start with the hedge increase"

        from backend.strategies.live_ingestion import LiveOutcomeConsumer

        consumer = LiveOutcomeConsumer(
            session_factory=pg["factory"],
            clock=clock,
            sequence_releaser=executor.release_sequence,
        )
        _ingest_fill(
            pg["factory"],
            account_id=live_env["account_scope"],
            run_id=attempt["run_id"],
            order_id=hedge_order,
            trade_id="TR-RESIZE-PART",
            quantity=40,
            side="BUY",
            symbol=OPT_HEDGE,
            token=OPT_HEDGE_TOKEN,
            terminal=False,
        )
        counts = await consumer.poll_once()
        assert counts["partial"] == 1, counts
        assert len(broker_calls) == 3, "a partial hedge released the short"
        claims = _claims(pg["factory"], plan["plan_id"])
        assert claims[0]["state"] == "withheld", claims
        assert "release_blocked" not in claims[0]["detail"], claims[0]

        _ingest_fill(
            pg["factory"],
            account_id=live_env["account_scope"],
            run_id=attempt["run_id"],
            order_id=hedge_order,
            trade_id="TR-RESIZE-FULL",
            quantity=35,
            side="BUY",
            symbol=OPT_HEDGE,
            token=OPT_HEDGE_TOKEN,
        )
        counts = await consumer.poll_once()
        assert counts["filled"] == 1, counts
        assert counts["sequence_released"] == 1, counts
        assert len(broker_calls) == 4
        short_intent = broker_calls[-1]
        assert short_intent.payload["order"]["transaction_type"] == "SELL"
        assert int(short_intent.payload["order"]["quantity"]) == LOT
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_live_roll_partial_acquisition_holds_old_generation_then_releases_once(
    pg, live_env, monkeypatch
):
    _seed_roll_catalog(pg["factory"])
    monkeypatch.setattr(phase2b_routes, "_FakeOptionsManager", _RollOptionsManager)
    clock = _clock()
    broker_calls = []

    class _Broker:
        calls = []

        async def handle(self, intent, *, context=None):
            broker_calls.append(intent)
            response = {"result": {"order_id": f"O-LIVE-ROLL-{len(broker_calls)}"}}
            self.calls.append((intent, response))
            return response

    client, executor, attempt, entry_plan = await _prepare_and_enter_option(
        pg, live_env, _Broker(), clock, broker_calls=broker_calls
    )
    try:
        run = _option_run(pg["factory"], entry_plan["plan_id"])
        proposed = await _submit_proposal(
            client,
            attempt,
            _adjust_payload(
                option_run_id=run["id"],
                based_on_generation=int(
                    run["metadata"].get("structure_generation") or 1
                ),
                structure_units=1,
                expiry="2026-11-26",
                short_symbol="NIFTY26NOV25000CE",
                short_token=900003,
                hedge_symbol="NIFTY26NOV30000CE",
                hedge_token=900004,
            ),
            account_scope=live_env["account_scope"],
        )
        assert proposed.status_code < 400, proposed.text
        plan = proposed.json()["plan"]
        executed, _reservation = await _execute(
            client, attempt["strategy_id"], plan["plan_id"]
        )
        assert executed.status_code < 400, executed.text
        assert len(broker_calls) == 3, "roll did not acquire its hedge first"
        assert broker_calls[-1].payload["order"]["tradingsymbol"] == "NIFTY26NOV30000CE"

        from backend.strategies.live_ingestion import LiveOutcomeConsumer

        consumer = LiveOutcomeConsumer(
            session_factory=pg["factory"],
            clock=clock,
            sequence_releaser=executor.release_sequence,
        )

        async def fill(order_no, trade_id, quantity, side, symbol, token, *, terminal=True):
            _ingest_fill(
                pg["factory"],
                account_id=live_env["account_scope"],
                run_id=attempt["run_id"],
                order_id=f"O-LIVE-ROLL-{order_no}",
                trade_id=trade_id,
                quantity=quantity,
                side=side,
                symbol=symbol,
                token=token,
                terminal=terminal,
            )
            return await consumer.poll_once()

        counts = await fill(
            3, "TR-ROLL-ACQUIRE-PART", 40, "BUY", "NIFTY26NOV30000CE", 900004,
            terminal=False,
        )
        assert counts["partial"] == 1, counts
        assert len(broker_calls) == 3, "a partial acquisition released a generation"
        claims = _claims(pg["factory"], plan["plan_id"])
        # The dependent acquisition short simply waits for its hedge (no blocker
        # is invented while the prerequisite is still in flight)...
        assert claims[0]["state"] == "withheld", claims
        assert "release_blocked" not in claims[0]["detail"], claims[0]
        assert claims[1]["state"] == "partial", claims
        # ...while the roll gate names WHY the old generation is held.
        assert claims[2]["detail"]["release_blocked"] == "option_roll_not_proven", claims
        assert claims[3]["detail"]["release_blocked"] == "option_roll_not_proven", claims

        # The remaining 35 lots complete the acquisition hedge (40 + 35 = 75).
        counts = await fill(
            3, "TR-ROLL-ACQUIRE-HEDGE", LOT - 40, "BUY", "NIFTY26NOV30000CE", 900004
        )
        assert counts["sequence_released"] == 1, counts
        assert len(broker_calls) == 4
        assert broker_calls[-1].payload["order"]["tradingsymbol"] == "NIFTY26NOV25000CE"

        counts = await fill(
            4, "TR-ROLL-ACQUIRE-SHORT", LOT, "SELL", "NIFTY26NOV25000CE", 900003
        )
        assert counts["sequence_released"] == 1, (
            counts,
            _claims(pg["factory"], plan["plan_id"]),
            _option_run(pg["factory"], plan["plan_id"]),
        )
        assert len(broker_calls) == 5, "old hedge was released beside the old short"
        assert broker_calls[-1].payload["order"]["transaction_type"] == "BUY"
        assert broker_calls[-1].payload["order"]["tradingsymbol"] == OPT_SHORT

        counts = await fill(5, "TR-ROLL-OLD-SHORT", LOT, "BUY", OPT_SHORT, OPT_SHORT_TOKEN)
        assert counts["sequence_released"] == 1, counts
        assert len(broker_calls) == 6
        assert broker_calls[-1].payload["order"]["transaction_type"] == "SELL"
        assert broker_calls[-1].payload["order"]["tradingsymbol"] == OPT_HEDGE

        counts = await fill(6, "TR-ROLL-OLD-HEDGE", LOT, "SELL", OPT_HEDGE, OPT_HEDGE_TOKEN)
        assert counts["filled"] == 1, counts
        completed = _option_run(pg["factory"], plan["plan_id"])
        assert completed["id"] == run["id"]
        assert completed["status"] == "entered"
        assert int(completed["metadata"]["structure_generation"]) == 2
        # The completion froze the NEW generation's protection policy on the owner
        # row in the same transaction: the roll's expiry, not the one it replaced.
        from backend.options.protection.ownership import (
            option_protection_policy_version,
        )

        owner = _owner_row(pg["factory"], run["id"])
        assert owner["state"] == "active", owner
        assert owner["policy"]["expiry"] == "2026-11-26", owner["policy"]
        assert owner["policy_version"] == option_protection_policy_version(
            owner["policy"]
        ), owner

        before = len(broker_calls)
        again = await executor.release_sequence()
        assert again["released"] == 0, again
        assert len(broker_calls) == before, "a restarted pass repeated a roll leg"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_live_option_resize_rejected_hedge_releases_nothing(pg, live_env):
    """A REJECTED hedge increase is not a position: the dependent short waits."""
    _seed_catalog(pg["factory"])
    clock = _clock()
    broker_calls = []

    class _Broker:
        calls = []

        async def handle(self, intent, *, context=None):
            broker_calls.append(intent)
            response = {"result": {"order_id": f"O-REJECT-{len(broker_calls)}"}}
            self.calls.append((intent, response))
            return response

    client, executor, attempt, entry_plan = await _prepare_and_enter_option(
        pg, live_env, _Broker(), clock, broker_calls=broker_calls
    )
    try:
        run = _option_run(pg["factory"], entry_plan["plan_id"])
        proposed = await _submit_proposal(
            client,
            attempt,
            _adjust_payload(
                option_run_id=run["id"],
                based_on_generation=int(
                    run["metadata"].get("structure_generation") or 1
                ),
            ),
            account_scope=live_env["account_scope"],
        )
        assert proposed.status_code < 400, proposed.text
        plan = proposed.json()["plan"]
        executed, _reservation = await _execute(
            client, attempt["strategy_id"], plan["plan_id"]
        )
        assert executed.status_code < 400, executed.text
        hedge_order = executed.json()["broker_order_ids"][0]
        assert len(broker_calls) == 3, "the resize did not start with the hedge increase"

        _ingest_rejection(
            pg["factory"],
            account_id=live_env["account_scope"],
            run_id=attempt["run_id"],
            order_id=hedge_order,
            symbol=OPT_HEDGE,
            token=OPT_HEDGE_TOKEN,
        )
        from backend.strategies.live_ingestion import LiveOutcomeConsumer

        consumer = LiveOutcomeConsumer(
            session_factory=pg["factory"],
            clock=clock,
            sequence_releaser=executor.release_sequence,
        )
        counts = await consumer.poll_once()
        assert counts["rejected"] == 1, counts
        assert len(broker_calls) == 3, "a rejected hedge released the short"
        claims = _claims(pg["factory"], plan["plan_id"])
        assert claims[0]["state"] == "withheld", claims
        assert claims[1]["state"] == "rejected", claims
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_live_adjust_with_a_stale_generation_is_refused_at_admission(pg, live_env):
    """A stale-based adjust refuses by name before any reservation is taken."""
    _seed_catalog(pg["factory"])
    clock = _clock()
    broker_calls = []

    class _Broker:
        calls = []

        async def handle(self, intent, *, context=None):
            broker_calls.append(intent)
            response = {"result": {"order_id": f"O-STALE-{len(broker_calls)}"}}
            self.calls.append((intent, response))
            return response

    client, executor, attempt, entry_plan = await _prepare_and_enter_option(
        pg, live_env, _Broker(), clock, broker_calls=broker_calls
    )
    try:
        run = _option_run(pg["factory"], entry_plan["plan_id"])
        stale_generation = int(run["metadata"].get("structure_generation") or 1) + 1
        proposed = await _submit_proposal(
            client,
            attempt,
            _adjust_payload(
                option_run_id=run["id"], based_on_generation=stale_generation
            ),
            account_scope=live_env["account_scope"],
        )
        assert proposed.status_code < 400, proposed.text
        plan = proposed.json()["plan"]
        reserved = await client.post(
            f"/api/strategies/{attempt['strategy_id']}/plans/{plan['plan_id']}/reserve"
        )
        assert reserved.status_code == 409, reserved.text
        assert reserved.json()["detail"]["rejection_reason"] == (
            "OPTION_ADJUSTMENT_STALE_BASIS"
        ), reserved.text
        assert len(broker_calls) == 2, "a stale adjust placed an order"
        assert run["metadata"].get("structure_generation") in (None, 1), run["metadata"]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_live_adjust_owner_unknown_blocks_increases_but_admits_reductions(
    pg, live_env
):
    """Unknown ownership refuses an increasing adjust; a reduce-only one runs."""
    _seed_catalog(pg["factory"])
    clock = _clock()
    broker_calls = []

    class _Broker:
        calls = []

        async def handle(self, intent, *, context=None):
            broker_calls.append(intent)
            response = {"result": {"order_id": f"O-OWNER-{len(broker_calls)}"}}
            self.calls.append((intent, response))
            return response

    client, executor, attempt, entry_plan = await _prepare_and_enter_option(
        pg, live_env, _Broker(), clock, broker_calls=broker_calls, entry_units=2
    )
    try:
        run = _option_run(pg["factory"], entry_plan["plan_id"])
        _delete_owner_row(pg["factory"], run["id"])
        generation = int(run["metadata"].get("structure_generation") or 1)

        # -- an INCREASE is refused: the run's ownership is not readable as active.
        grow = await _submit_proposal(
            client,
            attempt,
            _adjust_payload(
                option_run_id=run["id"],
                based_on_generation=generation,
                structure_units=3,
            ),
            account_scope=live_env["account_scope"],
        )
        assert grow.status_code < 400, grow.text
        grow_plan = grow.json()["plan"]
        refused = await client.post(
            f"/api/strategies/{attempt['strategy_id']}/plans/{grow_plan['plan_id']}/reserve"
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["rejection_reason"] == (
            "OPTION_PROTECTION_OWNER_UNKNOWN"
        ), refused.text
        assert len(broker_calls) == 2, "an ownerless increase placed an order"

        # -- a REDUCE-ONLY adjust stays admissible and closes the excess short.
        reduce = await _submit_proposal(
            client,
            attempt,
            _adjust_payload(
                option_run_id=run["id"],
                based_on_generation=generation,
                structure_units=1,
            ),
            account_scope=live_env["account_scope"],
        )
        assert reduce.status_code < 400, reduce.text
        reduce_plan = reduce.json()["plan"]
        admitted, _reservation = await _execute(
            client, attempt["strategy_id"], reduce_plan["plan_id"]
        )
        assert admitted.status_code < 400, admitted.text
        assert len(broker_calls) == 3, "the reduce-only adjust dispatched nothing"
        close_intent = broker_calls[-1]
        assert close_intent.payload["order"]["transaction_type"] == "BUY", close_intent.payload
        assert int(close_intent.payload["order"]["quantity"]) == LOT, close_intent.payload
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_live_roll_ledger_divergence_holds_the_old_generation(
    pg, live_env, monkeypatch
):
    """A run ledger that lost the new generation never releases the old one."""
    _seed_roll_catalog(pg["factory"])
    monkeypatch.setattr(phase2b_routes, "_FakeOptionsManager", _RollOptionsManager)
    clock = _clock()
    broker_calls = []

    class _Broker:
        calls = []

        async def handle(self, intent, *, context=None):
            broker_calls.append(intent)
            response = {"result": {"order_id": f"O-DIVERGE-{len(broker_calls)}"}}
            self.calls.append((intent, response))
            return response

    client, executor, attempt, entry_plan = await _prepare_and_enter_option(
        pg, live_env, _Broker(), clock, broker_calls=broker_calls
    )
    try:
        run = _option_run(pg["factory"], entry_plan["plan_id"])
        proposed = await _submit_proposal(
            client,
            attempt,
            _adjust_payload(
                option_run_id=run["id"],
                based_on_generation=int(
                    run["metadata"].get("structure_generation") or 1
                ),
                structure_units=1,
                expiry="2026-11-26",
                short_symbol="NIFTY26NOV25000CE",
                short_token=900003,
                hedge_symbol="NIFTY26NOV30000CE",
                hedge_token=900004,
            ),
            account_scope=live_env["account_scope"],
        )
        assert proposed.status_code < 400, proposed.text
        plan = proposed.json()["plan"]
        executed, _reservation = await _execute(
            client, attempt["strategy_id"], plan["plan_id"]
        )
        assert executed.status_code < 400, executed.text

        from backend.strategies.live_ingestion import LiveOutcomeConsumer

        consumer = LiveOutcomeConsumer(
            session_factory=pg["factory"],
            clock=clock,
            sequence_releaser=executor.release_sequence,
        )
        #: Records an outcome WITHOUT running the release pass, so the ledger can
        #: be corrupted at the one boundary the roll gate has to prove.
        quiet = LiveOutcomeConsumer(
            session_factory=pg["factory"], clock=clock, sequence_releaser=None
        )

        # -- the acquisition hedge fills FULL, which releases the dependent short.
        _ingest_fill(
            pg["factory"],
            account_id=live_env["account_scope"],
            run_id=attempt["run_id"],
            order_id="O-DIVERGE-3",
            trade_id="TR-DIV-ACQ-HEDGE",
            quantity=LOT,
            side="BUY",
            symbol="NIFTY26NOV30000CE",
            token=900004,
        )
        counts = await consumer.poll_once()
        assert counts["sequence_released"] == 1, counts
        assert len(broker_calls) == 4

        # -- the acquisition short fills, but the old generation is NOT released
        #    yet: the outcome is recorded from the ledger first.
        _ingest_fill(
            pg["factory"],
            account_id=live_env["account_scope"],
            run_id=attempt["run_id"],
            order_id="O-DIVERGE-4",
            trade_id="TR-DIV-ACQ-SHORT",
            quantity=LOT,
            side="SELL",
            symbol="NIFTY26NOV25000CE",
            token=900003,
        )
        counts = await quiet.poll_once()
        assert counts["filled"] == 1, counts
        assert len(broker_calls) == 4, "the old short was released before the proof"

        # -- the run's own ledger LOSES the new short: divergence, not proof.
        from sqlalchemy import text

        updated = _option_run(pg["factory"], plan["plan_id"])
        trimmed = [
            trade
            for trade in updated["trades"]
            if not (
                str(trade.get("plan_id") or "") == plan["plan_id"]
                and int(trade.get("step_no") or 0) == 1
            )
        ]
        import json

        with pg["factory"]() as session:
            session.execute(
                text(
                    "UPDATE public.option_run_states SET trades = CAST(:trades AS jsonb) "
                    "WHERE strategy_run_id = :run"
                ),
                {"trades": json.dumps(trimmed), "run": run["id"]},
            )
            session.commit()

        before = len(broker_calls)
        again = await executor.release_sequence()
        assert again["released"] == 0, again
        assert len(broker_calls) == before, "an inconsistent ledger released a leg"
        claims = _claims(pg["factory"], plan["plan_id"])
        assert claims[2]["state"] == "withheld", claims
        assert claims[2]["detail"]["release_blocked"] == (
            "LIVE_OPTION_RUN_LEDGER_INCONSISTENT"
        ), claims
        assert _option_run(pg["factory"], plan["plan_id"])["status"] == "adjusting"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_gated_option_legs_are_bounded_limits_and_a_timeout_cancels_once(
    pg, live_env, _margin_boundary
):
    """C1.2 S4 end to end: an immediate hedge stays MARKET, the gated short is a
    bounded LIMIT at the derived price, and a working LIMIT that outlives the
    platform timeout is cancelled exactly once - never repriced, never replaced,
    and never repeated after a restart."""
    _seed_catalog(pg["factory"])
    clock = _clock()
    broker_calls = []

    class _Broker:
        async def handle(self, intent, *, context=None):
            broker_calls.append(intent)
            if intent.intent_type == "cancel_order":
                order_id = str(intent.payload.get("order", {}).get("order_id") or "")
                return {
                    "result": {
                        "order_id": order_id,
                        "status": "CANCELLED",
                        "filled_quantity": 0,
                    }
                }
            return {"result": {"order_id": f"O-{len(broker_calls)}", "status": "success"}}

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

        executed, _reservation = await _execute(client, attempt["strategy_id"], plan["plan_id"])
        assert executed.status_code < 400, executed.text
        assert len(broker_calls) == 1, executed.text
        hedge_order = executed.json()["broker_order_ids"][0]
        # The immediate hedge is NOT a gated dependent leg: it keeps MARKET.
        assert broker_calls[0].payload["order"]["order_type"] == "MARKET"

        _ingest_fill(
            pg["factory"],
            account_id=live_env["account_scope"],
            run_id=attempt["run_id"],
            order_id=hedge_order,
            trade_id="TR-C12-S4-HEDGE",
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
        counts = await consumer.poll_once()
        assert counts["sequence_released"] == 1, counts
        assert len(broker_calls) == 2, counts
        released = broker_calls[1].payload["order"]
        assert released["order_type"] == "LIMIT", released
        # LTP 1500.0 (the harness quote) is the fallback reference; the band is
        # the frozen reference price 100.0 with a 0.5% half-width.
        assert abs(released["price"] - 1500.0 * 0.995) < 1e-6, released

        claims = _claims(pg["factory"], plan["plan_id"])
        short_claim = next(claim for claim in claims if claim["state"] == "pending")
        evidence = short_claim["detail"]["execution_order"]
        assert evidence["order_type"] == "LIMIT"
        assert evidence["reference_price_inr"] == 100.0
        assert abs(evidence["bound_price_inr"] - 100.0 * 0.995) < 1e-9
        assert evidence["reference_source"] == "ltp"
        assert evidence["tick_size"] == 0.05
        assert evidence["tick_source"] == "catalog"
        assert evidence["submitted_at"]
        short_orders = list(short_claim["broker_order_ids"])
        assert len(short_orders) == 1, short_claim
        short_order = short_orders[0]

        # The working LIMIT outlives the platform timeout: it is cancelled once,
        # with an explicit terminal outcome, and the dependent behaviour is
        # unchanged (an unfilled leg never becomes a market order).
        clock.now = clock.now + timedelta(seconds=31)
        counts = await consumer.poll_once()
        assert counts["sequence_released"] == 0, counts
        cancels = [intent for intent in broker_calls if intent.intent_type == "cancel_order"]
        assert len(cancels) == 1, broker_calls
        assert cancels[0].payload["order"]["order_id"] == short_order
        short_claim = next(
            claim
            for claim in _claims(pg["factory"], plan["plan_id"])
            if claim["detail"].get("execution_order")
        )
        assert short_claim["state"] == "rejected", short_claim
        assert short_claim["detail"]["limit_timeout"]["terminal"] == "cancelled"

        # A restart (a fresh pass, a fresh consumer) never sends a second cancel.
        restarted = LiveOutcomeConsumer(
            session_factory=pg["factory"],
            clock=clock,
            sequence_releaser=executor.release_sequence,
        )
        await restarted.poll_once()
        assert len([i for i in broker_calls if i.intent_type == "cancel_order"]) == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_a_released_reservation_blocks_the_gated_release_by_name(
    pg, live_env, _margin_boundary
):
    """C1.2 S3 reservation gate: the release re-reads capacity and never renews.

    The withheld short stays withheld while the plan's reservation is released -
    refused by NAME, not as a generic approval failure - and it is released only
    once the capacity is restored. The release pass never renews the reservation
    itself.
    """
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
        executed, reservation = await _execute(
            client, attempt["strategy_id"], plan["plan_id"]
        )
        assert executed.status_code < 400, executed.text
        assert len(broker_calls) == 1, executed.text
        hedge_order = executed.json()["broker_order_ids"][0]

        _ingest_fill(
            pg["factory"],
            account_id=live_env["account_scope"],
            run_id=attempt["run_id"],
            order_id=hedge_order,
            trade_id="TR-C12-S3-HEDGE",
            quantity=75,
            side="BUY",
            symbol="NIFTY26OCT30000CE",
            token=900002,
        )

        # The owner released the capacity after the hedge filled: the withheld
        # short is NOT dispatched against a reservation that no longer holds.
        from backend.strategies.reservations import ReservationLedger

        ledger = ReservationLedger(session_factory=pg["factory"])
        ledger.release(
            reservation["reservation_id"], reason="c12-s3", actor_id="app:owner"
        )
        assert ledger.get(reservation["reservation_id"])["status"] == "released"

        from backend.strategies.live_ingestion import LiveOutcomeConsumer

        consumer = LiveOutcomeConsumer(
            session_factory=pg["factory"],
            clock=clock,
            sequence_releaser=executor.release_sequence,
        )
        counts = await consumer.poll_once()
        claims = _claims(pg["factory"], plan["plan_id"])
        assert counts["sequence_blocked"] == 1, (counts, claims)
        assert claims[0]["state"] == "withheld", claims
        assert claims[0]["detail"]["release_blocked"] == "LIVE_RESERVATION_REQUIRED", claims
        assert len(broker_calls) == 1, "a released reservation released the short"
        # Never renewed by the release pass: the row is still released.
        assert ledger.get(reservation["reservation_id"])["status"] == "released"

        # Capacity restored (an operator action, not the release path): the SAME
        # withheld short now releases.
        with pg["factory"]() as session:
            from sqlalchemy import text

            session.execute(
                text(
                    "UPDATE public.strategy_reservations SET status = 'active' "
                    "WHERE reservation_id = :rid"
                ),
                {"rid": reservation["reservation_id"]},
            )
            session.commit()
        counts = await consumer.poll_once()
        claims = _claims(pg["factory"], plan["plan_id"])
        assert counts["sequence_released"] == 1, (counts, claims)
        assert claims[0]["state"] == "pending", claims
        assert len(broker_calls) == 2
    finally:
        await client.aclose()
