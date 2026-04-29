# Python SDK, Worker Protection, and Options Protection Assessment

Date: 2026-04-29

## Why this note exists

This is a handoff summary of the recent investigation and validation work around:

- the Python algo-worker SDK
- backend-owned worker protection
- existing options protection/runtime logic
- architectural comparison against OpenAlgo

The goal is to let another agent continue from the current conclusions without redoing the exploration.

---

## Executive summary

### Current conclusion

The Python SDK is now in a good state for non-options algo development and is strong enough to keep building on.

The backend-owned detached-worker protection model is a good architectural choice and should be kept.

The repo already has meaningful options-specific protection logic in the canonical runtime-managed options path, but it is not yet fully unified with the generic detached worker backend protection path.

### What we are leaning toward

1. Keep backend-owned protection as the platform safety model.
2. Treat detached external workers as disposable.
3. Prefer the canonical runtime-managed options path over legacy option protection paths.
4. Before major options expansion, map or unify protection ownership rather than inventing a third protection model.
5. Focus future options work on the canonical path, with clear visibility into what is covered vs still missing.

---

## Python SDK status

### Fixed during this session

Two real issues were found and fixed:

1. `list_trades()` for live worker runs failed because provider trades could include time-only timestamps like `09:31:10`.
2. live `preview_order()` degraded charges calculation because market order preview payloads could lack a usable `average_price`.

### Files changed

- `broker_api/kite_orders.py`
- `execution_accounting/kite_costs.py`
- `sdk/python/kite_algo_worker/async_client.py`
- `tests/test_live_cost_contract.py`
- `tests/test_worker_sdk_async.py`

### SDK validation outcome

Validated with a real worker token against the local dev backend.

Confirmed working:

- sync SDK:
  - `health()`
  - `get_funds(mode="live")`
  - `get_quotes(...)`
  - `get_candles_snapshot(...)`
  - `get_historical_candles_snapshot(...)`
  - `list_orders(...)`
  - `list_trades(...)`
  - `preview_order(...)`
- async SDK:
  - `health()`
  - `get_funds(...)`
  - `get_quotes(...)`
  - `get_historical_candles(...)`
- indicator stack:
  - `ta.sma`
  - `ta.ema`
  - `ta.rsi`
  - `ta.atr`
  - `LiveIndicatorEngine.from_history(...)`
- live websocket streams:
  - NIFTY tick stream returned active live snapshot data
  - NIFTY candle stream returned active current candle data

### Final view on SDK

The SDK is now ready to develop algo strategies against the backend safely.

It is not just a toy wrapper anymore; it now has meaningful runtime shape:

- worker-safe market data
- run lifecycle
- funds/run-funds
- live preview flow
- websocket support
- typed history/candle helpers
- indicators

### Important live validation note

No real live order was placed in the final validation step.

AMO permission was discussed, but final checks used health, quotes, funds, previews, orders/trades inspection, indicators, and websockets only.

---

## GOLDM status

### What was checked

Instrument searched and resolved:

- `MCX:GOLDM26MAYFUT`
- token `124881671`

### Result

The symbol resolves correctly, but live data was not active at validation time:

- quote response returned `missing`
- websocket tick snapshot returned no quote payload for GOLDM
- websocket candle snapshot returned:
  - `current: null`
  - `is_stale: true`

### Interpretation

This does **not** mean the SDK is broken.

It means the backend/runtime did not currently have active usable market data for that instrument at the time of validation.

NIFTY websocket/live data validation succeeded, so the websocket path itself is working.

---

## Generic detached worker backend protection

### Relevant files

- `api/worker_protection.py`
- `api/worker_protection_runtime.py`
- `api/routers/algo_workers.py`
- `main.py`

### What it currently supports

- position stoploss / target / trailing based on percent
- basket stoploss / target / trailing based on percent
- worker stale exit
- MIS squareoff buffer

### Tests run

Focused protection tests passed:

- `tests/test_worker_protection.py`
- `tests/test_worker_protection_runtime.py`

Result:

- `20 passed`

Also validated via SDK that `backend_protection` can be declared on run creation and is persisted into:

- `runtime_state.backend_protection`
- `runtime_state.backend_protection_state`

### Architectural conclusion

Backend-owned detached-worker protection is the right choice for this system.

Why:

- external workers can crash or disconnect
- frontend/browser presence must not be required for protection
- backend should remain the final safety owner

### What detached worker means here

If an external worker dies, that should **not** by itself break the generic worker protection path.

The real dependency is backend/runtime availability, not worker attachment.

---

## Existing options protection already in the repo

This was an important discovery.

The repo already contains meaningful options-specific protection logic. It is not starting from zero.

### Relevant files

- `strategies/option_strategy/models.py`
- `strategies/option_strategy/compiler.py`
- `strategies/option_strategy/runtime.py`
- `strategies/modular/runtime_option_strategy.py`
- `strategies/modular/combined_premium_stoploss.py`

### Existing options-native metrics

Defined in the canonical options model:

- `INDEX_PRICE`
- `COMBINED_PREMIUM_POINTS`
- `BASKET_MTM_RUPEES`

### Existing options-native preference fields

- `index_lower_boundary`
- `index_upper_boundary`
- `combined_premium_target`
- `combined_premium_stoploss`
- `basket_mtm_target`
- `basket_mtm_stoploss`

### Focused tests run

Ran:

- `tests.algo_runtime.test_runtime_option_strategy_algo`
- `tests.algo_runtime.test_combined_premium_stoploss_algo`
- `tests.test_option_strategy_compiler`

