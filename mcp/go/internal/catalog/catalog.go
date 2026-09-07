// Package catalog embeds the reviewed MCP tool catalog exported from the
// Python adapter. The exported file is the single source of truth; regenerate
// with mcp/go/tools/export_catalog.py after changing catalog.py/contracts.py.
package catalog

import (
	_ "embed"
	"encoding/json"
	"fmt"
)

// Spec mirrors kite_algo_mcp.catalog.ToolSpec plus its request schema.
type Spec struct {
	Name           string          `json:"name"`
	Group          string          `json:"group"`
	RequiredAction string          `json:"required_action"`
	Effect         string          `json:"effect"` // read | data_write | trade_write
	Scope          string          `json:"scope"`  // none | run | account
	Idempotent     bool            `json:"idempotent"`
	Description    string          `json:"description"`
	LiveOnly       bool            `json:"live_only"`
	ReconcileWith  *string         `json:"reconcile_with"`
	InputSchema    json.RawMessage `json:"input_schema"`
}

type file struct {
	Tools []Spec `json:"tools"`
}

//go:embed catalog.json
var embedded []byte

// Tools holds the catalog sorted by name; order matches the golden fixture.
var Tools = func() []Spec {
	var f file
	if err := json.Unmarshal(embedded, &f); err != nil {
		panic(fmt.Sprintf("catalog.json: %v", err))
	}
	return f.Tools
}()

// ByName indexes the catalog for O(1) lookups.
var ByName = func() map[string]Spec {
	m := make(map[string]Spec, len(Tools))
	for _, spec := range Tools {
		m[spec.Name] = spec
	}
	return m
}()
