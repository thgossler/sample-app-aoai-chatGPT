# MCP Servers Configuration

This document explains how to configure Model Context Protocol (MCP) servers for the Azure OpenAI Chat application. The application supports both local and remote MCP servers through a unified configuration system.

## Configuration File

The MCP servers are configured through the `mcp_servers.json` file in the backend/mcp_servers directory. This file defines a list of MCP servers that can be loaded and used by the chat application.

## Configuration Schema

```json
{
  "servers": [
    {
      "name": "server_name",
      "type": "local_stdio|local_http|remote_http",
      "enabled": true|false,
      "description": "Description of the server",
      "script_path": "path/to/server.py",  // For local_stdio only
      "tool_prefix": "prefix_",
      "environment_variables": [...],
      "required_env_vars": [...],
      "config": { ... }  // Server-specific configuration
    }
  ]
}
```

### Field Descriptions

- **name**: Unique identifier for the MCP server
- **type**: Type of MCP server connection
  - `local_stdio`: Local Python script using STDIO transport
  - `local_http`: Local HTTP server
  - `remote_http`: Remote HTTP server (like Azure Functions)
- **enabled**: Whether the server should be loaded (true/false)
- **description**: Human-readable description of the server
- **script_path**: Path to the Python script (for `local_stdio` type only)
- **tool_prefix**: Prefix added to tool names to avoid conflicts
- **environment_variables**: List of environment variables used by this server
- **required_env_vars**: List of environment variables that must be set
- **config**: Server-specific configuration object

## Server Types

### 1. Local STDIO MCP Server

Local Python scripts that communicate via STDIO (standard input/output).

#### Example Configuration

```json
{
  "name": "sw360",
  "type": "local_stdio",
  "enabled": true,
  "description": "SW360 MCP Server for component and vulnerability management",
  "script_path": "sw360_mcp_server.py",
  "tool_prefix": "local_sw360_",
  "environment_variables": [
    "SW360_API_KEY",
    "SW360_URL_ROOT"
  ],
  "required_env_vars": [
    "SW360_API_KEY", 
    "SW360_URL_ROOT"
  ]
}
```

#### Environment Variables

Set the required environment variables in your `.env` file or system environment:

```bash
SW360_API_KEY=your_api_key_here
SW360_URL_ROOT=https://sw360.example.com
```

### 2. Remote HTTP MCP Server (Azure Functions)

Remote servers accessible via HTTP, such as Azure Functions.

#### Example Configuration

```json
{
  "name": "azure_functions",
  "type": "remote_http",
  "enabled": true,
  "description": "Azure Functions remote MCP tools",
  "tool_prefix": "",
  "config": {
    "tool_endpoint": "${AZURE_OPENAI_FUNCTION_CALL_AZURE_FUNCTIONS_TOOL_BASE_URL}",
    "auth_type": "query_param",
    "auth_param": "code",
    "tool_auth_key": "${AZURE_OPENAI_FUNCTION_CALL_AZURE_FUNCTIONS_TOOL_KEY}"
  },
  "environment_variables": [],
  "required_env_vars": []
}
```

#### Environment Variables

```bash
AZURE_OPENAI_FUNCTION_CALL_AZURE_FUNCTIONS_TOOL_BASE_URL=https://your-function-app.azurewebsites.net/api/call_tool
AZURE_OPENAI_FUNCTION_CALL_AZURE_FUNCTIONS_TOOL_KEY=your_tool_function_key
```

### 3. Local HTTP MCP Server

Local HTTP servers running on localhost or within the network.

#### Example Configuration

```json
{
  "name": "my_local_http_mcp_server",
  "type": "local_http",
  "enabled": true,
  "description": "My local HTTP MCP Server",
  "tool_prefix": "myprefix_",
  "config": {
    "base_url": "http://localhost:8080",
    "tools_endpoint": "/tools",
    "tool_endpoint": "/call",
    "auth_type": "bearer_token",
    "auth_token": "${LOCAL_HTTP_MCP_AUTH_TOKEN}"
  },
  "environment_variables": [
    "LOCAL_HTTP_MCP_AUTH_TOKEN"
  ],
  "required_env_vars": [
    "LOCAL_HTTP_MCP_AUTH_TOKEN"
  ]
}
```

## Authentication Types

### Query Parameter Authentication

Used for Azure Functions and similar services that use query parameters for authentication:

```json
"config": {
  "auth_type": "query_param",
  "auth_param": "code",
  "tool_auth_key": "${FUNCTION_KEY}"
}
```

### Bearer Token Authentication

Used for services that require bearer token authentication:

```json
"config": {
  "auth_type": "bearer_token",
  "auth_token": "${AUTH_TOKEN}"
}
```

### API Key Authentication

Used for services that require API key authentication:

```json
"config": {
  "auth_type": "api_key",
  "api_key": "${API_KEY}",
  "header_name": "X-API-Key"  // Optional, defaults to "Authorization"
}
```

## Environment Variable Substitution

The configuration supports environment variable substitution in strings using the `${VARIABLE_NAME}` syntax. This allows you to keep sensitive information like API keys in environment variables rather than in the configuration file.

## Command Line Parameters

### Creating a New Local MCP Server

1. Create your MCP server Python script (e.g., `my_server.py`)
2. Add the server configuration to `mcp_servers.json`
3. Set required environment variables
4. Restart the application

### Debugging

Set the `DEBUG` environment variable to enable detailed logging:

```bash
DEBUG=true
```

## Tool Naming and Conflicts

To avoid tool name conflicts, each server can have a `tool_prefix`. The application will prepend this prefix to all tool names from that server. For example, an SW360 server with prefix `local_sw360_` will have tools like `local_sw360_get_project`.

## Example: Adding a New Local MCP Server

1. **Create the server script** (`my_custom_server.py`):

```python
from fastmcp import FastMCP

mcp = FastMCP("My Custom Server", version="1.0.0")

@mcp.tool()
def hello_world(name: str) -> str:
    return f"Hello, {name}!"

if __name__ == "__main__":
    mcp.run()
```

2. **Add configuration to `mcp_servers.json`**:

```json
{
  "name": "my_custom",
  "type": "local_stdio",
  "enabled": true,
  "description": "My custom MCP server",
  "script_path": "my_custom_server.py",
  "tool_prefix": "custom_",
  "environment_variables": [],
  "required_env_vars": []
}
```

3. **Restart the application** - the new tools will be available as `custom_hello_world`

## Troubleshooting

### Common Issues

1. **Server not loading**: Check that `enabled` is set to `true` and all required environment variables are set
2. **Tools not appearing**: Verify the server script runs correctly in test mode
3. **Authentication errors**: Check that API keys and URLs are correct
4. **Tool conflicts**: Use unique `tool_prefix` values for each server

### Logging

Enable debug logging to see detailed information about MCP server initialization:

```bash
DEBUG=true
```

Check the application logs for messages like:
- "MCP server 'server_name' initialized with N tools"
- "Failed to initialize MCP server 'server_name': error message"
