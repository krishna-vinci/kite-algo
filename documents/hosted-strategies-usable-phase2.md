# Hosted strategies - Phase 2 (governed execution) report

Date: 2026-09-23. Status: **ready_for_review** (Astra owns acceptance; nothing
here is self-accepted). Task `/root/hosted_execution_authorization`, workspace
`/home/krishna/kite-algo`, baseline `27c58b4` plus the accepted Phase-1 working
tree, prior production code `f0c747c`.

Implements Phase 2 of
`documents/hosted-strategies-usable-platform-plan-2026-09-23.md` against
`docs/agent-work/hosted-usable-platform/AUTHORIZATION-CONTRACT.md` and
`PHASE2-BRIEF.md`. No frontend work. No commit, stage, push, deploy, production
migration, real broker order, notification or live-authorization change was
performed; the only PostgreSQL used is the disposable instance on **15433**
(unique database per test, dropped afterwards). Port 15432 was never contacted.

## 1. What changed

### Schema (additive, one head)

`backend/alembic/versions/20260923_000042_hosted_execution_authorization.py`
(`down_revision = 20260922_000041`, still a single head), mirrored in
`backend/schema.sql` and the ORM (`backend/strategies/models.py`,
`backend/strategies/attribution_models.py`):

- `hosted_strategies.authorization_mode` - `approval_based` (DB default) or
  `autonomous`. The ORM column carries the same server default, so a raw insert
  that predates the column reads `approval_based`.
- `hosted_execution_grants` - immutable grant identity: owner, hosted and
  canonical strategy, immutable `version_id` + `version_number` +
  `source_sha256`, `account_id`, `execution_environment`, `policy_hash` +
  `policy_snapshot`, issuer/time, optional expiry, revocation and supersession
  metadata, plus the caller's `request_key` and its `content_sha256`. Composite
  FKs bind it to the hosted strategy/owner, the version, and the canonical
  strategy/account. `uq_hosted_execution_grant_active` is a **partial unique
  index** on `(strategy_id, account_id, execution_environment) WHERE status =
  'active'`. Triggers refuse identity mutation, deletion, and any
  `non-active -> active` transition.
- `hosted_execution_requests` - the durable, idempotent request identity
  (`UNIQUE (owner_id, plan_id, idempotency_key)`), with mode/grant, attempt and
  lease-epoch snapshot, plan hash, decision kind/actor/time/evidence, the linked
  reservation/approval/execution trail, the durable dispatch claim, and the
  outcome detail.
- `hosted_execution_audit` - append-only (trigger) audit of mode, grant, request
  and dispatch events with a server-derived `actor_kind` (`owner` / `system` /
  `automatic_grant`).
- `strategy_approvals.actor_kind` (`manual` default) and
  `authorization_evidence` - an autonomous authorisation is recorded as
  `automatic` with grant evidence, so it is never readable as a manual click.
- The ORM `execution_mode` checks for `hosted_strategies`,
  `hosted_strategy_schedules` and `strategy_jobs` were aligned with the
  already-shipped live migration (ORM-only parity; the database was already
  widened by `20260922_000039`).

### Authorization service (`backend/strategies/execution_authorization.py`)

Mode, grant and policy decisions, and nothing else. `set_mode`, `issue_grant`,
`revoke_grant` and the dispatcher's claim all take the `hosted_strategies` row
`FOR UPDATE` first, so "revocation won before the claim" is decidable rather
than racy. No network call happens inside that transaction.

- `policy_snapshot` = the recorded admission policy (`StrategyAdmissionPolicy`)
  plus the strategy's mandatory run-protection policy (`stale_exit_policy`,
  `max_duration_s`, `progress_deadline_s`); `policy_hash` is the canonical
  SHA-256 of that object. ANY change - including a tightening - invalidates the
  grant.
- `issue_grant` derives the source hash, account and policy from persisted
  records; the environment is validated against the vocabulary, and a `live`
  grant is refused while `HOSTED_LIVE_ENABLED` is off. A repeat with the same key
  AND the same content returns the original row (including its revocation
  state); the same key with different content conflicts. Issuing a new grant
  supersedes the previous active one in the same transaction.
