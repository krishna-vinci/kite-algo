FastAPI 🤝 FastMCP
Integrate FastMCP with FastAPI applications

New in 2.11: FastMCP is introducing a next-generation OpenAPI parser. The new parser has greatly improved performance and compatibility, and is also easier to maintain. To enable it, set the environment variable FASTMCP_EXPERIMENTAL_ENABLE_NEW_OPENAPI_PARSER=true.The new parser is largely API-compatible with the existing implementation and will become the default in a future version. We encourage all users to test it and report any issues before it becomes the default.

FastMCP provides two powerful ways to integrate with FastAPI applications:
- Convert existing API endpoints into MCP tools
- Add MCP functionality to your web application
Generating MCP servers from OpenAPI is a great way to get started with FastMCP, but in practice LLMs achieve significantly better performance with well-designed and curated MCP servers than with auto-converted OpenAPI servers. This is especially true for complex APIs with many endpoints and parameters.We recommend using the FastAPI integration for bootstrapping and prototyping, not for mirroring your API to LLM clients. See the post Stop Converting Your REST APIs to MCP for more details.
FastMCP does not include FastAPI as a dependency; you must install it separately to use this integration.

Example FastAPI Application

Throughout this guide, we’ll use this e-commerce API as our example (click the Copy button to copy it for use with other code blocks):
# Copy this FastAPI server into other code blocks in this guide

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Models
class Product(BaseModel):
    name: str
    price: float
    category: str
    description: str | None = None

class ProductResponse(BaseModel):
    id: int
    name: str
    price: float
    category: str
    description: str | None = None

# Create FastAPI app
app = FastAPI(title="E-commerce API", version="1.0.0")

# In-memory database
products_db = {
    1: ProductResponse(
        id=1, name="Laptop", price=999.99, category="Electronics"
    ),
    2: ProductResponse(
        id=2, name="Mouse", price=29.99, category="Electronics"
    ),
    3: ProductResponse(
        id=3, name="Desk Chair", price=299.99, category="Furniture"
    ),
}
next_id = 4

@app.get("/products", response_model=list[ProductResponse])
def list_products(
    category: str | None = None,
    max_price: float | None = None,
) -> list[ProductResponse]:
    """List all products with optional filtering."""
    products = list(products_db.values())
    if category:
        products = [p for p in products if p.category == category]
    if max_price:
        products = [p for p in products if p.price <= max_price]
    return products

@app.get("/products/{product_id}", response_model=ProductResponse)
def get_product(product_id: int):
    """Get a specific product by ID."""
    if product_id not in products_db:
        raise HTTPException(status_code=404, detail="Product not found")
    return products_db[product_id]

@app.post("/products", response_model=ProductResponse)
def create_product(product: Product):
    """Create a new product."""
    global next_id
    product_response = ProductResponse(id=next_id, **product.model_dump())
    products_db[next_id] = product_response
    next_id += 1
    return product_response

@app.put("/products/{product_id}", response_model=ProductResponse)
def update_product(product_id: int, product: Product):
    """Update an existing product."""
    if product_id not in products_db:
        raise HTTPException(status_code=404, detail="Product not found")
    products_db[product_id] = ProductResponse(
        id=product_id,
        **product.model_dump(),
    )
    return products_db[product_id]

@app.delete("/products/{product_id}")
def delete_product(product_id: int):
    """Delete a product."""
    if product_id not in products_db:
        raise HTTPException(status_code=404, detail="Product not found")
    del products_db[product_id]
    return {"message": "Product deleted"}
All subsequent code examples in this guide assume you have the above FastAPI application code already defined. Each example builds upon this base application, app.

Generating an MCP Server

New in version: 2.0.0 One of the most common ways to bootstrap an MCP server is to generate it from an existing FastAPI application. FastMCP will expose your FastAPI endpoints as MCP components (tools, by default) in order to expose your API to LLM clients.
Basic Conversion

