The FastMCP Server
The core FastMCP server class for building MCP applications with tools, resources, and prompts.

The central piece of a FastMCP application is the FastMCP server class. This class acts as the main container for your application’s tools, resources, and prompts, and manages communication with MCP clients. Instantiating a server is straightforward. You typically provide a name for your server, which helps identify it in client applications or logs.
from fastmcp import FastMCP

# Create a basic server instance
mcp = FastMCP(name="MyAssistantServer")

# You can also add instructions for how to interact with the server
mcp_with_instructions = FastMCP(
    name="HelpfulAssistant",
    instructions="""
        This server provides data analysis tools.
        Call get_average() to analyze numerical data.
    """,
)
The FastMCP constructor accepts several arguments:
FastMCP Constructor Parameters

A human-readable name for your server

Description of how to interact with this server. These instructions help clients understand the server’s purpose and available functionality

auth

OAuthProvider | TokenVerifier | None

Authentication provider for securing HTTP-based transports. See Authentication for configuration options
lifespan

AsyncContextManager | None

An async context manager function for server startup and shutdown logic

tools

list[Tool | Callable] | None

A list of tools (or functions to convert to tools) to add to the server. In some cases, providing tools programmatically may be more convenient than using the @mcp.tool decorator

Only expose components with at least one matching tag

Hide components with any matching tag

on_duplicate_tools

Literal["error", "warn", "replace"]

default:"error"

How to handle duplicate tool registrations

on_duplicate_resources

Literal["error", "warn", "replace"]

default:"warn"

How to handle duplicate resource registrations

on_duplicate_prompts

Literal["error", "warn", "replace"]

default:"replace"

How to handle duplicate prompt registrations

New in version: 2.11.0Whether to include FastMCP metadata in component responses. When True, component tags and other FastMCP-specific metadata are included in the _fastmcp namespace within each component’s meta field. When False, this metadata is omitted, resulting in cleaner integration with external systems. Can be overridden globally via FASTMCP_INCLUDE_FASTMCP_META environment variable

Components

FastMCP servers expose several types of components to the client:
Tools

Tools are functions that the client can call to perform actions or access external systems.
@mcp.tool
def multiply(a: float, b: float) -> float:
    """Multiplies two numbers together."""
    return a * b
See Tools for detailed documentation.
Resources

Resources expose data sources that the client can read.
@mcp.resource("data://config")
def get_config() -> dict:
    """Provides the application configuration."""
    return {"theme": "dark", "version": "1.0"}
See Resources & Templates for detailed documentation.
Resource Templates

Resource templates are parameterized resources that allow the client to request specific data.
@mcp.resource("users://{user_id}/profile")
def get_user_profile(user_id: int) -> dict:
    """Retrieves a user's profile by ID."""
    # The {user_id} in the URI is extracted and passed to this function
    return {"id": user_id, "name": f"User {user_id}", "status": "active"}
See Resources & Templates for detailed documentation.
Prompts

Prompts are reusable message templates for guiding the LLM.
@mcp.prompt
def analyze_data(data_points: list[float]) -> str:
    """Creates a prompt asking for analysis of numerical data."""
    formatted_data = ", ".join(str(point) for point in data_points)
    return f"Please analyze these data points: {formatted_data}"
See Prompts for detailed documentation.
Tag-Based Filtering

New in version: 2.8.0 FastMCP supports tag-based filtering to selectively expose components based on configurable include/exclude tag sets. This is useful for creating different views of your server for different environments or users. Components can be tagged when defined using the tags parameter:
@mcp.tool(tags={"public", "utility"})
def public_tool() -> str:
    return "This tool is public"

@mcp.tool(tags={"internal", "admin"})
def admin_tool() -> str:
    return "This tool is for admins only"
The filtering logic works as follows:
Include tags: If specified, only components with at least one matching tag are exposed
Exclude tags: Components with any matching tag are filtered out
Precedence: Exclude tags always take priority over include tags
To ensure a component is never exposed, you can set enabled=False on the component itself. To learn more, see the component-specific documentation.

You configure tag-based filtering when creating your server:
# Only expose components tagged with "public"
mcp = FastMCP(include_tags={"public"})

# Hide components tagged as "internal" or "deprecated"  
mcp = FastMCP(exclude_tags={"internal", "deprecated"})

# Combine both: show admin tools but hide deprecated ones
mcp = FastMCP(include_tags={"admin"}, exclude_tags={"deprecated"})
This filtering applies to all component types (tools, resources, resource templates, and prompts) and affects both listing and access.
Running the Server

FastMCP servers need a transport mechanism to communicate with clients. You typically start your server by calling the mcp.run() method on your FastMCP instance, often within an if __name__ == "__main__": block in your main server script. This pattern ensures compatibility with various MCP clients.
# my_server.py
from fastmcp import FastMCP

mcp = FastMCP(name="MyServer")

@mcp.tool
def greet(name: str) -> str:
    """Greet a user by name."""
    return f"Hello, {name}!"

if __name__ == "__main__":
    # This runs the server, defaulting to STDIO transport
    mcp.run()
    
    # To use a different transport, e.g., HTTP:
    # mcp.run(transport="http", host="127.0.0.1", port=9000)
FastMCP supports several transport options:
STDIO (default, for local tools)
HTTP (recommended for web services, uses Streamable HTTP protocol)
SSE (legacy web transport, deprecated)
The server can also be run using the FastMCP CLI. For detailed information on each transport, how to configure them (host, port, paths), and when to use which, please refer to the Running Your FastMCP Server guide.
Custom Routes

When running your server with HTTP transport, you can add custom web routes alongside your MCP endpoint using the @custom_route decorator. This is useful for simple endpoints like health checks that need to be served alongside your MCP server:
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse

mcp = FastMCP("MyServer")

@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")

if __name__ == "__main__":
    mcp.run(transport="http")  # Health check at http://localhost:8000/health
Custom routes are served alongside your MCP endpoint and are useful for:
Health check endpoints for monitoring
Simple status or info endpoints
Basic webhooks or callbacks
For more complex web applications, consider mounting your MCP server into a FastAPI or Starlette app.
Composing Servers

New in version: 2.2.0 FastMCP supports composing multiple servers together using import_server (static copy) and mount (live link). This allows you to organize large applications into modular components or reuse existing servers. See the Server Composition guide for full details, best practices, and examples.
# Example: Importing a subserver
from fastmcp import FastMCP
import asyncio

main = FastMCP(name="Main")
sub = FastMCP(name="Sub")

