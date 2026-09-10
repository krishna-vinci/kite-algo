# Workflow format — authoring contract (Phase 1 + Phase 2 + Phase 3)

The canonical definition is a JSON document; YAML is its human-editable representation. Files describe **what to monitor**; the API decides **what runs** (import always creates a draft; activation is an explicit API call). Any capability listed as unsupported fails validation with a named issue instead of being partially interpreted.

Status: implemented subset of [spec v2](../docs/superpowers/specs/2026-09-08-alerts-platform-spec-v2.md) — Phase 1 (F1–F6, F11) plus Phase 2 (F7 universes, F8 shared indicators and layered conditions). Schema version: `1`. Executable capabilities are discoverable at `GET /api/worker/workflows/capabilities`.

## Document shape

```yaml
version: 1
name: reliance-breakout            # unique per owner
instruments: ["NSE:RELIANCE"]      # "EXCHANGE:SYMBOL" shorthand or {symbol, exchange}
session: nse_equity                # nse_equity | mcx_commodity | currency
stages:
  - id: px                         # required; unique
    type: signal                   # Phase 1 supports "signal"
    clock: ltp                     # ltp | candle_close (alias: evaluate_on)
    timeframe: minute              # required iff clock: candle_close
    conditions:
      all:                         # AND-combined, up to 32
        - left:  {field: ltp}
          op: crosses_above
          right: {value: 3000}
alerts:
  - id: breakout                   # required; unique
    source: px                     # must reference a stage id
    trigger: once                  # once | on_transition (default) | once_per_session | reminder
    cooldown: 30m                  # duration (30m/1h) or seconds int
    rearm_below: 2985              # re-arm only after value passes this level
    notify_if_already_true: false  # default false: silent arming at activation
    expires: "2026-09-30T00:00:00+05:30"   # ISO-8601; alias: expires_at
    channels: [telegram_primary]   # channel names defined via the API
    message: "${symbol} broke ${level} at ${ltp} (${time})"   # optional template
data_policy:
  missing: exclude_and_report
  insufficient_history: wait
  require_closed_candles: true
```

`reminder_interval` (alias `reminder_interval_s`) is only valid with `trigger: reminder`; the example above uses `trigger: once`, so it carries none.

Session policies are market-segment-specific. `nse_equity` uses the imported
NSE-CM calendar. `mcx_commodity` is feed-driven in Phase 1 for MCX
instruments, and `currency` is feed-driven for CDS/BCD instruments; these two
policies do not apply NSE holiday suppression. A workflow session must match
the exchange of every instrument in the document, so MCX workflows should
declare `session: mcx_commodity` rather than relying on the NSE default.

Boolean shorthand `repeat: true/false` maps to `on_transition`/`once` and conflicts with an explicit `trigger`.

## Phase 2 — universes, shared features, layered stages

### Universe membership (F7)

A document may declare a membership expression instead of (or in addition to) explicit `instruments`. References combine by UNION and are deduplicated exchange-qualified; `intersect` restricts the union to names present in EVERY listed reference:

```yaml
universe:
  union:
    - universe: my-watchlist   # a saved universe (API-managed; owner-scoped)
    - index: nifty50           # an index constituent source list
  intersect:
    - universe: liquid-names   # membership := union ∩ liquid-names
  exclude:
    - universe: illiquid-names
  deduplicate: true            # default true
```

`EXCHANGE:SYMBOL` identity is preserved throughout: the same text on NSE and BSE stays distinct. Membership resolves against the published catalog; non-active members are reported in coverage (`rejected` with reasons), never silently dropped. New members are admitted with a fresh observation epoch — warmup gates their first signal; departed members are paused (history retained) and their subscriptions released. Every event records the membership revision in force at evaluation time. A failed membership resolution keeps the last valid membership (degraded, visible in worker health) and never materializes an empty union.

Universes are managed (and previews resolved without side effects) via `/api/worker/universes` (mounted under the `/api/worker/` bearer-token boundary); portfolio-derived universes are owner-scoped and read-only.

### Shared features and layered stages (F8)

Stage types: `signal` (fires alerts), `filter` (a condition layer that gates downstream stages), `feature` (declares a shared computed indicator). Layered chains use `input` (bounded depth 8, acyclic); a filter chain must all hold — evaluated under three-valued logic.

