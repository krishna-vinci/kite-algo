# Journal V2 Developer Guide

Date: 2026-05-01
Status: backend production-ready, frontend aligned for iterative UX refinement

## Purpose

Journal V2 is the canonical trading memory layer for Kite Algo. It enforces environment-safe storage and queries, durable strategy identity, episode-based analytics, and Markdown-first notes/timeline history.

This guide is the production handoff reference for backend contracts and frontend alignment.

## Core concepts

### Environment

Hard partition for trading truth (`live`, `paper`, `dry_run_preview`) with explicit `account_scope` and optional `environment_epoch`.

- `live` and `paper` data are never mixed by default.
- `dry_run_preview` is preview-only and not executed performance.

### Template

Durable strategy identity (`strategy_template`) used for canonical grouping across renames and label changes.

### Variant

Scenario/config identity (`strategy_variant`) under a template (`scenario_key`, `config_hash`, etc.) so distinct scenario behavior is not merged.

### Deployment

Configured strategy instance (`strategy_deployment`) that links template+variant into a concrete execution setup.

### Execution context

`journal_execution_contexts` represent run/session handles for a specific environment and source system, keyed by `(environment_id, source_system, external_run_id)`.

### Episode

Canonical review/analytics unit (`journal_episodes`) representing an entry-to-flat trade lifecycle. Episode sequencing and status transitions are used instead of per-fill grouping.

### Intent

Execution command/narrative object (`journal_execution_intents`) for entry, adjustment, exit, protection, and manual actions.

### Timeline event

Immutable event records (`journal_timeline_events`) for lifecycle/note/backfill/provenance narrative by `occurred_at` and stable ordering.

### Markdown note

User-authored memory (`journal_notes`) where `body_markdown` is canonical, `body_text` is derived for search, and `journal_note_revisions` preserve edit history.

### Metric snapshot

Versioned analytics record (`journal_metric_snapshots`) scoped by environment and subject with identity/grouping rule versions and calculation version.

### Unresolved item

Low-confidence identity/activity queue entry (`journal_unresolved_queue`) retained for operator review when canonical attribution is unsafe.

## Backend API contracts (Journal V2)

All routes are under `/api/journal/v2` and require app auth.

## Environments

### `GET /api/journal/v2/environments`

Optional query: `mode`

Example response:

```json
{
  "items": [
    {
      "id": "8f0e84a0-ae8a-49ee-bf7f-f3ec4f3f8c8b",
      "mode": "paper",
      "account_scope": "kite:paper-e2e",
      "display_name": "Paper E2E",
      "broker_user_id": null,
      "paper_account_key": "kite:paper-e2e",
      "environment_epoch": 1,
      "metadata": {}
    }
  ]
}
```

## Episodes

### `GET /api/journal/v2/episodes`

Required environment context:

- `environment_id`, or
- `mode + account_scope`

Optional: `execution_context_id`, `status`, `limit`, `offset`

Example response:

```json
{
  "items": [
    {
      "id": "f56e5ad7-d4fd-4994-a8be-70509ce50cc7",
      "environment_id": "8f0e84a0-ae8a-49ee-bf7f-f3ec4f3f8c8b",
      "execution_context_id": "7b8e16ab-d9f7-492c-8596-4feec6ea57a8",
      "episode_seq": 1,
      "status": "open",
      "opened_at": "2026-05-01T10:00:00+00:00",
      "closed_at": null,
      "metadata": {}
    }
  ],
  "count": 1
}
```

### `GET /api/journal/v2/episodes/{episode_id}`

Returns episode detail for a known ID, `404` if missing.

### `GET /api/journal/v2/episodes/{episode_id}/timeline`

Optional: `limit`, `offset`

Example response:

```json
{
  "items": [
    {
      "id": "87d84a7f-7512-4566-9d71-53e6ed8b9446",
      "environment_id": "8f0e84a0-ae8a-49ee-bf7f-f3ec4f3f8c8b",
      "episode_id": "f56e5ad7-d4fd-4994-a8be-70509ce50cc7",
      "execution_context_id": "7b8e16ab-d9f7-492c-8596-4feec6ea57a8",
      "subject_type": "episode",
      "subject_id": "f56e5ad7-d4fd-4994-a8be-70509ce50cc7",
      "event_type": "episode_opened",
      "channel": "entry",
      "actor_type": "system",
      "correlation_id": null,
      "causation_id": null,
      "occurred_at": "2026-05-01T10:00:00+00:00",
      "payload": {}
    }
  ],
  "count": 1
}
```

## Strategy/unresolved reads

### `GET /api/journal/v2/strategies`

Required environment context: `environment_id` or `mode + account_scope`.

### `GET /api/journal/v2/unresolved`

