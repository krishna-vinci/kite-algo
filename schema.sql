-- This script is designed to be idempotent.
-- It will migrate the schema from using a serial 'id' to using 'instrument_token' as the primary key.

-- First, ensure the table exists, creating it with the old schema if it's the very first run.
CREATE TABLE IF NOT EXISTS kite_ticker_tickers (
    id SERIAL PRIMARY KEY,
    instrument_token BIGINT NOT NULL,
    tradingsymbol VARCHAR(50) NOT NULL,
    company_name VARCHAR(255) NOT NULL,
    sector VARCHAR(100) NOT NULL,
    added_date DATE NOT NULL DEFAULT CURRENT_DATE,
    last_updated TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    source_list VARCHAR(255) NOT NULL
);

-- Run the migration logic only if the 'id' column exists, which indicates the old schema.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'kite_ticker_tickers' AND column_name = 'id'
    ) THEN
        -- Step 1: Remove duplicate instrument_token entries, keeping the one with the smallest id.
        DELETE FROM
            kite_ticker_tickers a
                USING kite_ticker_tickers b
        WHERE
            a.id > b.id
            AND a.instrument_token = b.instrument_token;

        -- Step 2: Alter the table to enforce uniqueness on instrument_token.
        ALTER TABLE kite_ticker_tickers DROP CONSTRAINT kite_ticker_tickers_pkey;
        ALTER TABLE kite_ticker_tickers ADD PRIMARY KEY (instrument_token);
        ALTER TABLE kite_ticker_tickers DROP COLUMN id;
    END IF;
END $$;

-- Create index for kite_ticker_tickers
CREATE INDEX IF NOT EXISTS idx_tradingsymbol ON kite_ticker_tickers (tradingsymbol);


-- Now, create the historical data table if it doesn't exist. This is non-destructive.
CREATE TABLE IF NOT EXISTS kite_historical_data (
    instrument_token BIGINT NOT NULL,
    tradingsymbol VARCHAR(50) NOT NULL,
    "timestamp" TIMESTAMP WITH TIME ZONE NOT NULL,
    interval VARCHAR(10) NOT NULL,
    open DOUBLE PRECISION NOT NULL,
    high DOUBLE PRECISION NOT NULL,
    low DOUBLE PRECISION NOT NULL,
    close DOUBLE PRECISION NOT NULL,
    volume BIGINT NOT NULL,
    oi BIGINT,
    PRIMARY KEY (instrument_token, "timestamp", interval),
    FOREIGN KEY (instrument_token) REFERENCES kite_ticker_tickers (instrument_token)
);

-- Create index for historical data
CREATE INDEX IF NOT EXISTS idx_timestamp ON kite_historical_data ("timestamp");
-- Table for Kite sessions
CREATE TABLE IF NOT EXISTS kite_sessions (
    session_id VARCHAR(36) PRIMARY KEY,
    access_token VARCHAR NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);

-- Table for Indices
CREATE TABLE IF NOT EXISTS indices (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(100) UNIQUE NOT NULL,
    name VARCHAR(255),
    exchange VARCHAR(10)
);

