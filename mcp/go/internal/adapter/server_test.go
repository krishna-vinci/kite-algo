package adapter

import (
	"context"
	"encoding/json"
	"os"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func TestListToolsMatchesGoldenCatalog(t *testing.T) {
	golden, err := os.ReadFile("../../../../tests/mcp/fixtures/go_parity_tool_names.json")
	if err != nil {
		t.Fatalf("golden fixture: %v", err)
	}
	var want []string
	if err := json.Unmarshal(golden, &want); err != nil {
		t.Fatalf("golden json: %v", err)
	}

	server := New(nil)
	serverTransport, clientTransport := mcp.NewInMemoryTransports()
	ctx := context.Background()
	go func() { _ = server.Run(ctx, serverTransport) }()

	client := mcp.NewClient(&mcp.Implementation{Name: "parity-test", Version: "0"}, nil)
	session, err := client.Connect(ctx, clientTransport, nil)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer session.Close()

	listed, err := session.ListTools(ctx, nil)
	if err != nil {
		t.Fatalf("list tools: %v", err)
	}
	if len(listed.Tools) != len(want) {
		t.Fatalf("tool count: got %d want %d", len(listed.Tools), len(want))
	}
	for i := range want {
		if listed.Tools[i].Name != want[i] {
			t.Fatalf("tool[%d]: got %q want %q", i, listed.Tools[i].Name, want[i])
		}
	}
}

func TestAnnotationsReflectSpec(t *testing.T) {
	server := New(nil)
	serverTransport, clientTransport := mcp.NewInMemoryTransports()
	ctx := context.Background()
	go func() { _ = server.Run(ctx, serverTransport) }()

	client := mcp.NewClient(&mcp.Implementation{Name: "parity-test", Version: "0"}, nil)
	session, err := client.Connect(ctx, clientTransport, nil)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer session.Close()

	listed, err := session.ListTools(ctx, nil)
	if err != nil {
		t.Fatalf("list tools: %v", err)
	}
	byName := map[string]*mcp.Tool{}
	for _, tool := range listed.Tools {
		byName[tool.Name] = tool
	}
	readTool := byName["get_quotes"]
	if readTool == nil || readTool.Annotations == nil || !readTool.Annotations.ReadOnlyHint {
		t.Fatalf("get_quotes must be annotated read-only: %+v", readTool)
	}
	indicator := byName["calculate_indicator"]
	if indicator == nil {
		t.Fatal("calculate_indicator missing from listing")
	}
	schemaBytes, err := json.Marshal(indicator.InputSchema)
	if err != nil || len(schemaBytes) < 10 {
		t.Fatalf("calculate_indicator must carry its exported schema: %s (%v)", schemaBytes, err)
	}
}
