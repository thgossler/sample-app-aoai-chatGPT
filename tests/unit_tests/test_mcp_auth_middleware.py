"""
Unit tests for ``backend.mcp_server.auth_middleware``.

Tests cover:
- Valid JWT acceptance
- Expired token rejection
- Wrong audience rejection
- Wrong issuer rejection
- Invalid signature rejection
- Missing kid in header
- Scope checking (scp claim)
- Role checking (roles claim)
- has_mcp_access / can_execute_tools helpers
- get_caller_identity helper
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend
import jwt

from backend.mcp_server.auth_middleware import AuthError, EntraIDTokenValidator

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

TENANT_ID = "test-tenant-id"
CLIENT_ID = "test-client-id"
AUDIENCE = f"api://{CLIENT_ID}"
ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
KID = "test-key-id"


@pytest.fixture(scope="module")
def rsa_private_key():
    return rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )


@pytest.fixture(scope="module")
def rsa_public_key(rsa_private_key):
    return rsa_private_key.public_key()


def _make_token(
    private_key,
    *,
    aud=AUDIENCE,
    iss=ISSUER,
    sub="test-user",
    exp_delta=3600,
    scp=None,
    roles=None,
    azp=None,
    kid=KID,
    algorithm="RS256",
):
    now = int(time.time())
    payload = {
        "aud": aud,
        "iss": iss,
        "sub": sub,
        "oid": "object-id-123",
        "tid": TENANT_ID,
        "iat": now,
        "nbf": now,
        "exp": now + exp_delta,
    }
    if scp:
        payload["scp"] = scp
    if roles:
        payload["roles"] = roles
    if azp:
        payload["azp"] = azp

    return jwt.encode(
        payload,
        private_key,
        algorithm=algorithm,
        headers={"kid": kid},
    )


def _make_jwk_set(public_key, kid=KID):
    """Return a mock PyJWKSet-like object."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    pub_pem = public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    jwk_key = MagicMock()
    jwk_key.key_id = kid
    jwk_key.key = public_key  # PyJWT accepts a cryptography public key
    return [jwk_key]


# ---------------------------------------------------------------------------
# EntraIDTokenValidator tests
# ---------------------------------------------------------------------------

class TestEntraIDTokenValidator:
    """Tests for JWT validation logic."""

    def _make_validator(self, **kwargs):
        return EntraIDTokenValidator(
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            **kwargs,
        )

    async def _validate_with_mock_jwks(self, validator, token, public_key):
        """Patch JWKS fetch to return our test key."""
        from cachetools import TTLCache
        import backend.mcp_server.auth_middleware as mod

        # Clear cache to force JWKS fetch
        mod._JWKS_CACHE.clear()

        mock_jwk = MagicMock()
        mock_jwk.key_id = KID
        mock_jwk.key = public_key

        mock_jwks = MagicMock()
        mock_jwks.keys = [mock_jwk]

        with patch.object(validator, "_fetch_jwks", AsyncMock(return_value={})):
            with patch("jwt.PyJWKSet.from_dict", return_value=mock_jwks):
                return await validator.validate_token(token)

    @pytest.mark.asyncio
    async def test_valid_token_accepted(self, rsa_private_key, rsa_public_key):
        token = _make_token(rsa_private_key, scp="MCP.Tools.Execute")
        validator = self._make_validator()
        claims = await self._validate_with_mock_jwks(validator, token, rsa_public_key)
        assert claims["sub"] == "test-user"
        assert claims["aud"] == AUDIENCE

    @pytest.mark.asyncio
    async def test_expired_token_rejected(self, rsa_private_key, rsa_public_key):
        token = _make_token(rsa_private_key, exp_delta=-10)
        validator = self._make_validator()
        with pytest.raises(AuthError, match="expired"):
            await self._validate_with_mock_jwks(validator, token, rsa_public_key)

    @pytest.mark.asyncio
    async def test_wrong_audience_rejected(self, rsa_private_key, rsa_public_key):
        token = _make_token(rsa_private_key, aud="api://wrong-id")
        validator = self._make_validator()
        with pytest.raises(AuthError, match="[Aa]udience|[Ii]nvalid"):
            await self._validate_with_mock_jwks(validator, token, rsa_public_key)

    @pytest.mark.asyncio
    async def test_wrong_issuer_rejected(self, rsa_private_key, rsa_public_key):
        token = _make_token(rsa_private_key, iss="https://evil.example.com/v2.0")
        validator = self._make_validator()
        with pytest.raises(AuthError):
            await self._validate_with_mock_jwks(validator, token, rsa_public_key)

    @pytest.mark.asyncio
    async def test_missing_kid_in_header(self, rsa_private_key):
        # Encode without kid header
        payload = {"aud": AUDIENCE, "iss": ISSUER, "exp": int(time.time()) + 3600}
        token = jwt.encode(payload, rsa_private_key, algorithm="RS256")
        validator = self._make_validator()
        with pytest.raises(AuthError, match="kid"):
            await validator.validate_token(token)

    @pytest.mark.asyncio
    async def test_malformed_jwt_rejected(self):
        validator = self._make_validator()
        with pytest.raises(AuthError):
            await validator.validate_token("not.a.jwt")


