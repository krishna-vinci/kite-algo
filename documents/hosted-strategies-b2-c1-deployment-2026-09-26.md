# Hosted strategies B2 + C1 deployment — 2026-09-26

Released revision: `685ddca` on `development`, pushed to `origin`. It contains the B1–B2 dynamic governed
options work (paper-certified, `documents/hosted-options-b2-completion-2026-09-26.md`) and the C1 live-readiness
work (fake-broker verified, `documents/hosted-live-readiness-c1-completion-2026-09-26.md`).

Authorized by the owner: push, production deploy, and C1.3 (adding the owner's own account to the live scope,
verified read-only). Not authorized and not done: real orders, notifications, enabling any live lane (C2).

## Rollout

All four application images were built before any container was replaced:

| Service | Image ID |
| --- | --- |
| `finance-app` | `44c2bb532f0b` |
| `alerts-worker` | `d9f5705ee4aa` |
| `strategy-runner` | `dee291155388` |
| `frontend-next` | `001005d04ee5` |

`finance-app` was recreated first. Its entrypoint applied the additive migrations
`20260924_000043 → 20260925_000044 → … → 20260925_000050 → 20260926_000051` in order, logged
`Schema migrations ensured` and `Application startup complete`, and became healthy. Only then were
`alerts-worker`, `strategy-runner` and `frontend-next` recreated. PostgreSQL, Redis, market-runtime, mcp-go and
their volumes were not recreated.

## Verification

- Production migration head: `20260926_000051`.
- All four recreated containers report healthy.
- SHA-256 of 11 key files inside `finance-app` (`/app`) match `685ddca`: live adapter, limit orders, live
  sequence/service, approvals, protection ownership, staged exit, plan binding, repair, owner actions, and the
  `000051` migration.
- Unauthenticated `/api/strategies`, `/api/strategies/{id}/option-runs` and
  `/api/strategies/{id}/owner-actions/flatten` return `401`.
- Startup logs: the API ran the migrations and started normally; the alerts worker started with 0 evaluations,
  0 deliveries and 0 errors; the supervisor is cycling; the frontend is ready.

## Production state and boundaries

The same before and after the rollout. The deployment created no hosted work:

- `strategy_jobs`: one pre-existing `recovery_required` row and three `stopped` rows.
- Enabled hosted schedules: 0. Governed execution requests: 0. Live plan submissions: 0.
- New tables are present and empty: `option_protection_owners` 0, `strategy_flatten_operations` 0.
- `HOSTED_LIVE_ENABLED=true`. `HOSTED_STRATEGY_ACCOUNT_SCOPES` held one entry (masked `kit***`, the pre-existing
  paper account) at deploy time.

No real broker order was placed and no notification was sent.

## C1.3

Pending the owner adding their own broker account to `HOSTED_STRATEGY_ACCOUNT_SCOPES` in the deployment env
file (agents may not edit `.env*`), followed by a `finance-app` restart and a read-only readiness check. No live
lane is enabled by C1.3; C2 enables lanes one at a time (CNC → MIS → futures → options), each with the owner's
approval.
