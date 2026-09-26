"""C1.1 S3: two staged plans competing for ONE account cash balance.

The account lock (``pg_advisory_xact_lock("admission:<account>")``) is what makes a
staged increase authorization atomic against every other claim on the same account.
This file proves it on a disposable PostgreSQL server, with its OWN scratch
database per test (never the shared ``kite_test``).

Boundary: the fake broker handles every "broker" call and there is no network and
no real order. The account's available CASH is a fixture (the deployment under test
has no broker session to read), and it is modelled the way a real funds read
behaves: every unfilled BUY this account already has outstanding blocks its
notional, so the money the FIRST plan spends is gone for the second. That model is
read from the platform's own durable claims - not from the test's memory - so it is
the same fact a broker's ``available.cash`` reports back.

The third scenario pins the ORDER of that read against the reservation account lock:
the read that authorizes a staged buy must happen while the account lock is held
(``backend/strategies/live_adapter.py`` ``release_step``), never before it - or a
competitor's confirmed fill can move cash out of the account between the read and
the authorization.

Everything else is production code: the operator routes, the supervisor credential,
the hosted-attempt authority, the proposal/compiler, the parent protocol, the
per-leg claims, the reservation ledger, the authorization and the release pass.
"""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from datetime import datetime, timezone

import pytest

# The suite drives its own clock in a disposable database with no imported NSE
# calendar, so the market session is supplied as EVIDENCE through the production
# seam rather than guessed (see tests/support/market_session_stub.py).
from tests.support.market_session_stub import open_market_session  # noqa: F401

from tests.integration.test_hosted_live_phase2a_routes_postgres import (
    APP_ADMIN_PASSWORD,
    APP_JWT_SECRET,
    INFY,
    INFY_TOKEN,
    RELIANCE,
    RELIANCE_TOKEN,
    SUPERVISOR_CREDENTIAL,
    _Env,
    _FakeBroker,
    _claim,
    _ingest_fill,
    _operator_client,
    _open_position,
    _prepare_live_attempt,
    _rebalance_plan,
    _seed_catalog,
    _submit_proposal,
)

#: The disposable test server's ADMIN DSN. The race suite creates its OWN database
#: through it: the shared ``kite_test`` database is used by other agents at the same
#: time and would collide. Never point this anywhere else.
PG_ADMIN = os.environ.get(
    "C11_RACE_PG_ADMIN", "postgresql://postgres:testonly@127.0.0.1:15433/postgres"
)

#: The frozen reference/quote price the fake market reader answers with.
REFERENCE_PRICE = 1500.0