@sub.tool
def hello(): 
    return "hi"

# Mount directly
main.mount(sub, prefix="sub")
Proxying Servers

New in version: 2.0.0 FastMCP can act as a proxy for any MCP server (local or remote) using FastMCP.as_proxy, letting you bridge transports or add a frontend to existing servers. For example, you can expose a remote SSE server locally via stdio, or vice versa. Proxies automatically handle concurrent operations safely by creating fresh sessions for each request when using disconnected clients. See the Proxying Servers guide for details and advanced usage.
from fastmcp import FastMCP, Client

backend = Client("http://example.com/mcp/sse")
proxy = FastMCP.as_proxy(backend, name="ProxyServer")
# Now use the proxy like any FastMCP server
OpenAPI Integration

New in version: 2.0.0 FastMCP can automatically generate servers from OpenAPI specifications or existing FastAPI applications using FastMCP.from_openapi() and FastMCP.from_fastapi(). This allows you to instantly convert existing APIs into MCP servers without manual tool creation. See the FastAPI Integration and OpenAPI Integration guides for detailed examples and configuration options.
import httpx
from fastmcp import FastMCP

# From OpenAPI spec
spec = httpx.get("https://api.example.com/openapi.json").json()
mcp = FastMCP.from_openapi(openapi_spec=spec, client=httpx.AsyncClient())

# From FastAPI app
from fastapi import FastAPI
app = FastAPI()
mcp = FastMCP.from_fastapi(app=app)
Server Configuration

Servers can be configured using a combination of initialization arguments, global settings, and transport-specific settings.
Server-Specific Configuration

Server-specific settings are passed when creating the FastMCP instance and control server behavior:
from fastmcp import FastMCP

# Configure server-specific settings
mcp = FastMCP(
    name="ConfiguredServer",
    include_tags={"public", "api"},              # Only expose these tagged components
    exclude_tags={"internal", "deprecated"},     # Hide these tagged components
    on_duplicate_tools="error",                  # Handle duplicate registrations
    on_duplicate_resources="warn",
    on_duplicate_prompts="replace",
    include_fastmcp_meta=False,                  # Disable FastMCP metadata for cleaner integration
)
Global Settings

Global settings affect all FastMCP servers and can be configured via environment variables (prefixed with FASTMCP_) or in a .env file:
import fastmcp

# Access global settings
print(fastmcp.settings.log_level)        # Default: "INFO"
print(fastmcp.settings.mask_error_details)  # Default: False
print(fastmcp.settings.resource_prefix_format)  # Default: "path"
print(fastmcp.settings.include_fastmcp_meta)   # Default: True
Common global settings include:
log_level: Logging level (“DEBUG”, “INFO”, “WARNING”, “ERROR”, “CRITICAL”), set with FASTMCP_LOG_LEVEL
mask_error_details: Whether to hide detailed error information from clients, set with FASTMCP_MASK_ERROR_DETAILS
resource_prefix_format: How to format resource prefixes (“path” or “protocol”), set with FASTMCP_RESOURCE_PREFIX_FORMAT
include_fastmcp_meta: Whether to include FastMCP metadata in component responses (default: True), set with FASTMCP_INCLUDE_FASTMCP_META
Transport-Specific Configuration

Transport settings are provided when running the server and control network behavior:
# Configure transport when running
mcp.run(
    transport="http",
    host="0.0.0.0",           # Bind to all interfaces
    port=9000,                # Custom port
    log_level="DEBUG",        # Override global log level
)

# Or for async usage
await mcp.run_async(
    transport="http", 
    host="127.0.0.1",
    port=8080,
)
Setting Global Configuration

Global FastMCP settings can be configured via environment variables (prefixed with FASTMCP_):
# Configure global FastMCP behavior
export FASTMCP_LOG_LEVEL=DEBUG
export FASTMCP_MASK_ERROR_DETAILS=True
export FASTMCP_RESOURCE_PREFIX_FORMAT=protocol
export FASTMCP_INCLUDE_FASTMCP_META=False
Custom Tool Serialization

New in version: 2.2.7 By default, FastMCP serializes tool return values to JSON when they need to be converted to text. You can customize this behavior by providing a tool_serializer function when creating your server:
import yaml
from fastmcp import FastMCP

# Define a custom serializer that formats dictionaries as YAML
def yaml_serializer(data):
    return yaml.dump(data, sort_keys=False)

# Create a server with the custom serializer
mcp = FastMCP(name="MyServer", tool_serializer=yaml_serializer)

@mcp.tool
def get_config():
    """Returns configuration in YAML format."""
    return {"api_key": "abc123", "debug": True, "rate_limit": 100}
The serializer function takes any data object and returns a string representation. This is applied to all non-string return values from your tools. Tools that already return strings bypass the serializer. This customization is useful when you want to:
Format data in a specific way (like YAML or custom formats)
Control specific serialization options (like indentation or sorting)
Add metadata or transform data before sending it to clients
If the serializer function raises an exception, the tool will fall back to the default JSON serialization to avoid breaking the server.


The FastMCP Server
The core FastMCP server class for building MCP applications with tools, resources, and prompts.

The central piece of a FastMCP application is the FastMCP server class. This class acts as the main container for your application’s tools, resources, and prompts, and manages communication with MCP clients. Instantiating a server is straightforward. You typically provide a name for your server, which helps identify it in client applications or logs.
from fastmcp import FastMCP

# Create a basic server instance
mcp = FastMCP(name="MyAssistantServer")

# You can also add instructions for how to interact with the server
mcp_with_instructions = FastMCP(
    name="HelpfulAssistant",
    instructions="""
        This server provides data analysis tools.
        Call get_average() to analyze numerical data.
    """,
)
The FastMCP constructor accepts several arguments:
FastMCP Constructor Parameters

A human-readable name for your server

Description of how to interact with this server. These instructions help clients understand the server’s purpose and available functionality

auth

OAuthProvider | TokenVerifier | None

Authentication provider for securing HTTP-based transports. See Authentication for configuration options
lifespan

AsyncContextManager | None

An async context manager function for server startup and shutdown logic

tools

list[Tool | Callable] | None

A list of tools (or functions to convert to tools) to add to the server. In some cases, providing tools programmatically may be more convenient than using the @mcp.tool decorator

Only expose components with at least one matching tag

Hide components with any matching tag

on_duplicate_tools

Literal["error", "warn", "replace"]

default:"error"

How to handle duplicate tool registrations

on_duplicate_resources

