"""
Test MCP integration functionality
"""
import pytest
import json
import os
import tempfile
from unittest.mock import patch, AsyncMock, MagicMock

# Add the backend module to the path
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

from backend.mcp_manager import MCPServerManager, MCPServerConfig


class TestMCPIntegration:
    """Test MCP integration functionality"""

    def test_mcp_server_config_creation(self):
        """Test MCPServerConfig creation"""
        config = MCPServerConfig(
            name="test_server",
            type="local_stdio",
            enabled=True,
            description="Test server",
            script_path="test.py",
            tool_prefix="test_",
            environment_variables=["TEST_VAR"],
            required_env_vars=["TEST_VAR"]
        )
        
        assert config.name == "test_server"
        assert config.type == "local_stdio"
        assert config.enabled is True
        assert config.tool_prefix == "test_"

    def test_load_configuration_file_not_exists(self):
        """Test handling when configuration file doesn't exist"""
        manager = MCPServerManager(config_file="nonexistent.json")
        assert len(manager.servers) == 0

    def test_load_configuration_valid_file(self):
        """Test loading valid configuration file"""
        config_data = {
            "servers": [
                {
                    "name": "test_server",
                    "type": "local_stdio",
                    "enabled": True,
                    "description": "Test server",
                    "script_path": "test.py",
                    "tool_prefix": "test_",
                    "environment_variables": ["TEST_VAR"],
                    "required_env_vars": ["TEST_VAR"]
                }
            ]
        }
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(config_data, f)
            config_file = f.name
        
        try:
            manager = MCPServerManager(config_file=config_file)
            assert len(manager.servers) == 1
            assert "test_server" in manager.servers
            assert manager.servers["test_server"].name == "test_server"
        finally:
            os.unlink(config_file)

    def test_substitute_env_vars(self):
        """Test environment variable substitution"""
        manager = MCPServerManager(config_file="nonexistent.json")
        
        with patch.dict(os.environ, {"TEST_VAR": "test_value"}):
            result = manager._substitute_env_vars("${TEST_VAR}/path")
            assert result == "test_value/path"
            
            result = manager._substitute_env_vars("prefix_${TEST_VAR}_suffix")
            assert result == "prefix_test_value_suffix"

    def test_check_required_env_vars(self):
        """Test checking required environment variables"""
        manager = MCPServerManager(config_file="nonexistent.json")
        server = MCPServerConfig(
            name="test",
            type="local_stdio",
            enabled=True,
            description="Test",
            required_env_vars=["REQUIRED_VAR"]
        )
        
        # Variable not set
        result = manager._check_required_env_vars(server)
        assert result is False
        
        # Variable set
        with patch.dict(os.environ, {"REQUIRED_VAR": "value"}):
            result = manager._check_required_env_vars(server)
            assert result is True

    @pytest.mark.asyncio
    async def test_initialize_server_not_found(self):
        """Test initializing non-existent server"""
        manager = MCPServerManager(config_file="nonexistent.json")
        result = await manager.initialize_server("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_initialize_server_disabled(self):
        """Test initializing disabled server"""
        config_data = {
            "servers": [
                {
                    "name": "disabled_server",
                    "type": "local_stdio",
                    "enabled": False,
                    "description": "Disabled server"
                }
            ]
        }
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(config_data, f)
            config_file = f.name
        
        try:
            manager = MCPServerManager(config_file=config_file)
            result = await manager.initialize_server("disabled_server")
            assert result is False
        finally:
            os.unlink(config_file)

    def test_get_tools_empty(self):
        """Test getting tools when none are registered"""
        manager = MCPServerManager(config_file="nonexistent.json")
        tools = manager.get_tools()
        assert tools == []

    def test_get_available_tool_names_empty(self):
        """Test getting tool names when none are available"""
        manager = MCPServerManager(config_file="nonexistent.json")
        tool_names = manager.get_available_tool_names()
        assert tool_names == []

    def test_is_tool_available_false(self):
        """Test checking tool availability when none are available"""
        manager = MCPServerManager(config_file="nonexistent.json")
        result = manager.is_tool_available("nonexistent_tool")
        assert result is False

    @pytest.mark.asyncio
    async def test_call_tool_not_found(self):
        """Test calling tool that doesn't exist"""
        manager = MCPServerManager(config_file="nonexistent.json")
        
        with pytest.raises(RuntimeError, match="No MCP server found for tool"):
            await manager.call_tool("nonexistent_tool", {})


if __name__ == "__main__":
    pytest.main([__file__])
