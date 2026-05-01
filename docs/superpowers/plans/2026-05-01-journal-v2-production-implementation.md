# Journal V2 Production Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a production-ready Journal V2 system with strict paper/live isolation, durable SDK strategy identity, episode-based journaling, advanced Markdown notes, timeline archive, and trustworthy analytics, ending with backend-complete functionality plus frontend alignment and a developer guide for final UX iteration.

**Architecture:** Add Journal V2 as an additive layer beside the current journal: new environment, identity, episode, intent, timeline, notes, and metrics primitives are introduced without deleting V1 tables or routes. New write paths dual-link from live/paper/worker systems into V2 records, while V1 journal data is backfilled into V2 with confidence metadata. Frontend work in this plan is deliberately alignment-level: expose mode/account/identity safely, add routes/components that match the new backend contract, and document final interaction decisions for developer/user iteration.

**Tech Stack:** FastAPI, Python service/repository modules, Postgres via SQLAlchemy text queries/session factory, existing `schema.sql` bootstrap style, pytest/unittest backend tests, Next.js App Router, React, TypeScript, existing frontend API client and operator panel components.

---

## Source spec

Implement from:

- `docs/superpowers/specs/2026-05-01-journal-v2-architecture-design.md`

Keep this plan and the spec in sync when implementation changes architectural decisions.

## File structure map

### Backend schema and models

- Modify: `schema.sql` — add Journal V2 tables, indexes, constraints, and compatibility columns.
- Modify: `journaling/models.py` — add typed Pydantic models/enums for environments, identity, episodes, intents, timeline events, notes, revisions, attachments, and V2 metric snapshots.
- Create: `journaling/v2/__init__.py` — package exports.
- Create: `journaling/v2/environment.py` — environment resolution and mode/account partition helpers.
- Create: `journaling/v2/identity.py` — strategy identity resolution, grouping snapshots, confidence values, and SDK metadata normalization.
- Create: `journaling/v2/episodes.py` — episode lifecycle helpers and position-effect classification.
- Create: `journaling/v2/notes.py` — Markdown note normalization, text extraction, revision helpers, and note template constants.
- Create: `journaling/v2/metrics.py` — episode-based and environment-aware metric builders.

### Backend repository and service

- Modify: `journaling/repository.py` — add V2 persistence methods while keeping existing V1 methods stable.
- Modify: `journaling/service.py` — expose V2 orchestration methods and keep V1 compatibility paths.
- Create: `journaling/v2_repository.py` — optional focused repository wrapper if `journaling/repository.py` becomes too large during implementation.
- Create: `journaling/v2_service.py` — optional focused service wrapper if `journaling/service.py` becomes too large during implementation.
- Modify: `journaling/runtime.py` — run V2 projection/recompute helpers without breaking current benchmark/summary refresh.
- Modify: `journaling/live_projector.py` — include environment/context/episode resolution for live fills.
- Modify: `paper_runtime/service.py` — record V2 paper facts, intents, and timeline events with paper environment identity.
- Modify: `paper_runtime/run_state.py` — expose the strategy/run truth needed for episode lifecycle updates.
- Modify: `api/routers/algo_workers.py` — pass worker template/scenario/deployment metadata into Journal V2 and keep token/account boundaries intact.
- Modify: `algo_runtime/execution_attribution.py` — include V2 identity fields in canonical attribution payloads.
- Modify: `algo_runtime/account_scope.py` — expose reusable environment-mode/account key normalization.

### Backend APIs, scripts, and docs

- Modify: `api/routers/journal.py` — add V2 environment-scoped endpoints and keep V1 endpoints stable until frontend migration completes.
- Create: `scripts/backfill_journal_v2.py` — backfill environments, contexts, episodes, and confidence markers from existing data.
- Create: `scripts/recompute_journal_v2_metrics.py` — recompute V2 metrics by environment/subject/window/version.
- Create: `docs/journal-v2-developer-guide.md` — implementation and frontend handoff guide.
- Modify: `documents/kite-backend-progress.md` — record Journal V2 progress and remaining risk.

### Frontend alignment

- Modify: `frontend-next/lib/journal/types.ts` — add environment, identity, episode, intent, timeline, note, and V2 metric types.
- Modify: `frontend-next/lib/journal/api.ts` — add V2 API client helpers and avoid mixed-mode default requests.
- Modify: `frontend-next/app/(app)/journal/page.tsx` — show explicit environment/mode context and call V2-safe summary endpoints.
- Create: `frontend-next/app/(app)/journal/episodes/page.tsx` — environment-scoped episode list.
- Create: `frontend-next/app/(app)/journal/episodes/[episodeId]/page.tsx` — initial episode detail shell.
- Create: `frontend-next/app/(app)/journal/notes/page.tsx` — initial notes archive shell.
- Create: `frontend-next/app/(app)/journal/unresolved/page.tsx` — initial unresolved identity/activity queue shell.
- Create: `frontend-next/components/journal/environment-selector.tsx` — explicit mode/account/environment selection.
- Create: `frontend-next/components/journal/episode-timeline.tsx` — timeline display.
- Create: `frontend-next/components/journal/markdown-note-editor.tsx` — lightweight Markdown editor with preview-ready state.
- Create: `frontend-next/components/journal/journal-v2-dev-notice.tsx` — visible note that UX polish continues with developer/user review.

