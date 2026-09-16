# Algo Worker and Python SDK Guide

This guide explains the worker model behind Kite Algo: why it exists, how it works, and how to think about building strategies on it.

For exact method signatures, request/response shapes, and reference examples, use [`../sdk/python/README.md`](../sdk/python/README.md).

## 1. Platform philosophy

Kite Algo is a trading platform, not a thin broker wrapper.

The core rule:

> Strategy code owns decisions. Kite Algo owns execution, attribution, grouped accounting, protection state, and journal-visible truth.

That rule keeps strategy workers small and replaceable while letting the platform keep the dangerous and stateful parts centralized.

### What the platform owns

- broker login, session lifecycle, and token management
- order attribution — every order is tagged to the correct run
- grouped accounting — run-level funds, P&L, exits
- execution routing — paper vs live, idempotency, error recovery
- protection — stoploss, target, basket-level, and worker-stale rules
- journaling — reviewable run history

### What your strategy owns

- market analysis, indicators, signal logic
- entry and exit decisions
- sizing and risk thresholds
- the sequence of intents you submit

### Why this separation matters

If every strategy had to own broker sessions, websocket reconnects, order tagging, and exit reconciliation, you would spend more time on plumbing than on alpha. Kite Algo absorbs that complexity so strategy code can stay focused.

```python
# Your strategy only needs to think about this
client.health()
run = client.create_run(strategy_run_id="run_001", template_id="my-strategy", ...)
order = equity_market_order("INFY", "BUY", 1)
client.place_order(run["strategy_run_id"], order, "run_001:entry:001")
```

## 2. Execution modes

Kite Algo supports three execution modes with a deliberate progression.

| Mode | What happens | When to use |
| --- | --- | --- |
| `dry_run` | Validates logic and payloads without any execution | First local development, CI smoke runs |
| `paper` | Durable backend-owned simulated orders, trades, P&L | Proving strategy behavior before live |
| `live` | Real broker orders through the backend's Kite session | After paper validation and explicit live acknowledgement |

### The progression rule

```text
dry_run  →  paper  →  explicit live validation  →  live
```

Never skip from `dry_run` to `live`. Use paper to prove grouping, risk patching, exits, and grouped P&L before any real capital is at risk.

### Live-mode gating

Live execution requires two things:
1. The worker token must explicitly allow `live`
2. The worker code must set `KITE_ALGO_ENABLE_LIVE=1`

This is an intentional double-gate. No worker accidentally places live orders.

## 3. The grouped accounting model

Brokers provide account-level truth — your total balance, total positions, total margin.

They do not tell you which strategy is using how much capital, or which P&L belongs to which run.

Kite Algo adds a grouped run model on top so every strategy becomes a first-class unit:

- its own `strategy_run_id`
- its own grouped orders and trades
- its own run-level funds usage
- its own run-level P&L
- its own exit and review path

### Why this matters in practice

Without grouped accounting, a trader running two strategies on the same broker account has no way to answer "is strategy A profitable?" without manual reconciliation.

With grouped accounting:

```python
# How much capital is Strategy A using?
run_funds = client.get_run_funds("strategy_a_v1")

# What is Strategy A's current P&L?
pnl = client.get_run_pnl("strategy_a_v1")
print(pnl["totals"]["net_pnl"])

# Is Strategy A flat?
# The backend tracks this through grouped orders/trades
```

This is what makes remote workers manageable instead of chaotic.

## 4. Lifecycle choices

Every strategy operates under one stable `strategy_run_id` per lifecycle.

Kite Algo supports two lifecycle styles — both valid, choose based on your needs.

### Raw-client style

Use this when you want full manual control: simple scripts, one-shot tasks, or when you prefer to wire every call yourself.

```python
client.health()
run = client.create_run(...)
client.place_order(run["strategy_run_id"], order, idempotency_key)
pnl = client.get_run_pnl(run["strategy_run_id"])
client.exit_run(run["strategy_run_id"])
```

You manage session, heartbeat, and safety calls explicitly.

### Managed-lifecycle style

Use this for longer-lived workers. `RunConfig` + `client.run(...)` + `ManagedRun` handle session plumbing while keeping trading decisions explicit.

```python
with client.run(config) as run:
    safety = run.safety_check()
    if safety.can_trade:
        run.place_order(order, idempotency_key=key, safety_token=safety.safety_token)
```

The managed lifecycle manages claim/heartbeat/release. It does not auto-trade or auto-exit.

### Which to choose

| If you want... | Use |
| --- | --- |
| Simplest mental model | Raw client |
| Session-aware long-running workers | Managed lifecycle |
| Full control over every HTTP call | Raw client |
| Less session plumbing | Managed lifecycle |

## 5. Safety and protection model

Safety checks are the gatekeeper for guarded trade actions. They are part of the modern worker story and should be used by any serious long-running worker.

### Safety check flow