Convert the FastAPI app to an MCP server with a single line:
# Assumes the FastAPI app from above is already defined
from fastmcp import FastMCP

# Convert to MCP server
mcp = FastMCP.from_fastapi(app=app)

if __name__ == "__main__":
    mcp.run()
Adding Components

Your converted MCP server is a full FastMCP instance, meaning you can add new tools, resources, and other components to it just like you would with any other FastMCP instance.
# Assumes the FastAPI app from above is already defined
from fastmcp import FastMCP

# Convert to MCP server
mcp = FastMCP.from_fastapi(app=app)

# Add a new tool
@mcp.tool
def get_product(product_id: int) -> ProductResponse:
    """Get a product by ID."""
    return products_db[product_id]

# Run the MCP server
if __name__ == "__main__":
    mcp.run()
Interacting with the MCP Server

Once you’ve converted your FastAPI app to an MCP server, you can interact with it using the FastMCP client to test functionality before deploying it to an LLM-based application.
# Assumes the FastAPI app from above is already defined
from fastmcp import FastMCP
from fastmcp.client import Client
import asyncio

# Convert to MCP server
mcp = FastMCP.from_fastapi(app=app)

async def demo():
    async with Client(mcp) as client:
        # List available tools
        tools = await client.list_tools()
        print(f"Available tools: {[t.name for t in tools]}")
        
        # Create a product
        result = await client.call_tool(
            "create_product_products_post",
            {
                "name": "Wireless Keyboard",
                "price": 79.99,
                "category": "Electronics",
                "description": "Bluetooth mechanical keyboard"
            }
        )
        print(f"Created product: {result.data}")
        
        # List electronics under $100
        result = await client.call_tool(
            "list_products_products_get",
            {"category": "Electronics", "max_price": 100}
        )
        print(f"Affordable electronics: {result.data}")

if __name__ == "__main__":
    asyncio.run(demo())
Custom Route Mapping

Because FastMCP’s FastAPI integration is based on its OpenAPI integration, you can customize how endpoints are converted to MCP components in exactly the same way. For example, here we use a RouteMap to map all GET requests to MCP resources, and all POST/PUT/DELETE requests to MCP tools:
# Assumes the FastAPI app from above is already defined
from fastmcp import FastMCP
from fastmcp.server.openapi import RouteMap, MCPType

# If using experimental parser, import from experimental module:
# from fastmcp.experimental.server.openapi import RouteMap, MCPType

# Custom mapping rules
mcp = FastMCP.from_fastapi(
    app=app,
    route_maps=[
        # GET with path params → ResourceTemplates
        RouteMap(
            methods=["GET"], 
            pattern=r".*\{.*\}.*", 
            mcp_type=MCPType.RESOURCE_TEMPLATE
        ),
        # Other GETs → Resources
        RouteMap(
            methods=["GET"], 
            pattern=r".*", 
            mcp_type=MCPType.RESOURCE
        ),
        # POST/PUT/DELETE → Tools (default)
    ],
)

# Now:
# - GET /products → Resource
# - GET /products/{id} → ResourceTemplate
# - POST/PUT/DELETE → Tools
Authentication and Headers

You can configure headers and other client options via the httpx_client_kwargs parameter. For example, to add authentication to your FastAPI app, you can pass a headers dictionary to the httpx_client_kwargs parameter:
# Assumes the FastAPI app from above is already defined
from fastmcp import FastMCP

# Add authentication to your FastAPI app
from fastapi import Depends, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

security = HTTPBearer()

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials.credentials != "secret-token":
        raise HTTPException(status_code=401, detail="Invalid authentication")
    return credentials.credentials

# Add a protected endpoint
@app.get("/admin/stats", dependencies=[Depends(verify_token)])
def get_admin_stats():
    return {
        "total_products": len(products_db),
        "categories": list(set(p.category for p in products_db.values()))
    }