### Tests

- Create: `tests/journaling/test_v2_environment.py`
- Create: `tests/journaling/test_v2_identity.py`
- Create: `tests/journaling/test_v2_episodes.py`
- Create: `tests/journaling/test_v2_notes.py`
- Create: `tests/journaling/test_v2_metrics.py`
- Create: `tests/test_journal_v2_router.py`
- Create: `tests/test_journal_v2_projection.py`
- Modify: `tests/test_journal_paper_costs.py`
- Modify: `tests/test_live_journal_projector.py`
- Modify: `tests/test_algo_worker_api.py`
- Create: `frontend-next/tests/journal-v2-api.test.ts`
- Create: `frontend-next/tests/journal-v2-pages.test.tsx`

---

## Phase 1 — Foundation: isolation, identity, episodes

### Task 1: Add Journal V2 schema foundation

**Files:**
- Modify: `schema.sql`
- Test: `tests/journaling/test_v2_environment.py`

- [ ] Add schema objects for `journal_execution_environments`, `journal_strategy_templates`, `journal_strategy_variants`, `journal_strategy_deployments`, `journal_execution_contexts`, `journal_episodes`, `journal_episode_legs`, and `journal_execution_intents`.

  Required table constraints:

  ```sql
  -- journal_execution_environments
  mode TEXT CHECK (mode IN ('live', 'paper', 'dry_run_preview'))
  account_scope TEXT NOT NULL
  environment_epoch INTEGER NOT NULL DEFAULT 1
  UNIQUE (mode, account_scope, COALESCE(broker_user_id, ''), COALESCE(paper_account_key, ''), environment_epoch)

  -- journal_execution_contexts
  UNIQUE (environment_id, source_system, external_run_id)

  -- journal_episodes
  UNIQUE (execution_context_id, episode_seq)

  -- journal_execution_intents
  UNIQUE (environment_id, idempotency_key)
  ```

- [ ] Extend existing journal fact/snapshot tables with nullable V2 compatibility columns:

  ```sql
  ALTER TABLE public.journal_execution_facts
    ADD COLUMN IF NOT EXISTS environment_id UUID,
    ADD COLUMN IF NOT EXISTS episode_id UUID,
    ADD COLUMN IF NOT EXISTS intent_id UUID,
    ADD COLUMN IF NOT EXISTS position_effect TEXT;

  ALTER TABLE public.journal_metric_snapshots
    ADD COLUMN IF NOT EXISTS environment_id UUID,
    ADD COLUMN IF NOT EXISTS identity_rule_version TEXT NOT NULL DEFAULT 'v1_legacy',
    ADD COLUMN IF NOT EXISTS grouping_rule_version TEXT NOT NULL DEFAULT 'v1_legacy';
  ```

- [ ] Include `paper_strategy_run` in the journal source-link source type constraint if the existing constraint is explicit.

- [ ] Write a schema smoke test that inserts two environments with the same `account_scope` but different modes and verifies both are allowed.

  Test shape:

  ```python
  def test_execution_environment_uniqueness_separates_live_and_paper(db_session):
      live_id = insert_environment(db_session, mode='live', account_scope='kite:XJJ446', broker_user_id='XJJ446')
      paper_id = insert_environment(db_session, mode='paper', account_scope='kite:paper-e2e', paper_account_key='kite:paper-e2e')
      assert live_id != paper_id
  ```

- [ ] Run: `PYTHONPATH=. uv run pytest tests/journaling/test_v2_environment.py -q`

  Expected: environment schema tests pass.

### Task 2: Add typed V2 models and enums

**Files:**
- Modify: `journaling/models.py`
- Test: `tests/journaling/test_v2_environment.py`

- [ ] Add enums:

  ```python
  class JournalEnvironmentMode(str, Enum):
      LIVE = 'live'
      PAPER = 'paper'
      DRY_RUN_PREVIEW = 'dry_run_preview'

  class JournalEpisodeStatus(str, Enum):
      DRAFT = 'draft'
      OPENING = 'opening'
      OPEN = 'open'
      REDUCING = 'reducing'
      FLAT_PENDING_CONFIRMATION = 'flat_pending_confirmation'
      CLOSED = 'closed'
      CANCELLED = 'cancelled'
      UNRESOLVED = 'unresolved'

  class JournalIntentChannel(str, Enum):
      ENTRY = 'entry'
      ADJUSTMENT = 'adjustment'
      EXIT = 'exit'
      PROTECTION = 'protection'
      MANUAL = 'manual'
  ```

