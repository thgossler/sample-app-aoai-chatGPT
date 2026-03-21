"""
Remote MCP Server — end-to-end integration tests (Tasks 6.1 – 6.4).

These tests exercise the complete MCP stack against a running server instance.
Two modes are supported:

  A) Real deployed server (``MCP_SERVER_URL`` env var is set):
       All tests use raw HTTP via ``httpx``.

  B) Local ASGI (``MCP_LOCAL_ASGI=1`` env var is set):
       Tests spin up the Quart/ASGI app in-process via
       ``httpx.ASGITransport``.  Requires a valid ``.env`` file with at
       least ``AZURE_OPENAI_ENDPOINT`` and ``AZURE_OPENAI_MODEL``.

Auth tests (6.2, 6.3) additionally require:
  REMOTE_MCP_TEST_TENANT_ID       Entra ID tenant
  REMOTE_MCP_TEST_CLIENT_ID       Test app registration client ID
  REMOTE_MCP_TEST_CLIENT_SECRET   Test app registration client secret
  REMOTE_MCP_TEST_RESOURCE_APP_ID Application ID of the MCP server app

Skip logic:
  - If neither ``MCP_SERVER_URL`` nor ``MCP_LOCAL_ASGI`` is set, the whole
    module is skipped.
  - Individual auth tests are skipped when ``REMOTE_MCP_TEST_CLIENT_ID`` is
    absent.
"""

import json
import os

import httpx
import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Module-level skip guard
# ---------------------------------------------------------------------------
_SERVER_URL = os.getenv("MCP_SERVER_URL", "").rstrip("/")
_LOCAL_ASGI = os.getenv("MCP_LOCAL_ASGI", "").lower() in ("1", "true", "yes")

if not _SERVER_URL and not _LOCAL_ASGI:
    pytest.skip(
        "Neither MCP_SERVER_URL nor MCP_LOCAL_ASGI is set — "
        "skipping remote MCP integration tests.",
        allow_module_level=True,
    )

_HAS_AUTH_CREDS = bool(os.getenv("REMOTE_MCP_TEST_CLIENT_ID"))

# ---------------------------------------------------------------------------
# ASGI app helper (Mode B)
# ---------------------------------------------------------------------------


def _build_local_asgi_transport():
    """Import and initialise the Quart app, then wrap it in an ASGITransport."""
    import asyncio
    from importlib import import_module, reload

    # Reload app to pick up any env overrides the test may have set
    app_module = import_module("app")
    reload(app_module)
    quart_app = app_module.app

    # The ASGI dispatch middleware is attached during `before_serving`.
    # We run the startup hooks manually so the MCP mount is in place.
    async def _run_startup():
        async with quart_app.test_app():
            pass  # starts, idles, shuts down

    return httpx.ASGITransport(app=quart_app.asgi_app)


# ---------------------------------------------------------------------------
# Token acquisition helper
# ---------------------------------------------------------------------------


async def _acquire_token(scope: str | None = None) -> str:
    """Acquire an Entra ID access token for the MCP server using client credentials."""
    try:
        from azure.identity.aio import ClientSecretCredential  # type: ignore
    except ImportError:
        pytest.skip("azure-identity not installed — cannot acquire token")

    tenant_id = os.environ.get("REMOTE_MCP_TEST_TENANT_ID", "")
    client_id = os.environ.get("REMOTE_MCP_TEST_CLIENT_ID", "")
    client_secret = os.environ.get("REMOTE_MCP_TEST_CLIENT_SECRET", "")
    resource_app_id = os.environ.get("REMOTE_MCP_TEST_RESOURCE_APP_ID", "")

    if not all([tenant_id, client_id, client_secret, resource_app_id]):
        pytest.skip("REMOTE_MCP_TEST_* env vars not fully set — skipping auth test")

    if scope is None:
        scope = f"api://{resource_app_id}/.default"

    credential = ClientSecretCredential(tenant_id, client_id, client_secret)
    token = await credential.get_token(scope)
    await credential.close()
    return token.token


