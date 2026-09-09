# Workflow format — Phase 1 authoring contract

The canonical definition is a JSON document; YAML is its human-editable representation. Files describe **what to monitor**; the API decides **what runs** (import always creates a draft; activation is an explicit API call). Any capability listed as unsupported fails validation with a named issue instead of being partially interpreted.

Status: implemented subset of [spec v2](../docs/superpowers/specs/2026-09-08-alerts-platform-spec-v2.md). Schema version: `1`.

## Document shape

```yaml
version: 1
name: reliance-breakout            # unique per owner
instruments: ["NSE:RELIANCE"]      # "EXCHANGE:SYMBOL" shorthand or {symbol, exchange}
session: nse_equity                # optional, default nse_equity
stages:
  - id: px                         # required; unique
    type: signal                   # Phase 1 supports "signal"
    clock: ltp                     # ltp | candle_close (alias: evaluate_on)
    timeframe: 1m                  # required iff clock: candle_close
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
    reminder_interval: 30m         # only with trigger: reminder
    channels: [telegram_primary]   # channel names defined via the API
    message: "${symbol} broke ${level} at ${ltp} (${time})"   # optional template
data_policy:
  missing: exclude_and_report
  insufficient_history: wait
  require_closed_candles: true
```

Boolean shorthand `repeat: true/false` maps to `on_transition`/`once` and conflicts with an explicit `trigger`.

## Operators

`gt gte lt lte crosses_above crosses_below within rises_pct falls_pct breaks_prev_high breaks_prev_low`

- Level ops (`gt gte lt lte`): match while the predicate holds; never fire by themselves.
- `crosses_above` / `crosses_below`: fire on a state transition (`prev < level <= cur`, mirrored). First observation of an epoch initializes and never fires. After firing, `rearm_level` (from `rearm_above`/`rearm_below`) must be crossed back before the rule can fire again — otherwise re-armed only when the value returns past the base level.
- `within`: right operand `{value: lo}`, with the upper bound in `params: {hi: ...}`; fires on outside→inside transition.
- `rises_pct` / `falls_pct`: percentage move from the baseline captured on the epoch's first observation (never re-derived).
- `breaks_prev_high` / `breaks_prev_low`: right operand `{value: <prev-day level>}` supplied by runtime context; guarded against re-firing every observation.

Fields: `ltp open high low close volume`. Missing data yields **unknown** — unknown propagates, only `true` matches, and a rule with any unknown condition never fires (E-15).

## Timeframes

`minute 3minute 5minute 10minute 15minute 30minute 60minute day` (required for `clock: candle_close`).

## Validation limits

Docs > 256 KiB, > 64 stages, > 256 alerts, > 1000 instruments, or > 32 conditions per stage are rejected, as are duplicate keys, custom YAML tags, unknown fields (typo field names never silently parse), duplicate ids, missing `alert.source` references, cycles in stage `input` chains, and unsupported capabilities (indicators, `fundamentals.*`, non-`signal` stages — reserved for later phases; a document containing them parses but cannot be activated).

Canonical hashes are stable across key order; moving or reformatting does not change identity, changing a level or period does.

## Canonical examples

- `tests/fixtures/workflows/basic-price.yaml` — minimal LTP crossing alert (activatable in Phase 1).
- `tests/fixtures/workflows/quality-momentum.yaml` — the full layered vision from the v1 proposal; parses today, activation blocked until later phases implement fundamental/indicator stages.

## API mapping

| Operation | Endpoint |
| --- | --- |
| Validate without saving | `POST /api/worker/workflows/validate` |
| Preview (writes nothing) | `POST /api/worker/workflows/preview` |
| Import YAML as draft | `POST /api/worker/workflows/import` |
| Create / list / read / update (expected_revision) | `POST|GET|PATCH /api/worker/workflows` |
| Lifecycle | `POST /api/worker/workflows/{id}/activate|pause|resume|archive` |
| History / health / export | `GET .../{id}/events?limit&offset`, `.../{id}/health`, `.../{id}/export` |
| Channels | `GET|POST /api/worker/notification-channels`, `POST .../{id}/test` |

Required worker-token actions: `workflows:read`, `workflows:write`, `workflows:activate`, `notifications:test`.
