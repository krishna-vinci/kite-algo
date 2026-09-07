# Design: Go Rewrite of the kite-algo MCP Server

Date: 2026-09-07
Status: **Deferred** — the <40 MiB target was relaxed the same day. The implemented fix is the Python slimming path (SDK lazy imports + server-side indicator endpoint, `POST /api/algo-workers/worker/indicators`), landing the adapter at ~72 MiB from ~119 MiB with pandas fully removed from its environment. This spec remains the validated plan if a <40 MiB target is ever reinstated (its measurements and safeguards inventory still apply).
Replaces: `mcp/python/` (FastMCP server, ~119 MiB container)

## 1. Problem

The MCP server (`kite-algo-mcp-1`) idles at ~119 MiB RSS. Its job is mostly
proxying: 73 cataloged tools forward requests to the FastAPI backend
(`finance-app:8777`, `/worker/*` endpoints). The RAM cost comes from the import
chain: the MCP imports the `kite_algo_worker` SDK, whose package root eagerly
imports pandas/numpy, plus the FastMCP/pydantic/uvicorn stack.

Measured in the live image (`ru_maxrss` of import-only processes):

| Configuration | Peak RSS |
|---|---|
| Full chain (fastmcp + pydantic + uvicorn + httpx + pandas + numpy) | ~98 MiB |
| Same stack without pandas/numpy (best-case Python floor) | ~52 MiB |
| pandas + numpy alone | ~45-46 MiB |
| Live container cgroup (anon) | ~113 MiB (import peak + serving) |

## 2. Decision record

1. **Go rewrite is justified, not assumed.** The Python floor is ~52 MiB
   imports-only (serving adds more), so the <40 MiB target is unreachable in
   Python even with lazy imports and a slimmer MCP library.
2. **Indicator computation moves to the backend (Option A).**
   `calculate_indicator` is the only tool that computes locally (pandas +
   `TechnicalAnalysis`, 22 indicator names). The math moves to a new
   authenticated backend endpoint so the Go server remains a pure proxy and
   one implementation of the math remains the source of truth.
3. **Alternatives rejected:** lazy-import-only fix (misses target: ~55-60 MiB);
   porting indicator math to Go (must maintain byte-exact numerical parity with
   pandas semantics — permanent tax); split Go+Python topology (two servers,
   two auth surfaces, splits the tool catalog across hosts).

## 3. Goals / Non-goals

**Goals**
- RSS < 40 MiB measured on the running container (projection 15-25 MiB; gate
  cutover on measurement, hard-cap with `GOMEMLIMIT=40MiB`).
- Full 73-tool catalog parity plus the 2 registered MCP resources.
- Drop-in env contract: all existing `KITE_MCP_*` variables keep their meaning.
- Every safeguard in §5 survives with a test proving it survived.

**Non-goals**
- stdio transport (HTTP only for v1; the Go SDK makes stdio a later ~10-line add).
- New tools, new resources, OpenTelemetry, persistence.
- Changing the backend except for the one new indicator endpoint (§6).

## 4. Architecture

New module `mcp/go/` mirroring the `market-runtime` layout:

```
mcp/go/
  cmd/kite-algo-mcp/        main: config, wiring, signal handling
  internal/catalog/         generated table: 73 ToolSpecs + embedded JSON schemas
  internal/policy/          port of policy.py (visibility, capabilities, authorize)
  internal/session/         port of sessions.py (run leases, heartbeats)
  internal/backend/         stdlib net/http client for /worker/* (bearer + session headers)
  internal/transport/       streamable HTTP, bearer auth, allowed hosts/origins, /healthz
  internal/tools/           per-group registration; generic proxy handler + response shapers
  schemas/                  JSON Schemas exported from contracts.py (go:embed)
  tools/export_schema.py    one-time pydantic -> JSON Schema exporter (kept for regen)
```

- **Single dependency:** `github.com/modelcontextprotocol/go-sdk`. Everything
  else is stdlib. (Fallback if the SDK blocks us: `mark3labs/mcp-go`.)
- **Generic proxy handler:** validate args against the embedded schema →
  authorize via policy → (lease if `trade_write` + `run_id`) → dispatch through
  the concurrency semaphore → backend call → apply the per-tool response shaper
  where one exists (e.g. depth view) → MCP result. After §6, all 73 tools share
  this path — none compute locally; per-tool differences (response shapers,
  extra validation) are data in the catalog table, not separate code paths.
- **Catalog generation:** `catalog.py`'s 73 `ToolSpec`s and `contracts.py`'s
  pydantic models are exported (script committed) into a Go table + embedded
  schemas, so the reviewed catalog stays the single source of truth at
  generation time; drift is caught by the contract tests (§7).

## 5. Safeguards inventory (must survive; each row gets a test)

