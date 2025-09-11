# Data Ingestion and Synchronization Process Design

## 1. `kite_ticker_tickers` Table Schema

The `kite_ticker_tickers` table will be created with the following schema, optimized for efficient lookup and historical data API usage:

```sql
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

-- Create an index on instrument_token for faster lookups
CREATE INDEX IF NOT EXISTS idx_instrument_token ON kite_ticker_tickers (instrument_token);

-- Create an index on tradingsymbol for faster lookups
CREATE INDEX IF NOT EXISTS idx_tradingsymbol ON kite_ticker_tickers (tradingsymbol);
```

## 2. Data Ingestion and Synchronization Python Script (`ingestion_script.py`)

### Overview
This script will consolidate data from three CSV files, match it with existing `kite_instruments` data, and then synchronize it with the `kite_ticker_tickers` table using an upsert (insert or update) logic.

### Processing Logic Steps:

#### 2.1. Process Input Data
- Read `ind_nifty50list.csv`, `ind_niftylargemidcap250list.csv`, and `ind_nifty500list.csv` individually.
- For each CSV file, assign a specific `source_list` tag (e.g., 'Nifty50', 'NiftyLargeMidcap250', 'Nifty500') to all entries originating from that file.
- Extract 'Symbol', 'Company Name', and 'Sector' for each entry.

#### 2.2. Fetch Existing `kite_instruments` Data
- Connect to the PostgreSQL database.
- Query the `kite_instruments` table to retrieve `tradingsymbol`, `instrument_token`, and `instrument_type`.
- Filter results to include only instruments where `instrument_type` is 'EQ'.
- Store this data in a dictionary for efficient lookup by `tradingsymbol`.

#### 2.3. Data Synchronization (Insert Logic)
- For each entry from the processed CSV files:
    - Look up the 'Symbol' in the `kite_instruments` data.
    - If a match is found (i.e., `tradingsymbol` exists and `instrument_type` is 'EQ'):
        - Retrieve the `instrument_token` from `kite_instruments`.
        - Insert a new record into `kite_ticker_tickers` with `instrument_token`, `tradingsymbol`, `company_name`, `sector`, `CURRENT_DATE` for `added_date`, `CURRENT_TIMESTAMP` for `last_updated`, and the specific `source_list` tag for that entry.
        - Note: Duplicate `instrument_token` values are now allowed as per the updated schema, enabling a stock to be listed multiple times if it appears in different source lists.
    - If no match is found in `kite_instruments` for a CSV symbol:
        - Log a warning message indicating the symbol could not be matched.

#### 2.4. Error Handling and Logging
- Implement logging for:
    - Successful database connections.
    - Successful data consolidation.
    - Symbols from CSVs that do not find a corresponding 'EQ' `tradingsymbol` in `kite_instruments`.
    - Successful inserts and updates into `kite_ticker_tickers`.
    - Database connection errors and other exceptions.

### 3. Execution and Verification Instructions

#### 3.1. Database Setup
1. Ensure PostgreSQL is running and accessible.
2. Connect to your database and execute the SQL script provided in Section 1 to create the `kite_ticker_tickers` table and its indexes.

#### 3.2. Python Script Execution
1. Ensure all required Python libraries (e.g., `pandas`, `psycopg2` or `sqlalchemy`) are installed.
2. Configure database connection details (host, port, database name, user, password) in the Python script (e.g., via environment variables or a configuration file).
3. Place the CSV files (`ind_nifty50list.csv`, `ind_niftylargemidcap250list.csv`, `ind_nifty500list.csv`) in the same directory as the Python script, or update the script with their correct paths.
4. Run the Python script.

#### 3.3. Data Verification
1. After the script completes, connect to the PostgreSQL database.
2. Query the `kite_ticker_tickers` table to verify that:
    - Records have been inserted/updated correctly.
    - `instrument_token` values are unique.
    - `source_list` accurately reflects the origin CSVs.
    - `added_date` and `last_updated` timestamps are correct.
3. Review the script's logs for any un-matched symbols or errors.

### Mermaid Diagram: Data Ingestion Workflow

```mermaid
graph TD
    A[Start] --> B{Read CSV Files};
    B --> C{Consolidate CSV Data};
    C --> D{Connect to Database};
    D --> E{Fetch kite_instruments Data};
    E --> F{Iterate Consolidated Symbols};
    F --> G{Match with kite_instruments?};
    G -- Yes --> H{Retrieve instrument_token};
    H --> I{Check kite_ticker_tickers for instrument_token};
    I -- Exists --> J{Update existing record};
    I -- Not Exists --> K{Insert new record};
    J --> L{Log Update};
    K --> L{Log Insert};
    L --> F;
    G -- No --> M{Log Unmatched Symbol};
    M --> F;
    F -- All Symbols Processed --> N{Close Database Connection};
    N --> O[End];