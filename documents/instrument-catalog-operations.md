# Instrument Catalog Operations

The instrument catalog is the shared identity layer for alerts, market data,
options, orders, and future frontend search.

## Authority and runtime roles

- PostgreSQL owns published catalog generations, stable `instrument_id` values,
  broker mappings, lifecycle state, and refresh diagnostics.
- Python owns normalization, resolution, search, validation, and refresh
  publication.
- Go `market-runtime` loads one complete published generation into an immutable
  in-memory cache. Numeric broker tokens remain internal to subscriptions and
  tick routing.
- `EXCHANGE:TRADINGSYMBOL` is the public authoring key. Bare symbols are not
  safe because the same text can exist on NSE, BSE, derivatives, or another
  segment.

## Identity vs metadata

`instrument_id` is the immutable identity of one exact listing or contract.
The identity matching key uses immutable contract attributes only — exchange,
tradingsymbol, instrument type, expiry, strike, option type — with numeric
canonicalization (a `0` strike and a `NULL` strike are the same identity).

Enrichment metadata (segment, underlying, name) is mutable and corrected in
place: a broker master that starts or stops supplying `underlying`, or a
segment naming change, must never split an identity or make an unchanged
contract look like a new one. A genuinely different contract (October vs
November gold, a different strike) is a different identity.

Broker mappings are versioned. A token change for the same contract keeps the
identity and starts a new current mapping. Token reuse by a different contract
closes the old mapping; it never merges the two identities.

## Refresh states

Each refresh creates a generation. The important states are:

- `staging`: downloaded rows are being validated and are not visible as the
  published catalog.
- `published`: every requested exchange passed validation.
- `degraded`: one or more exchanges published new data while rejected exchanges
  retained their previous valid snapshot.
- `failed`: no exchange could be safely accepted; the previous published state
  remains authoritative.

A failed or suspiciously incomplete MCX download does not deactivate MCX
contracts. Missing rows are reconciled only inside an exchange payload that
was accepted. Every accepted generation has a validation summary, its
accepted/retained exchange lists, and per-exchange source provenance
(`exchange_sources`).

## One complete publication (generation coherence)

Every successful publish is a COMPLETE publication: after the transaction
commits, all non-retired records — including exchanges whose data was
retained, and exchanges that were not part of this refresh request — point at
the new generation. Readers therefore always see exactly one generation.

Freshness is not faked. `instrument_catalog_generations.exchange_sources`
records, per exchange, whether the payload was `accepted` now or `retained`
from the generation where that exchange was last accepted, with the original
source generation id and observation time. A retained exchange is never made
to look fresh.

Concurrent refreshes serialize on a PostgreSQL advisory transaction lock; two
overlapping publishes cannot interleave record updates. Go rejects any view
that still contains multiple generations (defense in depth), and a failed Go
reload keeps the previous store and generation.

## Refresh completeness guard

An exchange payload is rejected before publication when it is suspiciously
small relative to the previously accepted coverage: fewer than
`max(minimum_count, ceil(previous_count * coverage_floor_ratio))` records is
treated as a truncated download and that exchange retains its previous valid
snapshot (marked degraded, reason in the validation summary). Exceptional
legitimate shrinks (delisting waves) are published by explicitly listing the
exchange in `force_exchanges`. Row-level validation rejects wrong-scope
downloads, duplicate tokens/public keys/identities, non-finite numeric
contract fields, and unparseable expiries as typed refresh failures.

## Health semantics

`InstrumentCatalog.health()` reports the last USABLE publication
(`published` or `degraded`, newest first). A failed or still-staging refresh
attempt never displaces it; failed attempts are surfaced separately under
`latest_attempt`. Compare `health.generation` with the Go reload
acknowledgement (`POST /internal/market-runtime/instruments/refresh` returns
the `generation` and `count` Go actually loaded; the orchestrator reports
`go_reload.matches_publication`). A mismatch means Go is serving a stale
instrument view — call the refresh endpoint and inspect its logs; never
repair it by hand-editing tokens.

## Go reload and health

After a successful catalog publication, the API notifies market-runtime at:

```text
POST /internal/market-runtime/instruments/refresh
GET  /internal/market-runtime/instruments/health
```

The reload builds a complete new store before swapping it. If loading fails,
the previous Go generation remains active. The alerts worker also logs the
Go cache generation next to its binding revision on every subscription sync.

