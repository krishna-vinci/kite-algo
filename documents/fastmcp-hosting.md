MCP JSON Configuration 🤝 FastMCP
Generate standard MCP configuration files for any compatible client

New in version: 2.10.3 FastMCP can generate standard MCP JSON configuration files that work with any MCP-compatible client including Claude Desktop, VS Code, Cursor, and other applications that support the Model Context Protocol. The MCP JSON configuration format is an emergent standard that has developed across the MCP ecosystem. This format defines how MCP clients should configure and launch MCP servers, providing a consistent way to specify server commands, arguments, and environment variables.
Configuration Structure

The standard uses a mcpServers object where each key represents a server name and the value contains the server’s configuration:
{
  "mcpServers": {
    "server-name": {
      "command": "executable",
      "args": ["arg1", "arg2"],
      "env": {
        "VAR": "value"
      }
    }
  }
}
Server Configuration Fields

command (required)

The executable command to run the MCP server. This should be an absolute path or a command available in the system PATH.
{
  "command": "python"
}
args (optional)

An array of command-line arguments passed to the server executable. Arguments are passed in order.
{
  "args": ["server.py", "--verbose", "--port", "8080"]
}
env (optional)

An object containing environment variables to set when launching the server. All values must be strings.
{
  "env": {
    "API_KEY": "secret-key",
    "DEBUG": "true",
    "PORT": "8080"
  }
}
Client Adoption

This format is widely adopted across the MCP ecosystem:
Claude Desktop: Uses ~/.claude/claude_desktop_config.json
Cursor: Uses ~/.cursor/mcp.json
VS Code: Uses workspace .vscode/mcp.json
Other clients: Many MCP-compatible applications follow this standard
Overview

The fastmcp install mcp-json command generates configuration in the standard mcpServers format used across the MCP ecosystem. This is useful when:
Working with unsupported clients - Any MCP client not directly integrated with FastMCP
CI/CD environments - Automated configuration generation for deployments
Configuration sharing - Easy distribution of server setups to team members
Custom tooling - Integration with your own MCP management tools
Manual setup - When you prefer to manually configure your MCP client
Basic Usage

Generate configuration and output to stdout (useful for piping):
fastmcp install mcp-json server.py
This outputs the server configuration JSON with the server name as the root key:
{
  "My Server": {
    "command": "uv",
    "args": [
      "run",
      "--with",
      "fastmcp", 
      "fastmcp",
      "run",
      "/absolute/path/to/server.py"
    ]
  }
}
To use this in a client configuration file, add it to the mcpServers object in your client’s configuration:
{
  "mcpServers": {
    "My Server": {
      "command": "uv",
      "args": [
        "run",
        "--with",
        "fastmcp", 
        "fastmcp",
        "run",
        "/absolute/path/to/server.py"
      ]
    }
  }
}
When using --python, --project, or --with-requirements, the generated configuration will include these options in the uv run command, ensuring your server runs with the correct Python version and dependencies.

Different MCP clients may have specific configuration requirements or formatting needs. Always consult your client’s documentation to ensure proper integration.

Configuration Options

Server Naming

# Use server's built-in name (from FastMCP constructor)
fastmcp install mcp-json server.py

# Override with custom name
fastmcp install mcp-json server.py --name "Custom Server Name"
Dependencies

Add Python packages your server needs:
# Single package
fastmcp install mcp-json server.py --with pandas

# Multiple packages  
fastmcp install mcp-json server.py --with pandas --with requests --with httpx

# Editable local package
fastmcp install mcp-json server.py --with-editable ./my-package

# From requirements file
fastmcp install mcp-json server.py --with-requirements requirements.txt
You can also use a fastmcp.json configuration file (recommended):
{
  "$schema": "https://gofastmcp.com/public/schemas/fastmcp.json/v1.json",
  "source": {
    "path": "server.py",
    "entrypoint": "mcp"
  },
  "environment": {
    "dependencies": ["pandas", "matplotlib", "seaborn"]
  }
}
Then simply install with:
fastmcp install mcp-json fastmcp.json
Environment Variables

