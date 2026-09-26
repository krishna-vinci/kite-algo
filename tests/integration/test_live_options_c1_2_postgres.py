"""C1.2 S1 live option evidence, through production routes and a fake broker.

This suite owns its scratch PostgreSQL database and must run one file per pytest
process, like the other hosted-live route suites.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

# The suite drives its own clock in a disposable database with no imported NSE
# calendar, so the market session is supplied as EVIDENCE through the production
# seam rather than guessed (see tests/support/market_session_stub.py).
from tests.support.market_session_stub import open_market_session  # noqa: F401

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
                "SELECT state, owner_run_id, owner_epoch, policy, policy_version, "
                " action_state "
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
        "action_state": str(row["action_state"] or "none"),
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
            "HOSTED_LIVE_LANES": "cnc,mis,futures,options",
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
            "HOSTED_LIVE_LANES",
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


# ---------------------------------------------------------------------------
# C1.2 S5: live protection continuity
#
# The owner row is what protects a LIVE structure: the live entry claims it with
# the run, the live adjust gate asks it before any leg, the successor's creation
# inherits it, and the platform's own staged exit resolves the run through it
# after the child is gone. These tests fake the broker ONLY (no real order, no
# network); every other read is the production one.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_options_resize_roll_and_protective_exit_end_to_end(
    pg, live_env, monkeypatch
):
    """C1.2 S5 end to end on the LIVE lane, with a fake broker only.

    entry (the owner claimed with the run) -> resize -> expiry roll -> stale
    protection triggers -> staged short-first protective exit -> the run exits
    and the owner row is released.

    Checked at every checkpoint: exactly ONE owner row; every GATED lane leg a
    bounded LIMIT (immediate legs keep their order type - and the protective
    exit itself keeps the existing structure submitter, per the S5 design's
    "the live basket boundary is the durable option run"); and no real account -
    the protective exit runs on the platform's own authority after the child's
    token is revoked, and only the injected broker boundaries are ever called.
    """
    from backend.options.protection.ownership import option_protection_policy_version
    from backend.strategies.live_ingestion import LiveOutcomeConsumer

    _seed_roll_catalog(pg["factory"])
    _fresh_options_manager(monkeypatch, base=_RollOptionsManager)
    clock = _clock()
    broker_calls = []

    class _Broker:
        calls = []

        async def handle(self, intent, *, context=None):
            broker_calls.append(intent)
            response = {"result": {"order_id": f"O-C12S5-E2E-{len(broker_calls)}"}}
            self.calls.append((intent, response))
            return response

    client, executor, attempt, entry_plan = await _prepare_and_enter_option(
        pg, live_env, _Broker(), clock, broker_calls=broker_calls, entry_units=1
    )
    consumer = LiveOutcomeConsumer(
        session_factory=pg["factory"],
        clock=clock,
        sequence_releaser=executor.release_sequence,
    )
    lane_plan_ids = [entry_plan["plan_id"]]
    try:
        run_id = _option_run(pg["factory"], entry_plan["plan_id"])["id"]
        assert _owner_row_count(pg["factory"], run_id) == 1

        async def lane_fill(order_no, trade_id, quantity, side, symbol, token):
            _ingest_fill(
                pg["factory"],
                account_id=live_env["account_scope"],
                run_id=attempt["run_id"],
                order_id=f"O-C12S5-E2E-{order_no}",
                trade_id=trade_id,
                quantity=quantity,
                side=side,
                symbol=symbol,
                token=token,
            )
            return await consumer.poll_once()

        async def submit_adjust(payload):
            proposed = await _submit_proposal(
                client, attempt, payload, account_scope=live_env["account_scope"]
            )
            assert proposed.status_code < 400, proposed.text
            plan = proposed.json()["plan"]
            assert plan is not None, proposed.text
            executed, _reservation = await _execute(
                client, attempt["strategy_id"], plan["plan_id"]
            )
            assert executed.status_code < 400, executed.text
            lane_plan_ids.append(plan["plan_id"])
            return plan

        # -- resize 1 -> 2: the gated short waits for its hedge increase.
        generation = int(
            _option_run(pg["factory"], entry_plan["plan_id"])["metadata"].get(
                "structure_generation"
            )
            or 1
        )
        await submit_adjust(
            _adjust_payload(
                option_run_id=run_id, based_on_generation=generation, structure_units=2
            )
        )
        assert len(broker_calls) == 3, "resize did not start with the hedge increase"
        counts = await lane_fill(
            3, "TR-S5-RESIZE-HEDGE", LOT, "BUY", OPT_HEDGE, OPT_HEDGE_TOKEN
        )
        assert counts["sequence_released"] == 1, counts
        assert len(broker_calls) == 4
        counts = await lane_fill(
            4, "TR-S5-RESIZE-SHORT", LOT, "SELL", OPT_SHORT, OPT_SHORT_TOKEN
        )
        assert counts["filled"] == 1, counts
        resized = _option_run(pg["factory"], entry_plan["plan_id"])
        assert resized["status"] == "entered", resized
        assert int(resized["metadata"]["structure_generation"]) == 2, resized["metadata"]
        owner = _owner_row(pg["factory"], run_id)
        assert _owner_row_count(pg["factory"], run_id) == 1
        # The generation completion froze the NEW policy on the owner row in the
        # SAME transaction as the held-structure write.
        assert owner["policy_version"] == option_protection_policy_version(owner["policy"])

        # -- expiry roll at 2 units: acquire first, then release the old set.
        await submit_adjust(
            _adjust_payload(
                option_run_id=run_id,
                based_on_generation=2,
                structure_units=2,
                expiry="2026-11-26",
                short_symbol="NIFTY26NOV25000CE",
                short_token=900003,
                hedge_symbol="NIFTY26NOV30000CE",
                hedge_token=900004,
            )
        )
        assert broker_calls[-1].payload["order"]["tradingsymbol"] == "NIFTY26NOV30000CE"
        rolled_size = LOT * 2
        counts = await lane_fill(
            5, "TR-S5-ROLL-ACQ-HEDGE", rolled_size, "BUY", "NIFTY26NOV30000CE", 900004
        )
        assert counts["sequence_released"] >= 1, counts
        counts = await lane_fill(
            6, "TR-S5-ROLL-ACQ-SHORT", rolled_size, "SELL", "NIFTY26NOV25000CE", 900003
        )
        assert counts["sequence_released"] >= 1, counts
        counts = await lane_fill(
            7, "TR-S5-ROLL-REL-SHORT", rolled_size, "BUY", OPT_SHORT, OPT_SHORT_TOKEN
        )
        assert counts["sequence_released"] >= 1, counts
        counts = await lane_fill(
            8, "TR-S5-ROLL-REL-HEDGE", rolled_size, "SELL", OPT_HEDGE, OPT_HEDGE_TOKEN
        )
        assert counts["filled"] == 1, counts
        rolled = _option_run(pg["factory"], entry_plan["plan_id"])
        assert rolled["status"] == "entered", rolled
        assert int(rolled["metadata"]["structure_generation"]) == 3, rolled["metadata"]
        rolled_owner = _owner_row(pg["factory"], run_id)
        assert _owner_row_count(pg["factory"], run_id) == 1
        assert rolled_owner["policy"]["expiry"] == "2026-11-26", rolled_owner["policy"]
        assert rolled_owner["policy_version"] == option_protection_policy_version(
            rolled_owner["policy"]
        )

        # -- the child dies: protection triggers on the stale heartbeat.
        _arm_stale_protection(pg["factory"], attempt["run_id"])
        _revoke_worker_token(pg["factory"], attempt["run_id"])
        assert _worker_token_status(pg["factory"], attempt["run_id"]) == "revoked"

        basket = _ProtectionBasket(pg["factory"])
        protection_clock = _S5Clock()
        runtime = _protection_runtime(pg["factory"], basket, now_fn=protection_clock)

        def fill_stage(trade_prefix):
            legs = _submitted_stage_legs(pg["factory"], run_id)
            for index, leg in enumerate(legs):
                symbol = str(leg["tradingsymbol"])
                _ingest_protection_fill(
                    pg["factory"],
                    account_id=live_env["account_scope"],
                    order_id=str(leg["order_id"]),
                    trade_id=f"{trade_prefix}-{index}",
                    quantity=int(leg["quantity"]),
                    side=str(leg["transaction_type"]).upper(),
                    symbol=symbol,
                    token=_S5_TOKEN_BY_SYMBOL[symbol],
                )
            return legs

        # Stage 1: the SHORT closes and the hedge is NOT offered.
        first = await runtime.evaluate_once()
        assert first == {"evaluated": 1, "triggered": 1, "errors": 0}, first
        assert [
            (row["tradingsymbol"], row["transaction_type"], row["quantity"])
            for row in basket.placed
        ] == [("NIFTY26NOV25000CE", "BUY", rolled_size)], basket.placed
        assert _owner_row(pg["factory"], run_id)["action_state"] == "staging"
        assert _owner_row_count(pg["factory"], run_id) == 1
        stage_one = fill_stage("TR-S5-PROT-SHORT")
        assert [leg["tradingsymbol"] for leg in stage_one] == ["NIFTY26NOV25000CE"]

        # Stage 2: the short is PROVEN closed, so the hedge releases once.
        protection_clock.advance(120)
        second = await runtime.evaluate_once()
        assert second["errors"] == 0, second
        assert [
            (row["tradingsymbol"], row["transaction_type"], row["quantity"])
            for row in basket.placed
        ] == [
            ("NIFTY26NOV25000CE", "BUY", rolled_size),
            ("NIFTY26NOV30000CE", "SELL", rolled_size),
        ], basket.placed
        fill_stage("TR-S5-PROT-HEDGE")

        # Stage 3: the run's OWN fills now prove flat, and the exit completes.
        protection_clock.advance(120)
        finished = await runtime.evaluate_once()
        assert finished["errors"] == 0, finished
        assert len(basket.placed) == 2, basket.placed
        assert _owner_row(pg["factory"], run_id)["action_state"] == "none"

        # Every GATED live leg the lane dispatched was a bounded LIMIT; the
        # immediate hedge entries kept MARKET.
        gated = []
        for plan_id in lane_plan_ids:
            for claim in _claims(pg["factory"], plan_id):
                evidence = (claim.get("detail") or {}).get("execution_order")
                if evidence:
                    gated.append((claim["step_no"], evidence))
        assert len(gated) >= 5, gated
        assert all(row["order_type"] == "LIMIT" for _step, row in gated), gated
        limit_intents = [
            intent
            for intent in broker_calls
            if intent.payload["order"]["order_type"] == "LIMIT"
        ]
        assert len(limit_intents) == len(gated), (
            len(limit_intents),
            [step for step, _row in gated],
        )
        assert all(intent.payload["order"].get("price") for intent in limit_intents), (
            limit_intents
        )
        assert all(
            intent.payload["order"]["order_type"] == "LIMIT"
            for intent in broker_calls
            if intent.payload["order"].get("price") is not None
        )
        assert broker_calls[0].payload["order"]["order_type"] == "MARKET"

        # The run's own fills now prove it flat, so the platform's terminal write
        # closes it - and the owner row is released in the SAME transaction (the
        # B2.4 terminal hook), which is the continuity this slice owns. This is
        # the store's own status write, the one production performs when a
        # structure closes.
        from backend.options.execution.durable_store import DurableOptionRunStore

        runs = DurableOptionRunStore(session_factory=pg["factory"])
        closed = runs.get_run(run_id)
        assert str(closed.status) == "entered", closed.status
        closed.status = "exited"
        runs.save_run(closed)
        assert runs.get_run(run_id).status == "exited"
        assert _owner_row(pg["factory"], run_id)["state"] == "released"
        assert _owner_row_count(pg["factory"], run_id) == 1
    finally:
        await client.aclose()


def _fresh_options_manager(monkeypatch, base=_FakeOptionsManager):
    """Point the harness at a manager whose snapshot is stamped FRESH on read.

    The harness clock is FROZEN while the chain/Greek freshness bound is measured
    against real time, so a slow (or loaded) run would otherwise read its own
    untouched snapshot as stale between proposals. Only the timestamps move: the
    contracts and Greeks are exactly the harness's own.
    """

    class _FreshOptionsManager(base):
        def get_snapshot(self, underlying):
            payload = super().get_snapshot(underlying)
            now = datetime.now(timezone.utc)
            payload["updated_at"] = now
            for expiry_payload in payload["per_expiry"].values():
                for row in expiry_payload["rows"]:
                    for key in ("ce", "pe"):
                        packet = row.get(key)
                        if isinstance(packet, dict):
                            row[key] = {**packet, "updated_at": now}
            return payload

    monkeypatch.setattr(phase2b_routes, "_FakeOptionsManager", _FreshOptionsManager)
    return _FreshOptionsManager


def _owner_row_count(factory, option_run_id: str) -> int:
    from sqlalchemy import text

    with factory() as session:
        return int(
            session.execute(
                text(
                    "SELECT count(*) FROM public.option_protection_owners "
                    "WHERE option_run_id = :run"
                ),
                {"run": option_run_id},
            ).scalar_one()
        )


def _worker_run_state(factory, run_id: str) -> dict:
    import json

    from sqlalchemy import text

    with factory() as session:
        raw = session.execute(
            text(
                "SELECT runtime_state_json FROM public.algo_worker_runs "
                "WHERE strategy_run_id = :run"
            ),
            {"run": run_id},
        ).scalar_one()
    return raw if isinstance(raw, dict) else json.loads(raw or "{}")


def _close_worker_run(factory, run_id: str) -> None:
    from sqlalchemy import text

    with factory() as session:
        session.execute(
            text(
                "UPDATE public.algo_worker_runs SET status = 'closed' "
                "WHERE strategy_run_id = :run"
            ),
            {"run": run_id},
        )
        session.commit()


def _revoke_worker_token(factory, run_id: str) -> None:
    from sqlalchemy import text

    with factory() as session:
        session.execute(
            text(
                "UPDATE public.algo_worker_tokens SET status = 'revoked' "
                "WHERE token_id = (SELECT token_id FROM public.algo_worker_runs "
                "WHERE strategy_run_id = :run)"
            ),
            {"run": run_id},
        )
        session.commit()


def _worker_token_status(factory, run_id: str) -> str:
    from sqlalchemy import text

    with factory() as session:
        return str(
            session.execute(
                text(
                    "SELECT t.status FROM public.algo_worker_tokens t "
                    "JOIN public.algo_worker_runs r ON r.token_id = t.token_id "
                    "WHERE r.strategy_run_id = :run"
                ),
                {"run": run_id},
            ).scalar_one()
        )


def _arm_stale_protection(
    factory, run_id: str, *, stale_sec: int = 60, heartbeat_age: int = 3600
) -> None:
    """Turn ON the run's own stale-exit protection and make its child stale.

    The structure identity was frozen at ENTRY (same transaction as the owner
    row). This only adds the hosted operation block and ages the heartbeat, which
    is exactly the run state a dead hosted child leaves behind: the owner row
    still carries the frozen policy, and the loop reads the run's own config.
    """
    import json

    from sqlalchemy import text

    state = _worker_run_state(factory, run_id)
    protection = dict(state.get("backend_protection") or {})
    protection["enabled"] = True
    protection["mode"] = "exposure"
    protection["version"] = int(protection.get("version") or 1)
    protection["operations"] = {
        "exit_on_worker_stale": True,
        "worker_stale_sec": int(stale_sec),
    }
    state["backend_protection"] = protection
    with factory() as session:
        session.execute(
            text(
                "UPDATE public.algo_worker_runs SET runtime_state_json = CAST(:state AS jsonb), "
                "last_heartbeat_at = NOW() - make_interval(secs => :age) "
                "WHERE strategy_run_id = :run"
            ),
            {"state": json.dumps(state), "age": int(heartbeat_age), "run": run_id},
        )
        session.commit()


def _seed_unresolved_stage_claim(factory, option_run_id: str) -> str:
    """A durable PRE-SEND claim the platform committed and never resolved."""
    from backend.options.execution.durable_store import DurableOptionRunStore

    digest = uuid.uuid4().hex[:20]
    DurableOptionRunStore(session_factory=factory).record_orders(
        option_run_id,
        [
            {
                "stage_digest": digest,
                "attempt": 1,
                "state": "sending",
                "idempotency_key": f"staged-structure-exit:{option_run_id}:{digest}:a1",
                "legs": [],
                "source": "hosted_option_protection",
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
        ],
    )
    return digest


def _submitted_stage_legs(factory, option_run_id: str) -> list[dict]:
    """The newest SETTLED stage's claimed legs, as production recorded them."""
    from backend.options.execution.durable_store import DurableOptionRunStore

    run = DurableOptionRunStore(session_factory=factory).get_run(option_run_id)
    stages = [
        dict(row)
        for row in (run.orders or [])
        if row.get("stage_digest") and str(row.get("state")) == "submitted"
    ]
    assert stages, run.orders
    stages.sort(key=lambda row: str(row.get("resolved_at") or ""))
    return [dict(leg or {}) for leg in (stages[-1].get("legs") or [])]