Required environment context: `environment_id` or `mode + account_scope`.

Example response:

```json
{
  "environment_id": "8f0e84a0-ae8a-49ee-bf7f-f3ec4f3f8c8b",
  "count": 1,
  "items": [
    {
      "id": "22529f64-4fef-41d5-a5a7-0817f5d2e868",
      "environment_id": "8f0e84a0-ae8a-49ee-bf7f-f3ec4f3f8c8b",
      "execution_context_id": null,
      "source_system": "algo_worker",
      "reason": "missing_template_id_strategy_name_only",
      "raw_identity": { "strategy_name": "Legacy Name" },
      "candidate_mappings": [],
      "metadata": { "resolution_confidence": "0.50" },
      "status": "open",
      "created_at": "2026-05-01T10:00:00+00:00",
      "resolved_at": null
    }
  ]
}
```

## Notes and attachments

### `GET /api/journal/v2/notes`

Required: `environment_id`

Optional: `subject_type`, `subject_id`, `episode_id`, `note_type`, `limit`, `offset`

### `POST /api/journal/v2/notes`

Example request:

```json
{
  "environment_id": "8f0e84a0-ae8a-49ee-bf7f-f3ec4f3f8c8b",
  "subject_type": "episode",
  "subject_id": "f56e5ad7-d4fd-4994-a8be-70509ce50cc7",
  "episode_id": "f56e5ad7-d4fd-4994-a8be-70509ce50cc7",
  "note_type": "thesis",
  "title": "Entry thesis",
  "body_markdown": "# Plan\n- Risk\n- Exit",
  "tags": ["breakout"],
  "metadata": {}
}
```

### `PATCH /api/journal/v2/notes/{note_id}`

Required note boundary fields: `environment_id`, `subject_type`, `subject_id`.

Example request:

```json
{
  "environment_id": "8f0e84a0-ae8a-49ee-bf7f-f3ec4f3f8c8b",
  "subject_type": "episode",
  "subject_id": "f56e5ad7-d4fd-4994-a8be-70509ce50cc7",
  "title": "Updated thesis",
  "change_reason": "clarified exit triggers"
}
```

### `GET /api/journal/v2/notes/{note_id}`

Returns current note head.

### `GET /api/journal/v2/notes/{note_id}/revisions`

Returns immutable revision history.

### `POST /api/journal/v2/attachments`

Example request:

```json
{
  "environment_id": "8f0e84a0-ae8a-49ee-bf7f-f3ec4f3f8c8b",
  "subject_type": "episode",
  "subject_id": "f56e5ad7-d4fd-4994-a8be-70509ce50cc7",
  "storage_key": "attachments/entry-chart.png",
  "mime_type": "image/png",
  "note_id": "ad7f98cb-ba52-4f73-9f71-8f42ef5db8ea"
}
```

## Analytics

### `GET /api/journal/v2/analytics/summary?environment_id=...`

Returns environment-scoped episode-based metrics.

### `GET /api/journal/v2/analytics/strategies?environment_id=...`

Returns environment-scoped strategy scorecards.

### `GET /api/journal/v2/analytics/compare-paper-live?template_id=...&paper_environment_id=...&live_environment_id=...`

Returns side-by-side paper/live payload and `combined: null`.

Example response:

```json
{
  "template_id": "tmpl-1",
  "paper_environment_id": "00000000-0000-4000-8000-000000000001",
  "live_environment_id": "00000000-0000-4000-8000-000000000002",
  "paper": { "closed_episode_count": 10, "net_pnl": 1200.0 },
  "live": { "closed_episode_count": 4, "net_pnl": 300.0 },
  "combined": null
}
```

## Safety rules (non-negotiable)

1. Never query performance without environment context.
2. Never group by strategy name alone.
3. Never combine paper/live P&L by default.
4. Dry-run preview data is not executed performance.

## Frontend alignment and handoff status

The frontend has been aligned to Journal V2 data boundaries and basic flows. Final UX decisions for editor behavior, episode page layout, note templates, analytics visualizations, and unresolved workflow should be completed with trader/developer feedback before treating the UI as final.

Current alignment level:

- Environment selector and context-first fetch patterns exist.
- Episode list/detail, notes archive/editor, unresolved listing, and analytics shells are wired to V2 APIs.
- API helper guards enforce explicit `environment_id` for V2 performance/list paths.
- Paper/live comparison rendering remains side-by-side with no combined default.

## Implementation status summary

- Tasks 1–22 delivered backend and alignment features for schema, identity, episodes, notes, timeline, projection, metrics, analytics, unresolved queue, scripts, and frontend contract alignment.
- This handoff (Task 23) documents production contracts and final UX-iteration expectations.
- Final verification pass (Task 24) must be used as release gate for this implementation batch.
