"""
Remote MCP Server — Streamable HTTP transport with Entra ID auth.

This module creates a FastMCP server that:

1. Exposes all tools registered in ``MCPServerManager`` (SW360, Azure
   Functions, etc.) as remote MCP tools.
2. Adds the ``search_knowledge_base`` and ``get_system_context`` tools that
   replicate the web-chat knowledge-base experience.
3. Exposes MCP resources (system message, knowledge-base info, citation config)
   and a reusable ``search-and-answer`` prompt template.
4. Validates every request with ``EntraIDTokenValidator`` before any tool is
   executed.
5. Returns RFC 6750-compliant 401 / 403 responses for auth failures.

Transport: MCP Streamable HTTP (POST/GET/DELETE /mcp)
          Backward-compat SSE endpoints (/sse, /messages) for older clients
Protocol:  MCP 2025-11-25
Auth:      OAuth 2.1 Resource Server (Entra ID as AS)

Usage (from app.py)
-------------------
    from backend.mcp_server.remote_mcp_server import RemoteMCPServer

    remote_mcp = RemoteMCPServer(
        app_settings=app_settings,
        mcp_manager=mcp_manager,
        rag_retriever=rag_retriever,   # may be None
        citation_resolver=citation_resolver,
    )
    await remote_mcp.initialize()

    # Mount the ASGI app onto the Quart app
    app.mount("/mcp", remote_mcp.asgi_app)
    app.register_blueprint(remote_mcp.blueprint)
"""

import json
import logging
import time
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from backend.mcp_server.auth_middleware import AuthError, EntraIDTokenValidator
from backend.mcp_server.citation_resolver import CitationLinkResolver
from backend.mcp_server.knowledge_base_tool import (
    get_system_context,
    search_knowledge_base,
)
from backend.mcp_server.prm_metadata import build_prm_metadata

logger = logging.getLogger(__name__)
_audit_logger = logging.getLogger("remote_mcp.audit")

# Request-scoped caller identity populated by _MCPAuthStarletteMiddleware.
# Tool functions read this to include caller info in audit log entries.
_caller_context: ContextVar[Dict[str, Any]] = ContextVar(
    "mcp_caller_context", default={}
)


class _SecurityHeadersMiddleware:
    """Pure ASGI middleware that adds security headers to every response."""

    HEADERS = [
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"strict-origin-when-cross-origin"),
        (b"permissions-policy", b"geolocation=(), camera=(), microphone=()"),
    ]

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(self.HEADERS)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class _MCPAuthStarletteMiddleware(BaseHTTPMiddleware):
    """
    Starlette middleware that validates Entra ID Bearer tokens on MCP requests.

    Injected into the FastMCP ``http_app()`` middleware stack so that auth is
    enforced inside the Starlette ASGI app rather than in the Quart route layer.
    """

    def __init__(
        self,
        app,
        validator: Optional[EntraIDTokenValidator] = None,
        server_url: str = "",
    ):
        super().__init__(app)
        self._validator = validator
        self._server_url = server_url

    async def dispatch(
        self, request: StarletteRequest, call_next
    ):
        if self._validator is None:
            return await call_next(request)

        auth_header = request.headers.get("authorization") or request.headers.get(
            "Authorization"
        )
        try:
            claims = await self._validator.validate_token(
                auth_header.split(" ", 1)[1]
                if auth_header and auth_header.lower().startswith("bearer ")
                else ""
            )
            if not EntraIDTokenValidator.has_mcp_access(claims):
                return JSONResponse(
                    {"error": "Forbidden", "detail": "Insufficient MCP scope/role"},
                    status_code=403,
                )
            # Propagate caller identity to tool functions via ContextVar
            _caller_context.set(EntraIDTokenValidator.get_caller_identity(claims))
        except AuthError as exc:
            if exc.status_code == 401:
                # The PRM metadata endpoint is served by Quart at the
                # app root, not under /mcp/.  Strip the MCP path so the
                # URL points to the unauthenticated Quart route.
                base_url = (
                    self._server_url.rsplit("/mcp", 1)[0]
                    if self._server_url
                    else ""
                )
                prm_url = (
                    f"{base_url}/.well-known/oauth-protected-resource"
                    if base_url
                    else ""
                )
                www_auth = 'Bearer realm="Remote MCP Server"'
                if prm_url:
                    www_auth += f', resource_metadata="{prm_url}"'
                return JSONResponse(
                    {"error": "Unauthorized", "detail": str(exc)},
                    status_code=401,
                    headers={"WWW-Authenticate": www_auth},
                )
            return JSONResponse(
                {"error": "Forbidden", "detail": str(exc)}, status_code=403
            )

        return await call_next(request)