- [ ] Add Pydantic models for `JournalExecutionEnvironment`, `JournalStrategyTemplate`, `JournalStrategyVariant`, `JournalStrategyDeployment`, `JournalExecutionContext`, `JournalEpisode`, `JournalEpisodeLeg`, and `JournalExecutionIntent`.

- [ ] Ensure every V2 model includes `metadata: Dict[str, Any] = Field(default_factory=dict)` where extension data is expected.

- [ ] Add model validation tests for required environment/account fields and invalid mode rejection.

  Test shape:

  ```python
  def test_environment_model_rejects_empty_account_scope():
      with pytest.raises(ValueError):
          JournalExecutionEnvironment(mode='paper', account_scope='')
  ```

- [ ] Run: `PYTHONPATH=. uv run pytest tests/journaling/test_v2_environment.py -q`

  Expected: model validation tests pass.

### Task 3: Implement environment resolver

**Files:**
- Create: `journaling/v2/__init__.py`
- Create: `journaling/v2/environment.py`
- Modify: `algo_runtime/account_scope.py`
- Test: `tests/journaling/test_v2_environment.py`

- [ ] Implement `resolve_environment_key(...)` that maps mode + account metadata into a stable lookup key.

  Required behavior:

  ```python
  resolve_environment_key(mode='live', account_scope='kite:XJJ446', broker_user_id='XJJ446')
  # -> mode='live', account_scope='kite:XJJ446', broker_user_id='XJJ446', paper_account_key=None, epoch=1

  resolve_environment_key(mode='paper', account_scope='kite:paper-e2e')
  # -> mode='paper', account_scope='kite:paper-e2e', broker_user_id=None, paper_account_key='kite:paper-e2e', epoch=1
  ```

- [ ] Reject `paper` mode for live account scopes and reject `live` mode for paper account scopes using the existing account-scope parser.

- [ ] Add optional `environment_epoch` support for paper reset boundaries.

- [ ] Write tests for live, paper, dry-run preview, invalid paper/live mismatch, and paper epoch separation.

- [ ] Run: `PYTHONPATH=. uv run pytest tests/journaling/test_v2_environment.py -q`

  Expected: resolver tests pass.

### Task 4: Add V2 repository environment/context methods

**Files:**
- Modify: `journaling/repository.py`
- Optional create: `journaling/v2_repository.py`
- Test: `tests/journaling/test_v2_environment.py`

- [ ] Add `ensure_execution_environment(...)`.

  Required signature:

  ```python
  def ensure_execution_environment(
      self,
      *,
      mode: str,
      account_scope: str,
      broker_user_id: str | None = None,
      paper_account_key: str | None = None,
      environment_epoch: int = 1,
      display_name: str | None = None,
  ) -> str:
      ...
  ```

- [ ] Add `get_execution_environment(environment_id: str)` and `list_execution_environments(mode: str | None = None)`.

- [ ] Add `ensure_execution_context(...)` keyed by `(environment_id, source_system, external_run_id)`.

- [ ] Write repository tests that ensure the same external run id creates separate contexts under two environments.

  Test shape:

  ```python
  def test_same_external_run_id_separates_by_environment(repository):
      live = repository.ensure_execution_environment(mode='live', account_scope='kite:XJJ446', broker_user_id='XJJ446')
      paper = repository.ensure_execution_environment(mode='paper', account_scope='kite:paper-a', paper_account_key='kite:paper-a')
      live_ctx = repository.ensure_execution_context(environment_id=live, source_system='algo_worker', external_run_id='run-1')
      paper_ctx = repository.ensure_execution_context(environment_id=paper, source_system='algo_worker', external_run_id='run-1')
      assert live_ctx != paper_ctx
  ```

- [ ] Run: `PYTHONPATH=. uv run pytest tests/journaling/test_v2_environment.py -q`

  Expected: repository environment/context tests pass.

### Task 5: Implement strategy identity resolver

**Files:**
- Create: `journaling/v2/identity.py`
- Modify: `journaling/models.py`
- Modify: `journaling/repository.py`
- Test: `tests/journaling/test_v2_identity.py`

- [ ] Define `ResolvedStrategyIdentity` with:

  ```python
  template_id: str
  strategy_family: str
  display_name: str
  variant_key: str | None
  deployment_key: str | None
  raw_identity: dict[str, Any]
  resolved_identity: dict[str, Any]
  resolution_method: str
  resolution_confidence: Decimal
  identity_rule_version: str = 'journal_v2_identity_v1'
  grouping_rule_version: str = 'journal_v2_grouping_v1'
  ambiguous: bool = False
  ```

