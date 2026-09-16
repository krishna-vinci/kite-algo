# MCP catalog source

This directory is **not** an MCP server. The Python adapter was retired at the
Phase 3 cutover (2026-09-11) and its server, image, packaging and tests are gone.
The Go adapter in [`../go`](../go) is the only MCP runtime.

Two modules survive because the Go adapter's catalog is still authored here and
exported into the binary:

| File | Role |
|---|---|
| `kite_algo_mcp/catalog.py` | The reviewed tool catalog: name, group, effect, scope, required action, live-only flag, description, reconcile-with. |
| `kite_algo_mcp/contracts.py` | The pydantic request models whose JSON schemas become each tool's `inputSchema`. |

[`../go/tools/export_catalog.py`](../go/tools/export_catalog.py) loads both files
standalone (stdlib plus pydantic only — it never imports the old package
`__init__`) and writes:

- `mcp/go/internal/catalog/catalog.json`, embedded into the Go binary via
  `//go:embed` in [`../go/internal/catalog/catalog.go`](../go/internal/catalog/catalog.go)
- `tests/mcp/fixtures/go_parity_tool_names.json`, the golden fixture the Go tests
  and the HTTP parity smoke test assert against

Regenerate after any change to the catalog or the contracts:

```sh
python3 mcp/go/tools/export_catalog.py
```

CI re-runs the exporter and fails if either generated file drifts, so a catalog
change that is not exported cannot merge. Editing `catalog.json` by hand is
always wrong — change `catalog.py` or `contracts.py` and re-export.
