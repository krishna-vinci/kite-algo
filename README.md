# Kite Algo

Kite Algo is a self-hosted algorithmic trading platform for Zerodha/Kite workflows.

It gives traders and strategy developers one backend-owned control plane for broker sessions, execution, grouped accounting, runtime state, and reviewable runs — while still letting strategy logic live in the app or in external workers through a typed Python SDK.

> Strategy code owns decisions. Kite Algo owns execution, attribution, accounting, protection, and runtime truth.

## Quick links

- [Quick start](#quick-start)
- [Production auth and admin password hash](#production-auth-and-admin-password-hash)
- [Why traders use it](#why-traders-use-it)
- [Trader-facing capabilities](#trader-facing-capabilities)
- [Algo workers and Python SDK](#algo-workers-and-python-sdk)
- [Architecture at a glance](#architecture-at-a-glance)
- [Documentation](#documentation)
- [Contributing](CONTRIBUTING.md)

## At a glance

| Surface | What you use it for | Best fit for |
| --- | --- | --- |
| **Operator frontend** | Run the day-to-day platform: auth, runtime views, journal surfaces, strategy workflows, and platform tools. | Traders and operators who want one control plane. |
| **Algo workers + Python SDK** | Run local or remote strategy code against worker-safe APIs for lifecycle, market data, execution, grouped funds/P&L, protection, and observability. | Systematic traders and developers who want strategy isolation without losing central execution/accounting. |
| **Backend API** | Build typed integrations around auth-safe and worker-safe platform flows. | Developers extending the platform or building supporting tooling. |
| **Go market runtime** | Own websocket subscriptions, reconnects, tick fanout, and the newer instrument/search serving path. | Users who care about a clean market-data ownership boundary. |

## Why traders use it

Kite Algo is for traders who want more than a thin broker wrapper. It is especially relevant if you want to:

- keep live and paper behavior inside one platform model
- run strategies with explicit grouped attribution instead of loose order tagging
- centralize broker sessions and execution state
- review runs after the fact through journal and summary surfaces
- move strategy logic into separate workers without rebuilding the trading plumbing

## Trader-facing capabilities

| Capability group | What is already in the repo |
| --- | --- |
| **Execution modes** | Dry-run, paper, and live execution paths with explicit attribution and accounting boundaries. |
| **Run-level visibility** | Grouped strategy runs with tracked orders, trades, funds usage, P&L, exits, and journaling. |
| **Market runtime** | Separate Go service for websocket ownership, reconnect handling, tick fanout, and instrument-serving duties. |
| **Options/runtime flows** | Options sessions, strategy flows, protection helpers, and execution-oriented modules. |
| **Operator UI** | Next.js frontend for runtime views, journal/review surfaces, and strategy workflows. |
| **Review and journaling** | Post-run summaries, comparisons, notes, and review-oriented workflows. |

## Algo workers and Python SDK

This is one of the repo's strongest differentiators.

Instead of forcing every strategy to own broker sessions, market-data transport, order tagging, grouped accounting, and exit orchestration, Kite Algo lets workers stay focused on decisions while the backend keeps critical state centralized.

Worker strategies can:

- run locally or on remote machines
- use typed lifecycle and market-data calls
- preview and place intents without owning broker internals
- read grouped run funds and grouped P&L
- patch backend protection state
- inspect worker timelines, safety checks, and run health through newer SDK helpers on the `development` branch
- stay inside the same accounting and journaling model as platform-native flows

Start here:

- [Algo worker + SDK guide](documents/algo-worker-sdk-guide.md)
- [Python SDK README](sdk/python/README.md)

Current public package install:

```bash
python3 -m pip install kite-algo-worker==0.7.6
```

> The `development` branch is prepared for the `0.7.6` worker SDK PyPI release. Publish/tag `kite-algo-worker-v0.7.6` before treating the pinned install as globally available.

## Quick start

This README shows the simplest production-style startup path.

For local development workflow, use [`CONTRIBUTING.md`](CONTRIBUTING.md).

### 1. Prepare environment variables

```bash
cp .env.example .env
```

Fill in at least:

- `APP_JWT_SECRET`
- `APP_ADMIN_USERNAME`
- `APP_ADMIN_PASSWORD_HASH_B64` from the next step
- `KITE_API_KEY`
- `KITE_API_SECRET`
- `KITE_USER_ID`
- `KITE_PASSWORD`
- `KITE_TOTP_KEY`

### 2. Generate the admin password hash

```bash
python3 -c 'from app.auth import hash_password; import base64, getpass; pw=getpass.getpass("Admin password: "); print(base64.b64encode(hash_password(pw).encode()).decode())'
```

Paste the output into `.env` like this:

```dotenv
APP_ADMIN_USERNAME=admin
APP_ADMIN_PASSWORD_HASH_B64=PASTE_OUTPUT_HERE
```

### 3. Start the stack

```bash
docker compose -f compose.yml up --build -d
```

### 4. Open the main surfaces

| Surface | URL |
| --- | --- |
| Frontend | `http://localhost:13000` |
| Backend API | `http://localhost:18777` |
| Market runtime | `http://localhost:18780/healthz` |

For development setup, tests, and local iteration, see [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Production auth and admin password hash

For production, prefer a hashed admin password over plain `APP_ADMIN_PASSWORD`.

This repo supports all of the following:

- `APP_ADMIN_PASSWORD_HASH`
- `APP_ADMIN_PASSWORD_HASH_B64`
- `APP_ADMIN_PASSWORD_HASH_FILE`

Why `APP_ADMIN_PASSWORD_HASH_B64` matters: the hash format contains `$`, and `.env` values commonly flow into Docker Compose. Base64 avoids accidental interpolation or escaping problems when you use `compose.yml`.

### Generate a production-safe hash from repo root

Run this from the repository root:

```bash
python -c 'from app.auth import hash_password; import base64, getpass; pw = getpass.getpass("Admin password: "); hashed = hash_password(pw); print("APP_ADMIN_PASSWORD_HASH=" + hashed); print("APP_ADMIN_PASSWORD_HASH_B64=" + base64.b64encode(hashed.encode()).decode())'
```

That uses the repo's own `app.auth.hash_password()` function and prints both the raw hash and the Compose-safe base64 form.

### Recommended `.env` pattern for production compose

```dotenv
APP_ADMIN_USERNAME=admin
APP_ADMIN_PASSWORD_HASH_B64=<paste-generated-base64-value>
```

Then start the production-style stack:

```bash
docker compose -f compose.yml up --build -d
```

### File-based secret option

If you mount secrets as files, put the raw `pbkdf2_sha256$...` value into a file and point `.env` at it:

```dotenv
APP_ADMIN_PASSWORD_HASH_FILE=/run/secrets/app_admin_password_hash
```

At startup, the app accepts `APP_ADMIN_PASSWORD`, `APP_ADMIN_PASSWORD_HASH`, `APP_ADMIN_PASSWORD_HASH_B64`, or `APP_ADMIN_PASSWORD_HASH_FILE`, and will fail fast outside insecure dev mode if none are configured.

## Architecture at a glance

| Layer | Responsibility | Main code |
| --- | --- | --- |
| App bootstrap and auth | Startup, middleware, auth config validation, request protection | `main.py`, `app/`, `api/routers/auth.py` |
| Broker session layer | Own Kite login/session lifecycle and broker-facing services | `broker_api/` |
| Typed API layer | Stable contracts for frontend and workers | `api/routers/`, `api/services/`, `api/repositories/` |
| Algo runtime | Strategy lifecycle, attribution, execution wiring | `algo_runtime/` |
| Options core | Options sessions, strategy flows, protection, and execution helpers | `options/` |
| Paper runtime | Durable simulated execution state | `paper_runtime/` |
| Execution accounting | Shared attribution and cost contracts | `execution_accounting/` |
| Journaling | Run history, review, summaries, analytics | `journaling/` |
| Market runtime | Websocket ownership, instrument serving, and tick fanout | `market-runtime/` |
| Worker SDK | Typed remote strategy interface | `sdk/python/` |
| Frontend | Operator-facing product surface | `frontend-next/` |

## System architecture flow

```text
Trader / Developer
        |
        v
  Next.js frontend  <---------------------------+
        |                                        |
        v                                        |
  FastAPI control plane                         |
        |                                        |
        +--> app bootstrap + middleware          |
        +--> broker session ownership            |
        +--> typed route handlers                |
        +--> algo runtime / paper runtime        |
        +--> options flows + protection          |
        +--> journaling / grouped reporting      |
        |                                        |
        v                                        |
  Redis-backed status/tick consumers             |
        ^                                        |
        |                                        |
  Redis tick/status bus                          |
        ^                                        |
        |                                        |
  market-runtime (Go) ---------------------------+
```

The frontend and external workers both enter through the same FastAPI control plane. The Go market runtime owns broker websocket connectivity and related market-data serving responsibilities so backend consumers do not each open their own broker websocket connections.

## Strategy and algo-worker flow

```text
Worker strategy code
        |
        v
Kite Algo Worker Python SDK
        |
        v
/api/algo-workers/worker/*
        |
        +--> create/recover run
        +--> claim session when needed
        +--> safety check + market data
        +--> preview/place intents
        +--> grouped funds + grouped P&L
        +--> timeline / decision observability
        +--> backend protection / GTT helpers
        +--> exit grouped run
        |
        v
Backend execution + attribution + accounting
        |
        +--> paper runtime   (paper)
        +--> broker session  (live)
        |
        v
Journal / review / control-plane visibility
```

## Repository layout

| Area | Purpose |
| --- | --- |
| `main.py`, `app/`, `api/` | App wiring, middleware, auth, typed API surfaces, support services/repositories |
| `broker_api/` | Broker sessions, orders, instruments, timeline, and market-facing helpers |
| `algo_runtime/`, `options/`, `paper_runtime/`, `execution_accounting/` | Strategy lifecycle, execution modes, protection, attribution, and accounting |
| `journaling/` | Review, summaries, notes, and run history |
| `market-runtime/` | Go websocket runtime and related market-data serving concerns |
| `sdk/python/` | External algo-worker SDK |
| `frontend-next/` | Operator-facing Next.js app |
| `documents/` | Public docs and onboarding |
| `tests/` | Verification suites |

For the detailed folder map, use [documents/codebase-map.md](documents/codebase-map.md).

## Documentation

Start with:

- [Public docs index](documents/README.md)
- [Platform overview](documents/platform-overview.md)
- [Codebase map](documents/codebase-map.md)
- [Algo worker + SDK guide](documents/algo-worker-sdk-guide.md)

Deeper references:

- [Kite websocket and market-runtime notes](documents/kite-websocket.md)
- [Live/paper accounting and worker live execution notes](documents/live-paper-accounting-and-worker-live-execution.md)
- [Backend progress tracker](documents/kite-backend-progress.md)

## Contributing, security, and license

- [Contributing guide](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Apache-2.0 license](LICENSE)
