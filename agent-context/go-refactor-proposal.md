# Go Refactor & RAM Reduction Proposal — kite-algo

Status: DRAFT v3 (post grill Rounds 1–2; incorporates C-1..C-7). Date: 2026-09-11.
Measurements taken 2026-09-11 inside running containers (docker cgroup stats + in-process
VmRSS/smaps/tracemalloc; reproducible via appendix methods).

## 1. Goal & constraints

- Fit the stack under ~500 MiB on a 1 vCPU / 1 GB target server without compromising
  trading correctness (options/greeks/tick/alert behavior is sacred).
- Tick-path, options-math, and alert changes are gated on live-market validation sessions.
- Non-goals (no RAM payoff, high rewrite risk): orders stack (~5.5k lines; order updates
  ALREADY traverse market-runtime — the order path is hybrid today), FastAPI router
  surface, journaling, auth. Worker SDK: inert **in the worker path** (the API-side
  import in indicators_service.py is exactly what Phase D removes).
- Every phase carries a rollback TRIGGER + ACTION + OWNER (§3 per phase, C-5), not just
  a mechanism.
- Containment note (C-7/C-6 context): candle consolidation grows market-runtime's blast
  radius; order updates already depend on it — isolation required (Phase B §).

## 2. Measured baseline (2026-09-11)

| Item | Value |
|---|---|
| Stack total (post Python-MCP retirement) | ~798 MiB. kite-test-postgres EXCLUDED from all accounting in this document — test-only, not started by docker compose |
| kite-app / alerts-worker / postgres / frontend / market-runtime(Go) / mcp-go / redis | 291 / 121 / 155 / 118 / 79 / 16 / 18 |
| Fresh API boot in-process | 316 MiB current RSS (smaps: 210 anon + 106 file-backed) |
| numba + llvmlite | +54 MiB imports; libllvmlite.so = 43 MiB resident (largest mapping) |
| scipy | +58 MiB when loaded; imported as a side effect of numba init (INFERRED from import-time trace — C-7); zero direct users in backend/ or SDK. Scipy eviction is therefore conditional on numba being the only importer — which the cephes-erf decision makes permanent |
| pandas + numpy | ~66 MiB combined |
| Alerts worker | 121 MiB = ~55 imports (verified pandas/numba/scipy-free by runtime check AND griller AST trace) + ~65 runtime state; worker code adds +0 MiB marginal imports inside the API (fold-in cost is the ~60 MiB of runtime state, not the code — reconciled per C-6) |
| Greeks speed (2000-strike chain) | numba 107 µs / numpy-vector 128 µs / naive fallback 5,150 µs; IV 200 strikes 0.4 / TBD (C-1: must be designed+measured) / 3.6 ms |
| Memory growth, 3000 full-chain rounds both modes | 0 KiB |
| Numba-vs-no-numba parity (measured with libm-accuracy erf) | 852 values: price/greeks ≤4 ulps; IV ≤2.5e-12 rel; degenerate branches agree exactly. NOTE: these numbers describe the measured prototype, not yet the shippable cephes-erf build (C-1) |

## 3. Phases (grill-amended order)

### Phase A — no code (NOW; blocking exit criteria)
- Scope note: test-only containers (e.g. kite-test-postgres) are out of scope for this plan entirely.
- Postgres max_connections 50→25 (verify app pool ceiling first); mem_limit + memswap_limit
  on EVERY compose service.
- **Blocking exit criterion (C-7):** per-service RSS re-measured on a target-class
  (1 GB/1-core) box; §4 budget table filled with those numbers — Phase A is not done until
  the table is real.
- Rollback: container start / git revert. Trigger: any service OOM-kill or failed boot →
  revert limit for that service, owner: operator.
- Remove never-imported yfinance at next image build.

### Instrumentation gate (immediately after A; permanent)
- Go tick-in counter in market-runtime; per-consumer lag (publish→process delta) logged
  per minute; Python CPU-seconds/min for finance-app + worker.
- This is the evidence base for Phase B/F decisions (C-3/C-4).

### Phase C — numba removal (FIRST code phase; market-day gated)
- Rewrite 3 array kernels + IV solver as numpy-vectorized math.
- **erf: port cephes erf/erfc (netlib double-precision rational approx, ~1 ulp) as
  branchless numpy (~30 lines). NOT A&S 7.1.26 (1.5e-7 breaches the thresholds below in
  the wings: delta 0.01 → 1.5e-5 rel). scipy stays out permanently (C-1).**
- Acceptance criteria: price <1e-10 rel; delta/gamma/vega <1e-9 rel; IV <1e-6 abs on a
  recorded real-snapshot corpus incl. expiry-day and T=1e-12; EXACT match on degenerate
  branches (T≤1e-12, σ≤1e-12, F==K → ±0.5).
- **IV solver design REQUIRED before this phase is "specified" (C-1):** vectorized
  fixed-iteration Newton with masked bisection fallback, tolerance-check order identical
  to the numba version; implemented and MEASURED (replace the §2 dash with a number)
  before any cutover. Until then Phase C is in-design, not ready.
- CI parity test: meta_path numba/llvmlite blocker + golden values.
- Live: old njit path frozen behind OPTIONS_ENGINE=numba|numpy; per-snapshot diff ≥5
  sessions. **Rollback policy (C-5): any single tolerance breach → auto-revert env to
  numba + restart the 5-session clock; owner: operator.** numba deleted from
  requirements only after 5 clean sessions.