- `evaluate(...)` re-derives authorisation for an exact
  version/source/account/environment/policy and names the refusal
  (`GRANT_REQUIRED`, `GRANT_REVOKED`, `GRANT_SUPERSEDED`, `GRANT_EXPIRED`,
  `GRANT_VERSION_MISMATCH`, `GRANT_SOURCE_CHANGED`, `GRANT_POLICY_CHANGED`,
  `GRANT_ACCOUNT_MISMATCH`, `AUTHORIZATION_MODE_NOT_AUTONOMOUS`). It accepts an
  optional session so the dispatch claim re-derives inside its own transaction.

### Shared pipeline (`backend/strategies/plan_pipeline.py`)

The single authoritative path from a frozen plan to an execution: authoritative
plan lookup, DERIVED environment (persisted binding), admission, reservation,
structural approval and execution. The operator routes and the background
dispatcher both call it; the router no longer re-implements any of it.

Two real weaknesses were closed while extracting it:

- `/plans/{id}/reserve` and `/plans/{id}/admission` used to take
  `execution_environment` from a query parameter, so a paper plan could be
  reserved in the `live` environment. The environment is now derived, and an
  explicitly disagreeing parameter is refused `PLAN_ENVIRONMENT_MISMATCH`.
- `ApprovalService.approve` gained explicit `actor_kind` / `evidence`, and the
  pipeline records an automatic authorisation with grant provenance.

### Durable execution requests (`backend/strategies/execution_requests.py`)

One durable request per `(owner, plan, idempotency key)`, created from the
persisted job/binding, never from the payload:

- `approval_based` -> `awaiting_approval`. Waiting is not execution: no
  approval, no reservation, no order.
- `autonomous` -> `queued` only under a matching current grant, recorded as
  `decision_kind=automatic` with grant evidence; otherwise `refused` with the
  named reason.
- `approve` / `reject` are owner-only, transactional and terminal, so a dropped
  HTTP response cannot lose an approved action.
- `claim_next` re-derives attempt authority, current mode, current policy hash
  and the grant before it flips a row to `dispatching` - under the strategy row
  lock.
- `dispatch` runs the shared pipeline (admission -> reservation -> structural
  approval -> execution) and records `executed`, `refused` (named) or
  `dispatch_unresolved`.
- `recover_abandoned_claims` inspects claims a dead dispatcher left behind: a
  plan-trail/pre-send record proves submission (`executed`); anything else stays
  `dispatch_unresolved` and is NOT retried.
- `dispatch` records the EXECUTOR's own outcome word (`submitted`, `filled`,
  `partial`, `rejected`, `no_op`, `uncertain`, `failed`) beside the request
  status, so "request dispatched" is never reported as "trade completed". A
  `filled` result is the only one whose operator `next_action` says every step
  filled.
- `release_authority_check(plan)` returns a callable the live adapter runs
  INSIDE the release-claim transaction (holding the canonical book lock and the
  hosted-strategy row lock a revocation takes). It loads the ORIGINAL governing
  request first and re-derives its mode/grant/version/source/policy/attempt on
  every dependent release, including when the strategy's CURRENT mode has moved
  back to `approval_based`. `LivePlanExecutor` also uses this production check
  when no collaborator was injected, so its default factory fails closed rather
  than skipping the check.

### Dispatcher and application lifecycle

`backend/strategies/execution_dispatcher.py` - one bounded pass at a time
(`recover -> claim -> dispatch`), a health snapshot (state, last pass, counts,
last error) published through the ordinary component-status surface, and a
deployment switch `HOSTED_EXECUTION_DISPATCH_ENABLED` (only an explicit falsy
spelling disables it). `backend/app/bootstrap.py` builds the shared pipeline
(`ensure_governed_execution_state`) and starts/stops the dispatcher inside
`combined_lifespan`, so it is real app machinery rather than test-only polling.

### Operator API (`/api/strategies/{strategy_id}`)

Cookie auth, the same-origin assertion and owner scoping are the existing ones;
no actor, source hash, account or policy hash is accepted from the caller.

