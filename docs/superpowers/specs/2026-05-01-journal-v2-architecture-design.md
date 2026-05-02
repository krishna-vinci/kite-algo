# Journal V2 Architecture Design

Date: 2026-05-01
Status: Approved direction, written for implementation planning

## Objective

Rebuild the trading journal roadmap around a production-grade, long-term trading memory system.

Journal V2 must preserve trading history for years, support serious review and research workflows, and produce trustworthy analytics without mixing simulated and real trading truth. It should not be centered on a shallow rules dashboard or a single freeform review note. It should be centered on durable identity, strict environment isolation, coherent entry-to-exit narratives, advanced notes, and recomputable analytics.

The three implementation phases are:

1. **Foundation** — isolation, identity, episodes, and execution intents.
2. **Memory layer** — rich Markdown notes, timeline, attachments, search, and intuitive review UX.
3. **Analytics** — episode-based metrics, strategy scorecards, paper-vs-live comparison, and legacy migration.

## Product framing

Journal V2 is the user-facing trading memory and analytics layer for live, paper, SDK-driven, option-strategy, discretionary, and future investment workflows.

Its purpose is to answer questions such as:

- What exactly happened in this trade or strategy cycle?
- Why was it entered, adjusted, and exited?
- Was this paper, live, or only a dry-run preview?
- Which strategy template and scenario produced it?
- Did paper performance translate to live performance?
- Which setup, scenario, deployment, or account is improving or degrading?
- What did I learn, and can I retrieve that lesson years later?

The journal should become a durable archive, not a temporary dashboard.

## Non-negotiable invariants

### 1. Paper and live must never mix accidentally

Paper and live are separate truth systems.

Journal V2 must not silently combine:

- live and paper P&L
- different live broker accounts
- different paper accounts/scopes
- pre-reset and post-reset paper account history
- dry-run previews with executed activity

Any cross-mode or cross-account comparison must be explicit and side-by-side.

### 2. Strategy names are labels, not identity

`strategy_name` is useful for display and search, but unsafe as a grouping key.

Canonical grouping must use stable identity layers such as:

- strategy template
- scenario / variant
- deployment
- execution context
- episode sequence
- environment

Names may change over time without rewriting historical truth.

### 3. Raw facts stay immutable; grouping is versioned

Raw execution facts, source payloads, and note revisions must be preserved.

Resolved strategy identity and grouping assignments should be derived, versioned, and recomputable. If grouping logic improves later, historical facts should not be overwritten or corrupted.

### 4. Analytics are episode-based, not fill-row based

Win rate, expectancy, profit factor, average win/loss, and similar trade-quality metrics must be computed from closed journal episodes, not raw order fills.

Fill rows are execution facts. Episodes are review and analytics units.

### 5. Rich editing experience, Markdown as canonical storage

The note-taking UX should feel rich-text and intuitive, but Markdown should be the durable source of truth.

The canonical note body is `body_markdown`. Search helpers and editor-specific JSON can be derived or optional.

## Current-state assessment

The current journal implementation has useful foundations:

- `journaling/models.py` defines journal runs, source links, execution facts, decision events, rules, equity points, metric snapshots, and benchmark definitions.
- `journaling/service.py` supports run creation, source linking, paper trade recording, review updates, summaries, trades, strategies, rules, insights, and benchmark comparisons.
- `api/routers/journal.py` exposes core journal endpoints.
- Paper runtime can link paper orders/trades into journal runs and execution facts.
- Live projection work can record live fills into journal execution facts or imported buckets.
- Frontend journal routes and components exist for overview, calendar, trades, strategies, rules, insights, and review drawer.

However, the current design is not sufficient for the target system:

- `journal_runs` is overloaded. It can mean strategy run, review unit, imported bucket, or option lifecycle.
- Reads can default to mixed paper/live/account data because environment identity is not mandatory.
- Strategy rollups are still too close to `strategy_family + strategy_name`.
- Notes are mostly `review_notes` in metadata plus decision-event summaries.
- Rule evidence and advanced review workflows are not central enough to the archive.
- Summary analytics are too dependent on execution fact cash-flow rows.
- Frontend types and pages do not consistently expose execution mode, account/environment, template, deployment, or episode identity.

Journal V2 should be additive and migration-friendly, but it must correct these architectural weaknesses.

## Canonical model

### Entity graph

Journal V2 should use this identity graph:

```text
strategy_template
  -> strategy_variant
    -> strategy_deployment
      -> strategy_execution_context
        -> journal_episode
          -> journal_episode_leg
          -> execution_intent
            -> journal_execution_fact

execution_environment
  -> strategy_execution_context
  -> journal_episode
  -> execution_intent
  -> journal_execution_fact
  -> journal_timeline_event
  -> journal_note / journal_attachment

journal_note
  -> journal_note_revision
  -> journal_attachment
```

The graph is conceptual. Strategy templates can be global across environments, while deployments, contexts, episodes, facts, notes, and metrics are environment-scoped. Some physical tables may be introduced incrementally, but these identities must remain distinct.

### `execution_environment`

Hard partition for trading truth.

Suggested fields:

- `environment_id`
- `mode` — `live`, `paper`, `dry_run_preview`
- `account_scope`
- `broker_user_id` nullable
- `paper_account_key` nullable
- `environment_epoch` nullable
- `workspace_id` nullable for future multi-user/workspace support
- `display_name`
- `created_at`
- `retired_at`

Environment examples:

- live broker account `XJJ446`
- paper account `kite:paper-e2e`, epoch 1
- paper account `kite:paper-e2e`, epoch 2 after reset
- dry-run preview context for a strategy template

`execution_mode` remains useful, but it is not enough. Every episode, fact, metric snapshot, note, and frontend query that affects trading truth must be environment-aware.

### `strategy_template`

Stable identity of strategy logic.

Suggested fields:

- `template_id`
- `family` — options, indicator, investment, discretionary, etc.
- `namespace` — internal, sdk, user, legacy
- `source_ref` — internal route key, SDK package/module/class/function, worker template id
- `display_name`
- `label_history_json`
- `template_version_policy`
- `created_at`

The worker API already requires `template_id`; Journal V2 should promote that into first-class journal identity.

### `strategy_variant`

Scenario/config identity below a template.

Suggested fields:

- `variant_id`
- `template_id`
- `scenario_key`
- `scenario_name`
- `config_hash`
- `symbol_scope_json`
- `parameter_fingerprint_json`
- `grouping_policy_version`

This prevents scenario A and scenario B under the same strategy template from collapsing into one bucket.

### `strategy_deployment`

Configured bot or strategy instance.

Suggested fields:

- `deployment_id`
- `template_id`
- `variant_id`
- `deployment_key`
- `environment_defaults_json`
- `parameter_snapshot_json`
- `created_by`
- `active_from`
- `active_to`

Deployments let the journal distinguish the same template run with different parameters or account placement.

### `strategy_execution_context`

Execution session or external run handle. Current `strategy_run_id` belongs here.

Suggested fields:

- `execution_context_id`
- `environment_id`
- `deployment_id`
- `external_run_id`
- `source_system` — algo_worker, option_strategy, manual, broker_import, paper_runtime, etc.
- `raw_identity_json`
- `resolved_identity_json`
- `resolution_method`
- `resolution_confidence`
- `identity_rule_version`
- `status`
- `started_at`
- `ended_at`

`external_run_id` should not be assumed globally unique across accounts, environments, or time.

### `journal_episode`

Canonical review and analytics unit.

For trading, one episode is a flat-to-flat position cycle. For investment activity, one episode is a rebalance/campaign cycle.

Suggested fields:

- `episode_id`
- `execution_context_id`
- `environment_id`
- `episode_seq`
- `episode_kind` — position_cycle, rebalance_cycle, campaign, imported_activity
- `status` — draft, opening, open, reducing, flat_pending_confirmation, closed, cancelled, unresolved
- `opened_at`
- `closed_at`
- `open_reason`
- `close_reason`
- `benchmark_id`
- `capital_basis_type`
- `capital_committed`
- `review_state`
- `identity_snapshot_json`
- `grouping_snapshot_json`
- `legacy_run_id` nullable for migration from existing `journal_runs`

Physical recommendation: add a new `journal_episodes` table and keep existing `journal_runs` as a compatibility/source table during migration. Existing `journal_runs` rows should be backfilled into `journal_episodes` with `episode_seq = 1`, `legacy_run_id = journal_runs.id`, and a confidence marker.

### `journal_episode_leg`

Per-instrument leg inside an episode.

Suggested fields:

- `leg_id`
- `episode_id`
- `instrument_token`
- `exchange`
- `tradingsymbol`
- `product`
- `leg_role` — primary, hedge, adjustment, holding
- `direction`
- `opened_quantity`
- `closed_quantity`
- `net_quantity`
- `lifecycle_state`
- `metadata_json`

Multi-leg strategies, rolls, hedges, and partial exits should be represented here.

### `execution_intent`

Common command/narrative unit for entry, adjustment, exit, protection, and manual intervention.

Suggested fields:

- `intent_id`
- `episode_id`
- `environment_id`
- `channel` — entry, adjustment, exit, protection, manual
- `intent_role` — open, scale_in, reduce, close, flip, hedge_add, hedge_remove
- `idempotency_key`
- `correlation_id`
- `causation_id`
- `request_payload_json`
- `source_system`
- `status`
- `created_at`
- `completed_at`

This lets operational entry and exit flows remain separate while the journal presents one coherent timeline.

### `journal_execution_fact`

Keep the current immutable execution fact concept, but make it episode-, intent-, leg-, and environment-aware.

Suggested additions/requirements:

- `episode_id`
- `intent_id` nullable for legacy/imported activity
- `leg_id`
- `environment_id`
- `position_effect` — open, add, reduce, close, flip
- `source_fact_key`
- `payload_json`

Existing `journal_execution_facts` can be extended rather than replaced.

## Paper/live/dry-run isolation

### Live

Live journal facts must derive from broker-backed truth and attributed intents/fills.

Requirements:

- anchored to `broker_user_id` and environment
- external/manual broker activity goes to imported or unresolved activity unless confidently attributable
- no live fact can be linked into a paper environment
- same `strategy_run_id` on two live accounts creates two execution contexts

### Paper

Paper journal facts must derive from the durable paper runtime and attributed paper lots/trades.

Requirements:

- anchored to paper account key and environment epoch
- paper reset creates a new environment epoch
- same paper scope and same symbol across multiple runs must not corrupt per-run truth
- ambiguous paper activity should become unresolved, not auto-merged
- paper metrics are included in journal only under paper environments

### Dry run

Dry-run activity is not executed activity.

Requirements:

- no canonical trading performance facts by default
- no P&L, win rate, expectancy, or performance totals mixed with paper/live
- optional future dry-run archive should use a preview/idea namespace separate from executed journal episodes

## Strategy identity and SDK grouping

### Required ingestion inputs

SDK and app callers should provide as much identity as possible:

- `template_id` required for modern SDK/worker strategy sources
- `strategy_family`
- `strategy_name` for display
- `scenario_key` optional but strongly recommended
- `scenario_name` optional
- `deployment_key` optional
- `config_hash` optional
- `strategy_run_id` / external run id
- `source_system`
- `tags`
- `entry_surface`
- `account_scope`
- code/source fingerprint when available
- raw metadata payload

### Resolution precedence

Identity resolution should follow this order:

1. explicit `template_id`
2. known internal source mapping
3. worker template id
4. deployment key
5. source/code fingerprint
6. alias registry
7. legacy heuristic

