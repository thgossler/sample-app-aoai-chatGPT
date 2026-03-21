# Entra ID Remote MCP Server Setup Guide

This document walks you through setting up the **Remote MCP Server** feature — an
MCP (Model Context Protocol) endpoint built into the chat application that allows
any MCP-compatible client (VS Code Copilot, Claude Desktop, custom agents) to
access the same knowledge base and tools that the web chat uses.

## Overview

The remote MCP server exposes the application as an **OAuth 2.1 Resource Server**,
secured with Microsoft Entra ID (formerly Azure AD) tokens. Clients authenticate
via their own Entra ID access token and gain access to:

| Tool / Resource | Description |
| --------------- | ----------- |
| `search_knowledge_base` | Query Azure AI Search with the same index the web chat uses |
| `get_system_context` | Read the configured system message and data source info |
| All tools from `mcp_servers.json` | SW360, Azure Functions, and any other configured servers |
| `context://system-message` | MCP resource — the assistant persona |
| `context://knowledge-base-info` | MCP resource — index & search capabilities |
| `context://citation-config` | MCP resource — citation URL configuration |
| `search-and-answer` prompt | A reusable grounded-answer prompt template |

**Protocol**: MCP Streamable HTTP transport (spec 2025-11-25)  
**Auth**: OAuth 2.1 Bearer tokens, RS256, validated locally (JWKS from Entra ID)  
**Discovery**: RFC 9728 Protected Resource Metadata at
`GET /.well-known/oauth-protected-resource`

---

## Prerequisites

- An active Azure subscription with an Entra ID tenant.
- The chat application deployed (Azure App Service or local dev with public IP for
  OAuth flows).
- `Global Administrator` or `Application Administrator` role in Entra ID to register
  app registrations.

---

## Step 1 — Register the MCP Server App in Entra ID

This app registration represents the **server** (the resource that tokens are
issued *for*).

