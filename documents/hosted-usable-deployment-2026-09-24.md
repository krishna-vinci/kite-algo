# Hosted usable platform deployment — 2026-09-24

Reviewed release: `e23be53` on `development`, committed unsigned locally; not pushed.
The user authorized commit, deployment, data-only testing, and paper testing in
approval-based and autonomous modes. Real orders were not authorized.

## Executed

- Built `finance-app`, `alerts-worker`, `strategy-runner`, and `frontend-next`
  with `compose.yml`, `compose.worker.yml`, and `compose.supervisor.yml`.
- Recreated the API first; its entrypoint upgraded the production database from
  `20260922_000041` to `20260923_000042`.
- Confirmed API health before recreating the other three application services.
- All four containers report healthy. PostgreSQL, Redis, market runtime, MCP,
  and other services were not recreated.
- Deployed hashes match workspace release files for the execution dispatcher,
  worker execution router, proposal service, and SDK hosted loader.
- The production frontend manifest includes `/strategies/new`.
- Unauthenticated `/api/strategies` returns 401; `/strategies/new` redirects
  with 307 to authentication.
- The new governed execution request and grant tables contain zero rows.

## Preserved

The existing paper recovery-required job was not reconciled or restarted.
No hosted jobs were queued/running at the deployment precheck. No live plan
submissions existed. Existing account scopes and live settings were unchanged;
the configured hosted account allowlist contains only a paper scope.
No real orders, notifications, or schedule activations were performed.

## Pending evidence

Authenticated deployed UI testing needs an operator login/session. The environment
holds a password hash, not a usable plaintext password. No authentication bypass
or fabricated operator cookie was used. Earlier isolated UI/paper evidence is
recorded in the phase reports and is not represented as deployed UI evidence.

The supplied Nifty-500 momentum adapter was excluded from this release commit.
It subsequently passed four isolated paper integration scenarios and 94 focused
tests, and is accepted as a paper-experimental example in a separate commit.
Its corrected breadth rule is daily no-entry/exit when breadth fails, with
monthly selection/rebalancing. It has not been registered or run in production.
Recurring operation remains blocked by hosted attempt recovery and invested-book
admission restrictions; see `documents/hosted-momentum-closure-2026-09-24.md`.
The general SDK dataclass loader fix is included in the deployed release.
