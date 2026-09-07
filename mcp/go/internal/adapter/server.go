// Package adapter assembles the MCP server from the embedded catalog.
// Phase 1 delivers tools/list parity: every cataloged tool is served with its
// exported schema and annotations; call dispatch lands in Phase 2 with the
// full policy/lease/error semantics (spec §5).
package adapter

import (
	"context"
	"fmt"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"kitealgo/kite-algo-mcp/internal/catalog"
	"kitealgo/kite-algo-mcp/internal/version"
)

// WorkerClient is the backend surface the dispatcher uses (Phase 2).
type WorkerClient interface {
	Get(ctx context.Context, path string) (map[string]any, error)
	Post(ctx context.Context, path string, payload any) (map[string]any, error)
}

// New builds an MCP server exposing every cataloged tool.
func New(client WorkerClient) *mcp.Server {
	server := mcp.NewServer(&mcp.Implementation{
		Name:    "kite-algo-mcp",
		Version: version.Version,
	}, nil)

	for i := range catalog.Tools {
		spec := catalog.Tools[i]
		server.AddTool(toolFromSpec(spec), dispatch(spec, client))
	}
	return server
}

func toolFromSpec(spec catalog.Spec) *mcp.Tool {
	tool := &mcp.Tool{
		Name:        spec.Name,
		Description: spec.Description,
		InputSchema: spec.InputSchema,
		Annotations: &mcp.ToolAnnotations{
			ReadOnlyHint:   spec.Effect == "read",
			IdempotentHint: spec.Idempotent,
		},
	}
	return tool
}

func dispatch(spec catalog.Spec, client WorkerClient) mcp.ToolHandler {
	return func(ctx context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		// Phase 2 replaces this with schema validation -> policy authorize ->
		// lease/semaphore -> backend call -> error taxonomy (spec §5).
		return nil, fmt.Errorf("not_implemented: %s dispatch arrives with phase 2", spec.Name)
	}
}