# Individual environment variables
fastmcp install mcp-json server.py \
  --env API_KEY=your-secret-key \
  --env DEBUG=true

# Load from .env file
fastmcp install mcp-json server.py --env-file .env
Python Version and Project Directory

Specify Python version or run within a specific project:
# Use specific Python version
fastmcp install mcp-json server.py --python 3.11

# Run within a project directory
fastmcp install mcp-json server.py --project /path/to/project
Server Object Selection

Use the same file.py:object notation as other FastMCP commands:
# Auto-detects server object (looks for 'mcp', 'server', or 'app')
fastmcp install mcp-json server.py

# Explicit server object
fastmcp install mcp-json server.py:my_custom_server
Clipboard Integration

Copy configuration directly to your clipboard for easy pasting:
fastmcp install mcp-json server.py --copy
The --copy flag requires the pyperclip Python package. If not installed, you’ll see an error message with installation instructions.

Usage Examples

Basic Server

fastmcp install mcp-json dice_server.py
Output:
{
  "Dice Server": {
    "command": "uv",
    "args": [
      "run",
      "--with",
      "fastmcp",
      "fastmcp", 
      "run",
      "/home/user/dice_server.py"
    ]
  }
}
Production Server with Dependencies

fastmcp install mcp-json api_server.py \
  --name "Production API Server" \
  --with requests \
  --with python-dotenv \
  --env API_BASE_URL=https://api.example.com \
  --env TIMEOUT=30
Advanced Configuration

fastmcp install mcp-json ml_server.py \
  --name "ML Analysis Server" \
  --python 3.11 \
  --with-requirements requirements.txt \
  --project /home/user/ml-project \
  --env GPU_DEVICE=0
Output:
{
  "Production API Server": {
    "command": "uv",
    "args": [
      "run",
      "--with",
      "fastmcp",
      "--with",
      "python-dotenv", 
      "--with",
      "requests",
      "fastmcp",
      "run", 
      "/home/user/api_server.py"
    ],
    "env": {
      "API_BASE_URL": "https://api.example.com",
      "TIMEOUT": "30"
    }
  }
}
The advanced configuration example generates:
{
  "ML Analysis Server": {
    "command": "uv",
    "args": [
      "run",
      "--python",
      "3.11",
      "--project",
      "/home/user/ml-project",
      "--with",
      "fastmcp",
      "--with-requirements",
      "requirements.txt",
      "fastmcp",
      "run",
      "/home/user/ml_server.py"
    ],
    "env": {
      "GPU_DEVICE": "0"
    }
  }
}
Pipeline Usage

Save configuration to file:
fastmcp install mcp-json server.py > mcp-config.json
Use in shell scripts:
#!/bin/bash
CONFIG=$(fastmcp install mcp-json server.py --name "CI Server")
echo "$CONFIG" | jq '."CI Server".command'
# Output: "uv"
Integration with MCP Clients

The generated configuration works with any MCP-compatible application:
Claude Desktop

Copy the mcpServers object into ~/.claude/claude_desktop_config.json
Cursor

Add to ~/.cursor/mcp.json
VS Code

Add to your workspace’s .vscode/mcp.json file
Custom Applications

Use the JSON configuration with any application that supports the MCP protocol
Configuration Format

The generated configuration outputs a server object with the server name as the root key:
{
  "<server-name>": {
    "command": "<executable>",
    "args": ["<arg1>", "<arg2>", "..."],
    "env": {
      "<ENV_VAR>": "<value>"
    }
  }
}
To use this in an MCP client, add it to the client’s mcpServers configuration object. Fields:
command: The executable to run (always uv for FastMCP servers)
args: Command-line arguments including dependencies and server path
env: Environment variables (only included if specified)
All file paths in the generated configuration are absolute paths. This ensures the configuration works regardless of the working directory when the MCP client starts the server.

Requirements

uv: Must be installed and available in your system PATH
pyperclip (optional): Required only for --copy functionality
Install uv if not already available:
# macOS
brew install uv

# Linux/Windows  
curl -LsSf https://astral.sh/uv/install.sh | sh