Literal["error", "warn", "replace"]

default:"warn"

How to handle duplicate resource registrations

on_duplicate_prompts

Literal["error", "warn", "replace"]

default:"replace"

How to handle duplicate prompt registrations

New in version: 2.11.0Whether to include FastMCP metadata in component responses. When True, component tags and other FastMCP-specific metadata are included in the _fastmcp namespace within each component’s meta field. When False, this metadata is omitted, resulting in cleaner integration with external systems. Can be overridden globally via FASTMCP_INCLUDE_FASTMCP_META environment variable

Components

FastMCP servers expose several types of components to the client:
Tools

Tools are functions that the client can call to perform actions or access external systems.
@mcp.tool
def multiply(a: float, b: float) -> float:
    """Multiplies two numbers together."""
    return a * b
See Tools for detailed documentation.
Resources

Resources expose data sources that the client can read.
@mcp.resource("data://config")
def get_config() -> dict:
    """Provides the application configuration."""
    return {"theme": "dark", "version": "1.0"}
See Resources & Templates for detailed documentation.
Resource Templates

Resource templates are parameterized resources that allow the client to request specific data.
@mcp.resource("users://{user_id}/profile")
def get_user_profile(user_id: int) -> dict:
    """Retrieves a user's profile by ID."""
    # The {user_id} in the URI is extracted and passed to this function
    return {"id": user_id, "name": f"User {user_id}", "status": "active"}
See Resources & Templates for detailed documentation.
Prompts

Prompts are reusable message templates for guiding the LLM.
@mcp.prompt
def analyze_data(data_points: list[float]) -> str:
    """Creates a prompt asking for analysis of numerical data."""
    formatted_data = ", ".join(str(point) for point in data_points)
    return f"Please analyze these data points: {formatted_data}"
See Prompts for detailed documentation.
Tag-Based Filtering

New in version: 2.8.0 FastMCP supports tag-based filtering to selectively expose components based on configurable include/exclude tag sets. This is useful for creating different views of your server for different environments or users. Components can be tagged when defined using the tags parameter:
@mcp.tool(tags={"public", "utility"})
def public_tool() -> str:
    return "This tool is public"

@mcp.tool(tags={"internal", "admin"})
def admin_tool() -> str:
    return "This tool is for admins only"
The filtering logic works as follows:
Include tags: If specified, only components with at least one matching tag are exposed
Exclude tags: Components with any matching tag are filtered out
Precedence: Exclude tags always take priority over include tags
To ensure a component is never exposed, you can set enabled=False on the component itself. To learn more, see the component-specific documentation.

You configure tag-based filtering when creating your server:
# Only expose components tagged with "public"
mcp = FastMCP(include_tags={"public"})

# Hide components tagged as "internal" or "deprecated"  
mcp = FastMCP(exclude_tags={"internal", "deprecated"})

# Combine both: show admin tools but hide deprecated ones
mcp = FastMCP(include_tags={"admin"}, exclude_tags={"deprecated"})
This filtering applies to all component types (tools, resources, resource templates, and prompts) and affects both listing and access.
Running the Server

FastMCP servers need a transport mechanism to communicate with clients. You typically start your server by calling the mcp.run() method on your FastMCP instance, often within an if __name__ == "__main__": block in your main server script. This pattern ensures compatibility with various MCP clients.
# my_server.py
from fastmcp import FastMCP

mcp = FastMCP(name="MyServer")

@mcp.tool
def greet(name: str) -> str:
    """Greet a user by name."""
    return f"Hello, {name}!"

if __name__ == "__main__":
    # This runs the server, defaulting to STDIO transport
    mcp.run()
    
    # To use a different transport, e.g., HTTP:
    # mcp.run(transport="http", host="127.0.0.1", port=9000)
FastMCP supports several transport options:
STDIO (default, for local tools)
HTTP (recommended for web services, uses Streamable HTTP protocol)
SSE (legacy web transport, deprecated)
The server can also be run using the FastMCP CLI. For detailed information on each transport, how to configure them (host, port, paths), and when to use which, please refer to the Running Your FastMCP Server guide.
Custom Routes

When running your server with HTTP transport, you can add custom web routes alongside your MCP endpoint using the @custom_route decorator. This is useful for simple endpoints like health checks that need to be served alongside your MCP server:
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse

mcp = FastMCP("MyServer")

@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")

if __name__ == "__main__":
    mcp.run(transport="http")  # Health check at http://localhost:8000/health
Custom routes are served alongside your MCP endpoint and are useful for:
Health check endpoints for monitoring
Simple status or info endpoints
Basic webhooks or callbacks
For more complex web applications, consider mounting your MCP server into a FastAPI or Starlette app.
Composing Servers

New in version: 2.2.0 FastMCP supports composing multiple servers together using import_server (static copy) and mount (live link). This allows you to organize large applications into modular components or reuse existing servers. See the Server Composition guide for full details, best practices, and examples.
# Example: Importing a subserver
from fastmcp import FastMCP
import asyncio

main = FastMCP(name="Main")
sub = FastMCP(name="Sub")

@sub.tool
def hello(): 
    return "hi"

# Mount directly
main.mount(sub, prefix="sub")
Proxying Servers

New in version: 2.0.0 FastMCP can act as a proxy for any MCP server (local or remote) using FastMCP.as_proxy, letting you bridge transports or add a frontend to existing servers. For example, you can expose a remote SSE server locally via stdio, or vice versa. Proxies automatically handle concurrent operations safely by creating fresh sessions for each request when using disconnected clients. See the Proxying Servers guide for details and advanced usage.
from fastmcp import FastMCP, Client

backend = Client("http://example.com/mcp/sse")
proxy = FastMCP.as_proxy(backend, name="ProxyServer")
# Now use the proxy like any FastMCP server
OpenAPI Integration

New in version: 2.0.0 FastMCP can automatically generate servers from OpenAPI specifications or existing FastAPI applications using FastMCP.from_openapi() and FastMCP.from_fastapi(). This allows you to instantly convert existing APIs into MCP servers without manual tool creation. See the FastAPI Integration and OpenAPI Integration guides for detailed examples and configuration options.
import httpx
from fastmcp import FastMCP

# From OpenAPI spec
spec = httpx.get("https://api.example.com/openapi.json").json()
mcp = FastMCP.from_openapi(openapi_spec=spec, client=httpx.AsyncClient())

# From FastAPI app
from fastapi import FastAPI
app = FastAPI()
mcp = FastMCP.from_fastapi(app=app)
Server Configuration

