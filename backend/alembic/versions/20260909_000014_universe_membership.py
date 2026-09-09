"""Add the universe membership tables (alerts platform).

Revision ID: 20260909_000014
Revises: 20260909_000013
"""

from __future__ import annotations

from alembic import op


revision = "20260909_000014"
down_revision = "20260909_000013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.universes (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id VARCHAR(255) NOT NULL,
            name VARCHAR(255) NOT NULL,
            kind VARCHAR(16) NOT NULL CHECK (kind IN ('explicit', 'index', 'portfolio')),
            source_config JSONB NOT NULL DEFAULT '{}'::jsonb,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (owner_id, name)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.universe_revisions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            universe_id UUID NOT NULL REFERENCES public.universes(id) ON DELETE CASCADE,
            revision INTEGER NOT NULL,
            expression JSONB NOT NULL DEFAULT '{}'::jsonb,
            members TEXT[] NOT NULL DEFAULT '{}',
            member_count INTEGER NOT NULL DEFAULT 0,
            source_generation UUID,
            coverage JSONB NOT NULL DEFAULT '{}'::jsonb,
            resolved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (universe_id, revision)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_universe_revisions_universe_resolved
            ON public.universe_revisions (universe_id, resolved_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.universe_revisions")
    op.execute("DROP TABLE IF EXISTS public.universes")
