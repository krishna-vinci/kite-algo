"""A failed event write must not masquerade as a benign duplicate (PostgreSQL).

Why this file exists
--------------------
``handle_observation`` writes the event inside a savepoint and catches
``IntegrityError`` to implement E-2/E-7: a racing writer, or a re-delivered bar,
already committed the occurrence key, so the loser skips without crashing.

That handler labelled *every* ``IntegrityError`` as ``duplicate_occurrence``. But
a missing foreign key raises the same class — for instance a delivery whose
``channel_id`` names a channel that does not exist. The consequences were all
silent and all bad:

* the event was lost while health reported only ``duplicate_occurrence``;
* the early return skipped the checkpoint write, so the rule re-fired and
  re-failed on every subsequent observation, forever;
* the misclassification made the failure look like correct dedup behaviour, so
  nothing prompted anyone to look.

The fix re-reads the occurrence key after the savepoint rollback: present means
it really was a duplicate, absent means something else went wrong and must
surface.

PostgreSQL only, because the misclassification needs real foreign-key
enforcement; SQLite does not enforce FKs by default, so the insert succeeds and
the bug cannot be reproduced.

    ALERTS_TEST_DATABASE_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \\
        pytest tests/integration/test_emission_integrity_postgres.py -q
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from backend.alerts.predicates import Observation
from backend.workflows import advanced_repository  # noqa: F401 (table registration)
from backend.workflows.compiler import compile_document
from backend.workflows.parser import parse_workflow_dict
from backend.workflows.repository import (
    Base,
    SignalEvent,
    SqlAlchemyWorkflowRepository,
)
from backend.workflows.service import EvaluationService

PG_URL = os.environ.get("ALERTS_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="ALERTS_TEST_DATABASE_URL not set; emission-integrity PostgreSQL suite skipped",
)

T0 = datetime(2026, 9, 13, 6, 0, tzinfo=timezone.utc)

#: The alert names a channel that will never exist. With no channel resolver the
#: name is used directly as a channel id, so the delivery insert violates
#: deliveries.channel_id -> channel_references.id.
DOC_WITH_BAD_CHANNEL = {
    "version": 1,
    "name": "bad-channel",
    "session": "nse_equity",
    "instruments": ["NSE:BADCH"],
    "stages": [
        {
            "id": "px",
            "type": "signal",
            "clock": "candle_close",
            "timeframe": "5minute",
            "conditions": {
                "all": [
                    {"left": {"field": "close"}, "op": "crosses_above", "right": {"value": 100}}
                ]
            },
        }
    ],
    "alerts": [
        {"id": "a1", "source": "px", "trigger": "on_transition", "channels": ["nope-does-not-exist"]}
    ],
}


#: A per-run owner, so cleanup touches only this suite's rows. The first version
#: of this file TRUNCATEd signal_events/deliveries and DELETEd every workflow —
#: which wiped the shared test database under the sibling PostgreSQL suites and
#: made them fail non-deterministically depending on file order. A test may only
#: clean up after itself.
OWNER = f"owner-integrity-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def factory():
    engine = create_engine(PG_URL, poolclass=NullPool)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture(autouse=True)
def clean(factory):
    """Remove only OWNER's rows, before and after each test."""
    _wipe_owner(factory)
    yield factory
    _wipe_owner(factory)


def _wipe_owner(factory) -> None:
    workflows = "SELECT id FROM workflows WHERE owner_id = :owner"
    with factory() as session:
        session.execute(
            text(f"DELETE FROM signal_events WHERE workflow_id::text IN ({workflows})"),
            {"owner": OWNER},
        )
        session.execute(
            text(
                "DELETE FROM evaluation_checkpoints WHERE subscription_id IN "
                "(SELECT id FROM alert_subscriptions WHERE revision_id IN "
                f"(SELECT id FROM workflow_revisions WHERE workflow_id IN ({workflows})))"
            ),
            {"owner": OWNER},
        )
        session.execute(
            text(
                "DELETE FROM evaluation_ownership WHERE subscription_id IN "
                "(SELECT id FROM alert_subscriptions WHERE revision_id IN "
                f"(SELECT id FROM workflow_revisions WHERE workflow_id IN ({workflows})))"
            ),
            {"owner": OWNER},
        )
        session.execute(
            text(
                "DELETE FROM alert_subscriptions WHERE revision_id IN "
                f"(SELECT id FROM workflow_revisions WHERE workflow_id IN ({workflows}))"
            ),
            {"owner": OWNER},
        )
        session.execute(
            text(f"DELETE FROM workflow_revisions WHERE workflow_id IN ({workflows})"),
            {"owner": OWNER},
        )
        session.execute(text("DELETE FROM workflows WHERE owner_id = :owner"), {"owner": OWNER})
        session.commit()


def _activate(factory, document) -> object:
    repo = SqlAlchemyWorkflowRepository(factory)
    compiled = compile_document(parse_workflow_dict(document))
    workflow, revision = repo.create_workflow(
        OWNER, document["name"], compiled.document.to_document_dict(), compiled.canonical_hash
    )
    repo.activate_revision(workflow.id, revision.id)
    active = repo.get_active_revision(workflow.id)
    # No channel_resolver: channel names are used as ids, which is what makes the
    # delivery insert violate its foreign key.
    EvaluationService(repo, factory).ensure_subscriptions(active)
    return repo, active


def _bar(ts, close):
    return Observation(
        ts=ts, epoch_id="candle", ltp=close, open=close, high=close + 1, low=close - 1,
        close=close, volume=1000.0, final=True,
    )


def test_a_non_duplicate_integrity_error_is_not_reported_as_a_duplicate(clean) -> None:
    """The failure surfaces instead of being disguised as dedup."""
    repo, active = _activate(clean, DOC_WITH_BAD_CHANNEL)
    service = EvaluationService(repo, clean)
    # Scoped to THIS suite's revision: list_active_subscriptions is global, and
    # the database is shared with the sibling PostgreSQL suites.
    subscriptions = [
        sub for sub in repo.list_active_subscriptions() if sub.revision_id == active.id
    ]
    assert len(subscriptions) == 1
    subscription = subscriptions[0]

    # The first observation initializes `prev`; the second crosses and tries to
    # emit, which is where the delivery foreign key fails.
    service.handle_observation(subscription, _bar(T0, 98.0))
    with pytest.raises(Exception) as exc:
        service.handle_observation(subscription, _bar(T0 + timedelta(minutes=5), 104.0))

    message = str(exc.value)
    assert "duplicate" not in message.lower(), (
        "a foreign-key failure was reported as a duplicate occurrence; the event "
        f"would have been lost silently: {message}"
    )

    with clean() as session:
        # Scoped to this suite's owner: the database is shared, so an unscoped
        # count would pick up every other suite's events.
        owned_events = session.execute(
            text(
                "SELECT count(*) FROM signal_events WHERE workflow_id::text IN "
                "(SELECT id FROM workflows WHERE owner_id = :owner)"
            ),
            {"owner": OWNER},
        ).scalar_one()
        assert owned_events == 0
