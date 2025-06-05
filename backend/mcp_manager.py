"""
Generic MCP Server Manager

This module provides a unified interface for managing both local and remote MCP servers.
It loads configuration from mcp_servers.json and handles initialization, tool registration,
and tool execution for different types of MCP servers.
"""

import json
import os
import logging
import re
from typing import Dict, List, Any, Optional, Union
from dataclasses import dataclass
import httpx
from fastmcp import Client
from fastmcp.client.transports import PythonStdioTransport


@dataclass
class MCPServerConfig:
    """Configuration for a single MCP server"""
    name: str
    type: str  # local_stdio, local_http, remote_http
    enabled: bool
    description: str
    script_path: Optional[str] = None
    tool_prefix: str = ""
    environment_variables: List[str] = None
    required_env_vars: List[str] = None
    config: Dict[str, Any] = None

    def __post_init__(self):
        if self.environment_variables is None:
            self.environment_variables = []
        if self.required_env_vars is None:
            self.required_env_vars = []
        if self.config is None:
            self.config = {}


class MCPServerManager:
    """Manages multiple MCP servers and their tools"""

    def __init__(self, config_file: str = "mcp_servers.json"):
        self.config_file = os.path.join(os.path.dirname(__file__), "mcp_servers", config_file)
        self.servers: Dict[str, MCPServerConfig] = {}
        self.clients: Dict[str, Any] = {}
        self.tools: List[Dict[str, Any]] = []
        self.available_tools: List[str] = []
        self._load_configuration()

    def _load_configuration(self):
        """Load MCP server configuration from JSON file"""
        if not os.path.exists(self.config_file):
            logging.warning(f"MCP configuration file {self.config_file} not found. No MCP servers will be loaded.")
            return

        try:
            with open(self.config_file, 'r') as f:
                config_data = json.load(f)
            
            for server_config in config_data.get('servers', []):
                server = MCPServerConfig(**server_config)
                self.servers[server.name] = server
                logging.debug(f"Loaded MCP server configuration: {server.name}")
                
        except Exception as e:
            logging.error(f"Failed to load MCP configuration from {self.config_file}: {e}")

    def _substitute_env_vars(self, text: str) -> str:
        """Substitute environment variables in configuration strings"""
        if not isinstance(text, str):
            return text
            
        # Replace ${VAR_NAME} with environment variable values
        pattern = r'\$\{([^}]+)\}'
        matches = re.findall(pattern, text)
        
        for var_name in matches:
            env_value = os.environ.get(var_name, '')
            text = text.replace(f'${{{var_name}}}', env_value)
        
        return text

    def _check_required_env_vars(self, server: MCPServerConfig) -> bool:
        """Check if all required environment variables are set"""
        missing_vars = []
        for var_name in server.required_env_vars:
            if not os.environ.get(var_name):
                missing_vars.append(var_name)
        
        if missing_vars:
            logging.warning(f"MCP server '{server.name}' missing required environment variables: {missing_vars}")
            return False
        
        return True

    async def initialize_server(self, server_name: str) -> bool:
        """Initialize a specific MCP server"""
        server = self.servers.get(server_name)
        if not server:
            logging.error(f"MCP server '{server_name}' not found in configuration")
            return False

        if not server.enabled:
            logging.debug(f"MCP server '{server_name}' is disabled")
            return False

        if not self._check_required_env_vars(server):
            return False

        try:
            if server.type == "local_stdio":
                return await self._initialize_local_stdio_server(server)
            elif server.type == "local_http":
                return await self._initialize_local_http_server(server)
            elif server.type == "remote_http":
                return await self._initialize_remote_http_server(server)
            else:
                logging.error(f"Unknown MCP server type: {server.type}")
                return False
                
        except Exception as e:
            logging.exception(f"Failed to initialize MCP server '{server_name}': {e}")
            return False

    async def _initialize_local_stdio_server(self, server: MCPServerConfig) -> bool:
        """Initialize a local STDIO MCP server"""
        if not server.script_path:
            logging.error(f"Local STDIO server '{server.name}' missing script_path")
            return False

        script_path = os.path.join(os.path.dirname(__file__), "mcp_servers", server.script_path)
        if not os.path.exists(script_path):
            logging.error(f"MCP server script not found: {script_path}")
            return False

        # Prepare environment variables
        env_vars = {}
        for var_name in server.environment_variables:
            if os.environ.get(var_name):
                env_vars[var_name] = os.environ.get(var_name)

        # Create transport with environment variables
        transport = PythonStdioTransport(
            script_path=script_path,
            env=env_vars
        )

        # Create and store client
        client = Client(transport)
        self.clients[server.name] = client

        # Get available tools
        async with client as conn:
            tools_list = await conn.list_tools()
            
            # Convert MCP tools to OpenAI function format
            for tool in tools_list:
                openai_tool = {
                    "type": "function",
                    "function": {
                        "name": f"{server.tool_prefix}{tool.name}",
                        "description": tool.description,
                        "parameters": tool.inputSchema
                    }
                }
                self.tools.append(openai_tool)
                self.available_tools.append(f"{server.tool_prefix}{tool.name}")

        logging.info(f"MCP server '{server.name}' initialized with {len(tools_list)} tools")
        return True

    async def _initialize_local_http_server(self, server: MCPServerConfig) -> bool:
        """Initialize a local HTTP MCP server"""
        # This would implement HTTP-based MCP client
        # For now, log that it's not implemented
        logging.warning(f"Local HTTP MCP servers not yet implemented for '{server.name}'")
        return False

    async def _initialize_remote_http_server(self, server: MCPServerConfig) -> bool:
        """Initialize a remote HTTP MCP server (like Azure Functions)"""
        config = server.config.copy()
        
        # Substitute environment variables in config
        for key, value in config.items():
            if isinstance(value, str):
                config[key] = self._substitute_env_vars(value)

        # Check if this is Azure Functions style
        if 'tools_endpoint' in config and 'tool_endpoint' in config:
            return await self._initialize_azure_functions_server(server, config)
        
        logging.warning(f"Remote HTTP server type not recognized for '{server.name}'")
        return False

    async def _initialize_azure_functions_server(self, server: MCPServerConfig, config: Dict[str, Any]) -> bool:
        """Initialize Azure Functions style remote MCP server"""
        try:
            tools_url = config['tools_endpoint']
            auth_key = config.get('tools_auth_key', '')
            
            if config.get('auth_type') == 'query_param':
                auth_param = config.get('auth_param', 'code')
                tools_url = f"{tools_url}?{auth_param}={auth_key}"

            # Fetch available tools
            async with httpx.AsyncClient() as client:
                response = await client.get(tools_url)
            
            if response.status_code == httpx.codes.OK:
                new_tools = json.loads(response.text)
                added_count = 0
                
                # Add tools with deduplication
                for tool in new_tools:
                    tool_name = f"{server.tool_prefix}{tool['function']['name']}"
                    if tool_name not in self.available_tools:
                        # Update tool name with prefix
                        tool_copy = tool.copy()
                        tool_copy['function']['name'] = tool_name
                        
                        self.tools.append(tool_copy)
                        self.available_tools.append(tool_name)
                        added_count += 1

                # Store server config for tool execution
                self.clients[server.name] = {
                    'type': 'azure_functions',
                    'config': config
                }

                logging.info(f"Remote MCP server '{server.name}' initialized with {added_count} tools")
                return True
            else:
                logging.error(f"Failed to get tools from '{server.name}': {response.status_code}")
                return False
                
        except Exception as e:
            logging.exception(f"Failed to initialize Azure Functions server '{server.name}': {e}")
            return False

    async def initialize_all_servers(self) -> int:
        """Initialize all enabled MCP servers"""
        initialized_count = 0
        
        for server_name in self.servers.keys():
            if await self.initialize_server(server_name):
                initialized_count += 1
        
        logging.info(f"Initialized {initialized_count} MCP servers with {len(self.tools)} total tools")
        return initialized_count

    async def call_tool(self, tool_name: str, tool_args: Dict[str, Any]) -> str:
        """Call a tool by name with the given arguments"""
        # Find which server owns this tool
        server_name = None
        actual_tool_name = tool_name
        
        for name, server in self.servers.items():
            if tool_name.startswith(server.tool_prefix):
                server_name = name
                actual_tool_name = tool_name[len(server.tool_prefix):]
                break
        
        if not server_name or server_name not in self.clients:
            raise RuntimeError(f"No MCP server found for tool: {tool_name}")

        client_or_config = self.clients[server_name]
        server = self.servers[server_name]

        try:
            if server.type == "local_stdio":
                return await self._call_local_stdio_tool(client_or_config, actual_tool_name, tool_args)
            elif server.type == "remote_http":
                return await self._call_remote_http_tool(client_or_config, actual_tool_name, tool_args)
            else:
                raise RuntimeError(f"Unsupported server type: {server.type}")
                
        except Exception as e:
            logging.error(f"Error calling tool {tool_name}: {e}")
            return f"Error: {str(e)}"

    async def _call_local_stdio_tool(self, client: Client, tool_name: str, tool_args: Dict[str, Any]) -> str:
        """Call a tool on a local STDIO MCP server"""
        async with client as conn:
            result = await conn.call_tool(tool_name, tool_args)
            if result is not None and hasattr(result, "__len__") and len(result) > 0 and hasattr(result[0], "text"):
                return result[0].text
            else:
                return str(result)

    async def _call_remote_http_tool(self, server_config: Dict[str, Any], tool_name: str, tool_args: Dict[str, Any]) -> str:
        """Call a tool on a remote HTTP MCP server"""
        if server_config['type'] == 'azure_functions':
            return await self._call_azure_functions_tool(server_config['config'], tool_name, tool_args)
        else:
            raise RuntimeError(f"Unknown remote HTTP server type: {server_config['type']}")

    async def _call_azure_functions_tool(self, config: Dict[str, Any], tool_name: str, tool_args: Dict[str, Any]) -> str:
        """Call a tool on Azure Functions"""
        tool_url = config['tool_endpoint']
        auth_key = config.get('tool_auth_key', '')
        
        if config.get('auth_type') == 'query_param':
            auth_param = config.get('auth_param', 'code')
            tool_url = f"{tool_url}?{auth_param}={auth_key}"

        headers = {'content-type': 'application/json'}
        body = {
            "tool_name": tool_name,
            "tool_arguments": tool_args
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(tool_url, data=json.dumps(body), headers=headers)
        
        response.raise_for_status()
        return response.text

    def get_tools(self) -> List[Dict[str, Any]]:
        """Get all registered tools"""
        return self.tools.copy()

    def get_available_tool_names(self) -> List[str]:
        """Get list of available tool names"""
        return self.available_tools.copy()

    def is_tool_available(self, tool_name: str) -> bool:
        """Check if a tool is available"""
        return tool_name in self.available_tools
