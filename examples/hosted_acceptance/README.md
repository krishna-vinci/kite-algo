# Simple hosted strategy acceptance (Bundle 4)

One deterministic single-instrument Python strategy, run end to end through the
platform's own paths, on a **disposable** database and a **loopback** API.

## Run it

```bash
# PostgreSQL test server on 15433 only (never production 15432). The runner
# creates and drops its own uniquely named database.
timeout 900 .venv/bin/python examples/hosted_acceptance/run_acceptance.py --timeout 240
```

Optional: `ACCEPTANCE_ADMIN_DSN` overrides the admin DSN
(default `postgresql://postgres:testonly@127.0.0.1:15433/postgres`).
`--keep-database` keeps the disposable database for inspection.

The runner prints one `[acceptance] …` line per step and writes a sanitized
evidence JSON under `examples/hosted_acceptance/evidence/`.

## What is real and what is simulated

| Element | How it is provided |
| --- | --- |
| database | disposable PostgreSQL on `127.0.0.1:15433`, migrated with `alembic upgrade head` |
| API | the production routers (operator, worker, hosted supervisor) served over loopback by `uvicorn` in-process |
| operator auth | the ordinary `/api/auth/login` route (this isolated instance's own admin password) |
| strategy + version + policy + job | the production operator routes (`/strategies`, `/versions`, `/admission-policy`, `/jobs`) |
| child credential | minted by the real supervisor lifecycle (`prepare_launch`) - no manufactured token |
| child process | `backend.strategies.supervisor.HostedSupervisor` spawning `python -m kite_algo_worker.hosted examples/hosted_acceptance/simple_entry_exit.py` with a minimal environment |
| entry/exit decisions | the child's proposals over HTTP (`/api/algo-workers/worker/proposals`) |
| admission / reservation / execution | the production routes (`/plans/{id}/reserve`, `/plans/{id}/execute`) |
| attribution | the production on-demand publication (`POST /strategies/{id}/positions/rebuild?environment=paper`) |
| market data | **simulated** at the market-data boundary (`SyntheticQuotes`, one deterministic price) |
| instrument catalog rows | seeded fixture rows in the disposable database (metadata only) |

Quotes are synthetic and every order is paper. Nothing here places a broker
order, sends a real notification, reads `.env`, or touches production.

## What the example does

`simple_entry_exit.py` replays a short synthetic price series from the job's
params. The first print above `entry_price` proposes a BUY of `quantity`; the
first print at or below `exit_price` proposes the exit (an absolute flat target).
The platform sequences the *executions*: the operator reserves and executes the
entry, publishes the attributed position, then reserves and executes the exit.
A reducing order against an unfilled entry would be a no-op, never a short.