-- Table for Historical Index Data
CREATE TABLE IF NOT EXISTS indices_historical_data (
    index_id INTEGER NOT NULL,
    date DATE NOT NULL,
    open NUMERIC(15, 2),
    high NUMERIC(15, 2),
    low NUMERIC(15, 2),
    close NUMERIC(15, 2),
    volume BIGINT,
    PRIMARY KEY (index_id, date),
    FOREIGN KEY (index_id) REFERENCES indices(id) ON DELETE CASCADE
);
-- Table for Kite Indices
CREATE TABLE IF NOT EXISTS kite_indices (
    instrument_token BIGINT PRIMARY KEY,
    exchange_token BIGINT,
    tradingsymbol VARCHAR(255),
    name VARCHAR(255),
    last_price DOUBLE PRECISION,
    expiry DATE,
    strike DOUBLE PRECISION,
    tick_size DOUBLE PRECISION,
    lot_size INTEGER,
    instrument_type VARCHAR(255),
    segment VARCHAR(255),
    exchange VARCHAR(255),
    last_updated TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_kite_indices_tradingsymbol ON kite_indices (tradingsymbol);

-- Table for Historical Index Data
CREATE TABLE IF NOT EXISTS kite_indices_historical_data (
    instrument_token BIGINT NOT NULL,
    tradingsymbol VARCHAR(50) NOT NULL,
    "timestamp" TIMESTAMP WITH TIME ZONE NOT NULL,
    interval VARCHAR(10) NOT NULL,
    open DOUBLE PRECISION NOT NULL,
    high DOUBLE PRECISION NOT NULL,
    low DOUBLE PRECISION NOT NULL,
    close DOUBLE PRECISION NOT NULL,
    volume BIGINT NOT NULL,
    oi BIGINT,
    PRIMARY KEY (instrument_token, "timestamp", interval),
    FOREIGN KEY (instrument_token) REFERENCES kite_indices (instrument_token) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_indices_historical_data_timestamp ON kite_indices_historical_data ("timestamp");

-- Migration: ensure missing columns exist in kite_indices_historical_data for legacy databases
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'kite_indices_historical_data'
          AND column_name = 'tradingsymbol'
    ) THEN
        ALTER TABLE kite_indices_historical_data
            ADD COLUMN tradingsymbol VARCHAR(50);
    END IF;
END $$;


-- Table for Alerts
CREATE TABLE IF NOT EXISTS alerts (
    uuid UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id VARCHAR(50) NOT NULL,
    name VARCHAR(255) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'enabled',
    alert_type VARCHAR(50) NOT NULL,
    lhs_exchange VARCHAR(50),
    lhs_tradingsymbol VARCHAR(50),
    lhs_attribute VARCHAR(50) NOT NULL,
    operator VARCHAR(10) NOT NULL,
    rhs_type VARCHAR(50) NOT NULL,
    rhs_constant DOUBLE PRECISION,
    rhs_exchange VARCHAR(50),
    rhs_tradingsymbol VARCHAR(50),
    rhs_attribute VARCHAR(50),
    basket JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    triggered_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_alerts_user_id ON alerts (user_id);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts (status);
CREATE INDEX IF NOT EXISTS idx_alerts_lhs_tradingsymbol ON alerts (lhs_tradingsymbol);

-- Table for Alert History
CREATE TABLE IF NOT EXISTS alert_history (
    id SERIAL PRIMARY KEY,
    alert_uuid UUID NOT NULL,
    triggered_at TIMESTAMP WITH TIME ZONE NOT NULL,
    trigger_price DOUBLE PRECISION NOT NULL,
    meta JSONB,
    FOREIGN KEY (alert_uuid) REFERENCES alerts (uuid) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_alert_history_alert_uuid ON alert_history (alert_uuid);

-- ─────────────────────────────────────────────────────────────────────────────
-- Alerts table extensions (idempotent)
-- Adds columns needed for mirroring Kite alerts state, scheduling and cooldowns
-- ─────────────────────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'alerts' AND column_name = 'alert_count'
    ) THEN
        ALTER TABLE alerts ADD COLUMN alert_count INTEGER NOT NULL DEFAULT 0;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'alerts' AND column_name = 'last_alert_count'
    ) THEN
        ALTER TABLE alerts ADD COLUMN last_alert_count INTEGER NOT NULL DEFAULT 0;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'alerts' AND column_name = 'last_notified_at'
    ) THEN
        ALTER TABLE alerts ADD COLUMN last_notified_at TIMESTAMPTZ NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'alerts' AND column_name = 'cooldown_sec'
    ) THEN
        ALTER TABLE alerts ADD COLUMN cooldown_sec INTEGER NOT NULL DEFAULT 120;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'alerts' AND column_name = 'schedule'
    ) THEN
        ALTER TABLE alerts ADD COLUMN schedule JSONB NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'alerts' AND column_name = 'tags'
    ) THEN
        ALTER TABLE alerts ADD COLUMN tags JSONB NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'alerts' AND column_name = 'source'
    ) THEN
        ALTER TABLE alerts ADD COLUMN source VARCHAR(20) NOT NULL DEFAULT 'kite';
    END IF;
END $$;

-- Helpful index
CREATE INDEX IF NOT EXISTS idx_alerts_last_notified_at ON alerts (last_notified_at);