- [ ] Implement precedence:

  1. explicit `template_id`
  2. known internal source mapping
  3. worker template id
  4. deployment key
  5. source/code fingerprint
  6. alias registry
  7. legacy heuristic

- [ ] Mark records ambiguous when only `strategy_name` is available.

- [ ] Persist templates, variants, and deployments through repository methods:

  ```python
  ensure_strategy_template(...)
  ensure_strategy_variant(...)
  ensure_strategy_deployment(...)
  ```

- [ ] Write tests for renamed strategy labels, same name across templates, and missing template id ambiguity.

- [ ] Run: `PYTHONPATH=. uv run pytest tests/journaling/test_v2_identity.py -q`

  Expected: identity resolver and repository tests pass.

### Task 6: Add episode lifecycle and intent primitives

**Files:**
- Create: `journaling/v2/episodes.py`
- Modify: `journaling/repository.py`
- Modify: `journaling/service.py`
- Test: `tests/journaling/test_v2_episodes.py`

- [ ] Add repository methods:

  ```python
  ensure_episode(...)
  update_episode_status(...)
  list_episodes(...)
  get_episode_detail(...)
  create_execution_intent(...)
  update_execution_intent_status(...)
  ```

- [ ] Implement episode sequencing:

  - first open cycle under a context gets `episode_seq = 1`
  - re-entry after flat gets `episode_seq = previous + 1`
  - partial exit remains same episode
  - flip closes current episode and creates next episode

- [ ] Implement `classify_position_effect(previous_qty, fill_side, fill_qty)`.

  Required outputs:

  ```python
  previous 0, BUY 10 -> open
  previous 10, BUY 5 -> add
  previous 10, SELL 4 -> reduce
  previous 10, SELL 10 -> close
  previous 10, SELL 15 -> flip
  ```

- [ ] Write tests for partial exit, full close, re-entry, and flip.

- [ ] Run: `PYTHONPATH=. uv run pytest tests/journaling/test_v2_episodes.py -q`

  Expected: episode lifecycle tests pass.

### Task 7: Wire worker SDK attribution into V2 identity/context

**Files:**
- Modify: `algo_runtime/execution_attribution.py`
- Modify: `api/routers/algo_workers.py`
- Modify: `journaling/service.py`
- Test: `tests/test_algo_worker_api.py`
- Test: `tests/journaling/test_v2_identity.py`

- [ ] Extend canonical attribution payloads to carry:

  ```python
  template_id
  strategy_family
  strategy_name
  scenario_key
  scenario_name
  deployment_key
  config_hash
  source_system='algo_worker'
  account_scope
  execution_mode
  ```

- [ ] On worker run creation, call Journal V2 context resolution after token/account access validation.

- [ ] Ensure V2 resolution failure never breaks order placement; store warning metadata and emit structured log.

- [ ] Add regression test: two worker runs with same `strategy_name` and different `template_id` produce separate strategy templates.

- [ ] Add regression test: live-bound token creating paper run uses paper environment, not live environment.

- [ ] Run: `PYTHONPATH=. uv run pytest tests/test_algo_worker_api.py tests/journaling/test_v2_identity.py -q`

  Expected: worker attribution tests pass.

### Task 8: Update journal API to require environment-safe reads

**Files:**
- Modify: `api/routers/journal.py`
- Modify: `journaling/service.py`
- Test: `tests/test_journal_v2_router.py`

- [ ] Add V2 endpoints:

  ```text
  GET /api/journal/v2/environments
  GET /api/journal/v2/episodes
  GET /api/journal/v2/episodes/{episode_id}
  GET /api/journal/v2/strategies
  GET /api/journal/v2/unresolved
  ```

- [ ] Require one of these query choices for summary/list endpoints:

  - `environment_id`
  - `mode` + `account_scope`

- [ ] Return `400` when a V2 performance endpoint is requested without environment context.

- [ ] Keep current V1 endpoints unchanged for compatibility.

- [ ] Write router tests for missing environment rejection and successful environment-scoped episode list.

- [ ] Run: `PYTHONPATH=. uv run pytest tests/test_journal_v2_router.py -q`

  Expected: V2 router tests pass.

---

## Phase 2 — Memory: notes, timeline, intuitive UX

### Task 9: Add timeline, notes, revisions, and attachments schema

**Files:**
- Modify: `schema.sql`
- Modify: `journaling/models.py`
- Test: `tests/journaling/test_v2_notes.py`

- [ ] Add tables:

  ```text
  journal_timeline_events
  journal_notes
  journal_note_revisions
  journal_attachments
  ```

- [ ] Make `body_markdown` the canonical note body.

- [ ] Include generated `body_text` for search.

- [ ] Add environment and subject columns to each note/timeline/attachment table.