Servers can be configured using a combination of initialization arguments, global settings, and transport-specific settings.
Server-Specific Configuration

Server-specific settings are passed when creating the FastMCP instance and control server behavior:
from fastmcp import FastMCP

# Configure server-specific settings
mcp = FastMCP(
    name="ConfiguredServer",
    include_tags={"public", "api"},              # Only expose these tagged components
    exclude_tags={"internal", "deprecated"},     # Hide these tagged components
    on_duplicate_tools="error",                  # Handle duplicate registrations
    on_duplicate_resources="warn",
    on_duplicate_prompts="replace",
    include_fastmcp_meta=False,                  # Disable FastMCP metadata for cleaner integration
)
Global Settings

Global settings affect all FastMCP servers and can be configured via environment variables (prefixed with FASTMCP_) or in a .env file:
import fastmcp

# Access global settings
print(fastmcp.settings.log_level)        # Default: "INFO"
print(fastmcp.settings.mask_error_details)  # Default: False
print(fastmcp.settings.resource_prefix_format)  # Default: "path"
print(fastmcp.settings.include_fastmcp_meta)   # Default: True
Common global settings include:
log_level: Logging level (“DEBUG”, “INFO”, “WARNING”, “ERROR”, “CRITICAL”), set with FASTMCP_LOG_LEVEL
mask_error_details: Whether to hide detailed error information from clients, set with FASTMCP_MASK_ERROR_DETAILS
resource_prefix_format: How to format resource prefixes (“path” or “protocol”), set with FASTMCP_RESOURCE_PREFIX_FORMAT
include_fastmcp_meta: Whether to include FastMCP metadata in component responses (default: True), set with FASTMCP_INCLUDE_FASTMCP_META
Transport-Specific Configuration

Transport settings are provided when running the server and control network behavior:
# Configure transport when running
mcp.run(
    transport="http",
    host="0.0.0.0",           # Bind to all interfaces
    port=9000,                # Custom port
    log_level="DEBUG",        # Override global log level
)

# Or for async usage
await mcp.run_async(
    transport="http", 
    host="127.0.0.1",
    port=8080,
)
Setting Global Configuration

Global FastMCP settings can be configured via environment variables (prefixed with FASTMCP_):
# Configure global FastMCP behavior
export FASTMCP_LOG_LEVEL=DEBUG
export FASTMCP_MASK_ERROR_DETAILS=True
export FASTMCP_RESOURCE_PREFIX_FORMAT=protocol
export FASTMCP_INCLUDE_FASTMCP_META=False
Custom Tool Serialization

New in version: 2.2.7 By default, FastMCP serializes tool return values to JSON when they need to be converted to text. You can customize this behavior by providing a tool_serializer function when creating your server:
import yaml
from fastmcp import FastMCP

# Define a custom serializer that formats dictionaries as YAML
def yaml_serializer(data):
    return yaml.dump(data, sort_keys=False)

# Create a server with the custom serializer
mcp = FastMCP(name="MyServer", tool_serializer=yaml_serializer)

@mcp.tool
def get_config():
    """Returns configuration in YAML format."""
    return {"api_key": "abc123", "debug": True, "rate_limit": 100}
The serializer function takes any data object and returns a string representation. This is applied to all non-string return values from your tools. Tools that already return strings bypass the serializer. This customization is useful when you want to:
Format data in a specific way (like YAML or custom formats)
Control specific serialization options (like indentation or sorting)
Add metadata or transform data before sending it to clients
If the serializer function raises an exception, the tool will fall back to the default JSON serialization to avoid breaking the server.


Resources & Templates
Expose data sources and dynamic content generators to your MCP client.

Resources represent data or files that an MCP client can read, and resource templates extend this concept by allowing clients to request dynamically generated resources based on parameters passed in the URI. FastMCP simplifies defining both static and dynamic resources, primarily using the @mcp.resource decorator.
What Are Resources?

Resources provide read-only access to data for the LLM or client application. When a client requests a resource URI:
FastMCP finds the corresponding resource definition.
If it’s dynamic (defined by a function), the function is executed.
The content (text, JSON, binary data) is returned to the client.
This allows LLMs to access files, database content, configuration, or dynamically generated information relevant to the conversation.
Resources

The @resource Decorator

The most common way to define a resource is by decorating a Python function. The decorator requires the resource’s unique URI.
import json
from fastmcp import FastMCP

mcp = FastMCP(name="DataServer")

# Basic dynamic resource returning a string
@mcp.resource("resource://greeting")
def get_greeting() -> str:
    """Provides a simple greeting message."""
    return "Hello from FastMCP Resources!"

# Resource returning JSON data (dict is auto-serialized)
@mcp.resource("data://config")
def get_config() -> dict:
    """Provides application configuration as JSON."""
    return {
        "theme": "dark",
        "version": "1.2.0",
        "features": ["tools", "resources"],
    }
Key Concepts:
URI: The first argument to @resource is the unique URI (e.g., "resource://greeting") clients use to request this data.
Lazy Loading: The decorated function (get_greeting, get_config) is only executed when a client specifically requests that resource URI via resources/read.
Inferred Metadata: By default:
Resource Name: Taken from the function name (get_greeting).
Resource Description: Taken from the function’s docstring.
Decorator Arguments

You can customize the resource’s properties using arguments in the @mcp.resource decorator:
from fastmcp import FastMCP

mcp = FastMCP(name="DataServer")

# Example specifying metadata
@mcp.resource(
    uri="data://app-status",      # Explicit URI (required)
    name="ApplicationStatus",     # Custom name
    description="Provides the current status of the application.", # Custom description
    mime_type="application/json", # Explicit MIME type
    tags={"monitoring", "status"}, # Categorization tags
    meta={"version": "2.1", "team": "infrastructure"}  # Custom metadata
)
def get_application_status() -> dict:
    """Internal function description (ignored if description is provided above)."""
    return {"status": "ok", "uptime": 12345, "version": mcp.settings.version} # Example usage
The unique identifier for the resource

A human-readable name. If not provided, defaults to function name

Explanation of the resource. If not provided, defaults to docstring

Specifies the content type. FastMCP often infers a default like text/plain or application/json, but explicit is better for non-text types

A set of strings used to categorize the resource. These can be used by the server and, in some cases, by clients to filter or group available resources.

A boolean to enable or disable the resource. See Disabling Resources for more information
annotations

Annotations | dict | None

An optional Annotations object or dictionary to add additional metadata about the resource.
Show Annotations attributes