class _BasketResult:
    """The orders service's basket response shape, without a broker."""

    def __init__(self, results) -> None:
        self._payload = {"status": "success", "results": list(results), "errors": []}

    def model_dump(self, mode="json"):
        _ = mode
        return self._payload


class _ProtectionBasket:
    """The live basket boundary with ONLY the broker call faked.

    The production structure submitter builds the platform's own attribution and
    basket; this stands in for ``OrdersService.place_basket``'s broker write and
    nothing else, and records the durable pre-send row the platform's own fence
    reads after a crash.
    """

    def __init__(self, factory) -> None:
        self.factory = factory
        self.placed: list[dict] = []

    async def place_basket(
        self, kite, request, corr_id, *, session_id, idempotency_key, response=None
    ):
        from sqlalchemy import text

        _ = (kite, corr_id, response, session_id)
        results = []
        for index, order in enumerate(request.orders):
            attribution = dict(getattr(order, "attribution", None) or {})
            order_id = f"OID-PROT-{len(self.placed) + 1}"
            # Pydantic enum members stringify as ``TransactionType.BUY``; the
            # broker boundary's plain value is what a real basket carries.
            side = str(
                getattr(order.transaction_type, "value", order.transaction_type) or ""
            ).rsplit(".", 1)[-1].upper()
            order_type = str(
                getattr(order.order_type, "value", order.order_type) or ""
            ).rsplit(".", 1)[-1].upper()
            self.placed.append(
                {
                    "index": index,
                    "tradingsymbol": str(order.tradingsymbol),
                    "transaction_type": side,
                    "quantity": int(order.quantity),
                    "autoslice": bool(order.autoslice),
                    "order_type": order_type,
                    "price": getattr(order, "price", None),
                    "idempotency_key": idempotency_key,
                    "attribution": attribution,
                }
            )
            with self.factory() as session:
                session.execute(
                    text(
                        "INSERT INTO public.live_order_intents "
                        "(intent_id, client_order_ref, account_id, strategy_run_id, "
                        " strategy_family, strategy_name, entry_surface, "
                        " broker_order_id, idempotency_key, execution_mode, status) "
                        "VALUES (:iid, :ref, :account, :run, 'options_strategy', "
                        " 'c12-s5', 'hosted_option_protection', :oid, :key, 'live', "
                        " 'placed')"
                    ),
                    {
                        "iid": f"lint_{uuid.uuid4().hex[:8]}",
                        "ref": str(attribution.get("client_order_ref") or ""),
                        "account": str(attribution.get("account_ref") or ""),
                        "run": str(attribution.get("strategy_run_id") or ""),
                        "oid": order_id,
                        "key": idempotency_key,
                    },
                )
                session.commit()
            results.append(
                {"index": index, "order_id": order_id, "status": "success", "error": None}
            )
        return _BasketResult(results)