1. Claim or manage the worker session
2. Call `safety_check()`
3. If `can_trade` is `false`, stop and inspect `blocking_reasons`
4. If a `safety_token` is returned, pass it into the next guarded action
5. If the token is rejected (expired), reacquire safety state — don't blindly retry

### Backend protection

Workers can register backend-owned exposure protection. When a declared rule triggers (stoploss, target, worker-stale), the backend executes a conservative attributed exit — without the worker needing to be running.

This means protection is not "worker implements stoploss logic." It is "worker declares thresholds, backend enforces them."

### Risk patching

Use `patch_risk(...)` to update thresholds at runtime without recreating the run. This is how strategies adjust stops, targets, and exposure controls as conditions change.

## 6. Options philosophy

Options workflows have their own namespace because option selection is inherently different from equity execution: you need session management, expiry chains, Greeks, strike resolution, and spread construction.

### Why the options namespace exists

Instead of every worker hand-coding option contract selection:

```python
# Before: manual option construction
order = {"exchange": "NFO", "tradingsymbol": "NIFTY26MAY25000CE", ...}
```

Use backend-backed resolution:

```python
# Now: platform-owned resolution
leg = resolve_offset_leg(client.options, underlying="NIFTY", product="MIS",
    expiry="current_week", option_type="CE", offset="ATM", transaction_type="BUY")
```

The resolver helpers construct `OptionExecutionLeg` payloads from backend option sessions. They do not create runs or place trades — you still own those decisions explicitly.

## 7. Observability model

Beyond orders and P&L, the platform provides worker-visible observability through timelines and health snapshots.

### Run timelines

Every strategy run has a timeline: a durable sequence of execution events, worker decisions, and protection state changes. This is backend-owned truth — you can read it, append decisions to it, and stream it, but you cannot alter non-decision events.

### Decision logging

Workers can explicitly log their decisions to the timeline:

```python
client.log_decision_event("run_001",
    event_type="signal.generated",
    summary="EMA crossover confirmed on INFY 5min",
    details={"symbol": "INFY", "fast_ema": 1450.1},
)
```

This strengthens the audit trail: the timeline shows both what the backend executed AND what the worker decided.

### Health snapshots

`get_run_health_snapshot(...)` gives operational health at a glance: heartbeat age, session status, recovery status, whether operator action is needed.

### P&L streams

`stream_run_pnl(...)` provides real-time SSE updates so workers can react to changing P&L without polling.

### Execution recovery

After submitting an intent, recover durable truth through order history,
basket/bracket state, and cursor-based execution events. Do not infer fills
from the original submission response. The SDK exposes
`get_order_history(...)`, `list_baskets(...)`, `get_basket(...)`,
`create_bracket(...)`, `list_brackets(...)`, `get_bracket(...)`,
`cancel_bracket(...)`, `list_execution_events(...)`, and
`stream_execution_events(...)`. `export_fundamentals_csv(...)` returns CSV
content as text for the caller to persist where appropriate.

The `AsyncKiteAlgoWorkerClient` exposes the same worker HTTP contract in 0.8.0;
its SSE helpers are consumed with `async for`. External adapters should depend
on this public SDK rather than backend-internal services.

## 8. Exit and recovery

### Exiting a run

Use `exit_run(...)` to close a grouped strategy. Always use this — never place ad-hoc exit orders manually.

```python
client.exit_run("run_001", reason="target reached", idempotency_key="run_001:exit:001")
```

For live runs, prefer `dry_run=True` first to preview the exit, then commit the real exit.

### Recovery after restart

If a worker process restarts:

1. `get_run(strategy_run_id)` — the backend still knows your run state
2. warm up historical candles and rebuild local indicator state
3. inspect `get_order_history(...)`, `list_baskets(...)`, and
   `list_execution_events(...)` from the last cursor
4. reconnect `stream_ticks(...)`, `stream_candles(...)`,
   `stream_run_pnl(...)`, or `stream_execution_events(...)`
5. resume your decision loop

The backend owns run identity and state. Worker restarts are a normal operational event, not a disaster.

### Session lifecycle

The managed lifecycle handles session plumbing for you. For raw-client flows, session management is explicit: `claim_session(...)` → work → `release_session(...)`.

A claimed session signals to the backend that a worker is actively operating on the run. Stale sessions trigger backend protection if configured.

## 9. Where to go for exact reference

This guide explains the model. For exact method signatures, field tables, request/response shapes, and runnable examples, see:

- [`../sdk/python/README.md`](../sdk/python/README.md) — the full SDK reference
- [`../sdk/python/examples/`](../sdk/python/examples/) — canonical scenario examples
- [`codebase-map.md`](codebase-map.md) — where worker features live in the codebase
- [`platform-overview.md`](platform-overview.md) — architecture and ownership boundaries

The SDK README is the single source of truth for install, methods, and release information.
