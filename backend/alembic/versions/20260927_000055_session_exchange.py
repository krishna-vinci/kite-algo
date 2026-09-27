"""market-session schedules name the exchange whose clock they run on.

Revision ID: 20260927_000055
Revises: 20260926_000054
Create Date: 2026-09-27

The ``market_session`` kind was NSE-only by construction, so its session edges
were implicit. Adding MCX means the stored schedule has to say which venue's
clock it runs on rather than leaving every reader to assume NSE. Existing rows
are backfilled to ``NSE`` - the only clock they could have meant - and the new
column is optional so a non-session kind never carries a session exchange.
"""

from alembic import op

revision = "20260927_000055"
down_revision = "20260926_000054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.hosted_strategy_schedules "
        "ADD COLUMN IF NOT EXISTS exchange TEXT"
    )
    op.execute(
        "UPDATE public.hosted_strategy_schedules SET exchange = 'NSE' "
        "WHERE schedule_kind = 'market_session' AND exchange IS NULL"
    )
    op.execute(
        "ALTER TABLE public.hosted_strategy_schedules "
        "DROP CONSTRAINT IF EXISTS ck_hosted_strategy_schedules_exchange"
    )
    op.execute(
        "ALTER TABLE public.hosted_strategy_schedules "
        "ADD CONSTRAINT ck_hosted_strategy_schedules_exchange CHECK "
        "(exchange IS NULL OR exchange IN ('NSE', 'BSE', 'NFO', 'BFO', 'MCX'))"
    )
    op.execute(
        "ALTER TABLE public.hosted_strategy_schedules "
        "DROP CONSTRAINT IF EXISTS ck_hosted_strategy_schedules_non_session_exchange"
    )
    op.execute(
        "ALTER TABLE public.hosted_strategy_schedules "
        "ADD CONSTRAINT ck_hosted_strategy_schedules_non_session_exchange CHECK "
        "(schedule_kind = 'market_session' OR exchange IS NULL)"
    )


def downgrade() -> None:
    for constraint in (
        "ck_hosted_strategy_schedules_non_session_exchange",
        "ck_hosted_strategy_schedules_exchange",
    ):
        op.execute(
            f"ALTER TABLE public.hosted_strategy_schedules "
            f"DROP CONSTRAINT IF EXISTS {constraint}"
        )
    op.execute("ALTER TABLE public.hosted_strategy_schedules DROP COLUMN IF EXISTS exchange")
