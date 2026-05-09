"""drop reverted instrument search index artifacts

Revision ID: 20260405_000003
Revises: 20260405_000002
Create Date: 2026-04-05 00:00:03
"""

from __future__ import annotations

from alembic import op


revision = "20260405_000003"
down_revision = "20260405_000002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.instruments_search_index")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")


def downgrade() -> None:
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
