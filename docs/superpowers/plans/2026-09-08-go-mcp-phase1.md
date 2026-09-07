# Go MCP Server — Phase 1 (Foundation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up `mcp/go` — a Go MCP server module that serves the exact 73-tool catalog (schemas included) over HTTP with a health endpoint, backed by a typed backend client, verified against the Python adapter's exported catalog.

**Architecture:** Table-driven: a Python export script emits one `catalog.json` (specs + JSON Schemas from `contracts.py`) which Go embeds and serves through the official MCP Go SDK. Backend calls go through a stdlib `net/http` client to `finance-app:8777` (`/api/algo-workers/worker/*`). Tool *dispatch semantics* (policy, leases, error taxonomy) land in Phase 2; Phase 1 proves the catalog/transport/parity foundation.

**Tech Stack:** Go ≥1.24, `github.com/modelcontextprotocol/go-sdk` (only dependency), Python 3.11 + pydantic for the export script, pytest for the contract harness.

**Spec:** `docs/superpowers/specs/2026-09-07-mcp-go-rewrite-design.md` (safeguards inventory §5 and cutover §7 apply to Phases 2-3).

---

### Task 0: Go toolchain ≥1.24

Host has go1.19.8 (`/usr/bin/go`); the MCP Go SDK requires a newer module directive.

- [ ] **Step 1: Install Go 1.24 to /usr/local**

```bash
cd /tmp && curl -sSLO https://go.dev/dl/go1.24.4.linux-amd64.tar.gz \
  && sudo rm -rf /usr/local/go && sudo tar -C /usr/local -xzf go1.24.4.linux-amd64.tar.gz
/usr/local/go/bin/go version
```

Expected: `go version go1.24.4 linux/amd64`. If sudo is unavailable, install to `$HOME/go-toolchain` and export `PATH=$HOME/go-toolchain/go/bin:$PATH` for all later steps.

- [ ] **Step 2: Record toolchain for the repo**

```bash
/usr/local/go/bin/go env -w GOTOOLCHAIN=auto
```

### Task 1: Module scaffold

**Files:**
- Create: `mcp/go/go.mod`
- Create: `mcp/go/cmd/kite-algo-mcp/main.go`
- Create: `mcp/go/internal/version/version.go`
- Test: `mcp/go/internal/version/version_test.go`

- [ ] **Step 1: Scaffold and fetch the SDK**

```bash
mkdir -p mcp/go/cmd/kite-algo-mcp mcp/go/internal/{version,catalog,backend,transport}
cd mcp/go
/usr/local/go/bin/go mod init kitealgo/kite-algo-mcp
/usr/local/go/bin/go get github.com/modelcontextprotocol/go-sdk@latest
```

If the SDK's minimum Go exceeds the installed toolchain, upgrade the toolchain; do not fork the SDK.

- [ ] **Step 2: Write the failing test**

`mcp/go/internal/version/version_test.go`:

```go
package version

import "testing"

func TestVersionIsSet(t *testing.T) {
	if Version == "" {
		t.Fatal("Version must not be empty")
	}
}
```

- [ ] **Step 3: Run it (expect compile failure)**

```bash
/usr/local/go/bin/go test ./internal/version/
```

Expected: FAIL (`Version` undefined).

- [ ] **Step 4: Implement**

`mcp/go/internal/version/version.go`:

```go
package version

// Version is overridden at build time via -ldflags.
var Version = "0.1.0-dev"
```

- [ ] **Step 5: Pass + commit**

```bash
/usr/local/go/bin/go test ./internal/version/ && \
git add mcp/go/go.mod mcp/go/go.sum mcp/go/cmd mcp/go/internal && \
git commit --no-gpg-sign -m "feat(mcp-go): scaffold go module with mcp sdk"
```

### Task 2: Catalog export script (Python → catalog.json)

**Files:**
- Create: `mcp/go/tools/export_catalog.py`
- Create (generated): `mcp/go/internal/catalog/catalog.json`
- Create (golden): `tests/mcp/fixtures/go_parity_tool_names.json` (generated)

