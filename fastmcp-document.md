Welcome! This guide will help you quickly set up FastMCP, run your first MCP server, and deploy a server to FastMCP Cloud. If you haven’t already installed FastMCP, follow the [installation instructions](https://gofastmcp.com/getting-started/installation).

## Create a FastMCP Server

A FastMCP server is a collection of tools, resources, and other MCP components. To create a server, start by instantiating the `FastMCP` class. Create a new file called `my_server.py` and add the following code:

```
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP</span></span>
<span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span><span>"My MCP Server"</span><span>)</span></span>
```

That’s it! You’ve created a FastMCP server, albeit a very boring one. Let’s add a tool to make it more interesting.

To add a tool that returns a simple greeting, write a function and decorate it with `@mcp.tool` to register it with the server:

```
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP</span></span>
<span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span><span>"My MCP Server"</span><span>)</span></span>
<span></span>
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> greet</span><span>(</span><span>name</span><span>: </span><span>str</span><span>) -&gt; </span><span>str</span><span>:</span></span>
<span><span>    return</span><span> f</span><span>"Hello, </span><span>{</span><span>name</span><span>}</span><span>!"</span></span>
```

## Run the Server

The simplest way to run your FastMCP server is to call its `run()` method. You can choose between different transports, like `stdio` for local servers, or `http` for remote access:

This lets us run the server with `python my_server.py`. The stdio transport is the traditional way to connect MCP servers to clients, while the HTTP transport enables remote connections.

### Using the FastMCP CLI

You can also use the `fastmcp run` command to start your server. Note that the FastMCP CLI **does not** execute the `__main__` block of your server file. Instead, it imports your server object and runs it with whatever transport and options you provide. For example, to run this server with the default stdio transport (no matter how you called `mcp.run()`), you can use the following command:

```
<span><span>fastmcp</span><span> run</span><span> my_server.py:mcp</span></span>
```

To run this server with the HTTP transport, you can use the following command:

```
<span><span>fastmcp</span><span> run</span><span> my_server.py:mcp</span><span> --transport</span><span> http</span><span> --port</span><span> 8000</span></span>
```

## Call Your Server

Once your server is running with HTTP transport, you can connect to it with a FastMCP client or any LLM client that supports the MCP protocol:

```
<span><span>import</span><span> asyncio</span></span>
<span><span>from</span><span> fastmcp </span><span>import</span><span> Client</span></span>
<span></span>
<span><span>client </span><span>=</span><span> Client(</span><span>"http://localhost:8000"</span><span>)</span></span>
<span></span>
<span><span>async</span><span> def</span><span> call_tool</span><span>(</span><span>name</span><span>: </span><span>str</span><span>):</span></span>
<span><span>    async</span><span> with</span><span> client:</span></span>
<span><span>        result </span><span>=</span><span> await</span><span> client.call_tool(</span><span>"greet"</span><span>, {</span><span>"name"</span><span>: name})</span></span>
<span><span>        print</span><span>(result)</span></span>
<span></span>
<span><span>asyncio.run(call_tool(</span><span>"Ford"</span><span>))</span></span>
```

Note that:

-   FastMCP clients are asynchronous, so we need to use `asyncio.run` to run the client
-   We must enter a client context (`async with client:`) before using the client
-   You can make multiple client calls within the same context

## Deploy to FastMCP Cloud

[FastMCP Cloud](https://fastmcp.cloud/) is a hosting service run by the FastMCP team at [Prefect](https://www.prefect.io/fastmcp). It is optimized to deploy authenticated FastMCP servers as quickly as possible, giving you a secure URL that you can plug into any LLM client.

To deploy your server, you’ll need a [GitHub account](https://github.com/). Once you have one, you can deploy your server in three steps:

1.  Push your `my_server.py` file to a GitHub repository
2.  Sign in to [FastMCP Cloud](https://fastmcp.cloud/) with your GitHub account
3.  Create a new project from your repository and enter `my_server.py:mcp` as the server entrypoint

That’s it! FastMCP Cloud will build and deploy your server, making it available at a URL like `https://your-project.fastmcp.app/mcp`. You can chat with it to test its functionality, or connect to it from any LLM client that supports the MCP protocol. For more details, see the [FastMCP Cloud guide](https://gofastmcp.com/deployment/fastmcp-cloud).



The central piece of a FastMCP application is the `FastMCP` server class. This class acts as the main container for your application’s tools, resources, and prompts, and manages communication with MCP clients.

## Creating a Server

Instantiating a server is straightforward. You typically provide a name for your server, which helps identify it in client applications or logs.

```
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP</span></span>
<span></span>
<span><span># Create a basic server instance</span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span><span>name</span><span>=</span><span>"MyAssistantServer"</span><span>)</span></span>
<span></span>
<span><span># You can also add instructions for how to interact with the server</span></span>
<span><span>mcp_with_instructions </span><span>=</span><span> FastMCP(</span></span>
<span><span>    name</span><span>=</span><span>"HelpfulAssistant"</span><span>,</span></span>
<span><span>    instructions</span><span>=</span><span>"""</span></span>
<span><span>        This server provides data analysis tools.</span></span>
<span><span>        Call get_average() to analyze numerical data.</span></span>
<span><span>    """</span><span>,</span></span>
<span><span>)</span></span>
```

The `FastMCP` constructor accepts several arguments:

## Components

FastMCP servers expose several types of components to the client:

### Tools

Tools are functions that the client can call to perform actions or access external systems.

```
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> multiply</span><span>(</span><span>a</span><span>: </span><span>float</span><span>, </span><span>b</span><span>: </span><span>float</span><span>) -&gt; </span><span>float</span><span>:</span></span>
<span><span>    """Multiplies two numbers together."""</span></span>
<span><span>    return</span><span> a </span><span>*</span><span> b</span></span>
```

See [Tools](https://gofastmcp.com/servers/tools) for detailed documentation.

### Resources

Resources expose data sources that the client can read.

```
<span><span>@mcp.resource</span><span>(</span><span>"data://config"</span><span>)</span></span>
<span><span>def</span><span> get_config</span><span>() -&gt; </span><span>dict</span><span>:</span></span>
<span><span>    """Provides the application configuration."""</span></span>
<span><span>    return</span><span> {</span><span>"theme"</span><span>: </span><span>"dark"</span><span>, </span><span>"version"</span><span>: </span><span>"1.0"</span><span>}</span></span>
```

See [Resources & Templates](https://gofastmcp.com/servers/resources) for detailed documentation.

### Resource Templates

Resource templates are parameterized resources that allow the client to request specific data.

```
<span><span>@mcp.resource</span><span>(</span><span>"users://</span><span>{user_id}</span><span>/profile"</span><span>)</span></span>
<span><span>def</span><span> get_user_profile</span><span>(</span><span>user_id</span><span>: </span><span>int</span><span>) -&gt; </span><span>dict</span><span>:</span></span>
<span><span>    """Retrieves a user's profile by ID."""</span></span>
<span><span>    # The {user_id} in the URI is extracted and passed to this function</span></span>
<span><span>    return</span><span> {</span><span>"id"</span><span>: user_id, </span><span>"name"</span><span>: </span><span>f</span><span>"User </span><span>{</span><span>user_id</span><span>}</span><span>"</span><span>, </span><span>"status"</span><span>: </span><span>"active"</span><span>}</span></span>
```

See [Resources & Templates](https://gofastmcp.com/servers/resources) for detailed documentation.

### Prompts

Prompts are reusable message templates for guiding the LLM.

```
<span><span>@mcp.prompt</span></span>
<span><span>def</span><span> analyze_data</span><span>(</span><span>data_points</span><span>: list[</span><span>float</span><span>]) -&gt; </span><span>str</span><span>:</span></span>
<span><span>    """Creates a prompt asking for analysis of numerical data."""</span></span>
<span><span>    formatted_data </span><span>=</span><span> ", "</span><span>.join(</span><span>str</span><span>(point) </span><span>for</span><span> point </span><span>in</span><span> data_points)</span></span>
<span><span>    return</span><span> f</span><span>"Please analyze these data points: </span><span>{</span><span>formatted_data</span><span>}</span><span>"</span></span>
```

See [Prompts](https://gofastmcp.com/servers/prompts) for detailed documentation.

## Tag-Based Filtering

`` New in version: `2.8.0` `` FastMCP supports tag-based filtering to selectively expose components based on configurable include/exclude tag sets. This is useful for creating different views of your server for different environments or users. Components can be tagged when defined using the `tags` parameter:

```
<span><span>@mcp.tool</span><span>(</span><span>tags</span><span>=</span><span>{</span><span>"public"</span><span>, </span><span>"utility"</span><span>})</span></span>
<span><span>def</span><span> public_tool</span><span>() -&gt; </span><span>str</span><span>:</span></span>
<span><span>    return</span><span> "This tool is public"</span></span>
<span></span>
<span><span>@mcp.tool</span><span>(</span><span>tags</span><span>=</span><span>{</span><span>"internal"</span><span>, </span><span>"admin"</span><span>})</span></span>
<span><span>def</span><span> admin_tool</span><span>() -&gt; </span><span>str</span><span>:</span></span>
<span><span>    return</span><span> "This tool is for admins only"</span></span>
```

The filtering logic works as follows:

-   **Include tags**: If specified, only components with at least one matching tag are exposed
-   **Exclude tags**: Components with any matching tag are filtered out
-   **Precedence**: Exclude tags always take priority over include tags

You configure tag-based filtering when creating your server:

```
<span><span># Only expose components tagged with "public"</span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span><span>include_tags</span><span>=</span><span>{</span><span>"public"</span><span>})</span></span>
<span></span>
<span><span># Hide components tagged as "internal" or "deprecated"  </span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span><span>exclude_tags</span><span>=</span><span>{</span><span>"internal"</span><span>, </span><span>"deprecated"</span><span>})</span></span>
<span></span>
<span><span># Combine both: show admin tools but hide deprecated ones</span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span><span>include_tags</span><span>=</span><span>{</span><span>"admin"</span><span>}, </span><span>exclude_tags</span><span>=</span><span>{</span><span>"deprecated"</span><span>})</span></span>
```

This filtering applies to all component types (tools, resources, resource templates, and prompts) and affects both listing and access.

## Running the Server

FastMCP servers need a transport mechanism to communicate with clients. You typically start your server by calling the `mcp.run()` method on your `FastMCP` instance, often within an `if __name__ == "__main__":` block in your main server script. This pattern ensures compatibility with various MCP clients.

```
<span><span># my_server.py</span></span>
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP</span></span>
<span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span><span>name</span><span>=</span><span>"MyServer"</span><span>)</span></span>
<span></span>
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> greet</span><span>(</span><span>name</span><span>: </span><span>str</span><span>) -&gt; </span><span>str</span><span>:</span></span>
<span><span>    """Greet a user by name."""</span></span>
<span><span>    return</span><span> f</span><span>"Hello, </span><span>{</span><span>name</span><span>}</span><span>!"</span></span>
<span></span>
<span><span>if</span><span> __name__</span><span> ==</span><span> "__main__"</span><span>:</span></span>
<span><span>    # This runs the server, defaulting to STDIO transport</span></span>
<span><span>    mcp.run()</span></span>
<span><span>    </span></span>
<span><span>    # To use a different transport, e.g., HTTP:</span></span>
<span><span>    # mcp.run(transport="http", host="127.0.0.1", port=9000)</span></span>
```

FastMCP supports several transport options:

-   STDIO (default, for local tools)
-   HTTP (recommended for web services, uses Streamable HTTP protocol)
-   SSE (legacy web transport, deprecated)

The server can also be run using the FastMCP CLI. For detailed information on each transport, how to configure them (host, port, paths), and when to use which, please refer to the [**Running Your FastMCP Server**](https://gofastmcp.com/deployment/running-server) guide.

## Custom Routes

When running your server with HTTP transport, you can add custom web routes alongside your MCP endpoint using the `@custom_route` decorator. This is useful for simple endpoints like health checks that need to be served alongside your MCP server:

```
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP</span></span>
<span><span>from</span><span> starlette.requests </span><span>import</span><span> Request</span></span>
<span><span>from</span><span> starlette.responses </span><span>import</span><span> PlainTextResponse</span></span>
<span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span><span>"MyServer"</span><span>)</span></span>
<span></span>
<span><span>@mcp.custom_route</span><span>(</span><span>"/health"</span><span>, </span><span>methods</span><span>=</span><span>[</span><span>"GET"</span><span>])</span></span>
<span><span>async</span><span> def</span><span> health_check</span><span>(</span><span>request</span><span>: Request) -&gt; PlainTextResponse:</span></span>
<span><span>    return</span><span> PlainTextResponse(</span><span>"OK"</span><span>)</span></span>
<span></span>
<span><span>if</span><span> __name__</span><span> ==</span><span> "__main__"</span><span>:</span></span>
<span><span>    mcp.run(</span><span>transport</span><span>=</span><span>"http"</span><span>)  </span><span># Health check at http://localhost:8000/health</span></span>
```

Custom routes are served alongside your MCP endpoint and are useful for:

-   Health check endpoints for monitoring
-   Simple status or info endpoints
-   Basic webhooks or callbacks

For more complex web applications, consider [mounting your MCP server into a FastAPI or Starlette app](https://gofastmcp.com/deployment/self-hosted#integration-with-web-frameworks).

## Composing Servers

`` New in version: `2.2.0` `` FastMCP supports composing multiple servers together using `import_server` (static copy) and `mount` (live link). This allows you to organize large applications into modular components or reuse existing servers. See the [Server Composition](https://gofastmcp.com/servers/composition) guide for full details, best practices, and examples.

```
<span><span># Example: Importing a subserver</span></span>
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP</span></span>
<span><span>import</span><span> asyncio</span></span>
<span></span>
<span><span>main </span><span>=</span><span> FastMCP(</span><span>name</span><span>=</span><span>"Main"</span><span>)</span></span>
<span><span>sub </span><span>=</span><span> FastMCP(</span><span>name</span><span>=</span><span>"Sub"</span><span>)</span></span>
<span></span>
<span><span>@sub.tool</span></span>
<span><span>def</span><span> hello</span><span>(): </span></span>
<span><span>    return</span><span> "hi"</span></span>
<span></span>
<span><span># Mount directly</span></span>
<span><span>main.mount(sub, </span><span>prefix</span><span>=</span><span>"sub"</span><span>)</span></span>
```

## Proxying Servers

`` New in version: `2.0.0` `` FastMCP can act as a proxy for any MCP server (local or remote) using `FastMCP.as_proxy`, letting you bridge transports or add a frontend to existing servers. For example, you can expose a remote SSE server locally via stdio, or vice versa. Proxies automatically handle concurrent operations safely by creating fresh sessions for each request when using disconnected clients. See the [Proxying Servers](https://gofastmcp.com/servers/proxy) guide for details and advanced usage.

```
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP, Client</span></span>
<span></span>
<span><span>backend </span><span>=</span><span> Client(</span><span>"http://example.com/mcp/sse"</span><span>)</span></span>
<span><span>proxy </span><span>=</span><span> FastMCP.as_proxy(backend, </span><span>name</span><span>=</span><span>"ProxyServer"</span><span>)</span></span>
<span><span># Now use the proxy like any FastMCP server</span></span>
```

## OpenAPI Integration

`` New in version: `2.0.0` `` FastMCP can automatically generate servers from OpenAPI specifications or existing FastAPI applications using `FastMCP.from_openapi()` and `FastMCP.from_fastapi()`. This allows you to instantly convert existing APIs into MCP servers without manual tool creation. See the [FastAPI Integration](https://gofastmcp.com/integrations/fastapi) and [OpenAPI Integration](https://gofastmcp.com/integrations/openapi) guides for detailed examples and configuration options.

```
<span><span>import</span><span> httpx</span></span>
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP</span></span>
<span></span>
<span><span># From OpenAPI spec</span></span>
<span><span>spec </span><span>=</span><span> httpx.get(</span><span>"https://api.example.com/openapi.json"</span><span>).json()</span></span>
<span><span>mcp </span><span>=</span><span> FastMCP.from_openapi(</span><span>openapi_spec</span><span>=</span><span>spec, </span><span>client</span><span>=</span><span>httpx.AsyncClient())</span></span>
<span></span>
<span><span># From FastAPI app</span></span>
<span><span>from</span><span> fastapi </span><span>import</span><span> FastAPI</span></span>
<span><span>app </span><span>=</span><span> FastAPI()</span></span>
<span><span>mcp </span><span>=</span><span> FastMCP.from_fastapi(</span><span>app</span><span>=</span><span>app)</span></span>
```

## Server Configuration

Servers can be configured using a combination of initialization arguments, global settings, and transport-specific settings.

### Server-Specific Configuration

Server-specific settings are passed when creating the `FastMCP` instance and control server behavior:

```
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP</span></span>
<span></span>
<span><span># Configure server-specific settings</span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span></span>
<span><span>    name</span><span>=</span><span>"ConfiguredServer"</span><span>,</span></span>
<span><span>    include_tags</span><span>=</span><span>{</span><span>"public"</span><span>, </span><span>"api"</span><span>},              </span><span># Only expose these tagged components</span></span>
<span><span>    exclude_tags</span><span>=</span><span>{</span><span>"internal"</span><span>, </span><span>"deprecated"</span><span>},     </span><span># Hide these tagged components</span></span>
<span><span>    on_duplicate_tools</span><span>=</span><span>"error"</span><span>,                  </span><span># Handle duplicate registrations</span></span>
<span><span>    on_duplicate_resources</span><span>=</span><span>"warn"</span><span>,</span></span>
<span><span>    on_duplicate_prompts</span><span>=</span><span>"replace"</span><span>,</span></span>
<span><span>    include_fastmcp_meta</span><span>=</span><span>False</span><span>,                  </span><span># Disable FastMCP metadata for cleaner integration</span></span>
<span><span>)</span></span>
```

### Global Settings

Global settings affect all FastMCP servers and can be configured via environment variables (prefixed with `FASTMCP_`) or in a `.env` file:

```
<span><span>import</span><span> fastmcp</span></span>
<span></span>
<span><span># Access global settings</span></span>
<span><span>print</span><span>(fastmcp.settings.log_level)        </span><span># Default: "INFO"</span></span>
<span><span>print</span><span>(fastmcp.settings.mask_error_details)  </span><span># Default: False</span></span>
<span><span>print</span><span>(fastmcp.settings.resource_prefix_format)  </span><span># Default: "path"</span></span>
<span><span>print</span><span>(fastmcp.settings.include_fastmcp_meta)   </span><span># Default: True</span></span>
```

Common global settings include:

-   **`log_level`**: Logging level (“DEBUG”, “INFO”, “WARNING”, “ERROR”, “CRITICAL”), set with `FASTMCP_LOG_LEVEL`
-   **`mask_error_details`**: Whether to hide detailed error information from clients, set with `FASTMCP_MASK_ERROR_DETAILS`
-   **`resource_prefix_format`**: How to format resource prefixes (“path” or “protocol”), set with `FASTMCP_RESOURCE_PREFIX_FORMAT`
-   **`include_fastmcp_meta`**: Whether to include FastMCP metadata in component responses (default: True), set with `FASTMCP_INCLUDE_FASTMCP_META`

### Transport-Specific Configuration

Transport settings are provided when running the server and control network behavior:

```
<span><span># Configure transport when running</span></span>
<span><span>mcp.run(</span></span>
<span><span>    transport</span><span>=</span><span>"http"</span><span>,</span></span>
<span><span>    host</span><span>=</span><span>"0.0.0.0"</span><span>,           </span><span># Bind to all interfaces</span></span>
<span><span>    port</span><span>=</span><span>9000</span><span>,                </span><span># Custom port</span></span>
<span><span>    log_level</span><span>=</span><span>"DEBUG"</span><span>,        </span><span># Override global log level</span></span>
<span><span>)</span></span>
<span></span>
<span><span># Or for async usage</span></span>
<span><span>await</span><span> mcp.run_async(</span></span>
<span><span>    transport</span><span>=</span><span>"http"</span><span>, </span></span>
<span><span>    host</span><span>=</span><span>"127.0.0.1"</span><span>,</span></span>
<span><span>    port</span><span>=</span><span>8080</span><span>,</span></span>
<span><span>)</span></span>
```

### Setting Global Configuration

Global FastMCP settings can be configured via environment variables (prefixed with `FASTMCP_`):

```
<span><span># Configure global FastMCP behavior</span></span>
<span><span>export</span><span> FASTMCP_LOG_LEVEL</span><span>=</span><span>DEBUG</span></span>
<span><span>export</span><span> FASTMCP_MASK_ERROR_DETAILS</span><span>=</span><span>True</span></span>
<span><span>export</span><span> FASTMCP_RESOURCE_PREFIX_FORMAT</span><span>=</span><span>protocol</span></span>
<span><span>export</span><span> FASTMCP_INCLUDE_FASTMCP_META</span><span>=</span><span>False</span></span>
```

### Custom Tool Serialization

`` New in version: `2.2.7` `` By default, FastMCP serializes tool return values to JSON when they need to be converted to text. You can customize this behavior by providing a `tool_serializer` function when creating your server:

```
<span><span>import</span><span> yaml</span></span>
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP</span></span>
<span></span>
<span><span># Define a custom serializer that formats dictionaries as YAML</span></span>
<span><span>def</span><span> yaml_serializer</span><span>(</span><span>data</span><span>):</span></span>
<span><span>    return</span><span> yaml.dump(data, </span><span>sort_keys</span><span>=</span><span>False</span><span>)</span></span>
<span></span>
<span><span># Create a server with the custom serializer</span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span><span>name</span><span>=</span><span>"MyServer"</span><span>, </span><span>tool_serializer</span><span>=</span><span>yaml_serializer)</span></span>
<span></span>
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> get_config</span><span>():</span></span>
<span><span>    """Returns configuration in YAML format."""</span></span>
<span><span>    return</span><span> {</span><span>"api_key"</span><span>: </span><span>"abc123"</span><span>, </span><span>"debug"</span><span>: </span><span>True</span><span>, </span><span>"rate_limit"</span><span>: </span><span>100</span><span>}</span></span>
```

The serializer function takes any data object and returns a string representation. This is applied to **all non-string return values** from your tools. Tools that already return strings bypass the serializer. This customization is useful when you want to:

-   Format data in a specific way (like YAML or custom formats)
-   Control specific serialization options (like indentation or sorting)
-   Add metadata or transform data before sending it to clients


Tools are the core building blocks that allow your LLM to interact with external systems, execute code, and access data that isn’t in its training data. In FastMCP, tools are Python functions exposed to LLMs through the MCP protocol.

Tools in FastMCP transform regular Python functions into capabilities that LLMs can invoke during conversations. When an LLM decides to use a tool:

1.  It sends a request with parameters based on the tool’s schema.
2.  FastMCP validates these parameters against your function’s signature.
3.  Your function executes with the validated inputs.
4.  The result is returned to the LLM, which can use it in its response.

This allows LLMs to perform tasks like querying databases, calling APIs, making calculations, or accessing files—extending their capabilities beyond what’s in their training data.

### The `@tool` Decorator

Creating a tool is as simple as decorating a Python function with `@mcp.tool`:

```
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP</span></span>
<span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span><span>name</span><span>=</span><span>"CalculatorServer"</span><span>)</span></span>
<span></span>
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> add</span><span>(</span><span>a</span><span>: </span><span>int</span><span>, </span><span>b</span><span>: </span><span>int</span><span>) -&gt; </span><span>int</span><span>:</span></span>
<span><span>    """Adds two integer numbers together."""</span></span>
<span><span>    return</span><span> a </span><span>+</span><span> b</span></span>
```

When this tool is registered, FastMCP automatically:

-   Uses the function name (`add`) as the tool name.
-   Uses the function’s docstring (`Adds two integer numbers...`) as the tool description.
-   Generates an input schema based on the function’s parameters and type annotations.
-   Handles parameter validation and error reporting.

The way you define your Python function dictates how the tool appears and behaves for the LLM client.

#### Decorator Arguments

While FastMCP infers the name and description from your function, you can override these and add additional metadata using arguments to the `@mcp.tool` decorator:

```
<span><span>@mcp.tool</span><span>(</span></span>
<span><span>    name</span><span>=</span><span>"find_products"</span><span>,           </span><span># Custom tool name for the LLM</span></span>
<span><span>    description</span><span>=</span><span>"Search the product catalog with optional category filtering."</span><span>, </span><span># Custom description</span></span>
<span><span>    tags</span><span>=</span><span>{</span><span>"catalog"</span><span>, </span><span>"search"</span><span>},      </span><span># Optional tags for organization/filtering</span></span>
<span><span>    meta</span><span>=</span><span>{</span><span>"version"</span><span>: </span><span>"1.2"</span><span>, </span><span>"author"</span><span>: </span><span>"product-team"</span><span>}  </span><span># Custom metadata</span></span>
<span><span>)</span></span>
<span><span>def</span><span> search_products_implementation</span><span>(</span><span>query</span><span>: </span><span>str</span><span>, </span><span>category</span><span>: </span><span>str</span><span> |</span><span> None</span><span> =</span><span> None</span><span>) -&gt; list[</span><span>dict</span><span>]:</span></span>
<span><span>    """Internal function description (ignored if description is provided above)."""</span></span>
<span><span>    # Implementation...</span></span>
<span><span>    print</span><span>(</span><span>f</span><span>"Searching for '</span><span>{</span><span>query</span><span>}</span><span>' in category '</span><span>{</span><span>category</span><span>}</span><span>'"</span><span>)</span></span>
<span><span>    return</span><span> [{</span><span>"id"</span><span>: </span><span>2</span><span>, </span><span>"name"</span><span>: </span><span>"Another Product"</span><span>}]</span></span>
```

### Async and Synchronous Tools

FastMCP is an async-first framework that seamlessly supports both asynchronous (`async def`) and synchronous (`def`) functions as tools. Async tools are preferred for I/O-bound operations to keep your server responsive. While synchronous tools work seamlessly in FastMCP, they can block the event loop during execution. For CPU-intensive or potentially blocking synchronous operations, consider alternative strategies. One approach is to use `anyio` (which FastMCP already uses internally) to wrap them as async functions, for example:

```
<span><span>import</span><span> anyio</span></span>
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP</span></span>
<span></span>
<span><span>mcp </span><span>=</span><span> FastMCP()</span></span>
<span></span>
<span><span>def</span><span> cpu_intensive_task</span><span>(</span><span>data</span><span>: </span><span>str</span><span>) -&gt; </span><span>str</span><span>:</span></span>
<span><span>    # Some heavy computation that could block the event loop</span></span>
<span><span>    return</span><span> processed_data</span></span>
<span></span>
<span><span>@mcp.tool</span></span>
<span><span>async</span><span> def</span><span> wrapped_cpu_task</span><span>(</span><span>data</span><span>: </span><span>str</span><span>) -&gt; </span><span>str</span><span>:</span></span>
<span><span>    """CPU-intensive task wrapped to prevent blocking."""</span></span>
<span><span>    return</span><span> await</span><span> anyio.to_thread.run_sync(cpu_intensive_task, data)</span></span>
```

Alternative approaches include using `asyncio.get_event_loop().run_in_executor()` or other threading techniques to manage blocking operations without impacting server responsiveness. For example, here’s a recipe for using the `asyncer` library (not included in FastMCP) to create a decorator that wraps synchronous functions, courtesy of [@hsheth2](https://github.com/jlowin/fastmcp/issues/864#issuecomment-3103678258):

### Type Annotations

Type annotations for parameters are essential for proper tool functionality. They:

1.  Inform the LLM about the expected data types for each parameter
2.  Enable FastMCP to validate input data from clients
3.  Generate accurate JSON schemas for the MCP protocol

Use standard Python type annotations for parameters:

```
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> analyze_text</span><span>(</span></span>
<span><span>    text</span><span>: </span><span>str</span><span>,</span></span>
<span><span>    max_tokens</span><span>: </span><span>int</span><span> =</span><span> 100</span><span>,</span></span>
<span><span>    language</span><span>: </span><span>str</span><span> |</span><span> None</span><span> =</span><span> None</span></span>
<span><span>) -&gt; </span><span>dict</span><span>:</span></span>
<span><span>    """Analyze the provided text."""</span></span>
<span><span>    # Implementation...</span></span>
```

FastMCP supports a wide range of type annotations, including all Pydantic types:

| Type Annotation | Example | Description |
| --- | --- | --- |
| Basic types | `int`, `float`, `str`, `bool` | Simple scalar values - see [Built-in Types](https://gofastmcp.com/getting-started/quickstart#built-in-types) |
| Binary data | `bytes` | Binary content - see [Binary Data](https://gofastmcp.com/getting-started/quickstart#binary-data) |
| Date and Time | `datetime`, `date`, `timedelta` | Date and time objects - see [Date and Time Types](https://gofastmcp.com/getting-started/quickstart#date-and-time-types) |
| Collection types | `list[str]`, `dict[str, int]`, `set[int]` | Collections of items - see [Collection Types](https://gofastmcp.com/getting-started/quickstart#collection-types) |
| Optional types | `float | None`, `Optional[float]` | Parameters that may be null/omitted - see [Union and Optional Types](https://gofastmcp.com/getting-started/quickstart#union-and-optional-types) |
| Union types | `str | int`, `Union[str, int]` | Parameters accepting multiple types - see [Union and Optional Types](https://gofastmcp.com/getting-started/quickstart#union-and-optional-types) |
| Constrained types | `Literal["A", "B"]`, `Enum` | Parameters with specific allowed values - see [Constrained Types](https://gofastmcp.com/getting-started/quickstart#constrained-types) |
| Paths | `Path` | File system paths - see [Paths](https://gofastmcp.com/getting-started/quickstart#paths) |
| UUIDs | `UUID` | Universally unique identifiers - see [UUIDs](https://gofastmcp.com/getting-started/quickstart#uuids) |
| Pydantic models | `UserData` | Complex structured data - see [Pydantic Models](https://gofastmcp.com/getting-started/quickstart#pydantic-models) |

For additional type annotations not listed here, see the [Parameter Types](https://gofastmcp.com/getting-started/quickstart#parameter-types) section below for more detailed information and examples.

### Parameter Metadata

You can provide additional metadata about parameters in several ways:

#### Simple String Descriptions

`` New in version: `2.11.0` `` For basic parameter descriptions, you can use a convenient shorthand with `Annotated`:

```
<span><span>from</span><span> typing </span><span>import</span><span> Annotated</span></span>
<span></span>
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> process_image</span><span>(</span></span>
<span><span>    image_url</span><span>: Annotated[</span><span>str</span><span>, </span><span>"URL of the image to process"</span><span>],</span></span>
<span><span>    resize</span><span>: Annotated[</span><span>bool</span><span>, </span><span>"Whether to resize the image"</span><span>] </span><span>=</span><span> False</span><span>,</span></span>
<span><span>    width</span><span>: Annotated[</span><span>int</span><span>, </span><span>"Target width in pixels"</span><span>] </span><span>=</span><span> 800</span><span>,</span></span>
<span><span>    format</span><span>: Annotated[</span><span>str</span><span>, </span><span>"Output image format"</span><span>] </span><span>=</span><span> "jpeg"</span></span>
<span><span>) -&gt; </span><span>dict</span><span>:</span></span>
<span><span>    """Process an image with optional resizing."""</span></span>
<span><span>    # Implementation...</span></span>
```

This shorthand syntax is equivalent to using `Field(description=...)` but more concise for simple descriptions.

#### Advanced Metadata with Field

For validation constraints and advanced metadata, use Pydantic’s `Field` class with `Annotated`:

```
<span><span>from</span><span> typing </span><span>import</span><span> Annotated</span></span>
<span><span>from</span><span> pydantic </span><span>import</span><span> Field</span></span>
<span></span>
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> process_image</span><span>(</span></span>
<span><span>    image_url</span><span>: Annotated[</span><span>str</span><span>, Field(</span><span>description</span><span>=</span><span>"URL of the image to process"</span><span>)],</span></span>
<span><span>    resize</span><span>: Annotated[</span><span>bool</span><span>, Field(</span><span>description</span><span>=</span><span>"Whether to resize the image"</span><span>)] </span><span>=</span><span> False</span><span>,</span></span>
<span><span>    width</span><span>: Annotated[</span><span>int</span><span>, Field(</span><span>description</span><span>=</span><span>"Target width in pixels"</span><span>, </span><span>ge</span><span>=</span><span>1</span><span>, </span><span>le</span><span>=</span><span>2000</span><span>)] </span><span>=</span><span> 800</span><span>,</span></span>
<span><span>    format</span><span>: Annotated[</span></span>
<span><span>        Literal[</span><span>"jpeg"</span><span>, </span><span>"png"</span><span>, </span><span>"webp"</span><span>], </span></span>
<span><span>        Field(</span><span>description</span><span>=</span><span>"Output image format"</span><span>)</span></span>
<span><span>    ] </span><span>=</span><span> "jpeg"</span></span>
<span><span>) -&gt; </span><span>dict</span><span>:</span></span>
<span><span>    """Process an image with optional resizing."""</span></span>
<span><span>    # Implementation...</span></span>
```

You can also use the Field as a default value, though the Annotated approach is preferred:

```
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> search_database</span><span>(</span></span>
<span><span>    query</span><span>: </span><span>str</span><span> =</span><span> Field(</span><span>description</span><span>=</span><span>"Search query string"</span><span>),</span></span>
<span><span>    limit</span><span>: </span><span>int</span><span> =</span><span> Field(</span><span>10</span><span>, </span><span>description</span><span>=</span><span>"Maximum number of results"</span><span>, </span><span>ge</span><span>=</span><span>1</span><span>, </span><span>le</span><span>=</span><span>100</span><span>)</span></span>
<span><span>) -&gt; </span><span>list</span><span>:</span></span>
<span><span>    """Search the database with the provided query."""</span></span>
<span><span>    # Implementation...</span></span>
```

Field provides several validation and documentation features:

-   `description`: Human-readable explanation of the parameter (shown to LLMs)
-   `ge`/`gt`/`le`/`lt`: Greater/less than (or equal) constraints
-   `min_length`/`max_length`: String or collection length constraints
-   `pattern`: Regex pattern for string validation
-   `default`: Default value if parameter is omitted

### Optional Arguments

FastMCP follows Python’s standard function parameter conventions. Parameters without default values are required, while those with default values are optional.

```
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> search_products</span><span>(</span></span>
<span><span>    query</span><span>: </span><span>str</span><span>,                   </span><span># Required - no default value</span></span>
<span><span>    max_results</span><span>: </span><span>int</span><span> =</span><span> 10</span><span>,        </span><span># Optional - has default value</span></span>
<span><span>    sort_by</span><span>: </span><span>str</span><span> =</span><span> "relevance"</span><span>,   </span><span># Optional - has default value</span></span>
<span><span>    category</span><span>: </span><span>str</span><span> |</span><span> None</span><span> =</span><span> None</span><span>   # Optional - can be None</span></span>
<span><span>) -&gt; list[</span><span>dict</span><span>]:</span></span>
<span><span>    """Search the product catalog."""</span></span>
<span><span>    # Implementation...</span></span>
```

In this example, the LLM must provide a `query` parameter, while `max_results`, `sort_by`, and `category` will use their default values if not explicitly provided.

### Excluding Arguments

`` New in version: `2.6.0` `` You can exclude certain arguments from the tool schema shown to the LLM. This is useful for arguments that are injected at runtime (such as `state`, `user_id`, or credentials) and should not be exposed to the LLM or client. Only arguments with default values can be excluded; attempting to exclude a required argument will raise an error. Example:

```
<span><span>@mcp.tool</span><span>(</span></span>
<span><span>    name</span><span>=</span><span>"get_user_details"</span><span>,</span></span>
<span><span>    exclude_args</span><span>=</span><span>[</span><span>"user_id"</span><span>]</span></span>
<span><span>)</span></span>
<span><span>def</span><span> get_user_details</span><span>(</span><span>user_id</span><span>: </span><span>str</span><span> =</span><span> None</span><span>) -&gt; </span><span>str</span><span>:</span></span>
<span><span>    # user_id will be injected by the server, not provided by the LLM</span></span>
<span><span>    ...</span></span>
```

With this configuration, `user_id` will not appear in the tool’s parameter schema, but can still be set by the server or framework at runtime. For more complex tool transformations, see [Transforming Tools](https://gofastmcp.com/patterns/tool-transformation).

### Disabling Tools

`` New in version: `2.8.0` `` You can control the visibility and availability of tools by enabling or disabling them. This is useful for feature flagging, maintenance, or dynamically changing the toolset available to a client. Disabled tools will not appear in the list of available tools returned by `list_tools`, and attempting to call a disabled tool will result in an “Unknown tool” error, just as if the tool did not exist. By default, all tools are enabled. You can disable a tool upon creation using the `enabled` parameter in the decorator:

```
<span><span>@mcp.tool</span><span>(</span><span>enabled</span><span>=</span><span>False</span><span>)</span></span>
<span><span>def</span><span> maintenance_tool</span><span>():</span></span>
<span><span>    """This tool is currently under maintenance."""</span></span>
<span><span>    return</span><span> "This tool is disabled."</span></span>
```

You can also toggle a tool’s state programmatically after it has been created:

```
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> dynamic_tool</span><span>():</span></span>
<span><span>    return</span><span> "I am a dynamic tool."</span></span>
<span></span>
<span><span># Disable and re-enable the tool</span></span>
<span><span>dynamic_tool.disable()</span></span>
<span><span>dynamic_tool.enable()</span></span>
```

### Return Values

FastMCP tools can return data in two complementary formats: **traditional content blocks** (like text and images) and **structured outputs** (machine-readable JSON). When you add return type annotations, FastMCP automatically generates **output schemas** to validate the structured data and enables clients to deserialize results back to Python objects. Understanding how these three concepts work together:

-   **Return Values**: What your Python function returns (determines both content blocks and structured data)
-   **Structured Outputs**: JSON data sent alongside traditional content for machine processing
-   **Output Schemas**: JSON Schema declarations that describe and validate the structured output format

The following sections explain each concept in detail.

#### Content Blocks

FastMCP automatically converts tool return values into appropriate MCP content blocks:

-   **`str`**: Sent as `TextContent`
-   **`bytes`**: Base64 encoded and sent as `BlobResourceContents` (within an `EmbeddedResource`)
-   **`fastmcp.utilities.types.Image`**: Sent as `ImageContent`
-   **`fastmcp.utilities.types.Audio`**: Sent as `AudioContent`
-   **`fastmcp.utilities.types.File`**: Sent as base64-encoded `EmbeddedResource`
-   **A list of any of the above**: Converts each item appropriately
-   **`None`**: Results in an empty response

#### Structured Output

`` New in version: `2.10.0` `` The 6/18/2025 MCP spec update [introduced](https://modelcontextprotocol.io/specification/2025-06-18/server/tools#structured-content) structured content, which is a new way to return data from tools. Structured content is a JSON object that is sent alongside traditional content. FastMCP automatically creates structured outputs alongside traditional content when your tool returns data that has a JSON object representation. This provides machine-readable JSON data that clients can deserialize back to Python objects. **Automatic Structured Content Rules:**

-   **Object-like results** (`dict`, Pydantic models, dataclasses) → Always become structured content (even without output schema)
-   **Non-object results** (`int`, `str`, `list`) → Only become structured content if there’s an output schema to validate/serialize them
-   **All results** → Always become traditional content blocks for backward compatibility

##### Object-like Results (Automatic Structured Content)

##### Non-object Results (Schema Required)

##### Complex Type Example

#### Output Schemas

`` New in version: `2.10.0` `` The 6/18/2025 MCP spec update [introduced](https://modelcontextprotocol.io/specification/2025-06-18/server/tools#output-schema) output schemas, which are a new way to describe the expected output format of a tool. When an output schema is provided, the tool _must_ return structured output that matches the schema. When you add return type annotations to your functions, FastMCP automatically generates JSON schemas that describe the expected output format. These schemas help MCP clients understand and validate the structured data they receive.

##### Primitive Type Wrapping

For primitive return types (like `int`, `str`, `bool`), FastMCP automatically wraps the result under a `"result"` key to create valid structured output:

##### Manual Schema Control

You can override the automatically generated schema by providing a custom `output_schema`:

```
<span><span>@mcp.tool</span><span>(</span><span>output_schema</span><span>=</span><span>{</span></span>
<span><span>    "type"</span><span>: </span><span>"object"</span><span>, </span></span>
<span><span>    "properties"</span><span>: {</span></span>
<span><span>        "data"</span><span>: {</span><span>"type"</span><span>: </span><span>"string"</span><span>},</span></span>
<span><span>        "metadata"</span><span>: {</span><span>"type"</span><span>: </span><span>"object"</span><span>}</span></span>
<span><span>    }</span></span>
<span><span>})</span></span>
<span><span>def</span><span> custom_schema_tool</span><span>() -&gt; </span><span>dict</span><span>:</span></span>
<span><span>    """Tool with custom output schema."""</span></span>
<span><span>    return</span><span> {</span><span>"data"</span><span>: </span><span>"Hello"</span><span>, </span><span>"metadata"</span><span>: {</span><span>"version"</span><span>: </span><span>"1.0"</span><span>}}</span></span>
```

Schema generation works for most common types including basic types, collections, union types, Pydantic models, TypedDict structures, and dataclasses.

#### Full Control with ToolResult

For complete control over both traditional content and structured output, return a `ToolResult` object:

```
<span><span>from</span><span> fastmcp.tools.tool </span><span>import</span><span> ToolResult</span></span>
<span></span>
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> advanced_tool</span><span>() -&gt; ToolResult:</span></span>
<span><span>    """Tool with full control over output."""</span></span>
<span><span>    return</span><span> ToolResult(</span></span>
<span><span>        content</span><span>=</span><span>[TextContent(</span><span>text</span><span>=</span><span>"Human-readable summary"</span><span>)],</span></span>
<span><span>        structured_content</span><span>=</span><span>{</span><span>"data"</span><span>: </span><span>"value"</span><span>, </span><span>"count"</span><span>: </span><span>42</span><span>}</span></span>
<span><span>    )</span></span>
```

When returning `ToolResult`:

-   You control exactly what content and structured data is sent
-   Output schemas are optional - structured content can be provided without a schema
-   Clients receive both traditional content blocks and structured data

### Error Handling

`` New in version: `2.4.1` `` If your tool encounters an error, you can raise a standard Python exception (`ValueError`, `TypeError`, `FileNotFoundError`, custom exceptions, etc.) or a FastMCP `ToolError`. By default, all exceptions (including their details) are logged and converted into an MCP error response to be sent back to the client LLM. This helps the LLM understand failures and react appropriately. If you want to mask internal error details for security reasons, you can:

1.  Use the `mask_error_details=True` parameter when creating your `FastMCP` instance:

```
<span><span>mcp </span><span>=</span><span> FastMCP(</span><span>name</span><span>=</span><span>"SecureServer"</span><span>, </span><span>mask_error_details</span><span>=</span><span>True</span><span>)</span></span>
```

2.  Or use `ToolError` to explicitly control what error information is sent to clients:

```
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP</span></span>
<span><span>from</span><span> fastmcp.exceptions </span><span>import</span><span> ToolError</span></span>
<span></span>
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> divide</span><span>(</span><span>a</span><span>: </span><span>float</span><span>, </span><span>b</span><span>: </span><span>float</span><span>) -&gt; </span><span>float</span><span>:</span></span>
<span><span>    """Divide a by b."""</span></span>
<span></span>
<span><span>    if</span><span> b </span><span>==</span><span> 0</span><span>:</span></span>
<span><span>        # Error messages from ToolError are always sent to clients,</span></span>
<span><span>        # regardless of mask_error_details setting</span></span>
<span><span>        raise</span><span> ToolError(</span><span>"Division by zero is not allowed."</span><span>)</span></span>
<span><span>    </span></span>
<span><span>    # If mask_error_details=True, this message would be masked</span></span>
<span><span>    if</span><span> not</span><span> isinstance</span><span>(a, (</span><span>int</span><span>, </span><span>float</span><span>)) </span><span>or</span><span> not</span><span> isinstance</span><span>(b, (</span><span>int</span><span>, </span><span>float</span><span>)):</span></span>
<span><span>        raise</span><span> TypeError</span><span>(</span><span>"Both arguments must be numbers."</span><span>)</span></span>
<span><span>        </span></span>
<span><span>    return</span><span> a </span><span>/</span><span> b</span></span>
```

When `mask_error_details=True`, only error messages from `ToolError` will include details, other exceptions will be converted to a generic message.

### Annotations

`` New in version: `2.2.7` `` FastMCP allows you to add specialized metadata to your tools through annotations. These annotations communicate how tools behave to client applications without consuming token context in LLM prompts. Annotations serve several purposes in client applications:

-   Adding user-friendly titles for display purposes
-   Indicating whether tools modify data or systems
-   Describing the safety profile of tools (destructive vs. non-destructive)
-   Signaling if tools interact with external systems

You can add annotations to a tool using the `annotations` parameter in the `@mcp.tool` decorator:

```
<span><span>@mcp.tool</span><span>(</span></span>
<span><span>    annotations</span><span>=</span><span>{</span></span>
<span><span>        "title"</span><span>: </span><span>"Calculate Sum"</span><span>,</span></span>
<span><span>        "readOnlyHint"</span><span>: </span><span>True</span><span>,</span></span>
<span><span>        "openWorldHint"</span><span>: </span><span>False</span></span>
<span><span>    }</span></span>
<span><span>)</span></span>
<span><span>def</span><span> calculate_sum</span><span>(</span><span>a</span><span>: </span><span>float</span><span>, </span><span>b</span><span>: </span><span>float</span><span>) -&gt; </span><span>float</span><span>:</span></span>
<span><span>    """Add two numbers together."""</span></span>
<span><span>    return</span><span> a </span><span>+</span><span> b</span></span>
```

FastMCP supports these standard annotations:

| Annotation | Type | Default | Purpose |
| --- | --- | --- | --- |
| `title` | string | \- | Display name for user interfaces |
| `readOnlyHint` | boolean | false | Indicates if the tool only reads without making changes |
| `destructiveHint` | boolean | true | For non-readonly tools, signals if changes are destructive |
| `idempotentHint` | boolean | false | Indicates if repeated identical calls have the same effect as a single call |
| `openWorldHint` | boolean | true | Specifies if the tool interacts with external systems |

Remember that annotations help make better user experiences but should be treated as advisory hints. They help client applications present appropriate UI elements and safety controls, but won’t enforce security boundaries on their own. Always focus on making your annotations accurately represent what your tool actually does.

### Notifications

`` New in version: `2.9.1` `` FastMCP automatically sends `notifications/tools/list_changed` notifications to connected clients when tools are added, removed, enabled, or disabled. This allows clients to stay up-to-date with the current tool set without manually polling for changes.

```
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> example_tool</span><span>() -&gt; </span><span>str</span><span>:</span></span>
<span><span>    return</span><span> "Hello!"</span></span>
<span></span>
<span><span># These operations trigger notifications:</span></span>
<span><span>mcp.add_tool(example_tool)     </span><span># Sends tools/list_changed notification</span></span>
<span><span>example_tool.disable()         </span><span># Sends tools/list_changed notification  </span></span>
<span><span>example_tool.enable()          </span><span># Sends tools/list_changed notification</span></span>
<span><span>mcp.remove_tool(</span><span>"example_tool"</span><span>) </span><span># Sends tools/list_changed notification</span></span>
```

Notifications are only sent when these operations occur within an active MCP request context (e.g., when called from within a tool or other MCP operation). Operations performed during server initialization do not trigger notifications. Clients can handle these notifications using a [message handler](https://gofastmcp.com/clients/messages) to automatically refresh their tool lists or update their interfaces.

## MCP Context

Tools can access MCP features like logging, reading resources, or reporting progress through the `Context` object. To use it, add a parameter to your tool function with the type hint `Context`.

```
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP, Context</span></span>
<span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span><span>name</span><span>=</span><span>"ContextDemo"</span><span>)</span></span>
<span></span>
<span><span>@mcp.tool</span></span>
<span><span>async</span><span> def</span><span> process_data</span><span>(</span><span>data_uri</span><span>: </span><span>str</span><span>, </span><span>ctx</span><span>: Context) -&gt; </span><span>dict</span><span>:</span></span>
<span><span>    """Process data from a resource with progress reporting."""</span></span>
<span><span>    await</span><span> ctx.info(</span><span>f</span><span>"Processing data from </span><span>{</span><span>data_uri</span><span>}</span><span>"</span><span>)</span></span>
<span><span>    </span></span>
<span><span>    # Read a resource</span></span>
<span><span>    resource </span><span>=</span><span> await</span><span> ctx.read_resource(data_uri)</span></span>
<span><span>    data </span><span>=</span><span> resource[</span><span>0</span><span>].content </span><span>if</span><span> resource </span><span>else</span><span> ""</span></span>
<span><span>    </span></span>
<span><span>    # Report progress</span></span>
<span><span>    await</span><span> ctx.report_progress(</span><span>progress</span><span>=</span><span>50</span><span>, </span><span>total</span><span>=</span><span>100</span><span>)</span></span>
<span><span>    </span></span>
<span><span>    # Example request to the client's LLM for help</span></span>
<span><span>    summary </span><span>=</span><span> await</span><span> ctx.sample(</span><span>f</span><span>"Summarize this in 10 words: </span><span>{</span><span>data[:</span><span>200</span><span>]</span><span>}</span><span>"</span><span>)</span></span>
<span><span>    </span></span>
<span><span>    await</span><span> ctx.report_progress(</span><span>progress</span><span>=</span><span>100</span><span>, </span><span>total</span><span>=</span><span>100</span><span>)</span></span>
<span><span>    return</span><span> {</span></span>
<span><span>        "length"</span><span>: </span><span>len</span><span>(data),</span></span>
<span><span>        "summary"</span><span>: summary.text</span></span>
<span><span>    }</span></span>
```

The Context object provides access to:

-   **Logging**: `ctx.debug()`, `ctx.info()`, `ctx.warning()`, `ctx.error()`
-   **Progress Reporting**: `ctx.report_progress(progress, total)`
-   **Resource Access**: `ctx.read_resource(uri)`
-   **LLM Sampling**: `ctx.sample(...)`
-   **Request Information**: `ctx.request_id`, `ctx.client_id`

For full documentation on the Context object and all its capabilities, see the [Context documentation](https://gofastmcp.com/servers/context).

## Parameter Types

FastMCP supports a wide variety of parameter types to give you flexibility when designing your tools. FastMCP generally supports all types that Pydantic supports as fields, including all Pydantic custom types. This means you can use any type that can be validated and parsed by Pydantic in your tool parameters. FastMCP supports **type coercion** when possible. This means that if a client sends data that doesn’t match the expected type, FastMCP will attempt to convert it to the appropriate type. For example, if a client sends a string for a parameter annotated as `int`, FastMCP will attempt to convert it to an integer. If the conversion is not possible, FastMCP will return a validation error.

### Built-in Types

The most common parameter types are Python’s built-in scalar types:

```
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> process_values</span><span>(</span></span>
<span><span>    name</span><span>: </span><span>str</span><span>,             </span><span># Text data</span></span>
<span><span>    count</span><span>: </span><span>int</span><span>,            </span><span># Integer numbers</span></span>
<span><span>    amount</span><span>: </span><span>float</span><span>,         </span><span># Floating point numbers</span></span>
<span><span>    enabled</span><span>: </span><span>bool</span><span>          # Boolean values (True/False)</span></span>
<span><span>):</span></span>
<span><span>    """Process various value types."""</span></span>
<span><span>    # Implementation...</span></span>
```

These types provide clear expectations to the LLM about what values are acceptable and allow FastMCP to validate inputs properly. Even if a client provides a string like “42”, it will be coerced to an integer for parameters annotated as `int`.

### Date and Time Types

FastMCP supports various date and time types from the `datetime` module:

```
<span><span>from</span><span> datetime </span><span>import</span><span> datetime, date, timedelta</span></span>
<span></span>
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> process_date_time</span><span>(</span></span>
<span><span>    event_date</span><span>: date,             </span><span># ISO format date string or date object</span></span>
<span><span>    event_time</span><span>: datetime,         </span><span># ISO format datetime string or datetime object</span></span>
<span><span>    duration</span><span>: timedelta </span><span>=</span><span> timedelta(</span><span>hours</span><span>=</span><span>1</span><span>)  </span><span># Integer seconds or timedelta</span></span>
<span><span>) -&gt; </span><span>str</span><span>:</span></span>
<span><span>    """Process date and time information."""</span></span>
<span><span>    # Types are automatically converted from strings</span></span>
<span><span>    assert</span><span> isinstance</span><span>(event_date, date)  </span></span>
<span><span>    assert</span><span> isinstance</span><span>(event_time, datetime)</span></span>
<span><span>    assert</span><span> isinstance</span><span>(duration, timedelta)</span></span>
<span><span>    </span></span>
<span><span>    return</span><span> f</span><span>"Event on </span><span>{</span><span>event_date</span><span>}</span><span> at </span><span>{</span><span>event_time</span><span>}</span><span> for </span><span>{</span><span>duration</span><span>}</span><span>"</span></span>
```

-   `datetime` - Accepts ISO format strings (e.g., “2023-04-15T14:30:00”)
-   `date` - Accepts ISO format date strings (e.g., “2023-04-15”)
-   `timedelta` - Accepts integer seconds or timedelta objects

### Collection Types

FastMCP supports all standard Python collection types:

```
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> analyze_data</span><span>(</span></span>
<span><span>    values</span><span>: list[</span><span>float</span><span>],           </span><span># List of numbers</span></span>
<span><span>    properties</span><span>: dict[</span><span>str</span><span>, </span><span>str</span><span>],    </span><span># Dictionary with string keys and values</span></span>
<span><span>    unique_ids</span><span>: set[</span><span>int</span><span>],          </span><span># Set of unique integers</span></span>
<span><span>    coordinates</span><span>: tuple[</span><span>float</span><span>, </span><span>float</span><span>],  </span><span># Tuple with fixed structure</span></span>
<span><span>    mixed_data</span><span>: dict[</span><span>str</span><span>, list[</span><span>int</span><span>]] </span><span># Nested collections</span></span>
<span><span>):</span></span>
<span><span>    """Analyze collections of data."""</span></span>
<span><span>    # Implementation...</span></span>
```

All collection types can be used as parameter annotations:

-   `list[T]` - Ordered sequence of items
-   `dict[K, V]` - Key-value mapping
-   `set[T]` - Unordered collection of unique items
-   `tuple[T1, T2, ...]` - Fixed-length sequence with potentially different types

Collection types can be nested and combined to represent complex data structures. JSON strings that match the expected structure will be automatically parsed and converted to the appropriate Python collection type.

### Union and Optional Types

For parameters that can accept multiple types or may be omitted:

```
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> flexible_search</span><span>(</span></span>
<span><span>    query</span><span>: </span><span>str</span><span> |</span><span> int</span><span>,              </span><span># Can be either string or integer</span></span>
<span><span>    filters</span><span>: dict[</span><span>str</span><span>, </span><span>str</span><span>] </span><span>|</span><span> None</span><span> =</span><span> None</span><span>,  </span><span># Optional dictionary</span></span>
<span><span>    sort_field</span><span>: </span><span>str</span><span> |</span><span> None</span><span> =</span><span> None</span><span>  # Optional string</span></span>
<span><span>):</span></span>
<span><span>    """Search with flexible parameter types."""</span></span>
<span><span>    # Implementation...</span></span>
```

Modern Python syntax (`str | int`) is preferred over older `Union[str, int]` forms. Similarly, `str | None` is preferred over `Optional[str]`.

### Constrained Types

When a parameter must be one of a predefined set of values, you can use either Literal types or Enums:

#### Literals

Literals constrain parameters to a specific set of values:

```
<span><span>from</span><span> typing </span><span>import</span><span> Literal</span></span>
<span></span>
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> sort_data</span><span>(</span></span>
<span><span>    data</span><span>: list[</span><span>float</span><span>],</span></span>
<span><span>    order</span><span>: Literal[</span><span>"ascending"</span><span>, </span><span>"descending"</span><span>] </span><span>=</span><span> "ascending"</span><span>,</span></span>
<span><span>    algorithm</span><span>: Literal[</span><span>"quicksort"</span><span>, </span><span>"mergesort"</span><span>, </span><span>"heapsort"</span><span>] </span><span>=</span><span> "quicksort"</span></span>
<span><span>):</span></span>
<span><span>    """Sort data using specific options."""</span></span>
<span><span>    # Implementation...</span></span>
```

Literal types:

-   Specify exact allowable values directly in the type annotation
-   Help LLMs understand exactly which values are acceptable
-   Provide input validation (errors for invalid values)
-   Create clear schemas for clients

#### Enums

For more structured sets of constrained values, use Python’s Enum class:

```
<span><span>from</span><span> enum </span><span>import</span><span> Enum</span></span>
<span></span>
<span><span>class</span><span> Color</span><span>(</span><span>Enum</span><span>):</span></span>
<span><span>    RED</span><span> =</span><span> "red"</span></span>
<span><span>    GREEN</span><span> =</span><span> "green"</span></span>
<span><span>    BLUE</span><span> =</span><span> "blue"</span></span>
<span></span>
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> process_image</span><span>(</span></span>
<span><span>    image_path</span><span>: </span><span>str</span><span>, </span></span>
<span><span>    color_filter</span><span>: Color </span><span>=</span><span> Color.</span><span>RED</span></span>
<span><span>):</span></span>
<span><span>    """Process an image with a color filter."""</span></span>
<span><span>    # Implementation...</span></span>
<span><span>    # color_filter will be a Color enum member</span></span>
```

When using Enum types:

-   Clients should provide the enum’s value (e.g., “red”), not the enum member name (e.g., “RED”)
-   FastMCP automatically coerces the string value into the appropriate Enum object
-   Your function receives the actual Enum member (e.g., `Color.RED`)
-   Validation errors are raised for values not in the enum

### Binary Data

There are two approaches to handling binary data in tool parameters:

#### Bytes

```
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> process_binary</span><span>(</span><span>data</span><span>: </span><span>bytes</span><span>):</span></span>
<span><span>    """Process binary data directly.</span></span>
<span><span>    </span></span>
<span><span>    The client can send a binary string, which will be </span></span>
<span><span>    converted directly to bytes.</span></span>
<span><span>    """</span></span>
<span><span>    # Implementation using binary data</span></span>
<span><span>    data_length </span><span>=</span><span> len</span><span>(data)</span></span>
<span><span>    # ...</span></span>
```

When you annotate a parameter as `bytes`, FastMCP will:

-   Convert raw strings directly to bytes
-   Validate that the input can be properly represented as bytes

FastMCP does not automatically decode base64-encoded strings for bytes parameters. If you need to accept base64-encoded data, you should handle the decoding manually as shown below.

#### Base64-encoded strings

```
<span><span>from</span><span> typing </span><span>import</span><span> Annotated</span></span>
<span><span>from</span><span> pydantic </span><span>import</span><span> Field</span></span>
<span></span>
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> process_image_data</span><span>(</span></span>
<span><span>    image_data</span><span>: Annotated[</span><span>str</span><span>, Field(</span><span>description</span><span>=</span><span>"Base64-encoded image data"</span><span>)]</span></span>
<span><span>):</span></span>
<span><span>    """Process an image from base64-encoded string.</span></span>
<span><span>    </span></span>
<span><span>    The client is expected to provide base64-encoded data as a string.</span></span>
<span><span>    You'll need to decode it manually.</span></span>
<span><span>    """</span></span>
<span><span>    # Manual base64 decoding</span></span>
<span><span>    import</span><span> base64</span></span>
<span><span>    binary_data </span><span>=</span><span> base64.b64decode(image_data)</span></span>
<span><span>    # Process binary_data...</span></span>
```

This approach is recommended when you expect to receive base64-encoded binary data from clients.

### Paths

The `Path` type from the `pathlib` module can be used for file system paths:

```
<span><span>from</span><span> pathlib </span><span>import</span><span> Path</span></span>
<span></span>
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> process_file</span><span>(</span><span>path</span><span>: Path) -&gt; </span><span>str</span><span>:</span></span>
<span><span>    """Process a file at the given path."""</span></span>
<span><span>    assert</span><span> isinstance</span><span>(path, Path)  </span><span># Path is properly converted</span></span>
<span><span>    return</span><span> f</span><span>"Processing file at </span><span>{</span><span>path</span><span>}</span><span>"</span></span>
```

When a client sends a string path, FastMCP automatically converts it to a `Path` object.

### UUIDs

The `UUID` type from the `uuid` module can be used for unique identifiers:

```
<span><span>import</span><span> uuid</span></span>
<span></span>
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> process_item</span><span>(</span></span>
<span><span>    item_id</span><span>: uuid.</span><span>UUID</span><span>  # String UUID or UUID object</span></span>
<span><span>) -&gt; </span><span>str</span><span>:</span></span>
<span><span>    """Process an item with the given UUID."""</span></span>
<span><span>    assert</span><span> isinstance</span><span>(item_id, uuid.</span><span>UUID</span><span>)  </span><span># Properly converted to UUID</span></span>
<span><span>    return</span><span> f</span><span>"Processing item </span><span>{</span><span>item_id</span><span>}</span><span>"</span></span>
```

When a client sends a string UUID (e.g., “123e4567-e89b-12d3-a456-426614174000”), FastMCP automatically converts it to a `UUID` object.

### Pydantic Models

For complex, structured data with nested fields and validation, use Pydantic models:

```
<span><span>from</span><span> pydantic </span><span>import</span><span> BaseModel, Field</span></span>
<span><span>from</span><span> typing </span><span>import</span><span> Optional</span></span>
<span></span>
<span><span>class</span><span> User</span><span>(</span><span>BaseModel</span><span>):</span></span>
<span><span>    username: </span><span>str</span></span>
<span><span>    email: </span><span>str</span><span> =</span><span> Field(</span><span>description</span><span>=</span><span>"User's email address"</span><span>)</span></span>
<span><span>    age: </span><span>int</span><span> |</span><span> None</span><span> =</span><span> None</span></span>
<span><span>    is_active: </span><span>bool</span><span> =</span><span> True</span></span>
<span></span>
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> create_user</span><span>(</span><span>user</span><span>: User):</span></span>
<span><span>    """Create a new user in the system."""</span></span>
<span><span>    # The input is automatically validated against the User model</span></span>
<span><span>    # Even if provided as a JSON string or dict</span></span>
<span><span>    # Implementation...</span></span>
```

Using Pydantic models provides:

-   Clear, self-documenting structure for complex inputs
-   Built-in data validation
-   Automatic generation of detailed JSON schemas for the LLM
-   Automatic conversion from dict/JSON input

Clients can provide data for Pydantic model parameters as either:

-   A JSON object (string)
-   A dictionary with the appropriate structure
-   Nested parameters in the appropriate format

### Pydantic Fields

FastMCP supports robust parameter validation through Pydantic’s `Field` class. This is especially useful to ensure that input values meet specific requirements beyond just their type. Note that fields can be used _outside_ Pydantic models to provide metadata and validation constraints. The preferred approach is using `Annotated` with `Field`:

```
<span><span>from</span><span> typing </span><span>import</span><span> Annotated</span></span>
<span><span>from</span><span> pydantic </span><span>import</span><span> Field</span></span>
<span></span>
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> analyze_metrics</span><span>(</span></span>
<span><span>    # Numbers with range constraints</span></span>
<span><span>    count</span><span>: Annotated[</span><span>int</span><span>, Field(</span><span>ge</span><span>=</span><span>0</span><span>, </span><span>le</span><span>=</span><span>100</span><span>)],         </span><span># 0 &lt;= count &lt;= 100</span></span>
<span><span>    ratio</span><span>: Annotated[</span><span>float</span><span>, Field(</span><span>gt</span><span>=</span><span>0</span><span>, </span><span>lt</span><span>=</span><span>1.0</span><span>)],       </span><span># 0 &lt; ratio &lt; 1.0</span></span>
<span><span>    </span></span>
<span><span>    # String with pattern and length constraints</span></span>
<span><span>    user_id</span><span>: Annotated[</span><span>str</span><span>, Field(</span></span>
<span><span>        pattern</span><span>=</span><span>r</span><span>"</span><span>^</span><span>[</span><span>A-Z</span><span>]</span><span>{2}</span><span>\d</span><span>{4}</span><span>$</span><span>"</span><span>,                     </span><span># Must match regex pattern</span></span>
<span><span>        description</span><span>=</span><span>"User ID in format XX0000"</span></span>
<span><span>    )],</span></span>
<span><span>    </span></span>
<span><span>    # String with length constraints</span></span>
<span><span>    comment</span><span>: Annotated[</span><span>str</span><span>, Field(</span><span>min_length</span><span>=</span><span>3</span><span>, </span><span>max_length</span><span>=</span><span>500</span><span>)] </span><span>=</span><span> ""</span><span>,</span></span>
<span><span>    </span></span>
<span><span>    # Numeric constraints</span></span>
<span><span>    factor</span><span>: Annotated[</span><span>int</span><span>, Field(</span><span>multiple_of</span><span>=</span><span>5</span><span>)] </span><span>=</span><span> 10</span><span>,  </span><span># Must be multiple of 5</span></span>
<span><span>):</span></span>
<span><span>    """Analyze metrics with validated parameters."""</span></span>
<span><span>    # Implementation...</span></span>
```

You can also use `Field` as a default value, though the `Annotated` approach is preferred:

```
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> validate_data</span><span>(</span></span>
<span><span>    # Value constraints</span></span>
<span><span>    age</span><span>: </span><span>int</span><span> =</span><span> Field(</span><span>ge</span><span>=</span><span>0</span><span>, </span><span>lt</span><span>=</span><span>120</span><span>),                     </span><span># 0 &lt;= age &lt; 120</span></span>
<span><span>    </span></span>
<span><span>    # String constraints</span></span>
<span><span>    email</span><span>: </span><span>str</span><span> =</span><span> Field(</span><span>pattern</span><span>=</span><span>r</span><span>"</span><span>^</span><span>[</span><span>\w</span><span>\.</span><span>-</span><span>]</span><span>+</span><span>@</span><span>[</span><span>\w</span><span>\.</span><span>-</span><span>]</span><span>+</span><span>\.</span><span>\w</span><span>+</span><span>$</span><span>"</span><span>),  </span><span># Email pattern</span></span>
<span><span>    </span></span>
<span><span>    # Collection constraints</span></span>
<span><span>    tags</span><span>: list[</span><span>str</span><span>] </span><span>=</span><span> Field(</span><span>min_length</span><span>=</span><span>1</span><span>, </span><span>max_length</span><span>=</span><span>10</span><span>)  </span><span># 1-10 tags</span></span>
<span><span>):</span></span>
<span><span>    """Process data with field validations."""</span></span>
<span><span>    # Implementation...</span></span>
```

Common validation options include:

| Validation | Type | Description |
| --- | --- | --- |
| `ge`, `gt` | Number | Greater than (or equal) constraint |
| `le`, `lt` | Number | Less than (or equal) constraint |
| `multiple_of` | Number | Value must be a multiple of this number |
| `min_length`, `max_length` | String, List, etc. | Length constraints |
| `pattern` | String | Regular expression pattern constraint |
| `description` | Any | Human-readable description (appears in schema) |

When a client sends invalid data, FastMCP will return a validation error explaining why the parameter failed validation.

## Server Behavior

### Duplicate Tools

`` New in version: `2.1.0` `` You can control how the FastMCP server behaves if you try to register multiple tools with the same name. This is configured using the `on_duplicate_tools` argument when creating the `FastMCP` instance.

```
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP</span></span>
<span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span></span>
<span><span>    name</span><span>=</span><span>"StrictServer"</span><span>,</span></span>
<span><span>    # Configure behavior for duplicate tool names</span></span>
<span><span>    on_duplicate_tools</span><span>=</span><span>"error"</span></span>
<span><span>)</span></span>
<span></span>
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> my_tool</span><span>(): </span><span>return</span><span> "Version 1"</span></span>
<span></span>
<span><span># This will now raise a ValueError because 'my_tool' already exists</span></span>
<span><span># and on_duplicate_tools is set to "error".</span></span>
<span><span># @mcp.tool</span></span>
<span><span># def my_tool(): return "Version 2"</span></span>
```

The duplicate behavior options are:

-   `"warn"` (default): Logs a warning and the new tool replaces the old one.
-   `"error"`: Raises a `ValueError`, preventing the duplicate registration.
-   `"replace"`: Silently replaces the existing tool with the new one.
-   `"ignore"`: Keeps the original tool and ignores the new registration attempt.

### Removing Tools

`` New in version: `2.3.4` `` You can dynamically remove tools from a server using the `remove_tool` method:

```
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP</span></span>
<span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span><span>name</span><span>=</span><span>"DynamicToolServer"</span><span>)</span></span>
<span></span>
<span><span>@mcp.tool</span></span>
<span><span>def</span><span> calculate_sum</span><span>(</span><span>a</span><span>: </span><span>int</span><span>, </span><span>b</span><span>: </span><span>int</span><span>) -&gt; </span><span>int</span><span>:</span></span>
<span><span>    """Add two numbers together."""</span></span>
<span><span>    return</span><span> a </span><span>+</span><span> b</span></span>
<span></span>
<span><span>mcp.remove_tool(</span><span>"calculate_sum"</span><span>)</span></span>
```


Resources represent data or files that an MCP client can read, and resource templates extend this concept by allowing clients to request dynamically generated resources based on parameters passed in the URI. FastMCP simplifies defining both static and dynamic resources, primarily using the `@mcp.resource` decorator.

## What Are Resources?

Resources provide read-only access to data for the LLM or client application. When a client requests a resource URI:

1.  FastMCP finds the corresponding resource definition.
2.  If it’s dynamic (defined by a function), the function is executed.
3.  The content (text, JSON, binary data) is returned to the client.

This allows LLMs to access files, database content, configuration, or dynamically generated information relevant to the conversation.

### The `@resource` Decorator

The most common way to define a resource is by decorating a Python function. The decorator requires the resource’s unique URI.

```
<span><span>import</span><span> json</span></span>
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP</span></span>
<span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span><span>name</span><span>=</span><span>"DataServer"</span><span>)</span></span>
<span></span>
<span><span># Basic dynamic resource returning a string</span></span>
<span><span>@mcp.resource</span><span>(</span><span>"resource://greeting"</span><span>)</span></span>
<span><span>def</span><span> get_greeting</span><span>() -&gt; </span><span>str</span><span>:</span></span>
<span><span>    """Provides a simple greeting message."""</span></span>
<span><span>    return</span><span> "Hello from FastMCP Resources!"</span></span>
<span></span>
<span><span># Resource returning JSON data (dict is auto-serialized)</span></span>
<span><span>@mcp.resource</span><span>(</span><span>"data://config"</span><span>)</span></span>
<span><span>def</span><span> get_config</span><span>() -&gt; </span><span>dict</span><span>:</span></span>
<span><span>    """Provides application configuration as JSON."""</span></span>
<span><span>    return</span><span> {</span></span>
<span><span>        "theme"</span><span>: </span><span>"dark"</span><span>,</span></span>
<span><span>        "version"</span><span>: </span><span>"1.2.0"</span><span>,</span></span>
<span><span>        "features"</span><span>: [</span><span>"tools"</span><span>, </span><span>"resources"</span><span>],</span></span>
<span><span>    }</span></span>
```

**Key Concepts:**

-   **URI:** The first argument to `@resource` is the unique URI (e.g., `"resource://greeting"`) clients use to request this data.
-   **Lazy Loading:** The decorated function (`get_greeting`, `get_config`) is only executed when a client specifically requests that resource URI via `resources/read`.
-   **Inferred Metadata:** By default:
    -   Resource Name: Taken from the function name (`get_greeting`).
    -   Resource Description: Taken from the function’s docstring.

#### Decorator Arguments

You can customize the resource’s properties using arguments in the `@mcp.resource` decorator:

```
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP</span></span>
<span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span><span>name</span><span>=</span><span>"DataServer"</span><span>)</span></span>
<span></span>
<span><span># Example specifying metadata</span></span>
<span><span>@mcp.resource</span><span>(</span></span>
<span><span>    uri</span><span>=</span><span>"data://app-status"</span><span>,      </span><span># Explicit URI (required)</span></span>
<span><span>    name</span><span>=</span><span>"ApplicationStatus"</span><span>,     </span><span># Custom name</span></span>
<span><span>    description</span><span>=</span><span>"Provides the current status of the application."</span><span>, </span><span># Custom description</span></span>
<span><span>    mime_type</span><span>=</span><span>"application/json"</span><span>, </span><span># Explicit MIME type</span></span>
<span><span>    tags</span><span>=</span><span>{</span><span>"monitoring"</span><span>, </span><span>"status"</span><span>}, </span><span># Categorization tags</span></span>
<span><span>    meta</span><span>=</span><span>{</span><span>"version"</span><span>: </span><span>"2.1"</span><span>, </span><span>"team"</span><span>: </span><span>"infrastructure"</span><span>}  </span><span># Custom metadata</span></span>
<span><span>)</span></span>
<span><span>def</span><span> get_application_status</span><span>() -&gt; </span><span>dict</span><span>:</span></span>
<span><span>    """Internal function description (ignored if description is provided above)."""</span></span>
<span><span>    return</span><span> {</span><span>"status"</span><span>: </span><span>"ok"</span><span>, </span><span>"uptime"</span><span>: </span><span>12345</span><span>, </span><span>"version"</span><span>: mcp.settings.version} </span><span># Example usage</span></span>
```

### Return Values

FastMCP automatically converts your function’s return value into the appropriate MCP resource content:

-   **`str`**: Sent as `TextResourceContents` (with `mime_type="text/plain"` by default).
-   **`dict`, `list`, `pydantic.BaseModel`**: Automatically serialized to a JSON string and sent as `TextResourceContents` (with `mime_type="application/json"` by default).
-   **`bytes`**: Base64 encoded and sent as `BlobResourceContents`. You should specify an appropriate `mime_type` (e.g., `"image/png"`, `"application/octet-stream"`).
-   **`None`**: Results in an empty resource content list being returned.

### Disabling Resources

`` New in version: `2.8.0` `` You can control the visibility and availability of resources and templates by enabling or disabling them. Disabled resources will not appear in the list of available resources or templates, and attempting to read a disabled resource will result in an “Unknown resource” error. By default, all resources are enabled. You can disable a resource upon creation using the `enabled` parameter in the decorator:

```
<span><span>@mcp.resource</span><span>(</span><span>"data://secret"</span><span>, </span><span>enabled</span><span>=</span><span>False</span><span>)</span></span>
<span><span>def</span><span> get_secret_data</span><span>():</span></span>
<span><span>    """This resource is currently disabled."""</span></span>
<span><span>    return</span><span> "Secret data"</span></span>
```

You can also toggle a resource’s state programmatically after it has been created:

```
<span><span>@mcp.resource</span><span>(</span><span>"data://config"</span><span>)</span></span>
<span><span>def</span><span> get_config</span><span>(): </span><span>return</span><span> {</span><span>"version"</span><span>: </span><span>1</span><span>}</span></span>
<span></span>
<span><span># Disable and re-enable the resource</span></span>
<span><span>get_config.disable()</span></span>
<span><span>get_config.enable()</span></span>
```

### Accessing MCP Context

`` New in version: `2.2.5` `` Resources and resource templates can access additional MCP information and features through the `Context` object. To access it, add a parameter to your resource function with a type annotation of `Context`:

```
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP, Context</span></span>
<span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span><span>name</span><span>=</span><span>"DataServer"</span><span>)</span></span>
<span></span>
<span><span>@mcp.resource</span><span>(</span><span>"resource://system-status"</span><span>)</span></span>
<span><span>async</span><span> def</span><span> get_system_status</span><span>(</span><span>ctx</span><span>: Context) -&gt; </span><span>dict</span><span>:</span></span>
<span><span>    """Provides system status information."""</span></span>
<span><span>    return</span><span> {</span></span>
<span><span>        "status"</span><span>: </span><span>"operational"</span><span>,</span></span>
<span><span>        "request_id"</span><span>: ctx.request_id</span></span>
<span><span>    }</span></span>
<span></span>
<span><span>@mcp.resource</span><span>(</span><span>"resource://</span><span>{name}</span><span>/details"</span><span>)</span></span>
<span><span>async</span><span> def</span><span> get_details</span><span>(</span><span>name</span><span>: </span><span>str</span><span>, </span><span>ctx</span><span>: Context) -&gt; </span><span>dict</span><span>:</span></span>
<span><span>    """Get details for a specific name."""</span></span>
<span><span>    return</span><span> {</span></span>
<span><span>        "name"</span><span>: name,</span></span>
<span><span>        "accessed_at"</span><span>: ctx.request_id</span></span>
<span><span>    }</span></span>
```

For full documentation on the Context object and all its capabilities, see the [Context documentation](https://gofastmcp.com/servers/context).

### Async Resources

Use `async def` for resource functions that perform I/O operations (e.g., reading from a database or network) to avoid blocking the server.

```
<span><span>import</span><span> aiofiles</span></span>
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP</span></span>
<span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span><span>name</span><span>=</span><span>"DataServer"</span><span>)</span></span>
<span></span>
<span><span>@mcp.resource</span><span>(</span><span>"file:///app/data/important_log.txt"</span><span>, </span><span>mime_type</span><span>=</span><span>"text/plain"</span><span>)</span></span>
<span><span>async</span><span> def</span><span> read_important_log</span><span>() -&gt; </span><span>str</span><span>:</span></span>
<span><span>    """Reads content from a specific log file asynchronously."""</span></span>
<span><span>    try</span><span>:</span></span>
<span><span>        async</span><span> with</span><span> aiofiles.open(</span><span>"/app/data/important_log.txt"</span><span>, </span><span>mode</span><span>=</span><span>"r"</span><span>) </span><span>as</span><span> f:</span></span>
<span><span>            content </span><span>=</span><span> await</span><span> f.read()</span></span>
<span><span>        return</span><span> content</span></span>
<span><span>    except</span><span> FileNotFoundError</span><span>:</span></span>
<span><span>        return</span><span> "Log file not found."</span></span>
```

### Resource Classes

While `@mcp.resource` is ideal for dynamic content, you can directly register pre-defined resources (like static files or simple text) using `mcp.add_resource()` and concrete `Resource` subclasses.

```
<span><span>from</span><span> pathlib </span><span>import</span><span> Path</span></span>
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP</span></span>
<span><span>from</span><span> fastmcp.resources </span><span>import</span><span> FileResource, TextResource, DirectoryResource</span></span>
<span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span><span>name</span><span>=</span><span>"DataServer"</span><span>)</span></span>
<span></span>
<span><span># 1. Exposing a static file directly</span></span>
<span><span>readme_path </span><span>=</span><span> Path(</span><span>"./README.md"</span><span>).resolve()</span></span>
<span><span>if</span><span> readme_path.exists():</span></span>
<span><span>    # Use a file:// URI scheme</span></span>
<span><span>    readme_resource </span><span>=</span><span> FileResource(</span></span>
<span><span>        uri</span><span>=</span><span>f</span><span>"file://</span><span>{</span><span>readme_path.as_posix()</span><span>}</span><span>"</span><span>,</span></span>
<span><span>        path</span><span>=</span><span>readme_path, </span><span># Path to the actual file</span></span>
<span><span>        name</span><span>=</span><span>"README File"</span><span>,</span></span>
<span><span>        description</span><span>=</span><span>"The project's README."</span><span>,</span></span>
<span><span>        mime_type</span><span>=</span><span>"text/markdown"</span><span>,</span></span>
<span><span>        tags</span><span>=</span><span>{</span><span>"documentation"</span><span>}</span></span>
<span><span>    )</span></span>
<span><span>    mcp.add_resource(readme_resource)</span></span>
<span></span>
<span><span># 2. Exposing simple, predefined text</span></span>
<span><span>notice_resource </span><span>=</span><span> TextResource(</span></span>
<span><span>    uri</span><span>=</span><span>"resource://notice"</span><span>,</span></span>
<span><span>    name</span><span>=</span><span>"Important Notice"</span><span>,</span></span>
<span><span>    text</span><span>=</span><span>"System maintenance scheduled for Sunday."</span><span>,</span></span>
<span><span>    tags</span><span>=</span><span>{</span><span>"notification"</span><span>}</span></span>
<span><span>)</span></span>
<span><span>mcp.add_resource(notice_resource)</span></span>
<span></span>
<span><span># 3. Using a custom key different from the URI</span></span>
<span><span>special_resource </span><span>=</span><span> TextResource(</span></span>
<span><span>    uri</span><span>=</span><span>"resource://common-notice"</span><span>,</span></span>
<span><span>    name</span><span>=</span><span>"Special Notice"</span><span>,</span></span>
<span><span>    text</span><span>=</span><span>"This is a special notice with a custom storage key."</span><span>,</span></span>
<span><span>)</span></span>
<span><span>mcp.add_resource(special_resource, </span><span>key</span><span>=</span><span>"resource://custom-key"</span><span>)</span></span>
<span></span>
<span><span># 4. Exposing a directory listing</span></span>
<span><span>data_dir_path </span><span>=</span><span> Path(</span><span>"./app_data"</span><span>).resolve()</span></span>
<span><span>if</span><span> data_dir_path.is_dir():</span></span>
<span><span>    data_listing_resource </span><span>=</span><span> DirectoryResource(</span></span>
<span><span>        uri</span><span>=</span><span>"resource://data-files"</span><span>,</span></span>
<span><span>        path</span><span>=</span><span>data_dir_path, </span><span># Path to the directory</span></span>
<span><span>        name</span><span>=</span><span>"Data Directory Listing"</span><span>,</span></span>
<span><span>        description</span><span>=</span><span>"Lists files available in the data directory."</span><span>,</span></span>
<span><span>        recursive</span><span>=</span><span>False</span><span> # Set to True to list subdirectories</span></span>
<span><span>    )</span></span>
<span><span>    mcp.add_resource(data_listing_resource) </span><span># Returns JSON list of files</span></span>
```

**Common Resource Classes:**

-   `TextResource`: For simple string content.
-   `BinaryResource`: For raw `bytes` content.
-   `FileResource`: Reads content from a local file path. Handles text/binary modes and lazy reading.
-   `HttpResource`: Fetches content from an HTTP(S) URL (requires `httpx`).
-   `DirectoryResource`: Lists files in a local directory (returns JSON).
-   (`FunctionResource`: Internal class used by `@mcp.resource`).

Use these when the content is static or sourced directly from a file/URL, bypassing the need for a dedicated Python function.

#### Custom Resource Keys

`` New in version: `2.2.0` `` When adding resources directly with `mcp.add_resource()`, you can optionally provide a custom storage key:

```
<span><span># Creating a resource with standard URI as the key</span></span>
<span><span>resource </span><span>=</span><span> TextResource(</span><span>uri</span><span>=</span><span>"resource://data"</span><span>)</span></span>
<span><span>mcp.add_resource(resource)  </span><span># Will be stored and accessed using "resource://data"</span></span>
<span></span>
<span><span># Creating a resource with a custom key</span></span>
<span><span>special_resource </span><span>=</span><span> TextResource(</span><span>uri</span><span>=</span><span>"resource://special-data"</span><span>)</span></span>
<span><span>mcp.add_resource(special_resource, </span><span>key</span><span>=</span><span>"internal://data-v2"</span><span>)  </span><span># Will be stored and accessed using "internal://data-v2"</span></span>
```

Note that this parameter is only available when using `add_resource()` directly and not through the `@resource` decorator, as URIs are provided explicitly when using the decorator.

### Notifications

`` New in version: `2.9.1` `` FastMCP automatically sends `notifications/resources/list_changed` notifications to connected clients when resources or templates are added, enabled, or disabled. This allows clients to stay up-to-date with the current resource set without manually polling for changes.

```
<span><span>@mcp.resource</span><span>(</span><span>"data://example"</span><span>)</span></span>
<span><span>def</span><span> example_resource</span><span>() -&gt; </span><span>str</span><span>:</span></span>
<span><span>    return</span><span> "Hello!"</span></span>
<span></span>
<span><span># These operations trigger notifications:</span></span>
<span><span>mcp.add_resource(example_resource)  </span><span># Sends resources/list_changed notification</span></span>
<span><span>example_resource.disable()          </span><span># Sends resources/list_changed notification  </span></span>
<span><span>example_resource.enable()           </span><span># Sends resources/list_changed notification</span></span>
```

Notifications are only sent when these operations occur within an active MCP request context (e.g., when called from within a tool or other MCP operation). Operations performed during server initialization do not trigger notifications. Clients can handle these notifications using a [message handler](https://gofastmcp.com/clients/messages) to automatically refresh their resource lists or update their interfaces.

### Annotations

`` New in version: `2.11.0` `` FastMCP allows you to add specialized metadata to your resources through annotations. These annotations communicate how resources behave to client applications without consuming token context in LLM prompts. Annotations serve several purposes in client applications:

-   Indicating whether resources are read-only or may have side effects
-   Describing the safety profile of resources (idempotent vs. non-idempotent)
-   Helping clients optimize caching and access patterns

You can add annotations to a resource using the `annotations` parameter in the `@mcp.resource` decorator:

```
<span><span>@mcp.resource</span><span>(</span></span>
<span><span>    "data://config"</span><span>,</span></span>
<span><span>    annotations</span><span>=</span><span>{</span></span>
<span><span>        "readOnlyHint"</span><span>: </span><span>True</span><span>,</span></span>
<span><span>        "idempotentHint"</span><span>: </span><span>True</span></span>
<span><span>    }</span></span>
<span><span>)</span></span>
<span><span>def</span><span> get_config</span><span>() -&gt; </span><span>dict</span><span>:</span></span>
<span><span>    """Get application configuration."""</span></span>
<span><span>    return</span><span> {</span><span>"version"</span><span>: </span><span>"1.0"</span><span>, </span><span>"debug"</span><span>: </span><span>False</span><span>}</span></span>
```

FastMCP supports these standard annotations:

| Annotation | Type | Default | Purpose |
| --- | --- | --- | --- |
| `readOnlyHint` | boolean | true | Indicates if the resource only provides data without side effects |
| `idempotentHint` | boolean | true | Indicates if repeated reads have the same effect as a single read |

Remember that annotations help make better user experiences but should be treated as advisory hints. They help client applications present appropriate UI elements and optimize access patterns, but won’t enforce behavior on their own. Always focus on making your annotations accurately represent what your resource actually does.

## Resource Templates

Resource Templates allow clients to request resources whose content depends on parameters embedded in the URI. Define a template using the **same `@mcp.resource` decorator**, but include `{parameter_name}` placeholders in the URI string and add corresponding arguments to your function signature. Resource templates share most configuration options with regular resources (name, description, mime\_type, tags, annotations), but add the ability to define URI parameters that map to function parameters. Resource templates generate a new resource for each unique set of parameters, which means that resources can be dynamically created on-demand. For example, if the resource template `"user://profile/{name}"` is registered, MCP clients could request `"user://profile/ford"` or `"user://profile/marvin"` to retrieve either of those two user profiles as resources, without having to register each resource individually.

Here is a complete example that shows how to define two resource templates:

```
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP</span></span>
<span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span><span>name</span><span>=</span><span>"DataServer"</span><span>)</span></span>
<span></span>
<span><span># Template URI includes {city} placeholder</span></span>
<span><span>@mcp.resource</span><span>(</span><span>"weather://</span><span>{city}</span><span>/current"</span><span>)</span></span>
<span><span>def</span><span> get_weather</span><span>(</span><span>city</span><span>: </span><span>str</span><span>) -&gt; </span><span>dict</span><span>:</span></span>
<span><span>    """Provides weather information for a specific city."""</span></span>
<span><span>    # In a real implementation, this would call a weather API</span></span>
<span><span>    # Here we're using simplified logic for example purposes</span></span>
<span><span>    return</span><span> {</span></span>
<span><span>        "city"</span><span>: city.capitalize(),</span></span>
<span><span>        "temperature"</span><span>: </span><span>22</span><span>,</span></span>
<span><span>        "condition"</span><span>: </span><span>"Sunny"</span><span>,</span></span>
<span><span>        "unit"</span><span>: </span><span>"celsius"</span></span>
<span><span>    }</span></span>
<span></span>
<span><span># Template with multiple parameters and annotations</span></span>
<span><span>@mcp.resource</span><span>(</span></span>
<span><span>    "repos://</span><span>{owner}</span><span>/</span><span>{repo}</span><span>/info"</span><span>,</span></span>
<span><span>    annotations</span><span>=</span><span>{</span></span>
<span><span>        "readOnlyHint"</span><span>: </span><span>True</span><span>,</span></span>
<span><span>        "idempotentHint"</span><span>: </span><span>True</span></span>
<span><span>    }</span></span>
<span><span>)</span></span>
<span><span>def</span><span> get_repo_info</span><span>(</span><span>owner</span><span>: </span><span>str</span><span>, </span><span>repo</span><span>: </span><span>str</span><span>) -&gt; </span><span>dict</span><span>:</span></span>
<span><span>    """Retrieves information about a GitHub repository."""</span></span>
<span><span>    # In a real implementation, this would call the GitHub API</span></span>
<span><span>    return</span><span> {</span></span>
<span><span>        "owner"</span><span>: owner,</span></span>
<span><span>        "name"</span><span>: repo,</span></span>
<span><span>        "full_name"</span><span>: </span><span>f</span><span>"</span><span>{</span><span>owner</span><span>}</span><span>/</span><span>{</span><span>repo</span><span>}</span><span>"</span><span>,</span></span>
<span><span>        "stars"</span><span>: </span><span>120</span><span>,</span></span>
<span><span>        "forks"</span><span>: </span><span>48</span></span>
<span><span>    }</span></span>
```

With these two templates defined, clients can request a variety of resources:

-   `weather://london/current` → Returns weather for London
-   `weather://paris/current` → Returns weather for Paris
-   `repos://jlowin/fastmcp/info` → Returns info about the jlowin/fastmcp repository
-   `repos://prefecthq/prefect/info` → Returns info about the prefecthq/prefect repository

### Wildcard Parameters

`` New in version: `2.2.4` ``

Resource templates support wildcard parameters that can match multiple path segments. While standard parameters (`{param}`) only match a single path segment and don’t cross ”/” boundaries, wildcard parameters (`{param*}`) can capture multiple segments including slashes. Wildcards capture all subsequent path segments _up until_ the defined part of the URI template (whether literal or another parameter). This allows you to have multiple wildcard parameters in a single URI template.

```
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP</span></span>
<span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span><span>name</span><span>=</span><span>"DataServer"</span><span>)</span></span>
<span></span>
<span></span>
<span><span># Standard parameter only matches one segment</span></span>
<span><span>@mcp.resource</span><span>(</span><span>"files://</span><span>{filename}</span><span>"</span><span>)</span></span>
<span><span>def</span><span> get_file</span><span>(</span><span>filename</span><span>: </span><span>str</span><span>) -&gt; </span><span>str</span><span>:</span></span>
<span><span>    """Retrieves a file by name."""</span></span>
<span><span>    # Will only match files://&lt;single-segment&gt;</span></span>
<span><span>    return</span><span> f</span><span>"File content for: </span><span>{</span><span>filename</span><span>}</span><span>"</span></span>
<span></span>
<span></span>
<span><span># Wildcard parameter can match multiple segments</span></span>
<span><span>@mcp.resource</span><span>(</span><span>"path://{filepath*}"</span><span>)</span></span>
<span><span>def</span><span> get_path_content</span><span>(</span><span>filepath</span><span>: </span><span>str</span><span>) -&gt; </span><span>str</span><span>:</span></span>
<span><span>    """Retrieves content at a specific path."""</span></span>
<span><span>    # Can match path://docs/server/resources.mdx</span></span>
<span><span>    return</span><span> f</span><span>"Content at path: </span><span>{</span><span>filepath</span><span>}</span><span>"</span></span>
<span></span>
<span></span>
<span><span># Mixing standard and wildcard parameters</span></span>
<span><span>@mcp.resource</span><span>(</span><span>"repo://</span><span>{owner}</span><span>/{path*}/template.py"</span><span>)</span></span>
<span><span>def</span><span> get_template_file</span><span>(</span><span>owner</span><span>: </span><span>str</span><span>, </span><span>path</span><span>: </span><span>str</span><span>) -&gt; </span><span>dict</span><span>:</span></span>
<span><span>    """Retrieves a file from a specific repository and path, but </span></span>
<span><span>    only if the resource ends with `template.py`"""</span></span>
<span><span>    # Can match repo://jlowin/fastmcp/src/resources/template.py</span></span>
<span><span>    return</span><span> {</span></span>
<span><span>        "owner"</span><span>: owner,</span></span>
<span><span>        "path"</span><span>: path </span><span>+</span><span> "/template.py"</span><span>,</span></span>
<span><span>        "content"</span><span>: </span><span>f</span><span>"File at </span><span>{</span><span>path</span><span>}</span><span>/template.py in </span><span>{</span><span>owner</span><span>}</span><span>'s repository"</span></span>
<span><span>    }</span></span>
```

Wildcard parameters are useful when:

-   Working with file paths or hierarchical data
-   Creating APIs that need to capture variable-length path segments
-   Building URL-like patterns similar to REST APIs

Note that like regular parameters, each wildcard parameter must still be a named parameter in your function signature, and all required function parameters must appear in the URI template.

### Default Values

`` New in version: `2.2.0` `` When creating resource templates, FastMCP enforces two rules for the relationship between URI template parameters and function parameters:

1.  **Required Function Parameters:** All function parameters without default values (required parameters) must appear in the URI template.
2.  **URI Parameters:** All URI template parameters must exist as function parameters.

However, function parameters with default values don’t need to be included in the URI template. When a client requests a resource, FastMCP will:

-   Extract parameter values from the URI for parameters included in the template
-   Use default values for any function parameters not in the URI template

This allows for flexible API designs. For example, a simple search template with optional parameters:

```
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP</span></span>
<span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span><span>name</span><span>=</span><span>"DataServer"</span><span>)</span></span>
<span></span>
<span><span>@mcp.resource</span><span>(</span><span>"search://</span><span>{query}</span><span>"</span><span>)</span></span>
<span><span>def</span><span> search_resources</span><span>(</span><span>query</span><span>: </span><span>str</span><span>, </span><span>max_results</span><span>: </span><span>int</span><span> =</span><span> 10</span><span>, </span><span>include_archived</span><span>: </span><span>bool</span><span> =</span><span> False</span><span>) -&gt; </span><span>dict</span><span>:</span></span>
<span><span>    """Search for resources matching the query string."""</span></span>
<span><span>    # Only 'query' is required in the URI, the other parameters use their defaults</span></span>
<span><span>    results </span><span>=</span><span> perform_search(query, </span><span>limit</span><span>=</span><span>max_results, </span><span>archived</span><span>=</span><span>include_archived)</span></span>
<span><span>    return</span><span> {</span></span>
<span><span>        "query"</span><span>: query,</span></span>
<span><span>        "max_results"</span><span>: max_results,</span></span>
<span><span>        "include_archived"</span><span>: include_archived,</span></span>
<span><span>        "results"</span><span>: results</span></span>
<span><span>    }</span></span>
```

With this template, clients can request `search://python` and the function will be called with `query="python", max_results=10, include_archived=False`. MCP Developers can still call the underlying `search_resources` function directly with more specific parameters. You can also create multiple resource templates that provide different ways to access the same underlying data by manually applying decorators to a single function:

```
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP</span></span>
<span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span><span>name</span><span>=</span><span>"DataServer"</span><span>)</span></span>
<span></span>
<span><span># Define a user lookup function that can be accessed by different identifiers</span></span>
<span><span>def</span><span> lookup_user</span><span>(</span><span>name</span><span>: </span><span>str</span><span> |</span><span> None</span><span> =</span><span> None</span><span>, </span><span>email</span><span>: </span><span>str</span><span> |</span><span> None</span><span> =</span><span> None</span><span>) -&gt; </span><span>dict</span><span>:</span></span>
<span><span>    """Look up a user by either name or email."""</span></span>
<span><span>    if</span><span> email:</span></span>
<span><span>        return</span><span> find_user_by_email(email)  </span><span># pseudocode</span></span>
<span><span>    elif</span><span> name:</span></span>
<span><span>        return</span><span> find_user_by_name(name)  </span><span># pseudocode</span></span>
<span><span>    else</span><span>:</span></span>
<span><span>        return</span><span> {</span><span>"error"</span><span>: </span><span>"No lookup parameters provided"</span><span>}</span></span>
<span></span>
<span><span># Manually apply multiple decorators to the same function</span></span>
<span><span>mcp.resource(</span><span>"users://email/</span><span>{email}</span><span>"</span><span>)(lookup_user)</span></span>
<span><span>mcp.resource(</span><span>"users://name/</span><span>{name}</span><span>"</span><span>)(lookup_user)</span></span>
```

Now an LLM or client can retrieve user information in two different ways:

-   `users://email/alice@example.com` → Looks up user by email (with name=None)
-   `users://name/Bob` → Looks up user by name (with email=None)

This approach allows a single function to be registered with multiple URI patterns while keeping the implementation clean and straightforward. Templates provide a powerful way to expose parameterized data access points following REST-like principles.

## Error Handling

`` New in version: `2.4.1` `` If your resource function encounters an error, you can raise a standard Python exception (`ValueError`, `TypeError`, `FileNotFoundError`, custom exceptions, etc.) or a FastMCP `ResourceError`. By default, all exceptions (including their details) are logged and converted into an MCP error response to be sent back to the client LLM. This helps the LLM understand failures and react appropriately. If you want to mask internal error details for security reasons, you can:

1.  Use the `mask_error_details=True` parameter when creating your `FastMCP` instance:

```
<span><span>mcp </span><span>=</span><span> FastMCP(</span><span>name</span><span>=</span><span>"SecureServer"</span><span>, </span><span>mask_error_details</span><span>=</span><span>True</span><span>)</span></span>
```

2.  Or use `ResourceError` to explicitly control what error information is sent to clients:

```
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP</span></span>
<span><span>from</span><span> fastmcp.exceptions </span><span>import</span><span> ResourceError</span></span>
<span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span><span>name</span><span>=</span><span>"DataServer"</span><span>)</span></span>
<span></span>
<span><span>@mcp.resource</span><span>(</span><span>"resource://safe-error"</span><span>)</span></span>
<span><span>def</span><span> fail_with_details</span><span>() -&gt; </span><span>str</span><span>:</span></span>
<span><span>    """This resource provides detailed error information."""</span></span>
<span><span>    # ResourceError contents are always sent back to clients,</span></span>
<span><span>    # regardless of mask_error_details setting</span></span>
<span><span>    raise</span><span> ResourceError(</span><span>"Unable to retrieve data: file not found"</span><span>)</span></span>
<span></span>
<span><span>@mcp.resource</span><span>(</span><span>"resource://masked-error"</span><span>)</span></span>
<span><span>def</span><span> fail_with_masked_details</span><span>() -&gt; </span><span>str</span><span>:</span></span>
<span><span>    """This resource masks internal error details when mask_error_details=True."""</span></span>
<span><span>    # This message would be masked if mask_error_details=True</span></span>
<span><span>    raise</span><span> ValueError</span><span>(</span><span>"Sensitive internal file path: /etc/secrets.conf"</span><span>)</span></span>
<span></span>
<span><span>@mcp.resource</span><span>(</span><span>"data://</span><span>{id}</span><span>"</span><span>)</span></span>
<span><span>def</span><span> get_data_by_id</span><span>(</span><span>id</span><span>: </span><span>str</span><span>) -&gt; </span><span>dict</span><span>:</span></span>
<span><span>    """Template resources also support the same error handling pattern."""</span></span>
<span><span>    if</span><span> id</span><span> ==</span><span> "secure"</span><span>:</span></span>
<span><span>        raise</span><span> ValueError</span><span>(</span><span>"Cannot access secure data"</span><span>)</span></span>
<span><span>    elif</span><span> id</span><span> ==</span><span> "missing"</span><span>:</span></span>
<span><span>        raise</span><span> ResourceError(</span><span>"Data ID 'missing' not found in database"</span><span>)</span></span>
<span><span>    return</span><span> {</span><span>"id"</span><span>: </span><span>id</span><span>, </span><span>"value"</span><span>: </span><span>"data"</span><span>}</span></span>
```

When `mask_error_details=True`, only error messages from `ResourceError` will include details, other exceptions will be converted to a generic message.

## Server Behavior

### Duplicate Resources

`` New in version: `2.1.0` `` You can configure how the FastMCP server handles attempts to register multiple resources or templates with the same URI. Use the `on_duplicate_resources` setting during `FastMCP` initialization.

```
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP</span></span>
<span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span></span>
<span><span>    name</span><span>=</span><span>"ResourceServer"</span><span>,</span></span>
<span><span>    on_duplicate_resources</span><span>=</span><span>"error"</span><span> # Raise error on duplicates</span></span>
<span><span>)</span></span>
<span></span>
<span><span>@mcp.resource</span><span>(</span><span>"data://config"</span><span>)</span></span>
<span><span>def</span><span> get_config_v1</span><span>(): </span><span>return</span><span> {</span><span>"version"</span><span>: </span><span>1</span><span>}</span></span>
<span></span>
<span><span># This registration attempt will raise a ValueError because</span></span>
<span><span># "data://config" is already registered and the behavior is "error".</span></span>
<span><span># @mcp.resource("data://config")</span></span>
<span><span># def get_config_v2(): return {"version": 2}</span></span>
```

The duplicate behavior options are:

-   `"warn"` (default): Logs a warning, and the new resource/template replaces the old one.
-   `"error"`: Raises a `ValueError`, preventing the duplicate registration.
-   `"replace"`: Silently replaces the existing resource/template with the new one.
-   `"ignore"`: Keeps the original resource/template and ignores the new registration attempt.


Prompts are reusable message templates that help LLMs generate structured, purposeful responses. FastMCP simplifies defining these templates, primarily using the `@mcp.prompt` decorator.

## What Are Prompts?

Prompts provide parameterized message templates for LLMs. When a client requests a prompt:

1.  FastMCP finds the corresponding prompt definition.
2.  If it has parameters, they are validated against your function signature.
3.  Your function executes with the validated inputs.
4.  The generated message(s) are returned to the LLM to guide its response.

This allows you to define consistent, reusable templates that LLMs can use across different clients and contexts.

### The `@prompt` Decorator

The most common way to define a prompt is by decorating a Python function. The decorator uses the function name as the prompt’s identifier.

```
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP</span></span>
<span><span>from</span><span> fastmcp.prompts.prompt </span><span>import</span><span> Message, PromptMessage, TextContent</span></span>
<span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span><span>name</span><span>=</span><span>"PromptServer"</span><span>)</span></span>
<span></span>
<span><span># Basic prompt returning a string (converted to user message automatically)</span></span>
<span><span>@mcp.prompt</span></span>
<span><span>def</span><span> ask_about_topic</span><span>(</span><span>topic</span><span>: </span><span>str</span><span>) -&gt; </span><span>str</span><span>:</span></span>
<span><span>    """Generates a user message asking for an explanation of a topic."""</span></span>
<span><span>    return</span><span> f</span><span>"Can you please explain the concept of '</span><span>{</span><span>topic</span><span>}</span><span>'?"</span></span>
<span></span>
<span><span># Prompt returning a specific message type</span></span>
<span><span>@mcp.prompt</span></span>
<span><span>def</span><span> generate_code_request</span><span>(</span><span>language</span><span>: </span><span>str</span><span>, </span><span>task_description</span><span>: </span><span>str</span><span>) -&gt; PromptMessage:</span></span>
<span><span>    """Generates a user message requesting code generation."""</span></span>
<span><span>    content </span><span>=</span><span> f</span><span>"Write a </span><span>{</span><span>language</span><span>}</span><span> function that performs the following task: </span><span>{</span><span>task_description</span><span>}</span><span>"</span></span>
<span><span>    return</span><span> PromptMessage(</span><span>role</span><span>=</span><span>"user"</span><span>, </span><span>content</span><span>=</span><span>TextContent(</span><span>type</span><span>=</span><span>"text"</span><span>, </span><span>text</span><span>=</span><span>content))</span></span>
```

**Key Concepts:**

-   **Name:** By default, the prompt name is taken from the function name.
-   **Parameters:** The function parameters define the inputs needed to generate the prompt.
-   **Inferred Metadata:** By default:
    -   Prompt Name: Taken from the function name (`ask_about_topic`).
    -   Prompt Description: Taken from the function’s docstring.

#### Decorator Arguments

While FastMCP infers the name and description from your function, you can override these and add additional metadata using arguments to the `@mcp.prompt` decorator:

```
<span><span>@mcp.prompt</span><span>(</span></span>
<span><span>    name</span><span>=</span><span>"analyze_data_request"</span><span>,          </span><span># Custom prompt name</span></span>
<span><span>    description</span><span>=</span><span>"Creates a request to analyze data with specific parameters"</span><span>,  </span><span># Custom description</span></span>
<span><span>    tags</span><span>=</span><span>{</span><span>"analysis"</span><span>, </span><span>"data"</span><span>},            </span><span># Optional categorization tags</span></span>
<span><span>    meta</span><span>=</span><span>{</span><span>"version"</span><span>: </span><span>"1.1"</span><span>, </span><span>"author"</span><span>: </span><span>"data-team"</span><span>}  </span><span># Custom metadata</span></span>
<span><span>)</span></span>
<span><span>def</span><span> data_analysis_prompt</span><span>(</span></span>
<span><span>    data_uri</span><span>: </span><span>str</span><span> =</span><span> Field(</span><span>description</span><span>=</span><span>"The URI of the resource containing the data."</span><span>),</span></span>
<span><span>    analysis_type</span><span>: </span><span>str</span><span> =</span><span> Field(</span><span>default</span><span>=</span><span>"summary"</span><span>, </span><span>description</span><span>=</span><span>"Type of analysis."</span><span>)</span></span>
<span><span>) -&gt; </span><span>str</span><span>:</span></span>
<span><span>    """This docstring is ignored when description is provided."""</span></span>
<span><span>    return</span><span> f</span><span>"Please perform a '</span><span>{</span><span>analysis_type</span><span>}</span><span>' analysis on the data found at </span><span>{</span><span>data_uri</span><span>}</span><span>."</span></span>
```

### Argument Types

`` New in version: `2.9.0` `` The MCP specification requires that all prompt arguments be passed as strings, but FastMCP allows you to use typed annotations for better developer experience. When you use complex types like `list[int]` or `dict[str, str]`, FastMCP:

1.  **Automatically converts** string arguments from MCP clients to the expected types
2.  **Generates helpful descriptions** showing the exact JSON string format needed
3.  **Preserves direct usage** - you can still call prompts with properly typed arguments

Since the MCP specification only allows string arguments, clients need to know what string format to use for complex types. FastMCP solves this by automatically enhancing the argument descriptions with JSON schema information, making it clear to both humans and LLMs how to format their arguments.

**MCP clients will call this prompt with string arguments:**

```
<span><span>{</span></span>
<span><span>  "numbers"</span><span>: </span><span>"[1, 2, 3, 4, 5]"</span><span>,</span></span>
<span><span>  "metadata"</span><span>: </span><span>"{</span><span>\"</span><span>source</span><span>\"</span><span>: </span><span>\"</span><span>api</span><span>\"</span><span>, </span><span>\"</span><span>version</span><span>\"</span><span>: </span><span>\"</span><span>1.0</span><span>\"</span><span>}"</span><span>,</span></span>
<span><span>  "threshold"</span><span>: </span><span>"2.5"</span></span>
<span><span>}</span></span>
```

**But you can still call it directly with proper types:**

```
<span><span># This also works for direct calls</span></span>
<span><span>result </span><span>=</span><span> await</span><span> prompt.render({</span></span>
<span><span>    "numbers"</span><span>: [</span><span>1</span><span>, </span><span>2</span><span>, </span><span>3</span><span>, </span><span>4</span><span>, </span><span>5</span><span>],</span></span>
<span><span>    "metadata"</span><span>: {</span><span>"source"</span><span>: </span><span>"api"</span><span>, </span><span>"version"</span><span>: </span><span>"1.0"</span><span>}, </span></span>
<span><span>    "threshold"</span><span>: </span><span>2.5</span></span>
<span><span>})</span></span>
```

### Return Values

FastMCP intelligently handles different return types from your prompt function:

-   **`str`**: Automatically converted to a single `PromptMessage`.
-   **`PromptMessage`**: Used directly as provided. (Note a more user-friendly `Message` constructor is available that can accept raw strings instead of `TextContent` objects.)
-   **`list[PromptMessage | str]`**: Used as a sequence of messages (a conversation).
-   **`Any`**: If the return type is not one of the above, the return value is attempted to be converted to a string and used as a `PromptMessage`.

```
<span><span>from</span><span> fastmcp.prompts.prompt </span><span>import</span><span> Message, PromptResult</span></span>
<span></span>
<span><span>@mcp.prompt</span></span>
<span><span>def</span><span> roleplay_scenario</span><span>(</span><span>character</span><span>: </span><span>str</span><span>, </span><span>situation</span><span>: </span><span>str</span><span>) -&gt; PromptResult:</span></span>
<span><span>    """Sets up a roleplaying scenario with initial messages."""</span></span>
<span><span>    return</span><span> [</span></span>
<span><span>        Message(</span><span>f</span><span>"Let's roleplay. You are </span><span>{</span><span>character</span><span>}</span><span>. The situation is: </span><span>{</span><span>situation</span><span>}</span><span>"</span><span>),</span></span>
<span><span>        Message(</span><span>"Okay, I understand. I am ready. What happens next?"</span><span>, </span><span>role</span><span>=</span><span>"assistant"</span><span>)</span></span>
<span><span>    ]</span></span>
```

### Required vs. Optional Parameters

Parameters in your function signature are considered **required** unless they have a default value.

```
<span><span>@mcp.prompt</span></span>
<span><span>def</span><span> data_analysis_prompt</span><span>(</span></span>
<span><span>    data_uri</span><span>: </span><span>str</span><span>,                        </span><span># Required - no default value</span></span>
<span><span>    analysis_type</span><span>: </span><span>str</span><span> =</span><span> "summary"</span><span>,       </span><span># Optional - has default value</span></span>
<span><span>    include_charts</span><span>: </span><span>bool</span><span> =</span><span> False</span><span>          # Optional - has default value</span></span>
<span><span>) -&gt; </span><span>str</span><span>:</span></span>
<span><span>    """Creates a request to analyze data with specific parameters."""</span></span>
<span><span>    prompt </span><span>=</span><span> f</span><span>"Please perform a '</span><span>{</span><span>analysis_type</span><span>}</span><span>' analysis on the data found at </span><span>{</span><span>data_uri</span><span>}</span><span>."</span></span>
<span><span>    if</span><span> include_charts:</span></span>
<span><span>        prompt </span><span>+=</span><span> " Include relevant charts and visualizations."</span></span>
<span><span>    return</span><span> prompt</span></span>
```

In this example, the client _must_ provide `data_uri`. If `analysis_type` or `include_charts` are omitted, their default values will be used.

### Disabling Prompts

`` New in version: `2.8.0` `` You can control the visibility and availability of prompts by enabling or disabling them. Disabled prompts will not appear in the list of available prompts, and attempting to call a disabled prompt will result in an “Unknown prompt” error. By default, all prompts are enabled. You can disable a prompt upon creation using the `enabled` parameter in the decorator:

```
<span><span>@mcp.prompt</span><span>(</span><span>enabled</span><span>=</span><span>False</span><span>)</span></span>
<span><span>def</span><span> experimental_prompt</span><span>():</span></span>
<span><span>    """This prompt is not ready for use."""</span></span>
<span><span>    return</span><span> "This is an experimental prompt."</span></span>
```

You can also toggle a prompt’s state programmatically after it has been created:

```
<span><span>@mcp.prompt</span></span>
<span><span>def</span><span> seasonal_prompt</span><span>(): </span><span>return</span><span> "Happy Holidays!"</span></span>
<span></span>
<span><span># Disable and re-enable the prompt</span></span>
<span><span>seasonal_prompt.disable()</span></span>
<span><span>seasonal_prompt.enable()</span></span>
```

### Async Prompts

FastMCP seamlessly supports both standard (`def`) and asynchronous (`async def`) functions as prompts.

```
<span><span># Synchronous prompt</span></span>
<span><span>@mcp.prompt</span></span>
<span><span>def</span><span> simple_question</span><span>(</span><span>question</span><span>: </span><span>str</span><span>) -&gt; </span><span>str</span><span>:</span></span>
<span><span>    """Generates a simple question to ask the LLM."""</span></span>
<span><span>    return</span><span> f</span><span>"Question: </span><span>{</span><span>question</span><span>}</span><span>"</span></span>
<span></span>
<span><span># Asynchronous prompt</span></span>
<span><span>@mcp.prompt</span></span>
<span><span>async</span><span> def</span><span> data_based_prompt</span><span>(</span><span>data_id</span><span>: </span><span>str</span><span>) -&gt; </span><span>str</span><span>:</span></span>
<span><span>    """Generates a prompt based on data that needs to be fetched."""</span></span>
<span><span>    # In a real scenario, you might fetch data from a database or API</span></span>
<span><span>    async</span><span> with</span><span> aiohttp.ClientSession() </span><span>as</span><span> session:</span></span>
<span><span>        async</span><span> with</span><span> session.get(</span><span>f</span><span>"https://api.example.com/data/</span><span>{</span><span>data_id</span><span>}</span><span>"</span><span>) </span><span>as</span><span> response:</span></span>
<span><span>            data </span><span>=</span><span> await</span><span> response.json()</span></span>
<span><span>            return</span><span> f</span><span>"Analyze this data: </span><span>{</span><span>data[</span><span>'content'</span><span>]</span><span>}</span><span>"</span></span>
```

Use `async def` when your prompt function performs I/O operations like network requests, database queries, file I/O, or external service calls.

### Accessing MCP Context

`` New in version: `2.2.5` `` Prompts can access additional MCP information and features through the `Context` object. To access it, add a parameter to your prompt function with a type annotation of `Context`:

```
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP, Context</span></span>
<span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span><span>name</span><span>=</span><span>"PromptServer"</span><span>)</span></span>
<span></span>
<span><span>@mcp.prompt</span></span>
<span><span>async</span><span> def</span><span> generate_report_request</span><span>(</span><span>report_type</span><span>: </span><span>str</span><span>, </span><span>ctx</span><span>: Context) -&gt; </span><span>str</span><span>:</span></span>
<span><span>    """Generates a request for a report."""</span></span>
<span><span>    return</span><span> f</span><span>"Please create a </span><span>{</span><span>report_type</span><span>}</span><span> report. Request ID: </span><span>{</span><span>ctx.request_id</span><span>}</span><span>"</span></span>
```

For full documentation on the Context object and all its capabilities, see the [Context documentation](https://gofastmcp.com/servers/context).

### Notifications

`` New in version: `2.9.1` `` FastMCP automatically sends `notifications/prompts/list_changed` notifications to connected clients when prompts are added, enabled, or disabled. This allows clients to stay up-to-date with the current prompt set without manually polling for changes.

```
<span><span>@mcp.prompt</span></span>
<span><span>def</span><span> example_prompt</span><span>() -&gt; </span><span>str</span><span>:</span></span>
<span><span>    return</span><span> "Hello!"</span></span>
<span></span>
<span><span># These operations trigger notifications:</span></span>
<span><span>mcp.add_prompt(example_prompt)  </span><span># Sends prompts/list_changed notification</span></span>
<span><span>example_prompt.disable()        </span><span># Sends prompts/list_changed notification  </span></span>
<span><span>example_prompt.enable()         </span><span># Sends prompts/list_changed notification</span></span>
```

Notifications are only sent when these operations occur within an active MCP request context (e.g., when called from within a tool or other MCP operation). Operations performed during server initialization do not trigger notifications. Clients can handle these notifications using a [message handler](https://gofastmcp.com/clients/messages) to automatically refresh their prompt lists or update their interfaces.

## Server Behavior

### Duplicate Prompts

`` New in version: `2.1.0` `` You can configure how the FastMCP server handles attempts to register multiple prompts with the same name. Use the `on_duplicate_prompts` setting during `FastMCP` initialization.

```
<span><span>from</span><span> fastmcp </span><span>import</span><span> FastMCP</span></span>
<span></span>
<span><span>mcp </span><span>=</span><span> FastMCP(</span></span>
<span><span>    name</span><span>=</span><span>"PromptServer"</span><span>,</span></span>
<span><span>    on_duplicate_prompts</span><span>=</span><span>"error"</span><span>  # Raise an error if a prompt name is duplicated</span></span>
<span><span>)</span></span>
<span></span>
<span><span>@mcp.prompt</span></span>
<span><span>def</span><span> greeting</span><span>(): </span><span>return</span><span> "Hello, how can I help you today?"</span></span>
<span></span>
<span><span># This registration attempt will raise a ValueError because</span></span>
<span><span># "greeting" is already registered and the behavior is "error".</span></span>
<span><span># @mcp.prompt</span></span>
<span><span># def greeting(): return "Hi there! What can I do for you?"</span></span>
```

The duplicate behavior options are:

-   `"warn"` (default): Logs a warning, and the new prompt replaces the old one.
-   `"error"`: Raises a `ValueError`, preventing the duplicate registration.
-   `"replace"`: Silently replaces the existing prompt with the new one.
-   `"ignore"`: Keeps the original prompt and ignores the new registration attempt.