| Method | Path | Meaning |
| --- | --- | --- |
| GET | `/authorization` | mode, active grant, current policy evidence/hash, `grant_usable`, `blocking_reasons` |
| PUT | `/authorization` | `{"mode": "approval_based"|"autonomous", "reason"?: str}` |
| GET | `/authorization/grants` | grant history (revoked/superseded kept) |
| POST | `/authorization/grants` | `{"idempotency_key", "version_id", "execution_environment", "expires_at"?}` |
| POST | `/authorization/grants/revoke` | `{"grant_id"?, "reason"?}` |
| GET | `/execution-requests` | durable requests, newest first |
| GET | `/execution-requests/{request_id}` | one request |
| POST | `/execution-requests/{request_id}/approve` | owner authorises the exact plan |
| POST | `/execution-requests/{request_id}/reject` | owner refuses, terminal |

### Worker API and SDK

New worker router `backend/api/routers/worker_executions.py`:

| Method | Path | Action |
| --- | --- | --- |
| POST | `/worker/executions` | `proposals:submit` + hosted attempt + session nonce |
| GET | `/worker/executions?strategy_run_id=` | `runs:read` |
| GET | `/worker/executions/{request_id}?strategy_run_id=` | `runs:read` |
| GET | `/worker/runs/{strategy_run_id}/positions` | `runs:read` (owned book + pending work) |

SDK (sync + async + `ManagedRun` + `ChildContext`, with manifest entries so the
audited contract-coverage test still matches the mounted routes):
`request_execution(plan_id, idempotency_key=...)`,
`list_execution_requests(...)`, `get_execution_request(...)`,
`get_owned_work(...)`, and the convenience
`ManagedRun.submit_and_request_execution(payload, idempotency_key=...)`, which
composes proposal + request without merging their durable identities.
`sdk/python/README.md` documents the mode/grant contract.

### Bypass refusal and mandate checks

`backend/api/services/hosted_attempt.py` gained the hosted guards; external runs
return from all of them unchanged:

- `assert_hosted_discretionary_mutation_allowed` - a hosted child may not place,
  basket, bracket, cancel, modify or flatten raw orders. `submit_worker_intent`
  (non dry-run), `exit_worker_run` (non dry-run), bracket create/cancel and
  order cancel/modify now refuse with `HOSTED_RAW_MUTATION_FORBIDDEN`, naming
  the governed surface. The SAME guard covers the options surface
  (`options.create_run`, `options.enter`, `options.exit`, `options.protection`,
  `options.protection_replay`) and the worker
  `PATCH /worker/runs/{id}/protection` route, which could otherwise disable or
  relax backend protection. Dry-run intents and dry-run exits are unchanged
  (they mutate nothing).
- `assert_hosted_owner_policy_keys_untouched` - a hosted child's `risk:update`
  patch (and the protection payload) may not name ANY owner-mandated policy key
  (`allocation_inr`, `per_instrument_notional_inr`, `gross_notional_inr`,
  `max_open_instruments`, `admissions_per_window`, `admission_window_seconds`,
  `daily_loss_budget_inr`). A tightening is an owner decision too, and an
  unreadable/`None`/NaN value is refused all the same, because the check is a
  key vocabulary rather than a numeric comparison. Named
  `HOSTED_OWNER_POLICY_MUTATION_FORBIDDEN`.

### Owned-work snapshot (`backend/strategies/execution_snapshot.py`)

Reads the existing attributed projection, the plan execution trail, the live step
claims and the governed requests for the run's canonical strategy: positions with
`unresolved_reason`; pending rows carrying `submitted_quantity` /
`filled_quantity` / `remaining_quantity` (never double-counted) and `coverage`
(`known` / `unknown`); projection publication/version/freshness; and
unknown-coverage notes. An unpublished projection is `unpublished_unknown`,
never a flat book. Because the read is per-strategy rather than per-run, a
previous attempt's exposure and pending work stay visible.

Three rules decide the shape:

* **One coordinate, one row.** A `(plan_id, step_no)` described by both a trail
  event and a `live_plan_submissions` claim is MERGED, not concatenated, so a
  fill is never counted as remaining work twice. If the two sources disagree
  about the outstanding quantity, neither is picked: the row is `unknown`.
* **Attributed, never relabelled.** Plans are selected through the authoritative
  `strategy_run_bindings` of the requested `(account, strategy, environment)`, and
  every row reports its OWN `execution_environment`, `product`, `side`,
  `tradingsymbol` and `instrument_id` (from the frozen live step spec, else the
  plan leg). A paper snapshot never relabels a live row.
* **Unknown is unknown.** Coverage is `known` only when the projection is
  published AND fresh AND no pending unit lacks quantity evidence. A stale
  projection, an unpublished projection or one unknown unit makes the overall
  coverage `unknown`.

