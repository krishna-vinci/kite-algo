from __future__ import annotations

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.broker_api.orders.bracket_runtime import BracketRuntimeStore


def _sqlite_store() -> tuple[BracketRuntimeStore, sessionmaker]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach_public_schema(dbapi_connection, connection_record):
        _ = connection_record
        cursor = dbapi_connection.cursor()
        cursor.execute("ATTACH DATABASE ':memory:' AS public")
        cursor.close()

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE public.bracket_intents (
                    bracket_intent_id TEXT PRIMARY KEY,
                    strategy_run_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    entry_basket_execution_id TEXT,
                    status TEXT NOT NULL,
                    action_required INTEGER NOT NULL DEFAULT 0,
                    action_reason TEXT,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    closed_at TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE public.bracket_actions (
                    action_id TEXT PRIMARY KEY,
                    bracket_intent_id TEXT NOT NULL,
                    strategy_run_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    claimed_at TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    error_json TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE public.live_order_intents (
                    intent_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    strategy_run_id TEXT NOT NULL,
                    broker_order_id TEXT,
                    bracket_intent_id TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE public.worker_execution_events (
                    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_run_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    basket_execution_id TEXT,
                    event_kind TEXT NOT NULL DEFAULT 'execution',
                    event_source TEXT NOT NULL DEFAULT 'legacy_execution',
                    event_type TEXT NOT NULL,
                    related_resource_type TEXT,
                    related_resource_id TEXT,
                    summary TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
    factory = sessionmaker(bind=engine)
    return BracketRuntimeStore(session_factory=factory), factory


def test_bracket_intent_starts_in_entry_submitting():
    store, factory = _sqlite_store()
    db = factory()
    try:
        intent = store.create_bracket_intent(
            db,
            strategy_run_id="run-1",
            account_id="kite:acct",
            config={"entry": {"quantity": 1}},
        )
        db.commit()
    finally:
        db.close()
    assert intent["status"] == "entry_submitting"


def test_claim_pending_actions_uses_skip_locked_semantics():
    store, factory = _sqlite_store()
    db = factory()
    try:
        intent = store.create_bracket_intent(
            db,
            strategy_run_id="run-1",
            account_id="kite:acct",
            config={"entry": {"quantity": 1}},
            bracket_intent_id="brk-1",
        )
        _ = intent
        action = store.enqueue_action(
            db,
            bracket_intent_id="brk-1",
            strategy_run_id="run-1",
            account_id="kite:acct",
            action_type="place_stoploss",
        )
        claimed = store.claim_pending_actions(db, limit=1)
        db.commit()
    finally:
        db.close()
    assert claimed[0]["action_id"] == action["action_id"]


def test_canonical_entry_full_fill_enqueues_place_stoploss_and_target():
    store, factory = _sqlite_store()
    db = factory()
    try:
        store.create_bracket_intent(
            db,
            strategy_run_id="run-1",
            account_id="kite:acct",
            config={
                "entry": {"quantity": 1, "broker_order_id": "OID-ENTRY-1"},
                "stoploss": {"order_type": "SL"},
                "target": {"order_type": "LIMIT"},
            },
            bracket_intent_id="brk-evt-1",
        )
        db.execute(
            text(
                """
                INSERT INTO public.live_order_intents (intent_id, account_id, strategy_run_id, broker_order_id, bracket_intent_id)
                VALUES ('lint-1', 'kite:acct', 'run-1', 'OID-ENTRY-1', 'brk-evt-1')
                """
            )
        )
        events = store.apply_order_event_observation(
            db,
            canonical_event={
                "account_id": "kite:acct",
                "order_id": "OID-ENTRY-1",
                "status": "COMPLETE",
                "filled_quantity": 1,
            },
        )
        intent = store.get_bracket_intent(db, strategy_run_id="run-1", bracket_intent_id="brk-evt-1") or {}
        actions = store.list_actions_for_intent(db, bracket_intent_id="brk-evt-1")
        db.commit()
    finally:
        db.close()
    assert intent["status"] == "arming_exits"
    assert {a["action_type"] for a in actions} == {"place_stoploss", "place_target"}
    assert events[0]["event_type"] == "bracket.state_changed"
    assert events[0]["event_kind"] == "execution"
    assert events[0]["event_source"] == "bracket_runtime"
    assert events[0]["related_resource_type"] == "bracket_intent"
