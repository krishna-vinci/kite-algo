package catalog

import (
	"encoding/json"
	"os"
	"testing"
)

func TestEmbeddedCatalogMatchesGoldenNames(t *testing.T) {
	golden, err := os.ReadFile("../../../../tests/mcp/fixtures/go_parity_tool_names.json")
	if err != nil {
		t.Fatalf("golden fixture: %v", err)
	}
	var want []string
	if err := json.Unmarshal(golden, &want); err != nil {
		t.Fatalf("golden json: %v", err)
	}
	if len(Tools) != len(want) {
		t.Fatalf("tool count: got %d want %d", len(Tools), len(want))
	}
	for i := range want {
		if Tools[i].Name != want[i] {
			t.Fatalf("tool[%d]: got %q want %q", i, Tools[i].Name, want[i])
		}
	}
}

func TestEveryToolHasNonEmptyInputSchema(t *testing.T) {
	for _, spec := range Tools {
		if len(spec.InputSchema) == 0 {
			t.Fatalf("tool %q has no input schema", spec.Name)
		}
		var probe map[string]any
		if err := json.Unmarshal(spec.InputSchema, &probe); err != nil {
			t.Fatalf("tool %q schema invalid json: %v", spec.Name, err)
		}
	}
}

func TestByNameLookup(t *testing.T) {
	spec, ok := ByName["calculate_indicator"]
	if !ok {
		t.Fatal("calculate_indicator missing from ByName")
	}
	if spec.RequiredAction != "market:read" {
		t.Fatalf("calculate_indicator action: %q", spec.RequiredAction)
	}
}
