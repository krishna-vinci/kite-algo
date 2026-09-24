# Recurring hosted portfolios and options: implementation contract

Baseline: `eed55a1`, development. Root Astra owns architecture, review and release;
one native Flash worker owns each implementation bundle. The user approved the
previously explained separation of evaluation lifetime from portfolio lifetime,
implementation, UI testing and option-strategy testing. No real-money test orders
or account-allowlist expansion are authorized by this work.

## Result

A finite evaluation can finish with intentionally held, correctly attributed
positions. The next evaluation reads that same durable portfolio and changes only
the required quantities. Unknown execution, cleanup or ownership still blocks it.
Approval-based and autonomous execution retain the same authorization boundaries.

This is not a relaxation of liquidation settlement. Existing four-axis `settled`
retains its meaning, including flatness. Claims, holdings and option structures
must not be released or declared closed just because a Python process finished.

## Phase A — recurring CNC portfolio and accounting

### Evaluation continuation

- Add a distinct server-derived continuation assessment and auditable outcome,
  separate from the full settlement rollup. Reuse the existing barrier, canonical
  attribution, cleanup and authority evidence; do not create a second ledger.
- Eligibility: finite attempt, normal completion, confirmed process/group cleanup,
  revoked child authority, current versioned proof of no unresolved execution,
  fresh and complete attributed book, no unexplained reconciliation divergence,
  no outstanding discretionary evaluation/approval, no active recovery action.
  Crashed, timed-out, uncertain or manually interrupted attempts are not silently
  cleared merely because they happen to have an open book.
- Proof is pinned to owner, hosted/canonical strategy, account, environment,
  predecessor attempt/epoch/run, barrier version, projection version and digest.
  Persist proof and continuation audit atomically with clearing the predecessor
  block. Revalidate under the existing strategy-row -> book lock order; concurrent
  fills, cleanup changes and duplicate continuation must lose safely.
- An open book is explicitly `held`, never flat/settled. Preserve instrument
  ownership, positions, cost history, option-run identities and protective policy.
  Never close a run if that silently disables standing protection. Establish the
  existing protection owner's continuity or refuse by name; in-flight protective
  exits remain blockers. Do not silently transfer risk authority to a child.
- Successful eligible completion is automatically processed by the host, and the
  shared Run now/scheduled-job path must be able to finish the proof after a host
  restart. Users must not click reconciliation after every healthy finite run.
  This automation does not approve the next evaluation's trades.
- Initial empty-book evidence must come from the canonical store/rebuild under
  its normal locking, not invented projection rows. Repeated evaluations must
  use production publication/rebuild paths, not harness-only seeding shortcuts.
- Reuse existing audit/proof storage if sufficient. If the invariant requires
  additive schema, add one new single-head migration and schema parity; no
  destructive/backfill shortcuts.

### Exposure and financing

- Strategy `allocation_inr` remains a strategy budget. Account available funds /
  margin remain a separate account constraint; never compare every strategy's
  reservation total against one strategy's budget or mix paper/live environments.
- Use canonical coordinates (instrument identity, product, environment) and
  per-instrument valid prices. Remove the single-reference-price valuation of
  unrelated positions. Unknown required valuation is a refusal, not zero.
- Derive desired post-plan quantities with exactly the same target/delta semantics
  the executors use. Include unchanged held coordinates when checking final gross,
  instrument limits and distinct open-instrument counts. Reapplying an unchanged
  target is a zero-order, zero-additional-capital operation.
- Distinguish current exposure, desired post-plan exposure, pending commitments,
  incremental funding and account availability in admission evidence. Do not
  count the same filled exposure through both a consumed reservation and a
  published position. Consumed-reservation history remains auditable.
- Rebalances must be able to sell removals and buy replacements without pretending
  unfilled sales have released cash. Reuse the existing domain-specific ordering;
  before any dependent buy, require confirmed sell outcomes and revalidate/claim
  actual available capacity under account/strategy locks. Partial/rejected/unknown
  sells cannot fund the full replacement. No new order before its financing is
  secured. Preserve live broker-margin checks and per-step authority checks.
- Admission, reservation and release must share environment/scope semantics and
  serialize competing plans. Two strategies cannot reserve the same actual account
  funds; one strategy cannot spend another strategy's allocated budget.
- Apply server allocation-basis validation to explicit exact-quantity proposal
  basis as well as weights; the child cannot silently size from a conflicting
  owner allocation. Missing explicit basis preserves existing governed behavior.