- [ ] **Step 1: Write the export script**

`mcp/go/tools/export_catalog.py`:

```python
"""Export the reviewed MCP catalog to JSON for the Go adapter.

Reads the Python adapter's catalog (specs) and contracts (JSON Schemas),
writes internal/catalog/catalog.json. Run from repo root:

    python3 mcp/go/tools/export_catalog.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "mcp" / "python"))

from kite_algo_mcp.catalog import TOOL_CATALOG, TOOL_SPECS  # noqa: E402
from kite_algo_mcp import contracts  # noqa: E402


def _schema_for(model_cls) -> dict:
    return model_cls.model_json_schema()


def main() -> None:
    tools = []
    for spec in TOOL_SPECS:
        entry = {
            "name": spec.name,
            "group": spec.group,
            "required_action": spec.required_action,
            "effect": spec.effect,
            "scope": spec.scope,
            "idempotent": spec.idempotent,
            "description": spec.description,
            "live_only": spec.live_only,
            "reconcile_with": spec.reconcile_with,
        }
        tools.append(entry)
    payload = {"tools": tools}
    out = REPO / "mcp/go/internal/catalog/catalog.json"
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    golden = REPO / "tests/mcp/fixtures/go_parity_tool_names.json"
    golden.parent.mkdir(parents=True, exist_ok=True)
    golden.write_text(json.dumps(sorted(TOOL_CATALOG.keys()), indent=2) + "\n")
    print(f"wrote {len(tools)} tools -> {out}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run and verify**

```bash
python3 mcp/go/tools/export_catalog.py
python3 -c "import json; d=json.load(open('mcp/go/internal/catalog/catalog.json')); print(len(d['tools']))"
```

Expected: `wrote 73 tools` and `73`.

- [ ] **Step 3: Commit generator + generated artifacts**

```bash
git add mcp/go/tools/export_catalog.py mcp/go/internal/catalog/catalog.json tests/mcp/fixtures/go_parity_tool_names.json
git commit --no-gpg-sign -m "feat(mcp-go): export reviewed catalog to embedded json"
```

### Task 3: Embed catalog in Go

**Files:**
- Create: `mcp/go/internal/catalog/catalog.go`
- Test: `mcp/go/internal/catalog/catalog_test.go`

- [ ] **Step 1: Failing test**

```go
package catalog

import (
	"encoding/json"
	"os"
	"testing"
)

func TestEmbeddedCatalogMatchesGoldenNames(t *testing.T) {
	golden, err := os.ReadFile("../../../tests/mcp/fixtures/go_parity_tool_names.json")
	if err != nil {
		t.Fatalf("golden fixture: %v", err)
	}
	var want []string
	if err := json.Unmarshal(golden, &want); err != nil {
		t.Fatalf("golden json: %v", err)
	}
	got := make([]string, 0, len(Tools))
	for _, tool := range Tools {
		got = append(got, tool.Name)
	}
	if len(got) != len(want) {
		t.Fatalf("tool count: got %d want %d", len(got), len(want))
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("tool[%d]: got %q want %q", i, got[i], want[i])
		}
	}
}
```

- [ ] **Step 2: Run (FAIL — Tools undefined)**

```bash
/usr/local/go/bin/go test ./internal/catalog/
```

- [ ] **Step 3: Implement**

`mcp/go/internal/catalog/catalog.go`:

```go
// Package catalog embeds the reviewed MCP tool catalog exported from the
// Python adapter. The exported file is the single source of truth; regenerate
// with mcp/go/tools/export_catalog.py after changing catalog.py/contracts.py.
package catalog

import (
	_ "embed"
	"encoding/json"
)

