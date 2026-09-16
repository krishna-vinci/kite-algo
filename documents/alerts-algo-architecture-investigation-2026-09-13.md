# Alerts, screeners, and strategy execution: architecture investigation

Date: 2026-09-13. Inspected revision: `f94b890` plus the existing working tree.

This is an evidence-based investigation and decision brief, not an approved implementation plan. No production code, running services, credentials, orders, or deployment settings were changed. Three independent investigations covered Go ownership, strategy lifecycle, and shared signals/notifications. The review traced source wiring rather than relying on phase-completion labels. It did not rerun live tests or measure process memory.

## Conclusion

The platform should present one coherent research/monitoring/strategy experience while retaining different execution environments. The existing alerts/screener investment is reusable. The main missing product capability is hosted strategy lifecycle over the existing worker-run execution path—not another Go strategy engine.

Sharing condition definitions, numerical semantics, instrument identity, and notification delivery is useful. Combining arbitrary user code, market ingestion, alert evaluation, and order execution in one process is not required for that sharing and would increase their failure coupling.

## What actually exists

| Module | Actual responsibility | Evidence |
|---|---|---|
| Go market-runtime | Broker websocket ingestion, owner-scoped subscription union, reconnect/rotation, catalog enrichment, latest-tick cache, marketwatch fan-out, broker order-update relay | `market-runtime/internal/service/service.go:127`, `:242`, `:261`, `:290`; `redis.go:41`; `http.go:14` |
| Python candle pipeline | Aggregates ticks into forming/completed candles and persists history | `backend/broker_api/market/candle_aggregator.py:56`, `:234` |
| Alerts/workflows | Declarative definitions, predicates, advanced conditions, feature windows, lifecycle, durable evaluation state, events | `backend/workflows/models.py`, `feature_engine.py`, `service.py`; `backend/alerts/predicates.py` |
| Screeners | Scheduled candidate filtering/ranking, snapshots, attachments, downstream universes; reuse alert predicates/features | `backend/screeners/runner.py:39`, `:242`, `:290`; `scheduler.py` |
| Notification delivery | Workflow/screener event outbox, per-channel attempts/retries/provider outcomes | `backend/notifications/repository.py:84`; `worker.py:126` |
| External algo workers | User-launched Python programs use the worker SDK for run/session lifecycle, data, decisions, execution and review | `sdk/python/kite_algo_worker/client.py:161`; `managed_run.py`; `sdk/python/examples/managed_run_worker.py` |
| Execution/protection/accounting | Backend-owned paper/live order paths, attribution, run state, protection, P&L and journal integration | `backend/api/routers/worker_auth.py:169`; `worker_execution.py:800`, `:854`; `worker_protection.py` |
| In-process algo kernel | Plugin registry, snapshots, dependency filtering, evaluation, state and intent-bridge scaffold | `backend/algo_runtime/kernel.py:114`; `registry.py:13` |
| Hosted strategy files | Draft only: no platform file discovery/store/runner found | `documents/hosted-strategies-proposal-draft.md:3`; `backend/app/bootstrap.py:388` |

The Go HTTP module does not expose strategy evaluation, alert/screener authoring, notification delivery, or broker order execution. Go relays order updates; that is distinct from deciding or placing orders. `mcp/go` is an adapter to Python APIs, not another strategy engine. Broker SDK libraries likewise do not establish service ownership.

Some older websocket documents assign candle aggregation to Go as a target architecture. Current source puts candles in Python. Treat those passages as proposals, not current topology.

## What is shared, duplicated, or disconnected

### Already shared

- Alerts and screeners use common canonical condition/stage models, validation, predicates and feature computation code. They are not wholly independent engines.
- Their notifications converge on the existing outbox/delivery module.
- Both use the market-data foundation and instrument catalog. Older algo dependency models still use broker token identities directly.
- The worker SDK already exposes workflow management, screener results, and external producer operations alongside run/execution operations. Separate permissions still apply.
- `backend/api/services/indicators_service.py:1` uses the SDK's `TechnicalAnalysis` directly for server-side indicator requests.

Shared implementation does not imply one global feature cache: the screener runner constructs a feature engine for its evaluation, while the streaming worker maintains its own windows.

### Real numerical duplication

