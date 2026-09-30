# Live Send-Time Admission Double-Counts Its Own Reservation — Fix Plan

> **For agentic workers:** You are the IMPLEMENTER. Do NOT load astra_flash-orchestrator or any orchestration/delegation skill; do not route to other models; implement directly. Read `/home/krishna/kite-algo/AGENTS.md`. If a step does not fit the real code, stop and report the mismatch (file:line) instead of improvising.

**Incident (prod 2026-09-30 10:15 IST):** live request `828bf45f-…` for 1 ITC (₹265.65) was refused at send: `LIVE_ADMISSION_REFUSED`, stage execution, `active_reserved_inr: 265.65`, `projected_gross_inr: 531.3` > `gross_limit_inr: 500`. The only active reservation was THIS plan's own reservation (made at reserve time). At send, `backend/strategies/live_adapter.py:~1330` re-runs `self.admission.evaluate(plan, execution_environment="live", …)`, and `AdmissionService.evaluate` (`backend/strategies/admission.py:1054`) → `capacity_held_inr` (:1161) → `financing.capacity_held` (`backend/strategies/financing.py:455`) sums every active/renewed/action_required/consumed-unpublished reservation for the strategy, including the plan's own. The plan's requirement is then added on top → counted twice. Every live order above half its limit is refused.

## Constraints
- Worktree `/home/krishna/kite-algo-worktrees/fx-live-double-count`, branch `codex/fx-live-double-count`. Never touch `.env*`. Do NOT commit. No restarts.
- Python: `/home/krishna/kite-algo/.venv/bin/python -m pytest …` from the worktree root. PG only 127.0.0.1:15433.
- Reserve-time admission must NOT change (there the plan has no reservation yet; it must still count every other reservation).

## Task 1: Exclude the plan's own reservation at send time
- `financing.capacity_held(...)` and `financing.account_capacity_held_inr(...)` (if the account-level sum is also used by evaluate — check) get an optional `exclude_plan_id: Optional[str] = None`; when set, reservations with that `plan_id` are left out of every sum.
- `AdmissionService.capacity_held_inr(...)` and `AdmissionService.evaluate(...)` get the same optional keyword (`exclude_plan_id=None`), passed through to every capacity sum evaluate performs.
- `live_adapter.py` send-time call passes `exclude_plan_id=str(plan.get("plan_id") or "")`. No other caller changes. Report every call site of `evaluate` and `capacity_held` you found and whether it changed.
- Tests (extend `tests/strategies/test_admission.py` and the closest live-adapter test file, e.g. `tests/strategies/test_live_lane_gate.py`): (a) a plan with its own active ₹265.65 reservation and gross limit ₹500 is ADMITTED at send when `exclude_plan_id` is its plan_id; (b) the same without exclusion is refused (documents today's behaviour); (c) another plan's reservation is still counted with exclusion set; (d) the live adapter passes the plan id (assert via a fake admission capturing kwargs).

## Task 2: Final run
- `tests/strategies/test_admission.py tests/strategies/test_live_lane_gate.py tests/strategies/test_reservations.py -q`, then `tests/strategies -q` (known pre-existing: 7 failures in `test_execution_dispatcher.py`, "no current event loop"), and `ADMISSION_PG_URL=postgresql://postgres:testonly@127.0.0.1:15433/kite_test … tests/integration/test_admission_approvals_postgres.py -q`.
- Final report per AGENTS.md.