// Spec mirrors kite_algo_mcp.catalog.ToolSpec.
type Spec struct {
	Name           string `json:"name"`
	Group          string `json:"group"`
	RequiredAction string `json:"required_action"`
	Effect         string `json:"effect"`   // read | data_write | trade_write
	Scope          string `json:"scope"`    // none | run | account
	Idempotent     bool   `json:"idempotent"`
	Description    string `json:"description"`
	LiveOnly       bool   `json:"live_only"`
	ReconcileWith  *string `json:"reconcile_with"`
}

type file struct {
	Tools []Spec `json:"tools"`
}

//go:embed catalog.json
var embedded file

var Tools = embedded.Tools

// ByName indexes the catalog for O(1) lookups.
var ByName = func() map[string]Spec {
	m := make(map[string]Spec, len(Tools))
	for _, spec := range Tools {
		m[spec.Name] = spec
	}
	return m
}()
```

- [ ] **Step 4: Pass + commit**

```bash
/usr/local/go/bin/go test ./internal/catalog/ && git add mcp/go/internal/catalog && git commit --no-gpg-sign -m "feat(mcp-go): embed 73-tool catalog with golden parity test"
```

### Task 4: Backend HTTP client

**Files:**
- Create: `mcp/go/internal/backend/client.go`
- Test: `mcp/go/internal/backend/client_test.go`

- [ ] **Step 1: Failing test**

```go
package backend

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestGetForwardsBearerAndDecodes(t *testing.T) {
	var gotAuth, gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		gotPath = r.URL.Path
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{"ok": true})
	}))
	defer srv.Close()

	c := New(srv.URL, "secret-1", 5)
	resp, err := c.Get(context.Background(), "/api/algo-workers/worker/health")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if gotAuth != "Bearer secret-1" {
		t.Fatalf("auth: %q", gotAuth)
	}
	if gotPath != "/api/algo-workers/worker/health" {
		t.Fatalf("path: %q", gotPath)
	}
	if resp["ok"] != true {
		t.Fatalf("resp: %v", resp)
	}
}

func TestPostSendsJSONBody(t *testing.T) {
	var body map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewDecoder(r.Body).Decode(&body)
		json.NewEncoder(w).Encode(map[string]any{"echo": body})
	}))
	defer srv.Close()

	c := New(srv.URL, "t", 5)
	resp, err := c.Post(context.Background(), "/api/algo-workers/worker/indicators", map[string]any{"name": "sma"})
	if err != nil {
		t.Fatalf("Post: %v", err)
	}
	echo := resp["echo"].(map[string]any)
	if echo["name"] != "sma" {
		t.Fatalf("echo: %v", echo)
	}
}
```

- [ ] **Step 2: Run (FAIL)** — `go test ./internal/backend/`

- [ ] **Step 3: Implement**

`mcp/go/internal/backend/client.go`:

```go
// Package backend is the stdlib HTTP client for the worker API.
package backend

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

type Client struct {
	base  string
	token string
	http  *http.Client
}

func New(baseURL, workerToken string, timeoutSeconds int) *Client {
	return &Client{
		base:  strings.TrimRight(baseURL, "/"),
		token: workerToken,
		http:  &http.Client{Timeout: time.Duration(timeoutSeconds) * time.Second},
	}
}

func (c *Client) Get(ctx context.Context, path string) (map[string]any, error) {
	return c.do(ctx, http.MethodGet, path, nil)
}

func (c *Client) Post(ctx context.Context, path string, payload any) (map[string]any, error) {
	return c.do(ctx, http.MethodPost, path, payload)
}

func (c *Client) do(ctx context.Context, method, path string, payload any) (map[string]any, error) {
	var body io.Reader
	if payload != nil {
		encoded, err := json.Marshal(payload)
		if err != nil {
			return nil, fmt.Errorf("encode payload: %w", err)
		}
		body = bytes.NewReader(encoded)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.base+path, body)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	if payload != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, &HTTPError{Status: resp.StatusCode, Body: raw}
	}
	out := map[string]any{}
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &out); err != nil {
			return nil, fmt.Errorf("decode %s %s: %w", method, path, err)
		}
	}
	return out, nil
}

