"""alerts platform phase 1 tables

Revision ID: 20260908_000011
Revises: 20260905_000010
Create Date: 2026-09-08 00:00:11
"""
from alembic import op

revision = "20260908_000011"
down_revision = "20260905_000010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Workflow authoring -----------------------------------------------------
    op.execute("""
    CREATE TABLE IF NOT EXISTS public.workflows (
        id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        name TEXT NOT NULL,
        idempotency_key TEXT,
        archived_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_workflows_owner_name UNIQUE (owner_id, name)
    );
    """)
    # Plain UNIQUE index: NULL idempotency keys must stay distinct (multiple
    # workflows created without a key).
    op.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS uq_workflows_idempotency_key
        ON public.workflows (idempotency_key);
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS idx_workflows_owner_id
        ON public.workflows (owner_id);
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS public.workflow_revisions (
        id TEXT PRIMARY KEY,
        workflow_id TEXT NOT NULL REFERENCES public.workflows(id),
        revision INT NOT NULL,
        canonical_hash TEXT NOT NULL,
        document JSONB NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        activated_at TIMESTAMPTZ,
        CONSTRAINT uq_workflow_revisions_workflow_revision UNIQUE (workflow_id, revision),
        CONSTRAINT uq_workflow_revisions_workflow_hash UNIQUE (workflow_id, canonical_hash)
    );
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS idx_workflow_revisions_canonical_hash
        ON public.workflow_revisions (canonical_hash);
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS public.alert_subscriptions (
        id TEXT PRIMARY KEY,
        revision_id TEXT NOT NULL REFERENCES public.workflow_revisions(id),
        alert_id TEXT NOT NULL,
        stage_id TEXT NOT NULL,
        instrument_symbol TEXT NOT NULL,
        instrument_exchange TEXT NOT NULL,
        instrument_key TEXT NOT NULL,
        trigger TEXT NOT NULL,
        config JSONB NOT NULL,
        state TEXT NOT NULL DEFAULT 'active',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_alert_subscriptions_revision_alert_instrument
            UNIQUE (revision_id, alert_id, instrument_key)
    );
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS idx_alert_subscriptions_instrument_key
        ON public.alert_subscriptions (instrument_key);
    """)

    # Evaluation state -------------------------------------------------------
    op.execute("""
    CREATE TABLE IF NOT EXISTS public.evaluation_checkpoints (
        subscription_id TEXT NOT NULL,
        instrument_key TEXT NOT NULL,
        epoch_id TEXT NOT NULL,
        state JSONB NOT NULL,
        owner_epoch INT NOT NULL DEFAULT 0,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (subscription_id, instrument_key, epoch_id)
    );
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS public.signal_events (
        id TEXT PRIMARY KEY,
        subscription_id TEXT NOT NULL REFERENCES public.alert_subscriptions(id),
        occurrence_key TEXT NOT NULL,
        fired_at TIMESTAMPTZ NOT NULL,
        evidence JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """)
    op.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS uq_signal_events_occurrence_key
        ON public.signal_events (occurrence_key);
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS idx_signal_events_subscription_fired
        ON public.signal_events (subscription_id, fired_at DESC);
    """)

    # Delivery outbox --------------------------------------------------------
    op.execute("""
    CREATE TABLE IF NOT EXISTS public.channel_references (
        id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        name TEXT NOT NULL,
        provider TEXT NOT NULL,
        destination JSONB NOT NULL,
        secret_env TEXT,
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_channel_references_owner_name UNIQUE (owner_id, name)
    );
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS idx_channel_references_owner_id
        ON public.channel_references (owner_id);
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS public.deliveries (
        id TEXT PRIMARY KEY,
        event_id TEXT NOT NULL REFERENCES public.signal_events(id),
        channel_id TEXT NOT NULL REFERENCES public.channel_references(id),
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INT NOT NULL DEFAULT 0,
        next_attempt_at TIMESTAMPTZ,
        lease_until TIMESTAMPTZ,
        last_error TEXT,
        delivered_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_deliveries_event_channel UNIQUE (event_id, channel_id)
    );
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS idx_deliveries_status_next_attempt
        ON public.deliveries (status, next_attempt_at);
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS public.delivery_attempts (
        id BIGSERIAL PRIMARY KEY,
        delivery_id TEXT NOT NULL REFERENCES public.deliveries(id),
        attempt_no INT NOT NULL,
        outcome TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS idx_delivery_attempts_delivery
        ON public.delivery_attempts (delivery_id, attempt_no);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.delivery_attempts")
    op.execute("DROP TABLE IF EXISTS public.deliveries")
    op.execute("DROP TABLE IF EXISTS public.channel_references")
    op.execute("DROP TABLE IF EXISTS public.signal_events")
    op.execute("DROP TABLE IF EXISTS public.evaluation_checkpoints")
    op.execute("DROP TABLE IF EXISTS public.alert_subscriptions")
    op.execute("DROP TABLE IF EXISTS public.workflow_revisions")
    op.execute("DROP TABLE IF EXISTS public.workflows")
