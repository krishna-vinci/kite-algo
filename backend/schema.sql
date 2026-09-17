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

-- =========================================
-- Generation-aware instrument catalog
-- =========================================

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
);

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
);

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
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_instrument_mapping_current_broker_token
  ON public.instrument_broker_mappings (broker, broker_token)
  WHERE is_current;

CREATE UNIQUE INDEX IF NOT EXISTS uq_instrument_mapping_current_identity_broker
  ON public.instrument_broker_mappings (instrument_id, broker)
  WHERE is_current;

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
);

CREATE INDEX IF NOT EXISTS idx_instrument_catalog_public_key
  ON public.instrument_catalog_records (public_key);
CREATE INDEX IF NOT EXISTS idx_instrument_catalog_exchange_symbol
  ON public.instrument_catalog_records (exchange, tradingsymbol);
CREATE INDEX IF NOT EXISTS idx_instrument_catalog_derivative
  ON public.instrument_catalog_records (underlying, option_type, expiry, strike);
CREATE INDEX IF NOT EXISTS idx_instrument_catalog_current_generation
  ON public.instrument_catalog_records (current_generation_id);
CREATE INDEX IF NOT EXISTS idx_instrument_mapping_current_token
  ON public.instrument_broker_mappings (broker, broker_token)
  WHERE is_current;
CREATE INDEX IF NOT EXISTS idx_instrument_staging_generation_exchange
  ON public.instrument_catalog_staging (generation_id, source_exchange);
CREATE INDEX IF NOT EXISTS idx_instrument_staging_identity
  ON public.instrument_catalog_staging (generation_id, identity_key);

DROP VIEW IF EXISTS public.instrument_catalog_published_v;
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
 AND m.is_current = TRUE;

-- =========================================
-- Universe membership (alerts platform)
-- Mirrors migration 20260909_000014_universe_membership.
-- =========================================

CREATE TABLE IF NOT EXISTS public.universes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id VARCHAR(255) NOT NULL,
  name VARCHAR(255) NOT NULL,
  -- 'screener' is admitted since migration 20260911_000016 (Phase 4): the
  -- Phase 3 dynamic-universe code path already supported it, but the original
  -- CHECK rejected it on real PostgreSQL.
  kind VARCHAR(16) NOT NULL CHECK (kind IN ('explicit', 'index', 'portfolio', 'screener')),
  source_config JSONB NOT NULL DEFAULT '{}'::jsonb,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (owner_id, name)
);

