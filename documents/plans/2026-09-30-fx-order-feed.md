# Order Alerts That Do Not Depend on the WebSocket Feed — Plan

> **For agentic workers:** You are the IMPLEMENTER. Read `/home/krishna/kite-algo/AGENT_MEMORY.md` first, then `/home/krishna/kite-algo/AGENTS.md`. Do NOT load orchestration skills or route to other models. If a step does not fit the real code, stop and report file:line. At the end append your AGENT_MEMORY.md Log entry and update "Open gaps" item 1.

**Established facts (orchestrator, prod 2026-09-30):** two real ITC orders completed at 11:33 and 11:38 IST. `order_events` has ZERO rows ever. The app's `MarketDataRuntime.last_order_update_at` is null (GET /api/auth/session-status → runtime.websocket), i.e. nothing ever arrived on Redis `market:order_updates`. Go market-runtime: 1 shard, connected, no last_error, ticks flowing, same Redis URL and same Kite API key as the app. So Kite order updates never reach the Go ticker callback (or the Go library drops them silently). Meanwhile the platform DID learn the fill within ~7 s through REST polling (the live step went `filled` at 11:33:20; the order worker polls `kite.orders()` during market hours — find it: `ORDER_WORKER_*` / `order_loop_interval` in `backend/broker_api/orders/order_runtime.py:123`, and `backend/strategies/live_ingestion.py`).

## Task 1: Fire order alerts from the REST order path too
- Find the code that reads the broker order book by REST and records order status (order worker poll / live ingestion / `order_state_projection` writes). At the point where an order's broker status is observed, call the SAME `OrderAlertListener` (`backend/platform/order_alerts.py`) with the order payload shape it expects (order_id, status, transaction_type, tradingsymbol, exchange, filled_quantity, quantity, average_price, product, order_type, status_message, tag).
- Use ONE shared listener instance for both paths (WS and REST), so its `(order_id, status)` dedupe prevents double pushes. Put the instance on `app.state` (bootstrap already creates it) and pass it to the REST path; if the REST path runs in another process, create one there and rely on `alert_owner_nowait`'s key dedupe (`order:{id}:{status}`).
- Only alert for orders first seen today (IST trading day) so a restart does not re-push old fills; keep a bounded seen set as the listener already does.
- Tests: the REST path with a fake broker order book showing a COMPLETE order → one alert; the same order seen again (REST twice, or WS then REST) → still one alert; an old (yesterday) order → no alert.

## Task 2: Make the WS gap observable (Go)
- `market-runtime/internal/service/shard.go` `handleError`: also `log.Printf` the error with the shard id (rate-limit to once per 10 s per shard).
- In the OnOrderUpdate callback and `Service.handleOrderUpdate`: log one line per order update received and published (`order_id`, `status`, shard) so the next live order shows whether Kite sent it.
- Check gokiteconnect v4.4.0 `ticker` source in the Go module cache (`go env GOMODCACHE`) for how text "order" messages are parsed and whether errors are surfaced; report what you find in one paragraph (e.g. does Kite need a specific subscription, does the lib require `OnOrderUpdate` before `Serve`).
- `go test ./internal/service/...` (known pre-existing failure: `TestServiceStatusExhaustedTakesPrecedence`).

## Final run
`tests/platform -q`, `tests/broker_api -q`, the live ingestion tests you touched, and the Go tests. Final report per AGENTS.md.