// HTTPError preserves backend status + body for Phase 2 error taxonomy.
type HTTPError struct {
	Status int
	Body   []byte
}

func (e *HTTPError) Error() string {
	return fmt.Sprintf("worker api %d: %s", e.Status, strings.TrimSpace(string(e.Body)))
}
```

- [ ] **Step 4: Pass + commit** — `go test ./internal/backend/ && git add ... && git commit --no-gpg-sign -m "feat(mcp-go): stdlib worker api client"`

### Task 5: HTTP server skeleton with /healthz (stdlib, no MCP yet)

**Files:**
- Create: `mcp/go/internal/transport/http.go`
- Test: `mcp/go/internal/transport/http_test.go`

- [ ] **Step 1: Failing test**

```go
package transport

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestHealthzOK(t *testing.T) {
	h := New(Options{}) // no auth configured yet
	srv := httptest.NewServer(h.Handler())
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != 200 {
		t.Fatalf("healthz: %d", resp.StatusCode)
	}
}
```

- [ ] **Step 2: Run (FAIL)**

- [ ] **Step 3: Implement**

`mcp/go/internal/transport/http.go`:

```go
// Package transport hosts the MCP streamable-HTTP endpoint and health probe.
package transport

import (
	"net/http"
)

type Options struct {
	// Phase 3 adds: BearerToken, AllowedHosts, AllowedOrigins.
}

func New(opts Options) *Server { return &Server{opts: opts} }