```yaml
stages:
  - id: trend                      # a shared feature stage
    type: feature
    clock: candle_close
    timeframe: day
    function: ema
    params: {period: 200}
  - id: breakout
    type: signal
    input: trend                   # ancestor filters gate this stage
    clock: candle_close
    timeframe: 5minute
    conditions:
      all:
        - {left: {indicator: ema, period: 20},
           op: crosses_above,
           right: {indicator: ema, period: 50}}
        - {left: {field: volume},
           op: gt,
           right: {multiply: [2, {indicator: sma, source: volume, period: 20, offset: 1}]}}
      any:
        - {left: {field: close}, op: gt, right: {value: 100}}
      not:
        - {left: {field: fundamentals.latest_roce_pct}, op: lt, right: {value: 0}}
```

- **Inline indicators**: `{indicator: <function>, period: ..., source: <field>, offset: <bars>, output: <key>}`. Supported functions: `sma ema wma rsi macd atr bollinger supertrend vwap_session volume_sma volume_ratio` (see `capabilities`). Numerics match the worker SDK fixtures (verified to ≤1e-9).
- **Feature identity**: (instrument, timeframe, function, canonical parameters, source field, offset, output, calculation version). Identical dependencies compute ONCE per market event in the engine and fan out to every dependent rule; rule-specific trigger state stays in checkpoints.
- **Stage references**: `{indicator: "stage:<feature-stage-id>"}` use a declared feature stage's value; the reference resolves to the referenced stage's own-timeframe snapshot and validates at compile time (a reference to a non-feature or unknown stage fails validation).
- **Bounded arithmetic**: `add subtract multiply divide` over operands (depth ≤ 3); division by zero/unknown is **unknown** (E-26), never an error.
- **Three-valued groups**: `all` AND, `any` OR, `not` negation with unknown propagation — unknown AND true = unknown; a rule with any unknown group never fires (E-15).
- **Warmup and confirmations**: features compute over COMPLETED candles only; a `day`-timeframe feature never sees a forming daily candle (E-16). Insufficient history is unknown with warmup progress in health. VWAP resets per session using the worker session policy — MCX/currency VWAP never silently uses NSE boundaries.
- **Layered clocks**: an ancestor filter evaluated on a different timeframe consumes that timeframe's latest COMPLETED bar snapshot (never a forming candle).
- **Fundamentals** (spec §5.8): `fundamentals.*` conditions read the LATEST stored snapshot from `public.fundamentals_features` (Screener.in sync — nightly scheduler plus on-demand `POST /algo-workers/worker/fundamentals/sync`), keyed by bare symbol with NSE-listing coverage: `NSE:SYMBOL` resolves, other exchanges are unavailable (unknown, never false). `evaluate_on: fundamentals_refresh` is an **alias for `candle_close`** — there is no dedicated fundamentals stream; a fundamentals-only stage therefore REQUIRES a `timeframe` (the candle clock dispatches it) and validation rejects one without. Fired events record the freshness the evaluation actually used (`fundamentals_acquired_at`, `fundamentals_as_of_date` in event evidence); worker health counts `fundamentals_hits`/`fundamentals_misses`/`fundamentals_stale` (staleness threshold `ALERTS_FUNDAMENTALS_STALE_HOURS`, default 168). Replay evaluates the same current snapshot — current fundamentals are never presented as historical truth.

### Delivery storm controls (E-25)

Per (workflow, alert) rolling emissions budget (`ALERTS_DELIVERY_BUDGET_PER_WINDOW`, default 60 per 60 s). Excess emissions are suppressed with reason `storm_budget` and reported; admitted members start silent until warmed, so membership expansion cannot manufacture an alert storm.

## Phase 3 — scheduled screeners and attachments (F9)

A document with a `screener` block is a **screener workflow**: same schema, revisions, lifecycle and authorization as alerts (worker scopes `workflows:read`/`workflows:write`; no new permission), but it executes as a SCHEDULED SCAN over stored completed candles — never live ticks.