New in version: 2.11.0Optional meta information about the resource. This data is passed through to the MCP client as the _meta field of the client-side resource object and can be used for custom metadata, versioning, or other application-specific purposes.

Return Values

FastMCP automatically converts your function’s return value into the appropriate MCP resource content:
str: Sent as TextResourceContents (with mime_type="text/plain" by default).
dict, list, pydantic.BaseModel: Automatically serialized to a JSON string and sent as TextResourceContents (with mime_type="application/json" by default).
bytes: Base64 encoded and sent as BlobResourceContents. You should specify an appropriate mime_type (e.g., "image/png", "application/octet-stream").
None: Results in an empty resource content list being returned.
Disabling Resources

New in version: 2.8.0 You can control the visibility and availability of resources and templates by enabling or disabling them. Disabled resources will not appear in the list of available resources or templates, and attempting to read a disabled resource will result in an “Unknown resource” error. By default, all resources are enabled. You can disable a resource upon creation using the enabled parameter in the decorator:
@mcp.resource("data://secret", enabled=False)
def get_secret_data():
    """This resource is currently disabled."""
    return "Secret data"
You can also toggle a resource’s state programmatically after it has been created:
@mcp.resource("data://config")
def get_config(): return {"version": 1}

# Disable and re-enable the resource
get_config.disable()
get_config.enable()
Accessing MCP Context

New in version: 2.2.5 Resources and resource templates can access additional MCP information and features through the Context object. To access it, add a parameter to your resource function with a type annotation of Context:
from fastmcp import FastMCP, Context

mcp = FastMCP(name="DataServer")

@mcp.resource("resource://system-status")
async def get_system_status(ctx: Context) -> dict:
    """Provides system status information."""
    return {
        "status": "operational",
        "request_id": ctx.request_id
    }

@mcp.resource("resource://{name}/details")
async def get_details(name: str, ctx: Context) -> dict:
    """Get details for a specific name."""
    return {
        "name": name,
        "accessed_at": ctx.request_id
    }
For full documentation on the Context object and all its capabilities, see the Context documentation.
Async Resources

Use async def for resource functions that perform I/O operations (e.g., reading from a database or network) to avoid blocking the server.
import aiofiles
from fastmcp import FastMCP

mcp = FastMCP(name="DataServer")

@mcp.resource("file:///app/data/important_log.txt", mime_type="text/plain")
async def read_important_log() -> str:
    """Reads content from a specific log file asynchronously."""
    try:
        async with aiofiles.open("/app/data/important_log.txt", mode="r") as f:
            content = await f.read()
        return content
    except FileNotFoundError:
        return "Log file not found."
Resource Classes

While @mcp.resource is ideal for dynamic content, you can directly register pre-defined resources (like static files or simple text) using mcp.add_resource() and concrete Resource subclasses.
from pathlib import Path
from fastmcp import FastMCP
from fastmcp.resources import FileResource, TextResource, DirectoryResource

mcp = FastMCP(name="DataServer")

# 1. Exposing a static file directly
readme_path = Path("./README.md").resolve()
if readme_path.exists():
    # Use a file:// URI scheme
    readme_resource = FileResource(
        uri=f"file://{readme_path.as_posix()}",
        path=readme_path, # Path to the actual file
        name="README File",
        description="The project's README.",
        mime_type="text/markdown",
        tags={"documentation"}
    )
    mcp.add_resource(readme_resource)

# 2. Exposing simple, predefined text
notice_resource = TextResource(
    uri="resource://notice",
    name="Important Notice",
    text="System maintenance scheduled for Sunday.",
    tags={"notification"}
)
mcp.add_resource(notice_resource)

# 3. Using a custom key different from the URI
special_resource = TextResource(
    uri="resource://common-notice",
    name="Special Notice",
    text="This is a special notice with a custom storage key.",
)
mcp.add_resource(special_resource, key="resource://custom-key")

# 4. Exposing a directory listing
data_dir_path = Path("./app_data").resolve()
if data_dir_path.is_dir():
    data_listing_resource = DirectoryResource(
        uri="resource://data-files",
        path=data_dir_path, # Path to the directory
        name="Data Directory Listing",
        description="Lists files available in the data directory.",
        recursive=False # Set to True to list subdirectories
    )
    mcp.add_resource(data_listing_resource) # Returns JSON list of files
Common Resource Classes:
TextResource: For simple string content.
BinaryResource: For raw bytes content.
FileResource: Reads content from a local file path. Handles text/binary modes and lazy reading.
HttpResource: Fetches content from an HTTP(S) URL (requires httpx).
DirectoryResource: Lists files in a local directory (returns JSON).
(FunctionResource: Internal class used by @mcp.resource).
Use these when the content is static or sourced directly from a file/URL, bypassing the need for a dedicated Python function.
Custom Resource Keys

New in version: 2.2.0 When adding resources directly with mcp.add_resource(), you can optionally provide a custom storage key:
# Creating a resource with standard URI as the key
resource = TextResource(uri="resource://data")
mcp.add_resource(resource)  # Will be stored and accessed using "resource://data"

# Creating a resource with a custom key
special_resource = TextResource(uri="resource://special-data")
mcp.add_resource(special_resource, key="internal://data-v2")  # Will be stored and accessed using "internal://data-v2"
Note that this parameter is only available when using add_resource() directly and not through the @resource decorator, as URIs are provided explicitly when using the decorator.
Notifications

New in version: 2.9.1 FastMCP automatically sends notifications/resources/list_changed notifications to connected clients when resources or templates are added, enabled, or disabled. This allows clients to stay up-to-date with the current resource set without manually polling for changes.
@mcp.resource("data://example")
def example_resource() -> str:
    return "Hello!"

# These operations trigger notifications:
mcp.add_resource(example_resource)  # Sends resources/list_changed notification
example_resource.disable()          # Sends resources/list_changed notification  
example_resource.enable()           # Sends resources/list_changed notification
Notifications are only sent when these operations occur within an active MCP request context (e.g., when called from within a tool or other MCP operation). Operations performed during server initialization do not trigger notifications. Clients can handle these notifications using a message handler to automatically refresh their resource lists or update their interfaces.
Annotations

