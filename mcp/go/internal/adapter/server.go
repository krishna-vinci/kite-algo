// Package adapter assembles the MCP server from the embedded catalog and the
// dispatch invoker. Registration is table-driven: schemas and annotations come
// from the exported catalog; every call goes through the reviewed safeguards.
package adapter

import (
	"context"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"kitealgo/kite-algo-mcp/internal/catalog"
	"kitealgo/kite-algo-mcp/internal/dispatch"
	"kitealgo/kite-algo-mcp/internal/version"
)

// New builds an MCP server exposing every cataloged tool. A nil invoker keeps
// Phase 1 behavior (listing works, calls error) for pure parity harnesses.
func New(inv *dispatch.Invoker) *mcp.Server {
	server := mcp.NewServer(&mcp.Implementation{
		Name:    "kite-algo-mcp",
		Version: version.Version,
	}, nil)

	for i := range catalog.Tools {
		spec := catalog.Tools[i]
		tool := &mcp.Tool{
			Name:        spec.Name,
			Description: spec.Description,
			InputSchema: spec.InputSchema,
			Annotations: &mcp.ToolAnnotations{
				ReadOnlyHint:   spec.Effect == "read",
				IdempotentHint: spec.Idempotent,
			},
		}
		server.AddTool(tool, func(ctx context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			result := dispatch.NotImplemented
			if inv != nil {
				result = inv.Call(ctx, spec.Name, req.Params.Arguments)
			}
			return &mcp.CallToolResult{
				Content: []mcp.Content{&mcp.TextContent{Text: result.Text}},
				IsError: result.IsError,
			}, nil
		})
	}
	return server
}
