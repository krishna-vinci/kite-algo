import os
from pathlib import Path

import psycopg2
from redis.asyncio import from_url as redis_from_url
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
TEST_REDIS_URL = os.getenv("TEST_REDIS_URL")
REPO_ROOT = Path(__file__).resolve().parents[1]


def integration_env_ready() -> bool:
    return bool(TEST_DATABASE_URL and TEST_REDIS_URL)


def create_test_session_factory():
    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    return engine, sessionmaker(autocommit=False, autoflush=False, bind=engine)


def apply_schema() -> None:
    with psycopg2.connect(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute((REPO_ROOT / "schema.sql").read_text())
            cur.execute(
                """
                ALTER TABLE public.worker_execution_events
                  ADD COLUMN IF NOT EXISTS event_kind TEXT NOT NULL DEFAULT 'execution'
                """
            )
            cur.execute(
                """
                ALTER TABLE public.worker_execution_events
                  ADD COLUMN IF NOT EXISTS event_source TEXT NOT NULL DEFAULT 'legacy_execution'
                """
            )
            cur.execute(
                """
                ALTER TABLE public.worker_execution_events
                  ADD COLUMN IF NOT EXISTS related_resource_type TEXT
                """
            )
            cur.execute(
                """
                ALTER TABLE public.worker_execution_events
                  ADD COLUMN IF NOT EXISTS related_resource_id TEXT
                """
            )
            cur.execute(
                """
                ALTER TABLE public.worker_execution_events
                  ADD COLUMN IF NOT EXISTS summary TEXT
                """
            )
        conn.commit()


def truncate_runtime_tables(session_factory) -> None:
    db = session_factory()
    try:
        db.execute(
            text(
                """
                TRUNCATE TABLE
                    canonical_order_events,
                    order_events,
                    ws_order_events,
                    order_state_projection,
                    order_trade_fills,
                    worker_execution_events,
                    worker_live_execution_links,
                    bracket_actions,
                    bracket_intents,
                    basket_execution_legs,
                    basket_executions,
                    live_order_intents,
                    account_positions,
                    kite_sessions
                RESTART IDENTITY CASCADE
                """
            )
        )
        db.commit()
    finally:
        db.close()


async def flush_test_redis():
    client = redis_from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        await client.flushdb()
    finally:
        await client.aclose()
