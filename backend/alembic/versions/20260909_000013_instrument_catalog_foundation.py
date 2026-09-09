"""Add the generation-aware instrument catalog foundation.

Revision ID: 20260909_000013
Revises: 20260909_000012
"""

from __future__ import annotations

from alembic import op


revision = "20260909_000013"
down_revision = "20260909_000012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.instrument_catalog_generations (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            status VARCHAR(16) NOT NULL CHECK (status IN ('staging', 'published', 'degraded', 'failed')),
            requested_exchanges TEXT[] NOT NULL DEFAULT '{}',
            accepted_exchanges TEXT[] NOT NULL DEFAULT '{}',
            retained_exchanges TEXT[] NOT NULL DEFAULT '{}',
            record_count INTEGER NOT NULL DEFAULT 0,
            validation_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
            exchange_sources JSONB NOT NULL DEFAULT '{}'::jsonb,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            published_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.instrument_catalog_records (
            instrument_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            identity_key TEXT NOT NULL UNIQUE,
            public_key TEXT NOT NULL,
            exchange VARCHAR(16) NOT NULL,
            segment VARCHAR(32),
            tradingsymbol VARCHAR(255) NOT NULL,
            name VARCHAR(255),
            instrument_type VARCHAR(32),
            underlying VARCHAR(255),
            option_type VARCHAR(10),
            expiry DATE,
            strike DOUBLE PRECISION,
            tick_size DOUBLE PRECISION,
            lot_size INTEGER,
            lifecycle_status VARCHAR(16) NOT NULL DEFAULT 'active'
                CHECK (lifecycle_status IN ('active', 'expired', 'retired')),
            first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            current_generation_id UUID REFERENCES public.instrument_catalog_generations(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.instrument_broker_mappings (
            mapping_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            instrument_id UUID NOT NULL REFERENCES public.instrument_catalog_records(instrument_id),
            broker VARCHAR(32) NOT NULL,
            broker_exchange VARCHAR(16) NOT NULL,
            broker_symbol VARCHAR(255) NOT NULL,
            broker_token BIGINT NOT NULL,
            broker_exchange_token BIGINT,
            valid_from_generation UUID NOT NULL REFERENCES public.instrument_catalog_generations(id),
            valid_to_generation UUID REFERENCES public.instrument_catalog_generations(id),
            is_current BOOLEAN NOT NULL DEFAULT TRUE,
            first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (instrument_id, broker, valid_from_generation),
            UNIQUE (broker, broker_token, valid_from_generation)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.instrument_catalog_staging (
            generation_id UUID NOT NULL REFERENCES public.instrument_catalog_generations(id) ON DELETE CASCADE,
            source_exchange VARCHAR(16) NOT NULL,
            broker VARCHAR(32) NOT NULL,
            broker_exchange VARCHAR(16) NOT NULL,
            broker_symbol VARCHAR(255) NOT NULL,
            broker_token BIGINT NOT NULL,
            broker_exchange_token BIGINT,
            identity_key TEXT NOT NULL,
            public_key TEXT NOT NULL,
            segment VARCHAR(32),
            name VARCHAR(255),
            instrument_type VARCHAR(32),
            underlying VARCHAR(255),
            option_type VARCHAR(10),
            expiry DATE,
            strike DOUBLE PRECISION,
            tick_size DOUBLE PRECISION,
            lot_size INTEGER,
            raw_record JSONB NOT NULL DEFAULT '{}'::jsonb,
            PRIMARY KEY (generation_id, broker, broker_token)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_instrument_mapping_current_broker_token
            ON public.instrument_broker_mappings (broker, broker_token)
            WHERE is_current
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_instrument_mapping_current_identity_broker
            ON public.instrument_broker_mappings (instrument_id, broker)
            WHERE is_current
        """
    )
    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_instrument_catalog_public_key ON public.instrument_catalog_records (public_key)",
        "CREATE INDEX IF NOT EXISTS idx_instrument_catalog_exchange_symbol ON public.instrument_catalog_records (exchange, tradingsymbol)",
        "CREATE INDEX IF NOT EXISTS idx_instrument_catalog_derivative ON public.instrument_catalog_records (underlying, option_type, expiry, strike)",
        "CREATE INDEX IF NOT EXISTS idx_instrument_catalog_current_generation ON public.instrument_catalog_records (current_generation_id)",
        "CREATE INDEX IF NOT EXISTS idx_instrument_mapping_current_token ON public.instrument_broker_mappings (broker, broker_token) WHERE is_current",
        "CREATE INDEX IF NOT EXISTS idx_instrument_staging_generation_exchange ON public.instrument_catalog_staging (generation_id, source_exchange)",
        "CREATE INDEX IF NOT EXISTS idx_instrument_staging_identity ON public.instrument_catalog_staging (generation_id, identity_key)",
    ):
        op.execute(statement)

    op.execute("DROP VIEW IF EXISTS public.instrument_catalog_published_v")
    op.execute(
        """
        CREATE VIEW public.instrument_catalog_published_v AS
        SELECT
            r.instrument_id,
            r.identity_key,
            r.public_key,
            r.exchange,
            r.segment,
            r.tradingsymbol,
            r.name,
            r.instrument_type,
            r.underlying,
            r.option_type,
            r.expiry,
            r.strike,
            r.tick_size,
            r.lot_size,
            r.lifecycle_status,
            r.current_generation_id AS catalog_generation,
            m.broker,
            m.broker_exchange,
            m.broker_symbol,
            m.broker_token,
            m.broker_exchange_token,
            g.status AS generation_status,
            g.published_at,
            g.validation_summary
        FROM public.instrument_catalog_records r
        JOIN public.instrument_catalog_generations g
          ON g.id = r.current_generation_id
         AND g.status IN ('published', 'degraded')
         AND r.lifecycle_status <> 'retired'
        JOIN public.instrument_broker_mappings m
          ON m.instrument_id = r.instrument_id
         AND m.is_current = TRUE
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS public.instrument_catalog_published_v")
    op.execute("DROP TABLE IF EXISTS public.instrument_catalog_staging")
    op.execute("DROP TABLE IF EXISTS public.instrument_broker_mappings")
    op.execute("DROP TABLE IF EXISTS public.instrument_catalog_records")
    op.execute("DROP TABLE IF EXISTS public.instrument_catalog_generations")
