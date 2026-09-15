"""Hosted strategy foundation (schema + authorization only).

This package holds the durable store for platform-hosted Python strategies and
the fencing/validation primitives that must exist BEFORE any execution path:

- ``models``     — ORM tables (``hosted_strategies``/``_versions``/``_schedules``,
  ``strategy_jobs``) on the shared alerts/workflows ``Base``;
- ``service``    — pure validation: parameter JSON-Schema checks, immutable
  configuration snapshots, child token composition, id/name limits. It never
  imports or executes strategy source, never creates a run or a token.
- ``repository`` — CRUD, immutable version numbering, and lease-epoch/attempt
  fencing with a durable ``recovery_required`` state.

There is deliberately no runner, scheduler or notification code here yet.
"""

from __future__ import annotations

__all__ = ["models", "repository", "service"]
