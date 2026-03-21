"""
Remote MCP Server package.

Exposes the existing MCP tools as a remote MCP server with Streamable HTTP
transport, secured by Microsoft Entra ID (OAuth 2.1 / OIDC) authentication.

MCP Specification: 2025-11-25
Transport: Streamable HTTP (POST/GET/DELETE /mcp)
Auth: OAuth 2.1 Resource Server with Entra ID as Authorization Server
"""