# Create MCP server with authentication headers
mcp = FastMCP.from_fastapi(
    app=app,
    httpx_client_kwargs={
        "headers": {
            "Authorization": "Bearer secret-token",
        }
    }
)
Mounting an MCP Server

New in version: 2.3.1 In addition to generating servers, FastMCP can facilitate adding MCP servers to your existing FastAPI application. You can do this by mounting the MCP ASGI application.
Basic Mounting

To mount an MCP server, you can use the http_app method on your FastMCP instance. This will return an ASGI application that can be mounted to your FastAPI application.
from fastmcp import FastMCP
from fastapi import FastAPI

# Create MCP server
mcp = FastMCP("Analytics Tools")

@mcp.tool
def analyze_pricing(category: str) -> dict:
    """Analyze pricing for a category."""
    products = [p for p in products_db.values() if p.category == category]
    if not products:
        return {"error": f"No products in {category}"}
    
    prices = [p.price for p in products]
    return {
        "category": category,
        "avg_price": round(sum(prices) / len(prices), 2),
        "min": min(prices),
        "max": max(prices),
    }

# Create ASGI app from MCP server
mcp_app = mcp.http_app(path='/mcp')

# Key: Pass lifespan to FastAPI
app = FastAPI(title="E-commerce API", lifespan=mcp_app.lifespan)

# Mount the MCP server
app.mount("/analytics", mcp_app)

# Now: API at /products/*, MCP at /analytics/mcp/
Offering an LLM-Friendly API

A common pattern is to generate an MCP server from your FastAPI app and mount it back into the same application. This provides an LLM-optimized interface alongside your regular API:
# Assumes the FastAPI app from above is already defined
from fastmcp import FastMCP
from fastapi import FastAPI

# 1. Generate MCP server from your API
mcp = FastMCP.from_fastapi(app=app, name="E-commerce MCP")

# 2. Create the MCP's ASGI app
mcp_app = mcp.http_app(path='/mcp')

# 3. Mount it back into your FastAPI app
app = FastAPI(title="E-commerce API", lifespan=mcp_app.lifespan)
app.mount("/llm", mcp_app)

# Now you have:
# - Regular API: http://localhost:8000/products
# - LLM-friendly MCP: http://localhost:8000/llm/mcp/
# Both served from the same FastAPI application!
This approach lets you maintain a single codebase while offering both traditional REST endpoints and MCP-compatible endpoints for LLM clients.
Key Considerations

Operation IDs

FastAPI operation IDs become MCP component names. Always specify meaningful operation IDs:
# Good - explicit operation_id
@app.get("/users/{user_id}", operation_id="get_user_by_id")
def get_user(user_id: int):
    return {"id": user_id}

# Less ideal - auto-generated name
@app.get("/users/{user_id}")
def get_user(user_id: int):
    return {"id": user_id}
Lifespan Management

When mounting MCP servers, always pass the lifespan context:
# Correct - lifespan passed
mcp_app = mcp.http_app(path='/mcp')
app = FastAPI(lifespan=mcp_app.lifespan)
app.mount("/mcp", mcp_app)

# Incorrect - missing lifespan
app = FastAPI()
app.mount("/mcp", mcp.http_app())  # Session manager won't initialize
Combining Lifespans

If your FastAPI app already has a lifespan (for database connections, startup tasks, etc.), you can’t simply replace it with the MCP lifespan. Instead, you need to create a new lifespan function that manages both contexts. This ensures that both your app’s initialization logic and the MCP server’s session manager run properly:
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastmcp import FastMCP

# Your existing lifespan
@asynccontextmanager
async def app_lifespan(app: FastAPI):
    # Startup
    print("Starting up the app...")
    # Initialize database, cache, etc.
    yield
    # Shutdown
    print("Shutting down the app...")

# Create MCP server
mcp = FastMCP("Tools")
mcp_app = mcp.http_app(path='/mcp')

