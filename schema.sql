-- Schema for kite-app. This file is authoritative for a fresh deployment.
-- Assumes a clean DB (you will drop tables/volume before build).
-- Uses IF NOT EXISTS and CREATE OR REPLACE for idempotence on repeated runs.

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

CREATE TABLE IF NOT EXISTS public.fyers_sessions (
  session_id         VARCHAR(36) PRIMARY KEY,
  access_token       TEXT NOT NULL,
  created_at         TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

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
-- Alerts schema (user-defined alerts)
-- =========================================

-- Enable UUID generation extensions if available (safe and idempotent)
-- If pgcrypto is unavailable, uuid-ossp provides uuid_generate_v4().
-- Note: requires sufficient DB privileges; statements are no-ops if not permitted.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Alerts: core table
CREATE TABLE IF NOT EXISTS public.alerts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  instrument_token BIGINT NOT NULL,
  comparator TEXT NOT NULL CHECK (comparator IN ('gt', 'lt')),
  target_type  TEXT NOT NULL CHECK (target_type IN ('absolute', 'percent')),
  absolute_target NUMERIC(18,6),
  percent         NUMERIC(9,6),
  baseline_price  NUMERIC(18,6),
  one_time BOOLEAN NOT NULL DEFAULT TRUE,
  name  TEXT,
  notes TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('draft', 'active', 'paused', 'canceled', 'triggered')),
  instrument_exchange     TEXT,
  instrument_tradingsymbol TEXT,
  ltp_source_hint TEXT,
  last_evaluated_price NUMERIC(18,6),
  triggered_at TIMESTAMP WITH TIME ZONE,
  created_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  updated_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  -- Consistency: absolute vs percent target semantics
  CONSTRAINT alerts_abs_or_pct_chk CHECK (
    (target_type = 'absolute' AND absolute_target IS NOT NULL AND percent IS NULL)
    OR
    (target_type = 'percent' AND percent IS NOT NULL)
  )
);

-- Migration-safe additions for legacy alerts table (idempotent)
-- Ensure required columns exist before index creation.
ALTER TABLE public.alerts ADD COLUMN IF NOT EXISTS instrument_token BIGINT;
ALTER TABLE public.alerts ADD COLUMN IF NOT EXISTS comparator TEXT;
ALTER TABLE public.alerts ADD COLUMN IF NOT EXISTS target_type TEXT;
ALTER TABLE public.alerts ADD COLUMN IF NOT EXISTS absolute_target NUMERIC(18,6);
ALTER TABLE public.alerts ADD COLUMN IF NOT EXISTS percent NUMERIC(9,6);
ALTER TABLE public.alerts ADD COLUMN IF NOT EXISTS baseline_price NUMERIC(18,6);
ALTER TABLE public.alerts ADD COLUMN IF NOT EXISTS one_time BOOLEAN DEFAULT TRUE;
ALTER TABLE public.alerts ADD COLUMN IF NOT EXISTS name TEXT;
ALTER TABLE public.alerts ADD COLUMN IF NOT EXISTS notes TEXT;
ALTER TABLE public.alerts ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'active';
ALTER TABLE public.alerts ADD COLUMN IF NOT EXISTS instrument_exchange TEXT;
ALTER TABLE public.alerts ADD COLUMN IF NOT EXISTS instrument_tradingsymbol TEXT;
ALTER TABLE public.alerts ADD COLUMN IF NOT EXISTS ltp_source_hint TEXT;
ALTER TABLE public.alerts ADD COLUMN IF NOT EXISTS last_evaluated_price NUMERIC(18,6);
ALTER TABLE public.alerts ADD COLUMN IF NOT EXISTS triggered_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE public.alerts ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW();
ALTER TABLE public.alerts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW();

-- Compatibility shim: legacy code expects alerts.uuid; mirror of id (UUID)
ALTER TABLE public.alerts ADD COLUMN IF NOT EXISTS uuid UUID GENERATED ALWAYS AS (id) STORED;
-- Unique index to support ON CONFLICT (uuid)
CREATE UNIQUE INDEX IF NOT EXISTS ux_alerts_uuid ON public.alerts (uuid);

-- Indexes for scans and evaluation
CREATE INDEX IF NOT EXISTS idx_alerts_status ON public.alerts (status);
-- Composite indexes (both orders to satisfy different access paths)
CREATE INDEX IF NOT EXISTS idx_alerts_token_status ON public.alerts (instrument_token, status);
CREATE INDEX IF NOT EXISTS idx_alerts_status_token ON public.alerts (status, instrument_token);
-- Active-only partial index useful for quick active-set selections
CREATE INDEX IF NOT EXISTS idx_alerts_active_partial ON public.alerts (id) WHERE status = 'active';

-- Alert events table
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
);

-- Useful indexes for event queries
CREATE INDEX IF NOT EXISTS idx_alert_events_alert_id ON public.alert_events (alert_id);
CREATE INDEX IF NOT EXISTS idx_alert_events_created_at ON public.alert_events (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alert_events_alert_id_created_at ON public.alert_events (alert_id, created_at);

-- Enforce one-time trigger semantics: only one 'triggered' event per alert
CREATE UNIQUE INDEX IF NOT EXISTS ux_alert_events_triggered_once
  ON public.alert_events (alert_id)
  WHERE event_type = 'triggered';

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
  source_list        VARCHAR(255) NOT NULL,
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
  last_updated       TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  PRIMARY KEY (instrument_token, source_list)
);

ALTER TABLE public.kite_ticker_tickers ADD COLUMN IF NOT EXISTS change_1d NUMERIC(10, 4);


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