## Alerts binding lifecycle

The evaluation worker owns exactly one mutable binding state (the instrument
binding registry). Source factories, candle-history readers, the
market-runtime renewal callback, and health reporting all read the current
accepted snapshot; there are no private copies that drift apart.

- New workflow symbols resolve and subscribe WITHOUT a restart, starting from
  zero tokens or zero workflows. Renewal is always constructed, so the first
  later activation establishes market-runtime subscriptions within one
  renewal interval.
- A replaced broker token rebuilds the affected sources with a fresh
  observation epoch while durable trigger state (checkpoints) is preserved.
- Retired, expired, ambiguous, or authoritatively unresolved instruments stop
  being eligible: their subscriptions are unsubscribed, sources are released,
  and the rules stay silent with `unresolved_instruments` in health.

### Compatibility fallback policy (C2)

`ALERTS_INSTRUMENT_TOKENS` is a compatibility token map with explicit policy:

| Catalog state | Default | `ALERTS_INSTRUMENT_TOKEN_FALLBACK=always` |
| --- | --- | --- |
| Uninitialized (no generation ever published) | env map applies (bootstrap/development), logged | env map applies |
| Unavailable (database down) | resolution raises; worker keeps current bindings and retries next pass | env map applies with warning |
| Not found, catalog initialized | authoritative rejection | env map applies with warning |
| Retired / expired / ambiguous | authoritative rejection, never bypassed | still rejected |

Authoritative rejection always wins in the default production posture; the
`always` mode is an explicit, logged migration escape hatch that still never
revives retired or expired records.

### Binding provenance (C6)

Subscriptions persist `instrument_binding` (instrument_id, public key, broker,
token, catalog generation, lifecycle) at creation AND backfill/refresh it on
subsequent materialization passes when the catalog data becomes available or
the mapping moves. Every signal event copies the binding in force at
evaluation time into its evidence, so rebinding never rewrites the meaning of
historical events. Expired contracts remain queryable for historical
provenance but are rejected for new activation.

## Legacy `kite_instruments` boundary

The following consumers still read the legacy compatibility projection and are
PRESERVED as-is until their migration slice:

- `backend/api/routers/marketwatch.py` — last_close lookup by token;
- `backend/broker_api/instruments/index_ingestion.py` — index ingestion;
- `backend/broker_api/market/candles_api.py` — token existence checks;
- `backend/broker_api/instruments/instruments_repository.py` — explicit
  compatibility fallbacks (legacy Go/SQL paths apply only while the catalog
  is uninitialized);
- `backend/broker_api/broker_api.py` — writes the projection from accepted
  published snapshots only.

The alerts worker and the options expiry selection tests do NOT depend on the
legacy table for identity. Do not claim all application consumers use the new
contract yet.

## Sessions are separate

Catalog lifecycle means whether a contract is present, expired, or retired.
It does not mean whether an exchange is currently open. NSE calendar policy,
MCX feed-driven policy, and currency-session policy remain worker-local
session behavior. A closed market is not an inactive instrument.

## Bootstrap procedure

1. Apply migrations (`alembic upgrade head`) — creates the catalog tables and
   the published view.
2. Trigger one instruments import (system-token broker download) to publish
   the initial validated generation; verify `health()` reports
   `published`/`degraded` with the expected exchanges.
3. Rebuild/redeploy market-runtime, then call the Go refresh endpoint and
   compare its `generation`/`count` with Python `health()`.
4. Only then stop supplying `ALERTS_INSTRUMENT_TOKENS`; set
   `ALERTS_INSTRUMENT_TOKEN_FALLBACK=always` first if a transition period is
   needed, and remove it once coverage is confirmed.

## Troubleshooting checklist

1. Check PostgreSQL generation status, its accepted/retained exchange lists,
   and `exchange_sources` freshness.
2. Resolve the qualified public key through the Python worker market endpoint.
3. Compare the returned `catalog_generation` with market-runtime health.
4. If Go is behind, call the refresh endpoint and inspect its response
   (`generation`, `count`, `matches_publication`).
5. If an alert has no token, inspect worker logs for catalog ambiguity,
   retirement, expiry, fallback-policy decisions, and the binding revision.
6. Never repair a mismatch by manually guessing or bulk-editing instrument
   tokens. Refresh and publish a validated catalog generation instead.
