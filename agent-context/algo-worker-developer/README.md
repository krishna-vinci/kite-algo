# Algo Worker Developer Agent Context

This folder is a copyable context pack for agents and developers building external Kite Algo strategies.

Give this pack to an external coding agent when you want worker code written in another machine or another repo. The pack is designed to be self-contained enough for safe worker development without handing over the full backend source tree.

## What this pack is for

- external strategy workers
- AI agents generating worker code
- developers who need the worker contract without reading the entire backend

## Current package status

- Package name: `kite-algo-worker`
- SDK version described by this pack: `0.7.7`
- Last public release before the 0.7.7 publication: `0.7.6`
- SDK tag convention: `kite-algo-worker-vX.Y.Z`

Canonical public install:

```bash
python3 -m pip install kite-algo-worker==0.7.7
```

The 0.7.7 install becomes public when tag `kite-algo-worker-v0.7.7` completes the trusted-publishing workflow. Keep the exact pin; do not silently fall back to an unpinned package. If this pack and the repo disagree, trust the repo files.

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
- `backend/api/routers/worker_auth.py`, `worker_market.py`, `worker_execution.py`, `worker_protection.py`, and `fundamentals.py`
- `backend/options/api/worker_options_router.py`
- `fundamentals/`

If this pack and the repo disagree, trust the repo files.
