# Platform Overview

## What Kite Algo is

Kite Algo is a layered algorithmic trading platform for Zerodha/Kite workflows.

It combines a typed FastAPI backend, a separate Go market runtime, grouped strategy-run execution paths, durable paper execution, post-trade journaling, and a worker-safe Python SDK for external algorithms.

The architecture is designed so infrastructure absorbs operational complexity and strategy code stays focused on decisions.

## Core philosophy

### 1. Infrastructure should absorb complexity

Strategy code should not have to own broker login, websocket reconnect behavior, order attribution, grouped accounting, or review-state projection. Those concerns belong in shared infrastructure.

### 2. Strategy code should stay simple

The platform is strongest when strategies think in terms of market data, indicators, options structures, lifecycle, and explicit order intents rather than raw broker transport details.

### 3. Runtime ownership should be explicit

Market-data ownership, execution ownership, and post-trade ownership are intentionally separated so the system is easier to reason about and safer to evolve.

### 4. External workers should still live inside platform truth

Remote or isolated strategy workers should be able to use backend-owned execution, grouped funds, grouped P&L, and journaling without becoming their own parallel trading system.

## Layer map

| Layer | Why it exists | Main code |
| --- | --- | --- |
| App auth | Protect operator access before any broker-backed action | `auth_service.py`, `api/routers/auth.py` |
| Broker session ownership | Centralize Kite login and session truth | `broker_api/` |
| Typed API layer | Stable contracts for frontend and workers | `api/routers/` |
| Algo runtime | Run lifecycle, attribution, intent handling | `algo_runtime/` |
| Options core | Options sessions, strategy flows, protection, and execution helpers | `options/` |
| Paper runtime | Durable simulated execution | `paper_runtime/` |
| Execution accounting | Shared attribution and cost semantics | `execution_accounting/` |
| Journaling | Reviewable run and trade history | `journaling/` |
| Go market runtime | Websocket ownership and normalized tick fanout | `market-runtime/` |
| Worker SDK | Typed external worker interface | `sdk/python/` |
| Frontend | Operator UI and user workflows | `frontend-next/` |

## Main system flow

```text
Frontend / API client
        |
        v
FastAPI backend
        |
        +--> app auth
        +--> broker session ownership
        +--> typed route handlers
        +--> algo runtime / paper runtime
        +--> options sessions + protection
        +--> journaling / grouped reporting
        |
        v
Backend consumers read status + ticks from Redis
        ^
        |
Redis tick bus
        ^
        |
Go market runtime
```

The frontend and external workers both enter through the same FastAPI control plane. The Go market runtime owns the broker websocket connection and publishes normalized ticks through Redis. Backend consumers read ticks and status from Redis rather than opening their own broker websocket connections.

## Strategy lifecycle flow

```text
Create or recover strategy_run_id
        |
        v
Read market data and history
        |
        v
Build explicit order intent(s)
        |
        v
Backend validates + attributes the run
        |
        +--> dry_run path
        +--> paper runtime path
        +--> live broker path
        |
        v
Grouped orders / trades / positions / P&L
        |
        v
Journal, review, summaries, and exits
```

Every strategy run carries a stable `strategy_run_id`. The backend attaches attribution metadata to orders and trades so grouped P&L, funds usage, and journaling stay coherent even when the strategy restarts or moves between machines.

## Ownership boundaries

| Concern | Primary owner |
| --- | --- |
| Strategy decisions | Worker or strategy logic |
| Broker login/session | Backend |
| Websocket subscriptions and tick fanout | Go market runtime |
| Order attribution and grouped run identity | Backend |
| Paper execution truth | `paper_runtime/` |
| Live execution truth and recovery | Backend + broker session layer |
| Grouped funds / grouped P&L | Backend accounting model |
| Journaling and review | `journaling/` |

## Why grouped runs, funds, and attribution exist

Kite and most brokers provide account-level truth, not strategy-level truth.

Kite Algo adds a grouped run model so a strategy can be treated as a first-class unit:

- its own run lifecycle
- its own grouped orders and trades
- its own grouped funds usage and P&L
- its own exit and review path
- its own journaling context

That makes external workers practical without letting every worker invent its own tracking rules.

## Why the algo-worker + SDK layer matters

The worker model is a major platform advantage.

It allows strategy code to run outside the backend while still using platform-owned:

- market data
- grouped funds and grouped P&L
- execution attribution
- live/paper boundaries
- exits and journaling

For more, read [`algo-worker-sdk-guide.md`](algo-worker-sdk-guide.md).

## Maturity notes

| Area | Current state |
| --- | --- |
| Worker-safe API surface | Strong and actively expanding |
| Python SDK | Strong core surface with room for more helpers |
| Go market runtime | Built and still being hardened operationally |
| Journaling / review model | Built with ongoing UI and workflow refinement |
| Frontend coverage | Active and evolving |

The platform already has substantial depth around worker-safe execution, grouped accounting, journaling, and backend-owned system boundaries. Some layers are strong and reusable today; others are still actively evolving.

## Where to go next

- Read [`codebase-map.md`](codebase-map.md) to find the right folders quickly.
- Read [`algo-worker-sdk-guide.md`](algo-worker-sdk-guide.md) if you care about external strategy execution.
- Read [`kite-websocket.md`](kite-websocket.md) for the market-runtime subsystem.