Result:

- `19 tests passed`

---

## Precise options protection coverage map

### Strong coverage in canonical runtime-managed path

#### 1. Directional options

Examples:

- buy call
- sell put
- bull call spread
- bear put spread

Current protection style:

- index lower/upper boundary
- optional basket MTM stop/target

Assessment:

- good coverage
- sensible metric choice

#### 2. Neutral short premium structures

Examples:

- short straddle
- short strangle
- iron condor

Current protection style:

- combined premium target
- optional combined premium stoploss
- index emergency guards
- optional basket MTM stop/target

Assessment:

- good coverage for practical use
- tested precedence and duplicate-exit blocking behavior

#### 3. Long vol structures

Examples:

- long straddle
- long strangle

Current protection style:

- combined premium target
- combined premium stoploss
- optional basket MTM

Assessment:

- good coverage

### Partial coverage

#### 4. Mixed-expiry / premium-managed structures

Examples:

- calendar-ish or diagonal-ish manual structures
- premium-managed structures using rupee P&L as primary truth

Current protection style:

- basket MTM led

Assessment:

- usable
- not obviously rich in spread-structure-specific semantics

#### 5. More complex defined-risk multi-leg structures

Examples:

- butterflies
- broken-wing butterflies
- ratio spreads
- custom hedged structures

Current protection style:

- basket MTM
- combined premium
- index guards

Assessment:

- generic protection possible
- not clearly structure-aware beyond current metrics

### Lagging / weak coverage

#### 6. Repair / roll / hedge workflows

Examples:

- rolling short legs
- hedging instead of exiting
- partial de-risking
- custom adjustment workflows

Current behavior:

- runtime mainly emits exit baskets

Assessment:

- weak for advanced adjustment workflows

#### 7. Greeks / IV aware protections

Examples:

- delta/gamma/vega limits
- IV spike/crush logic

Assessment:

- not meaningfully present

#### 8. Legacy options protection records / older paths

Feature docs indicate:

- legacy protection records are still separate from canonical option strategy system

Assessment:

- canonical path is stronger
- legacy path is not fully unified

---

## Honest conclusion on options protection

### It is not weak

For the canonical runtime-managed options path, the current options protection system is already meaningful and useful.

### It is not universal

It does not yet cleanly cover every advanced options strategy or adjustment workflow.

### Best wording

The current options protection is:

- strong for canonical runtime-managed options
- good for practical strategy monitoring and exits
- weaker for legacy paths and advanced repair/adjustment semantics

---

## OpenAlgo comparison

### Selective sources used

Local code inspection:

- `openalgo/openalgo-python-library/openalgo/base.py`
- `openalgo/openalgo-python-library/openalgo/orders.py`
- `openalgo/openalgo-python-library/openalgo/feed.py`
- `openalgo/openalgo-python-library/openalgo/strategy.py`
- `openalgo/openalgo-python-library/examples/supertrend_strategy.py`

Public docs selectively scraped:

- `https://docs.openalgo.in/trading-platform/python/strategy-management`
- `https://docs.openalgo.in/strategy-management`

### OpenAlgo model

OpenAlgo is much more:

- script-driven
- webhook-driven
- thin-client oriented
- simpler operationally

Typical shape:

- user Python script or external platform decides signals
- OpenAlgo server receives webhook/API call
- OpenAlgo routes execution

The docs explicitly describe strategy management around:

- webhook IDs
- configured symbols
- trading direction modes
- square-off scheduling
- signal-driven order execution

### This repo’s model

This repo is much more:

- backend-owned
- run-scoped
- mode-aware (`dry_run`, `paper`, `live`)
- protection-aware
- detached-worker-safe

Typical shape:

- backend owns market, run, execution, attribution, and protection boundaries
- workers call backend-safe APIs
- backend remains the final safety owner

### Honest tradeoff

#### OpenAlgo is better if you want:

- very fast scripting
- simpler mental model
- less platform machinery
- webhook-centric strategy workflows

#### This repo is better if you want:

- detached workers
- backend-owned safety
- cleaner paper/live/dry_run separation
- run-scoped P&L/funds/orders
- more production-governed execution

### Honest recommendation

For the product direction here, this repo’s architecture is the better long-term choice.

The main risk is not that the direction is wrong.
The main risk is fragmented protection/runtime ownership across:

- generic worker backend protection
- canonical options runtime-managed protection
- older legacy option paths

---

## What we are currently thinking to do

### Strategic lean

We are leaning toward:

1. keeping the current backend-owned protection philosophy
2. keeping detached worker support as a first-class design goal
3. treating the Python SDK as mature enough to move forward
4. focusing the next options work on the canonical runtime-managed path
5. avoiding expansion of legacy option protection side paths

### Practical next priorities

Most likely next useful work:

1. map canonical options coverage vs legacy options paths explicitly
2. decide whether options protection should be unified more clearly with generic worker protection or intentionally remain a separate canonical options runtime layer
3. validate the canonical options protection path more deeply in live or controlled paper scenarios
4. only then expand options strategy breadth further

### Important caution

Do not assume every options strategy in the repo currently benefits from the same protection maturity.

The canonical runtime-managed path appears strongest.

---

## Final honest take

### SDK

Good shape and ready to use.

### Generic worker protection

Good design and worth keeping.

### Options protection

Already real and useful, especially in the canonical runtime-managed path.

### Main remaining concern

Unification and confidence across all paths, not the basic architecture itself.
