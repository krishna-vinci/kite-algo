# Exchange calendar import contract

`backend/cli/import_exchange_calendar.py` accepts an operator-prepared canonical CSV. It never downloads or scrapes NSE.

The CSV schema version is `nse_cm_sessions_v1` and requires these columns:

```text
session_date,session_type,opens_at,closes_at,verified
```

`session_type` is `REGULAR`, `HOLIDAY`, or `SPECIAL`. Every row must be verified. Holidays have empty times; tradable sessions have ordered `HH:MM:SS` open and close times.

The operator must supply both separate evidence values:

- `--source-reference`: official NSE circular or document reference.
- `--official-source-document-sha256`: SHA-256 of that official source document.

The command calculates `canonical_csv_sha256` itself from the submitted CSV and stores it separately. It never represents the canonical CSV hash as the official source-document hash.

Without `--apply` the command validates only and never opens a database connection. With `--apply`, Alembic migration `20260829_000006` must already be applied; otherwise it fails with `EXCHANGE_CALENDAR_SCHEMA_MIGRATION_REQUIRED`.

Example dry run:

```text
python -m backend.cli.import_exchange_calendar sessions.csv \
  --source-reference https://www.nseindia.com/example-circular \
  --official-source-document-sha256 <64-lowercase-hex> \
  --actor operator@example --reason "annual NSE CM calendar"
```