# Combine both lifespans
@asynccontextmanager
async def combined_lifespan(app: FastAPI):
    # Run both lifespans
    async with app_lifespan(app):
        async with mcp_app.lifespan(app):
            yield

# Use the combined lifespan
app = FastAPI(lifespan=combined_lifespan)
app.mount("/mcp", mcp_app)
This pattern ensures both your app’s initialization logic and the MCP server’s session manager are properly managed. The key is using nested async with statements - the inner context (MCP) will be initialized after the outer context (your app), and cleaned up before it. This maintains the correct initialization and cleanup order for all your resources.
Performance Tips

Use in-memory transport for testing - Pass MCP servers directly to clients
Design purpose-built MCP tools - Better than auto-converting complex APIs
Keep tool parameters simple - LLMs perform better with focused interfaces
For more details on configuration options, see the OpenAPI Integration guide.



OpenAPI 🤝 FastMCP
Generate MCP servers from any OpenAPI specification

New in version: 2.0.0
New in 2.11: FastMCP is introducing a next-generation OpenAPI parser. The new parser has greatly improved performance and compatibility, and is also easier to maintain. To enable it, set the environment variable FASTMCP_EXPERIMENTAL_ENABLE_NEW_OPENAPI_PARSER=true.The new parser is largely API-compatible with the existing implementation and will become the default in a future version. We encourage all users to test it and report any issues before it becomes the default.

FastMCP can automatically generate an MCP server from any OpenAPI specification, allowing AI models to interact with existing APIs through the MCP protocol. Instead of manually creating tools and resources, you provide an OpenAPI spec and FastMCP intelligently converts API endpoints into the appropriate MCP components.
Generating MCP servers from OpenAPI is a great way to get started with FastMCP, but in practice LLMs achieve significantly better performance with well-designed and curated MCP servers than with auto-converted OpenAPI servers. This is especially true for complex APIs with many endpoints and parameters.We recommend using the FastAPI integration for bootstrapping and prototyping, not for mirroring your API to LLM clients. See the post Stop Converting Your REST APIs to MCP for more details.
Create a Server

To convert an OpenAPI specification to an MCP server, use the FastMCP.from_openapi() class method:
import httpx
from fastmcp import FastMCP

# Create an HTTP client for your API
client = httpx.AsyncClient(base_url="https://api.example.com")

# Load your OpenAPI spec 
openapi_spec = httpx.get("https://api.example.com/openapi.json").json()

# Create the MCP server
mcp = FastMCP.from_openapi(
    openapi_spec=openapi_spec,
    client=client,
    name="My API Server"
)

if __name__ == "__main__":
    mcp.run()
Authentication

If your API requires authentication, configure it on the HTTP client:
import httpx
from fastmcp import FastMCP

# Bearer token authentication
api_client = httpx.AsyncClient(
    base_url="https://api.example.com",
    headers={"Authorization": "Bearer YOUR_TOKEN"}
)

# Create MCP server with authenticated client
mcp = FastMCP.from_openapi(
    openapi_spec=spec, 
    client=api_client,
    timeout=30.0  # 30 second timeout for all requests
)
Route Mapping

By default, FastMCP converts every endpoint in your OpenAPI specification into an MCP Tool. This provides a simple, predictable starting point that ensures all your API’s functionality is immediately available to the vast majority of LLM clients which only support MCP tools. While this is a pragmatic default for maximum compatibility, you can easily customize this behavior. Internally, FastMCP uses an ordered list of RouteMap objects to determine how to map OpenAPI routes to various MCP component types. Each RouteMap specifies a combination of methods, patterns, and tags, as well as a corresponding MCP component type. Each OpenAPI route is checked against each RouteMap in order, and the first one that matches every criteria is used to determine its converted MCP type. A special type, EXCLUDE, can be used to exclude routes from the MCP server entirely.
Methods: HTTP methods to match (e.g. ["GET", "POST"] or "*" for all)
Pattern: Regex pattern to match the route path (e.g. r"^/users/.*" or r".*" for all)
Tags: A set of OpenAPI tags that must all be present. An empty set ({}) means no tag filtering, so the route matches regardless of its tags.
MCP type: What MCP component type to create (TOOL, RESOURCE, RESOURCE_TEMPLATE, or EXCLUDE)
MCP tags: A set of custom tags to add to components created from matching routes
Here is FastMCP’s default rule:
from fastmcp.server.openapi import RouteMap, MCPType