New in version: 2.11.0 FastMCP allows you to add specialized metadata to your resources through annotations. These annotations communicate how resources behave to client applications without consuming token context in LLM prompts. Annotations serve several purposes in client applications:
Indicating whether resources are read-only or may have side effects
Describing the safety profile of resources (idempotent vs. non-idempotent)
Helping clients optimize caching and access patterns
You can add annotations to a resource using the annotations parameter in the @mcp.resource decorator:
@mcp.resource(
    "data://config",
    annotations={
        "readOnlyHint": True,
        "idempotentHint": True
    }
)
def get_config() -> dict:
    """Get application configuration."""
    return {"version": "1.0", "debug": False}
FastMCP supports these standard annotations:
Annotation	Type	Default	Purpose
readOnlyHint	boolean	true	Indicates if the resource only provides data without side effects
idempotentHint	boolean	true	Indicates if repeated reads have the same effect as a single read
Remember that annotations help make better user experiences but should be treated as advisory hints. They help client applications present appropriate UI elements and optimize access patterns, but won’t enforce behavior on their own. Always focus on making your annotations accurately represent what your resource actually does.
Resource Templates

Resource Templates allow clients to request resources whose content depends on parameters embedded in the URI. Define a template using the same @mcp.resource decorator, but include {parameter_name} placeholders in the URI string and add corresponding arguments to your function signature. Resource templates share most configuration options with regular resources (name, description, mime_type, tags, annotations), but add the ability to define URI parameters that map to function parameters. Resource templates generate a new resource for each unique set of parameters, which means that resources can be dynamically created on-demand. For example, if the resource template "user://profile/{name}" is registered, MCP clients could request "user://profile/ford" or "user://profile/marvin" to retrieve either of those two user profiles as resources, without having to register each resource individually.
Functions with *args are not supported as resource templates. However, unlike tools and prompts, resource templates do support **kwargs because the URI template defines specific parameter names that will be collected and passed as keyword arguments.

Here is a complete example that shows how to define two resource templates:
from fastmcp import FastMCP

mcp = FastMCP(name="DataServer")

# Template URI includes {city} placeholder
@mcp.resource("weather://{city}/current")
def get_weather(city: str) -> dict:
    """Provides weather information for a specific city."""
    # In a real implementation, this would call a weather API
    # Here we're using simplified logic for example purposes
    return {
        "city": city.capitalize(),
        "temperature": 22,
        "condition": "Sunny",
        "unit": "celsius"
    }

# Template with multiple parameters and annotations
@mcp.resource(
    "repos://{owner}/{repo}/info",
    annotations={
        "readOnlyHint": True,
        "idempotentHint": True
    }
)
def get_repo_info(owner: str, repo: str) -> dict:
    """Retrieves information about a GitHub repository."""
    # In a real implementation, this would call the GitHub API
    return {
        "owner": owner,
        "name": repo,
        "full_name": f"{owner}/{repo}",
        "stars": 120,
        "forks": 48
    }
With these two templates defined, clients can request a variety of resources:
weather://london/current → Returns weather for London
weather://paris/current → Returns weather for Paris
repos://jlowin/fastmcp/info → Returns info about the jlowin/fastmcp repository
repos://prefecthq/prefect/info → Returns info about the prefecthq/prefect repository
Wildcard Parameters

New in version: 2.2.4
Please note: FastMCP’s support for wildcard parameters is an extension of the Model Context Protocol standard, which otherwise follows RFC 6570. Since all template processing happens in the FastMCP server, this should not cause any compatibility issues with other MCP implementations.

Resource templates support wildcard parameters that can match multiple path segments. While standard parameters ({param}) only match a single path segment and don’t cross ”/” boundaries, wildcard parameters ({param*}) can capture multiple segments including slashes. Wildcards capture all subsequent path segments up until the defined part of the URI template (whether literal or another parameter). This allows you to have multiple wildcard parameters in a single URI template.
from fastmcp import FastMCP

mcp = FastMCP(name="DataServer")


# Standard parameter only matches one segment
@mcp.resource("files://{filename}")
def get_file(filename: str) -> str:
    """Retrieves a file by name."""
    # Will only match files://<single-segment>
    return f"File content for: {filename}"


# Wildcard parameter can match multiple segments
@mcp.resource("path://{filepath*}")
def get_path_content(filepath: str) -> str:
    """Retrieves content at a specific path."""
    # Can match path://docs/server/resources.mdx
    return f"Content at path: {filepath}"


# Mixing standard and wildcard parameters
@mcp.resource("repo://{owner}/{path*}/template.py")
def get_template_file(owner: str, path: str) -> dict:
    """Retrieves a file from a specific repository and path, but 
    only if the resource ends with `template.py`"""
    # Can match repo://jlowin/fastmcp/src/resources/template.py
    return {
        "owner": owner,
        "path": path + "/template.py",
        "content": f"File at {path}/template.py in {owner}'s repository"
    }
Wildcard parameters are useful when:
Working with file paths or hierarchical data
Creating APIs that need to capture variable-length path segments
Building URL-like patterns similar to REST APIs
Note that like regular parameters, each wildcard parameter must still be a named parameter in your function signature, and all required function parameters must appear in the URI template.
Default Values

New in version: 2.2.0 When creating resource templates, FastMCP enforces two rules for the relationship between URI template parameters and function parameters:
Required Function Parameters: All function parameters without default values (required parameters) must appear in the URI template.
URI Parameters: All URI template parameters must exist as function parameters.
However, function parameters with default values don’t need to be included in the URI template. When a client requests a resource, FastMCP will:
Extract parameter values from the URI for parameters included in the template
Use default values for any function parameters not in the URI template
This allows for flexible API designs. For example, a simple search template with optional parameters:
from fastmcp import FastMCP

mcp = FastMCP(name="DataServer")

@mcp.resource("search://{query}")
def search_resources(query: str, max_results: int = 10, include_archived: bool = False) -> dict:
    """Search for resources matching the query string."""
    # Only 'query' is required in the URI, the other parameters use their defaults
    results = perform_search(query, limit=max_results, archived=include_archived)
    return {
        "query": query,
        "max_results": max_results,
        "include_archived": include_archived,
        "results": results
    }
With this template, clients can request search://python and the function will be called with query="python", max_results=10, include_archived=False. MCP Developers can still call the underlying search_resources function directly with more specific parameters. You can also create multiple resource templates that provide different ways to access the same underlying data by manually applying decorators to a single function:
from fastmcp import FastMCP

mcp = FastMCP(name="DataServer")

# Define a user lookup function that can be accessed by different identifiers
def lookup_user(name: str | None = None, email: str | None = None) -> dict:
    """Look up a user by either name or email."""
    if email:
        return find_user_by_email(email)  # pseudocode
    elif name:
        return find_user_by_name(name)  # pseudocode
    else:
        return {"error": "No lookup parameters provided"}