`backend/alerts/features.py` intentionally implements lightweight indicator kernels separately from the pandas/numpy SDK, with parity fixtures. This is an explicit deployment tradeoff, not automatically a defect.

The in-process algo kernel has a third EMA implementation. It seeds EMA from the first price (`backend/algo_runtime/indicators.py:59`), whereas alerts/SDK seed from an initial simple average (`backend/alerts/features.py:174`). A local pure-function comparison confirmed:

| Input `[1, 2, 3]`, period 3 | Output |
|---|---|
| Algo kernel EMA | `[1.0, 1.5, 2.25]` |
| Alert EMA | `[None, None, 2.0]` |

Do not silently replace one with the other in a running strategy. Select/version semantics and migrate deliberately. A common name alone is not numerical parity.

### The kernel is not a ready strategy-hosting product

Bootstrap constructs an empty `AlgoRegistry` at `backend/app/bootstrap.py:388`. No production registration calls were found. `kernel.py:59` skips unregistered strategy types. Storing an algo instance through an administrative endpoint does not load a Python file.

The kernel also executes actions before writing its checkpoint (`kernel.py:114` onward). It must not be assumed to inherit the alerts module's atomic checkpoint/event/outbox guarantees. Trading-side effects require their own durable execution protocol; importing a common predicate does not solve that.

## Notifications: the precise answer

| Source | Can use durable notification delivery today? |
|---|---|
| Alert condition | Yes, through its configured channels and signal event |
| Screener attachment | Yes, through the same delivery infrastructure |
| External model score referenced by a candle alert | Yes, indirectly: publish an accepted external value and let the alert evaluate it |
| Arbitrary strategy `notify(message)` | No general run-scoped notification operation was found |
| Kernel `NotifyAction` | Type and optional handler exist, but production bootstrap provides no handler |
| Scheduler ntfy message | Separate direct HTTP path, not the shared outbox |

Evidence: `backend/algo_runtime/intent_bridge.py:85`, `:139`; `backend/app/bootstrap.py:414`; `backend/broker_api/broker_api.py:63`; `sdk/python/kite_algo_worker/client.py:1167`.

The delivery table currently requires an event in `signal_events` (`backend/notifications/repository.py:89`). Generalizing notification producers therefore needs a deliberate event/attribution adapter; it is not just passing a string to the provider. Keep trading decisions, notification suppression, and delivery outcomes separate: a Telegram outage or notification cap must not accidentally control trading eligibility.

The SDK can poll workflow/screener event history. This is not a durable consumer with an acknowledged cursor and exactly-once order effects. A future signal-to-strategy adapter needs consumption identity, freshness, owner authorization, recovery, and run attribution. It should use underlying signal/result data, not delivered Telegram messages.

External inputs are sampled on a consuming alert stage's candle clock, not pushed on receipt (`backend/workflows/external_context.py:1`). Values can expire or be superseded before evaluation. The current screener scheduler wires a fundamentals context loader (`backend/screeners/scheduler.py:276`); external-score-to-screener integration was not found and should not be promised merely because the predicate can read a supplied context.

## Strategy files and advanced ML work

Today a developer can save a Python file anywhere and launch it themselves. They configure the SDK, create/claim a run, obtain data, calculate decisions, submit intents through the backend, and inspect results. `sdk/python/examples/signal_driven_worker.py` demonstrates decisions from an external model/file translated into this run/order path.

Dropping a file into a platform folder does not automatically discover or run it. The hosted-strategies document is explicitly a draft. Upload/edit/version/start/stop/schedule/logging and per-process lifecycle are proposals.

External workers already suit large datasets, custom Python dependencies, GPUs and remote machines because the user controls that environment. This is not a claim that the platform currently provisions or sandboxes those environments.

The SDK run context sends an initial heartbeat and releases the session; it does not start a recurring heartbeat loop (`client.py:161`, `managed_run.py:35`). A future hosted runner must explicitly own ongoing liveness. Stopping a process, releasing a session, cancelling outstanding orders, and closing positions are distinct operations and must not share an ambiguous Stop button.

The draft has decisions to resolve before implementation:

- It both pre-creates a run in the runner and asks the script to create one. Choose one lifecycle authority with a clear recovery path.
- Scoped credentials restrict API calls, but do not isolate arbitrary code that shares the API process environment, filesystem or broker secrets. A subprocess is crash/resource isolation, not automatically a security sandbox.
- Dependency environments, resource limits, trust model, source/version pinning, restart/heartbeat semantics and safe stop behavior must be explicit.
- Loading a file should register a versioned strategy; it should not silently authorize live orders. Default trial execution can use paper mode with explicit start.

Historical data access, dry-run validation, and paper trading exist in useful forms; they are not a historical backtesting/research engine. No general backtest runner was found. Research requires replay-time semantics, historical universe/data availability, fills/costs and reproducibility; it should remain a distinct future capability.

## Recommended user experience (proposal)

Offer common concepts with progressively more control:

1. **Find candidates:** choose a universe, filters, ranking and schedule.
2. **Monitor a condition:** select candidates, define a condition, select notification behavior.
3. **Run a strategy:** choose a named definition or custom code, configure allocation/risk/account, select paper/live, inspect execution and journal.

For a basic user, guided forms and templates hide worker tokens, broker tokens and process management. For a developer, a strategy file plus parameters is registered and launched by a hosted runner. For advanced users, an external worker uses the same run/attribution contract from a separate environment.

These are user-experience directions, not capabilities already delivered. In particular, a declarative alert definition currently has no order action, and the platform should not silently turn an existing alert into a trader.

A momentum or mean-reversion candidate rule can be reused as an observation. A trading strategy additionally needs holdings/weights, sizing, risk, order lifecycle and exits. A portfolio-wide rebalance engine is not implied by a screener's top-N list.

## Bounded architecture opportunities

### 1. Shared numerical and observation module

Files: `backend/alerts/features.py`, `backend/workflows/feature_engine.py`, `backend/algo_runtime/indicators.py`, `backend/api/services/indicators_service.py`, `sdk/python/kite_algo_worker/indicators/`.

Problem: overlapping indicators have different initialization, identity and output conventions. Solution: establish/version common semantics and use adapters for lightweight streaming and dataframe research implementations. This provides locality for numerical fixes and leverage across consumers without forcing every worker to load pandas or use one global process/cache.

### 2. Shared notification module

Files: `backend/notifications/`, `backend/algo_runtime/intent_bridge.py`, `backend/broker_api/broker_api.py`, SDK notification operations.

Problem: durable alert delivery, disconnected `NotifyAction`, and direct scheduler HTTP coexist. Solution: adapt approved event producers to the existing durable delivery module, retaining source attribution and authorization. This concentrates retries, credentials and history instead of cloning Telegram/ntfy code. Keep the already-agreed scheduler cutover gate until explicitly revised.

### 3. Hosted strategy lifecycle module

Files: worker run/session APIs, `sdk/python/kite_algo_worker/managed_run.py`, existing run monitoring UI; hosted-strategies draft.

Problem: simple scripts require manual hosting while an unused kernel looks like a second strategy path. Solution: host the established worker-run model rather than build another broker/execution engine. Explicitly decide whether the in-process kernel remains for trusted built-ins or is eventually retired; do not expand both hosting paths by accident.

### 4. Signal-to-strategy consumption seam

Files: workflow events/results, SDK workflow/screener operations, worker execution/session APIs.

Problem: reading event history is not a recoverable execution subscription. Solution: design an adapter for approved strategies to consume versioned signal/result occurrences and then use existing risk/execution authority. Tests should cross this complete seam, including recovery, rather than test a predicate and assume execution wiring follows.

## What to do next

Make one architecture decision before adding another runner or migrating to Go: keep the worker-run model as execution authority; reuse alerts/screeners for built-in observations; preserve external compute; add hosted authoring as lifecycle around that model. Resolve numerical semantics and notification attribution as narrow shared modules.

Then validate the design with one small strategy (for example, a conventional crossover) in monitoring and paper execution modes, using the same selected signal definition and existing run accounting. This is an architectural example to design, not authorization to place orders now.

Do not infer a Go rewrite or guaranteed RAM saving from a reported 120 MB footprint. No memory measurement was performed here. Go may be suitable for specific measured hot paths later; changing language is independent of delivering easier strategy authoring.