def _protection_runtime(factory, basket, *, now_fn=None):
    """The production protection loop, with only the broker write faked."""
    from types import SimpleNamespace

    from backend.api.repositories.algo_worker_repo import SqlAlchemyAlgoWorkerRepository
    from backend.api.routers import worker_shared
    from backend.api.services.protection_runtime import (
        WorkerProtectionRuntime,
        submit_worker_protection_exit,
        submit_worker_protection_structure_exit,
    )

    repo = SqlAlchemyAlgoWorkerRepository(factory)
    app = SimpleNamespace(
        state=SimpleNamespace(
            algo_worker_orders_service=basket,
            algo_worker_repository=repo,
        )
    )
    request = SimpleNamespace(headers={}, app=app, is_disconnected=lambda: False)

    async def _pnl(run):
        _ = run
        return {"legs": []}

    async def _structure_exit(run, state):
        original = getattr(worker_shared, "_load_live_kite_for_account", None)
        worker_shared._load_live_kite_for_account = lambda scope: {"scope": scope}
        try:
            return await submit_worker_protection_structure_exit(request, run, state)
        finally:
            if original is not None:
                worker_shared._load_live_kite_for_account = original

    return WorkerProtectionRuntime(
        repo=repo,
        pnl_loader=_pnl,
        exit_submitter=lambda run, state: submit_worker_protection_exit(request, run, state),
        structure_exit_submitter=_structure_exit,
        now_fn=now_fn or (lambda: datetime.now(timezone.utc)),
        squareoff_schedule={"NFO:MIS": "15:25"},
    )


