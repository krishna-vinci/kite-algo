# Hosted strategies: all-lane live release

Date: 2026-09-22. Baseline: development `806e71f`, pushed to origin.

## Authorization and objective

The user requested commit, push, and deployment with live enabled and selected all lanes: portfolios/CNC, MIS, futures/rolls, and options. Implement the remaining production integrations before enabling them. The approved R3 architecture and implementation roadmap remain the product contract. Real-money test orders and real notifications are excluded. Deployment, required production migrations, and read-only service/broker health and evidence checks are authorized. Do not create or activate a live strategy during verification. Preserve existing strategy modes and unrelated work.

## Release contract

### Shared live execution

Introduce a production service factory for live execution using the existing intent handler, order/fill ingestion, canonical attribution, account truth, admission, reservations, approvals and execution barrier. There must be no independent live fill ledger or alternative options engine.

Execution environment is derived from the persisted strategy, run binding and proposal/plan authority, with reservation environment checked for equality. An arbitrary request parameter or reservation alone cannot switch a paper plan to live. Account-to-broker-session resolution is server-side and owner-authorized.

Production evidence readers must verify canonical account/strategy/run/environment, hosted lease owner/epoch/attempt, token and evaluation validity, and current run state. Token expiry alone is not evaluation authority. Quotes and margin/funds evidence must be fresh, for the exact frozen instruments and quantities. Empty/unpublished account truth is unknown, not a flat account. Reuse read-only broker data and the existing ingest/publish services to establish complete snapshots; do not fabricate empty snapshots.

Plan steps and frozen target deltas are admitted and reserved before submission. Claims, submission identity, execution work and linkage are durable and transactionally fenced. Broker acceptance is pending, never filled. Transport-uncertain submissions retain work and capacity and are not automatically repeated. Production order/trade ingestion joins through authoritative broker order/run/plan-step linkage, deduplicates fills using existing broker identities, updates attribution and progresses the appropriate run/roll/basket, then releases only proven unfilled residual capacity. A partial fill retains residual work. Unresolved or unavailable evidence prevents settlement/replacement.

Add a production consumer for asynchronous outcomes and recoverable sequences. It may release a previously approved step only while its bounded authority and all dependency/admission conditions remain valid. It must not submit a new exposure-increasing action merely because the process restarted or a callback repeated. Background tasks need lifecycle wiring, bounded polling, error isolation and visible health. Existing external algo-worker behavior remains compatible.

### All-lane behavior

* CNC/portfolio: immutable weight sizing and full-snapshot semantics, strategy-owned holdings, shared account attribution rules, admission and capacity for each released leg. Use existing baskets/intent execution; reductions fund increases only after confirmed evidence. Corporate-action divergence freezes affected plans and exposes operator action.
* MIS: same live pipeline with existing product/session rules and the platform's own square-off clock. Reuse its risk-reducing submission and attribution checks; do not substitute a guessed exchange close.
* Futures/rolls: pinned contracts and lot sizes, current margin evidence, persisted two-plan roll binding, complete replacement fill before ordinary old-contract release. Partial/unknown acquisition never releases the close. Expiry escalation and separately authorized risk reduction keep existing semantics.
* Options: frozen structure maps to the existing durable option run via the explicit plan/run binding; own-run fills and phase ownership determine quantities. Full hedge-fill entry gating, short-first exits, partial/unknown state retention, enforced existing protection/expiry actions and evidence-based settlement. No caller assertion proves a fill or releases a dependent step.

### Live configuration and operator interface

Use an explicit `HOSTED_LIVE_ENABLED` deployment setting, default false. Persisted mode constraints and API/SDK validation may admit `live`, but admission, launch and submission also enforce the deployment setting. The final rollout sets it true only after acceptance of all lanes. Expose actual supported modes/lanes through the existing options/capabilities endpoint and align the frontend selector and wording. Live requires the existing owner approval; no automatic approval or delegated role expansion. Existing paper/dry-run records and schedules are never converted. Existing live operations outside hosted strategies are not globally disabled by this setting.

The live reconciliation path must use current live attribution, complete account truth, domain terminal evidence and the durable barrier rather than the paper collector. Validate the proof version under the shared book lock through worker-run closure, hosted unblock and audit. No account-net-flat substitute for strategy flatness.

## Dependency-ordered implementation and acceptance

1. Shared live production factory/readers/submission/ingestion/reconciliation, additive mode constraints and default-disabled configuration. Verify real issued hosted credentials and public API to fake broker boundary, including asynchronous fills, ownership, expiry, duplicate/uncertain/restart, attribution and settlement. Keep public deployment flag disabled.
2. All-lane dispatch, delivered in dependency bundles: **2A** durable multi-step parent/step executor plus portfolio/CNC and MIS; **2B** futures/rolls and durable options/protection on that executor; **2C** SDK/frontend supported-mode flow and focused UI tests/build. Add actual production-route tests for each lane with fake broker only, including partial fills and dependent-step refusal. The parent uses a durable `live_plan_executions` row with immutable dependency specifications; per-leg submission claims remain in `live_plan_submissions`. Materialize parent, steps and barrier work atomically. Capacity must cover all pending and withheld legs, not be consumed wholesale after the first fill. Domain-specific dependencies govern release; reductions-first is a portfolio rule, not a universal options/roll rule.
3. Root acceptance; commit and push reviewed live implementation to the authorized origin. Build release images, verify from-zero and deployed-head-to-final migration on disposable PostgreSQL, then deploy production with the live setting enabled. Check migration head, container health/source revision, authenticated supported modes and read-only evidence-source readiness. Do not place orders or send notifications. Report expired broker sessions/closed-market quote unavailability honestly; configuration enabled is distinct from a currently executable plan.

## Deployment evidence and boundaries

Preflight: production DB at `20260915_000024`; one disabled paper strategy, zero schedules and no active hosted job. API/runner/alerts/frontend images predate campaign code. Compose rollout uses base + worker + supervisor overlays; API owns Alembic startup and dependents wait for API health. Re-check active work before rollout. Build before replacing containers; apply required additive migrations through the normal API entrypoint. User prefers fix-forward; no rollback rehearsal requested. Do not alter market-runtime, Redis, PostgreSQL volumes or unrelated services unnecessarily.

Use disposable PostgreSQL15433 for integration tests; production15432 only for authorized rollout/read-only readiness. Never print secrets or full environment dumps. Root owns architecture/review/Git integration; one Flash worker owns implementation and scoped validation. No model substitution. Retry transient429 with backoff.

Independent finishing bundle: the frontend/SDK work may run in parallel with backend acceptance corrections in the verified separate worktree `/tmp/kite-hosted-live-ui`. That worker owns only frontend/SDK and its report; the main worker owns backend. The shared options contract adds `live_lanes` (empty when disabled, otherwise implemented CNC/MIS/futures/options) and `live_requires_owner_approval: true`, and filters `execution_modes` by the deployment flag. Root reviews and integrates the isolated diff before the combined build. Neither worker commits or deploys.

## Completion criteria

All lanes are selectable and reach the production live execution path under owner authorization and approval, with readiness refusals intact. Production uses the reviewed pushed revision and final migration head with hosted live enabled. No live test trade is claimed. Final report includes commits, images/revisions, migrations, actual test evidence, live setting, active-work checks, remaining operational requirements and unchanged unrelated files.
