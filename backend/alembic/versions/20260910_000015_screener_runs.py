"""screener runs, members and attachment state (alerts Phase 3 F9)

Revision ID: 20260910_000015
Revises: 20260909_000014
Create Date: 2026-09-10 00:00:15
"""
from alembic import op

revision = "20260910_000015"
down_revision = "20260909_000014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Screener attachment events are WORKFLOW-level (no alert subscription);
    # the delivery worker renders their context from event evidence.
    op.execute("ALTER TABLE signal_events ALTER COLUMN subscription_id DROP NOT NULL;")
    op.execute("ALTER TABLE signal_events ADD COLUMN IF NOT EXISTS workflow_id UUID;")
    op.execute("""
    CREATE INDEX IF NOT EXISTS idx_signal_events_workflow
        ON signal_events (workflow_id, fired_at DESC);
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS public.screener_run (
        id UUID PRIMARY KEY,
        owner_id TEXT NOT NULL,
        workflow_id UUID NOT NULL,
        workflow_revision_id UUID NOT NULL,
        occurrence_key TEXT NOT NULL UNIQUE,
        scheduled_for TIMESTAMPTZ NOT NULL,
        triggered_by TEXT NOT NULL DEFAULT 'schedule',
        status TEXT NOT NULL,
        universe_revision INTEGER,
        as_of TIMESTAMPTZ,
        coverage JSONB NOT NULL DEFAULT '{}'::jsonb,
        data_freshness JSONB NOT NULL DEFAULT '{}'::jsonb,
        failure_reason TEXT,
        lease_owner TEXT,
        lease_expires_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        completed_at TIMESTAMPTZ
    );
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS idx_screener_run_workflow
        ON public.screener_run (workflow_id, scheduled_for DESC);
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS public.screener_run_member (
        id BIGSERIAL PRIMARY KEY,
        run_id UUID NOT NULL REFERENCES public.screener_run(id) ON DELETE CASCADE,
        instrument_key TEXT NOT NULL,
        passed BOOLEAN NOT NULL DEFAULT false,
        exclusion_reason TEXT,
        values JSONB NOT NULL DEFAULT '{}'::jsonb,
        rank INTEGER,
        score DOUBLE PRECISION
    );
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS idx_screener_member_run
        ON public.screener_run_member (run_id);
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS public.screener_attachment_state (
        id BIGSERIAL PRIMARY KEY,
        owner_id TEXT NOT NULL,
        workflow_id UUID NOT NULL,
        workflow_revision_id UUID NOT NULL,
        attachment_id TEXT NOT NULL,
        instrument_key TEXT NOT NULL,
        present BOOLEAN NOT NULL DEFAULT false,
        last_complete_run_id UUID,
        last_rank INTEGER,
        consecutive_absent INTEGER NOT NULL DEFAULT 0,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (owner_id, workflow_id, workflow_revision_id, attachment_id, instrument_key)
    );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.screener_attachment_state")
    op.execute("DROP TABLE IF EXISTS public.screener_run_member")
    op.execute("DROP TABLE IF EXISTS public.screener_run")
    op.execute("DROP INDEX IF EXISTS signal_events_idx_signal_events_workflow")
    op.execute("ALTER TABLE signal_events DROP COLUMN IF EXISTS workflow_id")
    # subscription_id stays nullable: pre-existing rows may already be null
