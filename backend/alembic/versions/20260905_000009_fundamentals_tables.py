"""fundamentals tables

Revision ID: 20260905_000009
Revises: 20260905_000008
Create Date: 2026-09-05 00:00:09
"""
from alembic import op

revision = "20260905_000009"
down_revision = "20260905_000008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE IF NOT EXISTS public.fundamentals_metrics (
        id BIGSERIAL PRIMARY KEY,
        symbol TEXT NOT NULL,
        statement_scope TEXT NOT NULL DEFAULT 'consolidated',
        dataset TEXT NOT NULL,
        period_end DATE,
        metric_key TEXT NOT NULL,
        metric_name TEXT NOT NULL,
        value_text TEXT,
        numeric_value DOUBLE PRECISION,
        scraped_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (symbol, statement_scope, dataset, period_end, metric_key)
    );
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS idx_fund_metrics_symbol ON public.fundamentals_metrics (symbol, statement_scope, dataset);
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS public.fundamentals_symbol_state (
        symbol TEXT NOT NULL,
        statement_scope TEXT NOT NULL DEFAULT 'consolidated',
        status TEXT NOT NULL,
        etag TEXT,
        last_modified TEXT,
        content_fingerprint TEXT,
        last_checked_at TIMESTAMPTZ,
        last_success_at TIMESTAMPTZ,
        last_error TEXT,
        source_url TEXT,
        PRIMARY KEY (symbol, statement_scope)
    );
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS public.fundamentals_sync_runs (
        run_id UUID PRIMARY KEY,
        scope_type TEXT NOT NULL,
        scope_value TEXT NOT NULL,
        mode TEXT NOT NULL,
        symbols_requested INT NOT NULL DEFAULT 0,
        symbols_changed INT NOT NULL DEFAULT 0,
        symbols_unchanged INT NOT NULL DEFAULT 0,
        symbols_failed INT NOT NULL DEFAULT 0,
        symbols_skipped INT NOT NULL DEFAULT 0,
        started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        finished_at TIMESTAMPTZ,
        status TEXT NOT NULL DEFAULT 'running',
        error TEXT
    );
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS public.fundamentals_features (
        symbol TEXT NOT NULL,
        statement_scope TEXT NOT NULL DEFAULT 'consolidated',
        company_name TEXT,
        market_cap_cr DOUBLE PRECISION,
        current_price DOUBLE PRECISION,
        stock_pe DOUBLE PRECISION,
        book_value DOUBLE PRECISION,
        dividend_yield_pct DOUBLE PRECISION,
        latest_quarter_revenue DOUBLE PRECISION,
        latest_quarter_net_profit DOUBLE PRECISION,
        latest_quarter_eps DOUBLE PRECISION,
        ttm_revenue DOUBLE PRECISION,
        ttm_net_profit DOUBLE PRECISION,
        quarterly_revenue_yoy_pct DOUBLE PRECISION,
        quarterly_profit_yoy_pct DOUBLE PRECISION,
        latest_roce_pct DOUBLE PRECISION,
        latest_roe_pct DOUBLE PRECISION,
        promoter_holding_pct DOUBLE PRECISION,
        fii_holding_pct DOUBLE PRECISION,
        dii_holding_pct DOUBLE PRECISION,
        promoter_holding_change_1y_pct DOUBLE PRECISION,
        fii_holding_change_1y_pct DOUBLE PRECISION,
        dii_holding_change_1y_pct DOUBLE PRECISION,
        as_of_date DATE,
        scraped_at TIMESTAMPTZ,
        PRIMARY KEY (symbol, statement_scope)
    );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.fundamentals_features")
    op.execute("DROP TABLE IF EXISTS public.fundamentals_sync_runs")
    op.execute("DROP TABLE IF EXISTS public.fundamentals_symbol_state")
    op.execute("DROP TABLE IF EXISTS public.fundamentals_metrics")
