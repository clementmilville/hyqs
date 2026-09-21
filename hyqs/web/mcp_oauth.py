"""OAuth 2.1 helpers for the MCP server: Google OAuthProxy provider + identity resolver."""

from __future__ import annotations

import logging
import urllib.parse

from fastmcp.server.auth import MultiAuth, TokenVerifier
from fastmcp.server.auth.auth import AccessToken

from hyqs.config import Config
from hyqs.pipeline.store import JobStore
from hyqs.web.auth import AuthContext, resolve_api_token

log = logging.getLogger("hyqs.mcp_oauth")

# FastMCP's client-facing access token TTL. Left at FastMCP's default (None),
# it mirrors Google's short-lived (~1hr) upstream token, forcing bridge-style
# clients with poor refresh_token support (e.g. mcp-remote) through a full
# browser re-auth every hour. OAuthProxy still revalidates/refreshes the real
# upstream Google token on every request regardless of this value, so a long
# TTL here does not weaken server-side security.
_CLIENT_TOKEN_EXPIRY_SECONDS = 60 * 60 * 24 * 30  # 30 days

# Marks an AccessToken.claims dict as resolved from a hyqs API token secret
# (as opposed to a Google-issued identity, which carries an "email" claim
# instead). HyqsIdentityMiddleware branches on this to pick the right
# AuthContext constructor without re-deriving it from the raw token.
API_TOKEN_CLAIM_KIND = "api_token"


class ApiTokenVerifier(TokenVerifier):
    """FastMCP token verifier for hyqs project-scoped API token secrets.

    Delegates entirely to ``resolve_api_token`` — the same hashing, lookup,
    last-used tracking, and project-scoping the REST layer's ``resolve_auth``
    already uses — so MCP callers bearing an API token get an identical,
    hard-scoped identity instead of a separately-implemented one.
    """

    def __init__(self, store: JobStore) -> None:
        super().__init__()
        self.store = store

    async def verify_token(self, token: str) -> AccessToken | None:
        ctx = resolve_api_token(self.store, token)
        if ctx is None:
            return None
        return AccessToken(
            token=token,
            client_id=f"api_token:{ctx.api_token_id}",
            scopes=[],
            claims={
                "hyqs_auth_kind": API_TOKEN_CLAIM_KIND,
                "token_project_id": ctx.token_project_id,
                "token_role": ctx.token_role,
                "api_token_id": ctx.api_token_id,
            },
        )


def build_mcp_auth_provider(config: Config, store: JobStore):
    """Return the FastMCP auth provider for the MCP server.

    Composes Google OIDC (for interactive human clients, via OAuthProxy —
    adding DCR (RFC 7591) and Protected Resource Metadata (RFC 9728) that
    Google itself lacks) with ``ApiTokenVerifier`` (for automation clients
    bearing a hyqs project-scoped API token). ``MultiAuth`` tries the Google
    server first, falling back to the API-token verifier only when the
    bearer token isn't a valid Google-issued one — OAuth routes/metadata are
    owned entirely by the Google server.

    base_url is the full MCP resource URL (e.g. https://hyqs.example.com/mcp)
    so that /authorize, /token, /register, and /auth/callback are served under
    the /mcp mount path.  resource_base_url is the origin so that the resource
    metadata URL is computed correctly at the server root level.
    """
    from fastmcp.server.auth.providers.google import GoogleProvider

    resource_url = config.resolved_mcp_resource_url()
    if not resource_url:
        # Google OIDC needs a public redirect host, and there is no safe hostname
        # to guess. Fall back to API-token-only auth rather than either guessing
        # or leaving the MCP endpoint unauthenticated: a localhost-only install
        # has no public URL by design and must still start.
        log.warning(
            "no public MCP URL configured (set HYQS_MCP_RESOURCE_URL or "
            "HYQS_WEB_BASE_URL) — MCP accepts project-scoped API tokens only; "
            "Google sign-in is disabled"
        )
        return ApiTokenVerifier(store)
    parsed = urllib.parse.urlparse(resource_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    google_provider = GoogleProvider(
        client_id=config.google_client_id,
        client_secret=config.google_client_secret,
        base_url=resource_url,
        resource_base_url=origin,
        required_scopes=["openid", "email"],
        fastmcp_access_token_expiry_seconds=_CLIENT_TOKEN_EXPIRY_SECONDS,
    )
    # The outer scope gate applies one scope list to every verifier. Google
    # already enforces its own required OIDC scopes while validating/exchanging
    # tokens, whereas hyqs API tokens intentionally do not carry OAuth scopes.
    # Keep the shared gate scope-neutral so both credential types can reach the
    # identity and project-permission checks below it.
    return MultiAuth(
        server=google_provider,
        verifiers=[ApiTokenVerifier(store)],
        required_scopes=[],
    )


def resolve_oauth_identity(store: JobStore, email: str) -> AuthContext | None:
    """Map a Google OAuth email to a hyqs AuthContext.

    Returns None for unknown or inactive users — no auto-provisioning.
    """
    user = store.get_user_by_email(email)
    if user is None or not user.is_active:
        return None
    return AuthContext(
        user_id=user.id,
        user_email=user.email,
        is_platform_admin=user.is_platform_admin,
        display_name=user.display_name,
    )