| # | Safeguard (source) | Go port requirement |
|---|---|---|
| 1 | Bearer token on MCP HTTP (`http_transport.py`) + optional worker token to backend | constant-time compare, 401 semantics identical |
| 2 | `KITE_MCP_ALLOWED_HOSTS` / `ALLOWED_ORIGINS` | same defaults, same rejection behavior |
| 3 | Static visibility by effect/profile (`policy.visible`) | read: always; data_write: `ALLOW_DATA_REFRESH`; trade_write: profile ∈ {paper, live}; `live_only` → live only |
| 4 | Dynamic capability filtering (`policy.backend_visible`, refreshed from worker health per authorize, except `get_capabilities`) | actions/modes/accounts/templates sets; worker API stays the authoritative revocation boundary |
| 5 | Argument checks: `execution_mode`/`mode` (live requires live profile; allowed-modes), `account_scope`/`account`, `template_id` (empty allowed list = no restriction — preserve exactly) | policy violation codes identical |
| 6 | Policy violation codes: `unknown_tool`, `tool_disabled`, `live_profile_required`, `mode_not_allowed`, `account_not_allowed`, `template_not_allowed`, `live_only`, `backend_action_denied` | same codes, same retryable flags |
| 7 | Run leases with heartbeats (`sessions.py`): nonce, heartbeat interval, lost-lease refusal mid-mutation | goroutine per lease + `time.Ticker`; lease-lost blocks further mutations |
| 8 | Ambiguous outcome mapping: write returned but lease lost, or `trade_write` timeout/unknown-transport-error → `write_outcome_unknown` with `reconcile_with` + extracted submission identifiers; NEVER auto-retry a write | identical payloads |
| 9 | Error taxonomy: `lease_refused`, `not_ready`, `result_too_large`, `backend_timeout` (non-write), `backend_unauthorized` (401/403), `invalid_request` (400/422, ValueError/TypeError), `not_found`, `conflict` (409), `rate_limited` (429, retryable), `backend_error` | table-driven mapping; response-body differences must not change codes |
| 10 | Concurrency semaphore around dispatch | same limit via config, buffered channel |
| 11 | Serialization/result size limit (`result_too_large`) | equivalent cap on encoded result |
| 12 | Tool annotations (readOnlyHint/idempotency from spec) | same MCP annotations emitted |
| 13 | `/healthz` used by the Docker healthcheck | same path, same semantics |

## 6. Backend indicator endpoint (Option A, honestly scoped)

`POST /worker/indicators` on finance-app:
- Auth: existing worker token + action gate (same as other `/worker` writes/reads; disposition documented in `coverage.json`).
- Input caps: max bars (config, default ~5,000), allowlisted 22 indicator names, bounded period/multiplier ranges (mirror `contracts.py` validation).
- Implementation: adapt `mcp/python/kite_algo_mcp/tools/indicators.py::_indicator_result` onto `TechnicalAnalysis` where pandas already lives.
- Errors: 400/422 taxonomy consistent with other `/worker` endpoints.
- Regression tests: golden fixtures generated from the Python implementation
  (same bars in → identical values/`ready`/warmup rows out). This is the
  numerical-parity guarantee while Python remains the reference.
- MCP `calculate_indicator` becomes a standard proxy tool; the pandas import
  disappears from the MCP layer entirely.

## 7. Verification & cutover (no big bang)

1. **Parallel run:** Go server on :18789 beside Python on :18788 (separate
   compose service `mcp-go`, profile `mcp`).
2. **Contract tests:** existing `tests/mcp/` suites (catalog contract, http
   transport, sessions, orders, market, discovery) pointed at the Go port.
3. **Comparison matrix** (Go vs Python, automated where possible):
   - **Schemas:** tools/list output — names, descriptions, inputSchema byte-diff.
   - **Errors:** malformed args, unknown tool, unauthorized, backend down,
     backend 4xx/5xx, timeout — assert same codes/messages shape.
   - **Permissions:** all profile values (`read`, `paper`, `live`) ×
     `ALLOW_DATA_REFRESH` on/off × representative tools; capability revocation
     via stubbed worker health.
   - **Concurrency:** parallel invokes, lease contention, heartbeat loss during
     in-flight write (fault-injected stub).
   - **Failure behavior:** connection refused / mid-response reset / slow
     response (timeout path).
   - **Never replay real trading writes against both servers.** Write-path
     comparisons run against a stub backend with recorded responses; live
     verification is read-profile only.
4. **Resource gate:** observed `docker stats` RSS < 40 MiB under the test
   suite load + 24h idle; `GOMEMLIMIT=40MiB` set in compose.
5. **Swap:** compose `mcp` service switches to the Go build; Python retained as
   `mcp-legacy` profile for one release; then `mcp/python/` deleted.

Rollback: flip the `mcp` service build back to `mcp/python/` (kept until the
deletion step).

## 8. Acceptance criteria

- All existing `tests/mcp/` suites pass against the Go server unchanged.
- Comparison matrix (§7.3) green, including error/permission/concurrency paths.
- RSS < 40 MiB measured (target 15-25 MiB), image < 30 MB.
- `calculate_indicator` golden fixtures pass against the backend endpoint.
- Python server removed after one release on `mcp-legacy`.

## 9. Risks

| Risk | Mitigation |
|---|---|
| Go SDK protocol gaps (resources, annotations) | SDK is v1 stable; contract tests catch drift; fallback SDK identified |
| Numerical drift on indicators | single implementation stays in Python (backend); golden fixtures from the reference |
| Subtle policy semantics lost (e.g. empty-template-means-unrestricted) | §5 table is checklist + test per row; policy.py port reviewed line-by-line |
| Go GC holds RSS near cap under burst | `GOMEMLIMIT=40MiB` fails visibly instead of creeping; measured gate before swap |
| Schema export drift vs contracts.py | exporter committed; CI regenerates and diffs |

## 10. Open questions

None blocking. (coverage.json's 92 entries are backend endpoint dispositions
mapping to the 73 exposed tools — recorded here to prevent future confusion.)
