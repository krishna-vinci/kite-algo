# Hosted-strategy verification screenshots (2026-09-15)

Captured during the pre-deployment verification pass for the **manual data-only
release** of hosted strategies (see
`documents/hosted-strategies-supervisor-slice-report.md` §11).

Provenance — these are **not** production screenshots:

- control plane: the real routers served locally on `127.0.0.1:8181`
- database: disposable PostgreSQL (`hosted_verify` on the test instance), migrated
  from `alembic head`
- data: disposable fixtures seeded through the real HTTP APIs
- frontend: `next dev` on `127.0.0.1:3300` pointed at that backend
- browser: headless Chrome 146 driven over CDP

No production account, order, notification or deployment was involved.
