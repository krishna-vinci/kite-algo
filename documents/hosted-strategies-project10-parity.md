# Hosted strategies — Project 10 parity matrix (option structures, fill gating, expiry, settled — G8 — FINAL PHASE)

Status 2026-09-17/18, branch `development`. Plan: `docs/superpowers/plans/2026-09-17-options-structures.md` (D-1…D-8 binding). Two delegations: main Tasks 1–9 + a two-residual closing pass.

## Delivered
- Migration `20260917_000034` (additive; the planned CHECK widening was a **no-op — no status CHECK exists on option_run_states**; `settled` landed as `OptionRunStatus.SETTLED` + adapter registration, recorded in the migration docstring): `structure_digest` + `expiry_policy` columns, `option_settlement_evidence` (insert-only trigger; `evidence_source` constrained to broker_ledger|contract_note|exchange_file — "expiry time alone adjusts nothing" is a STRUCTURAL refusal, DB-verified).
- **Chain threading (D-1):** `option_structure` compiler resolves every leg against the PINNED catalog generation (strike/option_type now in the pinned read) via the selection policy; legs freeze into the immutable plan, never re-resolved; chain data feeds evaluation metrics only; refusals OPTION_LEG_UNRESOLVED / EXPIRY_UNAVAILABLE / SELECTION_POLICY_UNRESOLVABLE; plan→option-run handoff with attribution (one engine, no second lifecycle).
- **Vocabulary bridge (gap C part 1):** `backend/options/protection/bridge.py` — the one bidirectional adapter; compiler MetricKind appears outside options/strategy/ for the first time.
- **Hedge fill gating (gaps D/E):** `hedge_gate.py` — confirmed-fill quantities gate dependent shorts; proportional release ceiling (floors); rejection/timeout (OPTION_HEDGE_FILL_TIMEOUT_SECONDS default 30) ⇒ no release + action_required; protection during partial entry blocks new legs, may exit current legs.
- **Structure-aware exit builder (gaps C/G):** `build_structure_exit_orders` — short liabilities first, hedges released only against closure proof; closure key = tradingsymbol (what a caller actually reads).
- **Gap C fully closed:** `StructureIdentity` on `BackendProtectionConfig` (declared optional field — the model is extra="forbid"); the enforced runtime's structure-aware branch submits the evaluator's recommended exits through the durable claim path (evaluation → trigger → claim → submission → outcome); non-structure runs pinned byte-for-byte unchanged; fail-closed `seam_failed` evidence when the seam is unavailable.
- **Expiry policy + settled (gap H):** cutoff warnings/escalation (OPTIONS_EXPIRY_WARNING_DAYS); cash settlement ONLY with authoritative evidence (structural refusal otherwise); `settled` terminal state registered as a Phase 5 domain adapter; MIS options via the Phase 8 platform schedule.
- Owner settlement surfaces (owner + account authorization).

## Test evidence (independently re-run by the orchestrator)
- Unit: 16 compiler + 16 bridge + 18 hedge gate + 13 exit builder + 20 expiry + 13 runtime (3 new wiring tests incl. the non-structure regression and fail-closed seam); **701+1 targeted sweep; 3× stable full-sweep runs byte-identical (26 pre-existing tests/options failures — the documented baseline)**.
- **PG:** `OPTIONS_PG_URL=…` → **15 passed**: head + upgrade-from-prior-head; walkthroughs 4/5/6 (naked admission; iron condor 50% hedge fill + rejected leg → proportional ceiling + non-release + action_required; index cash settlement with evidence → settled, expiry-time-alone adjusts nothing); protection during partial entry; MIS square-off of options. Skip proven without URL.
- `tests/api` baseline identical (20); single head `…000034`; `git diff --check` clean; ONE options engine; both paper-only boundaries intact.
- Closing pass: gap-C wiring red-first (0 != 1 before the call site existed); the flaky settlement test's real cause (process-wide domain-adapter registry leak between suites) found and fixed in place with exact reproduction — the orchestrator's wall-clock diagnosis was wrong and the agent's correction was verified.

## Limitations / NOT PROVEN
Live options remain blocked (both paper-only boundaries intact) pending separate live-market certification; real broker/exchange settlement evidence flows unproven; no atomic multi-leg claims (permanent design).

## Gate summary
Roadmap Project 10 acceptance evidence Closed (walkthroughs 4–6, protection during partial entry, MIS square-off). Phase gate: **PASSED**. CAMPAIGN COMPLETE.
