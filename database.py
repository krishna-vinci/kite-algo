# database.py

import os
import psycopg2
from dotenv import load_dotenv

from sqlalchemy import create_engine, MetaData, Column, String, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from databases import Database  # if you still use it elsewhere
from datetime import datetime
import logging

# Configure logging for database operations
logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s')

# Load environment variables from .env file
load_dotenv()

# the Base for all ORM models
Base = declarative_base()
metadata = MetaData()
_SCHEMA_APPLIED: bool = False

# --- ORM model for Fyers sessions (if you want it here)
class FyersSession(Base):
    __tablename__ = "fyers_sessions"
    session_id   = Column(String(36), primary_key=True, index=True)
    access_token = Column(String, nullable=False)
    created_at   = Column(DateTime, default=datetime.utcnow, nullable=False)

def create_tables_if_not_exists(conn):
    """Executes the schema.sql to create tables if they don't exist."""
    try:
        with open("schema.sql", "r") as f:
            schema_sql = f.read()
        with conn.cursor() as cur:
            cur.execute(schema_sql)
        conn.commit()
        logging.info("Database schema checked and tables created/updated if necessary.")
    except FileNotFoundError:
        logging.error("schema.sql not found. Cannot create tables.")
    except Exception as e:
        logging.error(f"Error creating tables: {e}")
        conn.rollback() # Rollback in case of error
        raise
    
    # Create Position Protection System tables if they don't exist
    _create_position_protection_tables(conn)