The state vocabulary is the one the shipped migrations
(`20260922_000039`/`000040`) enforce: non-terminal
`pending`/`withheld`/`releasing`/`partial`/`finalizing`/`rejecting`/
`repair_required`/`uncertain`; terminal
`filled`/`rejected`/`no_op`/`residual_abandoned`. The ORM constraint was brought
into step with the migration/schema vocabulary.

## 2. Frontend handoff (exact final schema)

Statuses a request can hold: `requested`, `awaiting_approval`, `queued`,
`dispatching`, `executed`, `refused`, `rejected`, `dispatch_unresolved`.

Named refusal codes surfaced in a request's `refusal_code`: `GRANT_REQUIRED`,
`GRANT_REVOKED`, `GRANT_SUPERSEDED`, `GRANT_EXPIRED`, `GRANT_VERSION_MISMATCH`,
`GRANT_SOURCE_CHANGED`, `GRANT_POLICY_CHANGED`, `GRANT_ACCOUNT_MISMATCH`,
`AUTHORIZATION_MODE_NOT_AUTONOMOUS`, `HOSTED_ATTEMPT_FENCED`,
`HOSTED_ATTEMPT_STOPPED`, `HOSTED_ATTEMPT_REPLACED`, `HOSTED_LEASE_EXPIRED`,
`HOSTED_ATTEMPT_UNKNOWN`, `POLICY_CHANGED`, `ADMISSION_REFUSED`,
`PLAN_ALREADY_EXECUTED`, `OWNER_REJECTED`, `DISPATCH_OUTCOME_UNKNOWN`, plus the
executors' own refusal codes. Dispatch outcomes add `ORDER_REJECTED`, `NO_OP`,
`TRANSPORT_UNCERTAIN` and `EXECUTION_OUTCOME_UNKNOWN`; the executor's own word
is in `execution_detail.outcome_state` (`submitted`, `accepted`, `filled`,
`partial`, `rejected`, `no_op`, `uncertain`, `failed`). `terminal=true` means
"this request will not be dispatched again", never "the trade finished".

Authorization status (`GET /authorization`): `strategy_id`,
`authorization_mode`, `active_grant` (or null), `policy_snapshot` (the admission
and protection halves verbatim), `policy_hash`, `policy_concrete`,
`grant_usable`, `blocking_reasons`, `evaluated_at`. `policy_concrete=false`
means no capital basis is recorded: ask for the missing limits rather than
implying they are enforced. `grant_usable` describes the current record;
execution re-derives it server-side under the strategy lock.

Grant fields: identity (`grant_id`, `strategy_id`, `canonical_strategy_id`,
`version_id`, `version_number`, `source_sha256`, `account_id`,
`execution_environment`); evidence (`policy_hash`, `policy_snapshot`,
`issued_by`, `issued_at`, `expires_at`); lifecycle (`status`, `revoked_by`,
`revoked_at`, `revocation_reason`, `superseded_by`, `superseded_at`,
`supersession_reason`); plus `request_key` and `content_sha256` (technical -
hide both, and never reuse a key for a changed request) and `idempotent`.

Request fields: `request_id`, `strategy_id`, `canonical_strategy_id`,
`account_id`, `execution_environment`, `strategy_run_id`, `job_id`, `attempt`,
`lease_epoch`, `version_id`, `version_number`, `source_sha256`, `policy_hash`,
`evaluation_id`, `plan_id`, `plan_hash`, `authorization_mode`, `grant_id`,
`status`, `refusal_code`, `refusal_detail`, `decision_kind` (`manual` /
`automatic`), `decision_actor`, `decision_at`, `decision_evidence`,
`approval_id`, `reservation_id`, `execution_detail`, `dispatch_claim_id`,
`dispatch_claimed_at`, `dispatch_started_at`, `dispatch_finished_at`,
`idempotency_key` (hide), `created_at`, `updated_at`, `terminal`, `executable`.