- Expected: API 291 → ~175 (numba+scipy ≈ 112–117 MiB; API-only — worker verified free).

### Phase D — indicators + performance → Go (market-day gated)
- Extract indicator catalog from mcp/go into shared internal package served by
  market-runtime; worker_market.py router calls it; port performance_logic.py;
  numpy-fy historical_data.py's lazy pandas use.
- **Headline saving: pandas only (~35–45 MiB) — numpy STAYS (the Phase-C kernels and
  options_sessions remain numpy users forever) (C-4).** API lands ~140.

### Phase B — candle aggregation → market-runtime (SCOPE CUT + numeric entry trigger; likely deferred)
- Scope (C-3): `candle_aggregator.py` + `daily_candle_finalization.py` ONLY.
  `candle_ingestion.py` (Kite-REST backfill) stays Python, numpy-fied in D.
  `candles_api.py` stays Python — it reads the same Redis keys/channels Go writes.
- Three-mode flag: shadow (shadow_candle:* keys, ≥3 trading days byte-exact differential
  harness at every finalize) → go_serving → off. Per-tick forming writes preserved in v1;
  pipelining allowed; throttling separately gated behind a written freshness contract.
- Containment: own goroutine tree, recover() per batch, bounded ring buffers, independent
  kill-switch. Schema gate: alembic-written schema_version row; Go writer refuses until
  version ≥ N.
- **Numeric entry trigger (C-3), evaluated after A+C+D re-measure — do B only if:**
  (a) candle-aggregator share of finance-app CPU-seconds at market open > 15%, OR
  (b) tick→candle-finalize p99 > 250 ms, OR
  (c) remaining RSS gap to target > 60 MiB and no cheaper lever exists.
- **Expectation: DEFERRED.** Post C/D, B buys ~no RAM; its case is contention, which the
  instrumentation gate will either prove or dismiss.
- Rollback: flag → off; trigger: any post-cutover shadow mismatch → same-day revert,
  owner: operator.

### Phase F — alerts (data-decided)
- Option A: worker folded into API behind ALERTS_INPROCESS, supervised asyncio task with
  the EXISTING quarantine/teardown semantics preserved in-process (commit d7d5193 exists
  because alert-workflow failures were real). **Stated trade-off (C-6): a runaway workflow
  now shares the event loop with order-entry; +0 MiB is imports only — fold-in cost is
  ~60 MiB of moved runtime state.** −50 MiB net. Standalone mode preserved.
- Option B: alerts engine → Go (−121 MiB + one less Redis hop). Only if instrumentation
  shows RAM/latency still binding after C/D.
- Rollback: flag flip; trigger: API p99 latency regression during any live session →
  flag off, owner: operator.

### Phase G — frontend static export (SECURITY REDESIGN FIRST — C-2; carries the <500 gate)
- **Blocking prerequisites:** market-runtime websocket auth (today
  `CheckOrigin: return true`, no token — safe only while docker-network-only behind the
  Next proxy); audit of every endpoint that becomes browser-facing; written effort
  estimate. mcp-go needed KITE_MCP_HTTP_TOKEN + ALLOWED_ORIGINS for exactly this.
- −118 MiB. Until G's security work is specified and estimated, **<500 is aspirational,
  not committed.**

## 4. Budget table — to be filled from target-class box during Phase A (blocking)

| Service | Today | Post C/D | Post F | Post G | mem_limit |
|---|---|---|---|---|---|
| finance-app | 291 | ~140 | ~140 (or ~190 F-A) | ~140 | TBD (blocking) |
| alerts-worker | 121 | 121 | 0 | 0 | — |
| postgres | 155 | ~130 | ~130 | ~130 | TBD (blocking) |
| frontend-next | 118 | 118 | 118 | 0 | TBD (blocking) |
| market-runtime | 79 | ~95 | ~95+ | ~95+ | TBD (blocking) |
| mcp-go + redis | 34 | 34 | 34 | 34 | TBD (blocking) |
| **Total** | **~798** | **~640** | **~520–570** | **~400–450** | — |

## 5. Effort estimates
- A: 1 day. Instrumentation: 1–2 days. C: 2–4 days (incl. cephes erf + IV design/measure).
- D: 3–5 days. B (if triggered): 5–10 days. F-A: ~1 day. F-B: 10+ days.
- G: security work + static export — estimate REQUIRED before G enters the plan (C-2).

## 6. Signed execution sequence (griller-agreed, Round 2)
1. **A now** (blocking exit: target-box re-measurement + mem_limits everywhere).
2. **Instrumentation** (cheap, permanent, evidence for all later gates).
3. **C** — only after cephes erf lands and the IV solver is designed AND measured; flag-frozen cutover with defined breach handling; delete numba after 5 clean sessions.
4. **D** — pandas-only accounting; ingestion stays Python.
5. **Re-measure → decide B on the numeric triggers (expectation: deferred).**
6. **F from data**; F-A only with the isolation trade-off acknowledged.
7. **G last**, only with market-runtime auth + endpoint audit + estimate; if blocked, the
   <500 goal is formally moved to aspirational.
