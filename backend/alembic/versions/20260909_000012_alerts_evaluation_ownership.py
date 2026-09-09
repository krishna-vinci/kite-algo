"""durable evaluation ownership fence

Revision ID: 20260909_000012
Revises: 20260908_000011
Create Date: 2026-09-09 00:00:12
"""
from alembic import op

revision = "20260909_000012"
down_revision = "20260908_000011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE IF NOT EXISTS public.evaluation_ownership (
        subscription_id TEXT NOT NULL,
        instrument_key TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        owner_epoch INT NOT NULL DEFAULT 1,
        lease_until TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (subscription_id, instrument_key)
    );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.evaluation_ownership")