DEFAULT_ROUTE_MAPPINGS = [
    # All routes become tools
    RouteMap(mcp_type=MCPType.TOOL),
]
Experimental Parser: If you’re using the new parser (enabled via FASTMCP_EXPERIMENTAL_ENABLE_NEW_OPENAPI_PARSER=true), import from the experimental module instead:
from fastmcp.experimental.server.openapi import RouteMap, MCPType
The API is identical, but the implementation provides better performance and serverless compatibility.
Custom Route Maps

When creating your FastMCP server, you can customize routing behavior by providing your own list of RouteMap objects. Your custom maps are processed before the default route maps, and routes will be assigned to the first matching custom map. For example, prior to FastMCP 2.8.0, GET requests were automatically mapped to Resource and ResourceTemplate components based on whether they had path parameters. (This was changed solely for client compatibility reasons.) You can restore this behavior by providing custom route maps:
from fastmcp import FastMCP
from fastmcp.server.openapi import RouteMap, MCPType

# Restore pre-2.8.0 semantic mapping
semantic_maps = [
    # GET requests with path parameters become ResourceTemplates
    RouteMap(methods=["GET"], pattern=r".*\{.*\}.*", mcp_type=MCPType.RESOURCE_TEMPLATE),
    # All other GET requests become Resources
    RouteMap(methods=["GET"], pattern=r".*", mcp_type=MCPType.RESOURCE),
]

mcp = FastMCP.from_openapi(
    openapi_spec=spec,
    client=client,
    route_maps=semantic_maps,
)
With these maps, GET requests are handled semantically, and all other methods (POST, PUT, etc.) will fall through to the default rule and become Tools. Here is a more complete example that uses custom route maps to convert all GET endpoints under /analytics/ to tools while excluding all admin endpoints and all routes tagged “internal”. All other routes will be handled by the default rules:
from fastmcp import FastMCP
from fastmcp.server.openapi import RouteMap, MCPType

mcp = FastMCP.from_openapi(
    openapi_spec=spec,
    client=client,
    route_maps=[
        # Analytics `GET` endpoints are tools
        RouteMap(
            methods=["GET"], 
            pattern=r"^/analytics/.*", 
            mcp_type=MCPType.TOOL,
        ),

        # Exclude all admin endpoints
        RouteMap(
            pattern=r"^/admin/.*", 
            mcp_type=MCPType.EXCLUDE,
        ),

        # Exclude all routes tagged "internal"
        RouteMap(
            tags={"internal"},
            mcp_type=MCPType.EXCLUDE,
        ),
    ],
)
The default route maps are always applied after your custom maps, so you do not have to create route maps for every possible route.

Excluding Routes

To exclude routes from the MCP server, use a route map to assign them to MCPType.EXCLUDE. You can use this to remove sensitive or internal routes by targeting them specifically:
from fastmcp import FastMCP
from fastmcp.server.openapi import RouteMap, MCPType

mcp = FastMCP.from_openapi(
    openapi_spec=spec,
    client=client,
    route_maps=[
        RouteMap(pattern=r"^/admin/.*", mcp_type=MCPType.EXCLUDE),
        RouteMap(tags={"internal"}, mcp_type=MCPType.EXCLUDE),
    ],
)
Or you can use a catch-all rule to exclude everything that your maps don’t handle explicitly:
from fastmcp import FastMCP
from fastmcp.server.openapi import RouteMap, MCPType

