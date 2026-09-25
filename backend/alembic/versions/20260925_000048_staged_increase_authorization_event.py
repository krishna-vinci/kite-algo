"""staged increases: a durable, key-scoped authorization event.

A staged dependent buy must prove exactly which account funds authorized it,
without confusing that proof with generic execution progress. This adds the
narrow event vocabulary required by C1.1; authorization detail remains in the
append-only JSON payload.
"""

from alembic import op

revision = "20260925_000048"
down_revision = "20260925_000047"
branch_labels = None
depends_on = None


def _check() -> str:
    return (
        "event IN ('created', 'renewed', 'advanced', 'consumed', 'released', "
        "'expired', 'action_required', 'disposition_confirmed', "
        "'staged_increase_authorized')"
    )


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.strategy_reservation_events "
        "DROP CONSTRAINT IF EXISTS ck_res_event"
    )
    op.execute(
        f"ALTER TABLE public.strategy_reservation_events "
        f"ADD CONSTRAINT ck_res_event CHECK ({_check()})"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE public.strategy_reservation_events "
        "DROP CONSTRAINT IF EXISTS ck_res_event"
    )
    op.execute(
        "ALTER TABLE public.strategy_reservation_events ADD CONSTRAINT ck_res_event "
        "CHECK (event IN ('created', 'renewed', 'advanced', 'consumed', 'released', "
        "'expired', 'action_required', 'disposition_confirmed'))"
    )
