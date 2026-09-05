# Exchange calendar import contract

The exchange calendar for a segment is served exclusively from immutable, audited calendar versions stored in `public.exchange_calendar_source_documents` / `public.exchange_calendar_sessions` (Alembic `20260829_000006`) plus the refresh-state table `public.exchange_calendar_refresh_state` (Alembic `20260905_000008`). Missing schema fails closed with `EXCHANGE_CALENDAR_SCHEMA_MIGRATION_REQUIRED`; nothing is auto-created at runtime.

## Official source (automated daily sync)

Since 0.7.6 the platform synchronizes the calendar from the official NSE source:

- Warm-up page: `https://www.nseindia.com/resources/exchange-communication-holidays`
- API: `https://www.nseindia.com/api/holiday-master?type=trading`

Requests are sent with `User-Agent: Mozilla/5.0`, `Accept: application/json,text/plain,*/*` and the warm-up page as `Referer`, with a 20-second timeout on both requests. The exact official response bytes are hashed (`official_source_document_sha256`) separately from the canonical merged CSV (`canonical_csv_sha256`).

`backend/broker_api/market/nse_calendar_source.py`:

- accepts only these `tradingDate` formats: `%d-%b-%Y`, `%d-%b-%y`, `%d %B, %Y`, and ISO dates;
- fails closed on malformed dates, duplicate dates, missing CM data, HTML responses, and invalid/non-JSON responses;
- regenerates every released year as exactly 365 or 366 deterministic rows — weekdays default to verified `REGULAR 09:15:00–15:30:00`, weekends and official CM holidays become `HOLIDAY` rows without session times. No `SPECIAL` sessions are ever invented;
- merges refreshed years with every session of the current active immutable version: dates outside the refreshed years are preserved, and existing verified `SPECIAL` sessions inside refreshed years survive regeneration;
- imports the complete merged canonical CSV exactly once through `import_calendar_csv()` — never one calendar version per year;
- if none of the requested years has been released, it returns `released=false` and does not change the active calendar;
- if the official-source hash is unchanged, it returns `changed=false` and creates no new calendar version;
- a changed source creates exactly one new immutable version; a failed refresh retains the existing active version and updates the refresh state transactionally.

## Daily timing

`_schedule_exchange_calendar_refresh()` (started by `backend/app/bootstrap.py` as exactly one `calendar_refresh_task`) runs once daily at 05:45 `Asia/Kolkata` and refreshes the current year plus the next year. If next-year data is not released, current coverage is retained and a healthy awaiting-release state is recorded. On failure the refresh state records a degraded/failure state and the scheduler simply waits until the next daily window — there is no rapid retry loop. The task is cancelled and awaited cleanly at shutdown.

## Status endpoint

`GET /api/algo-workers/worker/market/calendar/status` (worker authentication, `market:read` permission, `schema_version=1`) returns a schema-version-1 envelope with `exchange`, `segment`, `active_calendar_version`, `coverage_start`, `coverage_end`, `complete`, `expiry_warning` (true when `coverage_end` is earlier than `now + 45 days`) and the refresh-state fields (`last_attempt_at`, `last_success_at`, `last_failure_at`, `last_error`, `observed_source_sha256`, `next_attempt_at`). Coverage is derived only from the active immutable calendar version; missing calendar data yields a truthful incomplete status. Exchange/segment are normalized to uppercase.

## Uncovered ranges fail closed

`GET /api/algo-workers/worker/market/calendar` returns `503 CALENDAR_RANGE_UNCOVERED` for any date range not fully covered by verified sessions of the active version. An uncovered date is never inferred to be a holiday (or a trading day).

## Operator import (manual recovery CLI)

`backend/cli/import_exchange_calendar.py` accepts an operator-prepared canonical CSV for manual recovery. It never downloads or scrapes NSE.

The CSV schema version is `nse_cm_sessions_v1` and requires these columns:

```text
session_date,session_type,opens_at,closes_at,verified
```

`session_type` is `REGULAR`, `HOLIDAY`, or `SPECIAL`. Every row must be verified. Holidays have empty times; tradable sessions have ordered `HH:MM:SS` open and close times.

The operator must supply both separate evidence values:

- `--source-reference`: official NSE circular or document reference.
- `--official-source-document-sha256`: SHA-256 of that official source document.

The command calculates `canonical_csv_sha256` itself from the submitted CSV and stores it separately. It never represents the canonical CSV hash as the official source-document hash.

Without `--apply` the command validates only and never opens a database connection. With `--apply`, Alembic migrations `20260829_000006` and `20260905_000008` must already be applied; otherwise it fails with `EXCHANGE_CALENDAR_SCHEMA_MIGRATION_REQUIRED`.

Example dry run:

```text
python -m backend.cli.import_exchange_calendar sessions.csv \
  --source-reference https://www.nseindia.com/example-circular \
  --official-source-document-sha256 <64-lowercase-hex> \
  --actor operator@example --reason "annual NSE CM calendar"
```

## Immutable version behavior

Every import (daily sync or operator) creates one new immutable `calendar_version` supersedes-linked to the previous one; existing versions are never rewritten. Reads always use the active (highest) version, and `SPECIAL` sessions can only enter the calendar through a verified operator import — the daily sync preserves them but never fabricates them.