```yaml
screener:
  schedule:
    every: 1d                     # 15m..31d; buckets are IST-anchored
    calendar: nse_equity          # ONLY nse_equity (calendar-backed)
    at: session_close             # "HH:MM" IST or session_close (15:30 IST)
  rank:
    by: {field: change_pct}       # field/indicator/expression
    direction: desc
  top_n: 20
  freshness_limit: 3d             # downstream dynamic-universe TTL
  attachments:
    - id: top10-entrants
      trigger: top_n              # entry | exit | top_n | rank_delta
      top_n: 10
      entry_rank: 10              # enter at rank <= 10 ...
      exit_rank: 15               # ... exit only when rank > 15 (hysteresis, E-17)
      initial_match: false        # first complete run is a silent baseline
      channels: [telegram_primary]
      message: "optional override template"
    - id: rank-movers
      trigger: rank_delta         # |rank - prev_complete_rank| >= threshold
      rank_delta: 5
      channels: [telegram_primary]
```

### Execution semantics

- **Pipeline**: universe resolution (union/intersect/exclude, catalog-qualified) → stored daily candles per member → staged conditions (same 3VL, layered chains, features and fundamentals as alerts) → deterministic ranking. Ties break by instrument identity (`EXCHANGE:SYMBOL` ascending) regardless of direction; null scores never rank; top-N truncation records the rank even beyond the cut (`beyond_top_n`).
- **Data fields**: `close/open/high/low/volume` from the latest completed daily candle, plus screener-only `change_pct` (vs previous completed close) and `turnover` (close × volume). These two are rejected in alert documents — there is no live path for them. A coherent `as_of` cutoff is applied to every member: a run never consumes future candles.
- **Missing data is not a failed match** (§5.3): a member with no candles, insufficient history or missing fundamentals is EXCLUDED with a typed reason (`no_data`, `insufficient_history`, `fundamentals_unknown`, `condition_unknown`, `rank_value_missing`) and counted in coverage.
- **Run statuses**: `complete` (every expected member evaluated, every condition resolved, every passing member ranked) / `partial` (results exist but at least one member excluded for a data reason) / `failed` (pipeline or universe-resolution error, with `failure_reason`). Coverage records `expected/evaluated/unavailable/unknown_conditions/rank_value_missing/qualifying` plus data freshness (max candle timestamp, fundamentals acquisition).
- **Scheduling**: buckets are computed in IST on the NSE calendar (session-gated — non-session days are skipped, so holidays produce no runs). Missed schedules COALESCE to the latest due occurrence (E-19: no backlog replay). Occurrence identity = `workflow_id:bucket_epoch`; concurrent workers race a unique constraint so one logical run wins (E-2), and leases (default 300 s) fence running executions — a crashed run is taken over after expiry and the stale owner cannot finalize (compare-and-swap).
- **Attachments**: evaluated only on COMPLETE runs. The first complete run of each (revision, attachment) initializes a silent baseline unless `initial_match: true`; `entry`/`exit` fire on the qualifying set with `exit_after` consecutive-absence buffering (default 1); `top_n` uses entry/exit rank bands (E-17 hysteresis); `rank_delta` compares against the PREVIOUS COMPLETE run's rank. Attachment events go through the existing signal event + outbox + delivery-worker machinery (idempotent per `run+attachment+instrument`; per-run emission cap `ALERTS_SCREENER_MAX_ATTACHMENT_EVENTS`, default 100, excess recorded in run coverage). State persists in `screener_attachment_state` — restart never resets baselines or hysteresis.
- **Partial runs** (E-18): visible with coverage and reasons; NEVER emit attachment events (no exits), never advance the comparison baseline, and never replace a downstream universe.
- **Downstream universes**: universe kind `screener` (`source_config: {workflow: <name>, top_n: ..., freshness_limit_s: ...}`) resolves to the latest COMPLETE run's qualifiers. Staleness past `freshness_limit_s` raises source-unavailable — dependent alerts go silent (unknown) instead of scanning a stale list. The scheduler re-materializes dependent universes after each complete run. Dependency cycles (a screener consuming its own results transitively) are rejected at authoring and resolution; ownership is enforced on every referenced resource.

### API mapping (screeners)

- `GET /api/worker/screeners/{id}/runs` — paginated run history
- `GET /api/worker/screeners/runs/{run_id}` — run detail + members (paginated)
- `POST /api/worker/screeners/{id}/runs` — manual run (`Idempotency-Key` query param honored; same key returns the original run)
- `GET /api/worker/screeners/{id}/events` — attachment event history
- `POST /api/worker/screeners/preview` — pure dry-run over stored data: no run rows, no state, no subscriptions, no outbox, no provider calls

## Operators

`gt gte lt lte crosses_above crosses_below within rises_pct falls_pct breaks_prev_high breaks_prev_low`