- [ ] Add indexes:

  ```sql
  CREATE INDEX IF NOT EXISTS idx_journal_notes_environment_subject
    ON public.journal_notes (environment_id, subject_type, subject_id, updated_at DESC);

  CREATE INDEX IF NOT EXISTS idx_journal_timeline_episode_time
    ON public.journal_timeline_events (episode_id, occurred_at ASC);
  ```

- [ ] Add tests that insert a note and two revisions and verify revision order.

- [ ] Run: `PYTHONPATH=. uv run pytest tests/journaling/test_v2_notes.py -q`

  Expected: note schema tests pass.

### Task 10: Implement Markdown note service

**Files:**
- Create: `journaling/v2/notes.py`
- Modify: `journaling/repository.py`
- Modify: `journaling/service.py`
- Test: `tests/journaling/test_v2_notes.py`

- [ ] Implement `markdown_to_search_text(markdown: str) -> str` that removes Markdown markers and normalizes whitespace.

- [ ] Add repository/service methods:

  ```python
  create_note(...)
  update_note(...)
  list_notes(...)
  get_note(...)
  list_note_revisions(...)
  attach_file_metadata(...)
  ```

- [ ] On note update, insert a `journal_note_revisions` row before updating the current note head.

- [ ] Add note templates as constants:

  ```python
  NOTE_TEMPLATE_THESIS
  NOTE_TEMPLATE_RISK_PLAN
  NOTE_TEMPLATE_ADJUSTMENT
  NOTE_TEMPLATE_EXIT_REVIEW
  NOTE_TEMPLATE_LESSON
  NOTE_TEMPLATE_PSYCHOLOGY
  NOTE_TEMPLATE_EXPERIMENT
  ```

- [ ] Write tests for create, update, generated search text, revision history, and environment-scoped note listing.

- [ ] Run: `PYTHONPATH=. uv run pytest tests/journaling/test_v2_notes.py -q`

  Expected: Markdown note service tests pass.

### Task 11: Implement timeline event service

**Files:**
- Modify: `journaling/repository.py`
- Modify: `journaling/service.py`
- Test: `tests/journaling/test_v2_notes.py`

- [ ] Add `append_timeline_event(...)` and `list_timeline_events(...)`.

- [ ] Emit timeline events for:

  - execution context created
  - episode opened
  - intent created
  - fill recorded
  - episode closed
  - note created
  - note updated
  - identity reclassified

- [ ] Ensure timeline event write failures do not break trading paths; log and continue.

- [ ] Write tests that verify event ordering by `occurred_at ASC, event_id ASC`.

- [ ] Run: `PYTHONPATH=. uv run pytest tests/journaling/test_v2_notes.py -q`

  Expected: timeline event tests pass.

### Task 12: Add V2 notes and timeline API routes

**Files:**
- Modify: `api/routers/journal.py`
- Test: `tests/test_journal_v2_router.py`

- [ ] Add endpoints:

  ```text
  GET /api/journal/v2/episodes/{episode_id}/timeline
  GET /api/journal/v2/notes
  POST /api/journal/v2/notes
  GET /api/journal/v2/notes/{note_id}
  PATCH /api/journal/v2/notes/{note_id}
  GET /api/journal/v2/notes/{note_id}/revisions
  POST /api/journal/v2/attachments
  ```

- [ ] Validate that note create/update requests include `environment_id`, `subject_type`, and `subject_id`.

- [ ] Add tests for note create/update/list by episode and by strategy template.

- [ ] Run: `PYTHONPATH=. uv run pytest tests/test_journal_v2_router.py -q`

  Expected: V2 notes router tests pass.

### Task 13: Backfill V1 review notes into V2 notes

**Files:**
- Create: `scripts/backfill_journal_v2.py`
- Modify: `journaling/service.py`
- Test: `tests/test_journal_v2_projection.py`

- [ ] Implement script modes:

  ```text
  --dry-run
  --apply
  --limit N
  --environment-mode live|paper
  ```

- [ ] Backfill `journal_runs.metadata_json.review_notes` into V2 `journal_notes` with `note_type='post_exit_review'` and `metadata.source='v1_review_notes'`.

- [ ] Backfill existing decision events into timeline events.

- [ ] Mark backfilled records with `identity_rule_version='v1_legacy_backfill'` and `resolution_confidence` based on available source links.

- [ ] Test dry-run does not write and apply writes expected notes/timeline events.

- [ ] Run: `PYTHONPATH=. uv run pytest tests/test_journal_v2_projection.py -q`

  Expected: backfill tests pass.

### Task 14: Frontend type and API alignment for V2 memory layer

**Files:**
- Modify: `frontend-next/lib/journal/types.ts`
- Modify: `frontend-next/lib/journal/api.ts`
- Test: `frontend-next/tests/journal-v2-api.test.ts`