### Boundaries and acceptance

Primary files: backend/strategies/{reconciliation,reconciliation_service,repository,
settlement,admission,reservations,plan_pipeline,execution,live_adapter,scheduling,
proposals}.py and their necessary services/models; hosted lifecycle/operator
routes; frontend job/reconciliation labels; SDK only if a real contract addition
is required; the momentum example and its harness. Preserve unrelated drafts.

Required evidence: real API + supervisor children + disposable PostgreSQL, using
one persistent strategy for entry -> healthy completion -> next evaluation with
holdings -> unchanged target no-op -> remove/add rebalance -> breadth exit.
Include manual and autonomous paths, restart between evaluations, other strategy
holdings unchanged, duplicate Run now/scheduler race, stale authority, late fill,
unknown cleanup/book, partial sale and concurrent capacity claims. Preserve all
existing full-liquidation settlement regressions. Live-adapter tests use broker
fakes; no real broker order is part of this acceptance.

## Phase B — options continuity on the existing engine

- Inspect and reuse the durable option run, frozen legs, role-based entry/exit,
  margin, Greeks/chain and protection services. A new evaluation must discover
  its strategy's existing structures instead of accidentally entering duplicates.
- Entry replay is idempotent; ambiguous new entry over an existing matching
  structure is refused unless an explicit supported operation resolves it.
  Do not overload IDs or allow arbitrary child enter/exit to bypass proposals.
- A later evaluation may explicitly target an owned existing structure for a
  supported exit/adjustment via a new governed plan. Validate canonical strategy,
  account, environment and frozen leg identity across attempts. Preserve active
  structure protection; active execution still blocks conflicting operations.
- Test debit spread and neutral multi-leg structure entry/hold/exit; hedge fill
  gating, partial fill/rejection, restart, duplicate evaluation, stale chain/Greeks,
  insufficient margin and protective action races. Exercise supported adjustment
  paths and label any unsupported Greek-driven leg/quantity adjustment explicitly;
  full-structure exit is not proof of arbitrary delta hedging.

## Phase C — authenticated UI and reviewed deployment

- Use the user-authorized app login at their supplied LAN URL; credentials stay
  outside repository, screenshots, logs and reports. No fabricated auth cookies,
  no global auth bypass. Isolated browser QA may use its own disposable owner.
- Test paste/upload -> parameters -> paper Run now -> review-first approval or
  autonomous grant -> request/status/positions -> subsequent evaluation -> exit.
  Verify held-portfolio continuation wording and real failures are understandable.
- Root reviews each actual diff and evidence before integrating. Commit/deploy
  only the reviewed authorized scope, keeping existing live access unchanged.
  No push without applicable authorization; no real-money certification claimed.
- Report implementation, isolated paper, deployed UI, and real-broker evidence as
  separate statuses. A healthy deployment is not complete live certification.

## Progress

- [x] Source mapping and root contract.
- [x] Phase A implemented and reviewed.
- [ ] Phase B implemented/tested and reviewed.
- [ ] Phase C signed-in UI pass and deployment verification.

### First implementation review

Core continuation/admission/UI changes returned, not accepted. One consolidated
correction bundle sent to the same Flash worker: distinguish exit zero from failed
child exit; fence completion reporting; integrate reservation scope and dependent
rebalance financing; include untouched coordinates and projection completeness;
prove protection continuity; distinguish outstanding approvals from historical
approval records. Persistent supervised momentum sequence remains required.

Independent root deployed-browser evidence is in
`documents/verification/hosted-authenticated-ui-2026-09-24/README.md`: authorized
data-only paper lifecycle completed, reconciled, test strategy disabled, browser
closed. This does not certify the new continuation code or trading lanes.

### Phase A acceptance

Accepted after four correction rounds. The final source hashes match
`documents/hosted-recurring-portfolios-phase-a-report.md`; focused backend,
PostgreSQL, TypeScript, component, and supervised momentum evidence passed; the
manual sequence genuinely waited for live owner approvals; and the zero-free-cash
CNC rebalance funded its dependent buy only from confirmed account money. The
final combined evidence artifact is
`examples/hosted_platform/evidence/phase5-20260924T110544Z.json`.

This acceptance covers paper recurring CNC portfolios only. Staged live
financing refuses by name, standing protection continuity remains a bounded
refusal, and Phase B options continuity has not been accepted by this record.