CREATE TABLE IF NOT EXISTS public.universe_revisions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  universe_id UUID NOT NULL REFERENCES public.universes(id) ON DELETE CASCADE,
  revision INTEGER NOT NULL,
  expression JSONB NOT NULL DEFAULT '{}'::jsonb,
  members TEXT[] NOT NULL DEFAULT '{}',
  member_count INTEGER NOT NULL DEFAULT 0,
  source_generation UUID,
  coverage JSONB NOT NULL DEFAULT '{}'::jsonb,
  resolved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (universe_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_universe_revisions_universe_resolved
  ON public.universe_revisions (universe_id, resolved_at DESC);

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
  ADD COLUMN IF NOT EXISTS summary_fields_json JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE public.algo_worker_runs
  ADD COLUMN IF NOT EXISTS risk_schema_json JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE public.algo_worker_runs
  ADD COLUMN IF NOT EXISTS allowed_actions_json JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE public.algo_worker_runs
  ADD COLUMN IF NOT EXISTS runtime_state_json JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE public.algo_worker_runs
  ADD COLUMN IF NOT EXISTS metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb;

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
  event_kind TEXT NOT NULL DEFAULT 'execution',
  event_source TEXT NOT NULL DEFAULT 'legacy_execution',
  event_type TEXT NOT NULL,
  related_resource_type TEXT,
  related_resource_id TEXT,
  summary TEXT,
  payload_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.worker_execution_events
  ADD COLUMN IF NOT EXISTS event_kind TEXT NOT NULL DEFAULT 'execution';

ALTER TABLE public.worker_execution_events
  ADD COLUMN IF NOT EXISTS event_source TEXT NOT NULL DEFAULT 'legacy_execution';

ALTER TABLE public.worker_execution_events
  ADD COLUMN IF NOT EXISTS related_resource_type TEXT;

ALTER TABLE public.worker_execution_events
  ADD COLUMN IF NOT EXISTS related_resource_id TEXT;

ALTER TABLE public.worker_execution_events
  ADD COLUMN IF NOT EXISTS summary TEXT;

CREATE INDEX IF NOT EXISTS idx_basket_executions_run_status
  ON public.basket_executions (strategy_run_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_worker_execution_events_run_cursor
  ON public.worker_execution_events (strategy_run_id, cursor);

CREATE INDEX IF NOT EXISTS idx_worker_execution_events_run_kind_cursor
  ON public.worker_execution_events (strategy_run_id, event_kind, cursor);

CREATE INDEX IF NOT EXISTS idx_worker_execution_events_run_source_cursor
  ON public.worker_execution_events (strategy_run_id, event_source, cursor);

CREATE INDEX IF NOT EXISTS idx_worker_execution_events_related_ref_cursor
  ON public.worker_execution_events (strategy_run_id, related_resource_type, related_resource_id, cursor)
  WHERE related_resource_type IS NOT NULL AND related_resource_id IS NOT NULL;

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

ALTER TABLE public.live_order_intents
  ADD COLUMN IF NOT EXISTS bracket_intent_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS ux_live_order_intents_client_order_ref
  ON public.live_order_intents (client_order_ref);

CREATE INDEX IF NOT EXISTS idx_live_order_intents_broker_order
  ON public.live_order_intents (account_id, broker_order_id);

CREATE INDEX IF NOT EXISTS idx_live_order_intents_strategy
  ON public.live_order_intents (strategy_run_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_live_order_intents_account_order_basket
  ON public.live_order_intents (account_id, broker_order_id, basket_execution_id);

CREATE TABLE IF NOT EXISTS public.worker_live_execution_links (
  link_id BIGSERIAL PRIMARY KEY,
  strategy_run_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  broker_order_id TEXT NOT NULL,
  trade_id TEXT,
  client_order_ref TEXT,
  basket_execution_id TEXT,
  basket_leg_index INTEGER,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_worker_exec_links_order
  ON public.worker_live_execution_links (account_id, broker_order_id)
  WHERE trade_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_worker_exec_links_trade
  ON public.worker_live_execution_links (account_id, trade_id)
  WHERE trade_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_worker_exec_links_run
  ON public.worker_live_execution_links (strategy_run_id, account_id, created_at DESC);

CREATE TABLE IF NOT EXISTS public.bracket_intents (
  bracket_intent_id TEXT PRIMARY KEY,
  strategy_run_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  entry_basket_execution_id TEXT,
  status TEXT NOT NULL,
  action_required BOOLEAN NOT NULL DEFAULT FALSE,
  action_reason TEXT,
  config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  closed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_bracket_intents_run_status
  ON public.bracket_intents (strategy_run_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS public.bracket_actions (
  action_id TEXT PRIMARY KEY,
  bracket_intent_id TEXT NOT NULL REFERENCES public.bracket_intents(bracket_intent_id) ON DELETE CASCADE,
  strategy_run_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  action_type TEXT NOT NULL,
  status TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ,
  claimed_at TIMESTAMPTZ,
  payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  error_json JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bracket_actions_pending
  ON public.bracket_actions (status, next_attempt_at, created_at);

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

-- ============================================================================
-- Screener runs and attachments (alerts Phase 3 F9).
-- Mirrors migration 20260910_000015_screener_runs.
-- ============================================================================

-- Screener attachment events are WORKFLOW-level (no alert subscription);
-- the delivery worker renders their context from event evidence.
ALTER TABLE signal_events ALTER COLUMN subscription_id DROP NOT NULL;
ALTER TABLE signal_events ADD COLUMN IF NOT EXISTS workflow_id UUID;
CREATE INDEX IF NOT EXISTS idx_signal_events_workflow
    ON signal_events (workflow_id, fired_at DESC);

-- Run-scoped notifications (hosted strategies): additive columns; no FK change.
ALTER TABLE signal_events ADD COLUMN IF NOT EXISTS source_kind TEXT NOT NULL DEFAULT 'workflow';
ALTER TABLE signal_events ADD COLUMN IF NOT EXISTS owner_id TEXT;
ALTER TABLE signal_events ADD COLUMN IF NOT EXISTS run_id TEXT;
CREATE INDEX IF NOT EXISTS idx_signal_events_run
    ON signal_events (run_id, fired_at DESC);
CREATE INDEX IF NOT EXISTS idx_signal_events_owner
    ON signal_events (owner_id);

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
CREATE INDEX IF NOT EXISTS idx_screener_run_workflow
    ON public.screener_run (workflow_id, scheduled_for DESC);

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
CREATE INDEX IF NOT EXISTS idx_screener_member_run
    ON public.screener_run_member (run_id);

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

-- =========================================
-- Alerts Phase 4 F10 — advanced conditions, breadth, session caps and
-- external signal producers.
-- Mirrors migration 20260911_000016_alerts_phase4.
--
-- Two classes of object: COMPUTED state (breadth threshold/contributions,
-- session and suppression counters, expiring external values) which rebuilds
-- on the next evaluation, and USER-AUTHORED configuration (external producer
-- definitions, their schemas, their credentials) which does not.
-- =========================================

-- One breadth threshold row per (owner, workflow, revision, stage). Breadth
-- state is deliberately NOT kept in per-subscription checkpoints: a
-- workflow-level event cannot be governed by N per-instrument copies.
CREATE TABLE IF NOT EXISTS public.alert_breadth_state (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id TEXT NOT NULL,
  workflow_id UUID NOT NULL,
  revision_id UUID NOT NULL,
  stage_id TEXT NOT NULL,
  -- Starts FALSE so the first legitimate crossing notifies.
  satisfied BOOLEAN NOT NULL DEFAULT false,
  crossing_seq BIGINT NOT NULL DEFAULT 0,
  satisfied_since_ts TIMESTAMPTZ,
  last_fired_ts TIMESTAMPTZ,
  last_count INTEGER,
  member_count INTEGER,
  -- Never moves backwards; excludes future contributions from a count.
  aggregation_watermark TIMESTAMPTZ,
  membership_resolved_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (owner_id, workflow_id, revision_id, stage_id)
);

-- One row per contributing instrument holding its LATEST qualifying trigger,
-- written with a guarded upsert so a late observation can never overwrite a
-- newer contribution for the same instrument.
CREATE TABLE IF NOT EXISTS public.alert_breadth_triggers (
  id BIGSERIAL PRIMARY KEY,
  owner_id TEXT NOT NULL,
  workflow_id UUID NOT NULL,
  revision_id UUID NOT NULL,
  stage_id TEXT NOT NULL,
  instrument_key TEXT NOT NULL,
  last_trigger_ts TIMESTAMPTZ NOT NULL,
  last_bar_ts TIMESTAMPTZ,
  universe_revision INTEGER,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (owner_id, workflow_id, revision_id, stage_id, instrument_key)
);
CREATE INDEX IF NOT EXISTS idx_breadth_triggers_window
    ON public.alert_breadth_triggers
    (workflow_id, revision_id, stage_id, last_trigger_ts);

-- Per-session notification cap, shared atomically across every instrument of
-- one alert and counting LOGICAL notifications (one per signal event, never
-- per channel delivery).
CREATE TABLE IF NOT EXISTS public.alert_session_counters (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id TEXT NOT NULL,
  workflow_id UUID NOT NULL,
  revision_id UUID NOT NULL,
  alert_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  first_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (owner_id, workflow_id, revision_id, alert_id, session_id)
);

-- Durable record of what was suppressed, so a skipped notification is
-- inspectable rather than silent.
CREATE TABLE IF NOT EXISTS public.alert_suppression_counters (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id TEXT NOT NULL,
  workflow_id UUID NOT NULL,
  revision_id UUID NOT NULL,
  alert_id TEXT NOT NULL,
  session_id TEXT,
  reason TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  first_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_instrument_key TEXT,
  last_stage_id TEXT,
  UNIQUE (owner_id, workflow_id, revision_id, alert_id, session_id, reason)
);
CREATE INDEX IF NOT EXISTS idx_suppression_counters_workflow
    ON public.alert_suppression_counters (workflow_id, reason);

-- USER-AUTHORED configuration: producer identity, owner and value schema.
CREATE TABLE IF NOT EXISTS public.external_signal_producers (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id TEXT NOT NULL,
  name TEXT NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT true,
  value_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
  default_ttl_s INTEGER NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked_at TIMESTAMPTZ,
  UNIQUE (owner_id, name)
);

-- Hash only: the raw secret is returned exactly once at issuance and is never
-- retrievable, echoed in another response, or logged.
CREATE TABLE IF NOT EXISTS public.external_signal_producer_credentials (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  producer_id UUID NOT NULL REFERENCES public.external_signal_producers(id) ON DELETE CASCADE,
  token_id TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at TIMESTAMPTZ,
  revoked_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_producer_credentials_producer
    ON public.external_signal_producer_credentials (producer_id, status);

-- Expiring typed values. Durable acceptance precedes the 2xx response; the
-- consuming stage samples them at its own candle clock, so a value can expire
-- between evaluations (disclosed, never implied away).
CREATE TABLE IF NOT EXISTS public.external_signal_values (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  producer_id UUID NOT NULL REFERENCES public.external_signal_producers(id) ON DELETE CASCADE,
  owner_id TEXT NOT NULL,
  instrument_key TEXT,
  event_time TIMESTAMPTZ NOT NULL,
  received_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('accepted', 'late')),
  value JSONB NOT NULL,
  content_hash TEXT NOT NULL,
  idempotency_key TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (producer_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_external_values_lookup
    ON public.external_signal_values (producer_id, instrument_key, event_time DESC);
CREATE INDEX IF NOT EXISTS idx_external_values_expiry
    ON public.external_signal_values (expires_at);

-- Mirrors migration 20260912_000017_delivery_attempt_provider_id.
-- Nullable and additive: the adapters already return a provider
-- acknowledgement (Telegram message_id / ntfy X-Ntfy-Id) but it was discarded
-- at write time, so "did the provider accept this, and under which id" could
-- not be answered. Rows recorded before this column exists stay NULL rather
-- than being backfilled with an invented value.
ALTER TABLE public.delivery_attempts ADD COLUMN IF NOT EXISTS provider_id VARCHAR(128);

-- Mirrors migration 20260912_000018_workflow_canvas_layout.
-- Canvas node POSITIONS only, never node semantics: the canvas is another
-- editor of the same canonical document, so nothing here can reach the
-- canonical hash and a cosmetic move provably creates no revision.
-- node_id is a namespaced identity ('stage:'/'alert:'/'channel:') because
-- stage ids, alert ids and channel names are separate id spaces that may
-- legally collide -- keyed bare, two different nodes could share one position.
CREATE TABLE IF NOT EXISTS public.workflow_canvas_layout (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  workflow_id TEXT NOT NULL REFERENCES public.workflows(id) ON DELETE CASCADE,
  node_id TEXT NOT NULL,
  x DOUBLE PRECISION NOT NULL,
  y DOUBLE PRECISION NOT NULL,
  collapsed BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_canvas_layout_owner_workflow_node
    UNIQUE (owner_id, workflow_id, node_id)
);
CREATE INDEX IF NOT EXISTS idx_canvas_layout_workflow
    ON public.workflow_canvas_layout (owner_id, workflow_id);

-- Mirrors migration 20260915_000019_hosted_strategy_foundation.
-- Hosted-strategy store + immutable versions + (stored-only) schedules + a
-- fenced job ledger. Purely additive; schema only, no execution path.
--   * snapshots (params/capabilities/policy + effective max_duration_s /
--     progress_deadline_s) live on jobs and schedules so a queued job is never
--     reconstructed from mutable strategy defaults;
--   * job_kind (continuous/finite) is SEPARATE from execution_mode
--     (paper/dry_run); both are CHECK-constrained;
--   * run_id is TEXT to match algo_worker_runs.strategy_run_id (TEXT);
--   * lease_owner/lease_epoch/lease_until fence transitions, and
--     status='recovery_required' blocks replacement until reconciliation.
CREATE TABLE IF NOT EXISTS public.hosted_strategies (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    name TEXT NOT NULL,
    template_id TEXT NOT NULL,
    description TEXT,
    default_execution_mode TEXT NOT NULL DEFAULT 'paper',
    default_job_kind TEXT NOT NULL DEFAULT 'finite',
    default_account_scope TEXT NOT NULL,
    max_duration_s INTEGER NOT NULL,
    progress_deadline_s INTEGER NOT NULL,
    stale_exit_policy TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_hosted_strategies_owner_name UNIQUE (owner_id, name),
    CONSTRAINT uq_hosted_strategies_template UNIQUE (template_id),
    CONSTRAINT uq_hosted_strategies_id_owner UNIQUE (id, owner_id),
    CONSTRAINT ck_hosted_strategies_template_id CHECK (template_id = 'hosted:' || id),
    CONSTRAINT ck_hosted_strategies_execution_mode CHECK (default_execution_mode IN ('paper', 'dry_run')),
    CONSTRAINT ck_hosted_strategies_job_kind CHECK (default_job_kind IN ('continuous', 'finite')),
    CONSTRAINT ck_hosted_strategies_max_duration CHECK (max_duration_s > 0),
    CONSTRAINT ck_hosted_strategies_progress_deadline CHECK (progress_deadline_s > 0),
    CONSTRAINT ck_hosted_strategies_stale_policy CHECK (stale_exit_policy IN ('none', 'exit_on_worker_stale')),
    CONSTRAINT ck_hosted_strategies_status CHECK (status IN ('active', 'disabled'))
);
CREATE INDEX IF NOT EXISTS idx_hosted_strategies_owner
    ON public.hosted_strategies (owner_id);

CREATE TABLE IF NOT EXISTS public.hosted_strategy_versions (
    id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL REFERENCES public.hosted_strategies(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    source TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    parameters_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    capabilities_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_hosted_strategy_versions_number UNIQUE (strategy_id, version),
    CONSTRAINT uq_hosted_strategy_versions_id_strategy UNIQUE (id, strategy_id),
    CONSTRAINT ck_hosted_strategy_versions_number CHECK (version > 0)
);
CREATE INDEX IF NOT EXISTS idx_hosted_strategy_versions_strategy
    ON public.hosted_strategy_versions (strategy_id, version DESC);

CREATE TABLE IF NOT EXISTS public.hosted_strategy_schedules (
    id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    account_scope TEXT NOT NULL,
    params_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    execution_mode TEXT NOT NULL,
    job_kind TEXT NOT NULL,
    policy_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    capabilities_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    max_duration_s INTEGER NOT NULL,
    progress_deadline_s INTEGER NOT NULL,
    schedule_kind TEXT NOT NULL,
    at_time TEXT NOT NULL,
    weekday INTEGER,
    timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    window_end TEXT,
    squareoff_at TEXT,
    enabled BOOLEAN NOT NULL DEFAULT true,
    manual_paused_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_hosted_strategy_schedules_strategy UNIQUE (strategy_id),
    CONSTRAINT fk_hosted_strategy_schedules_strategy_owner
        FOREIGN KEY (strategy_id, owner_id)
        REFERENCES public.hosted_strategies (id, owner_id) ON DELETE CASCADE,
    CONSTRAINT fk_hosted_strategy_schedules_version_strategy
        FOREIGN KEY (version_id, strategy_id)
        REFERENCES public.hosted_strategy_versions (id, strategy_id) ON DELETE RESTRICT,
    CONSTRAINT ck_hosted_strategy_schedules_execution_mode CHECK (execution_mode IN ('paper', 'dry_run')),
    CONSTRAINT ck_hosted_strategy_schedules_job_kind CHECK (job_kind IN ('continuous', 'finite')),
    CONSTRAINT ck_hosted_strategy_schedules_kind CHECK (schedule_kind IN ('daily', 'weekly')),
    CONSTRAINT ck_hosted_strategy_schedules_weekday CHECK (weekday IS NULL OR (weekday >= 0 AND weekday <= 6)),
    CONSTRAINT ck_hosted_strategy_schedules_weekly_weekday CHECK (schedule_kind <> 'weekly' OR weekday IS NOT NULL),
    CONSTRAINT ck_hosted_strategy_schedules_max_duration CHECK (max_duration_s > 0),
    CONSTRAINT ck_hosted_strategy_schedules_progress_deadline CHECK (progress_deadline_s > 0)
);

CREATE TABLE IF NOT EXISTS public.strategy_jobs (
    id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    account_scope TEXT NOT NULL,
    job_kind TEXT NOT NULL,
    execution_mode TEXT NOT NULL,
    desired_state TEXT NOT NULL DEFAULT 'started',
    occurrence_key TEXT,
    run_id TEXT,
    token_id TEXT,
    lease_owner TEXT,
    lease_epoch BIGINT NOT NULL DEFAULT 0,
    lease_until TIMESTAMPTZ,
    attempt INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'queued',
    params_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    capabilities_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    policy_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    max_duration_s INTEGER NOT NULL,
    progress_deadline_s INTEGER NOT NULL,
    identity_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_progress_at TIMESTAMPTZ,
    exit_code INTEGER,
    log_ref TEXT,
    handoff_at TIMESTAMPTZ,
    last_error TEXT,
    process_cleanup_state TEXT,
    process_cleanup_at TIMESTAMPTZ,
    process_cleanup_actor TEXT,
    stop_requested_at TIMESTAMPTZ,
    stop_requested_by TEXT,
    logs_discarded BOOLEAN NOT NULL DEFAULT false,
    logs_source TEXT,
    recovery_required_at TIMESTAMPTZ,
    reconciled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_strategy_jobs_occurrence UNIQUE (occurrence_key),
    CONSTRAINT fk_strategy_jobs_strategy_owner
        FOREIGN KEY (strategy_id, owner_id)
        REFERENCES public.hosted_strategies (id, owner_id) ON DELETE CASCADE,
    CONSTRAINT fk_strategy_jobs_version_strategy
        FOREIGN KEY (version_id, strategy_id)
        REFERENCES public.hosted_strategy_versions (id, strategy_id) ON DELETE RESTRICT,
    CONSTRAINT ck_strategy_jobs_job_kind CHECK (job_kind IN ('continuous', 'finite')),
    CONSTRAINT ck_strategy_jobs_execution_mode CHECK (execution_mode IN ('paper', 'dry_run')),
    CONSTRAINT ck_strategy_jobs_desired_state CHECK (desired_state IN ('started', 'paused', 'stopped')),
    CONSTRAINT ck_strategy_jobs_attempt CHECK (attempt > 0),
    CONSTRAINT ck_strategy_jobs_lease_epoch CHECK (lease_epoch >= 0),
    CONSTRAINT ck_strategy_jobs_max_duration CHECK (max_duration_s > 0),
    CONSTRAINT ck_strategy_jobs_progress_deadline CHECK (progress_deadline_s > 0),
    CONSTRAINT ck_strategy_jobs_status CHECK (
        status IN ('queued', 'starting', 'running', 'fencing', 'recovery_required', 'stopped', 'failed', 'hung')
    ),
    CONSTRAINT ck_strategy_jobs_process_cleanup_state CHECK (
        process_cleanup_state IS NULL OR process_cleanup_state IN ('confirmed', 'unresolved')
    )
);
CREATE INDEX IF NOT EXISTS idx_strategy_jobs_lease
    ON public.strategy_jobs (status, lease_until);
CREATE INDEX IF NOT EXISTS idx_strategy_jobs_owner_strategy
    ON public.strategy_jobs (owner_id, strategy_id);

CREATE TABLE IF NOT EXISTS public.strategy_job_reconciliations (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES public.strategy_jobs(id) ON DELETE CASCADE,
    strategy_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    run_id TEXT,
    outcome TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    evidence_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    actor_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_strategy_job_reconciliations_outcome CHECK (outcome IN ('reconciled', 'blocked')),
    CONSTRAINT ck_strategy_job_reconciliations_attempt CHECK (attempt > 0)
);
CREATE INDEX IF NOT EXISTS idx_strategy_job_reconciliations_job
    ON public.strategy_job_reconciliations (job_id, created_at DESC);

CREATE TABLE IF NOT EXISTS public.strategy_job_logs (
    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES public.strategy_jobs(id) ON DELETE CASCADE,
    attempt INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    content TEXT NOT NULL,
    byte_len INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_strategy_job_logs_seq UNIQUE (job_id, attempt, seq)
);
CREATE INDEX IF NOT EXISTS idx_strategy_job_logs_job
    ON public.strategy_job_logs (job_id, attempt, seq);

-- =========================================
-- Durable strategy attribution (G1)
-- =========================================
-- Canonical strategy identity plus two compute adapters (hosted, external).
-- Mirrors alembic revision 20260917_000025. Bindings are trigger-immutable and
-- ON DELETE RESTRICT in both directions, so attribution history outlives
-- operational run rows and deleting a strategy with history is refused.

CREATE TABLE IF NOT EXISTS public.strategies (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    name TEXT NOT NULL,
    account_scope TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    journal_template_id UUID REFERENCES public.journal_strategy_templates(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_strategies_owner_name UNIQUE (owner_id, name),
    -- Composite targets for database-enforced integrity: bindings reference
    -- (id, owner_id, account_scope); projection and state reference
    -- (id, account_scope) for account agreement.
    CONSTRAINT uq_strategies_id_owner_account UNIQUE (id, owner_id, account_scope),
    CONSTRAINT uq_strategies_id_account UNIQUE (id, account_scope),
    CONSTRAINT ck_strategies_status CHECK (status IN ('active', 'disabled', 'archived'))
);

-- Backfill: canonical row per hosted strategy, SAME id (IDs preserved).
INSERT INTO public.strategies (id, owner_id, name, account_scope, status)
SELECT hs.id, hs.owner_id, hs.name, hs.default_account_scope,
       CASE hs.status WHEN 'disabled' THEN 'disabled' ELSE 'active' END
FROM public.hosted_strategies hs
ON CONFLICT (id) DO NOTHING;

-- Hosted adapter now references the canonical strategy INCLUDING owner and
-- account, so hosted identity cannot drift from canonical identity.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_hosted_strategies_canonical'
    ) THEN
        ALTER TABLE public.hosted_strategies
            ADD CONSTRAINT fk_hosted_strategies_canonical
            FOREIGN KEY (id, owner_id, default_account_scope)
            REFERENCES public.strategies (id, owner_id, account_scope)
            ON DELETE RESTRICT;
    END IF;
END $$;

-- Composite FK target for binding environment integrity: a binding's
-- execution_environment must equal the run's persisted execution_mode.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_algo_worker_runs_id_mode'
    ) THEN
        ALTER TABLE public.algo_worker_runs
            ADD CONSTRAINT uq_algo_worker_runs_id_mode
            UNIQUE (strategy_run_id, execution_mode);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS public.external_strategy_adapters (
    id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL REFERENCES public.strategies(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'active',
    config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_external_adapters_status CHECK (status IN ('active', 'disabled'))
);
CREATE INDEX IF NOT EXISTS idx_external_adapters_strategy
    ON public.external_strategy_adapters (strategy_id);

CREATE TABLE IF NOT EXISTS public.worker_token_strategy_grants (
    token_id TEXT NOT NULL REFERENCES public.algo_worker_tokens(token_id) ON DELETE CASCADE,
    strategy_id TEXT NOT NULL REFERENCES public.strategies(id) ON DELETE RESTRICT,
    granted_by TEXT NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    PRIMARY KEY (token_id, strategy_id)
);
CREATE INDEX IF NOT EXISTS idx_grants_strategy
    ON public.worker_token_strategy_grants (strategy_id);

CREATE TABLE IF NOT EXISTS public.strategy_run_bindings (
    strategy_run_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    execution_environment TEXT NOT NULL,
    bound_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    bound_by TEXT NOT NULL,
    binding_source TEXT NOT NULL,
    CONSTRAINT ck_binding_source CHECK (
        binding_source IN ('hosted_job', 'external_run_create', 'audited_mapping', 'legacy_compat')
    ),
    CONSTRAINT ck_binding_environment CHECK (execution_environment IN ('live', 'paper', 'dry_run')),
    -- Owner/account integrity: the binding's owner and account must equal the
    -- canonical strategy's (composite FK — mismatch is impossible).
    CONSTRAINT fk_strategy_run_bindings_canonical
        FOREIGN KEY (strategy_id, owner_id, account_id)
        REFERENCES public.strategies (id, owner_id, account_scope) ON DELETE RESTRICT,
    -- Environment integrity: the binding's environment must equal the run's
    -- persisted execution_mode (composite FK).
    CONSTRAINT fk_strategy_run_bindings_run_mode
        FOREIGN KEY (strategy_run_id, execution_environment)
        REFERENCES public.algo_worker_runs (strategy_run_id, execution_mode) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_strategy_run_bindings_strategy
    ON public.strategy_run_bindings (strategy_id);
CREATE INDEX IF NOT EXISTS idx_strategy_run_bindings_account_env
    ON public.strategy_run_bindings (account_id, strategy_id, execution_environment);

-- Database-enforced immutability: INSERT-only. Audited legacy mapping adds NEW
-- rows; a mistaken binding requires the dedicated ownership-transfer protocol
-- (outside G1) which will itself append, never mutate.
CREATE OR REPLACE FUNCTION forbid_strategy_run_binding_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'strategy_run_bindings are immutable (insert-only)';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_strategy_run_bindings_immutable ON public.strategy_run_bindings;
CREATE TRIGGER trg_strategy_run_bindings_immutable
    BEFORE UPDATE OR DELETE ON public.strategy_run_bindings
    FOR EACH ROW EXECUTE FUNCTION forbid_strategy_run_binding_mutation();

CREATE TABLE IF NOT EXISTS public.strategy_position_projection (
    account_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    execution_environment TEXT NOT NULL,
    identity_kind TEXT NOT NULL,
    identity_key TEXT NOT NULL,
    product TEXT NOT NULL,
    canonical_instrument_id UUID,
    instrument_token BIGINT NOT NULL,
    exchange TEXT NOT NULL,
    tradingsymbol TEXT NOT NULL,
    net_quantity INTEGER NOT NULL,
    unresolved_reason TEXT,
    projection_version BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, strategy_id, execution_environment, identity_kind, identity_key, product),
    CONSTRAINT ck_spp_environment CHECK (execution_environment IN ('live', 'paper', 'dry_run')),
    CONSTRAINT ck_spp_identity_kind CHECK (identity_kind IN ('canonical', 'raw')),
    CONSTRAINT ck_spp_identity_consistency CHECK (
        (identity_kind = 'canonical' AND canonical_instrument_id IS NOT NULL)
        OR (identity_kind = 'raw' AND canonical_instrument_id IS NULL)
    ),
    -- Account agreement is database-enforced: a projection row's account must
    -- equal the canonical strategy's account.
    CONSTRAINT fk_spp_strategy_account
        FOREIGN KEY (strategy_id, account_id)
        REFERENCES public.strategies (id, account_scope) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_spp_strategy
    ON public.strategy_position_projection (account_id, strategy_id, execution_environment);

CREATE TABLE IF NOT EXISTS public.strategy_projection_state (
    account_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    execution_environment TEXT NOT NULL,
    projection_version BIGINT NOT NULL DEFAULT 0,
    content_sha256 TEXT,
    last_rebuild_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, strategy_id, execution_environment),
    CONSTRAINT ck_sps_environment CHECK (execution_environment IN ('live', 'paper', 'dry_run')),
    -- Account agreement is database-enforced, not merely a strategy_id FK.
    CONSTRAINT fk_sps_strategy_account
        FOREIGN KEY (strategy_id, account_id)
        REFERENCES public.strategies (id, account_scope) ON DELETE RESTRICT
);

-- =========================================
-- Strategy account truth (G2+G3+G4)
-- =========================================
-- Mirrors alembic revision 20260917_000026. Purely additive: nothing above is
-- altered. Ingested facts and adjustments are insert-only; reconciliation state
-- persists the per-coordinate classification so the freeze and the UI read
-- truth rather than recomputing it inline.

CREATE TABLE IF NOT EXISTS public.broker_trade_facts (
    fact_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id TEXT NOT NULL,
    trade_id TEXT NOT NULL,
    broker_order_id TEXT NOT NULL,
    instrument_token BIGINT NOT NULL,
    exchange TEXT NOT NULL,
    tradingsymbol TEXT NOT NULL,
    product TEXT NOT NULL,
    transaction_type TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    fill_price DOUBLE PRECISION,
    trade_timestamp TIMESTAMPTZ,
    ingest_generation BIGINT NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_broker_trade_facts_account_trade UNIQUE (account_id, trade_id),
    CONSTRAINT ck_btf_transaction_type CHECK (transaction_type IN ('BUY', 'SELL')),
    CONSTRAINT ck_btf_quantity CHECK (quantity > 0)
);
CREATE INDEX IF NOT EXISTS idx_btf_account_coord ON public.broker_trade_facts
    (account_id, instrument_token, exchange, tradingsymbol, product);

CREATE TABLE IF NOT EXISTS public.account_ingest_state (
    account_id TEXT PRIMARY KEY,
    last_orders_fetch_at TIMESTAMPTZ,
    last_complete_ingest_at TIMESTAMPTZ,
    ingest_generation BIGINT NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'idle',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_ais_status CHECK (status IN ('idle', 'refreshing', 'stale'))
);

CREATE TABLE IF NOT EXISTS public.strategy_reconciliation_state (
    account_id TEXT NOT NULL,
    instrument_token BIGINT NOT NULL,
    exchange TEXT NOT NULL,
    tradingsymbol TEXT NOT NULL,
    product TEXT NOT NULL,
    divergence_class TEXT NOT NULL,
    broker_quantity BIGINT NOT NULL,
    attributed_quantity BIGINT NOT NULL,
    manual_quantity BIGINT NOT NULL,
    residual_quantity BIGINT NOT NULL,
    refresh_attempts INTEGER NOT NULL DEFAULT 0,
    last_checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    owner_notified_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ,
    PRIMARY KEY (account_id, instrument_token, exchange, tradingsymbol, product),
    CONSTRAINT ck_srs_divergence_class CHECK (
        divergence_class IN ('aligned', 'pending_ingest', 'unexplained')
    )
);

CREATE TABLE IF NOT EXISTS public.strategy_attribution_adjustments (
    adjustment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id TEXT NOT NULL,
    adjustment_kind TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    created_by TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_saa_adjustment_kind CHECK (adjustment_kind IN ('owner_reclassification'))
);
CREATE INDEX IF NOT EXISTS idx_saa_account
    ON public.strategy_attribution_adjustments (account_id, created_at);

CREATE TABLE IF NOT EXISTS public.strategy_attribution_adjustment_lines (
    adjustment_id UUID NOT NULL
        REFERENCES public.strategy_attribution_adjustments(adjustment_id) ON DELETE RESTRICT,
    line_no INTEGER NOT NULL,
    strategy_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    instrument_token BIGINT NOT NULL,
    exchange TEXT NOT NULL,
    tradingsymbol TEXT NOT NULL,
    product TEXT NOT NULL,
    quantity_delta INTEGER NOT NULL,
    effective_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (adjustment_id, line_no),
    CONSTRAINT ck_saal_quantity_delta CHECK (quantity_delta <> 0),
    -- Owner/account integrity mirrors strategy_run_bindings exactly.
    CONSTRAINT fk_saal_strategy_canonical
        FOREIGN KEY (strategy_id, owner_id, account_id)
        REFERENCES public.strategies (id, owner_id, account_scope) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_saal_strategy_coord
    ON public.strategy_attribution_adjustment_lines
    (strategy_id, instrument_token, exchange, tradingsymbol, product);

-- Insert-only enforcement, same pattern as strategy_run_bindings.
CREATE OR REPLACE FUNCTION forbid_broker_trade_fact_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'broker_trade_facts are immutable (insert-only)';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_broker_trade_facts_immutable ON public.broker_trade_facts;
CREATE TRIGGER trg_broker_trade_facts_immutable
    BEFORE UPDATE OR DELETE ON public.broker_trade_facts
    FOR EACH ROW EXECUTE FUNCTION forbid_broker_trade_fact_mutation();

CREATE OR REPLACE FUNCTION forbid_strategy_attribution_adjustment_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'strategy_attribution_adjustments are immutable (insert-only)';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_strategy_attribution_adjustments_immutable
    ON public.strategy_attribution_adjustments;
CREATE TRIGGER trg_strategy_attribution_adjustments_immutable
    BEFORE UPDATE OR DELETE ON public.strategy_attribution_adjustments
    FOR EACH ROW EXECUTE FUNCTION forbid_strategy_attribution_adjustment_mutation();

DROP TRIGGER IF EXISTS trg_strategy_attribution_adjustment_lines_immutable
    ON public.strategy_attribution_adjustment_lines;
CREATE TRIGGER trg_strategy_attribution_adjustment_lines_immutable
    BEFORE UPDATE OR DELETE ON public.strategy_attribution_adjustment_lines
    FOR EACH ROW EXECUTE FUNCTION forbid_strategy_attribution_adjustment_mutation();
