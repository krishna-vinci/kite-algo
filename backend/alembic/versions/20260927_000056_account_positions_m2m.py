"""account positions keep the broker's day mark-to-market.

Revision ID: 20260927_000056
Revises: 20260927_000055
Create Date: 2026-09-27

The account day-loss cap summed ``realised + (last - average) * qty``, which is
P&L since entry: a carried position's earlier gains or losses were read as
today's. Kite reports ``m2m`` per net position - the day's mark-to-market from
the last close - and that is the number a day-loss cap has to sum.
"""

from alembic import op

revision = "20260927_000056"
down_revision = "20260927_000055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.account_positions ADD COLUMN IF NOT EXISTS m2m NUMERIC(18,6)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE public.account_positions DROP COLUMN IF EXISTS m2m")
