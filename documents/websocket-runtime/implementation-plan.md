# Websocket Market Runtime Implementation Plan

Last updated: 2026-04-05

## Goal

Build a Go market-runtime service and progressively replace the current fragmented Python websocket ownership with a single unified runtime.

## Design principles

- keep the external model unified
- keep Python business logic intact until new contracts are proven
- cut over lowest-risk pieces first
- remove duplicate ticker ownership as early as practical
- keep docs updated as part of the implementation, not afterward

## Implementation workflow for agents

For non-trivial websocket/runtime work:

1. update this file first when the next implementation step is not already explicit
2. make the code change against that recorded step
3. before commit, refresh this file so the current status and remaining work match the staged code

This keeps the implementation plan usable as a live handoff and commit-time checklist instead of a stale retrospective note.

## Phase 0 — documentation and contract lock

Done in planning:

- define the unified pooled model
- set shard soft limit to **2800**
- define no caller-facing priority system for first version
- create websocket-runtime docs folder

Exit criteria:

- this folder is the working source of truth
- the skill points future agents to these docs

## Phase 1 — Go runtime skeleton

Status: **In progress; initial scaffold now exists in `market-runtime/`**

Build the new service with:

- config loading
- Postgres or control-plane token watcher
- a single Kite websocket connection
- normalized tick model
- owner-based subscription registry
- mode aggregation
- Redis snapshot publishing
- health/status endpoint

Exit criteria:

- one hidden shard works reliably
- subscriptions can be set/replaced by owner
- normalized snapshots are visible in Redis

Implemented so far:

- new Go module at `market-runtime/`
- config loading and boot entrypoint
- internal HTTP control-plane endpoints
- owner-based subscription registry
- Redis snapshot/status/order-update publisher
- Postgres-backed system token lookup and token watcher
- broker websocket shard wrapper using `gokiteconnect/v4/ticker`

## Phase 2 — pooled sharding

Status: **Partially implemented in scaffold form**

Add:

- hidden shard manager for up to 3 connections
- lazy shard expansion
- stable token placement
- soft limit enforcement at **2800** per shard
- shard metrics and exhaustion reporting

Exit criteria:

- runtime can scale from 1 to 3 shards without caller awareness
- the service remains one logical market bus

Implemented so far:

- hidden shard allocator exists
- stable token assignment exists
- **2800** token soft-limit enforcement exists
- shard-aware status reporting exists

Still needed:

- full live multi-shard verification under real subscriptions
- shard lifecycle hardening under reconnect/rotation pressure
- cutover consumers onto the new runtime

## Phase 3 — marketwatch cutover

Status: **Implemented; Go marketwatch websocket is the only live marketwatch path**

Move the current marketwatch websocket fanout to the Go runtime.

Goals:

- frontend clients subscribe to Go runtime websocket instead of Python marketwatch ticker fanout
- same-token dedupe is verified
- snapshot + tick delivery semantics are documented

Exit criteria:

- marketwatch no longer depends on Python `WebSocketManager` for broker websocket ownership

Implemented so far:

- Go runtime exposes the primary marketwatch websocket endpoint at `/ws/marketwatch`
- frontend can target the Go runtime directly with `VITE_MARKET_RUNTIME_WS_URL`
- obsolete Python market-runtime proxy helpers were removed from `api/routers/marketwatch.py`
- the Python `/api/ws/marketwatch` route now rejects legacy callers and points them to the Go runtime endpoint

Still needed:

- live end-to-end verification against a running market-runtime service with real broker connectivity
- performance tuning for high fanout / many subscribed clients
- repeat the >2000 token load test with valid live broker instruments during market hours for a true throughput profile

## Phase 4 — candle aggregation cutover

Status: **Implemented in backend runtime mode; direct Python candle websocket ownership retired**

Move candle aggregation to consume the normalized Go tick stream.

Preferred outcome:

- candle aggregation lives inside the Go runtime or directly beside it using the same normalized feed
- no second direct `KiteTicker` remains for candle generation

Exit criteria:

- `broker_api/candle_aggregator.py` no longer owns a broker websocket connection

Implemented so far:

- `broker_api/candle_aggregator.py` now supports a `market_runtime` source when `MARKET_RUNTIME_ENABLED=true`
- in runtime mode the aggregator:
  - subscribes through market-runtime owner subscriptions
  - consumes normalized ticks from Redis `market:ticks`
  - refreshes its owner lease periodically
  - avoids direct `KiteTicker` ownership
- runtime candle path now normalizes ISO-string exchange timestamps from the Go runtime correctly
- system-token rotation in `main.py` no longer restarts the aggregator when runtime mode is enabled
- Python startup now requires the Go runtime path instead of falling back to `WebSocketManager`

Still needed:

- deeper live verification of candle parity versus the old direct websocket path
- optional relocation of the candle aggregation logic fully into Go if we want to remove the Python compatibility layer later

## Phase 5 — order update relay cutover

Status: **Implemented enough for Python websocket retirement**

Move websocket order-update capture into the Go runtime.

Keep in Python:

- canonical ingest
- dedupe
- downstream order runtime processing
- trade sync and reconciliation

Exit criteria:

- Go reliably relays order websocket updates
- Python remains the truth-processing layer

Implemented so far:

- Go runtime publishes websocket order updates to `market:order_updates`
- Python `MarketDataRuntime` bridge ingests those relayed updates into canonical order-runtime processing
- order update enable/disable/status endpoints now target the runtime bridge instead of Python `WebSocketManager`

## Phase 6 — Python consumer migration

Status: **Implemented for the remaining websocket-dependent backend consumers**

Adapt these modules to runtime contracts instead of raw websocket internals:

1. real-time positions tick input
2. options sessions
3. alerts engine
4. position protection engine

Required changes:

- stop direct `latest_ticks` coupling where practical
- stop all raw `kws.subscribe/unsubscribe/set_mode` usage
- use owner-based subscriptions
- read normalized latest tick snapshots from runtime-backed contract

Exit criteria:

- Python business modules consume the runtime, not broker websocket internals

Implemented so far:

- alerts engine now reads runtime tick cache and owns runtime subscriptions through a backend owner id
- position protection / index stoploss engine now reads runtime tick cache and owns runtime subscriptions through a backend owner id
- options sessions now converge subscriptions through the runtime and read cached runtime ticks instead of `latest_ticks` from `WebSocketManager`
- real-time positions now receive live ticks from the runtime bridge and maintain dedicated runtime subscriptions for tracked open-position tokens

## Phase 7 — retire old Python websocket ownership

Status: **Implemented in startup/runtime flow; live verification still pending**

After parity is proven:

- remove or heavily shrink `broker_api/websocket_manager.py`
- remove direct broker websocket handling from Python startup
- remove duplicate websocket ownership patterns

Exit criteria:

- Go runtime is the only broker websocket owner

Implemented so far:

- Python startup no longer creates or starts `broker_api/websocket_manager.py`
- Python marketwatch websocket fanout no longer serves live market data
- backend status endpoints now report runtime bridge state instead of Python websocket-manager state

## Verification checklist

- reconnect resubscribe correctness
- token rotation correctness
- shard expansion correctness
- same-token dedupe across many owners
- mode convergence correctness
- Redis snapshot freshness
- marketwatch tick parity
- candle parity
- order-update relay parity
- real-time position delta parity
- production memory baseline with compiled runtime image
- >2000 valid-token live subscription load profile

## Documentation maintenance rule

During implementation, every websocket/runtime change must also update this folder.

If the design evolves materially, add another markdown file instead of burying the reasoning in code diffs alone.