mcp = FastMCP.from_openapi(
    openapi_spec=spec,
    client=client,
    route_maps=[
        # custom mapping logic goes here
        # ... your specific route maps ...
        # exclude all remaining routes
        RouteMap(mcp_type=MCPType.EXCLUDE),
    ],
)
Using a catch-all exclusion rule will prevent the default route mappings from being applied, since it will match every remaining route. This is useful if you want to explicitly allow-list certain routes.

Advanced Route Mapping

New in version: 2.5.0 For advanced use cases that require more complex logic, you can provide a route_map_fn callable. After the route map logic is applied, this function is called on each matched route and its assigned MCP component type. It can optionally return a different component type to override the mapped assignment. If it returns None, the assigned type is used. In addition to more precise targeting of methods, patterns, and tags, this function can access any additional OpenAPI metadata about the route.
The route_map_fn is called on all routes, even those that matched MCPType.EXCLUDE in your custom maps. This gives you an opportunity to customize the mapping or even override an exclusion.

from fastmcp import FastMCP
from fastmcp.server.openapi import RouteMap, MCPType, HTTPRoute

def custom_route_mapper(route: HTTPRoute, mcp_type: MCPType) -> MCPType | None:
    """Advanced route type mapping."""
    # Convert all admin routes to tools regardless of HTTP method
    if "/admin/" in route.path:
        return MCPType.TOOL

    elif "internal" in route.tags:
        return MCPType.EXCLUDE
    
    # Convert user detail routes to templates even if they're POST
    elif route.path.startswith("/users/") and route.method == "POST":
        return MCPType.RESOURCE_TEMPLATE
    
    # Use defaults for all other routes
    return None

mcp = FastMCP.from_openapi(
    openapi_spec=spec,
    client=client,
    route_map_fn=custom_route_mapper,
)
Customization

Component Names

New in version: 2.5.0 FastMCP automatically generates names for MCP components based on the OpenAPI specification. By default, it uses the operationId from your OpenAPI spec, up to the first double underscore (__). All component names are automatically:
Slugified: Spaces and special characters are converted to underscores or removed
Truncated: Limited to 56 characters maximum to ensure compatibility
Unique: If multiple components have the same name, a number is automatically appended to make them unique
For more control over component names, you can provide an mcp_names dictionary that maps operationId values to your desired names. The operationId must be exactly as it appears in the OpenAPI spec. The provided name will always be slugified and truncated.
mcp = FastMCP.from_openapi(
    openapi_spec=spec,
    client=client,
    mcp_names={
        "list_users__with_pagination": "user_list",
        "create_user__admin_required": "create_user", 
        "get_user_details__admin_required": "user_detail",
    }
)
Any operationId not found in mcp_names will use the default strategy (operationId up to the first __).
Tags

New in version: 2.8.0 FastMCP provides several ways to add tags to your MCP components, allowing you to categorize and organize them for better discoverability and filtering. Tags are combined from multiple sources to create the final set of tags on each component.
RouteMap Tags

You can add custom tags to components created from specific routes using the mcp_tags parameter in RouteMap. These tags will be applied to all components created from routes that match that particular route map.
from fastmcp.server.openapi import RouteMap, MCPType

mcp = FastMCP.from_openapi(
    openapi_spec=spec,
    client=client,
    route_maps=[
        # Add custom tags to all POST endpoints
        RouteMap(
            methods=["POST"],
            pattern=r".*",
            mcp_type=MCPType.TOOL,
            mcp_tags={"write-operation", "api-mutation"}
        ),
        
        # Add different tags to detail view endpoints
        RouteMap(
            methods=["GET"],
            pattern=r".*\{.*\}.*",
            mcp_type=MCPType.RESOURCE_TEMPLATE,
            mcp_tags={"detail-view", "parameterized"}
        ),
        
        # Add tags to list endpoints
        RouteMap(
            methods=["GET"],
            pattern=r".*",
            mcp_type=MCPType.RESOURCE,
            mcp_tags={"list-data", "collection"}
        ),
    ],
)
Global Tags