class TestScopeAndRoleChecks:
    """Tests for scp/roles claim helpers."""

    def test_check_scopes_single_match(self):
        claims = {"scp": "openid profile MCP.Tools.Execute"}
        assert EntraIDTokenValidator.check_scopes(claims, ["MCP.Tools.Execute"]) is True

    def test_check_scopes_no_match(self):
        claims = {"scp": "openid profile"}
        assert EntraIDTokenValidator.check_scopes(claims, ["MCP.Tools.Execute"]) is False

    def test_check_scopes_any_match(self):
        claims = {"scp": "MCP.Tools.Read"}
        assert EntraIDTokenValidator.check_scopes(
            claims, ["MCP.Tools.Read", "MCP.Tools.Execute"]
        ) is True

    def test_check_scopes_empty_token(self):
        claims = {}
        assert EntraIDTokenValidator.check_scopes(claims, ["MCP.Tools.Execute"]) is False

    def test_check_roles_match(self):
        claims = {"roles": ["MCP.Admin"]}
        assert EntraIDTokenValidator.check_roles(claims, ["MCP.Admin"]) is True

    def test_check_roles_no_match(self):
        claims = {"roles": ["SomeOtherRole"]}
        assert EntraIDTokenValidator.check_roles(claims, ["MCP.ToolCaller"]) is False

    def test_check_roles_empty(self):
        claims = {}
        assert EntraIDTokenValidator.check_roles(claims, ["MCP.Admin"]) is False

    def test_has_mcp_access_via_scope(self):
        claims = {"scp": "MCP.Tools.Read"}
        assert EntraIDTokenValidator.has_mcp_access(claims) is True

    def test_has_mcp_access_via_role(self):
        claims = {"roles": ["MCP.ToolCaller"]}
        assert EntraIDTokenValidator.has_mcp_access(claims) is True

    def test_has_mcp_access_denied(self):
        claims = {"scp": "openid", "roles": []}
        assert EntraIDTokenValidator.has_mcp_access(claims) is False

    def test_can_execute_tools_via_scope(self):
        claims = {"scp": "MCP.Tools.Execute"}
        assert EntraIDTokenValidator.can_execute_tools(claims) is True

    def test_can_execute_tools_read_only_denied(self):
        claims = {"scp": "MCP.Tools.Read"}
        assert EntraIDTokenValidator.can_execute_tools(claims) is False

    def test_can_execute_tools_via_role(self):
        claims = {"roles": ["MCP.User"]}
        assert EntraIDTokenValidator.can_execute_tools(claims) is True

    def test_get_caller_identity(self):
        claims = {
            "oid": "oid-123",
            "sub": "sub-456",
            "azp": "azp-789",
            "name": "Test User",
            "tid": "tid-abc",
        }
        identity = EntraIDTokenValidator.get_caller_identity(claims)
        assert identity["oid"] == "oid-123"
        assert identity["sub"] == "sub-456"
        assert identity["name"] == "Test User"


# ---------------------------------------------------------------------------
# Multi-tenant JWKS fetch (task 7.5)
# ---------------------------------------------------------------------------