- [ ] Add TypeScript types:

  ```ts
  export type JournalEnvironment = { id: string; mode: 'live' | 'paper' | 'dry_run_preview'; account_scope: string; display_name: string };
  export type JournalEpisode = { id: string; environment_id: string; status: string; strategy_template_id: string | null; strategy_name: string; opened_at: string; closed_at: string | null };
  export type JournalTimelineEvent = { id: string; event_type: string; channel: string | null; occurred_at: string; payload: Record<string, unknown> };
  export type JournalNote = { id: string; environment_id: string; subject_type: string; subject_id: string; note_type: string; title: string; body_markdown: string; tags: string[]; updated_at: string };
  ```

- [ ] Add API helpers:

  ```ts
  fetchJournalEnvironments()
  fetchJournalEpisodes({ environment_id })
  fetchJournalEpisode(episodeId)
  fetchJournalTimeline(episodeId)
  fetchJournalNotes(params)
  createJournalNote(payload)
  updateJournalNote(noteId, payload)
  ```

- [ ] Add tests that verify V2 performance helpers require `environment_id` in params.

- [ ] Run inside `frontend-next`: `npm test -- journal-v2-api.test.ts`

  Expected: V2 API helper tests pass.

### Task 15: Add frontend V2 aligned shells

**Files:**
- Create: `frontend-next/components/journal/environment-selector.tsx`
- Create: `frontend-next/components/journal/episode-timeline.tsx`
- Create: `frontend-next/components/journal/markdown-note-editor.tsx`
- Create: `frontend-next/components/journal/journal-v2-dev-notice.tsx`
- Create: `frontend-next/app/(app)/journal/episodes/page.tsx`
- Create: `frontend-next/app/(app)/journal/episodes/[episodeId]/page.tsx`
- Create: `frontend-next/app/(app)/journal/notes/page.tsx`
- Test: `frontend-next/tests/journal-v2-pages.test.tsx`

- [ ] Build environment selector that always shows selected mode and account scope.

- [ ] Build episode list page that refuses to render performance numbers until an environment is selected.

- [ ] Build episode detail shell with identity header, timeline panel, note panel, and fills placeholder driven by API response fields.

- [ ] Build Markdown note editor with textarea, preview-ready state, save button, and explicit saved/error text.

- [ ] Add `JournalV2DevNotice` text:

  ```text
  Journal V2 backend primitives are active. Frontend interaction design is intentionally aligned to the new model and will be refined with trader/developer review.
  ```

- [ ] Write smoke tests for pages and environment selector.

- [ ] Run inside `frontend-next`: `npm test -- journal-v2-pages.test.tsx`

  Expected: V2 page smoke tests pass.

---

## Phase 3 — Analytics: trustworthy metrics and production hardening

### Task 16: Project live and paper fills into V2 episodes/facts

**Files:**
- Modify: `journaling/live_projector.py`
- Modify: `paper_runtime/service.py`
- Modify: `paper_runtime/run_state.py`
- Modify: `journaling/service.py`
- Test: `tests/test_live_journal_projector.py`
- Test: `tests/test_journal_paper_costs.py`
- Test: `tests/test_journal_v2_projection.py`

- [ ] Live fills: resolve environment from broker account, resolve execution context from strategy attribution, resolve/open episode, create intent/fact/timeline records.

- [ ] Paper fills: resolve environment from paper account key and epoch, resolve execution context from paper attribution, resolve/open episode, create intent/fact/timeline records.

- [ ] Preserve V1 `record_paper_trade` and live projection behavior during dual-write period.

- [ ] Add test: live fill and paper fill with same `strategy_run_id` create different V2 environments and different episodes.

- [ ] Add test: paper trade charges appear in V2 fact and episode metrics.

- [ ] Run: `PYTHONPATH=. uv run pytest tests/test_live_journal_projector.py tests/test_journal_paper_costs.py tests/test_journal_v2_projection.py -q`

  Expected: V2 projection tests pass without breaking V1 projection tests.

### Task 17: Harden paper lot consumption for strategy-aware exits

**Files:**
- Modify: `paper_runtime/service.py`
- Modify: `paper_runtime/run_state.py`
- Test: `tests/algo_runtime/test_paper_executor.py`
- Test: `tests/test_journal_v2_projection.py`

- [ ] Ensure paper lot consumption prefers lots matching the exiting episode/context attribution.

- [ ] If lots are ambiguous for same account/symbol/product, block strategy-level exit and return stale/unresolved reasons instead of consuming another episode's lots.

- [ ] Add regression: two paper episodes in same paper account trade same symbol; exiting one episode only reduces its attributed lots.

- [ ] Add regression: ambiguous legacy lots return blocked/unresolved status.

