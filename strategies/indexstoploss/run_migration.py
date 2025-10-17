#!/usr/bin/env python3
"""
Database migration runner for Position Protection System

Usage:
    python strategies/indexstoploss/run_migration.py
"""

import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from database import get_db_connection
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def run_migration():
    """Run the SQL migration file"""
    migration_file = Path(__file__).parent / "migrations.sql"
    
    if not migration_file.exists():
        logger.error(f"Migration file not found: {migration_file}")
        return False
    
    logger.info(f"Reading migration file: {migration_file}")
    
    with open(migration_file, 'r') as f:
        sql = f.read()
    
    conn = None
    try:
        logger.info("Connecting to database...")
        conn = get_db_connection()
        cur = conn.cursor()
        
        logger.info("Executing migration SQL...")
        cur.execute(sql)
        
        conn.commit()
        logger.info("✅ Migration completed successfully!")
        
        # Verify tables were created
        cur.execute("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = 'public' 
            AND table_name IN ('position_protection_strategies', 'strategy_events')
        """)
        
        tables = [row[0] for row in cur.fetchall()]
        logger.info(f"Created tables: {', '.join(tables)}")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Migration failed: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return False
        
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    success = run_migration()
    sys.exit(0 if success else 1)