You can add tags to all components by providing a tags parameter when creating your MCP server. These global tags will be applied to every component created from your OpenAPI specification.
mcp = FastMCP.from_openapi(
    openapi_spec=spec,
    client=client,
    tags={"api-v2", "production", "external"}
)
OpenAPI Tags in Client Meta

FastMCP automatically includes OpenAPI tags from your specification in the component’s metadata. These tags are available to MCP clients through the _meta._fastmcp.tags field, allowing clients to filter and organize components based on the original OpenAPI tagging:
{
  "paths": {
    "/users": {
      "get": {
        "tags": ["users", "public"],
        "operationId": "list_users",
        "summary": "List all users"
      }
    }
  }
}
This makes it easy for clients to understand and organize API endpoints based on their original OpenAPI categorization.
Advanced Customization

New in version: 2.5.0 By default, FastMCP creates MCP components using a variety of metadata from the OpenAPI spec, such as incorporating the OpenAPI description into the MCP component description. At times you may want to modify those MCP components in a variety of ways, such as adding LLM-specific instructions or tags. For fine-grained customization, you can provide a mcp_component_fn when creating the MCP server. After each MCP component has been created, this function is called on it and has the opportunity to modify it in-place.
Your mcp_component_fn is expected to modify the component in-place, not to return a new component. The result of the function is ignored.

from fastmcp.server.openapi import (
    HTTPRoute, 
    OpenAPITool, 
    OpenAPIResource, 
    OpenAPIResourceTemplate,
)

# If using experimental parser, import from experimental module:
# from fastmcp.experimental.server.openapi import (
#     HTTPRoute,
#     OpenAPITool,
#     OpenAPIResource,
#     OpenAPIResourceTemplate,
# )

def customize_components(
    route: HTTPRoute, 
    component: OpenAPITool | OpenAPIResource | OpenAPIResourceTemplate,
) -> None:
    # Add custom tags to all components
    component.tags.add("openapi")
    
    # Customize based on component type
    if isinstance(component, OpenAPITool):
        component.description = f"🔧 {component.description} (via API)"
    
    if isinstance(component, OpenAPIResource):
        component.description = f"📊 {component.description}"
        component.tags.add("data")

mcp = FastMCP.from_openapi(
    openapi_spec=spec,
    client=client,
    mcp_component_fn=customize_components,
)
Request Parameter Handling

FastMCP intelligently handles different types of parameters in OpenAPI requests:
Query Parameters

By default, FastMCP only includes query parameters that have non-empty values. Parameters with None values or empty strings are automatically filtered out.
# When calling this tool...
await client.call_tool("search_products", {
    "category": "electronics",  # ✅ Included
    "min_price": 100,           # ✅ Included  
    "max_price": None,          # ❌ Excluded
    "brand": "",                # ❌ Excluded
})

# The HTTP request will be: GET /products?category=electronics&min_price=100
Path Parameters

Path parameters are typically required by REST APIs. FastMCP:
Filters out None values
Validates that all required path parameters are provided
Raises clear errors for missing required parameters
# ✅ This works
await client.call_tool("get_user", {"user_id": 123})

# ❌ This raises: "Missing required path parameters: {'user_id'}"
await client.call_tool("get_user", {"user_id": None})
Array Parameters

FastMCP handles array parameters according to OpenAPI specifications:
Query arrays: Serialized based on the explode parameter (default: True)
Path arrays: Serialized as comma-separated values (OpenAPI ‘simple’ style)
# Query array with explode=true (default)
# ?tags=red&tags=blue&tags=green

# Query array with explode=false  
# ?tags=red,blue,green

# Path array (always comma-separated)
# /items/red,blue,green
Headers

Header parameters are automatically converted to strings and included in the HTTP request.