"""drop alerts and fyers tables

Revision ID: 20260501_000005
Revises: 20260415_000004
Create Date: 2026-05-01 00:00:05
"""

from __future__ import annotations

from alembic import op


revision = "20260501_000005"
down_revision = "20260415_000004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.alert_events")
    op.execute("DROP TABLE IF EXISTS public.alerts")
    op.execute("DROP TABLE IF EXISTS public.fyers_sessions")


def downgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.fyers_sessions (
          session_id VARCHAR(36) PRIMARY KEY,
          access_token TEXT NOT NULL,
          created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
        """
    )

    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.alerts (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          instrument_token BIGINT NOT NULL,
          comparator TEXT NOT NULL CHECK (comparator IN ('gt', 'lt')),
          target_type TEXT NOT NULL CHECK (target_type IN ('absolute', 'percent')),
          absolute_target NUMERIC(18,6),
          percent NUMERIC(9,6),
          baseline_price NUMERIC(18,6),
          one_time BOOLEAN NOT NULL DEFAULT TRUE,
          name TEXT,
          notes TEXT,
          status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('draft', 'active', 'paused', 'canceled', 'triggered')),
          instrument_exchange TEXT,
          instrument_tradingsymbol TEXT,
          ltp_source_hint TEXT,
          last_evaluated_price NUMERIC(18,6),
          triggered_at TIMESTAMP WITH TIME ZONE,
          created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
          CONSTRAINT alerts_abs_or_pct_chk CHECK (
            (target_type = 'absolute' AND absolute_target IS NOT NULL AND percent IS NULL)
            OR
            (target_type = 'percent' AND percent IS NOT NULL)
          )
        )
        """
    )

    op.execute(
        """
        ALTER TABLE public.alerts
        ADD COLUMN IF NOT EXISTS uuid UUID GENERATED ALWAYS AS (id) STORED
        """
    )
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_alerts_uuid ON public.alerts (uuid)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_alerts_status ON public.alerts (status)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_alerts_token_status ON public.alerts (instrument_token, status)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_alerts_status_token ON public.alerts (status, instrument_token)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_alerts_active_partial ON public.alerts (id) WHERE status = 'active'")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.alert_events (
          id BIGSERIAL PRIMARY KEY,
          alert_id UUID NOT NULL REFERENCES public.alerts(id) ON DELETE CASCADE,
          instrument_token BIGINT NOT NULL,
          event_type TEXT NOT NULL CHECK (event_type IN ('created','updated','activated','paused','resumed','canceled','triggered','reactivated','deleted')),
          price_at_event NUMERIC(18,6),
          direction TEXT CHECK (direction IN ('cross_up','cross_down')),
          reason TEXT,
          meta JSONB,
          created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_alert_events_alert_id ON public.alert_events (alert_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_alert_events_created_at ON public.alert_events (created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_alert_events_alert_id_created_at ON public.alert_events (alert_id, created_at)")
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_alert_events_triggered_once
          ON public.alert_events (alert_id)
          WHERE event_type = 'triggered'
        """
    )