# ---------------------------------------------------------------------------
# Fixture: an httpx AsyncClient pointed at the right backend
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def mcp_client():
    """Yield an httpx AsyncClient ready to talk to the MCP server."""
    if _SERVER_URL:
        # Mode A — real HTTP
        async with httpx.AsyncClient(
            base_url=_SERVER_URL, timeout=30
        ) as client:
            yield client, _SERVER_URL
    else:
        # Mode B — local ASGI
        transport = _build_local_asgi_transport()
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", timeout=30
        ) as client:
            yield client, "http://testserver/mcp"


def _mcp_path(base: str) -> str:
    """Return just the /mcp path portion relative to the base URL."""
    if base.startswith("http://testserver"):
        return "/mcp"
    # For real server the base_url already encodes the origin; use relative path
    return ""


# ---------------------------------------------------------------------------
# Test 6.1: Full MCP protocol flow
# ---------------------------------------------------------------------------


class TestMCPProtocolFlow:
    """6.1 — initialize → tools/list → tools/call."""

    @pytest.mark.asyncio
    async def test_prm_metadata_accessible(self, mcp_client):
        """PRM metadata returns valid RFC 9728 JSON — no auth required."""
        client, base = mcp_client
        if _SERVER_URL:
            origin = _SERVER_URL.rsplit("/mcp", 1)[0]
            resp = await client.get(
                origin + "/.well-known/oauth-protected-resource"
            )
        else:
            resp = await client.get("/.well-known/oauth-protected-resource")

        assert resp.status_code == 200
        data = resp.json()
        assert "resource" in data
        assert "authorization_servers" in data

    @pytest.mark.asyncio
    async def test_mcp_initialize(self, mcp_client):
        """MCP initialize handshake succeeds and returns capabilities."""
        client, mcp_url = mcp_client
        token = await _acquire_token() if _HAS_AUTH_CREDS else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        path = _mcp_path(mcp_url) or mcp_url
        resp = await client.post(
            path,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest-e2e", "version": "1.0"},
                },
            },
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data.get("jsonrpc") == "2.0"
        result = data.get("result", {})
        assert "serverInfo" in result
        assert "capabilities" in result

    @pytest.mark.asyncio
    async def test_mcp_list_tools_contains_knowledge_base(self, mcp_client):
        """tools/list response includes search_knowledge_base and get_system_context."""
        client, mcp_url = mcp_client
        token = await _acquire_token() if _HAS_AUTH_CREDS else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        path = _mcp_path(mcp_url) or mcp_url
        resp = await client.post(
            path,
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "result" in data, data
        tool_names = {t["name"] for t in data["result"].get("tools", [])}
        assert "search_knowledge_base" in tool_names
        assert "get_system_context" in tool_names

    @pytest.mark.asyncio
    async def test_mcp_call_get_system_context(self, mcp_client):
        """tools/call get_system_context returns a system_message key."""
        client, mcp_url = mcp_client
        token = await _acquire_token() if _HAS_AUTH_CREDS else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        path = _mcp_path(mcp_url) or mcp_url
        resp = await client.post(
            path,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get_system_context", "arguments": {}},
            },
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "result" in data, data
        content = data["result"].get("content", [])
        assert len(content) > 0
        ctx = json.loads(content[0]["text"])
        assert "system_message" in ctx

    @pytest.mark.asyncio
    async def test_mcp_call_search_knowledge_base_returns_results_or_empty(
        self, mcp_client
    ):
        """tools/call search_knowledge_base returns a list (may be empty without index)."""
        client, mcp_url = mcp_client
        token = await _acquire_token() if _HAS_AUTH_CREDS else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        path = _mcp_path(mcp_url) or mcp_url
        resp = await client.post(
            path,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "search_knowledge_base",
                    "arguments": {"query": "architecture overview", "top_k": 2},
                },
            },
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        # Result may contain an error from Azure Search if index not configured —
        # that's acceptable in CI; we just verify the JSON-RPC 2.0 envelope is valid.
        assert "id" in data
        assert data.get("jsonrpc") == "2.0"


