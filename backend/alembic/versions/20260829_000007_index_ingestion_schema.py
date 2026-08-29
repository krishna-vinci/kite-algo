"""move index ingestion schema changes into Alembic

Revision ID: 20260829_000007
Revises: 20260829_000006
Create Date: 2026-08-29 00:00:07
"""

from __future__ import annotations

from alembic import op


revision = "20260829_000007"
down_revision = "20260829_000006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in (
        "ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS isin_code VARCHAR(32)",
        "ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS series VARCHAR(32)",
        "ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS source_url TEXT",
        "ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS weight_source VARCHAR(128)",
        "ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS points_contribution NUMERIC(18, 4)",
        "ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS last_refreshed_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS baseline_close NUMERIC(18, 6)",
        "ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS baseline_index_weight NUMERIC(10, 4)",
        "ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS baseline_freefloat_marketcap NUMERIC(20, 2)",
        "ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS baseline_ff_factor NUMERIC(24, 10)",
        "ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS baseline_as_of_date DATE",
        "ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS needs_weight_review BOOLEAN NOT NULL DEFAULT FALSE",
    ):
        op.execute(statement)
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.index_refresh_state (
            source_list VARCHAR(255) PRIMARY KEY,
            last_constituent_refresh_at TIMESTAMP WITH TIME ZONE,
            last_live_refresh_at TIMESTAMP WITH TIME ZONE,
            added_symbols_json TEXT,
            removed_symbols_json TEXT,
            needs_review BOOLEAN NOT NULL DEFAULT FALSE,
            last_error TEXT,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
        """
    )
    for statement in (
        "ALTER TABLE public.index_refresh_state ADD COLUMN IF NOT EXISTS official_source_url TEXT",
        "ALTER TABLE public.index_refresh_state ADD COLUMN IF NOT EXISTS source_checksum CHAR(64)",
        "ALTER TABLE public.index_refresh_state ADD COLUMN IF NOT EXISTS expected_member_count INTEGER",
        "ALTER TABLE public.index_refresh_state ADD COLUMN IF NOT EXISTS actual_member_count INTEGER",
        "ALTER TABLE public.index_refresh_state ADD COLUMN IF NOT EXISTS complete BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE public.index_refresh_state ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE public.index_refresh_state ADD COLUMN IF NOT EXISTS last_success_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE public.index_refresh_state ADD COLUMN IF NOT EXISTS last_failure_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE public.index_refresh_state ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMP WITH TIME ZONE",
    ):
        op.execute(statement)


def downgrade() -> None:
    """Preserve legacy index-ingestion observations on downgrade."""
