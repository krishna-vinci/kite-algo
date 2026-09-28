# Live Order ntfy Alerts Implementation Plan

> **For agentic workers:** Execute task-by-task, in order. Do not load brainstorming or other workflow skills. AGENTS.md is not tracked in git: read `/home/krishna/kite-algo/AGENTS.md`. If a step does not fit the real code, stop and report the mismatch (file:line, what you saw) instead of improvising.

**Goal:** The owner gets an ntfy push for every live broker order outcome: filled, rejected, lapsed, or cancelled after a partial fill. Paper orders never alert (they never reach the broker).

**Architecture:** One listener in finance-app on `MarketDataRuntime.add_order_update_listener` (`backend/broker_api/orders/market_runtime_client.py:250`), which receives every normalized Kite order update (`_normalize_order_update_payload`, :547) for all live orders — strategy, manual and protection exits. The listener is synchronous and O(1): it decides, formats and calls `alert_owner_nowait` (`backend/platform/owner_alerts.py`), which is non-blocking and never raises. Registered in `backend/app/bootstrap.py` right after `app.state.market_data_runtime = market_data_runtime` (:482); unsubscribed on shutdown the same way other runtime listeners are cleaned up (find the shutdown path; if none exists for listeners, keep the unsubscribe callable on `app.state` and call it where `market_data_runtime` is stopped).

## Global Constraints

- Worktree `/home/krishna/kite-algo-worktrees/fx-order-alerts`, branch `codex/fx-order-alerts`. Never touch `.env*`. Do NOT commit.
- Python: `/home/krishna/kite-algo/.venv/bin/python -m pytest ...` from the worktree root.
- The listener must never raise into the feed and never block (no I/O on the calling thread).

## Decisions (fixed)

1. Alert on status `COMPLETE`, `REJECTED`, `LAPSED`; on `CANCELLED`/`CANCELED` only when `filled_quantity > 0` (partial fill then cancel). Every other status (OPEN, UPDATE, TRIGGER PENDING, plain cancels of unfilled orders) is ignored.
2. Dedupe: bounded in-memory set of `(order_id, status)` (max 5000, drop oldest) — Kite can resend the same terminal update. Also pass `key=f"order:{order_id}:{status}"` to `alert_owner_nowait`.
3. Env `ORDER_ALERTS_ENABLED` (default `true`; `0/false/no/off` disables). Read once at listener construction.
4. Messages (title / message / tags):
   - COMPLETE: title `Filled: BUY 1 ITC` ; message `NSE ITC BUY 1 @ 412.30 (MIS, LIMIT) order 2409...` ; tags `["white_check_mark"]`. Use `filled_quantity` and `average_price` (2 decimals).
   - REJECTED: title `Order REJECTED: SELL 50 NIFTY...` ; message includes `status_message` (or `status_message_raw`) ; tags `["rotating_light"]`.
   - LAPSED: title `Order LAPSED: ...` ; tags `["warning"]`.
   - partial CANCELLED: title `Partial fill then cancelled: ...` ; message `filled X of Y @ avg` ; tags `["warning"]`.
   - Include `tag` (strategy order tag) in the message when present.

## Tasks

### Task 1: `backend/platform/order_alerts.py`
- `class OrderAlertListener` with `__init__(self, alert=alert_owner_nowait, max_seen=5000)` and `__call__(self, payload: dict) -> None` (callable used directly as the listener). Pure formatting helper `format_order_alert(payload) -> tuple[title, message, tags] | None`.
- Tests `tests/platform/test_order_alerts.py` (fake `alert` recording calls): COMPLETE alerts once with qty/avg price; duplicate COMPLETE for the same order_id alerts once; REJECTED includes the reason; unfilled CANCELLED and OPEN do not alert; partial CANCELLED alerts; disabled via env alerts nothing; a malformed payload (missing fields / None) does not raise.

### Task 2: Wire in bootstrap
- Register after :482 when `ORDER_ALERTS_ENABLED`; store the unsubscribe; call it on shutdown.
- Test: extend the closest existing bootstrap/startup test only if one already constructs the runtime with fakes; otherwise skip and report (Task 1 covers the behaviour).

### Task 3: Docs + final run
- `documents/kite-algo-platform-reference.md`: owner alerts section — live order outcome pushes (statuses, env flag).
- Run `tests/platform -q` and `tests/broker_api/test_market_runtime_client.py -q` → PASS. Final report per AGENTS.md.
