# Algo Worker Developer Agent Context

This folder is a copyable context pack for agents and developers building external Kite Algo strategies.

Give this pack to an external coding agent when you want worker code written in another machine or another repo. The pack is designed to be self-contained enough for safe worker development without handing over the full backend source tree.

## What this pack is for

- external strategy workers
- AI agents generating worker code
- developers who need the worker contract without reading the entire backend

## Current package status

- Package name: `kite-algo-worker`
- Current public PyPI release: `0.7.1`
- SDK tag convention: `kite-algo-worker-vX.Y.Z`

Canonical public install:

```bash
python3 -m pip install kite-algo-worker==0.7.1
```

This pack describes the current released SDK surface. If this pack and the repo disagree, trust the repo files.

## Read in this order

1. `AGENT_PROMPT.md` — pasteable operating instruction for an external coding agent
2. `ALGO_WORKER_DEVELOPMENT_GUIDE.md` — safe-worker playbook and current workflow guidance
3. `examples/` — canonical example subset copied from `sdk/python/examples/`

## Pack contents

- `README.md`
- `AGENT_PROMPT.md`
- `ALGO_WORKER_DEVELOPMENT_GUIDE.md`
- `examples/`

## If you also have repo access

For deeper source-of-truth reading in the main repo, start with:

- `sdk/python/README.md`
- `documents/algo-worker-sdk-guide.md`
- `sdk/python/kite_algo_worker/`
- `api/routers/worker_auth.py`, `worker_market.py`, `worker_execution.py`, and `worker_protection.py`
- `options/api/worker_options_router.py`

If this pack and the repo disagree, trust the repo files.
