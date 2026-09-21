"""Framework-agnostic auth helpers shared between the HTTP and MCP layers."""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field

from hyqs.pipeline.store import JobStore


@dataclass
class AuthContext:
    user_id: int | None
    user_email: str | None
    is_platform_admin: bool
    display_name: str | None = field(default=None)
    # Project-scoped API token auth (mutually exclusive with the user_id path):
    # set only when this context was resolved from an api_tokens row.
    token_project_id: int | None = field(default=None)
    token_role: str | None = field(default=None)
    api_token_id: int | None = field(default=None)


def resolve_api_token(store: JobStore, token: str) -> AuthContext | None:
    """Resolve a project-scoped API token secret to its hard-scoped AuthContext.

    Returns None for an unknown or revoked secret. Owns the token's hashing,
    lookup, last-used tracking, and AuthContext construction so every caller
    (REST via resolve_auth, MCP via its own bearer-token path) shares the
    exact same resolution and project-scoping behavior.
    """
    api_token = store.get_api_token_by_secret(token)
    if api_token is None:
        return None
    store.touch_api_token(api_token.id)
    return AuthContext(
        user_id=None,
        user_email=None,
        is_platform_admin=False,
        token_project_id=api_token.project_id,
        token_role=api_token.role,
        api_token_id=api_token.id,
    )


def resolve_auth(store: JobStore, token: str, web_token: str) -> AuthContext | None:
    """Resolve a bearer token to an AuthContext. Returns None on auth failure.

    Handles three cases, tried in order:
    - Valid session token → AuthContext from user row
    - Token matches web_token → platform_admin AuthContext
    - Token matches a project-scoped API token → hard-scoped AuthContext
    """
    if token:
        user = store.get_session(token)
        if user is not None:
            return AuthContext(
                user_id=user.id,
                user_email=user.email,
                is_platform_admin=user.is_platform_admin,
                display_name=user.display_name,
            )
        if web_token and hmac.compare_digest(token, web_token):
            return AuthContext(user_id=None, user_email=None, is_platform_admin=True)
        return resolve_api_token(store, token)
    return None


def check_permission(
    store: JobStore,
    ctx: AuthContext,
    permission: str,
    project_id: int | None = None,
) -> bool:
    """Return True if ctx holds permission, optionally scoped to project_id."""
    if ctx.token_project_id is not None:
        # A project-scoped token can never satisfy a platform-wide check, nor
        # act on any project other than the one it was minted for.
        if project_id != ctx.token_project_id:
            return False
        perm_cache = store.get_role_permissions()
        return permission in perm_cache.get(ctx.token_role or "", [])
    perm_cache = store.get_role_permissions()
    if ctx.is_platform_admin:
        return permission in perm_cache.get("platform_admin", [])
    if not ctx.user_id:
        return False
    if project_id is not None:
        members = store.list_project_members(project_id)
        member = next((m for m in members if m.user_id == str(ctx.user_id)), None)
        if member is None:
            return False
        return permission in perm_cache.get(member.role, [])
    return permission in store.list_platform_permissions(ctx.user_id)


def check_env_permission(
    store: JobStore,
    ctx: AuthContext,
    permission: str,
    environment_id: int,
) -> bool:
    """Return True if ctx holds ``permission`` for a specific environment.

    Tries the project-wide check first (covers roles like project_admin,
    release_manager, prod_promoter that hold the permission across the whole
    project), then falls back to an environment-scoped grant from
    ``environment_members`` — the mechanism that lets a client_approver hold a
    permission for exactly one environment without being a project member.
    """
    environment = store.get_environment(environment_id)
    if environment is None:
        return False
    if check_permission(store, ctx, permission, project_id=environment.project_id):
        return True
    if ctx.user_id is None:
        return False
    member = store.get_environment_member(environment_id, str(ctx.user_id))
    if member is None:
        return False
    perm_cache = store.get_role_permissions()
    return permission in perm_cache.get(member.role, [])


def is_project_member(store: JobStore, ctx: AuthContext, project_id: int) -> bool:
    """Return True if ctx has access to project_id (platform_admin always does)."""
    if ctx.token_project_id is not None:
        return project_id == ctx.token_project_id
    if ctx.is_platform_admin:
        return True
    if not ctx.user_id:
        return False
    members = store.list_project_members(project_id)
    return any(m.user_id == str(ctx.user_id) for m in members)