If confidence is low, do not force a canonical merge. Mark the record unresolved and keep it visible.

### Grouping rules

Group by:

- environment
- template
- variant/scenario
- deployment
- execution context
- episode

Do not group by:

- raw strategy name alone
- symbol alone
- tags alone
- generated timestamp/random suffixes
- display labels

### Versioned grouping

Every resolved identity should carry:

- `identity_rule_version`
- `grouping_rule_version`
- `resolution_method`
- `resolution_confidence`
- `ambiguous`

This allows future reclassification without mutating raw facts.

## Notes and timeline architecture

### Storage principle

The editor should feel rich-text, but Markdown is the durable source of truth.

Recommended fields:

- `body_markdown` canonical
- `body_text` generated for search
- `body_json` optional for rich editor state

Markdown is portable, searchable, diffable, and long-lived.

### `journal_timeline_event`

Immutable event stream for journal narrative.

Suggested fields:

- `event_id`
- `environment_id`
- `episode_id` nullable
- `execution_context_id` nullable
- `subject_type`
- `subject_id`
- `channel`
- `event_type`
- `actor_type`
- `correlation_id`
- `causation_id`
- `occurred_at`
- `payload_json`

Event types include:

- order_intent_created
- order_submitted
- fill_recorded
- risk_changed
- protection_triggered
- manual_intervention_detected
- external_exit_detected
- identity_resolved
- identity_reclassified
- review_state_changed
- note_created
- note_updated
- attachment_added

### `journal_note`

Editable current note head.

Suggested fields:

- `note_id`
- `environment_id`
- `subject_type`
- `subject_id`
- `episode_id` nullable
- `note_type`
- `title`
- `body_markdown`
- `body_text`
- `body_json` nullable
- `effective_at`
- `author_id`
- `tags_json`
- `metadata_json`
- `created_at`
- `updated_at`
- `archived_at`

### `journal_note_revision`

Immutable edit history.

Suggested fields:

- `revision_id`
- `note_id`
- `revision_no`
- `body_markdown`
- `body_text`
- `editor_id`
- `edited_at`
- `change_reason`

### `journal_attachment`

Attachments for screenshots, exported charts, broker statements, CSVs, and other evidence.

Suggested fields:

- `attachment_id`
- `environment_id`
- `subject_type`
- `subject_id`
- `note_id` nullable
- `storage_key`
- `mime_type`
- `sha256`
- `size_bytes`
- `ocr_text` nullable
- `metadata_json`
- `created_at`

### Note subject levels

Notes must attach to more than one trade/run.

Allowed subjects:

- episode
- execution context
- strategy template
- strategy variant
- strategy deployment
- trading day/session
- environment/account
- imported/unresolved activity

### Note types

Initial note types:

- thesis
- pre_entry_checklist
- risk_plan
- market_context
- execution_rationale
- adjustment_rationale
- exit_rationale
- post_exit_review
- lesson
- psychology
- experiment
- ops_bug
- strategy_improvement
- rule_candidate

Rules can exist later, but notes and timeline are the core archive.

## Frontend UX principles

Journal V2 must be intuitive and effective, not just technically correct.

### Global journal context

Every page must make the active context visible:

- mode
- environment/account
- date/window
- strategy identity when scoped

Mixed-mode totals should not be the default.

### Main workspace shape

Recommended routes:

- `/journal` — environment-scoped overview
- `/journal/episodes` — searchable episode list
- `/journal/episodes/[episodeId]` — full episode narrative
- `/journal/strategies` — template/variant/deployment scorecards
- `/journal/strategies/[templateId]` — strategy notebook and analytics
- `/journal/calendar` — trading day/session view
- `/journal/notes` — searchable archive
- `/journal/analytics` — advanced metrics/comparisons
- `/journal/unresolved` — attribution/grouping queue

Existing pages can be evolved into this structure rather than replaced at once.

