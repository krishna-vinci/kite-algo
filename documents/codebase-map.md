# Codebase Map

This document helps contributors answer three questions quickly:

1. Which folder owns the behavior I want to change?
2. Which surrounding subsystem should I understand first?
3. Where should I start reading code?

## Top-level map

| Path | Ownership |
| --- | --- |
| `main.py` | FastAPI app wiring, router registration, startup/runtime hooks |
| `app/` | Bootstrap, middleware, auth helpers, database wiring, monitoring, and schedulers |
| `api/` | Public backend contracts plus grouped config, repositories, schemas, and services |
| `broker_api/` | Broker integration, sessions, orders, instruments, timeline, and market helpers |
| `algo_runtime/` | Strategy lifecycle, attribution, intent flow, runtime state |
| `options/` | Options sessions, strategy flows, protection, execution helpers, worker-safe option APIs |
| `paper_runtime/` | Durable simulated orders, trades, positions, funds, execution state |
| `execution_accounting/` | Shared order attribution, grouped-funds, and execution-cost contracts |
| `journaling/` | Journal services, metrics, summaries, and review-oriented state |
| `market-runtime/` | Go runtime for websocket ownership, instrument serving, and tick fanout |
| `sdk/python/` | Worker SDK, models, helpers, examples |
| `frontend-next/` | Next.js frontend app, components, tests |
| `strategies/` | Strategy implementations and strategy-oriented modules |
| `documents/` | Public architecture and onboarding docs |
| `tests/` | Backend/runtime verification suites |

## Backend API structure

### Route composition

FastAPI route registration starts in:

- `main.py`
- `api/routers/__init__.py`

That router index auto-composes the main app-facing and worker-facing route groups.

### Core route families

| Path | What it contains |
| --- | --- |
| `api/routers/auth.py` | App auth and broker-login-related routes |
| `api/routers/control.py` | Strategy/control-plane actions |
| `api/routers/journal.py` | Journal and review endpoints |
| `api/routers/analytics.py` | Analytics/reporting surfaces |
| `api/routers/market_data.py` | Market and quote surfaces |
| `api/routers/marketwatch.py` | Marketwatch and realtime streaming endpoints |
| `api/routers/historical.py` | Historical market-data endpoints |
| `api/routers/instruments.py` | Instrument discovery and search |
| `api/routers/ingestion.py` | Data ingestion and background import routes |
| `api/routers/user_settings.py` | User preferences and settings |

### Worker route families

| Path | What it contains |
| --- | --- |
| `api/routers/worker_auth.py` | Worker auth/session claim/release, liveness |
| `api/routers/worker_market.py` | Worker market-data and worker-safe market surfaces |
| `api/routers/worker_execution.py` | Worker runs, intents, timelines, decision logging, GTT, run health |
| `api/routers/worker_protection.py` | Worker safety/protection and guardrail endpoints |
| `api/routers/worker_shared.py` | Shared worker helpers used across route families |
| `options/api/worker_options_router.py` | Worker-safe options namespace under `/api/algo-workers/worker/options/*` |

If you are changing API behavior, start by reading the router file, then the service or runtime layer it calls.

## Supporting backend structure

| Path | Purpose |
| --- | --- |
| `api/services/` | Cross-router business logic, orchestration helpers, response shaping |
| `api/repositories/` | Shared persistence-oriented helpers used by routes/services |
| `api/schemas/` | Request/response schema helpers and typed payload modules |
| `broker_api/orders/` | Broker order routes, models, runtime helpers |
| `broker_api/session/` | Broker session ownership and login lifecycle |
| `broker_api/instruments/` | Instrument-serving routes and helpers |
| `broker_api/market/` | Candle/history/market-adjacent broker services |
| `broker_api/timeline/` | Timeline-adjacent broker/runtime integration helpers |

## Runtime ownership map

| Concern | Start here |
| --- | --- |
| Worker run lifecycle | `api/routers/worker_*.py`, `algo_runtime/` |
| Worker decision/timeline observability | `api/routers/worker_execution.py`, `broker_api/timeline/`, `algo_runtime/` |
| Worker options flows | `options/api/worker_options_router.py`, `options/`, `sdk/python/kite_algo_worker/options/` |
| Execution attribution | `algo_runtime/`, `execution_accounting/` |
| Paper execution behavior | `paper_runtime/` |
| Live broker order flow | `broker_api/`, live order intent paths, accounting notes in `documents/live-paper-accounting-and-worker-live-execution.md` |
| Tick ownership and fanout | `market-runtime/cmd/market-runtime/main.go`, `market-runtime/internal/` |
| Instrument search/serving | `broker_api/instruments/`, `market-runtime/internal/` |
| Journal projection and summaries | `journaling/service.py`, `journaling/runtime.py`, `journaling/repositories/`, `journaling/services/` |

## Frontend map

| Path | What to look for |
| --- | --- |
| `frontend-next/app/` | Routes and page-level app entry points |
| `frontend-next/components/` | Shared UI components |
| `frontend-next/features/` | Feature-oriented frontend logic |
| `frontend-next/hooks/` | Reusable hooks |
| `frontend-next/lib/` | API helpers, utilities, types |
| `frontend-next/tests/` | Frontend tests |

## Worker SDK map

| Path | Purpose |
| --- | --- |
| `sdk/python/kite_algo_worker/client.py` | Main sync client and worker run lifecycle helpers |
| `sdk/python/kite_algo_worker/managed_run.py` | Managed lifecycle wrapper for session-aware workers |
| `sdk/python/kite_algo_worker/options/` | Worker-safe options namespace and resolver helpers |
| `sdk/python/kite_algo_worker/helpers.py` | Small explicit helper functions and polling utilities |
| `sdk/python/examples/` | Canonical SDK examples |
| `sdk/python/README.md` | SDK installation, release, and usage reference |

## If you want to change X, start here

| Change type | First files to read |
| --- | --- |
| Add or change a backend endpoint | `api/routers/<area>.py`, then the called service/runtime module |
| Change worker lifecycle behavior | `api/routers/worker_*.py`, `algo_runtime/` |
| Improve paper execution correctness | `paper_runtime/`, `execution_accounting/`, related tests |
| Improve grouped funds/P&L behavior | `execution_accounting/`, `paper_runtime/`, `algo_runtime/`, journaling summaries |
| Improve options workflows | `options/`, `options/api/worker_options_router.py`, worker SDK option helpers |
| Improve journal/review workflows | `journaling/`, `api/routers/journal.py`, frontend journal pages |
| Improve market-data ownership | `market-runtime/`, `broker_api/instruments/`, `documents/kite-websocket.md` |
| Improve SDK ergonomics | `sdk/python/kite_algo_worker/`, `sdk/python/examples/`, worker route contracts |
| Improve frontend workflows | `frontend-next/` plus the backend routes they depend on |

## Reading order for new contributors

1. [`platform-overview.md`](platform-overview.md)
2. This file
3. [`algo-worker-sdk-guide.md`](algo-worker-sdk-guide.md) if you care about workers or SDKs
4. [`../CONTRIBUTING.md`](../CONTRIBUTING.md) for local workflow and checks
5. The specific router/service/runtime files for the subsystem you want to change