def _create_db() -> tuple[str, str]:
    import psycopg2

    name = f"kite_c11_race_{uuid.uuid4().hex[:10]}"
    conn = psycopg2.connect(PG_ADMIN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    conn.close()
    return name, f"{PG_ADMIN.rpartition('/')[0]}/{name}"


def _drop_db(name: str) -> None:
    import psycopg2

    conn = psycopg2.connect(PG_ADMIN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    conn.close()


@pytest.fixture
def pg():
    """A FRESH disposable database per test, migrated to head through alembic."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    from tests.integration import test_hosted_live_phase2a_routes_postgres as phase2a

    # The pinned catalog identity keys are globally unique, so this suite needs its
    # own catalog in its own database - never another suite's cached ids.
    phase2a._CATALOG.clear()
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
    """One live account/app environment, over this suite's own database."""
    broker_user_id = f"c11race{uuid.uuid4().hex[:6]}"
    account_scope = f"kite:{broker_user_id}"
    saved = {}
    for key in (
        "APP_ADMIN_PASSWORD_HASH",
        "APP_ADMIN_PASSWORD_HASH_B64",
        "APP_ADMIN_PASSWORD_HASH_FILE",
    ):
        saved[key] = os.environ.pop(key, None)
    os.environ.update(
        {
            "DATABASE_URL": pg["dsn"],
            "APP_ENV": "development",
            "APP_ALLOW_INSECURE_DEV_AUTH": "true",
            "APP_ADMIN_USERNAME": "admin",
            "APP_ADMIN_PASSWORD": APP_ADMIN_PASSWORD,
            "APP_JWT_SECRET": APP_JWT_SECRET,
            "JWT_SECRET": APP_JWT_SECRET,
            "HOSTED_SUPERVISOR_CREDENTIAL": SUPERVISOR_CREDENTIAL,
            "HOSTED_STRATEGY_ACCOUNT_SCOPES": account_scope,
            "HOSTED_LIVE_ENABLED": "true",
            "HOSTED_LIVE_LANES": "cnc,mis,futures,options",
        }
    )
    # The live session reader resolves the account's broker session from the
    # platform's own store; this is that row, not a bypass of the reader.
    from sqlalchemy import text

    with pg["factory"]() as session:
        session.execute(
            text(
                "INSERT INTO public.kite_sessions "
                "(session_id, access_token, broker_user_id, created_at) "
                "VALUES ('system', 'c11-race-access-token', :uid, NOW())"
            ),
            {"uid": broker_user_id},
        )
        session.commit()
    try:
        yield {"account_scope": account_scope, "broker_user_id": broker_user_id}
    finally:
        for key in (
            "APP_ADMIN_PASSWORD",
            "APP_JWT_SECRET",
            "JWT_SECRET",
            "HOSTED_STRATEGY_ACCOUNT_SCOPES",
            "HOSTED_LIVE_ENABLED",
            "HOSTED_LIVE_LANES",
            "HOSTED_SUPERVISOR_CREDENTIAL",
            "APP_ADMIN_USERNAME",
            "APP_ENV",
            "APP_ALLOW_INSECURE_DEV_AUTH",
        ):
            os.environ.pop(key, None)
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


class _AccountCash:
    """The account's ONE cash balance, as a broker's funds read reports it.

    ``usable`` is what is LEFT after every unfilled BUY this account already has
    outstanding has blocked its notional - read from the durable claims, so the
    first plan's accepted order really does take the money away from the second.
    """

    def __init__(self, factory, *, account_scope: str, balance_inr: float, clock) -> None:
        self.factory = factory
        self.account_scope = str(account_scope)
        self.balance_inr = float(balance_inr)
        self._clock = clock

    def blocked_inr(self) -> float:
        from sqlalchemy import text

        with self.factory() as session:
            value = session.execute(
                text(
                    """
                    SELECT COALESCE(SUM((delta_snapshot ->> 'notional_inr')::numeric), 0)
                    FROM public.live_plan_submissions
                    WHERE account_id = :account
                      AND state IN (
                          'pending', 'partial', 'finalizing', 'rejecting', 'uncertain'
                      )
                      AND delta_snapshot ->> 'increases_exposure' = 'true'
                      AND broker_order_ids <> '[]'::jsonb
                    """
                ),
                {"account": self.account_scope},
            ).scalar()
        return float(value or 0.0)

    def read(self, _account=None, _plan=None):
        """The production readers are called as (account_id, plan) or with none."""
        return {
            "usable": max(0.0, self.balance_inr - self.blocked_inr()),
            "as_of": self._clock().isoformat(),
            "account_scope": self.account_scope,
            "source": "race-suite-account-cash",
        }


class _CashAfterFills:
    """The broker's available cash: the balance MINUS what confirmed fills spent.

    The other half of a real funds read, and the half the overspend path hides in:
    an order that is merely outstanding does not reduce this figure (holding cash
    for it is the reservation's job), but a BUY that has FILLED has really moved
    cash out of the account. So a plan that fills shrinks the next plan's usable
    cash AND stops being an unfilled commitment at the same moment - which is why
    the read and the authorization have to happen under the one account lock.
    """

    def __init__(self, factory, *, account_scope: str, balance_inr: float, clock) -> None:
        self.factory = factory
        self.account_scope = str(account_scope)
        #: Re-anchored by the test between phases; see the scenario docstring.
        self.balance_inr = float(balance_inr)
        self._clock = clock

    def spent_inr(self) -> float:
        from sqlalchemy import text

        with self.factory() as session:
            value = session.execute(
                text(
                    """
                    SELECT COALESCE(SUM((delta_snapshot ->> 'notional_inr')::numeric), 0)
                    FROM public.live_plan_submissions
                    WHERE account_id = :account
                      AND state = 'filled'
                      AND delta_snapshot ->> 'increases_exposure' = 'true'
                    """
                ),
                {"account": self.account_scope},
            ).scalar()
        return float(value or 0.0)

    def read(self, _account=None, _plan=None):
        return {
            "usable": max(0.0, self.balance_inr - self.spent_inr()),
            "as_of": self._clock().isoformat(),
            "account_scope": self.account_scope,
            "source": "race-suite-cash-after-fills",
        }


def _reliance_buy_calls(env) -> list:
    """Every fake-broker call that would place a RELIANCE buy."""
    return [
        call
        for call in env.broker.calls
        if call[0].payload["order"]["transaction_type"] == "BUY"
        and call[0].payload["order"]["tradingsymbol"] == RELIANCE
    ]


@pytest.fixture(autouse=True)
def _claim_time_funds_boundary(monkeypatch):
    """The broker funds READ the reserve route uses.

    Only the CLAIM-time read is stubbed with a roomy figure: a staged plan defers
    its increase from claim cash by design, so this is not the number under test.
    The release-time read is the suite's own cash model (installed on the executor).
    """
    from backend.api.routers import strategies as strategies_module

    monkeypatch.setattr(
        strategies_module,
        "_live_margin_evidence",
        lambda _scope, _plan: {
            "usable": 5_000_000.0,
            "as_of": datetime.now(timezone.utc).isoformat(),
        },
    )


def _no_release_consumer(env):
    """Ingestion WITHOUT the sequence pass: fills land, nothing is released yet."""
    from backend.strategies.live_ingestion import LiveOutcomeConsumer

    return LiveOutcomeConsumer(
        session_factory=env.factory, clock=env.clock, sequence_releaser=None
    )


def _reservation_events(env, plan_id: str, event: str) -> list:
    from sqlalchemy import text

    with env.factory() as session:
        return list(
            session.execute(
                text(
                    "SELECT detail FROM public.strategy_reservation_events e "
                    "JOIN public.strategy_reservations r "
                    "  ON r.reservation_id = e.reservation_id "
                    "WHERE r.plan_id = :plan_id AND e.event = :event"
                ),
                {"plan_id": str(plan_id), "event": str(event)},
            ).scalars()
        )


async def _staged_pair_both_reductions_filled(env, revision_id, client):
    """Two staged CNC rebalances on ONE account, each with its reduction filled.

    Nothing has been released: ingestion runs without the sequence pass, so BOTH
    dependent buys are still ``withheld`` and hold their reservations when the
    competition starts. The two plans belong to two DIFFERENT strategies, so their
    books (and their book locks) are different - the reservation account lock is the
    only serialization they share.
    """
    plans = []
    for index, order_id in enumerate(("C11R-A-ENTRY", "C11R-B-ENTRY"), start=1):
        attempt = await _prepare_live_attempt(
            client, account_scope=env.account_scope, lease_until=env.lease_until
        )
        await _open_position(client, env, attempt, revision_id, order_id=order_id)
        plan_id, sell_spec, buy_spec, body = await _rebalance_plan(
            client, env, attempt, revision_id
        )
        assert len(body["broker_order_ids"]) == 1, body
        plans.append(
            {
                "attempt": attempt,
                "plan_id": plan_id,
                "sell_spec": sell_spec,
                "buy_spec": buy_spec,
                "sell_order": str(body["broker_order_ids"][0]),
            }
        )
    # BOTH reductions now fill - ingested WITHOUT the sequence pass, so neither
    # dependent buy has been released yet when the competition starts.
    for index, item in enumerate(plans, start=1):
        _ingest_fill(
            env.factory,
            account_id=env.account_scope,
            run_id=item["attempt"]["run_id"],
            order_id=item["sell_order"],
            trade_id=f"TR-C11R-{index}-{uuid.uuid4().hex[:6]}",
            quantity=int(item["sell_spec"]["quantity"]) or 1,
            side="SELL",
            symbol=INFY,
            token=INFY_TOKEN,
        )
    counts = await _no_release_consumer(env).poll_once()
    assert counts["filled"] == 2, counts
    for item in plans:
        claim = _claim(env.factory, item["plan_id"], item["buy_spec"]["step_no"])
        assert claim["state"] == "withheld", dict(claim)
        assert list(claim["broker_order_ids"]) == []
    return plans


def test_two_staged_plans_compete_for_one_account_balance(pg, live_env):
    """S3 §7.4: only the FIRST releasable authorization can win the account's cash.

    The balance covers one increase (plus a little), so the first plan's accepted
    order blocks its notional at the "broker" and the second plan's fresh funds read
    - taken inside its own release transaction under the account lock - no longer
    covers its increase once the other plan's reservation is counted. The second
    plan records ``ACCOUNT_FUNDS_UNSECURED`` and sends NOTHING; exactly one fake
    order is placed; exactly one authorization event exists on the account.
    """
    env = _Env(
        pg,
        live_env,
        broker=_FakeBroker(
            order_ids=(
                "C11R-A-ENTRY",
                "C11R-A-EXIT",
                "C11R-B-ENTRY",
                "C11R-B-EXIT",
                "C11R-A-BUY",
                "C11R-B-BUY",
            )
        ),
    )
    catalog = _seed_catalog(env.factory)
    revision_id = catalog["universe_revision_id"]

    async def _run():
        client = await _operator_client(env.app)
        try:
            plans = await _staged_pair_both_reductions_filled(env, revision_id, client)
            first, second = plans
            requirement = float(first["buy_spec"]["quantity"]) * REFERENCE_PRICE
            assert requirement == float(second["buy_spec"]["quantity"]) * REFERENCE_PRICE
            # ONE balance: enough for one increase and not for the pair. (Strictly
            # between the sum of the two increases and the sum plus one - so the
            # outcome cannot hinge on a rounding coincidence.)
            cash = _AccountCash(
                env.factory,
                account_scope=env.account_scope,
                balance_inr=2.0 * requirement + requirement / 2.0,
                clock=env.clock,
            )
            env.executor._margin_reader = cash.read  # the broker funds boundary

            calls_before = len(env.broker.calls)
            counts = await env.executor.release_sequence()
            assert counts["released"] == 1, counts
            assert counts["blocked"] >= 1, counts
            assert counts["errors"] == 0, counts

            # Exactly ONE dependent buy reached the fake broker.
            buy_calls = [
                call
                for call in env.broker.calls
                if call[0].payload["order"]["transaction_type"] == "BUY"
                and call[0].payload["order"]["tradingsymbol"] == RELIANCE
            ]
            assert len(buy_calls) == 1, [call[0].payload for call in env.broker.calls]
            assert len(env.broker.calls) == calls_before + 1

            # EXACTLY ONE authorization won. Which plan it is depends on the
            # serialization order the pass reached (oldest parent first) - not on
            # identity - so the winner and loser are read back from the claims.
            claims = {
                item["plan_id"]: _claim(
                    env.factory, item["plan_id"], item["buy_spec"]["step_no"]
                )
                for item in plans
            }
            winners = [
                item for item in plans if claims[item["plan_id"]]["state"] == "pending"
            ]
            losers = [
                item for item in plans if claims[item["plan_id"]]["state"] == "withheld"
            ]
            assert len(winners) == 1 and len(losers) == 1, {
                pid: dict(claim) for pid, claim in claims.items()
            }
            winner, loser = winners[0], losers[0]
            winner_claim = claims[winner["plan_id"]]
            loser_claim = claims[loser["plan_id"]]
            assert len(list(winner_claim["broker_order_ids"])) == 1, dict(winner_claim)
            assert list(loser_claim["broker_order_ids"]) == [], dict(loser_claim)
            loser_detail = dict(loser_claim["detail"])
            assert loser_detail["release_blocked"] == "ACCOUNT_FUNDS_UNSECURED", loser_detail
            refusal = dict(loser_detail["release_blocked_detail"])
            assert refusal["authorization_key"] == (
                f"{loser['plan_id']}:{loser['buy_spec']['step_no']}"
            ), refusal
            exceeded = dict(refusal["reservation_error_detail"])
            assert exceeded["scope"] == "account_funds_increase", exceeded
            assert float(exceeded["requested_increase_inr"]) == requirement, exceeded
            assert float(exceeded["competing_commitments_inr"]) == requirement, exceeded
            # The balance the loser read, once the winner's accepted order blocked
            # its notional: one and a half increases - NOT enough for its own
            # increase on top of the other plan's held reservation.
            assert float(exceeded["account_capacity_inr"]) == 1.5 * requirement, exceeded
            assert (
                float(exceeded["competing_commitments_inr"])
                + float(exceeded["requested_increase_inr"])
                > float(exceeded["account_capacity_inr"])
            ), exceeded

            # Exactly ONE authorization event lands on the account, and it is the
            # winner's: the loser's refusal wrote none.
            assert (
                len(_reservation_events(env, winner["plan_id"], "staged_increase_authorized"))
                == 1
            )
            assert (
                _reservation_events(env, loser["plan_id"], "staged_increase_authorized") == []
            )

            # Both reservations are still held: the loser's capacity was never
            # silently released, so an operator still owns the decision.
            assert str(env.executor.ledger.for_plan(winner["plan_id"])["status"]) in (
                "active",
                "renewed",
            )
            assert str(env.executor.ledger.for_plan(loser["plan_id"])["status"]) in (
                "active",
                "renewed",
            )

            # A second pass (and a pass from a fresh executor over the same durable
            # rows) changes NOTHING: no second order, no re-authorization.
            calls_after_competition = len(env.broker.calls)
            again = await env.executor.release_sequence()
            assert again["released"] == 0, again
            assert len(env.broker.calls) == calls_after_competition
            assert _claim(
                env.factory, loser["plan_id"], loser["buy_spec"]["step_no"]
            )["state"] == "withheld"
            return winner["plan_id"], loser["plan_id"]
        finally:
            await client.aclose()

    asyncio.run(_run())


def test_a_confirmed_fill_cannot_be_outrun_by_a_pre_lock_funds_read(pg, live_env):
    """S3 fix: the funds read that authorizes a buy must happen UNDER the account lock.

    This is the overspend path, closed. Strategy A's buy is already authorized and
    outstanding. Strategy B's release reads the account's cash; A's buy then FILLS -
    the cash is really gone AND A's reservation stops being an unfilled commitment -
    and B, taking the account lock afterwards, authorizes against cash A has already
    spent (only another plan's still-held reservation stands in its way, and that
    plan is charged against the STALE figure).

    The interleaving is forced with the lock itself: a driver holds the account
    lock, B's release starts and blocks on it, the driver then records A's confirmed
    fill (which consumes A's reservation), and only then releases the lock. A
    PRE-LOCK funds read has already returned by that point; the fixed read has not
    happened yet. So this test fails against the pre-lock ordering and passes once
    the read is inside the lock.
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool

    env = _Env(
        pg,
        live_env,
        broker=_FakeBroker(
            order_ids=(
                "C11X-A-ENTRY",
                "C11X-A-EXIT",
                "C11X-A-BUY",
                "C11X-B-ENTRY",
                "C11X-B-EXIT",
                "C11X-B-BUY",
            )
        ),
    )
    catalog = _seed_catalog(env.factory)
    revision_id = catalog["universe_revision_id"]

    async def _run():
        client = await _operator_client(env.app)
        try:
            ingestor = _no_release_consumer(env)
            probe = _CashAfterFills(
                env.factory, account_scope=env.account_scope, balance_inr=0.0, clock=env.clock
            )
            cash = _CashAfterFills(
                env.factory, account_scope=env.account_scope, balance_inr=0.0, clock=env.clock
            )

            # -- strategy A first: its buy is authorized and placed BEFORE B's plan
            #    exists, so A's reservation is the account's only commitment.
            a_attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            await _open_position(
                client, env, a_attempt, revision_id, order_id="C11X-A-ENTRY"
            )
            a_plan, a_sell, a_buy, a_body = await _rebalance_plan(
                client, env, a_attempt, revision_id
            )
            assert len(a_body["broker_order_ids"]) == 1, a_body
            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=a_attempt["run_id"],
                order_id=str(a_body["broker_order_ids"][0]),
                trade_id=f"TR-C11X-A-SELL-{uuid.uuid4().hex[:6]}",
                quantity=int(a_sell["quantity"]) or 1,
                side="SELL",
                symbol=INFY,
                token=INFY_TOKEN,
            )
            counts = await ingestor.poll_once()
            assert counts["filled"] == 1, counts
            requirement = float(a_buy["quantity"]) * REFERENCE_PRICE
            # Two and a quarter increases of headroom beyond what the confirmed
            # fills have already spent: enough for A's buy plus the still-held
            # reservation below, not enough once A's buy has really filled.
            cash.balance_inr = probe.spent_inr() + 2.25 * requirement
            env.executor._margin_reader = cash.read
            counts = await env.executor.release_sequence()
            assert counts["released"] == 1, counts
            a_claim = _claim(env.factory, a_plan, a_buy["step_no"])
            assert a_claim["state"] == "pending", dict(a_claim)
            assert len(_reliance_buy_calls(env)) == 1, [
                call[0].payload for call in env.broker.calls
            ]
            a_order = str(list(a_claim["broker_order_ids"])[0])

            # -- a THIRD strategy reserves the same account and never releases: its
            #    held reservation is the only thing left standing between B and the
            #    account's cash once A's reservation is consumed.
            c_attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            c_response = await _submit_proposal(
                client,
                c_attempt,
                {
                    "target_kind": "target_weights",
                    "payload": {
                        "universe_revision_id": revision_id,
                        "product": "CNC",
                        "target_weights": {RELIANCE: 0.04, INFY: 0.0},
                        "reference_prices": {RELIANCE: REFERENCE_PRICE, INFY: REFERENCE_PRICE},
                    },
                },
                account_scope=env.account_scope,
            )
            assert c_response.status_code < 400, c_response.text
            c_plan = c_response.json()["plan"]["plan_id"]
            c_reserved = await client.post(
                f"/api/strategies/{c_attempt['strategy_id']}/plans/{c_plan}/reserve"
            )
            assert c_reserved.status_code < 400, c_reserved.text
            c_reservation = c_reserved.json()
            assert float(c_reservation["reserved_notional_inr"]) == requirement, c_reservation

            # -- strategy B: materialized and funded by its own confirmed reduction,
            #    its buy withheld and NOT yet released (ingestion runs without the
            #    sequence pass, so nothing races the interleaving below).
            b_attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            await _open_position(
                client, env, b_attempt, revision_id, order_id="C11X-B-ENTRY"
            )
            b_plan, b_sell, b_buy, b_body = await _rebalance_plan(
                client, env, b_attempt, revision_id
            )
            assert len(b_body["broker_order_ids"]) == 1, b_body
            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=b_attempt["run_id"],
                order_id=str(b_body["broker_order_ids"][0]),
                trade_id=f"TR-C11X-B-SELL-{uuid.uuid4().hex[:6]}",
                quantity=int(b_sell["quantity"]) or 1,
                side="SELL",
                symbol=INFY,
                token=INFY_TOKEN,
            )
            counts = await ingestor.poll_once()
            assert counts["filled"] == 1, counts
            b_claim = _claim(env.factory, b_plan, b_buy["step_no"])
            assert b_claim["state"] == "withheld", dict(b_claim)
            assert len(_reliance_buy_calls(env)) == 1, [
                call[0].payload for call in env.broker.calls
            ]
            # Re-anchor the balance on the fills confirmed SO FAR (both opening buys),
            # keeping the same headroom. A's buy is OUTSTANDING at this instant, so it
            # has not reduced this figure yet - that is exactly the stale value a
            # pre-lock read would hand back.
            cash.balance_inr = probe.spent_inr() + 2.25 * requirement
            assert cash.read()["usable"] == 2.25 * requirement, cash.read()

            # -- the interleaving the fix is about.
            account_lock_key = f"admission:{env.account_scope}"
            acquired = threading.Event()
            release_lock = threading.Event()

            def _hold_account_lock():
                engine = create_engine(pg["dsn"], poolclass=NullPool)
                try:
                    with engine.connect() as conn:
                        with conn.begin():
                            conn.execute(
                                text(
                                    "SELECT pg_advisory_xact_lock("
                                    "hashtextextended(:key, 0))"
                                ),
                                {"key": account_lock_key},
                            )
                            acquired.set()
                            release_lock.wait(timeout=120)
                finally:
                    engine.dispose()

            outcome: dict = {}

            def _release_b():
                try:
                    outcome["result"] = asyncio.run(env.executor.release_sequence())
                except Exception as exc:  # noqa: BLE001 - reported to the test
                    outcome["error"] = exc

            lock_thread = threading.Thread(target=_hold_account_lock, daemon=True)
            lock_thread.start()
            assert acquired.wait(timeout=15), "the driver never took the account lock"
            worker = threading.Thread(target=_release_b, daemon=True)
            worker.start()
            # B is now blocked on the account lock. With the fix its funds read has
            # NOT happened yet; with a pre-lock read it already returned the
            # pre-fill figure.
            await asyncio.sleep(2.0)
            assert worker.is_alive(), "the release did not wait for the account lock"

            # ... and A's already-authorized buy FILLS while B waits: cash leaves the
            # account and A's reservation becomes a CONSUMED commitment.
            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=a_attempt["run_id"],
                order_id=a_order,
                trade_id=f"TR-C11X-A-BUY-{uuid.uuid4().hex[:6]}",
                quantity=int(a_buy["quantity"]),
                side="BUY",
                symbol=RELIANCE,
                token=RELIANCE_TOKEN,
            )
            counts = await ingestor.poll_once()
            assert counts["filled"] == 1, counts
            assert str(env.executor.ledger.for_plan(a_plan)["status"]) == "consumed"

            release_lock.set()
            worker.join(timeout=60)
            lock_thread.join(timeout=60)
            assert "error" not in outcome, outcome.get("error")
            assert outcome["result"]["released"] == 0, outcome["result"]
            assert outcome["result"]["blocked"] >= 1, outcome["result"]

            # B REFUSED: the cash it read (after the lock) is the post-fill figure,
            # which no longer covers its increase. Nothing was placed for it.
            b_claim = _claim(env.factory, b_plan, b_buy["step_no"])
            assert b_claim["state"] == "withheld", dict(b_claim)
            b_detail = dict(b_claim["detail"])
            assert b_detail["release_blocked"] == "ACCOUNT_FUNDS_UNSECURED", (
                b_detail["release_blocked"],
                {
                    key: value
                    for key, value in dict(
                        dict(b_detail.get("release_blocked_detail") or {}).get("detail") or {}
                    ).items()
                    if key
                    in (
                        "reason_code",
                        "required_inr",
                        "margin_available_inr",
                        "account_available_inr",
                        "staged_financing_lane",
                        "plan_requirement_inr",
                        "message",
                    )
                },
            )
            exceeded = dict(
                dict(b_detail["release_blocked_detail"])["reservation_error_detail"]
            )
            assert exceeded["scope"] == "account_funds_increase", exceeded
            # The one unfilled commitment left is the third strategy's held
            # reservation: A's is CONSUMED, so it no longer counts. The cash the read
            # returned is the post-fill figure, and it no longer covers this buy on
            # top of that commitment.
            assert float(exceeded["competing_commitments_inr"]) == requirement, exceeded
            assert float(exceeded["already_authorized_inr"]) == 0.0, exceeded
            assert float(exceeded["requested_increase_inr"]) == requirement, exceeded
            capacity = float(exceeded["account_capacity_inr"])
            assert requirement <= capacity < 2.0 * requirement, exceeded
            assert (
                float(exceeded["competing_commitments_inr"])
                + float(exceeded["requested_increase_inr"])
                > capacity
            ), exceeded
            # Exactly ONE buy ever reached the fake broker: A's. B's never did.
            assert len(_reliance_buy_calls(env)) == 1, [
                call[0].payload for call in env.broker.calls
            ]
            assert _reservation_events(env, b_plan, "staged_increase_authorized") == []
            # B's capacity was not silently released: an operator still owns it.
            assert str(env.executor.ledger.for_plan(b_plan)["status"]) in (
                "active",
                "renewed",
            )
        finally:
            await client.aclose()

    asyncio.run(_run())


def test_the_staged_authorization_waits_for_the_account_lock(pg, live_env):
    """S3 §3: the per-buy authorization is serialized by the ACCOUNT lock.

    The read-and-claim must be atomic against every other claim on the account, so
    ``authorize_staged_increase`` takes ``pg_advisory_xact_lock("admission:<account>")``
    and re-reads the reservation's authorization state after taking it. This proves
    the lock is real: while another transaction holds the account lock, a competing
    authorization neither completes nor records anything, and it finishes - once,
    correctly - the moment the lock is released.
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool

    env = _Env(
        pg,
        live_env,
        broker=_FakeBroker(order_ids=("C11L-ENTRY", "C11L-EXIT", "C11L-BUY")),
    )
    catalog = _seed_catalog(env.factory)
    revision_id = catalog["universe_revision_id"]

    async def _run():
        client = await _operator_client(env.app)
        try:
            attempt = await _prepare_live_attempt(
                client, account_scope=env.account_scope, lease_until=env.lease_until
            )
            await _open_position(client, env, attempt, revision_id, order_id="C11L-ENTRY")
            plan_id, sell_spec, buy_spec, body = await _rebalance_plan(
                client, env, attempt, revision_id
            )
            assert len(body["broker_order_ids"]) == 1, body
            _ingest_fill(
                env.factory,
                account_id=env.account_scope,
                run_id=attempt["run_id"],
                order_id=str(body["broker_order_ids"][0]),
                trade_id=f"TR-C11L-{uuid.uuid4().hex[:6]}",
                quantity=int(sell_spec["quantity"]) or 1,
                side="SELL",
                symbol=INFY,
                token=INFY_TOKEN,
            )
            counts = await _no_release_consumer(env).poll_once()
            assert counts["filled"] == 1, counts
            assert _claim(env.factory, plan_id, buy_spec["step_no"])["state"] == "withheld"
            requirement = float(buy_spec["quantity"]) * REFERENCE_PRICE

            account_lock_key = f"admission:{env.account_scope}"
            acquired = threading.Event()
            release_lock = threading.Event()

            def _hold_account_lock():
                engine = create_engine(pg["dsn"], poolclass=NullPool)
                try:
                    with engine.connect() as conn:
                        with conn.begin():
                            conn.execute(
                                text(
                                    "SELECT pg_advisory_xact_lock("
                                    "hashtextextended(:key, 0))"
                                ),
                                {"key": account_lock_key},
                            )
                            acquired.set()
                            release_lock.wait(timeout=60)
                finally:
                    engine.dispose()

            holder: dict = {}

            def _authorize():
                try:
                    holder["result"] = env.executor.ledger.authorize_staged_increase(
                        plan_id=plan_id,
                        step_no=buy_spec["step_no"],
                        requirement_inr=requirement,
                        account_capacity_inr=10.0 * requirement,
                        quote={"ltp": REFERENCE_PRICE, "as_of": env.clock().isoformat()},
                        funds_evidence={
                            "usable": 10.0 * requirement,
                            "as_of": env.clock().isoformat(),
                        },
                        actor_id="race-prover",
                    )
                except Exception as exc:  # noqa: BLE001 - reported to the test
                    holder["error"] = exc
                finally:
                    holder["done"] = True

            lock_thread = threading.Thread(target=_hold_account_lock, daemon=True)
            lock_thread.start()
            assert acquired.wait(timeout=15), "the helper never took the account lock"
            worker = threading.Thread(target=_authorize, daemon=True)
            worker.start()
            worker.join(timeout=2.0)
            assert worker.is_alive(), (
                "the authorization did not wait for the account lock"
            )
            # Nothing half-done: no event, no state change, while the lock is held.
            assert _reservation_events(env, plan_id, "staged_increase_authorized") == []
            assert _claim(env.factory, plan_id, buy_spec["step_no"])["state"] == "withheld"

            release_lock.set()
            worker.join(timeout=30)
            lock_thread.join(timeout=30)
            assert holder.get("done") is True, holder
            assert "error" not in holder, holder.get("error")
            assert holder["result"]["authorized"] is True, holder["result"]
            authorized = _reservation_events(env, plan_id, "staged_increase_authorized")
            assert len(authorized) == 1, authorized
            assert float(authorized[0]["increase_inr"]) == requirement, authorized
            assert authorized[0]["authorization_key"] == (
                f"{plan_id}:{buy_spec['step_no']}"
            ), authorized
        finally:
            await client.aclose()

    asyncio.run(_run())
