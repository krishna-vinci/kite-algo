# Kite Algo Documentation

This folder is the public documentation entry point for the repository.

If you are new to the project, start with the curated files below before reading deeper implementation notes.

## Start here

| If you want to... | Read this first |
| --- | --- |
| Understand the product quickly | [`../README.md`](../README.md) |
| Start the stack correctly | [`../README.md#quick-start`](../README.md#quick-start) |
| Set a production-safe admin password hash | [`../README.md#production-auth-and-admin-password-hash`](../README.md#production-auth-and-admin-password-hash) |
| Understand the architecture and philosophy | [`platform-overview.md`](platform-overview.md) |
| Understand the folder layout and ownership boundaries | [`codebase-map.md`](codebase-map.md) |
| Build or review external strategy workers | [`algo-worker-sdk-guide.md`](algo-worker-sdk-guide.md) |

## Recommended reading path for developers

1. [`platform-overview.md`](platform-overview.md) — architecture, philosophy, and system flow
2. [`codebase-map.md`](codebase-map.md) — where to find code and how to change it safely
3. [`algo-worker-sdk-guide.md`](algo-worker-sdk-guide.md) — worker model, lifecycle, and SDK surface
4. Deeper subsystem references below

## Recommended reading path for traders and power users

1. [`../README.md`](../README.md) — product capabilities and quick start
2. [`platform-overview.md`](platform-overview.md) — how the platform is organized
3. [`live-paper-accounting-and-worker-live-execution.md`](live-paper-accounting-and-worker-live-execution.md) — execution modes, accounting, and live worker behavior

## Deeper references already in this repo

| Document | What it covers |
| --- | --- |
| [`kite-websocket.md`](kite-websocket.md) | Websocket market-runtime overview and re-architecture notes |
| [`live-paper-accounting-and-worker-live-execution.md`](live-paper-accounting-and-worker-live-execution.md) | Live/paper accounting and worker execution notes |
| [`kite-backend-progress.md`](kite-backend-progress.md) | Current backend progress tracker |
| [`unified-design-core.md`](unified-design-core.md) | Deep position-protection architecture |
| [`unified-design-implementation.md`](unified-design-implementation.md) | Implementation detail for the unified protection system |

## Public docs philosophy

The goal of this folder is not to mirror every internal plan. It is to give contributors and evaluators a clear starting map, then point them deeper only when needed.