type Server struct{ opts Options }

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	// Task 7 mounts the MCP streamable handler at "/" here.
	return mux
}
```

- [ ] **Step 4: Pass + commit**

### Task 6: MCP server assembly — tools/list parity with Python

**Files:**
- Modify: `mcp/go/cmd/kite-algo-mcp/main.go`
- Create: `mcp/go/internal/adapter/server.go`
- Test: `mcp/go/internal/adapter/server_test.go`

The SDK's exact construction API is resolved in this task from the vendored module docs (`go doc github.com/modelcontextprotocol/go-sdk/mcp.NewServer`, `...StreamableHTTPHandler`); the structural requirements below are fixed.

- [ ] **Step 1: Failing test** — construct the adapter server, list tools through the SDK's in-memory client, assert the 73 names match the golden fixture and each inputSchema carries `additionalProperties: false` semantics matching the exported schema.

- [ ] **Step 2: Run (FAIL)**

- [ ] **Step 3: Implement** `internal/adapter/server.go`: build one `*mcp.Server`; for each `catalog.Spec`, register a tool whose name/description/annotations come from the spec and whose input schema comes from `contracts` JSON Schemas (exported in Phase 1b — see Task 6b). Dispatch handler in Phase 1 returns a structured `not_implemented` tool error; parity of tools/list is the deliverable.

- [ ] **Step 3b (Task 6b): extend export script** to emit each tool's JSON Schema (`model_json_schema()` of the request model from `contracts.py`, resolved via a name→model map in the script) into `catalog.json` as `input_schema`; regenerate; extend the Task 3 golden test to assert `input_schema` present and `$schema`-valid JSON for all 73.

- [ ] **Step 4: main.go** wires flags: `--http --host 0.0.0.0 --port 8788 --api-url URL --worker-token T` (stdio flag reserved Phase 3); mounts adapter handler via `transport`.

- [ ] **Step 5: Pass + commit** — `go test ./... && git commit --no-gpg-sign -m "feat(mcp-go): serve 73-tool catalog over streamable http"`

### Task 7: Contract harness — pytest against the Go binary

**Files:**
- Modify: `tests/mcp/conftest.py` (add `KITE_MCP_PARITY_BIN` env switch: when set, session-scoped fixture spawns the Go binary on a free port and points tests at it; default stays Python)

- [ ] **Step 1:** implement env switch; **Step 2:** `KITE_MCP_PARITY_BIN=/path/kite-algo-mcp pytest tests/mcp/test_catalog_contract.py -q` → tool-list assertions pass against Go; **Step 3:** document expected failures (dispatch semantics = Phase 2) in `tests/mcp/README.md`; **Step 4:** commit.

### Task 8: Dockerfile + compose parity service

**Files:**
- Create: `mcp/go/Dockerfile` (multi-stage `golang:1.24-alpine` build → `FROM scratch`, copy binary + CA certs, `USER 10001`, EXPOSE 8788, HEALTHCHECK against /healthz)
- Modify: `compose.yml` (add `mcp-go` service, profile `mcp`, port `18789:8788`, same env contract as `mcp`)

- [ ] Steps: build image (`docker build -f mcp/go/Dockerfile -t kite-algo-mcp-go:dev mcp/go`), `docker stats` RSS < 40 MiB (Phase 1 gate), add compose service, commit.

### Task 9: Phase gate review

Run: golden parity test, contract harness subset, RSS measurement, `go vet ./...`, `gofmt -l .`. Record results in this file under "Phase 1 outcome", then plan Phase 2 (policy/sessions/error taxonomy — spec §5 items 5-13) and Phase 3 (auth/hosts/origins, stdio, comparison matrix §7.3, cutover).

## Phase 1 outcome

**Complete (2026-09-08).** All nine tasks green.

- Toolchain: go1.24 installed; SDK auto-chained GOTOOLCHAIN to go1.25.0.
- Module `mcp/go` on `github.com/modelcontextprotocol/go-sdk` v1.7.0 (only dependency).
- Catalog exporter (`tools/export_catalog.py`) emits all 73 tools with
  request-model JSON Schemas (pydantic `model_json_schema`) plus synthesized
  schemas for the 8 ad-hoc-arg tools; golden fixture pins names.
- Embedded catalog passes golden name parity + schema presence + ByName tests.
- Backend client (bearer, typed HTTPError) and /healthz transport tested.
- Adapter serves the full catalog over the SDK: tools/list parity proven via
  in-memory session, pytest harness (`tests/mcp/test_go_parity.py`, run with
  KITE_MCP_PARITY_BIN set) AND a live curl streamable-HTTP handshake (73 tools).
- Dockerfile: multi-stage alpine build -> **19.6 MB image** (Python: 230 MB);
  compose override `compose.mcp-go.yml` runs `mcp-go` on 18789.
- **Phase gate: RSS 8.0-8.1 MiB serving tools/list in Docker -- target <40 MiB
  exceeded by 5x.** go vet clean, gofmt clean.

Remaining before cutover (Phase 2): dispatch semantics -- schema validation,
policy/profiles/capability filtering, run leases with heartbeats, error
taxonomy, concurrency semaphore, result caps (spec section 5) -- each with
tests; then Phase 3 hardening + comparison matrix + cutover.


## Phase 2+3 outcome (2026-09-08, commit fff3e2c)

Phase 2 (safeguards) and the Phase 3 transport items are implemented and
tested. Deployment decision per user: NO cutover - python (18788) and go
(18789) adapters run side by side as permanent containers.

- Dispatch table: 73 entries generated from endpoint_manifest + tool
  closures; 13 trade_write tools under leases; safety pre-check on
  place_order/place_basket/enter_option_run.
- Safeguards tests: policy (visibility, capability normalization,
  action/mode/account gates, empty-template rule), session (lifecycle,
  heartbeat-loss refusal, outcome-unknown), dispatch (unknown tool,
  policy refusal, status taxonomy, lease claim/nonce/release, safety
  refusal, claim-failure mapping, shapers).
- Transport: bearer guard honored from KITE_MCP_HTTP_TOKEN; /mcp +
  stateless JSON to mirror the python adapter.
- E2E through BOTH containers vs live backend: 73/73 names, caps MATCH,
  calculate_indicator MATCH (go->backend->SDK pandas), funds/search OK,
  backend_action_denied identical. Go RSS ~10-16 MiB in Docker.