- [ ] Run: `PYTHONPATH=. uv run pytest tests/algo_runtime/test_paper_executor.py tests/test_journal_v2_projection.py -q`

  Expected: paper same-symbol and legacy ambiguity tests pass.

### Task 18: Implement episode-based metric builders

**Files:**
- Create: `journaling/v2/metrics.py`
- Modify: `journaling/repository.py`
- Modify: `journaling/service.py`
- Test: `tests/journaling/test_v2_metrics.py`

- [ ] Implement episode metric builder using closed episodes and facts attached to the episode.

- [ ] Metrics required for first production cut:

  ```text
  gross_pnl
  net_pnl
  total_charges
  realized_pnl
  hold_seconds
  closed_episode_count
  win_rate
  average_win
  average_loss
  expectancy
  profit_factor
  ```

- [ ] Do not compute win/loss from individual fills.

- [ ] Return explicit unsupported values for MAE/MFE and R-multiple until market/risk-plan inputs are present.

- [ ] Write tests proving one entry fill and one exit fill produce one episode outcome.

- [ ] Run: `PYTHONPATH=. uv run pytest tests/journaling/test_v2_metrics.py -q`

  Expected: episode metrics tests pass.

### Task 19: Add environment-aware strategy and account analytics

**Files:**
- Modify: `journaling/v2/metrics.py`
- Modify: `journaling/repository.py`
- Modify: `journaling/service.py`
- Modify: `api/routers/journal.py`
- Test: `tests/journaling/test_v2_metrics.py`
- Test: `tests/test_journal_v2_router.py`

- [ ] Add analytics endpoints:

  ```text
  GET /api/journal/v2/analytics/summary?environment_id=...
  GET /api/journal/v2/analytics/strategies?environment_id=...
  GET /api/journal/v2/analytics/compare-paper-live?template_id=...
  ```

- [ ] Require explicit environment for summary and strategies.

- [ ] For paper-vs-live comparison, return separate payloads:

  ```json
  {
    "template_id": "...",
    "paper": { "episode_count": 10, "net_pnl": 1200.0 },
    "live": { "episode_count": 4, "net_pnl": 300.0 },
    "combined": null
  }
  ```

- [ ] Store metric snapshots with environment, identity rule version, grouping rule version, and calc version.

- [ ] Write tests that mixed live/paper totals are not returned.

- [ ] Run: `PYTHONPATH=. uv run pytest tests/journaling/test_v2_metrics.py tests/test_journal_v2_router.py -q`

  Expected: environment-aware analytics tests pass.

### Task 20: Add unresolved identity/activity queue

**Files:**
- Modify: `schema.sql`
- Modify: `journaling/v2/identity.py`
- Modify: `journaling/repository.py`
- Modify: `journaling/service.py`
- Modify: `api/routers/journal.py`
- Create: `frontend-next/app/(app)/journal/unresolved/page.tsx`
- Test: `tests/journaling/test_v2_identity.py`
- Test: `tests/test_journal_v2_router.py`

- [ ] Add table or status projection for unresolved records containing raw identity, reason, environment, source system, candidate mappings, and created time.

- [ ] Route low-confidence identity resolutions into unresolved queue.

- [ ] Add endpoint:

  ```text
  GET /api/journal/v2/unresolved?environment_id=...
  ```

- [ ] Add read-only frontend page listing unresolved items with reason and raw strategy labels.

- [ ] Keep manual reclassification write endpoint out of this first production cut unless backend data review confirms exact operator workflow.

- [ ] Write tests for missing template id creating unresolved item.

- [ ] Run: `PYTHONPATH=. uv run pytest tests/journaling/test_v2_identity.py tests/test_journal_v2_router.py -q`

  Expected: unresolved queue tests pass.

### Task 21: Add V2 recompute and backfill scripts

**Files:**
- Create: `scripts/backfill_journal_v2.py`
- Create: `scripts/recompute_journal_v2_metrics.py`
- Test: `tests/test_journal_v2_projection.py`
- Test: `tests/journaling/test_v2_metrics.py`

- [ ] Backfill script supports:

  ```text
  --dry-run
  --apply
  --limit N
  --mode live|paper
  --account-scope VALUE
  ```

- [ ] Recompute script supports:

  ```text
  --environment-id UUID
  --subject-type episode|strategy_template|strategy_deployment|environment
  --subject-id VALUE
  --window since_inception|day|week|month|year
  --calc-version journal_v2_metrics_v1
  ```

- [ ] Script output must include counts for scanned, created, updated, unresolved, skipped, and failed.

- [ ] Add tests for dry-run no-write and apply write behavior.

- [ ] Run: `PYTHONPATH=. uv run pytest tests/test_journal_v2_projection.py tests/journaling/test_v2_metrics.py -q`

  Expected: script behavior tests pass.

### Task 22: Frontend analytics alignment

