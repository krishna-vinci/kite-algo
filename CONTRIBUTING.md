# Contributing to Kite Algo

Thanks for contributing.

Kite Algo is a full-stack trading platform with real execution, paper execution, runtime services, grouped accounting, journaling, and an external worker model. Good contributions respect those boundaries instead of cutting around them.

## Before you start

Please read these first:

- [README.md](README.md)
- [documents/README.md](documents/README.md)
- [documents/platform-overview.md](documents/platform-overview.md)
- [documents/codebase-map.md](documents/codebase-map.md)
- [documents/algo-worker-sdk-guide.md](documents/algo-worker-sdk-guide.md)

## Prerequisites

- Python 3
- Docker + Docker Compose
- Node.js for `frontend-next/`
- Go for `market-runtime/`
- Git

## Local development setup

This file covers the contributor workflow. Unlike the root `README.md`, it uses the local development stack.

### 1. Prepare environment variables

```bash
cp .env.example .env
```

Set real values for app auth and broker credentials before using live broker-backed flows.

For auth setup details, including production-safe admin hash generation and when to use `APP_ADMIN_PASSWORD_HASH_B64`, see [README.md](README.md#production-auth-and-admin-password-hash).

### 2. Start the local stack

```bash
docker compose -f compose.dev.yml up --build
```

Use `compose.dev.yml` for contributor workflows. The production-oriented stack is `compose.yml`.

### 3. Local URLs

| Surface | URL |
| --- | --- |
| Frontend | `http://localhost:13000` |
| Backend | `http://localhost:18777` |
| Market runtime | `http://localhost:18780/healthz` |
| Meilisearch | `http://localhost:17700` |

## Repository map

| Path | Responsibility |
| --- | --- |
| `api/` | Public backend routes and API helpers |
| `broker_api/` | Broker-facing services and session handling |
| `algo_runtime/` | Strategy lifecycle, attribution, and execution wiring |
| `options/` | Options sessions, strategy flows, protection, and execution helpers |
| `paper_runtime/` | Durable paper execution state |
| `execution_accounting/` | Shared attribution and cost contracts |
| `journaling/` | Run history, journal views, summaries, review flows |
| `market-runtime/` | Go websocket runtime |
| `sdk/python/` | Python SDK for external algo workers |
| `frontend-next/` | Next.js frontend |
| `strategies/` | Strategy implementations and strategy-oriented modules |
| `documents/` | Public docs and onboarding |
| `tests/` | Verification suites |

## Contribution lanes

| Area | Good contribution examples | Start here |
| --- | --- | --- |
| Backend/API | Typed route cleanup, validation, contract improvements, auth-safe flows | `api/`, `broker_api/`, `main.py` |
| Algo runtime | Run lifecycle, attribution, intent handling, dry-run/paper/live coherence | `algo_runtime/` |
| Options | Options sessions, strategy flows, protection, and execution helpers | `options/`, worker options endpoints, frontend options pages |
| Paper execution | Simulation accuracy, funds/P&L correctness, isolation fixes | `paper_runtime/`, `execution_accounting/` |
| Journaling | Run summaries, review flows, metrics, note handling | `journaling/` |
| Market runtime | Subscription ownership, runtime status, tick fanout | `market-runtime/` |
| Worker SDK | Typed models, helper ergonomics, examples, worker-safe flows | `sdk/python/` |
| Frontend | Operator UX, typed pages, tests, data presentation | `frontend-next/` |
| Docs | Architecture, codebase guides, onboarding improvements | `README.md`, `documents/`, `CONTRIBUTING.md` |

## Workflow expectations

- prefer small, reviewable diffs
- keep ownership boundaries intact
- do not bypass backend-owned execution or accounting rules
- do not commit secrets or real credentials
- add or update tests when behavior changes
- update docs when public behavior or developer workflows change

## High-risk areas

Take extra care in these parts of the codebase:

- `auth_service.py` and auth/session routes
- live execution paths
- grouped funds and grouped P&L logic
- execution attribution and reconciliation
- `market-runtime/` ownership and tick distribution
- worker-token authorization and worker-safe route boundaries

## Running checks

### Backend

```bash
pytest tests -q
```

### Frontend

```bash
cd frontend-next
npm run lint
npm run test
npm run typecheck
```

### Market runtime

```bash
cd market-runtime
go test ./...
```

## Good first contributions

- improve docs or code comments around system boundaries
- tighten validation on typed endpoints
- add or improve focused tests
- improve SDK ergonomics and examples
- improve frontend clarity without changing platform boundaries

## Before opening a large PR

For larger architecture or workflow changes, open an issue or start a discussion in the PR first so the expected boundary changes are clear.

## Security

If you find a vulnerability, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.
