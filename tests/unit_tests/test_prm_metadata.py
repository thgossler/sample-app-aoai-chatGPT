"""
Unit tests for ``backend.mcp_server.prm_metadata``.
"""

import pytest
from backend.mcp_server.prm_metadata import build_prm_metadata

TENANT_ID = "my-tenant"
CLIENT_ID = "my-client-id"
SERVER_URL = "https://myapp.azurewebsites.net/mcp"


class TestBuildPrmMetadata:
    def test_required_fields_present(self):
        meta = build_prm_metadata(
            server_url=SERVER_URL,
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
        )
        assert "resource" in meta
        assert "authorization_servers" in meta
        assert "scopes_supported" in meta
        assert "bearer_methods_supported" in meta

    def test_resource_is_server_url(self):
        meta = build_prm_metadata(
            server_url=SERVER_URL,
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
        )
        assert meta["resource"] == SERVER_URL

    def test_resource_strips_trailing_slash(self):
        meta = build_prm_metadata(
            server_url=SERVER_URL + "/",
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
        )
        assert not meta["resource"].endswith("/")

    def test_authorization_server_is_entra_v2(self):
        meta = build_prm_metadata(
            server_url=SERVER_URL,
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
        )
        expected = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
        assert meta["authorization_servers"] == [expected]

    def test_default_scopes_include_read_and_execute(self):
        meta = build_prm_metadata(
            server_url=SERVER_URL,
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
        )
        scopes = meta["scopes_supported"]
        assert any("MCP.Tools.Read" in s for s in scopes)
        assert any("MCP.Tools.Execute" in s for s in scopes)

    def test_custom_scopes_override_defaults(self):
        custom = ["api://x/Custom.Scope"]
        meta = build_prm_metadata(
            server_url=SERVER_URL,
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            scopes_supported=custom,
        )
        assert meta["scopes_supported"] == custom

    def test_bearer_header_supported(self):
        meta = build_prm_metadata(
            server_url=SERVER_URL,
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
        )
        assert "header" in meta["bearer_methods_supported"]

    def test_default_scope_included_when_provided(self):
        meta = build_prm_metadata(
            server_url=SERVER_URL,
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            default_scope=f"api://{CLIENT_ID}/MCP.Tools.Execute",
        )
        assert "scopes_default" in meta
        assert meta["scopes_default"][0].endswith("MCP.Tools.Execute")

    def test_no_default_scope_when_omitted(self):
        meta = build_prm_metadata(
            server_url=SERVER_URL,
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
        )
        assert "scopes_default" not in meta

    def test_resource_id_overrides_server_url(self):
        # When resource_id is provided it becomes the ``resource`` identifier
        # (must match the scopes' Application ID URI so Entra ID accepts the
        # RFC 8707 resource parameter), while the documentation link still
        # derives from server_url.
        resource_id = f"api://{CLIENT_ID}"
        meta = build_prm_metadata(
            server_url=SERVER_URL,
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            resource_id=resource_id,
        )
        assert meta["resource"] == resource_id
        assert meta["resource_documentation"].startswith("https://myapp.azurewebsites.net")

    def test_resource_id_strips_trailing_slash(self):
        meta = build_prm_metadata(
            server_url=SERVER_URL,
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            resource_id=f"api://{CLIENT_ID}/",
        )
        assert meta["resource"] == f"api://{CLIENT_ID}"

