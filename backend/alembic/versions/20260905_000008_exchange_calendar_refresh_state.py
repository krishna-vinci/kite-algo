"""exchange calendar refresh state

Revision ID: 20260905_000008
Revises: 20260829_000007
Create Date: 2026-09-05 00:00:08
"""
from alembic import op

revision = "20260905_000008"
down_revision = "20260829_000007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE public.exchange_calendar_refresh_state (
        exchange TEXT NOT NULL,
        segment TEXT NOT NULL,
        last_attempt_at TIMESTAMPTZ,
        last_success_at TIMESTAMPTZ,
        last_failure_at TIMESTAMPTZ,
        last_error TEXT,
        observed_source_sha256 CHAR(64),
        active_calendar_version BIGINT,
        coverage_start DATE,
        coverage_end DATE,
        next_attempt_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (exchange, segment))""")


def downgrade() -> None:
    op.execute("DROP TABLE public.exchange_calendar_refresh_state")
