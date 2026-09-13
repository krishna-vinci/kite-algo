"""The evaluation ownership fence under a concurrent first claim (PostgreSQL).

Why this file exists
--------------------
``claim_evaluation`` reads the fence row with ``SELECT ... FOR UPDATE``. That
locks an existing row, but it locks **nothing when the row does not exist** —
so two workers racing to claim a brand-new subscription both reach the INSERT
and one loses on the primary key.

Losing is the fence working. What was wrong is how it failed: the
``IntegrityError`` escaped, was logged as "evaluation crashed for subscription
…", and aborted the caller's transaction. A first-claim race should be a
quiet loss, never a crash.

SQLite cannot show this: it serialises writers, so the concurrent INSERT that
triggers the race never happens. This is PostgreSQL-only by nature.

    ALERTS_TEST_DATABASE_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \\
        pytest tests/integration/test_evaluation_ownership_postgres.py -q
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from backend.workflows import advanced_repository  # noqa: F401 (table registration)
from backend.workflows.repository import (
    Base,  # noqa: F401 (table registration)
    EvaluationOwnership,
    SqlAlchemyWorkflowRepository,
)

PG_URL = os.environ.get("ALERTS_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="ALERTS_TEST_DATABASE_URL not set; evaluation-ownership PostgreSQL suite skipped",
)

T0 = datetime(2026, 9, 13, 6, 0, tzinfo=timezone.utc)


#: Prefix for every synthetic key this suite creates, so cleanup deletes only its
#: own fence rows. A blanket TRUNCATE here would wipe the shared test database
#: under sibling PostgreSQL suites — a test may only clean up after itself.
KEY_PREFIX = "ownprobe-"


@pytest.fixture(scope="module")
def factory():
    engine = create_engine(PG_URL, poolclass=NullPool)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture(autouse=True)
def clean(factory):
    with factory() as session:
        session.execute(
            text("DELETE FROM evaluation_ownership WHERE subscription_id LIKE :prefix"),
            {"prefix": f"{KEY_PREFIX}%"},
        )
        session.commit()
    return factory


def _claim(factory, *, subscription_id: str, owner: str, barrier: threading.Barrier, out: list):
    """Claim from its own thread and record the outcome (or the exception)."""
    repo = SqlAlchemyWorkflowRepository(factory)
    barrier.wait(timeout=10)
    try:
        out.append(
            repo.claim_evaluation(
                subscription_id,
                "NSE:RACE",
                owner,
                lease_seconds=120.0,
                now=T0,
            )
        )
    except Exception as exc:  # noqa: BLE001 - the failure IS the assertion
        out.append(exc)


def test_concurrent_first_claim_is_a_quiet_loss_not_a_crash(factory) -> None:
    """Both claimers agree on the outcome and neither raises.

    Repeated over several fresh subscriptions because the race is timing
    dependent: one round passing proves little, and the bug appeared only under
    real concurrency in the first place.
    """
    for round_index in range(8):
        subscription_id = f"{KEY_PREFIX}race-{round_index}"
        barrier = threading.Barrier(2)
        out: list = []
        threads = [
            threading.Thread(
                target=_claim,
                kwargs={
                    "factory": factory,
                    "subscription_id": subscription_id,
                    "owner": f"worker-{round_index}-{index}",
                    "barrier": barrier,
                    "out": out,
                },
            )
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        failures = [item for item in out if isinstance(item, BaseException)]
        assert not failures, f"round {round_index}: claim raised {failures!r}"
        # Exactly one winner: the loser gets None (the fence), never an epoch it
        # does not own. Two epochs would mean two owners both thought they won.
        winners = [item for item in out if isinstance(item, int)]
        assert len(winners) == 1, f"round {round_index}: expected one winner, got {out!r}"
        assert winners[0] == 1, f"round {round_index}: first claim must be epoch 1, got {out!r}"


def test_second_claimer_is_fenced_while_the_lease_is_held(factory) -> None:
    """The ordinary (non-racing) path still refuses a live foreign owner."""
    repo = SqlAlchemyWorkflowRepository(factory)
    assert repo.claim_evaluation(f"{KEY_PREFIX}sub-1", "NSE:X", "worker-a", lease_seconds=120.0, now=T0) == 1
    assert repo.claim_evaluation(f"{KEY_PREFIX}sub-1", "NSE:X", "worker-b", lease_seconds=120.0, now=T0) is None
    # The holder renews without side effects, and keeps its epoch.
    assert repo.claim_evaluation(f"{KEY_PREFIX}sub-1", "NSE:X", "worker-a", lease_seconds=120.0, now=T0) == 1


def test_expired_lease_takeover_increments_the_epoch(factory) -> None:
    """Fencing still works: a takeover after expiry bumps the epoch so the
    previous owner's writes are rejected at the transaction boundary."""
    repo = SqlAlchemyWorkflowRepository(factory)
    assert repo.claim_evaluation(f"{KEY_PREFIX}sub-2", "NSE:Y", "worker-a", lease_seconds=60.0, now=T0) == 1
    later = T0 + timedelta(seconds=120)
    assert repo.claim_evaluation(f"{KEY_PREFIX}sub-2", "NSE:Y", "worker-b", lease_seconds=60.0, now=later) == 2
    with factory() as session:
        row = session.execute(
            select(EvaluationOwnership).where(EvaluationOwnership.subscription_id == f"{KEY_PREFIX}sub-2")
        ).scalar_one()
        assert row.owner_id == "worker-b"
        assert int(row.owner_epoch) == 2