- Level ops (`gt gte lt lte`): match while the predicate holds; never fire by themselves.
- `crosses_above` / `crosses_below`: fire on a state transition (`prev < level <= cur`, mirrored). First observation of an epoch initializes and never fires. After firing, `rearm_level` (from `rearm_above`/`rearm_below`) must be crossed back before the rule can fire again — otherwise re-armed only when the value returns past the base level.
- `within`: right operand `{value: lo}`, with the finite numeric upper bound in `params: {hi: ...}` (required — a missing `hi` fails validation with a `bad_value` issue); fires on outside→inside transition.
- `rises_pct` / `falls_pct`: percentage move from the baseline captured on the epoch's first observation (never re-derived).
- `breaks_prev_high` / `breaks_prev_low`: the right operand is a previous-day level — `right: {field: prev_day_high}` (or `prev_day_low`), resolved from runtime context; a caller-supplied literal value (`right: {value: <level>}`) is also allowed. Guarded against re-firing every observation.

Fields: `ltp open high low close volume`. Missing data yields **unknown** — unknown propagates, only `true` matches, and a rule with any unknown condition never fires (E-15).

## Timeframes

`minute 3minute 5minute 10minute 15minute 30minute 60minute day` (required for `clock: candle_close`).

## Validation limits

Docs > 64 stages, > 256 alerts, > 1000 instruments, or > 32 conditions per stage are rejected, as are duplicate keys, custom YAML tags, unknown fields (typo field names never silently parse), duplicate ids, missing `alert.source` references, cycles in stage `input` chains, and unsupported capabilities (indicators, `fundamentals.*`, `universe`, upstream stage `input` references, non-`signal` stages — reserved for later phases; a document containing them parses but validation fails with named issues, so it cannot be activated). The **256 KiB size limit applies to the YAML text import** path (`POST .../import` and `yaml_text` fields); documents submitted through the JSON API (`document`) are bounded by the schema's own caps instead.

Canonical hashes are stable across key order; moving or reformatting does not change identity, changing a level or period does.

## Canonical examples

- `tests/fixtures/workflows/basic-price.yaml` — minimal LTP crossing alert (activatable in Phase 1).
- `tests/fixtures/workflows/quality-momentum.yaml` — the full layered vision from the v1 proposal; parses today, activation blocked until later phases implement fundamental/indicator stages.

## API mapping

| Operation | Endpoint |
| --- | --- |
| Validate without saving | `POST /api/worker/workflows/validate` |
| Preview (writes nothing; optionally evaluates supplied recent samples in memory) | `POST /api/worker/workflows/preview` |
| Import YAML as draft | `POST /api/worker/workflows/import` |
| Create / list | `POST /api/worker/workflows`, `GET /api/worker/workflows` |
| Read / update (expected_revision) | `GET /api/worker/workflows/{id}`, `PATCH /api/worker/workflows/{id}` |
| Lifecycle | `POST /api/worker/workflows/{id}/activate|pause|resume|archive` (`activate` takes an optional body `{"revision": N}` to roll back to an explicit revision) |
| History / health / export | `GET /api/worker/workflows/{id}/events?limit&offset`, `GET /api/worker/workflows/{id}/health`, `GET /api/worker/workflows/{id}/export` |
| Channels | `GET|POST /api/worker/notification-channels`, `POST /api/worker/notification-channels/{id}/test` |

Required worker-token actions: `workflows:read`, `workflows:write`, `workflows:activate`, `notifications:test`.

### Preview samples

Preview is deterministic and read-only. Add an `observations` array to the
preview request when a dry-run is wanted:

```json
{
  "yaml_text": "...",
  "observations": [
    {"instrument_key": "NSE:RELIANCE", "epoch_id": "preview-1", "ts": "2026-09-08T09:15:00Z", "ltp": 2990},
    {"instrument_key": "NSE:RELIANCE", "epoch_id": "preview-1", "ts": "2026-09-08T09:16:00Z", "ltp": 3010}
  ]
}
```

The response reports `evaluation: dry_run`, `evaluated_observations`,
`warmup_bars`, `would_fire`, and `unknown_reasons`. With no samples it reports
`dry_run_no_data`. Samples need a real timestamp; the API never substitutes
server wall-clock time. Preview does not persist checkpoints, signal events, or
deliveries.
