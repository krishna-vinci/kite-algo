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
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Load environment variables from .env file
load_dotenv()

# the Base for all ORM models
Base = declarative_base()
metadata = MetaData()

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
        create_tables_if_not_exists(conn)
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

# async Database client (if you still need it)
database = Database(DATABASE_URL)