class RemoteMCPServer:
    """
    Wraps FastMCP to provide a Streamable-HTTP MCP server with Entra ID auth.

    Attributes
    ----------
    mcp:
        The underlying ``FastMCP`` instance.
    blueprint:
        A Quart Blueprint that adds the
        ``GET /.well-known/oauth-protected-resource`` endpoint to the main app.
    """

    def __init__(
        self,
        app_settings,
        mcp_manager=None,
        rag_retriever=None,
        citation_resolver: Optional[CitationLinkResolver] = None,
    ):
        self._settings = app_settings
        self._mcp_manager = mcp_manager
        self._rag_retriever = rag_retriever
        self._citation_resolver = citation_resolver
        self._validator: Optional[EntraIDTokenValidator] = None

        mcp_cfg = getattr(app_settings, "remote_mcp_server", None)
        self._mcp_cfg = mcp_cfg

        server_name = getattr(app_settings.ui, "title", "Chat App") if app_settings.ui else "Chat App"
        # Use stateless HTTP mode so the server does not rely on the
        # Mcp-Session-Id response header surviving through reverse proxies
        # (Azure App Service / EasyAuth can strip custom headers).
        self.mcp = FastMCP(name=f"{server_name} MCP Server", stateless_http=True)

        self._initialized = False

        # Tool-level RBAC registry: maps tool_name → list of required roles/scopes.
        # A caller satisfies the requirement when their token contains ANY of the
        # listed roles (via ``roles`` claim) or scopes (via ``scp`` claim).
        # Callers with ``MCP.Admin`` role bypass all per-tool requirements.
        # Empty list (or tool not in dict) = standard can_execute_tools() check.
        self._tool_role_requirements: Dict[str, List[str]] = {}

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    async def initialize(self):
        """Register tools, resources, and prompts on the FastMCP instance."""
        if self._initialized:
            return

        self._setup_auth()
        self._register_knowledge_base_tools()
        self._register_manager_tools()
        self._register_resources()
        self._register_prompts()

        self._initialized = True
        logger.info("RemoteMCPServer initialized")

    def _get_cors_origins(self) -> list:
        """Return the list of allowed CORS origins from settings."""
        cfg = self._mcp_cfg
        if cfg and cfg.cors_allowed_origins:
            return [o.strip() for o in cfg.cors_allowed_origins.split(",") if o.strip()]
        return ["*"]

    def _setup_auth(self):
        """Create the token validator from settings (no-op if auth not configured)."""
        cfg = self._mcp_cfg
        if not cfg or not cfg.auth_tenant_id or not cfg.auth_client_id:
            logger.warning(
                "Remote MCP server auth is NOT configured — "
                "all requests will be accepted WITHOUT authentication.  "
                "Set REMOTE_MCP_AUTH_TENANT_ID and REMOTE_MCP_AUTH_CLIENT_ID "
                "to enable Entra ID token validation."
            )
            return

        self._validator = EntraIDTokenValidator(
            tenant_id=cfg.auth_tenant_id,
            client_id=cfg.auth_client_id,
            audience=cfg.auth_audience,
            issuer=cfg.auth_issuer,
            multi_tenant=cfg.auth_multi_tenant,
            allowed_client_ids=cfg.auth_allowed_client_ids,
        )
        logger.info(
            "Entra ID auth configured: tenant=%s client=%s",
            cfg.auth_tenant_id,
            cfg.auth_client_id,
        )

    # ------------------------------------------------------------------
    # Tool registration
    # ------------------------------------------------------------------

    @staticmethod
    def _audit_log(tool_name: str, success: bool, duration_ms: float) -> None:
        """Emit a structured audit log entry for a tool invocation."""
        identity = _caller_context.get()
        _audit_logger.info(
            "tool_call tool=%s success=%s duration_ms=%.1f "
            "caller_oid=%s caller_app=%s caller_name=%s tid=%s",
            tool_name,
            success,
            duration_ms,
            identity.get("oid", ""),
            identity.get("appid", ""),
            identity.get("name", ""),
            identity.get("tid", ""),
        )

    # ------------------------------------------------------------------
    # Tool-level RBAC (task 7.3)
    # ------------------------------------------------------------------

    def set_tool_role_requirement(
        self, tool_name: str, required_roles: List[str]
    ) -> None:
        """
        Require the caller to hold at least one of *required_roles* (or
        ``MCP.Admin``) in order to invoke *tool_name*.

        Call this after ``initialize()`` if you need to restrict specific tools
        beyond the default ``can_execute_tools()`` check.

        Example::

            server.set_tool_role_requirement(
                "reset_index", ["MCP.Admin"]
            )
        """
        self._tool_role_requirements[tool_name] = required_roles

    def _check_tool_rbac(self, tool_name: str) -> None:
        """
        Raise ``AuthError(403)`` when the current caller (from ``_caller_context``)
        does not satisfy the per-tool role requirement.

        Callers with ``MCP.Admin`` role always pass.
        When auth is disabled (no validator) the check is skipped.
        """
        if self._validator is None:
            return  # auth not configured — bypass

        required = self._tool_role_requirements.get(tool_name)
        if not required:
            return  # no extra requirement for this tool

        claims = _caller_context.get()
        if not claims:
            return  # no claims available (e.g. auth disabled at request level)

        # MCP.Admin is a global override
        token_roles = set(claims.get("roles", []))
        if "MCP.Admin" in token_roles:
            return

        # Check whether the caller has any of the required roles/scopes
        has_role = bool(token_roles & set(required))
        token_scopes = set(claims.get("scp", "").split()) if claims.get("scp") else set()
        has_scope = bool(token_scopes & set(required))

        if not has_role and not has_scope:
            raise AuthError(
                f"Insufficient permissions for tool '{tool_name}'. "
                f"Required (any of): {required}",
                status_code=403,
            )

    def _register_knowledge_base_tools(self):
        """Register search_knowledge_base and get_system_context tools."""

        # Capture references in closure
        rag_retriever = self._rag_retriever
        citation_resolver = self._citation_resolver
        app_settings = self._settings

        # Build agent-name-aware descriptions from settings
        mcp_cfg = self._mcp_cfg
        agent_name: str = (
            mcp_cfg.agent_name.strip()
            if mcp_cfg and mcp_cfg.agent_name and mcp_cfg.agent_name.strip()
            else ""
        )
        kb_label = f"{agent_name} knowledge base" if agent_name else "organisational knowledge base"
        kb_search_desc = (
            f"Search the {kb_label} "
            "using semantic, vector, or hybrid search and return relevant "
            "passages with source links.  Use this tool to answer questions "
            "that require grounding in internal documents, wiki pages, or other "
            "ingested knowledge.  Results include the content, title, and a "
            "clickable source_url linking to the original document."
        )

        @self.mcp.tool(
            name="search_knowledge_base",
            description=kb_search_desc,
        )
        async def _search_knowledge_base(
            query: str,
            top_k: int = 5,
            search_type: Optional[str] = None,
        ) -> str:
            t0 = time.monotonic()
            success = True
            try:
                self._check_tool_rbac("search_knowledge_base")
                return await search_knowledge_base(
                    query=query,
                    top_k=top_k,
                    search_type=search_type,
                    rag_retriever=rag_retriever,
                    citation_resolver=citation_resolver,
                    app_settings=app_settings,
                )
            except AuthError as exc:
                success = False
                return json.dumps({"error": str(exc), "status_code": exc.status_code})
            except Exception:
                success = False
                raise
            finally:
                self._audit_log(
                    "search_knowledge_base", success, (time.monotonic() - t0) * 1000
                )

        sys_ctx_desc = (
            f"Return the system message, data source information, and UI "
            f"configuration for the {kb_label}.  Use this to understand "
            "the assistant persona and available search capabilities before "
            "answering questions."
        )

        @self.mcp.tool(
            name="get_system_context",
            description=sys_ctx_desc,
        )
        async def _get_system_context() -> str:
            t0 = time.monotonic()
            success = True
            try:
                self._check_tool_rbac("get_system_context")
                return await get_system_context(app_settings=app_settings)
            except AuthError as exc:
                success = False
                return json.dumps({"error": str(exc), "status_code": exc.status_code})
            except Exception:
                success = False
                raise
            finally:
                self._audit_log(
                    "get_system_context", success, (time.monotonic() - t0) * 1000
                )

        logger.debug("Registered knowledge base tools")

    def _register_manager_tools(self):
        """Re-expose all tools from MCPServerManager as remote MCP tools."""
        if not self._mcp_manager:
            return

        all_tools = self._mcp_manager.get_tools()
        manager = self._mcp_manager

        for tool_def in all_tools:
            fn_def = tool_def.get("function", {})
            tool_name: str = fn_def.get("name", "")
            tool_desc: str = fn_def.get("description", "")
            tool_params: Dict[str, Any] = fn_def.get("parameters") or {}

            if not tool_name:
                continue

            # Build a dynamic async function for each tool.
            # We capture tool_name in the default argument to avoid
            # late-binding closure issues.
            def _make_tool_fn(captured_name: str):
                async def _tool_fn(**kwargs) -> str:
                    t0 = time.monotonic()
                    success = True
                    try:
                        self._check_tool_rbac(captured_name)
                        result = await manager.call_tool(captured_name, kwargs)
                        return str(result)
                    except AuthError as exc:
                        success = False
                        return json.dumps({"error": str(exc), "status_code": exc.status_code})
                    except Exception as exc:
                        success = False
                        logger.error("Error calling managed tool %s: %s", captured_name, exc)
                        return json.dumps({"error": str(exc), "tool": captured_name})
                    finally:
                        self._audit_log(
                            captured_name, success, (time.monotonic() - t0) * 1000
                        )

                _tool_fn.__name__ = captured_name
                return _tool_fn

            fn = _make_tool_fn(tool_name)

            try:
                self.mcp.tool(name=tool_name, description=tool_desc)(fn)
                logger.debug("Registered managed tool: %s", tool_name)
            except Exception as exc:
                logger.warning("Could not register managed tool '%s': %s", tool_name, exc)

        logger.info(
            "Registered %d managed tools from MCPServerManager", len(all_tools)
        )

    # ------------------------------------------------------------------
    # Resource registration
    # ------------------------------------------------------------------

    def _register_resources(self):
        """Register MCP resources (read-only context data)."""
        app_settings = self._settings

        @self.mcp.resource("context://system-message")
        async def _system_message_resource() -> str:
            """The configured system message / assistant persona."""
            return app_settings.azure_openai.system_message or ""

        @self.mcp.resource("context://knowledge-base-info")
        async def _knowledge_base_info_resource() -> str:
            """Available data sources and search capabilities."""
            info: Dict[str, Any] = {
                "datasource_type": app_settings.base_settings.datasource_type,
                "has_knowledge_base": app_settings.datasource is not None,
            }
            if app_settings.datasource:
                ds = app_settings.datasource
                info["index"] = getattr(ds, "index", None)
                info["query_type"] = getattr(ds, "query_type", None)
                info["top_k"] = getattr(ds, "top_k", 5)
            return json.dumps(info, indent=2)

        @self.mcp.resource("context://citation-config")
        async def _citation_config_resource() -> str:
            """How citations should be interpreted and linked."""
            cfg = app_settings.citation_file
            return json.dumps(
                {
                    "storage_base_url": getattr(cfg, "storage_base_url", None),
                    "link_base_url": getattr(cfg, "link_base_url", None),
                    "has_link_appendix": bool(getattr(cfg, "link_url_appendix", None)),
                },
                indent=2,
            )

        logger.debug("Registered MCP resources")

    # ------------------------------------------------------------------
    # Prompt registration
    # ------------------------------------------------------------------

    def _register_prompts(self):
        """Register reusable MCP prompt templates."""

        @self.mcp.prompt(
            name="search-and-answer",
            description=(
                "Template: answer a question using the knowledge base. "
                "Calls search_knowledge_base then synthesises a grounded answer."
            ),
        )
        async def _search_and_answer(question: str) -> list:
            return [
                {
                    "role": "user",
                    "content": (
                        f"Please search the knowledge base for information relevant "
                        f"to the following question and provide a grounded answer "
                        f"with source references:\n\n{question}"
                    ),
                }
            ]

        @self.mcp.prompt(
            name="summarize-document",
            description="Template: retrieve and summarise a named document.",
        )
        async def _summarize_document(document_name: str) -> list:
            return [
                {
                    "role": "user",
                    "content": (
                        f"Please search the knowledge base for the document "
                        f"'{document_name}' and provide a concise summary of its "
                        f"content, including key points and any relevant source links."
                    ),
                }
            ]

        logger.debug("Registered MCP prompts")

    # ------------------------------------------------------------------
    # Auth validation helper (used by Quart route wrapper)
    # ------------------------------------------------------------------

    async def validate_request_token(self, authorization_header: Optional[str]) -> Dict[str, Any]:
        """
        Validate the Bearer token from the Authorization header.

        Returns decoded JWT claims on success.
        Raises ``AuthError`` on failure.
        """
        if self._validator is None:
            # Auth not configured — accept all requests (dev/non-prod only)
            return {}

        if not authorization_header:
            raise AuthError("Missing Authorization header", status_code=401)

        parts = authorization_header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise AuthError("Authorization header must be 'Bearer <token>'", status_code=401)

        token = parts[1]
        claims = await self._validator.validate_token(token)

        if not EntraIDTokenValidator.has_mcp_access(claims):
            raise AuthError(
                "Insufficient permissions — token requires MCP.Tools.Read, "
                "MCP.Tools.Execute, MCP.User, MCP.ToolCaller, or MCP.Admin scope/role.",
                status_code=403,
            )

        return claims

    # ------------------------------------------------------------------
    # Protected Resource Metadata
    # ------------------------------------------------------------------

    def get_prm_metadata(self) -> Optional[Dict[str, Any]]:
        """Return the PRM dict or None if auth is not configured."""
        cfg = self._mcp_cfg
        if not cfg or not cfg.auth_tenant_id or not cfg.auth_client_id:
            return None

        server_url = cfg.server_url or "/mcp"
        scopes = None
        if cfg.auth_client_id:
            scopes = [
                f"api://{cfg.auth_client_id}/MCP.Tools.Read",
                f"api://{cfg.auth_client_id}/MCP.Tools.Execute",
            ]

        return build_prm_metadata(
            server_url=server_url,
            tenant_id=cfg.auth_tenant_id,
            client_id=cfg.auth_client_id,
            scopes_supported=scopes,
            default_scope=cfg.auth_default_scope,
        )

    # ------------------------------------------------------------------
    # ASGI app (Starlette/FastMCP) for mounting alongside Quart
    # ------------------------------------------------------------------

    def get_asgi_app(self, base_path: str = "/mcp"):
        """
        Return a Starlette ASGI app for the MCP Streamable HTTP transport.

        The app is pre-wired with:
        - CORS middleware (permissive — all origins allowed)
        - Entra ID Bearer token auth middleware (if configured)

        Mount this ASGI app on Quart via an ``_MCPASGIDispatch`` middleware so
        that all ``/mcp`` requests are handled by FastMCP rather than Quart.

        Parameters
        ----------
        base_path:
            The URL path prefix under which FastMCP listens, e.g. ``"/mcp"``.
        """
        cfg = self._mcp_cfg
        server_url = (cfg.server_url or "") if cfg else ""

        cors_origins = self._get_cors_origins()
        middleware: List[Any] = [
            # Security headers — outermost so they are always present.
            Middleware(_SecurityHeadersMiddleware),
            # CORS must run before auth so preflight responses are returned
            # before any auth check.
            Middleware(
                CORSMiddleware,
                allow_origins=cors_origins,
                allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
                allow_headers=["*"],
                expose_headers=["Mcp-Session-Id"],
            ),
        ]

        if self._validator:
            middleware.append(
                Middleware(
                    _MCPAuthStarletteMiddleware,
                    validator=self._validator,
                    server_url=server_url,
                )
            )
        else:
            logger.warning(
                "get_asgi_app: no auth validator configured — MCP endpoint "
                "accepts ALL requests without token validation."
            )

        return self.mcp.http_app(path=base_path, middleware=middleware)

    def get_sse_asgi_app(self):
        """
        Return a legacy SSE Starlette ASGI app for backward compatibility.

        Older MCP clients (pre-2025-11-25 spec) connect via
        ``GET /sse`` (opens event stream) and ``POST /messages``
        (sends JSON-RPC requests).  This app handles those paths using
        FastMCP's SSE transport so that they continue to work alongside
        the primary Streamable HTTP ``/mcp`` endpoint.

        Auth middleware is applied identically to the Streamable HTTP app.
        """
        cfg = self._mcp_cfg
        server_url = (cfg.server_url or "") if cfg else ""

        cors_origins = self._get_cors_origins()
        middleware: List[Any] = [
            Middleware(_SecurityHeadersMiddleware),
            Middleware(
                CORSMiddleware,
                allow_origins=cors_origins,
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["*"],
                expose_headers=["Mcp-Session-Id"],
            ),
        ]

        if self._validator:
            middleware.append(
                Middleware(
                    _MCPAuthStarletteMiddleware,
                    validator=self._validator,
                    server_url=server_url,
                )
            )

        # Use http_app with SSE transport — FastMCP handles the /sse and /messages paths
        return self.mcp.http_app(path="/sse", middleware=middleware, transport="sse")