def _create_position_protection_tables(conn):
    """Creates Position Protection System tables if they don't exist."""
    try:
        with conn.cursor() as cur:
            # Table: position_protection_strategies
            cur.execute("""
                CREATE TABLE IF NOT EXISTS position_protection_strategies (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    name TEXT,
                    strategy_type TEXT CHECK (strategy_type IN ('manual', 'straddle', 'strangle', 'iron_condor', 'single_leg')),
                    notes TEXT,
                    monitoring_mode TEXT NOT NULL CHECK (monitoring_mode IN ('index', 'premium', 'hybrid', 'combined_premium')),
                    exit_logic TEXT DEFAULT 'any' CHECK (exit_logic IN ('any', 'all')),
                    index_instrument_token BIGINT,
                    index_tradingsymbol TEXT,
                    index_exchange TEXT DEFAULT 'NSE',
                    index_upper_stoploss NUMERIC(18,6),
                    index_lower_stoploss NUMERIC(18,6),
                    stoploss_order_type TEXT DEFAULT 'MARKET' CHECK (stoploss_order_type IN ('MARKET', 'LIMIT', 'SL-M')),
                    stoploss_limit_offset NUMERIC(18,6),
                    trailing_mode TEXT CHECK (trailing_mode IN ('continuous', 'step', 'atr', 'none')),
                    trailing_distance NUMERIC(18,6),
                    trailing_unit TEXT CHECK (trailing_unit IN ('points', 'percent')) DEFAULT 'points',
                    trailing_step_size NUMERIC(18,6),
                    trailing_atr_multiplier NUMERIC(5,2),
                    trailing_atr_period INT DEFAULT 14,
                    trailing_lock_profit NUMERIC(18,6),
                    trailing_highest_price NUMERIC(18,6),
                    trailing_current_level NUMERIC(18,6),
                    trailing_activated BOOLEAN DEFAULT FALSE,
                    premium_thresholds JSONB DEFAULT '{}'::JSONB,
                    combined_premium_entry_type TEXT CHECK (combined_premium_entry_type IN ('credit', 'debit')),
                    combined_premium_profit_target NUMERIC(18,6),
                    combined_premium_trailing_enabled BOOLEAN DEFAULT FALSE,
                    combined_premium_trailing_distance NUMERIC(18,6),
                    combined_premium_trailing_lock_profit NUMERIC(18,6),
                    initial_net_premium NUMERIC(18,6),
                    current_net_premium NUMERIC(18,6),
                    best_net_premium NUMERIC(18,6),
                    combined_premium_trailing_sl NUMERIC(18,6),
                    combined_premium_levels JSONB DEFAULT '[]'::JSONB,
                    position_snapshot JSONB NOT NULL,
                    exit_levels JSONB DEFAULT '[]'::JSONB,
                    takeprofit_levels JSONB DEFAULT '[]'::JSONB,
                    target_delta NUMERIC(5,4),
                    risk_amount NUMERIC(18,2),
                    remaining_quantities JSONB DEFAULT '{}'::JSONB,
                    placed_orders JSONB DEFAULT '[]'::JSONB,
                    execution_errors JSONB DEFAULT '[]'::JSONB,
                    levels_executed JSONB DEFAULT '[]'::JSONB,
                    stoploss_executed BOOLEAN DEFAULT FALSE,
                    product_conversion_enabled BOOLEAN DEFAULT FALSE,
                    convert_to_nrml_at TEXT CHECK (convert_to_nrml_at IN ('never', 'tp1', 'tp2', 'manual')),
                    nrml_conversion_done BOOLEAN DEFAULT FALSE,
                    virtual_contract_calculated BOOLEAN DEFAULT FALSE,
                    virtual_contract_data JSONB,
                    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'completed', 'triggered', 'error', 'partial')),
                    last_evaluated_price NUMERIC(18,6),
                    last_evaluated_at TIMESTAMP WITH TIME ZONE,
                    last_health_check TIMESTAMP WITH TIME ZONE,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                    CONSTRAINT valid_mode_config CHECK (
                        (monitoring_mode = 'index' AND index_instrument_token IS NOT NULL AND (index_upper_stoploss IS NOT NULL OR index_lower_stoploss IS NOT NULL)) OR
                        (monitoring_mode = 'premium' AND premium_thresholds IS NOT NULL) OR
                        (monitoring_mode = 'hybrid' AND index_instrument_token IS NOT NULL AND (index_upper_stoploss IS NOT NULL OR index_lower_stoploss IS NOT NULL) AND premium_thresholds IS NOT NULL) OR
                        (monitoring_mode = 'combined_premium' AND combined_premium_entry_type IS NOT NULL AND index_instrument_token IS NOT NULL AND (index_upper_stoploss IS NOT NULL OR index_lower_stoploss IS NOT NULL) AND (combined_premium_profit_target IS NOT NULL OR combined_premium_trailing_enabled IS TRUE OR jsonb_array_length(coalesce(combined_premium_levels, '[]'::jsonb)) > 0))
                    ),
                    CONSTRAINT valid_trailing CHECK (
                        (trailing_mode IS NULL OR trailing_mode = 'none') OR (trailing_mode = 'continuous' AND trailing_distance IS NOT NULL) OR (trailing_mode = 'step' AND trailing_distance IS NOT NULL AND trailing_step_size IS NOT NULL) OR (trailing_mode = 'atr' AND trailing_atr_multiplier IS NOT NULL)
                    )
                )
            """)
            
            # Create indexes
            cur.execute("CREATE INDEX IF NOT EXISTS idx_strategies_status ON position_protection_strategies(status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_strategies_active ON position_protection_strategies(id) WHERE status IN ('active', 'partial')")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_strategies_mode ON position_protection_strategies(monitoring_mode)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_strategies_index_token ON position_protection_strategies(index_instrument_token)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_strategies_updated ON position_protection_strategies(updated_at DESC)")
            
            # Table: strategy_events
            cur.execute("""
                CREATE TABLE IF NOT EXISTS strategy_events (
                    id BIGSERIAL PRIMARY KEY,
                    strategy_id UUID NOT NULL REFERENCES position_protection_strategies(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL CHECK (event_type IN ('created', 'updated', 'index_stoploss_triggered', 'premium_stoploss_triggered', 'index_upper_stoploss_triggered', 'index_lower_stoploss_triggered', 'combined_premium_triggered', 'combined_premium_profit_target', 'combined_premium_index_upper_stoploss', 'combined_premium_index_lower_stoploss', 'combined_premium_trailing_sl', 'level_triggered', 'trailing_activated', 'trailing_updated', 'product_converted', 'paused', 'resumed', 'completed', 'order_placed', 'order_filled', 'order_failed', 'virtual_contract_calculated', 'error')),
                    trigger_price NUMERIC(18,6),
                    trigger_type TEXT CHECK (trigger_type IN ('index', 'premium', 'combined_premium')),
                    level_name TEXT,
                    quantity_affected INT,
                    lots_affected NUMERIC(10,2),
                    order_id TEXT,
                    correlation_id TEXT,
                    idempotency_key TEXT,
                    order_type TEXT,
                    order_status TEXT,
                    filled_quantity INT,
                    average_fill_price NUMERIC(18,6),
                    instrument_token BIGINT,
                    positions_affected JSONB,
                    highest_price_at_event NUMERIC(18,6),
                    trailing_level_at_event NUMERIC(18,6),
                    trailing_mode TEXT,
                    product_before TEXT,
                    product_after TEXT,
                    error_message TEXT,
                    error_details JSONB,
                    retry_count INT DEFAULT 0,
                    meta JSONB,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
                )
            """)
            
            # Create indexes for strategy_events
            cur.execute("CREATE INDEX IF NOT EXISTS idx_strategy_events_strategy_id ON strategy_events(strategy_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_strategy_events_created_at ON strategy_events(created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_strategy_events_type ON strategy_events(event_type)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_strategy_events_order_id ON strategy_events(order_id) WHERE order_id IS NOT NULL")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_strategy_events_correlation_id ON strategy_events(correlation_id) WHERE correlation_id IS NOT NULL")
        
        conn.commit()
        logging.info("Position Protection System tables created/verified successfully.")
    except Exception as e:
        logging.error(f"Error creating Position Protection tables: {e}")
        conn.rollback()
        raise