class TestMultiTenantJWKS:
    """Verify that multi-tenant mode fetches signing keys from the token's tenant."""

    def test_jwks_url_uses_supplied_tenant_id(self):
        """_jwks_url() uses the tenant_id argument when provided."""
        v = EntraIDTokenValidator(TENANT_ID, CLIENT_ID)
        url = v._jwks_url("other-tenant")
        assert "other-tenant" in url
        assert TENANT_ID not in url

    def test_jwks_url_falls_back_to_self_tenant(self):
        """_jwks_url() falls back to self.tenant_id when no argument given."""
        v = EntraIDTokenValidator(TENANT_ID, CLIENT_ID)
        url = v._jwks_url()
        assert TENANT_ID in url

    @pytest.mark.asyncio
    async def test_get_signing_key_uses_effective_tenant(self, rsa_public_key):
        """_get_signing_key(kid, tenant_id) caches and fetches under that tenant."""
        import backend.mcp_server.auth_middleware as mod

        mod._JWKS_CACHE.clear()

        mock_jwk = MagicMock()
        mock_jwk.key_id = KID
        mock_jwk.key = rsa_public_key
        mock_jwks = MagicMock()
        mock_jwks.keys = [mock_jwk]

        validator = EntraIDTokenValidator(TENANT_ID, CLIENT_ID)
        fetched_tids = []

        async def _fake_fetch(tenant_id=None):
            fetched_tids.append(tenant_id)
            return {}

        with patch.object(validator, "_fetch_jwks", side_effect=_fake_fetch):
            with patch("jwt.PyJWKSet.from_dict", return_value=mock_jwks):
                key = await validator._get_signing_key(KID, tenant_id="ext-tenant")

        assert fetched_tids == ["ext-tenant"]
        assert key.key == rsa_public_key
        # Cached under ext-tenant, not the default
        assert f"ext-tenant:{KID}" in mod._JWKS_CACHE
        assert f"{TENANT_ID}:{KID}" not in mod._JWKS_CACHE

    @pytest.mark.asyncio
    async def test_get_signing_key_falls_back_to_self_tenant(self, rsa_public_key):
        """_get_signing_key without tenant_id uses self.tenant_id."""
        import backend.mcp_server.auth_middleware as mod

        mod._JWKS_CACHE.clear()

        mock_jwk = MagicMock()
        mock_jwk.key_id = KID
        mock_jwk.key = rsa_public_key
        mock_jwks = MagicMock()
        mock_jwks.keys = [mock_jwk]

        validator = EntraIDTokenValidator(TENANT_ID, CLIENT_ID)
        fetched_tids = []

        async def _fake_fetch(tenant_id=None):
            fetched_tids.append(tenant_id)
            return {}

        with patch.object(validator, "_fetch_jwks", side_effect=_fake_fetch):
            with patch("jwt.PyJWKSet.from_dict", return_value=mock_jwks):
                await validator._get_signing_key(KID)

        assert fetched_tids == [TENANT_ID]


# ---------------------------------------------------------------------------
# OBO token exchange (task 7.1)
# ---------------------------------------------------------------------------


class TestOBOTokenExchange:
    """Unit tests for exchange_token_obo()."""

    @pytest.mark.asyncio
    async def test_obo_returns_access_token_on_success(self):
        """Successful OBO exchange returns the downstream access token."""
        validator = EntraIDTokenValidator(TENANT_ID, CLIENT_ID)

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value={"access_token": "downstream-token-abc"}
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            token = await validator.exchange_token_obo(
                user_token="user-jwt",
                downstream_scope="https://graph.microsoft.com/.default",
                client_secret="secret",
            )

        assert token == "downstream-token-abc"

    @pytest.mark.asyncio
    async def test_obo_raises_auth_error_on_non_200(self):
        """A non-200 response raises AuthError(502)."""
        validator = EntraIDTokenValidator(TENANT_ID, CLIENT_ID)

        mock_response = MagicMock()
        mock_response.status = 400
        mock_response.json = AsyncMock(
            return_value={
                "error": "invalid_grant",
                "error_description": "AADSTS70011: Invalid scope",
            }
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(AuthError) as exc_info:
                await validator.exchange_token_obo(
                    user_token="user-jwt",
                    downstream_scope="https://graph.microsoft.com/.default",
                    client_secret="secret",
                )
        assert exc_info.value.status_code == 502
        assert "invalid_grant" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_obo_raises_auth_error_when_no_access_token(self):
        """OBO response with no access_token raises AuthError."""
        validator = EntraIDTokenValidator(TENANT_ID, CLIENT_ID)

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(AuthError) as exc_info:
                await validator.exchange_token_obo(
                    user_token="user-jwt",
                    downstream_scope="https://graph.microsoft.com/.default",
                    client_secret="secret",
                )
        assert exc_info.value.status_code == 502

    @pytest.mark.asyncio
    async def test_obo_posts_correct_grant_type(self):
        """exchange_token_obo uses jwt-bearer grant type and on_behalf_of."""
        validator = EntraIDTokenValidator(TENANT_ID, CLIENT_ID)

        posted_data = {}

        def _fake_post(url, data=None, **kwargs):
            posted_data.update(data or {})
            resp = MagicMock()
            resp.status = 200
            resp.json = AsyncMock(return_value={"access_token": "tok"})
            resp.__aenter__ = AsyncMock(return_value=resp)
            resp.__aexit__ = AsyncMock(return_value=False)
            return resp

        mock_session = MagicMock()
        mock_session.post = _fake_post
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            await validator.exchange_token_obo(
                user_token="my-user-token",
                downstream_scope="https://graph.microsoft.com/.default",
                client_secret="my-secret",
            )

        assert posted_data["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
        assert posted_data["assertion"] == "my-user-token"
        assert posted_data["requested_token_use"] == "on_behalf_of"
        assert posted_data["client_id"] == CLIENT_ID
        assert posted_data["client_secret"] == "my-secret"