Worker response additions: `next_action` (a sentence the UI may show verbatim)
and `idempotent`. `GET /worker/runs/{id}/positions` returns `projection`
(`published`, `coverage`, `projection_version`, `content_sha256`,
`last_rebuild_at`, `age_seconds`, `fresh`), `positions`, `pending` (each with
`source` =
`plan_execution` / `live_submission` / `execution_request`, `state`, the three
quantities, `coverage`, `reason`, `detail`, `sources`, `execution_environment`,
`instrument_id`, `exchange`, `tradingsymbol`, `product`, `side`, `confidence`),
`coverage`, `observed_at`, `notes`. The operator approval row now also exposes
`actor_kind` (`manual` / `automatic`) and `authorization_evidence`, so an
automatic grant decision is never displayed as an owner click.

### SDK example (hosted child)

```python
def main(ctx):
    proposal = ctx.client.submit_proposal({
        "strategy_run_id": ctx.run_id,
        "evaluation_id": "eval-2026-09-23-1",
        "evaluation_kind": "run_now",
        "target_kind": "single_instrument",
        "payload": {"legs": [{"instrument_id": "...", "signed_quantity": 5}]},
    })["plan"]

    request = ctx.request_execution(plan["plan_id"], idempotency_key="eval-2026-09-23-1")
    # approval_based -> "awaiting_approval"; autonomous + matching grant -> "queued"
    if request["status"] == "refused":
        log(request["refusal_code"])          # named, no trade placed
    if request["status"] == "awaiting_approval":
        return                                # the owner decides in the app

    work = ctx.owned_work()
    pending = [row for row in work["pending"] if row["plan_id"] == plan["plan_id"]]
    if pending:                               # already asked for; do not repeat it
        return
```

## 3. Verification (exact commands, exits, results)

Every pytest run used the repo venv and was **escalated out of the sandbox**: the
async harness hangs inside it (the same limitation Phase 1 recorded). No suite
touched 15432, production containers, the broker, or notifications.

Exit codes are the **real process status** of `pytest`, captured without a pipe.
A piped run reports the last command in the pipeline, which is how the earlier
draft of this table came to record `0` next to failures. Logs for these runs are
under `/tmp`.

| # | Command | Exit | Result |
| --- | --- | --- | --- |
| 1 | `.venv/bin/python -m pytest tests/strategies/test_execution_authorization.py tests/strategies/test_execution_dispatcher.py tests/api/test_hosted_execution_requests.py tests/api/test_hosted_execution_bypasses.py tests/api/test_hosted_child_authority.py tests/options/test_options_auth_boundaries.py tests/strategies/test_lifecycle_prepare.py -q` | 0 | 131 passed - grant lifecycle and every `evaluate` refusal; dispatcher truth table, bounded pass, degraded pass, stale-finish CAS; the durable request workflow over the REAL `PaperPlanExecutor` (manual wait then one submission; autonomous under a matching grant); the result-honesty matrix (`submitted`/`accepted`/`filled`/`partial` preserved, `rejected`/`no_op` refused by name, `uncertain`/`failed`/exception unresolved); snapshot merge/attribution/coverage; options/protection/risk bypass refusal with external parity |
| 2 | `HOSTED_EXECUTION_PG_URL=...15433... .venv/bin/python -m pytest tests/integration/test_hosted_execution_authorization_postgres.py -q` | 0 | 13 passed - migration from zero and from the prior head `20260922_000041`, single head `20260923_000042`, downgrade/upgrade, a legacy row defaulting to `approval_based`, the partial unique active-grant index, identity/delete/no-reactivation triggers, append-only audit, revocation-versus-claim ordering both ways, **concurrent identical creates replay one row** (4 racers: one writer, three replays), changed-content conflict, **stale finish refused by CAS**, superseded-claim finish refused |
| 3 | `RECONCILIATION_PG_ADMIN=...15433... .venv/bin/python -m pytest tests/integration/test_hosted_execution_governed_release_postgres.py -q` | 0 | 3 passed - the REAL `LivePlanExecutor.release_sequence` over a fake broker: an active grant releases leg 2 exactly once; a revocation between the legs keeps it `withheld` with `release_blocked=GRANT_REVOKED` and places nothing; a mode change between the legs refuses with `AUTHORIZATION_MODE_NOT_AUTONOMOUS`. The executor in that harness has **no** injected authorization reader, so the check under test is the production default |
| 4 | `RECONCILIATION_PG_ADMIN=...15433... .venv/bin/python -m pytest tests/integration/test_hosted_live_phase2a_routes_postgres.py tests/integration/test_hosted_live_phase2b_routes_postgres.py -q` | 0 | 14 passed - the pre-existing CNC/MIS and futures-roll/option-structure live suites are unchanged by the default authoritative release check |
| 5 | `.venv/bin/python -m pytest tests/strategies/test_attribution_service.py tests/api/test_strategies_api.py tests/strategies/test_execution.py tests/api/test_algo_worker_api.py tests/api/test_algo_worker_route_mounts.py tests/sdk -q` | 1 | 523 passed, 1 skipped, 3 failed - **all three pre-existing / not caused by this bundle**: 2 are `tests/sdk/test_worker_protection_runtime.py` against the untouched `backend/api/services/protection_runtime.py`; 1 is `test_hosted_options_expose_only_authorized_account_scopes`, which depends on this workspace's `.env` (`HOSTED_LIVE_ENABLED=true`) |
| 6 | `HOSTED_LIVE_ENABLED=false ... pytest tests/api/test_strategies_api.py -q` | 0 | 23 passed - the same file with the env-dependent case neutralised, confirming row 5's third failure is environmental |
| 6b | `HOSTED_LIVE_ENABLED=false ... pytest tests/api/test_hosted_child_authority.py tests/api/test_hosted_execution_bypasses.py tests/api/test_hosted_execution_requests.py tests/api/test_hosted_data_foundation.py tests/api/test_hosted_proposal_authority.py tests/api/test_hosted_live_capabilities.py tests/api/test_hosted_lifecycle_api.py -q` | 0 | 134 passed - the whole hosted HTTP boundary in one process, with the env artifact removed |
| 7 | `.venv/bin/python scripts/check_worker_sdk_version_refs.py` | 0 | `All worker SDK version references match 0.14.0` |
| 8 | `.venv/bin/python -m ruff check <every new/changed Phase-2 file>` | 0 | `All checks passed!` (the two `text` findings in the pre-existing `live_service.py` imports are unchanged from `HEAD`) |
| 9 | `.venv/bin/python -m py_compile <every new/changed Phase-2 file>` | 0 | compiled cleanly |

