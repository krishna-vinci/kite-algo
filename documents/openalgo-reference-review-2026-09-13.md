# OpenAlgo reference review

Read-only source investigation, 2026-09-13. No strategies were executed, orders placed, notifications sent, or services changed.

## Evidence boundary

Local reference: `openalgo/`, remote `https://github.com/marketcalls/openalgo.git`, revision `c71d875fe2ae6de4bb858d422da911cab4ad77ae`, dated 2026-04-06.

Upstream GitHub README, commit history, Python hosting source and portfolio directory were inspected through the web reader. The retrieved commit-history page shows September 8 commit `9117eb9`; it is a cached view, not proof of the exact current remote HEAD. Several upstream service-source requests failed. Therefore detailed Flow and Telegram implementation findings below describe the local revision, not verified September implementations. The reference checkout was not updated.

Sources:
- https://github.com/marketcalls/openalgo
- https://github.com/marketcalls/openalgo/commits/main/
- https://raw.githubusercontent.com/marketcalls/openalgo/main/blueprints/python_strategy.py
- https://github.com/marketcalls/openalgo/tree/main/portfolio

## What the reference actually demonstrates

| Capability | Evidence | Implication for kite-algo |
|---|---|---|
| Hosted Python scripts | `openalgo/blueprints/python_strategy.py:377` starts a script with the Python interpreter; `:469` creates the subprocess. The file also implements scheduling, logs and process recovery. | A hosted runner can make existing SDK workers accessible through upload/edit/start/schedule/logs. A strategy need not become a new built-in kernel class. |
| Resource limits | Same file `:327` sets Unix resource limits; subprocess arguments create a process group/session. | Useful containment for resource use, but this is not a filesystem/secret/security sandbox. Separate processes do not establish tenant isolation. |
| Visual workflows | `openalgo/services/flow_executor_service.py` has separate node execution for market data, conditions, orders and Telegram. | A common authoring experience can expose several outcomes without requiring one evaluator for arbitrary Python and visual rules. |
| Shared execution services | `openalgo/services/flow_openalgo_client.py:59` wraps the internal order service with an SDK-like interface. | Reuse the existing backend execution authority through adapters. Do not create a second broker-order implementation in the alert worker. |
| Workflow notifications | `openalgo/services/flow_executor_service.py:1152` invokes the client Telegram operation. | Notification should be available as an explicit workflow/strategy capability. Our arbitrary strategy notification adapter remains missing. |
| Order notifications | `openalgo/services/telegram_alert_service.py:415` dispatches through a thread pool; `:285` sends HTTP and queues on failure. | This path is not evidence of transactionally atomic order-plus-notification publication. Retain our durable outbox rather than copying this dispatch design. |
| Simulated execution | `openalgo/sandbox/execution_engine.py:45` processes pending orders against quotes and updates positions. | Paper execution, historical backtesting and isolation of user code are distinct capabilities. |

The upstream README presents API access, hosted Python and visual Flow as different ways into the product. That supports shared platform services with multiple execution environments; it does not establish a single universal strategy evaluator.

## Corrections to the older comparison

- Do not describe subprocess hosting as complete security isolation.
- Do not say current OpenAlgo has no backtesting: the retrieved upstream history explicitly contains backtester changes, and the portfolio directory includes analytics, rebalance and walkforward modules. Their exact strategy coverage was not established in this review.
- Do not claim current OpenAlgo lacks all journaling, portfolio or multi-account capabilities without tracing those current modules. This review does not establish those absences.
- Keep local April implementation evidence separate from upstream feature evidence. Failed raw-source requests do not justify assuming implementation stayed unchanged.

## Recommended direction, not an approved implementation plan

1. Keep existing alerts/screeners and their durable state, ownership fencing and outbox.
2. Offer three authoring/execution paths: built-in rules, platform-hosted Python, and external workers for custom dependencies/ML.
3. Build hosted lifecycle on the existing worker-run API, with source revisions, explicit parameters, logs, recurring heartbeat ownership and clear stop semantics.
4. Define common signal/result and numerical contracts before connecting engines. Existing kernel and alert EMA initialization differs; sharing an indicator name is insufficient.
5. Add a run-scoped notification producer adapter to the durable delivery system. Notification caps or delivery failures must not control order eligibility.
6. Connect screener/signal results to strategies only through an authorized, freshness-aware, recoverable consumption protocol. Polling event history is not that protocol by itself.
7. Let trade intents continue through existing risk, account, paper/live and idempotency enforcement. A screener selects/ranks candidates; portfolio sizing, rebalance and execution remain distinct responsibilities.
8. Do not initiate a Go rewrite based on this reference or an unmeasured memory-saving estimate. Establish ownership and user flows first.

Suggested first product example: save a momentum ranking screen; inspect its candidates; notify on membership changes; optionally connect its results to a separately configured paper portfolio strategy. Advanced users can supply Python scoring through a hosted or external worker. These connections are proposed work, not a statement that the entire flow already exists.
