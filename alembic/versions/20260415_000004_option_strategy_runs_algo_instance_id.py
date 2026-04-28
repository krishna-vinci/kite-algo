"""ensure option_strategy_runs has algo_instance_id

Revision ID: 20260415_000004
Revises: 20260405_000003
Create Date: 2026-04-15 00:00:04
"""

from __future__ import annotations

from alembic import op


revision = "20260415_000004"
down_revision = "20260405_000003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'option_strategy_runs'
            ) THEN
                ALTER TABLE public.option_strategy_runs
                ADD COLUMN IF NOT EXISTS algo_instance_id TEXT;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'option_strategy_runs'
            ) THEN
                ALTER TABLE public.option_strategy_runs
                DROP COLUMN IF EXISTS algo_instance_id;
            END IF;
        END
        $$;
        """
    )
