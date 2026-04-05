"""add pg_trgm instrument search index

Revision ID: 20260405_000002
Revises: 20260330_000001
Create Date: 2026-04-05 00:00:02
"""

from __future__ import annotations

from alembic import op


revision = "20260405_000002"
down_revision = "20260330_000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.instruments_search_index (
          instrument_token BIGINT PRIMARY KEY,
          exchange_token BIGINT,
          tradingsymbol VARCHAR(255) NOT NULL,
          name VARCHAR(255),
          last_price DOUBLE PRECISION,
          expiry DATE,
          strike DOUBLE PRECISION,
          tick_size DOUBLE PRECISION,
          lot_size INTEGER,
          instrument_type VARCHAR(32),
          segment VARCHAR(32),
          exchange VARCHAR(16),
          underlying VARCHAR(255),
          option_type VARCHAR(10),
          last_updated TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
          derivative_kind VARCHAR(16),
          expiry_ts BIGINT,
          expiry_year INTEGER,
          expiry_month INTEGER,
          expiry_label VARCHAR(32),
          type_rank INTEGER NOT NULL DEFAULT 9,
          boost_score INTEGER NOT NULL DEFAULT 0,
          aliases TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
          search_text TEXT NOT NULL DEFAULT ''
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_instruments_search_index_underlying ON public.instruments_search_index (underlying)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_instruments_search_index_exchange ON public.instruments_search_index (exchange)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_instruments_search_index_type_exchange ON public.instruments_search_index (instrument_type, exchange)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_instruments_search_index_underlying_opt_exp_strike ON public.instruments_search_index (underlying, option_type, expiry, strike)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_instruments_search_index_expiry_ts ON public.instruments_search_index (expiry_ts)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_instruments_search_index_search_text_trgm ON public.instruments_search_index USING gin (search_text gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_instruments_search_index_tradingsymbol_trgm ON public.instruments_search_index USING gin (tradingsymbol gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_instruments_search_index_name_trgm ON public.instruments_search_index USING gin (name gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_instruments_search_index_name_trgm")
    op.execute("DROP INDEX IF EXISTS idx_instruments_search_index_tradingsymbol_trgm")
    op.execute("DROP INDEX IF EXISTS idx_instruments_search_index_search_text_trgm")
    op.execute("DROP INDEX IF EXISTS idx_instruments_search_index_expiry_ts")
    op.execute("DROP INDEX IF EXISTS idx_instruments_search_index_underlying_opt_exp_strike")
    op.execute("DROP INDEX IF EXISTS idx_instruments_search_index_type_exchange")
    op.execute("DROP INDEX IF EXISTS idx_instruments_search_index_exchange")
    op.execute("DROP INDEX IF EXISTS idx_instruments_search_index_underlying")
    op.execute("DROP TABLE IF EXISTS public.instruments_search_index")