**Files:**
- Modify: `frontend-next/lib/journal/types.ts`
- Modify: `frontend-next/lib/journal/api.ts`
- Modify: `frontend-next/app/(app)/journal/page.tsx`
- Create: `frontend-next/app/(app)/journal/analytics/page.tsx`
- Modify: `frontend-next/app/(app)/journal/strategies/page.tsx`
- Test: `frontend-next/tests/journal-v2-pages.test.tsx`

- [ ] Update overview page to use explicit environment context and show a warning when no environment is selected.

- [ ] Add analytics page with three safe sections:

  - environment summary
  - strategy template scorecards
  - paper-vs-live comparison selector

- [ ] Ensure paper and live comparison appears as side-by-side columns and never a combined total.

- [ ] Add frontend test that the words `Combined P&L` are not rendered by the paper-vs-live component.

- [ ] Run inside `frontend-next`: `npm test -- journal-v2-pages.test.tsx`

  Expected: V2 frontend analytics alignment tests pass.

### Task 23: Developer guide and production handoff

**Files:**
- Create: `docs/journal-v2-developer-guide.md`
- Modify: `documents/kite-backend-progress.md`
- Modify: `docs/superpowers/specs/2026-05-01-journal-v2-architecture-design.md` if implementation materially changes the design

- [ ] Document Journal V2 concepts:

  - environment
  - template
  - variant
  - deployment
  - execution context
  - episode
  - intent
  - timeline event
  - Markdown note
  - metric snapshot
  - unresolved item

- [ ] Document backend API contracts with example payloads.

- [ ] Document frontend alignment status:

  ```text
  The frontend has been aligned to Journal V2 data boundaries and basic flows. Final UX decisions for editor behavior, episode page layout, note templates, analytics visualizations, and unresolved workflow should be completed with trader/developer feedback before treating the UI as final.
  ```

- [ ] Document safety rules:

  - never query performance without environment context
  - never group by strategy name alone
  - never combine paper/live P&L by default
  - dry-run preview data is not executed performance

- [ ] Update backend progress tracker with completed tasks and remaining frontend iteration notes.

### Task 24: Final verification and reviewer pass

**Files:**
- No new files required

- [ ] Run focused backend suite:

  ```bash
  PYTHONPATH=. uv run pytest \
    tests/journaling/test_v2_environment.py \
    tests/journaling/test_v2_identity.py \
    tests/journaling/test_v2_episodes.py \
    tests/journaling/test_v2_notes.py \
    tests/journaling/test_v2_metrics.py \
    tests/test_journal_v2_router.py \
    tests/test_journal_v2_projection.py \
    tests/test_journal_paper_costs.py \
    tests/test_live_journal_projector.py \
    -q
  ```

  Expected: all tests pass.

- [ ] Run nearby worker/paper regression suite:

  ```bash
  PYTHONPATH=. uv run pytest \
    tests/test_algo_worker_api.py \
    tests/algo_runtime/test_paper_executor.py \
    tests/test_worker_run_access.py \
    -q
  ```

  Expected: all tests pass.

- [ ] Run frontend checks:

  ```bash
  npm test -- journal-v2-api.test.ts journal-v2-pages.test.tsx
  npm run typecheck
  ```

  Expected: V2 frontend tests and typecheck pass.

- [ ] Run reviewer subagent with focus on:

  - paper/live contamination
  - account/environment scoping
  - SDK identity grouping
  - episode lifecycle edge cases
  - note revision durability
  - frontend mixed-mode defaults

- [ ] Fix blocking reviewer findings and rerun the affected checks.

---

## Commit guidance for implementers

Use small commits by task or tightly related task group. Recommended messages:

```text
feat: add journal v2 environment identity
feat: add journal v2 strategy identity resolution
feat: add journal v2 episodes and intents
feat: add journal v2 markdown notes and timeline
feat: project live and paper fills into journal v2
feat: add journal v2 episode analytics
feat: align journal frontend with v2 boundaries
docs: add journal v2 developer guide
```

Do not commit unrelated local docs/tests/untracked files unless they belong to the current task.

## Production-readiness checklist

- [ ] No performance endpoint silently mixes live and paper.
- [ ] No strategy rollup groups by `strategy_name` alone.
- [ ] Every V2 fact, episode, intent, note, and metric has an environment.
- [ ] Paper account reset creates a new environment epoch.
- [ ] Same external run id across accounts creates separate contexts.
- [ ] Dry-run preview data does not enter executed performance facts.
- [ ] Episode metrics are computed from closed episodes, not raw fills.
- [ ] Notes use Markdown canonical body and preserve revisions.
- [ ] Legacy/ambiguous data is visible but excluded from canonical analytics unless mapped.
- [ ] Frontend shows mode/account context and avoids combined paper/live totals.
- [ ] Developer guide explains the remaining frontend design iteration needed with trader/developer feedback.
