# Live MARKET Orders: Market Protection + Definite Kite Rejections — Fix Plan

> **For agentic workers:** You are the IMPLEMENTER. Do NOT load astra_flash-orchestrator or any orchestration/delegation skill; do not route to other models; implement directly. Read `/home/krishna/kite-algo/AGENTS.md`. HIGH-RISK live execution path: run the whole `tests/strategies/` and `tests/broker_api/` directories at the end. If a step does not fit the real code, stop and report the mismatch (file:line).

**Incident (prod 2026-09-30 10:46 IST):** the first hosted live order (1 ITC MIS MARKET, intent `lint_ae3610ec022a4b2faa255e606b048fe2`, client ref `KA6B02F1BF`, plan `8ccd5ab5-2d96-4772-9e05-936d3f34abe7`) was refused by Kite at `backend/broker_api/orders/service.py:374 place_order`:
`kiteconnect.exceptions.InputException: Market orders without market protection are not allowed via API. Please set market protection or use a Limit order.`
Kite created no order (verified via kite.orders(): no ITC order; no position). But the platform recorded the step as uncertain: request `f2449eb0-…` → `dispatch_unresolved / TRANSPORT_UNCERTAIN`, job `recovery_required`, intent `failed`.

**Facts:**
- `PlaceOrderRequest.market_protection: Optional[int]` exists (`backend/broker_api/orders/models.py:60`, validated -1..100). kiteconnect `place_order(..., market_protection=None)` supports it.
- Protection exits already set it: `backend/options/protection/exit_builder.py:41-43` (`-1` = Kite automatic protection when not given).
- The hosted live path builds MARKET orders without it.

## Constraints
- Worktree `/home/krishna/kite-algo-worktrees/fx-market-protection`, branch `codex/fx-market-protection`. Never touch `.env*`. Do NOT commit. No restarts. NEVER call the real broker in tests.
- Python: `/home/krishna/kite-algo/.venv/bin/python -m pytest …` from the worktree root.

## Task 1: Default market protection at the broker boundary
- In `OrderService.place_order` (`backend/broker_api/orders/service.py:~286-380`), before building the kite params: if `order_type` is MARKET or SL-M and `market_protection` is None, set it to the value of env `KITE_DEFAULT_MARKET_PROTECTION` (default `-1`, parsed as int, validated -1..100; invalid → -1 with a warning). Explicit values are kept as given. LIMIT/SL untouched.
- Make sure the value actually reaches BOTH send paths: `kite.place_order(variety=..., **params)` and the autoslice `kite._post("order.place", params=params)` branch.
- Check whether modify-order or basket/GTT paths also send MARKET orders to Kite (grep `kite.place_order`, `order.place`, `place_gtt`, `modify_order` across backend/) and report each; apply the same default only where it is a plain MARKET/SL-M placement.
- Tests (extend the test file for `backend/broker_api/orders/service.py`, find it under tests/broker_api/): MARKET without protection → kite called with `market_protection=-1`; explicit 5 kept; LIMIT → not set; env override 2 applied; autoslice path carries it.

## Task 2: A definite Kite input rejection is a rejection, not uncertain
- Trace how an exception from `place_order` becomes the live step state `uncertain` (`backend/strategies/live_adapter.py` submission path; `_STEP_REASON` :3412; `execution_requests.py:995` TRANSPORT_UNCERTAIN). Report the exact lines.
- Map ONLY `kiteconnect.exceptions.InputException` raised by the place call (Kite validation, HTTP 400: the order was never created) to the definite `rejected` state with the broker message as the reason (outcome refusal code `LIVE_ORDER_REJECTED` or the existing rejected mapping). Every other exception (NetworkException, timeouts, 5xx, DataException, GeneralException, unknown) stays `uncertain` — do not widen.
- If the exception type is lost before the adapter sees it (e.g. wrapped by `run_kite_write_action`), carry the type name or a `definite_rejection: True` flag through explicitly; do not string-match the message.
- A rejected entry must also release its reservation (the refusal/rejection path already added in commit e59a3a6 for pre-submission refusals may or may not cover it — check `live_sequence.py` settlement: all legs terminal + no fill → released) and let the job end normally (not `recovery_required`) if the platform treats a rejected entry that way elsewhere. Report what happens.
- Tests: InputException on submit → step `rejected`, request refused (not dispatch_unresolved), reservation released; NetworkException → still uncertain.

## Task 3: Report only (no code): the current stuck incident
- Read-only: how does the platform resolve the existing uncertain step for plan `8ccd5ab5-…` (repair/resolve route, or ingestion matching the client ref `KA6B02F1BF` against the broker order book)? Give the exact operator route(s) and body to resolve it as "no broker order exists", and to reconcile job `hsj_eb4d3b81f30640bab14aa3530aea1924`. Do not call them.

## Final run
`tests/broker_api -q`, `tests/strategies -q` (known pre-existing: 7 failures in test_execution_dispatcher.py "no current event loop"), plus any options/protection test file that uses exit_builder. Final report per AGENTS.md.
