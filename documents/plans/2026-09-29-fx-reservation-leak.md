# Leaked Reservations After Pre-Broker Refusal — Implementation Plan

> **For agentic workers:** Execute task-by-task, in order. Do not load brainstorming or other workflow skills. AGENTS.md is not tracked in git: read `/home/krishna/kite-algo/AGENTS.md`. This is a HIGH-RISK money path (reservations/admission): run the whole `tests/strategies/` directory at the end. If a step does not fit the real code, stop and report the mismatch (file:line, what you saw) instead of improvising.

**Incident (prod, 2026-09-28/29):** hosted live request `1c1780e4-…` (plan `aa4ce926-74a6-4a22-90ae-2bfc8b5ea2f0`) was reserved (`strategy_reservations` `c967af08-9154-48a4-bedd-b39ee98e0c9b`, ₹266, status `active`), then refused `LIVE_QUOTE_MISSING` at execution BEFORE any broker submission (0 `live_plan_submissions`, 0 `live_order_intents`). The reservation was never released or expired. Its `valid_until` lapsed 2026-09-28 09:48 UTC, but it still counts in `financing.capacity_held` (`PENDING_COMMITMENT_STATUSES` includes `active`, no validity check, `backend/strategies/financing.py:455-530`). The next day's ₹263.95 entry was refused `ADMISSION_REFUSED / GROSS_NOTIONAL_EXCEEDED` (projected 529.95 > 500).

**Facts:**
- `ReservationLedger.release(reservation_id, reason=…)` (`backend/strategies/reservations.py:732`) already refuses consumed or `advanced` reservations (`ReleaseForbidden`); only `UNSTARTED_STATUSES = ("active","renewed")` (:52) can be released.
- `ReservationLedger.expire(…)` (:784) expires lapsed unstarted reservations — **nothing calls it** (grep).
- Live reservations settle only when every leg is terminal (`backend/strategies/live_sequence.py:~1446-1568`: consumed if any fill, else released). A refusal before submission never reaches that code.
- Paper executor settles in `backend/strategies/execution.py:~3340-3378`: "release requires proof, never a guess".

## Global Constraints
- Worktree `/home/krishna/kite-algo-worktrees/fx-reservation-leak`, branch `codex/fx-reservation-leak`. Never touch `.env*`. Do NOT commit. No container restarts. Prod DB read-only if you look (`docker exec kite-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT …"'`).
- Python: `/home/krishna/kite-algo/.venv/bin/python -m pytest …` from the worktree root. PG tests only on `127.0.0.1:15433`.
- Never release capacity without PROOF that no broker/paper work exists for the reservation's plan. When in doubt, hold (current behaviour).

## Task 1: Release on refusal before submission (live)
- Find where a live plan execution ends as a refusal before any submission: the dispatcher/executor path that turns `LiveRefusal` / `LiveEvidenceUnavailable` (live_adapter.py:1260, :2129; live_readers.py) or an execute-time admission refusal into a `refused` execution-request finish (`backend/strategies/execution_requests.py:1253 finish`, `backend/strategies/execution_dispatcher.py`, `backend/strategies/live_service.py`). Report the exact call site you chose.
- At that point, if the plan has a reservation AND there are zero `live_plan_submissions` rows for the plan_id (proof no broker work began), call `ledger.release(reservation_id, reason="refused_before_submission", actor_id="system")`. Catch `ReleaseForbidden` / `ReservationStateError` → log at info and keep the refusal outcome unchanged. Never raise out of the refusal path.
- Paper: check whether a paper plan whose every order was REJECTED by the paper runtime (e.g. "No reference price") leaves its reservation `active` (execution.py:3340-3378: `submitted_any` / `failed` logic). Report yes/no with the line. If yes and all its paper orders are `rejected`, release with the same reason (proof = every order terminal-rejected, none filled). If the logic is ambiguous, do NOT change paper; report.
- Tests (extend the existing test files for these modules): a live refusal before submission releases the reservation (status `released`, event recorded, reason); a refusal when a submission row exists does NOT release; a consumed/advanced reservation is never released (ReleaseForbidden swallowed, refusal unchanged).

## Task 2: Proof-gated sweep of lapsed, never-started reservations
- Add `ReservationLedger.expire_lapsed(*, account_id, strategy_id, execution_environment, now=None) -> list[str]`: for reservations of that scope in `UNSTARTED_STATUSES` with `valid_until < now`, no `advanced` event, AND zero `live_plan_submissions` rows for their plan_id AND zero paper orders for their plan_id (find the paper orders table/attribution key used by `execution.py`'s paper path), call `self.expire(...)`; skip (and log at info) any that fail the proof. Return the expired ids.
- Call it at the start of admission for that strategy/environment, before `capacity_held_inr` (`backend/strategies/admission.py:~1127`), wrapped so a sweep error never changes admission (log a warning and continue with current behaviour).
- Tests: lapsed + no work → expired and no longer counted in `capacity_held`; lapsed but a `live_plan_submissions` row exists → kept; not yet lapsed → kept; `advanced` → kept. Include one PostgreSQL integration test in the closest existing `tests/integration/*reservation*_postgres.py` or `*admission*_postgres.py` file (see its header for the env var; server 127.0.0.1:15433).

## Task 3: Docs + final run
- `documents/kite-algo-platform-reference.md` reservations/admission section: release on refusal-before-submission; lapsed never-started reservations expire at admission (proof rule).
- `documents/runbooks/README.md`: under recovery, add the remedy for a live job refused before any broker call that is stuck `recovery_required` with `EVIDENCE_UNAVAILABLE / live_attribution_unpublished`: `POST /api/strategies/{id}/positions/rebuild?environment=live` (publishes the empty book, no broker call), then the job's reconciliation POST.
- Run: `tests/strategies -q`, the touched integration file(s) with their env vars, and `tests/api/test_hosted_execution_requests.py -q`. Report pre-existing failures separately (verify each against clean `development`).
- Final report per AGENTS.md, including the exact call sites and the paper yes/no finding.
