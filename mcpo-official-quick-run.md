We recommend using uv for lightning-fast startup and zero config.

uvx mcpo --port 8000 --api-key "top-secret" -- your_mcp_server_command
Or, if you’re using Python:

pip install mcpo
mcpo --port 8000 --api-key "top-secret" -- your_mcp_server_command
To use an SSE-compatible MCP server, simply specify the server type and endpoint:

mcpo --port 8000 --api-key "top-secret" --server-type "sse" -- http://127.0.0.1:8001/sse
You can also provide headers for the SSE connection:

mcpo --port 8000 --api-key "top-secret" --server-type "sse" --header '{"Authorization": "Bearer token", "X-Custom-Header": "value"}' -- http://127.0.0.1:8001/sse
To use a Streamable HTTP-compatible MCP server, specify the server type and endpoint:

mcpo --port 8000 --api-key "top-secret" --server-type "streamable-http" -- http://127.0.0.1:8002/mcp
You can also run mcpo via Docker with no installation:

docker run -p 8000:8000 ghcr.io/open-webui/mcpo:main --api-key "top-secret" -- your_mcp_server_command
Example:

uvx mcpo --port 8000 --api-key "top-secret" -- uvx mcp-server-time --local-timezone=America/New_York
That’s it. Your MCP tool is now available at http://localhost:8000 with a generated OpenAPI schema — test it live at http://localhost:8000/docs.

🤝 To integrate with Open WebUI after launching the server, check our docs.

🔄 Using a Config File

You can serve multiple MCP tools via a single config file that follows the Claude Desktop format.

Enable hot-reload mode with --hot-reload to automatically watch your config file for changes and reload servers without downtime:

Start via:

mcpo --config /path/to/config.json
Or with hot-reload enabled:

mcpo --config /path/to/config.json --hot-reload
Example config.json:

{
  "mcpServers": {
    "memory": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-memory"]
    },
    "time": {
      "command": "uvx",
      "args": ["mcp-server-time", "--local-timezone=America/New_York"]
    },
    "mcp_sse": {
      "type": "sse", // Explicitly define type
      "url": "http://127.0.0.1:8001/sse",
      "headers": {
        "Authorization": "Bearer token",
        "X-Custom-Header": "value"
      }
    },
    "mcp_streamable_http": {
      "type": "streamable-http",
      "url": "http://127.0.0.1:8002/mcp"
    } // Streamable HTTP MCP Server
  }
}
Each tool will be accessible under its own unique route, e.g.:

http://localhost:8000/memory
http://localhost:8000/time
Each with a dedicated OpenAPI schema and proxy handler. Access full schema UI at: http://localhost:8000/<tool>/docs (e.g. /memory/docs, /time/docs)

