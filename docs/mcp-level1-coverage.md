# Kite Algo MCP Level 1

The `kite-algo-mcp` package is a local stdio adapter over the public
`kite-algo-worker` SDK 0.9.0. It does not add a backend strategy service. The
worker remains authoritative for token scope, account access, execution risk,
leases, persistence, and broker responses.

## Start-up

Build or install the package in its isolated environment:

```sh
python3 -m venv mcp/python/.venv
mcp/python/.venv/bin/python -m pip install -e ./sdk/python -e ./mcp/python
export KITE_MCP_API_URL=https://worker.example.invalid
export KITE_MCP_WORKER_TOKEN='provided-out-of-band'
export KITE_MCP_PROFILE=read
export KITE_MCP_ALLOW_DATA_REFRESH=false
mcp/python/.venv/bin/kite-algo-mcp
```

Loopback `http://127.0.0.1`, `http://localhost`, and `http://[::1]` are
allowed for local development; non-loopback URLs must use HTTPS. Credentials
are read from the environment, never command-line arguments. The server emits
protocol data only on stdout and diagnostics only on stderr.

Profiles are `read`, `paper`, and `live`. Read mode exposes research and
inspection tools only. Paper mode exposes explicit paper run/order tools but
rejects live execution and account-level live GTT mutations. Live mode is an
explicit opt-in and still requires the worker token to authorize the selected
account, template, mode, and action. `KITE_MCP_ALLOW_DATA_REFRESH=true` is a
separate opt-in for historical ingestion and fundamentals refresh.

An MCP host should require an explicit human/host approval for write and
destructive tools. Approval annotations are metadata, not authorization.

## Safety and result semantics

The adapter exposes 73 reviewed tools in the full live/refresh catalog. It
uses the same catalog for visibility and dispatch policy, but checks backend
health capabilities again at invocation. Disabled tools are absent from
`tools/list`; direct calls are rejected. Inputs use strict Pydantic schemas and
reject extra fields. There is no generic HTTP, SQL, Python, filesystem-write,
credential-management, broker-login, Telegram, screener, scheduler,
rebalancer, optimizer, or background strategy tool.

All calls return a bounded `ToolResult` envelope. Responses are capped at 256
KiB; symbols, candles, event pages and basket legs are bounded before backend
calls. Missing fundamentals and indicator warmup values remain null. Non-finite
numbers become null with a warning. Sensitive credential fields and worker
lease nonces are redacted server-side.

Run mutations are serialized per run and use an MCP-owned temporary worker
lease. The nonce never crosses the model boundary. A claim conflict refuses
the operation; it does not force takeover. Entry submissions run the backend
safety check and carry an explicit idempotency key. Preview tools never submit
an intent. Cancellation and run exit remain exact, scoped risk-reducing
operations and are not blocked by an entry safety refusal.

If a mutation times out or disconnects after submission may have started, the
result is `outcome_unknown=true` with the matching reconciliation read tool.
The adapter never retries that write. Partial basket and broker rejection
details are preserved. A failed lease release is logged without replacing a
successful execution result with a retry instruction.

Times and freshness are returned from the worker in ISO form; market sessions
follow the worker's exchange/timezone metadata (NSE data is conventionally
Asia/Kolkata). The adapter does not fabricate stale, missing, margin, or depth
values. A quote without upstream depth is reported as `available=false`,
distinct from an empty order book.

## Tool families

The catalog covers capabilities rather than one tool per HTTP route:

- discovery: capabilities, token-scoped runs, funds, portfolio;
- market: instrument lookup, quotes/snapshot/depth, candles/history, calendar,
  supported index constituents and status (including nullable stored sectors);
- fundamentals: features, statements, freshness/status, and opt-in refresh;
- indicators: one allowlisted SDK indicator over bounded supplied bars (SMA,
  EMA, WMA, VWMA, Supertrend, RSI, MACD, PPO, DPO, stochastic, CCI,
  Williams R, linear regression, ATR, Bollinger, Keltner, ADX, Aroon, SAR,
  OBV, VWAP, MFI, crossover/crossunder, highest/lowest, rising/falling);
