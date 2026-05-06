# Codebase Map

This document helps contributors answer three questions quickly:

1. Which folder owns the behavior I want to change?
2. Which surrounding subsystem should I understand first?
3. Where should I start reading code?

## Top-level map

| Path | Ownership |
| --- | --- |
| `main.py` | FastAPI app wiring, router registration, startup/runtime hooks |
| `api/` | Public backend contracts, route handlers, API composition |
| `broker_api/` | Broker integration, sessions, market/broker service helpers |
| `algo_runtime/` | Strategy lifecycle, attribution, intent flow, runtime state |
| `options/` | Options sessions, strategy flows, protection, and execution helpers |
| `paper_runtime/` | Durable simulated orders, trades, positions, funds, execution state |
| `execution_accounting/` | Shared order attribution and execution-cost contracts |
| `journaling/` | Journal services, metrics, summaries, review-oriented state |
| `market-runtime/` | Go runtime for websocket ownership and tick fanout |
| `sdk/python/` | Worker SDK, models, examples |
| `frontend-next/` | Next.js frontend app, components, tests |
| `strategies/` | Strategy implementations and strategy-oriented modules |
| `documents/` | Public architecture and onboarding docs |
| `tests/` | Backend/runtime verification suites |

## Backend API structure

| Path | What it contains |
| --- | --- |
| `api/routers/auth.py` | App auth and broker-login-related routes |
| `api/routers/algo_workers.py` | Worker lifecycle, worker execution, worker-safe flows |
| `api/routers/control.py` | Strategy/control-plane actions |
| `api/routers/journal.py` | Journal and review endpoints |
| `api/routers/market_data.py` | Market and quote surfaces |
| `api/routers/marketwatch.py` | Marketwatch and realtime streaming endpoints |
| `api/routers/historical.py` | Historical market-data endpoints |
| `api/routers/instruments.py` | Instrument discovery and search |
| `api/routers/ingestion.py` | Data ingestion and background import routes |
| `api/routers/user_settings.py` | User preferences and settings |

If you are changing API behavior, start by reading the router file, then the service or runtime layer it calls.

## Runtime ownership map

| Concern | Start here |
| --- | --- |
| Worker run lifecycle | `api/routers/algo_workers.py`, `algo_runtime/` |
| Execution attribution | `algo_runtime/`, `execution_accounting/` |
| Options sessions and protection | `options/`, worker options endpoints, frontend options pages |
| Paper execution behavior | `paper_runtime/service.py`, `paper_runtime/executor.py`, `paper_runtime/run_state.py` |
| Live broker order flow | `broker_api/`, live order intent paths, accounting notes in `documents/live-paper-accounting-and-worker-live-execution.md` |
| Tick ownership and fanout | `market-runtime/cmd/market-runtime/main.go`, `market-runtime/internal/service/` |
| Journal projection and summaries | `journaling/service.py`, `journaling/runtime.py` |

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
| `sdk/python/kite_algo_worker/` | SDK client, models, helpers |
| `sdk/python/examples/` | Worker usage examples |
| `sdk/python/README.md` | SDK installation and usage |

## If you want to change X, start here

| Change type | First files to read |
| --- | --- |
| Add or change a backend endpoint | `api/routers/<area>.py`, then the called service/runtime module |
| Change worker lifecycle behavior | `api/routers/algo_workers.py`, `algo_runtime/` |
| Improve paper execution correctness | `paper_runtime/`, `execution_accounting/`, related tests |
| Improve grouped funds/P&L behavior | `execution_accounting/`, `paper_runtime/`, `algo_runtime/`, journaling summaries |
| Improve options workflows | `options/`, worker options endpoints, frontend options pages |
| Improve journal/review workflows | `journaling/`, `api/routers/journal.py`, frontend journal pages |
| Improve market-data ownership | `market-runtime/`, `documents/kite-websocket.md` |
| Improve SDK ergonomics | `sdk/python/kite_algo_worker/`, `sdk/python/examples/`, worker route contracts |
| Improve frontend workflows | `frontend-next/` plus the backend routes they depend on |

## Reading order for new contributors

1. [`platform-overview.md`](platform-overview.md)
2. This file
3. [`algo-worker-sdk-guide.md`](algo-worker-sdk-guide.md) if you care about workers or SDKs
4. The specific service or router files for the subsystem you want to change
