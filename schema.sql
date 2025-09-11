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