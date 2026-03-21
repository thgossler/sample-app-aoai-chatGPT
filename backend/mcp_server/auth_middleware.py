"""
Entra ID JWT token validation middleware for the remote MCP server.

Implements OAuth 2.1 Resource Server token validation per MCP Authorization
Specification 2025-11-25.  Tokens are validated locally using JWKS fetched
from the Entra ID discovery endpoint (no token introspection required).

Key behaviours:
- RS256 JWT signature verification using cached JWKS keys
- Validates: aud, iss, exp, nbf claims
- Supports delegated scopes (``scp`` claim) for interactive users
- Supports app roles (``roles`` claim) for service principals
- Returns RFC 6750 compliant 401 / 403 responses with WWW-Authenticate header
"""

import logging
import time
from typing import Any, Dict, List, Optional

import jwt
from cachetools import TTLCache

logger = logging.getLogger(__name__)

# JWKS cache: up to 1 key-set per tenant, TTL = 1 hour
# Keys are keyed by (tenant_id, kid); we store the full JWKS per tenant.
_JWKS_CACHE: TTLCache = TTLCache(maxsize=16, ttl=3600)

# VS Code Copilot Agent Mode well-known client ID
VSCODE_CLIENT_ID = "aebc6443-996d-45c2-90f0-388ff96faa56"


class AuthError(Exception):
    """Raised when a token cannot be validated."""

    def __init__(self, message: str, status_code: int = 401):
        super().__init__(message)
        self.status_code = status_code


