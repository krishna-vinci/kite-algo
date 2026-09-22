"""Widen the roll event vocabulary with the replacement-fill evidence row.

Revision ID: 20260921_000036
Revises: 20260921_000035
Create Date: 2026-09-21

Why this exists:

- The roll's proof used to be read from the attributed book of the new contract,
  which mixes holdings that predate the roll (or belong to an unrelated decision)
  with the replacement it is meant to prove - and the HTTP surface could also
  pass a declared quantity. Proof is now the roll's own durable record of the
  confirmed executions that filled the replacement leg, which needs one new event
  name in ``ck_roll_event``.

- Purely additive: the allowed set only grows, so every existing row stays valid;
  the downgrade restores the previous vocabulary (any row of the new kind is
  rejected, which is the truthful pre-change state).
"""

from alembic import op

revision = "20260921_000036"
down_revision = "20260921_000035"
branch_labels = None
depends_on = None

_WIDENED = (
    "'created','acquired','replacement_filled','fill_proven','close_released',"
    "'old_flat','completed','stalled','escalated'"
)
_NARROW = (
    "'created','acquired','fill_proven','close_released','old_flat','completed',"
    "'stalled','escalated'"
)


def upgrade() -> None:
    op.execute("ALTER TABLE public.strategy_roll_events DROP CONSTRAINT ck_roll_event")
    op.execute(
        "ALTER TABLE public.strategy_roll_events ADD CONSTRAINT ck_roll_event "
        f"CHECK (event IN ({_WIDENED}))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE public.strategy_roll_events DROP CONSTRAINT ck_roll_event")
    op.execute(
        "ALTER TABLE public.strategy_roll_events ADD CONSTRAINT ck_roll_event "
        f"CHECK (event IN ({_NARROW}))"
    )