Corrections this bundle's review forced, and the tests that now pin them:

* the background release path (bootstrap, the pipeline's live executor factory
  and the router's default executor) passes the production authorization reader,
  and `LivePlanExecutor` builds it itself when none was injected;
* the dependent-release check loads the ORIGINAL governing request first and
  re-derives mode/grant/version/source/policy/attempt on every release, inside
  the release-claim transaction that takes the same strategy row lock a
  revocation takes;
* the hosted options surface, the worker protection route and owner-mandated
  risk keys are refused by name for a hosted child;
* the owned-work snapshot merges trail and live claims on `(plan, step)`,
  selects plans through the run binding, reports each row's own environment and
  instrument/side, and makes overall coverage `unknown` when any unit or the
  projection itself lacks evidence;
* dispatch records the executor's own outcome instead of a blanket `executed`;
* `finish` is CAS-fenced on `(status, dispatch_claim_id)` and `dispatch`
  re-validates the full attempt fence at the dispatch boundary;
* concurrent identical creates replay the original request instead of raising.

## 4. Tested vs unverified

Tested: the whole decision path (mode, grant issue/idempotency/supersede/revoke/
expiry/policy/version/account refusals), manual wait and owner approval/rejection,
autonomous queueing under a matching grant, the dispatch claim with single-winner
concurrency and concurrent idempotent create, recovery without replay,
stale-finish fencing, dependent-release authority through the REAL release pass
(active / revoked / mode-changed), raw-mutation and owner-policy refusal with
external compatibility, the run-bound owned-work snapshot over mixed paper/live
data, the operator cookie/origin/owner surface, and the SDK/route contract.

Not verified (stated, not implied):

- **No live broker order, fill or settlement was produced.** The broker INTENT
  boundary is faked everywhere, including the governed-release proofs. The real
  release pass, adapter, sequence store, per-step claims, reservation ledger and
  barrier are production code, but a real multi-step live release against a
  broker (and the futures-roll / option-structure lanes end to end) remains
  Phase 5 evidence.
- The dispatcher is a bounded poller; throughput under a large backlog and
  behaviour across a real process restart were not measured. Its default-ON
  choice is a deployment decision (see 5.1).
- `get_owned_work` reports what the existing books and links record. No new
  reconciliation was added, so an account whose projection was never published
  still reads `unpublished_unknown`, by design. The options-lane adjustment case
  is validated at the pending-work level, not as an arbitrary options-adjustment
  proof (that is Phase 5).
- No frontend was built, so no browser workflow is claimed, and nothing was
  deployed: the running containers still execute pre-change code.

## 5. Decisions and risks for Astra

1. **Dispatcher default.** `HOSTED_EXECUTION_DISPATCH_ENABLED` defaults to ON, so
   an owner approval or a matching grant becomes work without extra deployment
   configuration. A governed request is inert until one of those exists; flip the
   default and document it if you prefer an opt-in rollout.
2. **Hosted child `runs:exit` is now refused.** Flattening places real closing
   orders, so it moved to the governed path (a closing plan + request). The
   platform's own stale-exit policy still runs outside the child token. This is a
   deliberate capability change for hosted children and needs your acceptance;
   external runs are untouched.
3. **Paper approval is not a live approval.** A paper request in
   `approval_based` mode waits for the owner's decision, and no
   `strategy_approvals` row is fabricated for it. The live path still creates the
   real approval row, now with `actor_kind='automatic'` plus grant evidence for
   autonomous requests; the operator `ApprovalResponse` schema was extended to
   carry those two fields, because it previously rejected them and broke the live
   approval route - a regression this bundle fixes.
4. **SDK bumped to 0.14.0.** The four new public methods are a minor-version
   change; every version-reference file the checker validates was updated in the
   same pass. Two places still say `0.13.0` on purpose and were NOT rewritten,
   because they are historical evidence rather than current-state claims:
   `documents/hosted-strategies-usable-phase1.md` (its 2026-09-23 verification
   log) and `documents/hosted-strategies-live-ui.md` (a note recording that a
   particular earlier SDK change needed no bump). `documents/hosted-strategies-
   supervisor-slice-report.md` likewise describes the 0.13.0 surface it shipped.
5. **Pre-existing defects found and handled.** (a) The options mutation guard
   called `_repo(...).get_run` for external tokens, so
   `tests/options/test_options_auth_boundaries.py` failed at baseline 27c58b4;
   its fake repo now implements `get_run` (returning `None`, i.e. no bound run).
   (b) `tests/strategies/test_lifecycle_prepare.py` still asserted the pre-Phase-1
   `data=true` action set; it now matches the accepted data lens
   (`market:read`, `market:stream`, `universes:read`, `universes:resolve`).
   (c) The options protection-replay guard referenced an undeclared `request`
   parameter, which `ruff` caught before it could 500 at runtime.
6. **Policy-change semantics.** Any change to either policy half - a tightening
   included - invalidates a grant and requires a fresh authorisation, which is
   the contract's stated preference for predictability over a hidden subset
   rule. The tests pin that.
7. **Revocation is not cancellation.** A revocation denies later dispatch
   claims; a claim that already won may have reached the broker. The audit says
   exactly that, and recovery never replays an unresolved claim.
8. **The ORM state vocabulary was stale.** The `live_plan_submissions` ORM check
   constraint admitted only four states while the shipped migrations admit
   twelve; it now matches the migration/schema vocabulary so a metadata-created
   database can hold `withheld`/`partial`/`releasing`.

## 6. Checkpoint

Done: additive schema + ORM + `schema.sql` parity and migration; the
authorization service; the extracted shared pipeline (with the reserve/approval
environment fix); the durable execution-request service with claim, dispatch,
result-honesty mapping, CAS-fenced finish and recovery; the bounded dispatcher
with app-lifecycle wiring, health and a kill switch; the operator routes; the
worker router, SDK methods (0.14.0) and manifest entries; the raw-mutation,
options/protection and owner-policy guards; the owned-work snapshot; and this
bundle's corrections - the authoritative default release check, original-request
dependent-release authority under the strategy lock, concurrent-create replay,
and the new SQLite/PostgreSQL proofs including the governed release suite.

Not started: Phases 3-5 (unified first-run UI, scheduling/approval/recovery UI,
representative end-to-end examples). Nothing is committed, staged or deployed;
the working tree holds the Phase-1 diff, the Phase-2 diff and the pre-existing
unrelated changes (`documents/hosted-strategies-architecture-r1.md`, untracked
drafts, `.commandcode/`).