# Manually apply multiple decorators to the same function
mcp.resource("users://email/{email}")(lookup_user)
mcp.resource("users://name/{name}")(lookup_user)
Now an LLM or client can retrieve user information in two different ways:
users://email/alice@example.com → Looks up user by email (with name=None)
users://name/Bob → Looks up user by name (with email=None)
This approach allows a single function to be registered with multiple URI patterns while keeping the implementation clean and straightforward. Templates provide a powerful way to expose parameterized data access points following REST-like principles.
Error Handling

New in version: 2.4.1 If your resource function encounters an error, you can raise a standard Python exception (ValueError, TypeError, FileNotFoundError, custom exceptions, etc.) or a FastMCP ResourceError. By default, all exceptions (including their details) are logged and converted into an MCP error response to be sent back to the client LLM. This helps the LLM understand failures and react appropriately. If you want to mask internal error details for security reasons, you can:
Use the mask_error_details=True parameter when creating your FastMCP instance:
mcp = FastMCP(name="SecureServer", mask_error_details=True)
Or use ResourceError to explicitly control what error information is sent to clients:
from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError

mcp = FastMCP(name="DataServer")

@mcp.resource("resource://safe-error")
def fail_with_details() -> str:
    """This resource provides detailed error information."""
    # ResourceError contents are always sent back to clients,
    # regardless of mask_error_details setting
    raise ResourceError("Unable to retrieve data: file not found")

@mcp.resource("resource://masked-error")
def fail_with_masked_details() -> str:
    """This resource masks internal error details when mask_error_details=True."""
    # This message would be masked if mask_error_details=True
    raise ValueError("Sensitive internal file path: /etc/secrets.conf")

@mcp.resource("data://{id}")
def get_data_by_id(id: str) -> dict:
    """Template resources also support the same error handling pattern."""
    if id == "secure":
        raise ValueError("Cannot access secure data")
    elif id == "missing":
        raise ResourceError("Data ID 'missing' not found in database")
    return {"id": id, "value": "data"}
When mask_error_details=True, only error messages from ResourceError will include details, other exceptions will be converted to a generic message.
Server Behavior

Duplicate Resources

New in version: 2.1.0 You can configure how the FastMCP server handles attempts to register multiple resources or templates with the same URI. Use the on_duplicate_resources setting during FastMCP initialization.
from fastmcp import FastMCP

mcp = FastMCP(
    name="ResourceServer",
    on_duplicate_resources="error" # Raise error on duplicates
)

@mcp.resource("data://config")
def get_config_v1(): return {"version": 1}

# This registration attempt will raise a ValueError because
# "data://config" is already registered and the behavior is "error".
# @mcp.resource("data://config")
# def get_config_v2(): return {"version": 2}
The duplicate behavior options are:
"warn" (default): Logs a warning, and the new resource/template replaces the old one.
"error": Raises a ValueError, preventing the duplicate registration.
"replace": Silently replaces the existing resource/template with the new one.
"ignore": Keeps the original resource/template and ignores the new registration attempt.


Prompts
Create reusable, parameterized prompt templates for MCP clients.

Prompts are reusable message templates that help LLMs generate structured, purposeful responses. FastMCP simplifies defining these templates, primarily using the @mcp.prompt decorator. Prompts provide parameterized message templates for LLMs. When a client requests a prompt:
FastMCP finds the corresponding prompt definition.
If it has parameters, they are validated against your function signature.
Your function executes with the validated inputs.
The generated message(s) are returned to the LLM to guide its response.
This allows you to define consistent, reusable templates that LLMs can use across different clients and contexts.
Prompts

The @prompt Decorator

The most common way to define a prompt is by decorating a Python function. The decorator uses the function name as the prompt’s identifier.
from fastmcp import FastMCP
from fastmcp.prompts.prompt import Message, PromptMessage, TextContent

mcp = FastMCP(name="PromptServer")

# Basic prompt returning a string (converted to user message automatically)
@mcp.prompt
def ask_about_topic(topic: str) -> str:
    """Generates a user message asking for an explanation of a topic."""
    return f"Can you please explain the concept of '{topic}'?"

# Prompt returning a specific message type
@mcp.prompt
def generate_code_request(language: str, task_description: str) -> PromptMessage:
    """Generates a user message requesting code generation."""
    content = f"Write a {language} function that performs the following task: {task_description}"
    return PromptMessage(role="user", content=TextContent(type="text", text=content))
Key Concepts:
Name: By default, the prompt name is taken from the function name.
Parameters: The function parameters define the inputs needed to generate the prompt.
Inferred Metadata: By default:
Prompt Name: Taken from the function name (ask_about_topic).
Prompt Description: Taken from the function’s docstring.
Functions with *args or **kwargs are not supported as prompts. This restriction exists because FastMCP needs to generate a complete parameter schema for the MCP protocol, which isn’t possible with variable argument lists.

Decorator Arguments

While FastMCP infers the name and description from your function, you can override these and add additional metadata using arguments to the @mcp.prompt decorator:
@mcp.prompt(
    name="analyze_data_request",          # Custom prompt name
    description="Creates a request to analyze data with specific parameters",  # Custom description
    tags={"analysis", "data"},            # Optional categorization tags
    meta={"version": "1.1", "author": "data-team"}  # Custom metadata
)
def data_analysis_prompt(
    data_uri: str = Field(description="The URI of the resource containing the data."),
    analysis_type: str = Field(default="summary", description="Type of analysis.")
) -> str:
    """This docstring is ignored when description is provided."""
    return f"Please perform a '{analysis_type}' analysis on the data found at {data_uri}."
@prompt Decorator Arguments

Sets the explicit prompt name exposed via MCP. If not provided, uses the function name

Provides the description exposed via MCP. If set, the function’s docstring is ignored for this purpose

A set of strings used to categorize the prompt. These can be used by the server and, in some cases, by clients to filter or group available prompts.

A boolean to enable or disable the prompt. See Disabling Prompts for more information
New in version: 2.11.0Optional meta information about the prompt. This data is passed through to the MCP client as the _meta field of the client-side prompt object and can be used for custom metadata, versioning, or other application-specific purposes.

Argument Types

