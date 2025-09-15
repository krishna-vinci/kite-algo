# MCP Integration and Development Plan

This document outlines the integration strategy and development plan for incorporating a Multi-Client Platform (MCP) into the existing Kite API-based trading application.

## Phase 1: Analysis and Design

### 1.1: Integration Strategy and High-Level Architecture

The integration will follow a **service-based architecture**, where the MCP server operates as a distinct service within the main FastAPI application. This approach promotes modularity and separation of concerns.

- **`main.py` (Main Application)**: Will act as the primary entry point and will be responsible for user authentication and request routing. It will mount the MCP server as a sub-application.
- **`mcp_server.py` (MCP Server)**: Will be refactored into a dedicated FastMCP server. It will expose trading and data retrieval functionalities as MCP "tools" and will handle all interactions with the Kite API for multiple clients.
- **Communication Protocol**: The communication between the main application and the MCP server will be handled through **internal library calls**, leveraging FastAPI's dependency injection system. This is the most efficient and secure method as both components will run in the same process.

### 1.2: Refine the API Contract between the Main App and MCP Server

The API contract will be defined by the MCP tools exposed by `mcp_server.py`. These tools will be decorated with `@mcp.tool` and will have clearly defined input parameters and return types.

**Example MCP Tools:**

- `get_profile(client_id: str) -> dict`
- `get_holdings(client_id: str) -> list`
- `place_order(client_id: str, order_details: dict) -> dict`

### 1.3: Detail the Authentication Bridge Mechanism

The core of the integration is the secure handling of access tokens. The existing authentication mechanism will be preserved and extended to the MCP server.

1.  The user logs in through the existing flow in `main.py`.
2.  The access token is stored in a session, identified by a `kite_session_id` cookie.
3.  When a request is made to an MCP endpoint, a new dependency will be created to extract the `kite_session_id` from the request cookies.
4.  This dependency will then retrieve the corresponding access token from the database.
5.  The access token will be passed to the MCP tool, which will use it to initialize a `KiteConnect` instance for the specific client.

This ensures that the MCP server never stores or manages tokens directly, adhering to the project's critical constraint.

### 1.4: Outline the Data Flow for a Complete Trade Order

1.  **User Action**: The user initiates a trade from the frontend.
2.  **API Request**: The frontend sends a request to an endpoint in `main.py`.
3.  **Authentication**: The `get_kite` dependency in `broker_api/broker_api.py` validates the session and retrieves the access token.
4.  **Delegation**: The endpoint in `main.py` calls the appropriate MCP tool (e.g., `place_order`) and passes the access token and order details.
5.  **MCP Execution**: The MCP tool in `mcp_server.py` uses the access token to create a `KiteConnect` instance and places the order with the Kite API.
6.  **Response**: The response from the Kite API is returned through the MCP tool to the main application and then to the user.

## Phase 2: Development and Implementation

### 2.1: Set Up the Development Environment and Dependencies

- **Add `fastmcp` to `requirements.txt`**: The FastMCP library will be the primary new dependency.
- **Environment Variables**: No new environment variables are anticipated at this stage.

### 2.2: Refactor `mcp_server.py` into a FastMCP-compliant Service

- **Instantiate `FastMCP`**: Create an instance of the `FastMCP` class.
- **Convert Endpoints to Tools**: Convert the existing FastAPI endpoints into functions decorated with `@mcp.tool`.
- **Handle `KiteConnect` Initialization**: Each tool will accept an access token as a parameter and initialize a `KiteConnect` instance.

### 2.3: Modify `main.py` to Integrate the FastMCP Server

- **Mount the MCP Server**: Use `app.mount()` to integrate the FastMCP server as a sub-application.
- **Update Endpoints**: Modify the existing trading-related endpoints to call the new MCP tools instead of directly interacting with the `broker_api`.

### 2.4: Adapt `broker_api.py` to Delegate Calls to the MCP Server

- The functions in `broker_api.py` that directly interact with the Kite API for trading operations will be deprecated and eventually removed. The `get_kite` dependency will be repurposed to only handle the retrieval of the access token.

## Phase 3: Testing and Validation

### 3.1: Develop a Unit and Integration Testing Strategy

- **Unit Tests**:
    - Test the logic of each MCP tool in `mcp_server.py` with mock `KiteConnect` instances.
    - Test the authentication bridge to ensure tokens are passed correctly.
- **Integration Tests**:
    - Create end-to-end tests that simulate a user logging in, placing an order, and retrieving data through the integrated system.

## Phase 4: Deployment and Operations

### 4.1: Plan for Self-Hosting Deployment

- **`fastmcp.json`**: Create a `fastmcp.json` file to define the deployment configuration. This will specify the source, environment, and deployment settings, making the deployment process reproducible.
- **Dockerfile**: Update the existing `Dockerfile` to include the installation of `fastmcp` and to run the application using the `fastmcp run` command.

### 4.2: Identify Potential Challenges and Mitigation Strategies

- **Token Expiry**:
    - **Challenge**: Access tokens can expire.
    - **Mitigation**: Implement a mechanism within the `get_kite` dependency to check for token expiry and trigger a re-login if necessary.
- **Data Synchronization**:
    - **Challenge**: Ensuring data consistency across multiple clients.
    - **Mitigation**: The MCP server will be stateless. All state will be managed by the Kite API, ensuring that the data is always synchronized.
- **Performance Bottlenecks**:
    - **Challenge**: The MCP server could become a bottleneck.
    - **Mitigation**: The use of asynchronous tools (`async def`) in the MCP server will ensure that it can handle multiple requests concurrently without blocking.
- **Error Propagation**:
    - **Challenge**: Errors from the MCP server need to be propagated to the main application and the user.
    - **Mitigation**: Use FastAPI's exception handling mechanisms to catch errors from the MCP tools and return appropriate HTTP responses.