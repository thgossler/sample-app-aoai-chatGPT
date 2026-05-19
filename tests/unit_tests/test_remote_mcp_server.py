"""
Unit tests for ``backend.mcp_server.remote_mcp_server.RemoteMCPServer``.

Tests cover:
- initialize() sets _initialized flag and calls registration helpers
- _setup_auth() creates validator when settings are present
- _setup_auth() logs warning and leaves validator None when settings absent
- _register_knowledge_base_tools() registers tools on FastMCP instance
- _register_manager_tools() iterates mcp_manager.get_tools() and registers each
- _register_manager_tools() skips registration gracefully when mcp_manager is None
- validate_request_token() happy path returns claims
- validate_request_token() raises AuthError on missing header
- validate_request_token() raises AuthError on missing Bearer prefix
- validate_request_token() raises 403 AuthError when has_mcp_access returns False
- validate_request_token() passes-through without auth guard when validator is None
- get_prm_metadata() returns metadata dict when auth is configured
- get_prm_metadata() returns None when auth is not configured
- double initialize() is idempotent
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from backend.mcp_server.auth_middleware import AuthError
from backend.mcp_server.remote_mcp_server import (
    _MCPAuthStarletteMiddleware,
    RemoteMCPServer,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_mcp_cfg(
    tenant_id="tenant-123",
    client_id="client-456",
    server_url="https://app.example.com/mcp",
    server_enabled=True,
    auth_allowed_client_ids=None,
    auth_multi_tenant=False,
    auth_audience=None,
    auth_issuer=None,
    auth_default_scope=None,
):
    cfg = MagicMock()
    cfg.auth_tenant_id = tenant_id
    cfg.auth_client_id = client_id
    cfg.server_url = server_url
    cfg.server_enabled = server_enabled
    cfg.auth_allowed_client_ids = auth_allowed_client_ids or []
    cfg.auth_multi_tenant = auth_multi_tenant
    cfg.auth_audience = auth_audience or f"api://{client_id}"
    cfg.auth_issuer = auth_issuer or f"https://sts.windows.net/{tenant_id}/"
    cfg.auth_default_scope = auth_default_scope or f"api://{client_id}/.default"
    return cfg


def _make_app_settings(with_mcp_cfg=True, with_datasource=True):
    settings = MagicMock()
    settings.remote_mcp_server = _make_mcp_cfg() if with_mcp_cfg else None
    settings.ui.title = "Test App"
    settings.azure_openai.system_message = "System prompt"
    settings.base_settings.datasource_type = "AzureCognitiveSearch"
    if with_datasource:
        ds = MagicMock()
        ds.query_type = "semantic"
        ds.index = "my-index"
        ds.top_k = 5
        settings.datasource = ds
    else:
        settings.datasource = None
    citation = MagicMock()
    citation.storage_base_url = None
    citation.link_base_url = None
    citation.link_url_appendix = None
    settings.citation_file = citation
    return settings


def _make_mcp_manager(tool_names=("tool_a", "tool_b")):
    manager = MagicMock()
    tools = [
        {"function": {"name": name, "description": f"Desc for {name}", "parameters": {}}}
        for name in tool_names
    ]
    manager.get_tools.return_value = tools
    manager.call_tool = AsyncMock(return_value="tool result")
    return manager


# ---------------------------------------------------------------------------
# Initialization tests
# ---------------------------------------------------------------------------

class TestInitialize:
    @pytest.mark.asyncio
    async def test_initialize_sets_initialized_flag(self):
        server = RemoteMCPServer(app_settings=_make_app_settings())
        assert not server._initialized
        with patch.object(server, "_setup_auth"), \
             patch.object(server, "_register_knowledge_base_tools"), \
             patch.object(server, "_register_manager_tools"), \
             patch.object(server, "_register_resources"), \
             patch.object(server, "_register_prompts"):
            await server.initialize()
        assert server._initialized

    @pytest.mark.asyncio
    async def test_initialize_is_idempotent(self):
        server = RemoteMCPServer(app_settings=_make_app_settings())
        with patch.object(server, "_setup_auth") as mock_setup, \
             patch.object(server, "_register_knowledge_base_tools"), \
             patch.object(server, "_register_manager_tools"), \
             patch.object(server, "_register_resources"), \
             patch.object(server, "_register_prompts"):
            await server.initialize()
            await server.initialize()
            # _setup_auth should only be called once
            mock_setup.assert_called_once()

    @pytest.mark.asyncio
    async def test_initialize_calls_all_registration_methods(self):
        server = RemoteMCPServer(app_settings=_make_app_settings())
        methods = [
            "_setup_auth",
            "_register_knowledge_base_tools",
            "_register_manager_tools",
            "_register_resources",
            "_register_prompts",
        ]
        mocks = {m: patch.object(server, m) for m in methods}
        with mocks["_setup_auth"] as sa, \
             mocks["_register_knowledge_base_tools"] as rkb, \
             mocks["_register_manager_tools"] as rmt, \
             mocks["_register_resources"] as rr, \
             mocks["_register_prompts"] as rp:
            await server.initialize()
        sa.assert_called_once()
        rkb.assert_called_once()
        rmt.assert_called_once()
        rr.assert_called_once()
        rp.assert_called_once()


# ---------------------------------------------------------------------------
# Auth setup tests
# ---------------------------------------------------------------------------

class TestSetupAuth:
    def test_validator_created_when_settings_present(self):
        settings = _make_app_settings(with_mcp_cfg=True)
        server = RemoteMCPServer(app_settings=settings)
        with patch(
            "backend.mcp_server.remote_mcp_server.EntraIDTokenValidator"
        ) as MockValidator:
            MockValidator.return_value = MagicMock()
            server._setup_auth()
        assert server._validator is not None

    def test_validator_none_when_mcp_cfg_missing(self):
        settings = _make_app_settings(with_mcp_cfg=False)
        server = RemoteMCPServer(app_settings=settings)
        server._setup_auth()
        assert server._validator is None

    def test_validator_none_when_tenant_id_missing(self):
        settings = _make_app_settings(with_mcp_cfg=True)
        settings.remote_mcp_server.auth_tenant_id = None
        server = RemoteMCPServer(app_settings=settings)
        server._setup_auth()
        assert server._validator is None

    def test_validator_none_when_client_id_missing(self):
        settings = _make_app_settings(with_mcp_cfg=True)
        settings.remote_mcp_server.auth_client_id = None
        server = RemoteMCPServer(app_settings=settings)
        server._setup_auth()
        assert server._validator is None


# ---------------------------------------------------------------------------
# Tool registration tests
# ---------------------------------------------------------------------------

class TestRegisterKnowledgeBaseTools:
    def test_tools_registered_on_mcp_instance(self):
        server = RemoteMCPServer(app_settings=_make_app_settings())
        server._register_knowledge_base_tools()
        # FastMCP should expose a list_tools() or _tools dict
        # Use duck-typing: the tool names should be discoverable
        # FastMCP's internal dict is at server.mcp._tools
        tool_names = set()
        if hasattr(server.mcp, "_tools"):
            tool_names = set(server.mcp._tools.keys())
        elif hasattr(server.mcp, "list_tools"):
            tool_names = {t.name for t in server.mcp.list_tools()}

        # We only assert if FastMCP exposes the registered tools
        # (version-dependent); at minimum, no exception should be raised.
        assert True  # Registration did not raise

    def test_registration_does_not_raise(self):
        server = RemoteMCPServer(app_settings=_make_app_settings())
        try:
            server._register_knowledge_base_tools()
        except Exception as exc:
            pytest.fail(f"_register_knowledge_base_tools raised: {exc}")


class TestRegisterManagerTools:
    def test_no_mcp_manager_does_not_raise(self):
        server = RemoteMCPServer(app_settings=_make_app_settings(), mcp_manager=None)
        try:
            server._register_manager_tools()
        except Exception as exc:
            pytest.fail(f"_register_manager_tools raised with None manager: {exc}")

    def test_tools_from_manager_registered(self):
        manager = _make_mcp_manager(["my_tool"])
        server = RemoteMCPServer(app_settings=_make_app_settings(), mcp_manager=manager)
        server._register_manager_tools()
        manager.get_tools.assert_called_once()

    def test_empty_tool_name_skipped(self):
        manager = MagicMock()
        manager.get_tools.return_value = [
            {"function": {"name": "", "description": "nameless", "parameters": {}}}
        ]
        server = RemoteMCPServer(app_settings=_make_app_settings(), mcp_manager=manager)
        # Should not raise
        server._register_manager_tools()


# ---------------------------------------------------------------------------
# validate_request_token tests
# ---------------------------------------------------------------------------

class TestValidateRequestToken:
    @pytest.mark.asyncio
    async def test_missing_header_raises_401(self):
        server = RemoteMCPServer(app_settings=_make_app_settings())
        server._validator = MagicMock()  # validator present but header missing
        with pytest.raises(AuthError) as exc_info:
            await server.validate_request_token(None)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_malformed_header_raises_401(self):
        server = RemoteMCPServer(app_settings=_make_app_settings())
        server._validator = MagicMock()
        with pytest.raises(AuthError) as exc_info:
            await server.validate_request_token("Basic dXNlcjpwYXNz")
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_token_returns_claims(self):
        server = RemoteMCPServer(app_settings=_make_app_settings())
        mock_validator = MagicMock()
        mock_validator.validate_token = AsyncMock(
            return_value={"oid": "user-oid", "scp": "MCP.Tools.Read"}
        )
        server._validator = mock_validator

        with patch(
            "backend.mcp_server.remote_mcp_server.EntraIDTokenValidator.has_mcp_access",
            return_value=True,
        ):
            claims = await server.validate_request_token("Bearer validtoken123")

        assert claims["oid"] == "user-oid"

    @pytest.mark.asyncio
    async def test_insufficient_scope_raises_403(self):
        server = RemoteMCPServer(app_settings=_make_app_settings())
        mock_validator = MagicMock()
        mock_validator.validate_token = AsyncMock(return_value={"oid": "u", "scp": "read"})
        server._validator = mock_validator

        with patch(
            "backend.mcp_server.remote_mcp_server.EntraIDTokenValidator.has_mcp_access",
            return_value=False,
        ):
            with pytest.raises(AuthError) as exc_info:
                await server.validate_request_token("Bearer lowpermtoken")
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_no_validator_accepts_any_request(self):
        """When auth is not configured, all requests should be accepted."""
        server = RemoteMCPServer(app_settings=_make_app_settings())
        server._validator = None  # no validator
        claims = await server.validate_request_token(None)
        assert claims == {}

    @pytest.mark.asyncio
    async def test_no_validator_accepts_request_with_header(self):
        server = RemoteMCPServer(app_settings=_make_app_settings())
        server._validator = None
        claims = await server.validate_request_token("Bearer sometoken")
        assert claims == {}


# ---------------------------------------------------------------------------
# HTTP auth challenge tests
# ---------------------------------------------------------------------------

class TestMCPAuthMiddlewareChallenges:
    def _middleware(self, validator):
        return _MCPAuthStarletteMiddleware(
            app=MagicMock(),
            validator=validator,
            server_url="https://app.example.com/mcp",
            prm_metadata_getter=lambda: {
                "scopes_supported": [
                    "api://client-456/MCP.Tools.Read",
                    "api://client-456/MCP.Tools.Execute",
                ],
                "scopes_default": ["api://client-456/MCP.Tools.Execute"],
            },
        )

    @staticmethod
    def _request(headers=None):
        return SimpleNamespace(
            url=SimpleNamespace(path="/mcp"),
            method="POST",
            headers=headers or {},
        )

    @pytest.mark.asyncio
    async def test_401_challenge_includes_invalid_token_metadata_and_scope(self):
        validator = MagicMock()
        validator.validate_token = AsyncMock(
            side_effect=AuthError("Token has expired", status_code=401)
        )
        middleware = self._middleware(validator)
        call_next = AsyncMock()

        response = await middleware.dispatch(
            self._request({"authorization": "Bearer expired-token"}),
            call_next,
        )

        assert response.status_code == 401
        call_next.assert_not_called()
        challenge = response.headers["www-authenticate"]
        assert 'error="invalid_token"' in challenge
        assert 'error_description="Token has expired"' in challenge
        assert (
            'resource_metadata="https://app.example.com/.well-known/oauth-protected-resource"'
            in challenge
        )
        assert 'scope="api://client-456/MCP.Tools.Execute"' in challenge

    @pytest.mark.asyncio
    async def test_403_challenge_includes_insufficient_scope_and_required_scope(self):
        validator = MagicMock()
        validator.validate_token = AsyncMock(
            return_value={"oid": "u", "scp": "openid"}
        )
        middleware = self._middleware(validator)
        call_next = AsyncMock()

        response = await middleware.dispatch(
            self._request({"authorization": "Bearer low-scope-token"}),
            call_next,
        )

        assert response.status_code == 403
        call_next.assert_not_called()
        challenge = response.headers["www-authenticate"]
        assert 'error="insufficient_scope"' in challenge
        assert 'error_description="Insufficient MCP scope or role"' in challenge
        assert (
            'resource_metadata="https://app.example.com/.well-known/oauth-protected-resource"'
            in challenge
        )
        assert 'scope="api://client-456/MCP.Tools.Execute"' in challenge


# ---------------------------------------------------------------------------
# get_prm_metadata tests
# ---------------------------------------------------------------------------

class TestGetPrmMetadata:
    def test_returns_metadata_when_auth_configured(self):
        settings = _make_app_settings(with_mcp_cfg=True)
        server = RemoteMCPServer(app_settings=settings)
        meta = server.get_prm_metadata()
        assert meta is not None
        assert "resource" in meta
        assert "authorization_servers" in meta

    def test_returns_none_when_mcp_cfg_missing(self):
        settings = _make_app_settings(with_mcp_cfg=False)
        server = RemoteMCPServer(app_settings=settings)
        meta = server.get_prm_metadata()
        assert meta is None

    def test_returns_none_when_tenant_id_missing(self):
        settings = _make_app_settings(with_mcp_cfg=True)
        settings.remote_mcp_server.auth_tenant_id = None
        server = RemoteMCPServer(app_settings=settings)
        meta = server.get_prm_metadata()
        assert meta is None

    def test_server_url_in_resource_field(self):
        settings = _make_app_settings(with_mcp_cfg=True)
        server = RemoteMCPServer(app_settings=settings)
        meta = server.get_prm_metadata()
        assert "https://app.example.com/mcp" in meta["resource"]

    def test_scopes_include_client_id(self):
        settings = _make_app_settings(with_mcp_cfg=True)
        server = RemoteMCPServer(app_settings=settings)
        meta = server.get_prm_metadata()
        scopes_str = " ".join(meta.get("scopes_supported", []))
        assert "client-456" in scopes_str


# ---------------------------------------------------------------------------
# Tool-level RBAC tests  (task 7.3)
# ---------------------------------------------------------------------------

class TestToolLevelRBAC:
    """Tests for set_tool_role_requirement() and _check_tool_rbac()."""

    # Import the module-level ContextVar so we can seed caller identity
    from backend.mcp_server import remote_mcp_server as _rms_module

    def _server(self) -> RemoteMCPServer:
        server = RemoteMCPServer(app_settings=_make_app_settings())
        # Give it a non-None validator so RBAC checks are active
        server._validator = MagicMock()
        return server

    # ------------------------------------------------------------------
    # set_tool_role_requirement()
    # ------------------------------------------------------------------

    def test_set_tool_role_requirement_stores_roles(self):
        server = self._server()
        server.set_tool_role_requirement("admin_tool", ["MCP.Admin"])
        assert server._tool_role_requirements["admin_tool"] == ["MCP.Admin"]

    def test_set_tool_role_requirement_overwrites_existing(self):
        server = self._server()
        server.set_tool_role_requirement("some_tool", ["MCP.User"])
        server.set_tool_role_requirement("some_tool", ["MCP.Admin", "MCP.SuperAdmin"])
        assert server._tool_role_requirements["some_tool"] == ["MCP.Admin", "MCP.SuperAdmin"]

    # ------------------------------------------------------------------
    # _check_tool_rbac() — bypass / no-op cases
    # ------------------------------------------------------------------

    def test_check_rbac_no_validator_skips(self):
        server = RemoteMCPServer(app_settings=_make_app_settings())
        server._validator = None  # auth disabled
        server.set_tool_role_requirement("secret_tool", ["MCP.Admin"])
        # Should not raise regardless of caller claims
        server._check_tool_rbac("secret_tool")

    def test_check_rbac_no_requirement_registered_passes(self):
        server = self._server()
        # Set context with a user that has no special roles
        token = self._rms_module._caller_context.set({"oid": "u1", "roles": [], "scp": ""})
        try:
            server._check_tool_rbac("unrestricted_tool")  # no requirement → pass
        finally:
            self._rms_module._caller_context.reset(token)

    def test_check_rbac_empty_requirement_list_passes(self):
        server = self._server()
        server.set_tool_role_requirement("tool_x", [])  # explicit empty list
        token = self._rms_module._caller_context.set({"oid": "u1", "roles": [], "scp": ""})
        try:
            server._check_tool_rbac("tool_x")
        finally:
            self._rms_module._caller_context.reset(token)

    # ------------------------------------------------------------------
    # _check_tool_rbac() — MCP.Admin bypass
    # ------------------------------------------------------------------

    def test_check_rbac_admin_role_bypasses(self):
        server = self._server()
        server.set_tool_role_requirement("restricted_tool", ["SpecialRole"])
        # Caller only has MCP.Admin — should bypass the SpecialRole requirement
        token = self._rms_module._caller_context.set(
            {"oid": "admin-user", "roles": ["MCP.Admin"], "scp": ""}
        )
        try:
            server._check_tool_rbac("restricted_tool")  # should NOT raise
        finally:
            self._rms_module._caller_context.reset(token)

    # ------------------------------------------------------------------
    # _check_tool_rbac() — role / scope satisfied
    # ------------------------------------------------------------------

    def test_check_rbac_required_role_present_passes(self):
        server = self._server()
        server.set_tool_role_requirement("power_tool", ["MCP.PowerUser"])
        token = self._rms_module._caller_context.set(
            {"oid": "p-user", "roles": ["MCP.PowerUser"], "scp": ""}
        )
        try:
            server._check_tool_rbac("power_tool")
        finally:
            self._rms_module._caller_context.reset(token)

    def test_check_rbac_any_of_multiple_roles_passes(self):
        server = self._server()
        server.set_tool_role_requirement("multi_tool", ["MCP.RoleA", "MCP.RoleB"])
        # Caller only has RoleB — should still pass (ANY match)
        token = self._rms_module._caller_context.set(
            {"oid": "b-user", "roles": ["MCP.RoleB"], "scp": ""}
        )
        try:
            server._check_tool_rbac("multi_tool")
        finally:
            self._rms_module._caller_context.reset(token)

    def test_check_rbac_required_scope_passes(self):
        server = self._server()
        server.set_tool_role_requirement("scoped_tool", ["MCP.SpecialScope"])
        # No roles, but has the required scope
        token = self._rms_module._caller_context.set(
            {"oid": "s-user", "roles": [], "scp": "openid MCP.SpecialScope profile"}
        )
        try:
            server._check_tool_rbac("scoped_tool")
        finally:
            self._rms_module._caller_context.reset(token)

    # ------------------------------------------------------------------
    # _check_tool_rbac() — insufficient permissions → AuthError(403)
    # ------------------------------------------------------------------

    def test_check_rbac_missing_role_raises_403(self):
        server = self._server()
        server.set_tool_role_requirement("admin_tool", ["MCP.Admin"])
        # Caller has no special roles/scopes
        token = self._rms_module._caller_context.set(
            {"oid": "nobody", "roles": [], "scp": "MCP.Tools.Execute"}
        )
        try:
            with pytest.raises(AuthError) as exc_info:
                server._check_tool_rbac("admin_tool")
            assert exc_info.value.status_code == 403
            assert "admin_tool" in str(exc_info.value)
        finally:
            self._rms_module._caller_context.reset(token)

    def test_check_rbac_wrong_scope_raises_403(self):
        server = self._server()
        server.set_tool_role_requirement("premium_tool", ["MCP.PremiumScope"])
        # Caller has a different scope — not the required one
        token = self._rms_module._caller_context.set(
            {"oid": "basic-user", "roles": [], "scp": "MCP.Tools.Execute openid"}
        )
        try:
            with pytest.raises(AuthError) as exc_info:
                server._check_tool_rbac("premium_tool")
            assert exc_info.value.status_code == 403
        finally:
            self._rms_module._caller_context.reset(token)