1. Open the [Azure portal](https://portal.azure.com) → **Microsoft Entra ID** →
   **App registrations** → **New registration**.
2. Set:
   - **Name**: `My Chat App — Remote MCP Server`
   - **Supported account types**: *Single tenant* (or *Multi-tenant* if clients
     will come from different tenants — set `REMOTE_MCP_AUTH_MULTI_TENANT=true`)
   - **Redirect URI**: Leave blank (this is a daemon/non-interactive resource).
3. Click **Register**.
4. Note the **Application (client) ID** — this becomes `REMOTE_MCP_AUTH_CLIENT_ID`.
5. Note the **Directory (tenant) ID** — this becomes `REMOTE_MCP_AUTH_TENANT_ID`.

### 1a — Expose an API (define scopes)

1. In the app registration, go to **Expose an API**.
2. Click **Set** next to *Application ID URI* — accept the default
   `api://<client-id>` or use a custom URI.
3. Click **Add a scope** and create the following:

| Scope | Who can consent | Display name | Description |
| ----- | --------------- | ------------ | ----------- |
| `MCP.Tools.Read` | Admins and users | Read MCP tools | Search knowledge base & read context |
| `MCP.Tools.Execute` | Admins and users | Execute MCP tools | Call any tool via MCP |

4. Optionally create **App roles** (for service-level access without user consent):

   Go to **App roles** → **Create app role**:

| Display name | Allowed member types | Value | Description |
| ------------ | -------------------- | ----- | ----------- |
| MCP Admin | Applications + Users/Groups | `MCP.Admin` | Full MCP access |
| MCP Tool Caller | Applications + Users/Groups | `MCP.ToolCaller` | Execute tools |
| MCP User | Users/Groups | `MCP.User` | Read-only access |

---

## Step 2 — Register a Client App (for each MCP client)

Each MCP client needs its own Entra ID app registration, *except* VS Code Copilot
which already has a Microsoft-published registration.

### Known client IDs (no registration required)

| Tool | Client ID | Notes |
| ----- | -------- | ----- |
| **VS Code Copilot** (agent mode) | `aebc6443-996d-45c2-90f0-388ff96faa56` | Built-in; pre-authorise and add to allowed list |
| Azure CLI | `04b07795-8ddb-461a-bbee-02f9e1bf7b46` | Useful for token acquisition in scripts |

### 2a — Register: Claude Code

Claude Code (Anthropic's terminal CLI) does not have a Microsoft-published client
ID. Create a dedicated registration:

1. **App registrations** → **New registration**
   - **Name**: `My Chat App — Claude Code MCP Client`
   - **Supported account types**: Single tenant (or multitenant if needed)
   - Click **Register**
2. Under **Authentication** → **Add a platform** → **Mobile and desktop**
   - Redirect URI: `http://localhost`
   - Click **Configure**
3. Under **Authentication** → enable **Allow public client flows** → **Yes** → **Save**
4. Under **API permissions** → **Add a permission** → **My APIs** →
   `My Chat App — Remote MCP Server` → tick `MCP.Tools.Read` and
   `MCP.Tools.Execute` → **Add permissions** → **Grant admin consent**
5. Note the **Application (client) ID** — you will need it in Steps 3 and 6.

### 2b — Register: GitHub Copilot CLI (`gh copilot`)

The `gh copilot` CLI extension and GitHub Copilot agent mode outside of VS Code do
not have a Microsoft-published Entra ID client ID. Create a dedicated registration:

1. **App registrations** → **New registration**
   - **Name**: `My Chat App — GitHub Copilot CLI MCP Client`
   - **Supported account types**: Single tenant
   - Click **Register**
2. Under **Authentication** → **Add a platform** → **Mobile and desktop**
   - Redirect URI: `http://localhost`
   - Click **Configure**
3. Under **Authentication** → enable **Allow public client flows** → **Yes** → **Save**
4. Under **API permissions** → **Add a permission** → **My APIs** →
   `My Chat App — Remote MCP Server` → tick `MCP.Tools.Read` and
   `MCP.Tools.Execute` → **Add permissions** → **Grant admin consent**
5. Note the **Application (client) ID** — you will need it in Steps 3 and 7.

> **GitHub Copilot in VS Code** (agent mode) uses VS Code's built-in client ID
> `aebc6443-996d-45c2-90f0-388ff96faa56` — no separate registration needed for
> that scenario.

### 2c — Pre-authorise all client IDs on the server

For each client app you registered (and for the VS Code built-in ID), repeat:

1. Open the **server** app registration → **Expose an API** →
   **Authorized client applications** → **Add a client application**.
2. Enter the client ID and tick both scopes (`MCP.Tools.Read`, `MCP.Tools.Execute`).
3. **Save**.
4. Add the client ID to `REMOTE_MCP_AUTH_ALLOWED_CLIENT_IDS` in your `.env`
   (comma-separated, see Step 3).

---

## Step 3 — Configure the Application

Add the following variables to your `.env` file (or App Service configuration):

```bash
# ── Remote MCP Server ──────────────────────────────────────────────────────

# Enable the Streamable HTTP MCP endpoint (/mcp, /sse, /.well-known/…)
REMOTE_MCP_SERVER_ENABLED=true

# Public URL of the MCP server (used in the PRM discovery document)
REMOTE_MCP_SERVER_URL=https://your-chat-app.azurewebsites.net/mcp

# Entra ID tenant where the server app registration lives
REMOTE_MCP_AUTH_TENANT_ID=<Directory (tenant) ID from Step 1>

# Application (client) ID of the server app registration
REMOTE_MCP_AUTH_CLIENT_ID=<Application (client) ID from Step 1>

# Comma-separated list of trusted client app IDs (Steps 2a–2c)
# VS Code Copilot built-in + custom registrations from Steps 2a and 2b:
REMOTE_MCP_AUTH_ALLOWED_CLIENT_IDS=aebc6443-996d-45c2-90f0-388ff96faa56,<claude-code-client-id>,<gh-copilot-cli-client-id>

# Set to true if you registered for multi-tenant in Step 1
# REMOTE_MCP_AUTH_MULTI_TENANT=false

# Optional overrides — auto-derived from tenant/client IDs when left blank:
# REMOTE_MCP_AUTH_AUDIENCE=api://<client-id>
# REMOTE_MCP_AUTH_ISSUER=https://sts.windows.net/<tenant-id>/
# REMOTE_MCP_AUTH_DEFAULT_SCOPE=api://<client-id>/.default
```

> **No client secret is ever sent to the MCP server.** The server validates
> tokens locally using the Entra ID JWKS endpoint (public keys only).

---

## Step 4 — Verify the Discovery Document

Start the application and confirm the discovery endpoint responds:

```bash
curl https://your-chat-app.azurewebsites.net/.well-known/oauth-protected-resource
```

Expected response (abbreviated):

```json
{
  "resource": "https://your-chat-app.azurewebsites.net/mcp",
  "authorization_servers": [
    "https://login.microsoftonline.com/<tenant-id>/v2.0"
  ],
  "scopes_supported": [
    "api://<client-id>/MCP.Tools.Read",
    "api://<client-id>/MCP.Tools.Execute"
  ],
  "bearer_methods_supported": ["header"],
  "resource_documentation": "https://your-chat-app.azurewebsites.net/docs/mcp"
}
```

---

## Step 5 — Connect VS Code Copilot

1. Open **VS Code** → Command Palette → **MCP: Add Server**.
2. Choose **HTTP** transport.
3. Enter the server URL: `https://your-chat-app.azurewebsites.net/mcp`
4. VS Code will detect the `WWW-Authenticate` header from the discovery document
   and prompt you to sign in with your Entra ID account.
5. After sign-in, the MCP tools appear in the Copilot tool list (`search_knowledge_base`,
   `get_system_context`, etc.).

### VS Code `settings.json` (manual configuration)

```json
{
  "mcp": {
    "servers": {
      "my-chat-app": {
        "type": "http",
        "url": "https://your-chat-app.azurewebsites.net/mcp"
      }
    }
  }
}
```

---

## Step 6 — Connect Claude Desktop and Claude Code

### 6a — Claude Desktop

Claude Desktop connects via the `mcp-remote` bridge. Add an entry to
`~/.claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "my-chat-app": {
      "command": "npx",
      "args": [
        "@anthropic-ai/mcp-remote@latest",
        "https://your-chat-app.azurewebsites.net/mcp",
        "--header",
        "Authorization: Bearer YOUR_ACCESS_TOKEN"
      ]
    }
  }
}
```

Obtain a token (requires Azure CLI logged in with an account that has the
`MCP.Tools.Execute` scope):

```bash
az account get-access-token \
  --resource api://<server-client-id> \
  --query accessToken \
  --output tsv
```

### 6b — Claude Code

Claude Code (the `claude` terminal CLI, ≥ 1.x) supports HTTP MCP servers
natively. It discovers auth requirements from `/.well-known/oauth-protected-resource`
and initiates an OAuth PKCE flow through your default browser.

**Prerequisites**: you have completed Step 2a (created the Claude Code app
registration) and noted its client ID.

#### Option A — Native HTTP with automatic OAuth (recommended)

Add a server to your project's `.mcp.json` (or `~/.claude/mcp_servers.json`
for global use):

```json
{
  "mcpServers": {
    "my-chat-app": {
      "type": "http",
      "url": "https://your-chat-app.azurewebsites.net/mcp"
    }
  }
}
```

On first use, Claude Code discovers the Entra ID authorization server from the
PRM document and opens your browser to sign in. To specify the client ID
registered in Step 2a and skip any interactive prompt, add the server via the
CLI:

```bash
claude mcp add my-chat-app \
  --transport http \
  https://your-chat-app.azurewebsites.net/mcp \
  --oauth-client-id <claude-code-client-id-from-step-2a>
```

#### Option B — Static bearer token (simpler, no browser required)

Acquire a token via Azure CLI and inject it via `mcp-remote`:

```bash
export MCP_TOKEN=$(az account get-access-token \
  --resource api://<server-client-id> \
  --query accessToken --output tsv)

claude mcp add my-chat-app \
  -- npx @anthropic-ai/mcp-remote@latest \
     https://your-chat-app.azurewebsites.net/mcp \
     --header "Authorization: Bearer $MCP_TOKEN"
```

Or add it directly to `.mcp.json`:

```json
{
  "mcpServers": {
    "my-chat-app": {
      "command": "npx",
      "args": [
        "@anthropic-ai/mcp-remote@latest",
        "https://your-chat-app.azurewebsites.net/mcp",
        "--header",
        "Authorization: Bearer YOUR_ACCESS_TOKEN"
      ]
    }
  }
}
```

---

## Step 7 — Connect GitHub Copilot CLI

### 7a — GitHub Copilot in VS Code (agent mode)

GitHub Copilot agent mode in VS Code **already works** using the VS Code
built-in client ID (`aebc6443-996d-45c2-90f0-388ff96faa56`) — no extra
registration is needed beyond pre-authorising it in Step 2c. See Step 5.

To share the MCP server config with all contributors automatically, create
`.github/mcp.json` in your repository root:

```json
{
  "inputs": [],
  "servers": {
    "my-chat-app": {
      "type": "http",
      "url": "https://your-chat-app.azurewebsites.net/mcp"
    }
  }
}
```

VS Code 1.99+ reads this file automatically when the repository is opened.
GitHub Copilot discovers the PRM document and signs in via the VS Code OAuth
flow (same as Step 5).

### 7b — GitHub Copilot CLI (`gh copilot` extension)

The `gh copilot` extension does not directly support plugging in external
OAuth-protected MCP servers. The recommended approach is to acquire a delegated
token via the **device code flow** using the client ID registered in Step 2b,
then pass it as a header through `mcp-remote`.

**Acquire a token interactively (device code flow):**

```bash
# Requires: pip install msal
python3 - <<'EOF'
import msal

TENANT_ID  = "<your-tenant-id>"
CLIENT_ID  = "<gh-copilot-cli-client-id-from-step-2b>"
SCOPE      = "api://<server-client-id>/MCP.Tools.Execute"

app = msal.PublicClientApplication(
    CLIENT_ID,
    authority=f"https://login.microsoftonline.com/{TENANT_ID}",
)

flow = app.initiate_device_flow(scopes=[SCOPE])
print(flow["message"])  # Go to https://microsoft.com/devicelogin — enter code XXXX-XXXX
input("Press Enter after signing in...")

result = app.acquire_token_by_device_flow(flow)
print("\nAccess token:\n" + result["access_token"])
EOF
```

**Use the token:**

```bash
export MCP_TOKEN="<token-from-above>"

# Quick connectivity test
npx @anthropic-ai/mcp-remote@latest \
  https://your-chat-app.azurewebsites.net/mcp \
  --header "Authorization: Bearer $MCP_TOKEN"
```

> For non-interactive automation (CI/CD), use the **Python service principal**
> approach in Step 8 instead.

---

## Step 8 — Connect a Python Service Principal (Daemon / Agent)

Use this pattern when you need a **non-interactive service** (CI pipeline, backend agent,
automation script) to call MCP tools using client credentials rather than a user's delegated
token.

### 8a — Assign the App Role to the Service Principal

1. In the **server** app registration → **App roles** → ensure `MCP.ToolCaller` or
   `MCP.Admin` exists (created in Step 1).
2. In the **client** app registration → **API permissions** →
   **Add a permission** → **My APIs** → server app → **Application permissions** →
   tick `MCP.ToolCaller` → **Add permissions**.
3. Click **Grant admin consent**.

### 8b — Python Example with `azure-identity` and `fastmcp`

Install dependencies:

```bash
pip install azure-identity fastmcp httpx
```

```python
"""
Service principal MCP client example.

Calls the remote MCP server using client credentials (daemon/non-interactive).
Requires:
  - AZURE_TENANT_ID
  - AZURE_CLIENT_ID         (client app registration)
  - AZURE_CLIENT_SECRET     (or use cert via CertificateCredential)
  - MCP_SERVER_URL          e.g. https://your-chat-app.azurewebsites.net/mcp
  - MCP_RESOURCE_APP_ID     Application (client) ID of the *server* app registration
"""

import asyncio
import os

from azure.identity.aio import ClientSecretCredential
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


async def get_access_token() -> str:
    """Acquire an Entra ID access token for the MCP server resource."""
    credential = ClientSecretCredential(
        tenant_id=os.environ["AZURE_TENANT_ID"],
        client_id=os.environ["AZURE_CLIENT_ID"],
        client_secret=os.environ["AZURE_CLIENT_SECRET"],
    )
    resource_app_id = os.environ["MCP_RESOURCE_APP_ID"]
    scope = f"api://{resource_app_id}/.default"

    token = await credential.get_token(scope)
    await credential.close()
    return token.token


async def main():
    mcp_url = os.environ["MCP_SERVER_URL"]
    token = await get_access_token()

    # Build transport with Authorization header pre-set
    transport = StreamableHttpTransport(
        url=mcp_url,
        headers={"Authorization": f"Bearer {token}"},
    )

    async with Client(transport) as client:
        # List available tools
        tools = await client.list_tools()
        print("Available tools:")
        for tool in tools:
            print(f"  - {tool.name}: {tool.description}")

        # Call the knowledge base search tool
        result = await client.call_tool(
            "search_knowledge_base",
            {"query": "What is the data architecture?", "top_k": 3},
        )
        print("\nSearch result:")
        for item in result:
            print(item.text)


if __name__ == "__main__":
    asyncio.run(main())
```

### 8c — Minimal `httpx` Example (no FastMCP SDK)

If you only need a one-off tool call without the full MCP SDK:

```python
import asyncio
import json
import os

import httpx
from azure.identity.aio import ClientSecretCredential


async def call_mcp_tool(tool_name: str, arguments: dict) -> str:
    credential = ClientSecretCredential(
        tenant_id=os.environ["AZURE_TENANT_ID"],
        client_id=os.environ["AZURE_CLIENT_ID"],
        client_secret=os.environ["AZURE_CLIENT_SECRET"],
    )
    scope = f"api://{os.environ['MCP_RESOURCE_APP_ID']}/.default"
    token = (await credential.get_token(scope)).token
    await credential.close()

    mcp_url = os.environ["MCP_SERVER_URL"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # MCP JSON-RPC 2.0 initialize → tools/call flow
    async with httpx.AsyncClient() as http:
        # 1. Initialize session
        init_resp = await http.post(
            mcp_url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "my-script", "version": "1.0"},
                },
            },
        )
        init_resp.raise_for_status()
        session_id = init_resp.headers.get("mcp-session-id", "")

        if session_id:
            headers["Mcp-Session-Id"] = session_id

        # 2. Call the tool
        call_resp = await http.post(
            mcp_url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            },
        )
        call_resp.raise_for_status()
        data = call_resp.json()
        return json.dumps(data.get("result", data), indent=2)


if __name__ == "__main__":
    result = asyncio.run(
        call_mcp_tool("search_knowledge_base", {"query": "architecture overview"})
    )
    print(result)
```

---

## Scopes Reference

| Scope / Role | Grant type | Min permission |
| ------------ | ---------- | -------------- |
| `MCP.Tools.Read` | Delegated | List tools, search KB, read context |
| `MCP.Tools.Execute` | Delegated | Execute any registered tool |
| `MCP.User` | Delegated (app role) | Read-only; same as `MCP.Tools.Read` |
| `MCP.ToolCaller` | App role | Execute tools; for daemon clients |
| `MCP.Admin` | App role | Full access; for admin agents |

---

## Troubleshooting

### `401 Unauthorized` — `invalid_token`

- Token `aud` does not match `REMOTE_MCP_AUTH_AUDIENCE`.
  Make sure the client requests a token for `api://<client-id>` (not the default
  Microsoft Graph audience).
- Token expired. Refresh and retry.

### `403 Forbidden` — `insufficient_scope`

- The token lacks any of the required MCP scopes/roles.
  Ensure the client app has been granted `MCP.Tools.Read` (minimum) and admin consent
  has been given.

### `401 Unauthorized` — `unknown_client`

- The `azp` (authorized party) claim in the token does not match
  `REMOTE_MCP_AUTH_ALLOWED_CLIENT_IDS`.
  Add the client's app ID to `REMOTE_MCP_AUTH_ALLOWED_CLIENT_IDS` and redeploy.

### `404 Not Found` on `/mcp`

- `REMOTE_MCP_SERVER_ENABLED` is not set to `true`.
- Check application logs for startup errors in `init_remote_mcp_server()`.

### CORS errors from a browser-based client

- The `/mcp` and `/.well-known/oauth-protected-resource` routes include
  `Access-Control-Allow-Origin: *` headers.
  If your client requires specific allowed origins, set
  `REMOTE_MCP_CORS_ALLOWED_ORIGINS` to a comma-separated list of origins.

---

## Security Notes

1. **Token validation is local** — the app validates RS256 tokens using Entra ID's
   public JWKS keys, cached for 1 hour. No calls to Entra ID are made per request.
2. **No client secrets** — the server never holds a client secret for token
   validation; it trusts Entra ID's public keys.
3. **Allowed client IDs** — only tokens issued *for* a pre-authorised client app
   (the `azp` claim) are accepted. This prevents token theft from other applications.
4. **Scope / role check** — every request is checked for at least one of
   `MCP.Tools.Read`, `MCP.Tools.Execute`, `MCP.User`, `MCP.ToolCaller`, or
   `MCP.Admin` before any tool is invoked.
5. **Dev mode** — if `REMOTE_MCP_AUTH_TENANT_ID` / `REMOTE_MCP_AUTH_CLIENT_ID` are
   not set, the server starts without auth and logs a prominent warning. **Never
   deploy without auth in production.**
