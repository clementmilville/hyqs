# Job #2968: Accept scoped API tokens for MCP authentication

**Date:** 2026-08-01

This diff adds hyqs project-scoped API token support to the MCP server alongside existing Google OAuth. It extracts token resolution logic into a shared `resolve_api_token()` function so both REST and MCP authentication use identical token hashing, lookup, and project-scoping, then wires an `ApiTokenVerifier` into FastMCP's `MultiAuth` provider to accept either Google-issued or hyqs API token bearer tokens. The MCP identity middleware now branches on a claim marker to resolve either token type into the same `AuthContext` structure, maintaining the existing permission-checking logic while granting automation clients hard-scoped project access via API tokens.
This diff adds hyqs project-scoped API token support to the MCP server alongside existing Google OAuth. It extracts token resolution logic into a shared `resolve_api_token()` function so both REST and MCP authentication use identical token hashing, lookup, and project-scoping, then wires an `ApiTokenVerifier` into FastMCP's `MultiAuth` provider to accept either Google-issued or hyqs API token bearer tokens. The MCP identity middleware now branches on a claim marker to resolve either token type into the same `AuthContext` structure, maintaining the existing permission-checking logic while granting automation clients hard-scoped project access via API tokens.

## Files touched
- hyqs/web/auth.py
- hyqs/web/mcp_oauth.py
- hyqs/web/mcp_server.py
- tests/test_auth.py
- tests/test_mcp_server.py