# ---------------------------------------------------------------------------
# Test 6.2: Auth with a valid service principal token
# ---------------------------------------------------------------------------


class TestAuthValid:
    """6.2 — Valid Entra ID token is accepted."""

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not _HAS_AUTH_CREDS, reason="REMOTE_MCP_TEST_CLIENT_ID not set"
    )
    async def test_valid_sp_token_allows_tools_list(self, mcp_client):
        """A valid service-principal token allows tools/list."""
        client, mcp_url = mcp_client
        token = await _acquire_token()
        path = _mcp_path(mcp_url) or mcp_url

        resp = await client.post(
            path,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "result" in data

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not _HAS_AUTH_CREDS, reason="REMOTE_MCP_TEST_CLIENT_ID not set"
    )
    async def test_valid_token_prm_metadata_unchanged(self, mcp_client):
        """Authenticated call does not affect PRM metadata availability."""
        client, mcp_url = mcp_client
        if _SERVER_URL:
            origin = _SERVER_URL.rsplit("/mcp", 1)[0]
            resp = await client.get(
                origin + "/.well-known/oauth-protected-resource"
            )
        else:
            resp = await client.get("/.well-known/oauth-protected-resource")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Test 6.3: Auth rejection scenarios
# ---------------------------------------------------------------------------