def _ingest_protection_fill(
    factory,
    *,
    account_id,
    order_id,
    trade_id,
    quantity,
    side,
    symbol,
    token,
    product="NRML",
    terminal=True,
):
    """The ORDINARY ingestion artifact for a platform protection order's fill."""
    from sqlalchemy import text

    status = "COMPLETE" if terminal else "OPEN"
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO public.order_trade_fills (account_id, trade_id, order_id, "
                " instrument_token, exchange, tradingsymbol, product, transaction_type, "
                " quantity, price, fill_timestamp, applied_to_position) "
                "VALUES (:account, :tid, :oid, :token, 'NFO', :symbol, :product, :side, "
                " :qty, 100.0, NOW(), true)"
            ),
            {
                "account": account_id,
                "tid": trade_id,
                "oid": order_id,
                "token": int(token),
                "symbol": symbol,
                "product": str(product).upper(),
                "side": side,
                "qty": int(quantity),
            },
        )
        session.execute(
            text(
                "INSERT INTO public.order_state_projection (account_id, order_id, "
                " latest_status, latest_event_timestamp, last_seen_filled_quantity, "
                " dirty_for_trade_sync, needs_reconcile, terminal, exchange, "
                " tradingsymbol, instrument_token, product, transaction_type, updated_at) "
                "VALUES (:account, :oid, :status, NOW(), :qty, false, false, :terminal, "
                " 'NFO', :symbol, :token, :product, :side, NOW()) "
                "ON CONFLICT (account_id, order_id) DO UPDATE SET latest_status = :status, "
                " last_seen_filled_quantity = :qty, terminal = :terminal"
            ),
            {
                "account": account_id,
                "oid": order_id,
                "status": status,
                "terminal": bool(terminal),
                "qty": int(quantity),
                "symbol": symbol,
                "token": int(token),
                "product": str(product).upper(),
                "side": side,
            },
        )
        session.commit()