New in version: 2.9.0 The MCP specification requires that all prompt arguments be passed as strings, but FastMCP allows you to use typed annotations for better developer experience. When you use complex types like list[int] or dict[str, str], FastMCP:
Automatically converts string arguments from MCP clients to the expected types
Generates helpful descriptions showing the exact JSON string format needed
Preserves direct usage - you can still call prompts with properly typed arguments
Since the MCP specification only allows string arguments, clients need to know what string format to use for complex types. FastMCP solves this by automatically enhancing the argument descriptions with JSON schema information, making it clear to both humans and LLMs how to format their arguments.
@mcp.prompt
def analyze_data(
    numbers: list[int],
    metadata: dict[str, str], 
    threshold: float
) -> str:
    """Analyze numerical data."""
    avg = sum(numbers) / len(numbers)
    return f"Average: {avg}, above threshold: {avg > threshold}"
MCP clients will call this prompt with string arguments:
{
  "numbers": "[1, 2, 3, 4, 5]",
  "metadata": "{\"source\": \"api\", \"version\": \"1.0\"}",
  "threshold": "2.5"
}
But you can still call it directly with proper types:
# This also works for direct calls
result = await prompt.render({
    "numbers": [1, 2, 3, 4, 5],
    "metadata": {"source": "api", "version": "1.0"}, 
    "threshold": 2.5
})
Keep your type annotations simple when using this feature. Complex nested types or custom classes may not convert reliably from JSON strings. The automatically generated schema descriptions are the only guidance users receive about the expected format.Good choices: list[int], dict[str, str], float, bool Avoid: Complex Pydantic models, deeply nested structures, custom classes

Return Values

FastMCP intelligently handles different return types from your prompt function:
str: Automatically converted to a single PromptMessage.
PromptMessage: Used directly as provided. (Note a more user-friendly Message constructor is available that can accept raw strings instead of TextContent objects.)
list[PromptMessage | str]: Used as a sequence of messages (a conversation).
Any: If the return type is not one of the above, the return value is attempted to be converted to a string and used as a PromptMessage.
from fastmcp.prompts.prompt import Message, PromptResult

@mcp.prompt
def roleplay_scenario(character: str, situation: str) -> PromptResult:
    """Sets up a roleplaying scenario with initial messages."""
    return [
        Message(f"Let's roleplay. You are {character}. The situation is: {situation}"),
        Message("Okay, I understand. I am ready. What happens next?", role="assistant")
    ]
Required vs. Optional Parameters

Parameters in your function signature are considered required unless they have a default value.
@mcp.prompt
def data_analysis_prompt(
    data_uri: str,                        # Required - no default value
    analysis_type: str = "summary",       # Optional - has default value
    include_charts: bool = False          # Optional - has default value
) -> str:
    """Creates a request to analyze data with specific parameters."""
    prompt = f"Please perform a '{analysis_type}' analysis on the data found at {data_uri}."
    if include_charts:
        prompt += " Include relevant charts and visualizations."
    return prompt
In this example, the client must provide data_uri. If analysis_type or include_charts are omitted, their default values will be used.
Disabling Prompts

New in version: 2.8.0 You can control the visibility and availability of prompts by enabling or disabling them. Disabled prompts will not appear in the list of available prompts, and attempting to call a disabled prompt will result in an “Unknown prompt” error. By default, all prompts are enabled. You can disable a prompt upon creation using the enabled parameter in the decorator:
@mcp.prompt(enabled=False)
def experimental_prompt():
    """This prompt is not ready for use."""
    return "This is an experimental prompt."
You can also toggle a prompt’s state programmatically after it has been created:
@mcp.prompt
def seasonal_prompt(): return "Happy Holidays!"

# Disable and re-enable the prompt
seasonal_prompt.disable()
seasonal_prompt.enable()
Async Prompts

FastMCP seamlessly supports both standard (def) and asynchronous (async def) functions as prompts.
# Synchronous prompt
@mcp.prompt
def simple_question(question: str) -> str:
    """Generates a simple question to ask the LLM."""
    return f"Question: {question}"

# Asynchronous prompt
@mcp.prompt
async def data_based_prompt(data_id: str) -> str:
    """Generates a prompt based on data that needs to be fetched."""
    # In a real scenario, you might fetch data from a database or API
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://api.example.com/data/{data_id}") as response:
            data = await response.json()
            return f"Analyze this data: {data['content']}"
Use async def when your prompt function performs I/O operations like network requests, database queries, file I/O, or external service calls.
Accessing MCP Context

New in version: 2.2.5 Prompts can access additional MCP information and features through the Context object. To access it, add a parameter to your prompt function with a type annotation of Context:
from fastmcp import FastMCP, Context

mcp = FastMCP(name="PromptServer")

@mcp.prompt
async def generate_report_request(report_type: str, ctx: Context) -> str:
    """Generates a request for a report."""
    return f"Please create a {report_type} report. Request ID: {ctx.request_id}"
For full documentation on the Context object and all its capabilities, see the Context documentation.
Notifications

New in version: 2.9.1 FastMCP automatically sends notifications/prompts/list_changed notifications to connected clients when prompts are added, enabled, or disabled. This allows clients to stay up-to-date with the current prompt set without manually polling for changes.
@mcp.prompt
def example_prompt() -> str:
    return "Hello!"

# These operations trigger notifications:
mcp.add_prompt(example_prompt)  # Sends prompts/list_changed notification
example_prompt.disable()        # Sends prompts/list_changed notification  
example_prompt.enable()         # Sends prompts/list_changed notification
Notifications are only sent when these operations occur within an active MCP request context (e.g., when called from within a tool or other MCP operation). Operations performed during server initialization do not trigger notifications. Clients can handle these notifications using a message handler to automatically refresh their prompt lists or update their interfaces.
Server Behavior

Duplicate Prompts

New in version: 2.1.0 You can configure how the FastMCP server handles attempts to register multiple prompts with the same name. Use the on_duplicate_prompts setting during FastMCP initialization.
from fastmcp import FastMCP

mcp = FastMCP(
    name="PromptServer",
    on_duplicate_prompts="error"  # Raise an error if a prompt name is duplicated
)

@mcp.prompt
def greeting(): return "Hello, how can I help you today?"

# This registration attempt will raise a ValueError because
# "greeting" is already registered and the behavior is "error".
# @mcp.prompt
# def greeting(): return "Hi there! What can I do for you?"
The duplicate behavior options are:
"warn" (default): Logs a warning, and the new prompt replaces the old one.
"error": Raises a ValueError, preventing the duplicate registration.
"replace": Silently replaces the existing prompt with the new one.
"ignore": Keeps the original prompt and ignores the new registration attempt.