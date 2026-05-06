# Websocket Market Runtime Docs

Last updated: 2026-04-05

This folder is the source of truth for the planned websocket re-architecture.

## Goal

Replace the current fragmented Python websocket ownership with a single **Go market-runtime service** that is:

- isolated from the main app process
- unified for all frontend and backend consumers
- robust under reconnects and token rotation
- simple for humans and LLMs to use correctly

## Current status

The Go market-runtime is now the only intended websocket owner.

- Python `WebSocketManager` startup has been retired
- backend consumers now target the runtime contract instead of raw broker websocket ownership
- legacy Python marketwatch websocket handling has been sunk in favor of direct Go runtime connections
- production compose now uses a compiled runtime binary instead of `go run`
- remaining work is now operational verification, live shard/load validation, and parity observation

Recent verification:

- hardened production runtime memory is now in the low tens of MiB instead of the earlier dev-style `go run` footprint
- a synthetic 2200-token owner test stayed healthy on one shard under the configured 2800 soft limit
- that synthetic owner test was later removed cleanly

## Core model

- the system exposes **one logical market-data service**
- callers never choose a Kite websocket connection directly
- the runtime may use **up to 3 hidden Kite websocket connections** internally
- each connection has a **soft limit of 2800 tokens**
- the broker hard limit remains **3000 tokens per connection** and **3 connections per API key**
- shard expansion is automatic and internal
- same-token subscriptions are globally deduplicated upstream
- highest requested mode per token is used upstream (`full > quote > ltp`)

## Current docs in this folder

- `spec.md` — architecture and operating model
- `contracts.md` — control-plane, Redis, and stream contracts
- `implementation-plan.md` — rollout plan and cutover order

## Maintenance rule

Whenever websocket or market-runtime behavior changes, update this folder.

That includes changes to:

- websocket ownership
- subscription semantics
- Redis key/channel schema
- runtime APIs
- reconnect or token-rotation behavior
- candle aggregation ownership
- order-update relay behavior

If one file is not enough, add more markdown files here instead of leaving the design only in code or chat history.