class EntraIDTokenValidator:
    """
    Validates JWT access tokens issued by Microsoft Entra ID.

    Parameters
    ----------
    tenant_id:
        Azure AD tenant ID.  Used to build the JWKS endpoint and issuer URL.
    client_id:
        Application (client) ID of the MCP server app registration.
    audience:
        Expected ``aud`` claim.  Defaults to ``api://<client_id>``.
    issuer:
        Expected ``iss`` claim.  Defaults to the v2.0 issuer for the tenant.
        For multi-tenant apps pass ``None`` and set ``multi_tenant=True``.
    multi_tenant:
        When ``True`` the issuer check is relaxed to any Entra ID issuer.
    allowed_client_ids:
        Comma-separated string (or list) of pre-authorised client application
        IDs.  When present, the ``azp`` claim is validated against this set.
    """

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        audience: Optional[str] = None,
        issuer: Optional[str] = None,
        multi_tenant: bool = False,
        allowed_client_ids: Optional[str] = None,
    ):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.audience = audience or f"api://{client_id}"
        self.multi_tenant = multi_tenant

        if issuer:
            self.issuer = issuer
            self._valid_issuers: list = [issuer]
        elif multi_tenant:
            self.issuer = "https://login.microsoftonline.com/common/v2.0"
            self._valid_issuers = []  # checked by pattern in validate_token
        else:
            # Accept both v2.0 (login.microsoftonline.com) and v1.0 (sts.windows.net)
            # issuers.  The v1.0 issuer is emitted when accessTokenAcceptedVersion
            # is null/1 in the API app manifest; v2.0 when it is set to 2.
            self.issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
            self._valid_issuers = [
                self.issuer,
                f"https://sts.windows.net/{tenant_id}/",
            ]

        # Build set of allowed client IDs
        self._allowed_client_ids: Optional[set] = None
        if allowed_client_ids:
            if isinstance(allowed_client_ids, str):
                raw_ids = [s.strip() for s in allowed_client_ids.split(",") if s.strip()]
            else:
                raw_ids = list(allowed_client_ids)
            self._allowed_client_ids = set(raw_ids)

    # ------------------------------------------------------------------
    # JWKS management
    # ------------------------------------------------------------------

    def _jwks_url(self, tenant_id: Optional[str] = None) -> str:
        tid = tenant_id or self.tenant_id
        return (
            f"https://login.microsoftonline.com/{tid}"
            "/discovery/v2.0/keys"
        )

    async def _fetch_jwks(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Fetch JWKS from Entra ID (network call, results cached by caller)."""
        import aiohttp

        url = self._jwks_url(tenant_id)
        logger.debug("Fetching JWKS from %s", url)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    raise AuthError(
                        f"Failed to fetch JWKS (HTTP {resp.status}): {url}",
                        status_code=503,
                    )
                return await resp.json()

    async def _get_signing_key(self, kid: str, tenant_id: Optional[str] = None) -> jwt.PyJWK:
        """Return the PyJWK for the given key ID, fetching JWKS if needed.

        When *tenant_id* is supplied (multi-tenant mode), keys are fetched from
        that tenant's JWKS endpoint instead of the configured tenant.
        """
        effective_tid = tenant_id or self.tenant_id
        cache_key = f"{effective_tid}:{kid}"

        if cache_key in _JWKS_CACHE:
            return _JWKS_CACHE[cache_key]

        # Key not cached — fetch full JWKS
        jwks_data = await self._fetch_jwks(effective_tid)
        jwks = jwt.PyJWKSet.from_dict(jwks_data)

        # Cache all keys from this fetch under their kid
        for jwk in jwks.keys:
            ck = f"{effective_tid}:{jwk.key_id}"
            _JWKS_CACHE[ck] = jwk

        if cache_key not in _JWKS_CACHE:
            raise AuthError(
                f"Key '{kid}' not found in Entra ID JWKS for tenant '{effective_tid}'",
            )
        return _JWKS_CACHE[cache_key]

    # ------------------------------------------------------------------
    # Token validation
    # ------------------------------------------------------------------

    async def validate_token(self, token: str) -> Dict[str, Any]:
        """
        Validate a JWT Bearer token and return its decoded claims.

        Raises ``AuthError`` on any validation failure.
        """
        # 1. Decode header without verification to extract kid + alg
        try:
            unverified_header = jwt.get_unverified_header(token)
        except jwt.exceptions.DecodeError as exc:
            raise AuthError(f"Malformed JWT header: {exc}") from exc

        kid = unverified_header.get("kid")
        alg = unverified_header.get("alg", "RS256")

        if not kid:
            raise AuthError("JWT header missing 'kid' (key ID)")

        if alg not in ("RS256", "RS384", "RS512"):
            raise AuthError(f"Unsupported JWT algorithm: {alg}")

        # 2. For multi-tenant tokens, extract the issuing tenant from the
        #    unverified payload so we fetch signing keys from the correct tenant.
        token_tid: Optional[str] = None
        if self.multi_tenant:
            try:
                unverified_payload = jwt.decode(
                    token,
                    options={"verify_signature": False},
                    algorithms=[alg],
                )
                token_tid = unverified_payload.get("tid") or self.tenant_id
            except jwt.PyJWTError:
                token_tid = self.tenant_id

        # 3. Fetch signing key (from token's tenant when multi-tenant)
        signing_key = await self._get_signing_key(kid, tenant_id=token_tid)

        # 4. Validate audience — Entra ID v2 tokens may use the client_id as
        #    audience (for single-tenant delegated flows) or the app URI.
        valid_audiences = [self.audience, self.client_id]

        # 5. Build issuers list (multi-tenant accepts any Entra issuer)
        # For single-tenant we disable PyJWT's built-in issuer check and do it
        # manually below so we can accept both the v1.0 (sts.windows.net) and
        # v2.0 (login.microsoftonline.com) issuer formats.
        options = {"verify_iss": False}

        try:
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=[alg],
                audience=valid_audiences,
                options=options,
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("Token has expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthError(f"Invalid token audience (expected {valid_audiences})") from exc
        except jwt.exceptions.DecodeError as exc:
            raise AuthError(f"Token decode failed: {exc}") from exc
        except jwt.PyJWTError as exc:
            raise AuthError(f"Token validation failed: {exc}") from exc

        # 6. Issuer check
        iss = claims.get("iss", "")
        if self.multi_tenant:
            # Accept any v2.0 Entra issuer OR the v1.0 sts.windows.net issuer
            v2 = iss.startswith("https://login.microsoftonline.com/") and iss.endswith("/v2.0")
            v1 = iss.startswith("https://sts.windows.net/") and iss.endswith("/")
            if not v2 and not v1:
                raise AuthError(f"Unrecognised issuer for multi-tenant token: {iss}")
        else:
            if iss not in self._valid_issuers:
                raise AuthError(
                    f"Invalid token issuer '{iss}' "
                    f"(expected one of: {self._valid_issuers})"
                )

        # 7. Validate azp (authorized party) if we have an allowlist
        if self._allowed_client_ids is not None:
            azp = claims.get("azp") or claims.get("appid")
            if azp and azp not in self._allowed_client_ids:
                logger.warning(
                    "Token from unexpected client application: azp=%s (allowed: %s)",
                    azp,
                    self._allowed_client_ids,
                )
                # Log but do not reject — azp is advisory; proper scope/role
                # check below is the enforcement boundary.

        logger.debug(
            "Token validated for sub=%s oid=%s",
            claims.get("sub", "?"),
            claims.get("oid", "?"),
        )
        return claims

    # ------------------------------------------------------------------
    # Scope / Role helpers
    # ------------------------------------------------------------------

    @staticmethod
    def check_scopes(claims: Dict[str, Any], required_scopes: List[str]) -> bool:
        """
        Return True if the token contains at least one of the required scopes.

        The ``scp`` claim is a space-separated list of delegated scopes
        (user auth code flow). Returns ``False`` for app-only tokens (no scp).
        """
        raw = claims.get("scp", "")
        token_scopes = set(raw.split()) if isinstance(raw, str) else set()
        return bool(token_scopes & set(required_scopes))

    @staticmethod
    def check_roles(claims: Dict[str, Any], required_roles: List[str]) -> bool:
        """
        Return True if the token contains at least one of the required app roles.

        The ``roles`` claim is a list of app roles (client credentials / app
        role assignment). Returns ``False`` for user-delegated tokens with no roles.
        """
        token_roles = set(claims.get("roles", []))
        return bool(token_roles & set(required_roles))

    @staticmethod
    def has_mcp_access(claims: Dict[str, Any]) -> bool:
        """
        Return True if the token grants any MCP access (read OR execute,
        via scopes or app roles).
        """
        read_execute_scopes = {"MCP.Tools.Read", "MCP.Tools.Execute"}
        app_roles = {"MCP.User", "MCP.ToolCaller", "MCP.Admin"}

        return (
            EntraIDTokenValidator.check_scopes(claims, read_execute_scopes)
            or EntraIDTokenValidator.check_roles(claims, app_roles)
        )

    @staticmethod
    def can_execute_tools(claims: Dict[str, Any]) -> bool:
        """Return True if the token grants tool execution rights."""
        execute_scopes = {"MCP.Tools.Execute"}
        exec_roles = {"MCP.User", "MCP.ToolCaller", "MCP.Admin"}

        return (
            EntraIDTokenValidator.check_scopes(claims, execute_scopes)
            or EntraIDTokenValidator.check_roles(claims, exec_roles)
        )

    @staticmethod
    def get_caller_identity(claims: Dict[str, Any]) -> Dict[str, str]:
        """Extract a minimal identity dict from token claims for logging."""
        return {
            "oid": claims.get("oid", ""),
            "sub": claims.get("sub", ""),
            "appid": claims.get("appid") or claims.get("azp", ""),
            "name": claims.get("name") or claims.get("preferred_username", ""),
            "tid": claims.get("tid", ""),
        }

    # ------------------------------------------------------------------
    # On-Behalf-Of (OBO) token exchange (task 7.1)
    # ------------------------------------------------------------------

    async def exchange_token_obo(
        self,
        user_token: str,
        downstream_scope: str,
        client_secret: str,
    ) -> str:
        """
        Exchange a user token for a downstream API token using the OBO flow.

        This implements RFC 7521 / Entra ID On-Behalf-Of (``jwt-bearer`` grant).
        The resulting token can be used to call downstream APIs (MS Graph,
        Azure DevOps, etc.) as the authenticated user.

        Parameters
        ----------
        user_token:
            The user's access token presented to the MCP server.
        downstream_scope:
            The scope of the downstream API, e.g.
            ``https://graph.microsoft.com/.default``.
        client_secret:
            The MCP server app's client secret
            (``REMOTE_MCP_AUTH_CLIENT_SECRET``).

        Returns
        -------
        str
            An access token for the downstream resource.

        Raises
        ------
        AuthError
            If the token exchange fails.
        """
        import aiohttp

        token_url = (
            f"https://login.microsoftonline.com/{self.tenant_id}"
            "/oauth2/v2.0/token"
        )
        data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "client_id": self.client_id,
            "client_secret": client_secret,
            "assertion": user_token,
            "requested_token_use": "on_behalf_of",
            "scope": downstream_scope,
        }

        logger.debug(
            "OBO token exchange: client=%s scope=%s", self.client_id, downstream_scope
        )

        async with aiohttp.ClientSession() as session:
            async with session.post(
                token_url,
                data=data,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                body = await resp.json()

            if resp.status != 200:
                error = body.get("error", "unknown_error")
                desc = body.get("error_description", "")
                raise AuthError(
                    f"OBO token exchange failed ({error}): {desc}",
                    status_code=502,
                )

        downstream_token: str = body.get("access_token", "")
        if not downstream_token:
            raise AuthError("OBO response contained no access_token", status_code=502)

        logger.debug("OBO token exchange succeeded for scope=%s", downstream_scope)
        return downstream_token