- scoped run/order tools: create, safety, previews, explicit paper/live
  submissions, orders/trades/history, baskets, brackets, GTTs, risk,
  protection, PnL/timeline/events, decision logging, exact cancellation/exit;
- options: expiries, chains, Greeks, PCR/max pain, typed contract resolution,
  previews, explicit option runs, protection state/update, and simulation-only
  protection replay.

The SDK endpoint disposition is machine-checked in
`mcp/python/kite_algo_mcp/coverage.json`: every HTTP endpoint has exactly one
`tool`, `internal`, or `deferred` record, and WebSocket/SSE streams remain
internal/deferred because Level 1 has bounded snapshots and pages instead of
endless MCP calls. CSV export remains internal; JSON fundamentals cover the
research use case. The maintained comparison intentionally does not promise
all OpenAlgo names or an empirically measured usage percentage.

| Surface | Level 1 disposition | Difference from a broad OpenAlgo-style surface |
| --- | --- | --- |
| Quotes, candles, calendar, index members, fundamentals | bounded MCP tools | snapshots/pages only; no generic route proxy or unbounded stream |
| SDK indicators | one typed calculation tool | no screener, universe scan, ranking or saved filter |
| Runs, orders, baskets, protection, options | explicit scoped tools | worker leases, safety and idempotency stay authoritative; no smart/split executor |
| Historical/fundamentals ingestion | opt-in data-write tools | never hidden behind an ordinary read |
| Telegram, scheduler, rebalancer, optimizer | excluded | planned only after this release's acceptance gates |

## Composition examples

Research composition is explicit: call `get_index_constituents` for a
supported universe, choose a bounded symbol subset in the host/agent, call
`get_fundamentals_features`, retrieve candles, then call
`calculate_indicator`. There is no universe scan, ranking, saved filter, or
dedicated screener tool.

Paper execution is similarly explicit: choose a paper account/run with
`create_run`, call `preview_order` or `preview_basket`, then submit once with
an idempotency key and inspect orders/trades/PnL before an exact cancellation
or exit. A preview does not imply eventual submission while the host is
offline; MCP does not keep a chat alive or guarantee scheduling.

Option workflows use typed expiry/underlying/selector fields, preview first,
then explicit run entry/exit. Option protection replay evaluates supplied
snapshots in memory and does not persist protection or orders; it is classified
as a read/computation tool.

## Packaging and exclusions

The exact runtime resolution is recorded in `mcp/python/requirements.lock`.
The adapter uses standalone `fastmcp==4.0.3` over official `mcp==2.1.1` in the
isolated MCP environment; those dependencies are not installed in the backend
`.venv`. The CLI keeps FastMCP's low-level server and official MCP message
types, but uses native asyncio pipes for stdio: the released `mcp==2.1.1`
`anyio.wrap_file` bridge did not drain subprocess pipes in this execution
environment. An official `mcp.ClientSession` initialize/list-tools smoke test
passes against both the editable and clean wheel installs, including listing
and reading the two static resources. CI builds and installs wheels before
running protocol tests. No live credentials or live
submissions are used by package tests. Telegram alerts,
dedicated screeners, schedulers, autonomous rebalancing, optimizers,
backtests, smart/split-order executors, and service deployment remain outside
this release.

## Regression limitations

The bounded full backend gate was run as specified. SDK, route-mount,
discovery, depth, ingestion and MCP suites pass; the broad
`tests/api/test_algo_worker_api.py` run reaches
`test_worker_market_candles_current_falls_back_to_latest_cached_candle` and
does not complete within 45 seconds (exit 124). The same test passes alone in
1.86 seconds, so this is recorded as a broader existing TestClient/test-order
environment hang rather than counted as a passing or skipped test. No backend
service was recreated to work around it.
