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
