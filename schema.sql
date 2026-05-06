-- Schema for kite-app. This file is authoritative for a fresh deployment.
-- Assumes a clean DB (you will drop tables/volume before build).
-- Uses IF NOT EXISTS and CREATE OR REPLACE for idempotence on repeated runs.

-- Enable UUID generation helpers used by multiple tables.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- =========================================
-- Tables
-- =========================================

-- Core instruments table (equities, futures, options, etc.)
CREATE TABLE IF NOT EXISTS public.kite_instruments (
  instrument_token   BIGINT PRIMARY KEY,
  exchange_token     BIGINT,
  tradingsymbol      VARCHAR(255) NOT NULL,
  name               VARCHAR(255),
  last_price         DOUBLE PRECISION,
  expiry             DATE,
  strike             DOUBLE PRECISION,
  tick_size          DOUBLE PRECISION,
  lot_size           INTEGER,
  instrument_type    VARCHAR(32),          -- e.g., EQ, FUT, CE, PE
  segment            VARCHAR(32),          -- e.g., NSE, NFO-OPT, NFO-FUT, MCX-FUT, INDICES
  exchange           VARCHAR(16),          -- e.g., NSE, BSE, NFO, BFO, MCX
  -- Search-enrichment fields:
  underlying         VARCHAR(255),         -- parsed underlying (e.g., NIFTY, RELIANCE)
  option_type        VARCHAR(10),          -- CE, PE or NULL for non-options
  last_updated       TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Indices table (kept separate for historical/index-specific workflows)
-- We do NOT add the search-only columns here to keep the table purpose minimal.
CREATE TABLE IF NOT EXISTS public.kite_indices (
  instrument_token   BIGINT PRIMARY KEY,
  exchange_token     BIGINT,
  tradingsymbol      VARCHAR(255) NOT NULL,
  name               VARCHAR(255),
  last_price         DOUBLE PRECISION,
  expiry             DATE,
  strike             DOUBLE PRECISION,
  tick_size          DOUBLE PRECISION,
  lot_size           INTEGER,
  instrument_type    VARCHAR(32),
  segment            VARCHAR(32),
  exchange           VARCHAR(16),
  last_updated       TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- =========================================
-- Indexes (search-critical)
-- =========================================

-- Speed up common search filters on instruments:
CREATE INDEX IF NOT EXISTS idx_kite_instruments_tradingsymbol
  ON public.kite_instruments (tradingsymbol);

CREATE INDEX IF NOT EXISTS idx_kite_instruments_underlying
  ON public.kite_instruments (underlying);

CREATE INDEX IF NOT EXISTS idx_kite_instruments_option_type
  ON public.kite_instruments (option_type);

CREATE INDEX IF NOT EXISTS idx_kite_instruments_underlying_opt_exp_strike
  ON public.kite_instruments (underlying, option_type, expiry, strike);

CREATE INDEX IF NOT EXISTS idx_kite_instruments_insttype_exchange
  ON public.kite_instruments (instrument_type, exchange);

-- Helpful when searching by expiry or strike specifically:
CREATE INDEX IF NOT EXISTS idx_kite_instruments_expiry
  ON public.kite_instruments (expiry);

CREATE INDEX IF NOT EXISTS idx_kite_instruments_strike
  ON public.kite_instruments (strike);

-- Optional indexes for indices table (lightweight):
CREATE INDEX IF NOT EXISTS idx_kite_indices_tradingsymbol
  ON public.kite_indices (tradingsymbol);

CREATE INDEX IF NOT EXISTS idx_kite_indices_segment
  ON public.kite_indices (segment);

-- Table for single-user settings (e.g., marketwatch subscriptions)
CREATE TABLE IF NOT EXISTS public.user_settings (
  owner_id           VARCHAR(255) PRIMARY KEY DEFAULT 'default',
  settings_json      JSONB,
  last_updated       TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Auth/session storage for broker integrations
CREATE TABLE IF NOT EXISTS public.kite_sessions (
  session_id         VARCHAR(36) PRIMARY KEY,
  access_token       TEXT NOT NULL,
  created_at         TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

ALTER TABLE public.kite_sessions
  ADD COLUMN IF NOT EXISTS broker_user_id VARCHAR(64);

CREATE INDEX IF NOT EXISTS idx_kite_sessions_broker_user_id
  ON public.kite_sessions (broker_user_id);

-- =========================================
-- Unified search view
-- =========================================

-- Recreate the view to surface the same columns from both tables.
DROP VIEW IF EXISTS public.instruments_search_v;

CREATE OR REPLACE VIEW public.instruments_search_v AS
  -- Instruments side (has underlying and option_type)
  SELECT
    i.instrument_token,
    i.exchange_token,
    i.tradingsymbol,
    i.name,
    i.last_price,
    i.expiry,
    i.strike,
    i.tick_size,
    i.lot_size,
    i.instrument_type,
    i.segment,
    i.exchange,
    i.underlying,
    i.option_type,
    i.last_updated
  FROM public.kite_instruments i

  UNION ALL

  -- Indices side (no underlying/option_type; expose as NULLs to keep schema aligned)
  SELECT
    idx.instrument_token,
    idx.exchange_token,
    idx.tradingsymbol,
    idx.name,
    idx.last_price,
    idx.expiry,
    idx.strike,
    idx.tick_size,
    idx.lot_size,
    idx.instrument_type,
    idx.segment,
    idx.exchange,
    NULL::VARCHAR(255) AS underlying,
    NULL::VARCHAR(10)  AS option_type,
    idx.last_updated
  FROM public.kite_indices idx;

-- =========================================
-- Ingestion and Ticker-Specific Data
-- =========================================

-- Table for enriched ticker data, including sector and other metadata from ingestion.
CREATE TABLE IF NOT EXISTS public.kite_ticker_tickers (
  instrument_token   BIGINT,
  tradingsymbol      VARCHAR(255) NOT NULL,
  company_name       VARCHAR(255),
  sector             VARCHAR(255),
  exchange           VARCHAR(20),
  isin_code          VARCHAR(32),
  series             VARCHAR(32),
  source_list        VARCHAR(255) NOT NULL,
  source_url         TEXT,
  weight_source      VARCHAR(128),
  baseline_close     NUMERIC(18, 6),
  baseline_index_weight NUMERIC(10, 4),
  baseline_freefloat_marketcap NUMERIC(20, 2),
  baseline_ff_factor NUMERIC(24, 10),
  baseline_as_of_date DATE,
  needs_weight_review BOOLEAN NOT NULL DEFAULT FALSE,
  -- OHLC data (close is previous day's close, used as baseline)
  open               NUMERIC(18, 6),
  high               NUMERIC(18, 6),
  low                NUMERIC(18, 6),
  close              NUMERIC(18, 6),
  -- Current price and change metrics
  ltp                NUMERIC(18, 6),
  change_1d          NUMERIC(10, 4),
  net_change         NUMERIC(18, 6),
  net_change_percent NUMERIC(10, 4),
  -- Index metrics
  return_attribution NUMERIC(10, 4),
  index_weight       NUMERIC(10, 4),
  freefloat_marketcap NUMERIC(20, 2),
  points_contribution NUMERIC(18, 4),
  last_updated       TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  last_refreshed_at  TIMESTAMP WITH TIME ZONE,
  PRIMARY KEY (instrument_token, source_list)
);

ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS change_1d NUMERIC(10, 4);
ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS isin_code VARCHAR(32);
ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS series VARCHAR(32);
ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS source_url TEXT;
ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS weight_source VARCHAR(128);
ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS points_contribution NUMERIC(18, 4);
ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS last_refreshed_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS baseline_close NUMERIC(18, 6);
ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS baseline_index_weight NUMERIC(10, 4);
ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS baseline_freefloat_marketcap NUMERIC(20, 2);
ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS baseline_ff_factor NUMERIC(24, 10);
ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS baseline_as_of_date DATE;
ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS needs_weight_review BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS public.index_refresh_state (
  source_list VARCHAR(255) PRIMARY KEY,
  last_constituent_refresh_at TIMESTAMP WITH TIME ZONE,
  last_live_refresh_at TIMESTAMP WITH TIME ZONE,
  added_symbols_json TEXT,
  removed_symbols_json TEXT,
  needs_review BOOLEAN NOT NULL DEFAULT FALSE,
  last_error TEXT,
  updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);


-- =========================================
-- Historical Data and Watchlists
-- =========================================

-- Table for historical OHLCV candle data
CREATE TABLE IF NOT EXISTS public.historical_candles (
  instrument_token   BIGINT NOT NULL,
  interval           TEXT NOT NULL,
  ts                 TIMESTAMPTZ NOT NULL,
  open               NUMERIC(18,6) NOT NULL,
  high               NUMERIC(18,6) NOT NULL,
  low                NUMERIC(18,6) NOT NULL,
  close              NUMERIC(18,6) NOT NULL,
  volume             BIGINT,
  oi                 BIGINT,
  created_at         TIMESTAMPTZ DEFAULT NOW(),
  updated_at         TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (instrument_token, interval, ts)
);

-- Index for efficient querying of historical candles
CREATE INDEX IF NOT EXISTS idx_hist_candles_token_interval_ts
  ON public.historical_candles (instrument_token, interval, ts DESC);

-- Legacy historical data table still used by broker/performance/momentum flows.
CREATE TABLE IF NOT EXISTS public.kite_historical_data (
  instrument_token   BIGINT NOT NULL,
  tradingsymbol      VARCHAR(255) NOT NULL,
  "timestamp"       TIMESTAMPTZ NOT NULL,
  interval           TEXT NOT NULL,
  open               NUMERIC(18,6) NOT NULL,
  high               NUMERIC(18,6) NOT NULL,
  low                NUMERIC(18,6) NOT NULL,
  close              NUMERIC(18,6) NOT NULL,
  volume             BIGINT,
  oi                 BIGINT,
  created_at         TIMESTAMPTZ DEFAULT NOW(),
  updated_at         TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (instrument_token, "timestamp", interval)
);

CREATE INDEX IF NOT EXISTS idx_kite_historical_data_token_interval_ts
  ON public.kite_historical_data (instrument_token, interval, "timestamp" DESC);

-- Legacy index historical data table used by index backfill flows.
CREATE TABLE IF NOT EXISTS public.kite_indices_historical_data (
  instrument_token   BIGINT NOT NULL,
  tradingsymbol      VARCHAR(255) NOT NULL,
  "timestamp"       TIMESTAMPTZ NOT NULL,
  interval           TEXT NOT NULL,
  open               NUMERIC(18,6) NOT NULL,
  high               NUMERIC(18,6) NOT NULL,
  low                NUMERIC(18,6) NOT NULL,
  close              NUMERIC(18,6) NOT NULL,
  volume             BIGINT,
  oi                 BIGINT,
  created_at         TIMESTAMPTZ DEFAULT NOW(),
  updated_at         TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (instrument_token, "timestamp", interval)
);

CREATE INDEX IF NOT EXISTS idx_kite_indices_historical_data_token_interval_ts
  ON public.kite_indices_historical_data (instrument_token, interval, "timestamp" DESC);

-- Covering index for momentum scans (latest & 252nd closes per tradingsymbol)
CREATE INDEX IF NOT EXISTS idx_kite_hist_tradingsymbol_ts
  ON public.kite_historical_data (tradingsymbol, "timestamp" DESC);

-- Table for user-specific watchlists
CREATE TABLE IF NOT EXISTS public.user_watchlists (
  owner_id           VARCHAR(255) NOT NULL DEFAULT 'default',
  instrument_token   BIGINT NOT NULL,
  tradingsymbol      TEXT,
  name               TEXT,
  exchange           TEXT,
  instrument_type    TEXT,
  PRIMARY KEY (owner_id, instrument_token)
);

-- Index for user watchlists
CREATE INDEX IF NOT EXISTS idx_user_watchlists_owner
  ON public.user_watchlists (owner_id);

-- =========================================
-- Kite Connect Webhook / Postback Events
-- =========================================

-- Table for storing Kite Connect postback events
CREATE TABLE IF NOT EXISTS public.order_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  status TEXT NOT NULL,
  event_timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
  received_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  exchange TEXT,
  tradingsymbol TEXT,
  instrument_token BIGINT,
  transaction_type TEXT,
  quantity INT,
  filled_quantity INT,
  average_price NUMERIC(18,6),
  payload_json JSONB NOT NULL,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

ALTER TABLE public.order_events
  ADD COLUMN IF NOT EXISTS event_fingerprint TEXT;

-- Unique constraint for idempotency
DROP INDEX IF EXISTS ux_order_events_unique;

-- Indexes for efficient querying
CREATE INDEX IF NOT EXISTS idx_order_events_order_id
  ON public.order_events (order_id);

CREATE INDEX IF NOT EXISTS idx_order_events_user_id
  ON public.order_events (user_id);

CREATE INDEX IF NOT EXISTS idx_order_events_status
  ON public.order_events (status);

CREATE INDEX IF NOT EXISTS idx_order_events_timestamp
  ON public.order_events (event_timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_order_events_received
  ON public.order_events (received_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS ux_order_events_event_fingerprint
  ON public.order_events (event_fingerprint)
  WHERE event_fingerprint IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.ws_order_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id TEXT,
  user_id TEXT,
  status TEXT,
  event_timestamp TIMESTAMPTZ NOT NULL,
  received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  exchange TEXT,
  tradingsymbol TEXT,
  instrument_token BIGINT,
  transaction_type TEXT,
  quantity INT,
  filled_quantity INT,
  average_price NUMERIC(18,6),
  payload_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.ws_order_events
  ADD COLUMN IF NOT EXISTS event_fingerprint TEXT;

CREATE INDEX IF NOT EXISTS idx_ws_order_events_order_id
  ON public.ws_order_events (order_id);

CREATE INDEX IF NOT EXISTS idx_ws_order_events_user_id
  ON public.ws_order_events (user_id);

CREATE INDEX IF NOT EXISTS idx_ws_order_events_status
  ON public.ws_order_events (status);

CREATE INDEX IF NOT EXISTS idx_ws_order_events_timestamp
  ON public.ws_order_events (event_timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_ws_order_events_received
  ON public.ws_order_events (received_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS ux_ws_order_events_event_fingerprint
  ON public.ws_order_events (event_fingerprint)
  WHERE event_fingerprint IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.canonical_order_events (
  id BIGSERIAL PRIMARY KEY,
  account_id TEXT NOT NULL,
  source TEXT NOT NULL,
  source_event_key TEXT NOT NULL,
  raw_event_table TEXT,
  raw_event_id TEXT,
  order_id TEXT NOT NULL,
  status TEXT NOT NULL,
  event_timestamp TIMESTAMPTZ NOT NULL,
  exchange_update_timestamp TIMESTAMPTZ,
  exchange TEXT,
  tradingsymbol TEXT,
  instrument_token BIGINT,
  product TEXT,
  transaction_type TEXT,
  quantity INT,
  filled_quantity INT NOT NULL DEFAULT 0,
  average_price NUMERIC(18,6),
  payload_json JSONB NOT NULL,
  processing_state TEXT NOT NULL DEFAULT 'pending',
  process_attempts INT NOT NULL DEFAULT 0,
  processing_started_at TIMESTAMPTZ,
  last_error TEXT,
  processed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT canonical_order_events_processing_state_chk
    CHECK (processing_state IN ('pending','processing','processed','failed'))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_canonical_order_events_source_key
  ON public.canonical_order_events (source, source_event_key);

CREATE INDEX IF NOT EXISTS idx_canonical_order_events_processing
  ON public.canonical_order_events (processing_state, created_at);

CREATE INDEX IF NOT EXISTS idx_canonical_order_events_account_order
  ON public.canonical_order_events (account_id, order_id, event_timestamp DESC);

CREATE TABLE IF NOT EXISTS public.order_state_projection (
  account_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  latest_canonical_event_id BIGINT,
  latest_status TEXT NOT NULL,
  latest_event_timestamp TIMESTAMPTZ NOT NULL,
  last_seen_filled_quantity INT NOT NULL DEFAULT 0,
  dirty_for_trade_sync BOOLEAN NOT NULL DEFAULT FALSE,
  needs_reconcile BOOLEAN NOT NULL DEFAULT FALSE,
  terminal BOOLEAN NOT NULL DEFAULT FALSE,
  exchange TEXT,
  tradingsymbol TEXT,
  instrument_token BIGINT,
  product TEXT,
  transaction_type TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (account_id, order_id)
);

CREATE INDEX IF NOT EXISTS idx_order_state_projection_dirty
  ON public.order_state_projection (dirty_for_trade_sync, needs_reconcile, updated_at);

CREATE TABLE IF NOT EXISTS public.order_trade_fills (
  account_id TEXT NOT NULL,
  trade_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  instrument_token BIGINT NOT NULL,
  exchange TEXT,
  tradingsymbol TEXT,
  product TEXT NOT NULL,
  transaction_type TEXT NOT NULL,
  quantity INT NOT NULL,
  price NUMERIC(18,6) NOT NULL,
  fill_timestamp TIMESTAMPTZ NOT NULL,
  applied_to_position BOOLEAN NOT NULL DEFAULT FALSE,
  applied_at TIMESTAMPTZ,
  payload_json JSONB,
  PRIMARY KEY (account_id, trade_id)
);

CREATE INDEX IF NOT EXISTS idx_order_trade_fills_order
  ON public.order_trade_fills (account_id, order_id);

CREATE INDEX IF NOT EXISTS idx_order_trade_fills_fill_timestamp
  ON public.order_trade_fills (account_id, fill_timestamp DESC);

CREATE TABLE IF NOT EXISTS public.account_positions (
  account_id TEXT NOT NULL,
  instrument_token BIGINT NOT NULL,
  product TEXT NOT NULL,
  exchange TEXT NOT NULL,
  tradingsymbol TEXT NOT NULL,
  net_quantity INT NOT NULL DEFAULT 0,
  buy_quantity INT NOT NULL DEFAULT 0,
  sell_quantity INT NOT NULL DEFAULT 0,
  buy_value NUMERIC(18,6) NOT NULL DEFAULT 0,
  sell_value NUMERIC(18,6) NOT NULL DEFAULT 0,
  average_price NUMERIC(18,6),
  realized_pnl NUMERIC(18,6) NOT NULL DEFAULT 0,
  last_price NUMERIC(18,6),
  close_price NUMERIC(18,6),
  last_trade_price NUMERIC(18,6),
  last_trade_at TIMESTAMPTZ,
  last_reconciled_at TIMESTAMPTZ,
  reconcile_version BIGINT NOT NULL DEFAULT 0,
  last_updated_source TEXT NOT NULL DEFAULT 'reconcile',
  version BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (account_id, instrument_token, product)
);

ALTER TABLE public.account_positions
  ADD COLUMN IF NOT EXISTS reconcile_version BIGINT NOT NULL DEFAULT 0;

ALTER TABLE public.account_positions
  ADD COLUMN IF NOT EXISTS realized_pnl NUMERIC(18,6) NOT NULL DEFAULT 0;

ALTER TABLE public.account_positions
  ADD COLUMN IF NOT EXISTS last_updated_source TEXT NOT NULL DEFAULT 'reconcile';

CREATE INDEX IF NOT EXISTS idx_account_positions_account_token
  ON public.account_positions (account_id, instrument_token);

CREATE INDEX IF NOT EXISTS idx_account_positions_open_only
  ON public.account_positions (account_id, instrument_token)
  WHERE net_quantity <> 0;

-- =========================================
-- Modular Algo Runtime Tables
-- =========================================

CREATE TABLE IF NOT EXISTS public.algo_instances (
  instance_id TEXT PRIMARY KEY,
  algo_type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'enabled',
  execution_mode TEXT NOT NULL DEFAULT 'live',
  config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  dependency_spec_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT algo_instances_status_check CHECK (status IN ('enabled', 'running', 'paused', 'stopped', 'error')),
  CONSTRAINT algo_instances_execution_mode_check CHECK (execution_mode IN ('live', 'paper', 'dry_run'))
);

ALTER TABLE public.algo_instances
  ADD COLUMN IF NOT EXISTS algo_type TEXT;

ALTER TABLE public.algo_instances
  ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'enabled';

ALTER TABLE public.algo_instances
  ADD COLUMN IF NOT EXISTS execution_mode TEXT NOT NULL DEFAULT 'live';

ALTER TABLE public.algo_instances
  DROP CONSTRAINT IF EXISTS algo_instances_execution_mode_check;

ALTER TABLE public.algo_instances
  ADD CONSTRAINT algo_instances_execution_mode_check CHECK (execution_mode IN ('live', 'paper', 'dry_run'));

ALTER TABLE public.algo_instances
  ADD COLUMN IF NOT EXISTS config_json JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE public.algo_instances
  ADD COLUMN IF NOT EXISTS dependency_spec_json JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE public.algo_instances
  ADD COLUMN IF NOT EXISTS metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE public.algo_instances
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

ALTER TABLE public.algo_instances
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_algo_instances_status
  ON public.algo_instances (status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_algo_instances_type
  ON public.algo_instances (algo_type, updated_at DESC);

CREATE TABLE IF NOT EXISTS public.algo_instance_checkpoints (
  instance_id TEXT PRIMARY KEY REFERENCES public.algo_instances(instance_id) ON DELETE CASCADE,
  last_evaluated_at TIMESTAMPTZ,
  last_action_json JSONB,
  state_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.algo_instance_checkpoints
  ADD COLUMN IF NOT EXISTS last_evaluated_at TIMESTAMPTZ;

ALTER TABLE public.algo_instance_checkpoints
  ADD COLUMN IF NOT EXISTS last_action_json JSONB;

ALTER TABLE public.algo_instance_checkpoints
  ADD COLUMN IF NOT EXISTS state_json JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE public.algo_instance_checkpoints
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_algo_checkpoints_updated
  ON public.algo_instance_checkpoints (updated_at DESC);

-- =========================================
-- Paper Runtime Tables
-- =========================================

CREATE TABLE IF NOT EXISTS public.paper_accounts (
  account_scope TEXT PRIMARY KEY,
  currency TEXT NOT NULL DEFAULT 'INR',
  starting_balance NUMERIC(18,6) NOT NULL DEFAULT 0,
  available_funds NUMERIC(18,6) NOT NULL DEFAULT 0,
  blocked_funds NUMERIC(18,6) NOT NULL DEFAULT 0,
  realized_pnl NUMERIC(18,6) NOT NULL DEFAULT 0,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.paper_orders (
  account_scope TEXT NOT NULL,
  order_id TEXT NOT NULL,
  instrument_token BIGINT NOT NULL,
  exchange TEXT NOT NULL DEFAULT 'NSE',
  tradingsymbol TEXT,
  product TEXT NOT NULL DEFAULT 'MIS',
  transaction_type TEXT NOT NULL,
  order_type TEXT NOT NULL DEFAULT 'market',
  quantity INT NOT NULL,
  filled_quantity INT NOT NULL DEFAULT 0,
  pending_quantity INT NOT NULL DEFAULT 0,
  price NUMERIC(18,6),
  trigger_price NUMERIC(18,6),
  average_price NUMERIC(18,6),
  status TEXT NOT NULL DEFAULT 'pending',
  placed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at TIMESTAMPTZ,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (account_scope, order_id),
  CONSTRAINT fk_paper_order_account FOREIGN KEY (account_scope)
    REFERENCES public.paper_accounts(account_scope) ON DELETE CASCADE,
  CONSTRAINT paper_order_transaction_type_check CHECK (transaction_type IN ('buy', 'sell')),
  CONSTRAINT paper_order_type_check CHECK (order_type IN ('market', 'limit', 'sl', 'sl_m')),
  CONSTRAINT paper_order_status_check CHECK (status IN ('pending', 'open', 'partially_filled', 'filled', 'cancelled', 'rejected', 'expired')),
  CONSTRAINT paper_order_qty_check CHECK (quantity > 0 AND filled_quantity >= 0 AND pending_quantity >= 0)
);

CREATE INDEX IF NOT EXISTS idx_paper_orders_account_status_updated
  ON public.paper_orders (account_scope, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_paper_orders_account_token_status
  ON public.paper_orders (account_scope, instrument_token, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_paper_orders_pending_token
  ON public.paper_orders (account_scope, instrument_token, placed_at)
  WHERE status IN ('pending', 'open', 'partially_filled');

CREATE TABLE IF NOT EXISTS public.paper_trades (
  account_scope TEXT NOT NULL,
  trade_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  instrument_token BIGINT NOT NULL,
  transaction_type TEXT NOT NULL,
  quantity INT NOT NULL,
  price NUMERIC(18,6) NOT NULL,
  trade_timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (account_scope, trade_id),
  CONSTRAINT paper_trade_transaction_type_check CHECK (transaction_type IN ('buy', 'sell')),
  CONSTRAINT paper_trade_qty_check CHECK (quantity > 0)
);

CREATE INDEX IF NOT EXISTS idx_paper_trades_order
  ON public.paper_trades (account_scope, order_id, trade_timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_paper_trades_token
  ON public.paper_trades (account_scope, instrument_token, trade_timestamp DESC);

CREATE TABLE IF NOT EXISTS public.paper_positions (
  account_scope TEXT NOT NULL,
  instrument_token BIGINT NOT NULL,
  product TEXT NOT NULL DEFAULT 'MIS',
  exchange TEXT NOT NULL DEFAULT 'NSE',
  tradingsymbol TEXT,
  net_quantity INT NOT NULL DEFAULT 0,
  average_price NUMERIC(18,6) NOT NULL DEFAULT 0,
  buy_quantity INT NOT NULL DEFAULT 0,
  sell_quantity INT NOT NULL DEFAULT 0,
  buy_value NUMERIC(18,6) NOT NULL DEFAULT 0,
  sell_value NUMERIC(18,6) NOT NULL DEFAULT 0,
  realized_pnl NUMERIC(18,6) NOT NULL DEFAULT 0,
  unrealized_pnl NUMERIC(18,6) NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (account_scope, instrument_token, product),
  CONSTRAINT fk_paper_position_account FOREIGN KEY (account_scope)
    REFERENCES public.paper_accounts(account_scope) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_paper_positions_account_updated
  ON public.paper_positions (account_scope, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_paper_positions_open_only
  ON public.paper_positions (account_scope, instrument_token)
  WHERE net_quantity <> 0;

CREATE TABLE IF NOT EXISTS public.paper_position_lots (
  account_scope TEXT NOT NULL,
  lot_id TEXT NOT NULL,
  instrument_token BIGINT NOT NULL,
  product TEXT NOT NULL DEFAULT 'MIS',
  source_trade_id TEXT NOT NULL,
  source_order_id TEXT,
  open_quantity INT NOT NULL,
  remaining_quantity INT NOT NULL,
  entry_price NUMERIC(18,6) NOT NULL,
  opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  closed_at TIMESTAMPTZ,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (account_scope, lot_id),
  CONSTRAINT fk_paper_lot_account FOREIGN KEY (account_scope)
    REFERENCES public.paper_accounts(account_scope) ON DELETE CASCADE,
  CONSTRAINT paper_position_lot_qty_check CHECK (open_quantity > 0 AND remaining_quantity >= 0 AND remaining_quantity <= open_quantity)
);

CREATE INDEX IF NOT EXISTS idx_paper_position_lots_opened
  ON public.paper_position_lots (account_scope, instrument_token, product, opened_at);

CREATE INDEX IF NOT EXISTS idx_paper_position_lots_source_trade
  ON public.paper_position_lots (account_scope, source_trade_id);

CREATE TABLE IF NOT EXISTS public.paper_fund_ledger (
  entry_id BIGSERIAL PRIMARY KEY,
  account_scope TEXT NOT NULL,
  entry_type TEXT NOT NULL,
  amount NUMERIC(18,6) NOT NULL,
  balance_after NUMERIC(18,6),
  reference_type TEXT,
  reference_id TEXT,
  notes TEXT,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT fk_paper_fund_account FOREIGN KEY (account_scope)
    REFERENCES public.paper_accounts(account_scope) ON DELETE CASCADE,
  CONSTRAINT paper_fund_entry_type_check CHECK (entry_type IN ('credit', 'debit', 'reserve', 'release', 'adjustment'))
);

CREATE INDEX IF NOT EXISTS idx_paper_fund_ledger_account_created
  ON public.paper_fund_ledger (account_scope, created_at DESC, entry_id DESC);

CREATE INDEX IF NOT EXISTS idx_paper_fund_ledger_reference
  ON public.paper_fund_ledger (account_scope, reference_type, reference_id)
  WHERE reference_id IS NOT NULL;

-- =========================================
-- Algo Worker API Tables
-- =========================================

CREATE TABLE IF NOT EXISTS public.algo_worker_tokens (
  token_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  account_scope TEXT,
  allowed_modes JSONB NOT NULL DEFAULT '["paper", "dry_run"]'::jsonb,
  allowed_actions JSONB NOT NULL DEFAULT '[]'::jsonb,
  allowed_templates JSONB NOT NULL DEFAULT '[]'::jsonb,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
  heartbeat_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at TIMESTAMPTZ,
  last_used_at TIMESTAMPTZ,
  last_heartbeat_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_algo_worker_tokens_status
  ON public.algo_worker_tokens (status, created_at DESC);

CREATE TABLE IF NOT EXISTS public.algo_worker_runs (
  strategy_run_id TEXT PRIMARY KEY,
  token_id TEXT NOT NULL,
  template_id TEXT NOT NULL,
  account_scope TEXT NOT NULL,
  execution_mode TEXT NOT NULL CHECK (execution_mode IN ('paper', 'dry_run', 'live')),
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'paused', 'exiting', 'closed', 'failed')),
  summary_fields_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  risk_schema_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  allowed_actions_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  runtime_state_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  worker_session_nonce TEXT,
  worker_session_claimed_at TIMESTAMPTZ,
  last_heartbeat_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  closed_at TIMESTAMPTZ
);

ALTER TABLE public.algo_worker_runs
  ADD COLUMN IF NOT EXISTS worker_session_nonce TEXT;

ALTER TABLE public.algo_worker_runs
  ADD COLUMN IF NOT EXISTS worker_session_claimed_at TIMESTAMPTZ;

ALTER TABLE public.algo_worker_runs
  ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_algo_worker_runs_account_status
  ON public.algo_worker_runs (account_scope, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_algo_worker_runs_token
  ON public.algo_worker_runs (token_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS public.algo_worker_intents (
  intent_id BIGSERIAL PRIMARY KEY,
  token_id TEXT NOT NULL,
  strategy_run_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  intent_type TEXT NOT NULL,
  request_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL,
  result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (strategy_run_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_algo_worker_intents_run_created
  ON public.algo_worker_intents (strategy_run_id, created_at DESC);

CREATE TABLE IF NOT EXISTS public.basket_executions (
  basket_execution_id TEXT PRIMARY KEY,
  strategy_run_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  execution_mode TEXT NOT NULL,
  status TEXT NOT NULL,
  all_or_none BOOLEAN NOT NULL DEFAULT FALSE,
  action_required BOOLEAN NOT NULL DEFAULT FALSE,
  action_reason TEXT,
  rollback_status TEXT NOT NULL DEFAULT 'none',
  requested_leg_count INTEGER NOT NULL DEFAULT 0,
  completed_leg_count INTEGER NOT NULL DEFAULT 0,
  terminal_leg_count INTEGER NOT NULL DEFAULT 0,
  total_requested_quantity INTEGER NOT NULL DEFAULT 0,
  total_filled_quantity INTEGER NOT NULL DEFAULT 0,
  latest_event_cursor BIGINT,
  latest_event_at TIMESTAMPTZ,
  request_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.basket_execution_legs (
  basket_execution_id TEXT NOT NULL REFERENCES public.basket_executions(basket_execution_id) ON DELETE CASCADE,
  leg_index INTEGER NOT NULL,
  status TEXT NOT NULL,
  exchange TEXT,
  tradingsymbol TEXT,
  product TEXT,
  transaction_type TEXT,
  requested_quantity INTEGER NOT NULL DEFAULT 0,
  broker_order_id TEXT,
  client_order_ref TEXT,
  latest_broker_status TEXT,
  last_seen_filled_quantity INTEGER NOT NULL DEFAULT 0,
  average_price DOUBLE PRECISION,
  request_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (basket_execution_id, leg_index)
);

CREATE TABLE IF NOT EXISTS public.worker_execution_events (
  cursor BIGSERIAL PRIMARY KEY,
  strategy_run_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  basket_execution_id TEXT,
  event_type TEXT NOT NULL,
  payload_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_basket_executions_run_status
  ON public.basket_executions (strategy_run_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_worker_execution_events_run_cursor
  ON public.worker_execution_events (strategy_run_id, cursor);

CREATE INDEX IF NOT EXISTS idx_worker_execution_events_basket_cursor
  ON public.worker_execution_events (basket_execution_id, cursor)
  WHERE basket_execution_id IS NOT NULL;

-- =========================================
-- Index Stoploss Strategy Tables
-- =========================================

CREATE TABLE IF NOT EXISTS public.position_protection_strategies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id VARCHAR(255) NOT NULL DEFAULT 'default',
    name VARCHAR(255),
    strategy_type VARCHAR(50) DEFAULT 'manual',
    status VARCHAR(50) DEFAULT 'active', -- active, paused, completed, triggered, error, partial
    monitoring_mode VARCHAR(50) NOT NULL, -- index, combined_premium
    
    -- Index Config
    index_instrument_token BIGINT,
    index_tradingsymbol VARCHAR(255),
    index_exchange VARCHAR(20),
    index_upper_stoploss NUMERIC(18,6),
    index_lower_stoploss NUMERIC(18,6),
    
    -- Order Config
    stoploss_order_type VARCHAR(20) DEFAULT 'MARKET',
    stoploss_limit_offset NUMERIC(18,6),
    
    -- Trailing Config
    trailing_mode VARCHAR(50) DEFAULT 'none',
    trailing_distance NUMERIC(18,6),
    trailing_unit VARCHAR(20) DEFAULT 'points',
    trailing_step_size NUMERIC(18,6),
    trailing_lock_profit NUMERIC(18,6),
    trailing_state JSONB, -- Stores current level, activation status
    
    -- Combined Premium Config & State
    combined_premium_entry_type VARCHAR(20), -- credit, debit
    combined_premium_profit_target NUMERIC(18,6),
    combined_premium_trailing_enabled BOOLEAN DEFAULT FALSE,
    combined_premium_trailing_distance NUMERIC(18,6),
    combined_premium_trailing_lock_profit NUMERIC(18,6),
    combined_premium_levels JSONB, -- List of partial exit levels
    
    combined_premium_state JSONB, -- current_net_premium, net_pnl, etc.
    
    -- Position Data
    position_snapshot JSONB, -- List of positions at creation
    remaining_quantities JSONB, -- Tracking remaining qty per instrument
    
    -- Execution Tracking
    placed_orders JSONB DEFAULT '[]'::jsonb, -- List of orders placed
    execution_errors JSONB DEFAULT '[]'::jsonb,
    levels_executed JSONB DEFAULT '[]'::jsonb, -- List of executed level IDs
    stoploss_executed BOOLEAN DEFAULT FALSE,
    
    -- Audit & Runtime
    last_evaluated_price NUMERIC(18,6),
    last_evaluated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_pps_user_status ON public.position_protection_strategies(user_id, status);
CREATE INDEX IF NOT EXISTS idx_pps_token ON public.position_protection_strategies(index_instrument_token);
CREATE INDEX IF NOT EXISTS idx_pps_created ON public.position_protection_strategies(created_at DESC);

CREATE TABLE IF NOT EXISTS public.strategy_events (
    id BIGSERIAL PRIMARY KEY,
    strategy_id UUID NOT NULL REFERENCES public.position_protection_strategies(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    trigger_price NUMERIC(18,6),
    order_id TEXT,
    instrument_token BIGINT,
    quantity_affected INT,
    error_message TEXT,
    meta JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_strat_events_strat_id ON public.strategy_events(strategy_id);
CREATE INDEX IF NOT EXISTS idx_strat_events_created ON public.strategy_events(created_at DESC);

CREATE TABLE IF NOT EXISTS public.option_strategy_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    underlying VARCHAR(32) NOT NULL,
    expiry DATE NOT NULL,
    user_intent VARCHAR(128) NOT NULL,
    inferred_structure VARCHAR(128) NOT NULL,
    inferred_family VARCHAR(64) NOT NULL,
    execution_mode VARCHAR(16) NOT NULL CHECK (execution_mode IN ('dry_run', 'paper', 'live')),
    status VARCHAR(32) NOT NULL DEFAULT 'planned' CHECK (status IN ('planned', 'success', 'partial', 'failed')),
    selected_legs JSONB NOT NULL DEFAULT '[]'::jsonb,
    canonical_strategy JSONB NOT NULL DEFAULT '{}'::jsonb,
    order_plan JSONB NOT NULL DEFAULT '{}'::jsonb,
    execution_result JSONB,
    algo_instance_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_option_strategy_runs_created ON public.option_strategy_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_option_strategy_runs_mode_status ON public.option_strategy_runs(execution_mode, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS public.option_run_states (
    strategy_run_id TEXT PRIMARY KEY,
    strategy_name TEXT NOT NULL,
    product VARCHAR(8) NOT NULL CHECK (product IN ('MIS', 'NRML')),
    status VARCHAR(64) NOT NULL,
    legs JSONB NOT NULL DEFAULT '[]'::jsonb,
    protection JSONB,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    orders JSONB NOT NULL DEFAULT '[]'::jsonb,
    trades JSONB NOT NULL DEFAULT '[]'::jsonb,
    completed_legs JSONB NOT NULL DEFAULT '[]'::jsonb,
    failed_legs JSONB NOT NULL DEFAULT '[]'::jsonb,
    pending_legs JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_option_run_states_status
    ON public.option_run_states(status);

CREATE INDEX IF NOT EXISTS idx_option_run_states_updated
    ON public.option_run_states(updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_option_run_states_product
    ON public.option_run_states(product);

-- =========================================
-- Portfolio snapshots and history
-- =========================================

CREATE TABLE IF NOT EXISTS public.portfolio_snapshots (
    id BIGSERIAL PRIMARY KEY,
    "timestamp" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    strategy_name VARCHAR(255) NOT NULL,
    symbol VARCHAR(255) NOT NULL,
    quantity INTEGER NOT NULL,
    purchase_price NUMERIC(18,6) NOT NULL,
    total_value NUMERIC(18,6) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_strategy_ts
    ON public.portfolio_snapshots(strategy_name, "timestamp" DESC);

CREATE TABLE IF NOT EXISTS public.portfolio_history (
    id BIGSERIAL PRIMARY KEY,
    "timestamp" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    strategy_name VARCHAR(255) NOT NULL,
    total_capital NUMERIC(18,6) NOT NULL,
    total_value NUMERIC(18,6) NOT NULL,
    profit_loss NUMERIC(18,6) NOT NULL,
    percentage_change NUMERIC(18,6) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_portfolio_history_strategy_ts
    ON public.portfolio_history(strategy_name, "timestamp" DESC);

-- =========================================
-- Investing Strategies Table
-- =========================================

CREATE TABLE IF NOT EXISTS public.investing_strategies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id VARCHAR(50),
    kite_ref_tag VARCHAR(20),  -- Tag sent to Kite API (max 20 chars), format: MOM-N50-25-11-26
    strategy_name VARCHAR(255) NOT NULL,
    strategy_type VARCHAR(50) NOT NULL,
    tag VARCHAR(50) NOT NULL,
    instrument_token BIGINT NOT NULL,
    tradingsymbol VARCHAR(255) NOT NULL,
    exchange VARCHAR(20) DEFAULT 'NSE',
    quantity INTEGER NOT NULL,
    invested_amount NUMERIC(18,2),
    entry_price NUMERIC(18,2),
    entry_date TIMESTAMPTZ DEFAULT NOW(),
    last_price NUMERIC(18,2),
    pnl NUMERIC(18,2),
    pnl_percent NUMERIC(10,2),
    status VARCHAR(20) DEFAULT 'ACTIVE',
    exit_date TIMESTAMPTZ,
    exit_price NUMERIC(18,2),
    linked_index_token BIGINT,
    linked_index_symbol VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_investing_strategies_name ON public.investing_strategies(strategy_name);
CREATE INDEX IF NOT EXISTS idx_investing_strategies_tag ON public.investing_strategies(tag);
CREATE INDEX IF NOT EXISTS idx_investing_strategies_status ON public.investing_strategies(status);
CREATE INDEX IF NOT EXISTS idx_investing_strategies_order_id ON public.investing_strategies(order_id) WHERE order_id IS NOT NULL;

-- =========================================
-- Trading Journal Tables
-- =========================================

CREATE TABLE IF NOT EXISTS public.journal_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_family TEXT NOT NULL,
    strategy_name TEXT,
    entry_surface TEXT,
    execution_mode TEXT NOT NULL CHECK (execution_mode IN ('live', 'paper', 'dry_run')),
    account_ref TEXT,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'open', 'closed', 'cancelled', 'reviewed')),
    benchmark_id TEXT NOT NULL DEFAULT 'NIFTY50',
    capital_basis_type TEXT NOT NULL CHECK (capital_basis_type IN ('cash_deployed', 'margin_used', 'notional', 'portfolio_nav')),
    capital_committed NUMERIC(18,6),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    review_state TEXT NOT NULL DEFAULT 'pending' CHECK (review_state IN ('pending', 'in_progress', 'reviewed', 'waived')),
    source_summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT journal_runs_strategy_family_chk CHECK (strategy_family IN ('options_strategy', 'indicator_strategy', 'investment_strategy', 'discretionary_strategy'))
);

CREATE INDEX IF NOT EXISTS idx_journal_runs_family_started
    ON public.journal_runs (strategy_family, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_journal_runs_status_started
    ON public.journal_runs (status, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_journal_runs_benchmark_started
    ON public.journal_runs (benchmark_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_journal_runs_entry_surface
    ON public.journal_runs (entry_surface, started_at DESC)
    WHERE entry_surface IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.journal_run_legs (
    id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES public.journal_runs(id) ON DELETE CASCADE,
    instrument_token BIGINT,
    exchange TEXT,
    tradingsymbol TEXT,
    product TEXT,
    leg_role TEXT,
    direction TEXT CHECK (direction IN ('long', 'short')),
    opened_quantity INT NOT NULL DEFAULT 0,
    closed_quantity INT NOT NULL DEFAULT 0,
    net_quantity INT NOT NULL DEFAULT 0,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_journal_run_legs_run
    ON public.journal_run_legs (run_id, id);

CREATE INDEX IF NOT EXISTS idx_journal_run_legs_token
    ON public.journal_run_legs (instrument_token)
    WHERE instrument_token IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.live_order_intents (
  intent_id TEXT PRIMARY KEY,
  client_order_ref TEXT NOT NULL,
  account_id TEXT NOT NULL,
  strategy_run_id TEXT NOT NULL,
  journal_run_id UUID,
  strategy_family TEXT NOT NULL,
  strategy_name TEXT NOT NULL,
  execution_mode TEXT NOT NULL DEFAULT 'live',
  entry_surface TEXT NOT NULL,
  idempotency_key TEXT,
  broker_order_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  attribution_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  cost_contract_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  error_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.live_order_intents
  ADD COLUMN IF NOT EXISTS basket_execution_id TEXT,
  ADD COLUMN IF NOT EXISTS basket_leg_index INTEGER;

CREATE UNIQUE INDEX IF NOT EXISTS ux_live_order_intents_client_order_ref
  ON public.live_order_intents (client_order_ref);

CREATE INDEX IF NOT EXISTS idx_live_order_intents_broker_order
  ON public.live_order_intents (account_id, broker_order_id);

CREATE INDEX IF NOT EXISTS idx_live_order_intents_strategy
  ON public.live_order_intents (strategy_run_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_live_order_intents_account_order_basket
  ON public.live_order_intents (account_id, broker_order_id, basket_execution_id);

CREATE TABLE IF NOT EXISTS public.journal_source_links (
    id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES public.journal_runs(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    source_key TEXT NOT NULL,
    source_key_2 TEXT,
    linked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT journal_source_links_source_type_chk CHECK (source_type IN ('live_order', 'paper_trade', 'paper_order', 'paper_strategy_run', 'option_strategy_run', 'algo_instance', 'investing_strategy', 'live_fill', 'broker_import'))
);

ALTER TABLE public.journal_source_links
    DROP CONSTRAINT IF EXISTS journal_source_links_source_type_chk;

ALTER TABLE public.journal_source_links
    ADD CONSTRAINT journal_source_links_source_type_chk CHECK (source_type IN ('live_order', 'paper_trade', 'paper_order', 'paper_strategy_run', 'option_strategy_run', 'algo_instance', 'investing_strategy', 'live_fill', 'broker_import'));

CREATE INDEX IF NOT EXISTS idx_journal_source_links_run
    ON public.journal_source_links (run_id, linked_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS ux_journal_source_links_source_key
    ON public.journal_source_links (source_type, source_key, COALESCE(source_key_2, ''));

-- Journal V2 foundation: execution environments, strategy identity, contexts, episodes, intents
CREATE TABLE IF NOT EXISTS public.journal_execution_environments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    mode TEXT NOT NULL CHECK (mode IN ('live', 'paper', 'dry_run_preview')),
    account_scope TEXT NOT NULL,
    broker_user_id TEXT,
    paper_account_key TEXT,
    environment_epoch INTEGER NOT NULL DEFAULT 1,
    display_name TEXT,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    retired_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_journal_execution_environments_identity
    ON public.journal_execution_environments (
        mode,
        account_scope,
        COALESCE(broker_user_id, ''),
        COALESCE(paper_account_key, ''),
        environment_epoch
    );

CREATE INDEX IF NOT EXISTS idx_journal_execution_environments_mode_scope
    ON public.journal_execution_environments (mode, account_scope);

CREATE TABLE IF NOT EXISTS public.journal_strategy_templates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_family TEXT NOT NULL,
    template_key TEXT NOT NULL,
    display_name TEXT,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_journal_strategy_templates_template_key
    ON public.journal_strategy_templates (template_key);

CREATE TABLE IF NOT EXISTS public.journal_strategy_variants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    template_id UUID NOT NULL REFERENCES public.journal_strategy_templates(id) ON DELETE CASCADE,
    variant_key TEXT NOT NULL,
    display_name TEXT,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_journal_strategy_variants_template_variant
    ON public.journal_strategy_variants (template_id, variant_key);

CREATE TABLE IF NOT EXISTS public.journal_strategy_deployments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    template_id UUID NOT NULL REFERENCES public.journal_strategy_templates(id) ON DELETE CASCADE,
    variant_id UUID REFERENCES public.journal_strategy_variants(id) ON DELETE SET NULL,
    deployment_key TEXT NOT NULL,
    display_name TEXT,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_journal_strategy_deployments_template_deployment
    ON public.journal_strategy_deployments (template_id, deployment_key);

CREATE TABLE IF NOT EXISTS public.journal_execution_contexts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    environment_id UUID NOT NULL REFERENCES public.journal_execution_environments(id) ON DELETE RESTRICT,
    source_system TEXT NOT NULL,
    external_run_id TEXT NOT NULL,
    strategy_template_id UUID REFERENCES public.journal_strategy_templates(id) ON DELETE SET NULL,
    strategy_variant_id UUID REFERENCES public.journal_strategy_variants(id) ON DELETE SET NULL,
    strategy_deployment_id UUID REFERENCES public.journal_strategy_deployments(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'active',
    opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_journal_execution_contexts_environment_source_external
    ON public.journal_execution_contexts (environment_id, source_system, external_run_id);

CREATE TABLE IF NOT EXISTS public.journal_episodes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    environment_id UUID NOT NULL REFERENCES public.journal_execution_environments(id) ON DELETE RESTRICT,
    execution_context_id UUID NOT NULL REFERENCES public.journal_execution_contexts(id) ON DELETE CASCADE,
    episode_seq INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ,
    notes TEXT NOT NULL DEFAULT '',
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.journal_episodes
    ADD COLUMN IF NOT EXISTS notes TEXT NOT NULL DEFAULT '';

CREATE UNIQUE INDEX IF NOT EXISTS ux_journal_episodes_context_seq
    ON public.journal_episodes (execution_context_id, episode_seq);

CREATE INDEX IF NOT EXISTS idx_journal_episodes_environment_status_opened
    ON public.journal_episodes (environment_id, status, opened_at DESC);

CREATE INDEX IF NOT EXISTS idx_journal_episodes_environment_opened_at
    ON public.journal_episodes (environment_id, opened_at DESC);

CREATE INDEX IF NOT EXISTS idx_journal_episodes_environment_closed_at
    ON public.journal_episodes (environment_id, closed_at DESC)
    WHERE closed_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.journal_episode_legs (
    id BIGSERIAL PRIMARY KEY,
    episode_id UUID NOT NULL REFERENCES public.journal_episodes(id) ON DELETE CASCADE,
    leg_seq INTEGER NOT NULL DEFAULT 1,
    instrument_token BIGINT,
    exchange TEXT,
    tradingsymbol TEXT,
    product TEXT,
    direction TEXT CHECK (direction IN ('long', 'short')),
    opened_quantity INT NOT NULL DEFAULT 0,
    closed_quantity INT NOT NULL DEFAULT 0,
    net_quantity INT NOT NULL DEFAULT 0,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_journal_episode_legs_episode_leg_seq
    ON public.journal_episode_legs (episode_id, leg_seq);

CREATE TABLE IF NOT EXISTS public.journal_execution_intents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    environment_id UUID NOT NULL REFERENCES public.journal_execution_environments(id) ON DELETE RESTRICT,
    execution_context_id UUID REFERENCES public.journal_execution_contexts(id) ON DELETE SET NULL,
    episode_id UUID REFERENCES public.journal_episodes(id) ON DELETE SET NULL,
    channel TEXT,
    intent_type TEXT,
    idempotency_key TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_journal_execution_intents_environment_idempotency
    ON public.journal_execution_intents (environment_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.journal_timeline_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    environment_id UUID NOT NULL REFERENCES public.journal_execution_environments(id) ON DELETE RESTRICT,
    episode_id UUID REFERENCES public.journal_episodes(id) ON DELETE SET NULL,
    execution_context_id UUID REFERENCES public.journal_execution_contexts(id) ON DELETE SET NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    channel TEXT,
    event_type TEXT NOT NULL,
    actor_type TEXT NOT NULL DEFAULT 'system',
    correlation_id TEXT,
    causation_id TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_journal_timeline_episode_time
    ON public.journal_timeline_events (episode_id, occurred_at ASC)
    WHERE episode_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_journal_timeline_environment_subject_time
    ON public.journal_timeline_events (environment_id, subject_type, subject_id, occurred_at ASC);

CREATE TABLE IF NOT EXISTS public.journal_notes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    environment_id UUID NOT NULL REFERENCES public.journal_execution_environments(id) ON DELETE RESTRICT,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    episode_id UUID REFERENCES public.journal_episodes(id) ON DELETE SET NULL,
    note_type TEXT NOT NULL,
    title TEXT NOT NULL,
    body_markdown TEXT NOT NULL,
    body_text TEXT NOT NULL DEFAULT '',
    body_json JSONB,
    effective_at TIMESTAMPTZ,
    author_id TEXT,
    tags_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_journal_notes_environment_subject
    ON public.journal_notes (environment_id, subject_type, subject_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_journal_notes_episode_updated
    ON public.journal_notes (episode_id, updated_at DESC)
    WHERE episode_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.journal_note_revisions (
    id BIGSERIAL PRIMARY KEY,
    note_id UUID NOT NULL REFERENCES public.journal_notes(id) ON DELETE CASCADE,
    revision_no INTEGER NOT NULL,
    body_markdown TEXT NOT NULL,
    body_text TEXT NOT NULL DEFAULT '',
    editor_id TEXT,
    edited_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    change_reason TEXT,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_journal_note_revisions_note_revision
    ON public.journal_note_revisions (note_id, revision_no);

CREATE TABLE IF NOT EXISTS public.journal_attachments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    environment_id UUID NOT NULL REFERENCES public.journal_execution_environments(id) ON DELETE RESTRICT,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    note_id UUID REFERENCES public.journal_notes(id) ON DELETE SET NULL,
    storage_key TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    sha256 TEXT,
    size_bytes BIGINT,
    ocr_text TEXT,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_journal_attachments_environment_subject_created
    ON public.journal_attachments (environment_id, subject_type, subject_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_journal_attachments_note_created
    ON public.journal_attachments (note_id, created_at DESC)
    WHERE note_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.journal_unresolved_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    environment_id UUID NOT NULL REFERENCES public.journal_execution_environments(id) ON DELETE RESTRICT,
    execution_context_id UUID REFERENCES public.journal_execution_contexts(id) ON DELETE SET NULL,
    source_system TEXT NOT NULL,
    reason TEXT NOT NULL,
    raw_identity_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    candidate_mappings_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'ignored')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_journal_unresolved_queue_environment_created
    ON public.journal_unresolved_queue (environment_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_journal_unresolved_queue_status_created
    ON public.journal_unresolved_queue (status, created_at DESC);

CREATE TABLE IF NOT EXISTS public.journal_execution_facts (
    id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES public.journal_runs(id) ON DELETE CASCADE,
    leg_id BIGINT REFERENCES public.journal_run_legs(id) ON DELETE SET NULL,
    source_type TEXT NOT NULL,
    source_fact_key TEXT NOT NULL,
    order_id TEXT,
    trade_id TEXT,
    fill_timestamp TIMESTAMPTZ NOT NULL,
    side TEXT NOT NULL,
    quantity INT NOT NULL,
    price NUMERIC(18,6) NOT NULL,
    gross_cash_flow NUMERIC(18,6),
    fees_amount NUMERIC(18,6) NOT NULL DEFAULT 0,
    taxes_amount NUMERIC(18,6) NOT NULL DEFAULT 0,
    slippage_amount NUMERIC(18,6) NOT NULL DEFAULT 0,
    brokerage NUMERIC(18,6) NOT NULL DEFAULT 0,
    exchange_txn_charge NUMERIC(18,6) NOT NULL DEFAULT 0,
    stt NUMERIC(18,6) NOT NULL DEFAULT 0,
    stamp_duty NUMERIC(18,6) NOT NULL DEFAULT 0,
    sebi_charge NUMERIC(18,6) NOT NULL DEFAULT 0,
    gst NUMERIC(18,6) NOT NULL DEFAULT 0,
    margin_required NUMERIC(18,6) NOT NULL DEFAULT 0,
    charges_status TEXT NOT NULL DEFAULT 'unavailable',
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.journal_execution_facts
    ADD COLUMN IF NOT EXISTS environment_id UUID;

ALTER TABLE public.journal_execution_facts
    ADD COLUMN IF NOT EXISTS episode_id UUID;

ALTER TABLE public.journal_execution_facts
    ADD COLUMN IF NOT EXISTS intent_id UUID;

ALTER TABLE public.journal_execution_facts
    ADD COLUMN IF NOT EXISTS position_effect TEXT;

ALTER TABLE public.journal_execution_facts
    ADD COLUMN IF NOT EXISTS brokerage NUMERIC(18,6) NOT NULL DEFAULT 0;

ALTER TABLE public.journal_execution_facts
    ADD COLUMN IF NOT EXISTS exchange_txn_charge NUMERIC(18,6) NOT NULL DEFAULT 0;

ALTER TABLE public.journal_execution_facts
    ADD COLUMN IF NOT EXISTS stt NUMERIC(18,6) NOT NULL DEFAULT 0;

ALTER TABLE public.journal_execution_facts
    ADD COLUMN IF NOT EXISTS stamp_duty NUMERIC(18,6) NOT NULL DEFAULT 0;

ALTER TABLE public.journal_execution_facts
    ADD COLUMN IF NOT EXISTS sebi_charge NUMERIC(18,6) NOT NULL DEFAULT 0;

ALTER TABLE public.journal_execution_facts
    ADD COLUMN IF NOT EXISTS gst NUMERIC(18,6) NOT NULL DEFAULT 0;

ALTER TABLE public.journal_execution_facts
    ADD COLUMN IF NOT EXISTS margin_required NUMERIC(18,6) NOT NULL DEFAULT 0;

ALTER TABLE public.journal_execution_facts
    ADD COLUMN IF NOT EXISTS charges_status TEXT NOT NULL DEFAULT 'unavailable';

ALTER TABLE public.journal_execution_facts
    DROP CONSTRAINT IF EXISTS journal_execution_facts_position_effect_chk;

ALTER TABLE public.journal_execution_facts
    ADD CONSTRAINT journal_execution_facts_position_effect_chk
    CHECK (position_effect IS NULL OR position_effect IN ('open', 'add', 'reduce', 'close', 'flip')) NOT VALID;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'journal_execution_facts_charges_status_chk'
          AND conrelid = 'public.journal_execution_facts'::regclass
    ) THEN
        ALTER TABLE public.journal_execution_facts
            ADD CONSTRAINT journal_execution_facts_charges_status_chk
            CHECK (charges_status IN ('estimated', 'broker_quoted', 'reconciled', 'unavailable')) NOT VALID;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS ux_journal_execution_facts_source_fact
    ON public.journal_execution_facts (source_type, source_fact_key);

CREATE INDEX IF NOT EXISTS idx_journal_execution_facts_run_time
    ON public.journal_execution_facts (run_id, fill_timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_journal_execution_facts_environment_episode_fill_time
    ON public.journal_execution_facts (environment_id, episode_id, fill_timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_journal_execution_facts_environment_fill_timestamp
    ON public.journal_execution_facts (environment_id, fill_timestamp DESC)
    WHERE environment_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.journal_v2_projection_claims (
    source_type TEXT NOT NULL,
    source_fact_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'processing' CHECK (status IN ('processing', 'projected', 'failed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source_type, source_fact_key)
);

CREATE INDEX IF NOT EXISTS idx_journal_v2_projection_claims_status_updated
    ON public.journal_v2_projection_claims (status, updated_at DESC);

CREATE TABLE IF NOT EXISTS public.journal_decision_events (
    id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES public.journal_runs(id) ON DELETE CASCADE,
    decision_type TEXT NOT NULL CHECK (decision_type IN ('thesis', 'entry', 'adjustment', 'risk_change', 'exit', 'algo_trigger', 'review')),
    actor_type TEXT NOT NULL CHECK (actor_type IN ('user', 'system', 'algo')),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    summary TEXT,
    context_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_journal_decision_events_run_time
    ON public.journal_decision_events (run_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS public.journal_rules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    family_scope TEXT,
    strategy_scope TEXT,
    title TEXT NOT NULL,
    rule_type TEXT NOT NULL CHECK (rule_type IN ('universal', 'strategy_specific', 'risk_execution', 'psychological')),
    enforcement_level TEXT NOT NULL CHECK (enforcement_level IN ('hard_block', 'soft_warning', 'review_only')),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'reinforced', 'decaying', 'retired')),
    version INT NOT NULL DEFAULT 1,
    description TEXT,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_journal_rules_scope
    ON public.journal_rules (family_scope, strategy_scope, status);

CREATE TABLE IF NOT EXISTS public.journal_rule_evidence (
    id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES public.journal_runs(id) ON DELETE CASCADE,
    rule_id UUID NOT NULL REFERENCES public.journal_rules(id) ON DELETE CASCADE,
    result TEXT NOT NULL CHECK (result IN ('followed', 'violated', 'overridden', 'not_applicable')),
    notes TEXT,
    evidence_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_journal_rule_evidence_run
    ON public.journal_rule_evidence (run_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_journal_rule_evidence_rule
    ON public.journal_rule_evidence (rule_id, created_at DESC);

CREATE TABLE IF NOT EXISTS public.benchmark_definitions (
    benchmark_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_list TEXT NOT NULL DEFAULT 'Nifty50',
    instrument_token BIGINT,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.benchmark_daily_prices (
    benchmark_id TEXT NOT NULL REFERENCES public.benchmark_definitions(benchmark_id) ON DELETE CASCADE,
    trading_day DATE NOT NULL,
    open NUMERIC(18,6),
    high NUMERIC(18,6),
    low NUMERIC(18,6),
    close NUMERIC(18,6) NOT NULL,
    daily_return NUMERIC(18,10),
    source TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (benchmark_id, trading_day)
);

CREATE INDEX IF NOT EXISTS idx_benchmark_daily_prices_day
    ON public.benchmark_daily_prices (trading_day DESC, benchmark_id);

INSERT INTO public.benchmark_definitions (benchmark_id, name, source_list, metadata_json)
VALUES ('NIFTY50', 'Nifty 50', 'Nifty50', '{}'::jsonb)
ON CONFLICT (benchmark_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS public.journal_equity_points (
    id BIGSERIAL PRIMARY KEY,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('run', 'family', 'strategy', 'portfolio')),
    subject_id TEXT NOT NULL,
    interval TEXT NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    starting_equity NUMERIC(18,6),
    ending_equity NUMERIC(18,6) NOT NULL,
    realized_pnl NUMERIC(18,6) NOT NULL DEFAULT 0,
    unrealized_pnl NUMERIC(18,6) NOT NULL DEFAULT 0,
    cash_flow NUMERIC(18,6) NOT NULL DEFAULT 0,
    fees NUMERIC(18,6) NOT NULL DEFAULT 0,
    return_pct NUMERIC(18,10),
    benchmark_return_pct NUMERIC(18,10),
    excess_return_pct NUMERIC(18,10),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_journal_equity_points_subject_slot
    ON public.journal_equity_points (subject_type, subject_id, interval, as_of);

CREATE INDEX IF NOT EXISTS idx_journal_equity_points_subject_time
    ON public.journal_equity_points (subject_type, subject_id, as_of DESC);

CREATE TABLE IF NOT EXISTS public.journal_metric_snapshots (
    id BIGSERIAL PRIMARY KEY,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    time_window TEXT NOT NULL,
    calc_version TEXT NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

ALTER TABLE public.journal_metric_snapshots
    ADD COLUMN IF NOT EXISTS environment_id UUID;

ALTER TABLE public.journal_metric_snapshots
    ADD COLUMN IF NOT EXISTS identity_rule_version TEXT NOT NULL DEFAULT 'v1_legacy';

ALTER TABLE public.journal_metric_snapshots
    ADD COLUMN IF NOT EXISTS grouping_rule_version TEXT NOT NULL DEFAULT 'v1_legacy';

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'journal_metric_snapshots'
          AND column_name = 'window'
    ) THEN
        EXECUTE 'ALTER TABLE public.journal_metric_snapshots RENAME COLUMN "window" TO time_window';
    END IF;
END $$;

DROP INDEX IF EXISTS public.ux_journal_metric_snapshots_subject_window_version;

CREATE UNIQUE INDEX IF NOT EXISTS ux_journal_metric_snapshots_legacy_subject_window_version
    ON public.journal_metric_snapshots (subject_type, subject_id, time_window, calc_version)
    WHERE environment_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_journal_metric_snapshots_v2_environment_subject_window_version
    ON public.journal_metric_snapshots (
        environment_id,
        subject_type,
        subject_id,
        time_window,
        calc_version,
        identity_rule_version,
        grouping_rule_version
    )
    WHERE environment_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_journal_metric_snapshots_lookup
    ON public.journal_metric_snapshots (subject_type, subject_id, computed_at DESC);

CREATE INDEX IF NOT EXISTS idx_journal_metric_snapshots_environment_subject_window_version
    ON public.journal_metric_snapshots (
        environment_id,
        subject_type,
        subject_id,
        time_window,
        calc_version,
        identity_rule_version,
        grouping_rule_version
    );

CREATE TABLE IF NOT EXISTS public.journal_projection_state (
    projector_name TEXT PRIMARY KEY,
    cursor_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
