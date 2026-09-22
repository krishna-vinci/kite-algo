"""Admit the live release-recovery trail event.

Revision ID: 20260922_000041
Revises: 20260922_000040
Create Date: 2026-09-22

Why one more vocabulary value is needed:

A ``releasing`` live step is the window where the release pass committed the claim
and the process died before an order reference was persisted. That window is
decidable only from the platform's own durable pre-send records:

* a pre-send record NAMING a broker order means the order EXISTS, and the repair
  is to ADOPT the discovered reference onto the claim so ordinary ingestion owns
  it;
* a pre-send record with NO broker id means the send was ATTEMPTED and its outcome
  is UNKNOWN. An idless ``failed`` row is NOT non-submission - the live order path
  writes that same status both for an explicit refusal and for a failure AFTER
  acceptance (a timeout, a socket close, a death with the broker holding the
  order) - so nothing is abandoned, released or retried on it;
* absence of any pre-send record is only decisive where the write path guarantees
  the row precedes EVERY broker write for the step. Where a sender can still be
  paused before writing its row, missing rows stay UNKNOWN too and nothing is
  released on them.

That repair is not an abandonment and must not be recorded as one: it is the
append-only plan trail's evidence that a real broker order was recovered for a
step the crash left unbound. Hence a new event name, additive to the existing
vocabulary (which 000039 widened to include ``residual_abandoned``).

No column, index or table changes: the claim already carries ``broker_order_ids``
and its ``detail``, and the trail already carries actors and detail. Downgrade
restores the previous vocabulary, which fails while a recovery row exists - the
correct fix-forward posture.
"""

from alembic import op

revision = "20260922_000041"
down_revision = "20260922_000040"
branch_labels = None
depends_on = None

_EVENTS_WITH_RECOVERY = (
    "'submitted','filled','partially_filled','rejected','failed','no_op',"
    "'residual_abandoned','release_recovered'"
)

_EVENTS_WITHOUT_RECOVERY = (
    "'submitted','filled','partially_filled','rejected','failed','no_op',"
    "'residual_abandoned'"
)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.strategy_plan_execution_events "
        "DROP CONSTRAINT IF EXISTS strategy_plan_execution_events_event_check"
    )
    op.execute(
        "ALTER TABLE public.strategy_plan_execution_events "
        "DROP CONSTRAINT IF EXISTS ck_spee_event"
    )
    op.execute(
        "ALTER TABLE public.strategy_plan_execution_events "
        "ADD CONSTRAINT ck_spee_event CHECK (event IN (" + _EVENTS_WITH_RECOVERY + "))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE public.strategy_plan_execution_events "
        "DROP CONSTRAINT IF EXISTS ck_spee_event"
    )
    op.execute(
        "ALTER TABLE public.strategy_plan_execution_events "
        "ADD CONSTRAINT ck_spee_event CHECK (event IN (" + _EVENTS_WITHOUT_RECOVERY + "))"
    )