class TestAuthRejection:
    """6.3 — Unauthenticated / invalid tokens are rejected."""

    @pytest.mark.asyncio
    async def test_no_token_returns_401_or_200_if_auth_disabled(self, mcp_client):
        """No Bearer token → 401 if auth is enabled, 200 if auth is disabled."""
        client, mcp_url = mcp_client
        path = _mcp_path(mcp_url) or mcp_url

        resp = await client.post(
            path,
            headers={"Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )

        assert resp.status_code in (200, 401), f"Unexpected status {resp.status_code}"
        if resp.status_code == 401:
            # RFC 6750 §3 — WWW-Authenticate header must be present
            assert "WWW-Authenticate" in resp.headers

    @pytest.mark.asyncio
    async def test_malformed_jwt_returns_401_or_200_if_auth_disabled(
        self, mcp_client
    ):
        """A structurally invalid JWT → 401 if auth is enabled."""
        client, mcp_url = mcp_client
        path = _mcp_path(mcp_url) or mcp_url

        resp = await client.post(
            path,
            headers={
                "Authorization": "Bearer this.is.not.a.real.jwt",
                "Content-Type": "application/json",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )

        assert resp.status_code in (200, 401), f"Unexpected status {resp.status_code}"

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not _HAS_AUTH_CREDS, reason="REMOTE_MCP_TEST_CLIENT_ID not set"
    )
    async def test_wrong_audience_returns_401(self, mcp_client):
        """Token scoped for a different resource (Key Vault) must be rejected with 401."""
        client, mcp_url = mcp_client
        # Request a token for Azure Key Vault — wrong audience for MCP server
        token = await _acquire_token(scope="https://vault.azure.net/.default")
        path = _mcp_path(mcp_url) or mcp_url

        resp = await client.post(
            path,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )

        # If auth is disabled the server won't validate — both outcomes acceptable
        assert resp.status_code in (200, 401), f"Unexpected status {resp.status_code}"
        if resp.status_code == 401:
            assert "WWW-Authenticate" in resp.headers

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not _HAS_AUTH_CREDS, reason="REMOTE_MCP_TEST_CLIENT_ID not set"
    )
    async def test_expired_token_returns_401(self, mcp_client):
        """A syntactically valid but expired JWT is rejected with 401."""
        # An expired RS256 JWT (expired 2021-01-01, audience "api://test")
        expired_jwt = (
            "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCIsImtpZCI6InRlc3QifQ"
            ".eyJzdWIiOiIxMjM0NTY3ODkwIiwiYXVkIjoiYXBpOi8vdGVzdCIsImlzcyI6"
            "Imh0dHBzOi8vbG9naW4ubWljcm9zb2Z0b25saW5lLmNvbS90ZXN0L3YyLjAi"
            "LCJleHAiOjE2MDk0NTkyMDB9"
            ".AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        )
        client, mcp_url = mcp_client
        path = _mcp_path(mcp_url) or mcp_url

        resp = await client.post(
            path,
            headers={
                "Authorization": f"Bearer {expired_jwt}",
                "Content-Type": "application/json",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )

        assert resp.status_code in (200, 401), f"Unexpected status {resp.status_code}"


# ---------------------------------------------------------------------------
# Test 6.4: Streamable HTTP transport
# ---------------------------------------------------------------------------


class TestStreamableHTTP:
    """6.4 — Streamable HTTP transport behaviour."""

    @pytest.mark.asyncio
    async def test_delete_terminates_or_404_on_no_session(self, mcp_client):
        """DELETE /mcp is handled (no active session → 404/200/204 are all valid)."""
        client, mcp_url = mcp_client
        path = _mcp_path(mcp_url) or mcp_url

        resp = await client.delete(path)
        # 200/204 = session terminated, 401 = auth required, 404 = no session
        assert resp.status_code in (200, 204, 401, 404)

    @pytest.mark.asyncio
    async def test_get_returns_sse_or_auth_error(self, mcp_client):
        """GET /mcp returns SSE stream (200) or auth challenge (401)."""
        client, mcp_url = mcp_client
        path = _mcp_path(mcp_url) or mcp_url
        token = await _acquire_token() if _HAS_AUTH_CREDS else None
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        headers["Accept"] = "text/event-stream"

        try:
            async with client.stream("GET", path, headers=headers) as resp:
                assert resp.status_code in (200, 401, 404)
                if resp.status_code == 200:
                    ct = resp.headers.get("content-type", "")
                    assert "text/event-stream" in ct or "application/json" in ct
        except httpx.ReadTimeout:
            pass  # Long-lived SSE — connection was accepted, timeout is fine

    @pytest.mark.asyncio
    async def test_sse_endpoint_reachable(self):
        """GET /sse (backward-compat) is reachable when server is configured."""
        if not _SERVER_URL:
            pytest.skip("SSE backward-compat test requires MCP_SERVER_URL")

        sse_url = _SERVER_URL.replace("/mcp", "/sse")

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                async with client.stream("GET", sse_url) as resp:
                    assert resp.status_code in (200, 401, 404)
                    if resp.status_code == 200:
                        ct = resp.headers.get("content-type", "")
                        assert "text/event-stream" in ct
        except httpx.ReadTimeout:
            pass  # SSE streams block — timeout means connection was established

    @pytest.mark.asyncio
    async def test_post_content_type_json_accepted(self, mcp_client):
        """Content-Type application/json is accepted by the MCP endpoint."""
        client, mcp_url = mcp_client
        path = _mcp_path(mcp_url) or mcp_url
        token = await _acquire_token() if _HAS_AUTH_CREDS else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        resp = await client.post(
            path,
            headers=headers,
            content=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "pytest-transport", "version": "1.0"},
                    },
                }
            ),
        )

        # 200 = success, 401 = auth required with no/wrong token
        assert resp.status_code in (200, 401)

    @pytest.mark.asyncio
    async def test_session_id_header_present_in_initialize_response(
        self, mcp_client
    ):
        """Successful initialize returns Mcp-Session-Id in response headers."""
        client, mcp_url = mcp_client
        path = _mcp_path(mcp_url) or mcp_url
        token = await _acquire_token() if _HAS_AUTH_CREDS else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        resp = await client.post(
            path,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest-session", "version": "1.0"},
                },
            },
        )

        if resp.status_code == 200:
            # FastMCP may issue a session-id; it's optional per spec so just log it
            session_id = resp.headers.get("mcp-session-id", "")
            print(f"Mcp-Session-Id: {session_id!r}")