class _S5Clock:
    """A moveable clock for the protection loop's own claim window."""

    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds: int) -> None:
        self.now = self.now + timedelta(seconds=int(seconds))


_S5_TOKEN_BY_SYMBOL = {
    OPT_SHORT: OPT_SHORT_TOKEN,
    OPT_HEDGE: OPT_HEDGE_TOKEN,
    "NIFTY26NOV25000CE": 900003,
    "NIFTY26NOV30000CE": 900004,
}


@pytest.mark.asyncio
async def test_live_entry_claims_exactly_one_active_owner_row(pg, live_env, monkeypatch):
    """C1.2 S5 (1): a LIVE entry claims its protection owner with the run.

    The owner row, the run and its binding edge are written in ONE transaction by
    the shared ``_create_entry_run_atomically`` hook, so a live run can never read
    as "active" without an owner - and the worker run carries the SAME frozen
    structure identity the owner row does.
    """
    _fresh_options_manager(monkeypatch)
    _seed_catalog(pg["factory"])
    clock = _clock()
    broker_calls = []

    class _Broker:
        calls = []

        async def handle(self, intent, *, context=None):
            broker_calls.append(intent)
            response = {"result": {"order_id": f"O-C12S5-ENTRY-{len(broker_calls)}"}}
            self.calls.append((intent, response))
            return response

    client, _executor, attempt, plan = await _prepare_and_enter_option(
        pg, live_env, _Broker(), clock, broker_calls=broker_calls
    )
    try:
        run = _option_run(pg["factory"], plan["plan_id"])
        assert _owner_row_count(pg["factory"], run["id"]) == 1
        owner = _owner_row(pg["factory"], run["id"])
        assert owner["state"] == "active", owner
        assert owner["owner_run_id"] == attempt["run_id"], owner
        assert int(owner["owner_epoch"]) == 1, owner
        assert owner["policy"]["structure_digest"], owner["policy"]
        assert owner["policy_version"], owner

        config = dict(
            (_worker_run_state(pg["factory"], attempt["run_id"]).get("backend_protection") or {})
        )
        assert str((config.get("structure") or {}).get("structure_digest") or "") == (
            owner["policy"]["structure_digest"]
        ), config
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_live_owner_handover_moves_the_structure_at_successor_creation(
    pg, live_env, monkeypatch
):
    """C1.2 S5 (4): a LIVE successor inherits the structure exactly as paper does.

    The successor's hosted-run creation reads ACTIVE owner rows for the job's own
    ``execution_environment`` (``'live'`` here, from ``job.execution_mode``),
    finds the ended predecessor's structure, and moves it in ONE epoch CAS that
    carries the SAME frozen policy. The one-row invariant holds across the move.
    """
    from backend.api.services import hosted_lifecycle
    from backend.api.repositories.algo_worker_repo import SqlAlchemyAlgoWorkerRepository
    from backend.strategies.repository import SqlAlchemyStrategyRepository

    _fresh_options_manager(monkeypatch)
    _seed_catalog(pg["factory"])
    clock = _clock()
    broker_calls = []

    class _Broker:
        calls = []

        async def handle(self, intent, *, context=None):
            broker_calls.append(intent)
            response = {"result": {"order_id": f"O-C12S5-HO-{len(broker_calls)}"}}
            self.calls.append((intent, response))
            return response

    client, _executor, attempt, plan = await _prepare_and_enter_option(
        pg, live_env, _Broker(), clock, broker_calls=broker_calls
    )
    try:
        run = _option_run(pg["factory"], plan["plan_id"])
        before = _owner_row(pg["factory"], run["id"])
        assert before["owner_run_id"] == attempt["run_id"], before
        assert _owner_row_count(pg["factory"], run["id"]) == 1

        # A continuation closes the predecessor; only then is its structure
        # inherited by the successor's creation.
        _close_worker_run(pg["factory"], attempt["run_id"])

        class _JobStub:
            id = "hsj-c12-s5"
            strategy_id = attempt["strategy_id"]
            account_scope = live_env["account_scope"]
            execution_mode = "live"

        repo = SqlAlchemyStrategyRepository(pg["factory"])
        store = hosted_lifecycle._protection_owner_store(repo)
        inherited = await hosted_lifecycle._handover_protection_rows(
            strategy_repo=repo,
            worker_repo=SqlAlchemyAlgoWorkerRepository(pg["factory"]),
            job=_JobStub(),
        )
        assert [str(row["option_run_id"]) for row in inherited] == [run["id"]], inherited

        successor_run_id = f"run-c12s5-successor-{uuid.uuid4().hex[:6]}"
        refused = await hosted_lifecycle._transfer_inherited_structures(
            owner_store=store,
            rows=inherited,
            successor_run_id=successor_run_id,
        )
        assert refused is None, refused

        moved = _owner_row(pg["factory"], run["id"])
        assert moved["owner_run_id"] == successor_run_id, moved
        assert int(moved["owner_epoch"]) == int(before["owner_epoch"]) + 1, moved
        # The handover moves the OWNER only: the structure and its policy ride along.
        assert moved["policy_version"] == before["policy_version"], moved
        assert _owner_row_count(pg["factory"], run["id"]) == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_a_superseded_live_owner_blocks_an_increase_by_name(pg, live_env, monkeypatch):
    """C1.2 S5 (2): the OTHER half of the same split, on the LIVE lane.

    A caller that IS the structure's own OPEN run, but which the owner row has
    moved PAST, may not grow the structure. The refusal is BY NAME at the gate
    that knows the caller (execution), and nothing is dispatched. Reduce-only and
    exit work stay admissible under an UNKNOWN owner, which the existing
    ``OWNER_UNKNOWN`` twin proves; a SUPERSEDED caller is not the same thing.
    """
    _fresh_options_manager(monkeypatch)
    _seed_catalog(pg["factory"])
    clock = _clock()
    broker_calls = []

    class _Broker:
        calls = []

        async def handle(self, intent, *, context=None):
            broker_calls.append(intent)
            response = {"result": {"order_id": f"O-C12S5-SUPERSEDED-{len(broker_calls)}"}}
            self.calls.append((intent, response))
            return response

    client, _executor, attempt, plan = await _prepare_and_enter_option(
        pg, live_env, _Broker(), clock, broker_calls=broker_calls
    )
    try:
        from backend.options.protection.ownership import OptionProtectionOwnerStore

        run = _option_run(pg["factory"], plan["plan_id"])
        before = _owner_row(pg["factory"], run["id"])
        store = OptionProtectionOwnerStore(session_factory=pg["factory"])
        store.transfer(
            run["id"],
            f"run-c12s5-successor-{uuid.uuid4().hex[:6]}",
            int(before["owner_epoch"]),
            before["policy"],
            before["policy_version"],
        )

        generation = int(run["metadata"].get("structure_generation") or 1)
        grow = await _submit_proposal(
            client,
            attempt,
            _adjust_payload(
                option_run_id=run["id"],
                based_on_generation=generation,
                structure_units=2,
            ),
            account_scope=live_env["account_scope"],
        )
        assert grow.status_code < 400, grow.text
        grow_plan = grow.json()["plan"]
        assert grow_plan is not None, grow.text
        reserved = await client.post(
            f"/api/strategies/{attempt['strategy_id']}/plans/{grow_plan['plan_id']}/reserve"
        )
        assert reserved.status_code < 400, reserved.text
        published = await client.post(
            f"/api/strategies/{attempt['strategy_id']}/positions/rebuild?environment=live"
        )
        assert published.status_code < 400, published.text
        approved = await client.post(
            f"/api/strategies/{attempt['strategy_id']}/plans/{grow_plan['plan_id']}/approval",
            json={"reservation_id": reserved.json()["reservation_id"], "validity_seconds": 3600},
        )
        assert approved.status_code < 400, approved.text
        executed = await client.post(
            f"/api/strategies/{attempt['strategy_id']}/plans/{grow_plan['plan_id']}/execute"
        )
        assert executed.status_code == 409, executed.text
        assert executed.json()["detail"]["rejection_reason"] == (
            "OPTION_PROTECTION_OWNER_CONFLICT"
        ), executed.text
        assert len(broker_calls) == 2, "a superseded increase placed an order"
        # The losing side changes NOTHING: the successor still owns the structure.
        after = _option_run(pg["factory"], plan["plan_id"])
        assert after["status"] == "entered", after
        assert int(after["metadata"].get("structure_generation") or 1) == generation
        assert _owner_row(pg["factory"], run["id"])["owner_run_id"].startswith(
            "run-c12s5-successor-"
        )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_an_unresolved_protective_stage_blocks_a_live_adjust(
    pg, live_env, monkeypatch
):
    """C1.2 S5 (3): a stage the platform committed owns the run's next transition.

    The owner row's ``action_state`` is a HINT; the run's own durable stage
    records are the EVIDENCE. A ``sending`` claim still refuses the live adjust BY
    NAME, and nothing is dispatched.
    """
    _fresh_options_manager(monkeypatch)
    _seed_catalog(pg["factory"])
    clock = _clock()
    broker_calls = []

    class _Broker:
        calls = []

        async def handle(self, intent, *, context=None):
            broker_calls.append(intent)
            response = {"result": {"order_id": f"O-C12S5-UNRES-{len(broker_calls)}"}}
            self.calls.append((intent, response))
            return response

    client, _executor, attempt, plan = await _prepare_and_enter_option(
        pg, live_env, _Broker(), clock, broker_calls=broker_calls
    )
    try:
        run = _option_run(pg["factory"], plan["plan_id"])
        # The owner row says the structure is idle...
        assert _owner_row(pg["factory"], run["id"])["owner_run_id"] == attempt["run_id"]
        # ...while the run's own record says a protective stage is in flight.
        _seed_unresolved_stage_claim(pg["factory"], run["id"])

        generation = int(run["metadata"].get("structure_generation") or 1)
        grow = await _submit_proposal(
            client,
            attempt,
            _adjust_payload(
                option_run_id=run["id"],
                based_on_generation=generation,
                structure_units=2,
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
            "OPTION_PROTECTIVE_EXIT_UNRESOLVED"
        ), refused.text
        assert len(broker_calls) == 2, "an adjust was dispatched beside an unresolved stage"
        # The run did not move and the claim is still the run's own truth.
        after = _option_run(pg["factory"], plan["plan_id"])
        assert after["status"] == "entered", after
        assert int(after["metadata"].get("structure_generation") or 1) == generation
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_live_protective_exit_restart_never_creates_a_second_stage(
    pg, live_env, monkeypatch
):
    """C1.2 S5 (3): a restart (or a dead child's replacement) duplicates no stage.

    The platform's staged exit resolves the run through the OWNER ROW, commits its
    pre-send claim, and closes the short first. A second evaluation - a fresh
    runtime, a fresh clock, the same durable evidence - reconciles the committed
    stage and sends NOTHING.
    """
    _fresh_options_manager(monkeypatch)
    _seed_catalog(pg["factory"])
    clock = _clock()
    broker_calls = []

    class _Broker:
        calls = []

        async def handle(self, intent, *, context=None):
            broker_calls.append(intent)
            response = {"result": {"order_id": f"O-C12S5-RESTART-{len(broker_calls)}"}}
            self.calls.append((intent, response))
            return response

    client, _executor, attempt, plan = await _prepare_and_enter_option(
        pg, live_env, _Broker(), clock, broker_calls=broker_calls
    )
    try:
        run = _option_run(pg["factory"], plan["plan_id"])
        _arm_stale_protection(pg["factory"], attempt["run_id"])
        _revoke_worker_token(pg["factory"], attempt["run_id"])
        assert _worker_token_status(pg["factory"], attempt["run_id"]) == "revoked"

        basket = _ProtectionBasket(pg["factory"])
        protection_clock = _S5Clock()
        runtime = _protection_runtime(pg["factory"], basket, now_fn=protection_clock)

        first = await runtime.evaluate_once()
        assert first == {"evaluated": 1, "triggered": 1, "errors": 0}, first
        assert [
            (row["tradingsymbol"], row["transaction_type"], row["quantity"])
            for row in basket.placed
        ] == [(OPT_SHORT, "BUY", LOT)], basket.placed
        assert all(order["autoslice"] is True for order in basket.placed), basket.placed
        assert _owner_row(pg["factory"], run["id"])["action_state"] == "staging"

        # A RESTART: the exit claim's own window has elapsed, so the loop
        # re-evaluates - and the durable stage it already committed is reconciled
        # instead of re-sent.
        protection_clock.advance(120)
        restarted_basket = _ProtectionBasket(pg["factory"])
        restarted = _protection_runtime(
            pg["factory"], restarted_basket, now_fn=protection_clock
        )
        again = await restarted.evaluate_once()
        assert again["errors"] == 0, again
        assert restarted_basket.placed == [], restarted_basket.placed

        # The same holds for a THIRD pass and for the ORIGINAL boundary: no stage
        # was ever sent twice.
        protection_clock.advance(120)
        await runtime.evaluate_once()
        assert len(basket.placed) == 1, basket.placed
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
    _fresh_options_manager(monkeypatch, base=_RollOptionsManager)
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
async def test_live_option_resize_rejected_hedge_releases_nothing(pg, live_env, monkeypatch):
    """A REJECTED hedge increase is not a position: the dependent short waits."""
    _fresh_options_manager(monkeypatch)
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
    pg, live_env, monkeypatch
):
    """Unknown ownership refuses an increasing adjust; a reduce-only one runs."""
    # Stamp the chain fresh on read: the harness clock is frozen while the chain
    # freshness bound is real-time, so a slow run would read its snapshot stale.
    _fresh_options_manager(monkeypatch)
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
    _fresh_options_manager(monkeypatch, base=_RollOptionsManager)
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