def get_db_connection():
    """Establishes and returns a database connection, ensuring tables exist."""
    try:
        conn = psycopg2.connect(
            dbname=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT")
        )
        logging.info("Successfully connected to the database.")
        global _SCHEMA_APPLIED
        if not _SCHEMA_APPLIED:
            create_tables_if_not_exists(conn)
            _SCHEMA_APPLIED = True
        return conn
    except Exception as e:
        logging.error(f"Error connecting to the database or creating tables: {e}")
        raise

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
)

# synchronous SQLAlchemy
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

def get_db():
    """Dependency to get a DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# async Database client (if you still need it)
database = Database(DATABASE_URL)

# ───────── User Settings ─────────
import json

def get_user_settings(db_session, owner_id: str = "default") -> dict:
    """Fetches user settings JSON from the database."""
    from sqlalchemy import text
    try:
        stmt = text("SELECT settings_json FROM user_settings WHERE owner_id = :owner_id")
        result = db_session.execute(stmt, {"owner_id": owner_id}).fetchone()
        if result and result[0]:
            return result[0]
    except Exception as e:
        logging.error(f"Error fetching user settings for {owner_id}: {e}")
    return {}

def update_user_settings(db_session, settings: dict, owner_id: str = "default"):
    """Upserts user settings JSON to the database."""
    from sqlalchemy import text
    try:
        stmt = text("""
            INSERT INTO user_settings (owner_id, settings_json, last_updated)
            VALUES (:owner_id, :settings, NOW())
            ON CONFLICT (owner_id) DO UPDATE SET
                settings_json = EXCLUDED.settings_json,
                last_updated = NOW();
        """)
        db_session.execute(stmt, {"owner_id": owner_id, "settings": json.dumps(settings)})
        db_session.commit()
    except Exception as e:
        logging.error(f"Error updating user settings for {owner_id}: {e}")
        db_session.rollback()
        raise

async def get_nifty50_instruments():
    """
    Fetches Nifty50 instruments from the database.
    """
    from databases import Database
    DATABASE_URL = os.getenv(
        "DATABASE_URL",
        f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
    )
    database = Database(DATABASE_URL)
    await database.connect()
    query = """
        SELECT instrument_token, tradingsymbol, sector
        FROM kite_ticker_tickers
        WHERE source_list='Nifty50' AND instrument_token IS NOT NULL;
    """
    results = await database.fetch_all(query)
    await database.disconnect()
    return results
