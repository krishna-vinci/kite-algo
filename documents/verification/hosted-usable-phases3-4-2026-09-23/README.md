# Hosted-strategy Phases 3-4 verification screenshots (2026-09-23)

Captured during the Phase-3/4 acceptance pass for the hosted-strategy composer
and operation UI. See
`documents/hosted-strategies-usable-phase3-4.md` for the full report.

Provenance - these are **not** production screenshots:

- database: a disposable `kite_ui_qa_<random>` database on the shared test
  instance (`127.0.0.1:15433`), migrated with `alembic upgrade head` and dropped
  after the run; `harness-result.json` names the database of the recorded run
- API: the real `/api/auth` and `/api/strategies` routers served over loopback by
  `uvicorn`, with the market boundary faked (one deterministic price) and no
  broker, notification or supervisor process
- frontend: the real `next dev` server on `127.0.0.1:3300`, using the app's
  existing `/api` rewrite
- browser: headless Chrome 146 driven over CDP with real DOM input events

`harness.py` is the reproducible driver (`python harness.py`), and
`harness-result.json` is what the recorded run asserted. No production account,
order, notification, migration or deployment was involved.

| File | Shows |
| --- | --- |
| `01-composer-ready-desktop.png` | the single creation page with the tested starter pasted and readiness "Ready to run" |
| `02-composer-keyboard-focus.png` | the source editor focused and typed into with keyboard events only |
| `03-composer-blocked-source.png` | a source with no `main(ctx)` blocked, with the reason and the primary action disabled |
| `04-composer-file-type-error.png` | a dropped `.txt` refused, with the typed source preserved and the launch still allowed (`refused_drop_notice_shown`, `refused_drop_does_not_block_launch`) |
| `05-composer-schema-parameter.png` | a parameter added as an ordinary field with the value the first run sends (`symbol` = `NSE:NIFTY 50`) |
| `06-composer-data-only-permissions.png` | a data-only strategy has no trade decision at all |
| `07-composer-autonomous-limits.png` | automatic trading selected, owner limits empty (no defaults); review-first shows its own limits (`review_first_limits_visible`) |
| `08-strategy-detail-created.png` | the strategy page after create-and-queue, data-only |
| `09-run-now-schema-parameters.png` | Run now with the pinned version's schema as a real field, not a JSON box (`run_now_params_are_fields`) |
| `10-authorization-summary.png` | the exact meaning of the grant about to be issued, and that this version cannot trade (`authorization_notes_no_trade_capability`) |
| `11-authorization-granted.png` | the active authorization and the grant history |
| `12-authorization-revoked.png` | the revoked grant, with "does not cancel an order the broker already holds" |
| `13-execution-request-waiting.png` | a pending request: "Waiting for your decision", not a running process, with the queued/dispatched/unresolved reading of the states |
| `14-plan-review.png` | the frozen plan's legs and the admission/margin preview |
| `15-schedule-form.png` | the schedule form with the run's own parameter values and the session check |
| `16-schedule-saved.png` | next run, last occurrence and the runtime's missed-run/overlap policy |
| `17-schedule-disabled.png` | the schedule disabled |
| `18-composer-mobile.png` | the composer at a 390px layout viewport (the shell, not this page, sets the width - see section 6 of the report) |
| `19-strategy-detail-mobile.png` | the strategy page at a 390px layout viewport, same shell limit |
| `20-strategy-detail-narrow-tables.png` | the wide tables scrolling inside their own card at 390px instead of widening the page |