### Episode detail page

The episode detail page is the main journal object.

It should show:

- identity header: mode, account, template, scenario, deployment, episode status
- P&L and risk summary
- legs and fills
- entry/adjustment/exit intent timeline
- notes grouped by type
- attachments/screenshots
- benchmark and episode metrics
- source links and audit facts
- unresolved warnings if attribution is uncertain

### Notes UX

The notes UI should support:

- Markdown shortcuts with rich preview
- templates for pre-entry and post-exit reviews
- quick note from episode timeline
- tags
- attachments
- revision history
- search
- keyboard-friendly editing
- autosave with explicit saved/error state

## Metrics architecture

### Subject levels

Metrics should exist at these levels:

- episode
- execution context
- strategy template
- strategy variant
- strategy deployment
- environment/account
- portfolio/workspace in the future

### Metric snapshot keys

Metric snapshots should include:

- `environment_id`
- `subject_type`
- `subject_id`
- `window`
- `calc_version`
- `identity_rule_version`
- `grouping_rule_version`

### Episode metrics

Examples:

- realized P&L
- gross P&L
- net P&L
- charges
- hold time
- R multiple when risk plan exists
- MAE/MFE when market data is available
- exit reason
- slippage
- intervention count
- entry quality and exit quality scores when reviewed

### Strategy metrics

Examples:

- closed episode count
- win rate
- average win
- average loss
- expectancy
- profit factor
- drawdown
- review completion rate
- paper-vs-live conversion gap
- live-only scorecard
- paper-only scorecard
- scenario comparison

### Environment/account metrics

Examples:

- daily NAV/equity curve
- realized/unrealized split
- benchmark comparison
- drawdown
- concentration
- strategy allocation
- mode-specific performance

### Benchmark policy

Benchmark comparison should align to the same subject window and capital basis.

Open-run and portfolio metrics should use NAV/equity series, not trade cash-flow approximation.

If data is insufficient, return explicit unsupported/null metric states.

## Edge cases and required behavior

### Partial exits

Remain in the same episode. Record exit-channel intents and reducing fills. Close only when flat and confirmed.

### Re-entry after flat

Create a new episode sequence under the same execution context if the bot/session continues.

### Flip trades

Close one episode and open the next episode at the same timestamp if needed.

### Renamed strategy

Keep the same template identity. Store name changes as label history/timeline events.

### Same name, different templates

Never merge. Template identity wins over display name.

### Copied SDK bot

If explicit template identity is shared intentionally, group under that template. If code fingerprint or deployment suggests a collision, flag for identity resolution.

### Manual intervention

Record timeline event. Attach to an episode only when attribution is safe. Otherwise route to unresolved/manual activity.

### External broker exit

Attach only if exactly one reducing candidate exists in the same environment/account. Otherwise leave unresolved.

### Paper account reset

Create a new environment epoch. Do not merge pre-reset and post-reset paper performance.

### Same paper scope, same symbol, multiple runs

Use strategy-aware lots and episode attribution. If run truth is stale or ambiguous, mark unresolved rather than guessing.

### Legacy data

Keep searchable. Backfill identity/environment where possible. Mark low-confidence rows as legacy/unresolved and exclude from canonical analytics until mapped.

## Three-phase implementation plan

### Phase 1 — Foundation: isolation, identity, episodes

Goal: prevent history corruption and establish the correct canonical units.

Scope:

- add `execution_environments`
- backfill live/paper environments from existing account scopes
- add environment-aware columns/indexes to journal tables
- fix source-link uniqueness and semantics so account/environment participates
- promote worker `template_id` into journal identity
- add strategy template/variant/deployment/execution-context entities
- add `journal_episodes` and backfill existing `journal_runs` into episode sequence 1
- add `execution_intents`
- update journal APIs to require or default to an explicit environment context
- update frontend to show mode/account chips and avoid mixed totals

Success criteria:

- same `strategy_run_id` on two accounts does not collide
- paper and live never mix in default summaries
- same strategy name across two templates stays separate
- dry-run is excluded from executed performance
- legacy rows are marked with confidence/migration status

### Phase 2 — Memory: notes, timeline, intuitive UX

Goal: make the journal useful every day and valuable years later.

Scope:

- add timeline events
- add notes with Markdown canonical storage
- add note revisions
- add attachments
- add note tags and search text
- build episode detail page
- build strategy notebook page
- build notes archive/search page
- add note templates for thesis, risk plan, adjustment, exit, review, lesson, psychology, and experiment
- support quick note creation from timeline events

Success criteria:

- every episode can show a coherent entry-to-exit story
- notes are searchable and typed
- edits preserve revision history
- notes can attach to episode, strategy, deployment, day/session, or environment
- paper/live context is visible on every note and episode page

### Phase 3 — Analytics: trustworthy metrics and comparisons

Goal: produce serious, mode-safe, long-term performance analytics.

Scope:

- rebuild episode-based metrics
- add environment/account NAV/equity series
- add strategy template/variant/deployment scorecards
- add paper-vs-live side-by-side comparison
- add benchmark-aligned returns from equity/NAV windows
- add grouping and metric versioning
- add unresolved identity queue and manual reclassification tools
- migrate/deprecate name-based rollups and fill-row win-rate approximations
- add backend/frontend tests for environment separation and episode analytics

Success criteria:

- win rate and expectancy are computed from closed episodes
- open positions do not distort entry cash-flow metrics
- live and paper comparisons are explicit side-by-side views
- strategy analytics survive renames and SDK naming inconsistencies
- old ambiguous data remains visible but does not pollute canonical analytics

## Migration strategy

Migration should be additive and reversible.

Recommended sequence:

1. add new tables/columns without deleting current journal tables
2. backfill execution environments
3. backfill execution contexts from source links, worker runs, option strategy runs, paper attribution, and live intents
4. map existing `journal_runs` to episode sequence 1
5. mark low-confidence legacy rows
6. dual-write new journal events/facts into V2 structures while preserving V1 reads
7. migrate frontend pages to V2 endpoints/context
8. deprecate mixed-mode and name-based endpoints after validation

## Out of scope for Journal V2 core

The following are not required for the three-phase core:

- AI-authored journal entries
- autonomous trading action based on journal notes
- full separate database per mode
- public social/community journal features
- rule/adherence engine as the main product surface
- complex ML pattern discovery before the substrate is correct

## Testing requirements

### Phase 1 tests

- live and paper environments cannot mix in summaries
- same `strategy_run_id` across two accounts creates two contexts
- same name across templates stays separate
- paper account reset creates a new epoch
- dry-run preview does not create performance facts

### Phase 2 tests

- note create/update revision history
- Markdown body and generated search text behavior
- attachment metadata storage
- timeline ordering and subject linking
- frontend episode detail empty/error/success states

### Phase 3 tests

- episode-based win/loss metrics
- partial exit and re-entry episode handling
- paper-vs-live side-by-side comparison
- benchmark alignment with insufficient-data null states
- legacy low-confidence rows excluded from canonical analytics

## Success criteria

Journal V2 is production-ready when:

- every journal fact, episode, note, and metric belongs to an explicit environment
- paper/live/dry-run separation is enforced in storage, APIs, and UI
- SDK strategies group by durable identity, not names
- entry and exit channels produce one coherent episode narrative
- notes are rich, typed, searchable, versioned, and attachable across subjects
- analytics are based on closed episodes and NAV/equity series where appropriate
- grouping and metric logic are versioned and recomputable
- legacy ambiguous data is preserved but does not corrupt canonical analytics

## Final design position

Journal V2 should not be a prettier version of the current journal dashboard.

It should be the durable memory layer of the trading system: environment-safe, identity-safe, episode-based, note-rich, and analytics-grade.
