"""
OAuth 2.0 Protected Resource Metadata endpoint (RFC 9728).

Per MCP Authorization Specification 2025-11-25, the server MUST expose
``GET /.well-known/oauth-protected-resource`` so that MCP clients can
automatically discover:

- Which authorization server issues tokens (Entra ID)
- The resource URI to request (used as the ``resource`` parameter in token
  requests per RFC 8707)
- Which scopes are available

VS Code Copilot Agent Mode, GitHub Copilot, and other RFC-9728-aware MCP
clients will fetch this metadata on first connection and drive the
Authorization Code + PKCE flow automatically — the end user simply signs in
with their Entra ID credentials.
"""

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def build_prm_metadata(
    server_url: str,
    tenant_id: str,
    client_id: str,
    scopes_supported: list[str] | None = None,
    default_scope: str | None = None,
) -> Dict[str, Any]:
    """
    Build the Protected Resource Metadata dict (RFC 9728 §2).

    Parameters
    ----------
    server_url:
        The full URL of the MCP endpoint (e.g.
        ``https://myapp.azurewebsites.net/mcp``).  This becomes the
        ``resource`` identifier that clients MUST include in token requests.
    tenant_id:
        Entra ID tenant ID.
    client_id:
        Application (client) ID of the MCP server app registration.
    scopes_supported:
        List of OAuth scopes.  Defaults to the two standard MCP scopes.
    default_scope:
        Scope string advertised to clients that support a single scope hint.
    """
    if scopes_supported is None:
        scopes_supported = [
            f"api://{client_id}/MCP.Tools.Read",
            f"api://{client_id}/MCP.Tools.Execute",
        ]

    # Strip trailing slash from server_url to form clean resource URI
    resource = server_url.rstrip("/")

    authorization_server = (
        f"https://login.microsoftonline.com/{tenant_id}/v2.0"
    )

    metadata: Dict[str, Any] = {
        # RFC 9728 §2 REQUIRED fields
        "resource": resource,
        "authorization_servers": [authorization_server],
        # RFC 9728 §2 RECOMMENDED fields
        "scopes_supported": scopes_supported,
        "bearer_methods_supported": ["header"],
        # Informational
        "resource_documentation": f"{resource.rsplit('/mcp', 1)[0]}/docs/mcp",
    }

    if default_scope:
        metadata["scopes_default"] = [default_scope]

    logger.debug("PRM metadata built for resource=%s", resource)
    return metadata
