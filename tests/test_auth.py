"""Tests for hyqs.web.auth (real Postgres via the shared `store` fixture, no mocks)."""

from __future__ import annotations

import uuid

import pytest

from hyqs.web.auth import (
    AuthContext,
    check_env_permission,
    check_permission,
    is_project_member,
    resolve_api_token,
    resolve_auth,
)

_WEB_TOKEN = "the-web-token"


@pytest.fixture
def auth_project(store):
    project = store.create_project("Auth Test Project", f"/tmp/test-auth-{uuid.uuid4()}")
    yield project
    store.delete_project(project.id)


@pytest.fixture
def other_project(store):
    project = store.create_project("Other Auth Project", f"/tmp/test-auth-{uuid.uuid4()}")
    yield project
    store.delete_project(project.id)


def test_resolve_auth_returns_none_for_unknown_token(store):
    assert resolve_auth(store, "not-a-real-token", _WEB_TOKEN) is None


def test_resolve_auth_web_token_yields_platform_admin_context(store):
    ctx = resolve_auth(store, _WEB_TOKEN, _WEB_TOKEN)

    assert ctx is not None
    assert ctx.is_platform_admin is True
    assert ctx.token_project_id is None


def test_resolve_auth_api_token_yields_scoped_context(store, auth_project):
    _token, secret = store.create_api_token(auth_project.id, "CI", "contributor")

    ctx = resolve_auth(store, secret, _WEB_TOKEN)

    assert ctx is not None
    assert ctx.is_platform_admin is False
    assert ctx.user_id is None
    assert ctx.token_project_id == auth_project.id
    assert ctx.token_role == "contributor"
    assert ctx.api_token_id is not None


def test_resolve_auth_api_token_touches_last_used(store, auth_project):
    token, secret = store.create_api_token(auth_project.id, "CI", "viewer")
    assert token.last_used_at is None

    resolve_auth(store, secret, _WEB_TOKEN)

    [refreshed] = store.list_api_tokens(auth_project.id)
    assert refreshed.last_used_at is not None


def test_resolve_auth_revoked_api_token_fails(store, auth_project):
    token, secret = store.create_api_token(auth_project.id, "CI", "viewer")
    store.revoke_api_token(auth_project.id, token.id)

    assert resolve_auth(store, secret, _WEB_TOKEN) is None


# --- resolve_api_token (shared by resolve_auth and the MCP bearer-token path) -


def test_resolve_api_token_returns_none_for_unknown_secret(store):
    assert resolve_api_token(store, "hpat_not-a-real-secret") is None


def test_resolve_api_token_yields_same_scoped_context_as_resolve_auth(store, auth_project):
    _token, secret = store.create_api_token(auth_project.id, "CI", "contributor")

    direct = resolve_api_token(store, secret)
    via_resolve_auth = resolve_auth(store, secret, _WEB_TOKEN)

    assert direct == via_resolve_auth
    assert direct.token_project_id == auth_project.id
    assert direct.token_role == "contributor"
    assert direct.api_token_id is not None


def test_resolve_api_token_touches_last_used(store, auth_project):
    token, secret = store.create_api_token(auth_project.id, "CI", "viewer")
    assert token.last_used_at is None

    resolve_api_token(store, secret)

    [refreshed] = store.list_api_tokens(auth_project.id)
    assert refreshed.last_used_at is not None


def test_resolve_api_token_returns_none_for_revoked_secret(store, auth_project):
    token, secret = store.create_api_token(auth_project.id, "CI", "viewer")
    store.revoke_api_token(auth_project.id, token.id)

    assert resolve_api_token(store, secret) is None


def test_check_permission_token_scoped_to_bound_project(store, auth_project, other_project):
    _token, secret = store.create_api_token(auth_project.id, "CI", "project_admin")
    ctx = resolve_auth(store, secret, _WEB_TOKEN)

    assert check_permission(store, ctx, "edit_project", project_id=auth_project.id) is True
    assert check_permission(store, ctx, "edit_project", project_id=other_project.id) is False


def test_check_permission_token_role_grants_only_role_permissions(store, auth_project):
    _token, secret = store.create_api_token(auth_project.id, "CI", "contributor")
    ctx = resolve_auth(store, secret, _WEB_TOKEN)

    # contributor role grants queue_job, but not edit_project (project_admin-only).
    assert check_permission(store, ctx, "queue_job", project_id=auth_project.id) is True
    assert check_permission(store, ctx, "edit_project", project_id=auth_project.id) is False


def test_check_permission_token_automation_client_role_grants_and_denies(store, auth_project):
    _token, secret = store.create_api_token(auth_project.id, "CI", "automation_client")
    ctx = resolve_auth(store, secret, _WEB_TOKEN)

    for permission in ("queue_job", "cancel_job", "retry_job", "edit_job_deps", "resolve_job"):
        assert check_permission(store, ctx, permission, project_id=auth_project.id) is True

    # automation_client is contributor minus archive_job, plus never gets
    # project-admin-only permissions.
    for permission in ("archive_job", "edit_project", "manage_webhooks"):
        assert check_permission(store, ctx, permission, project_id=auth_project.id) is False


def test_check_permission_token_never_falls_through_to_platform_admin(store, auth_project):
    _token, secret = store.create_api_token(auth_project.id, "CI", "viewer")
    ctx = resolve_auth(store, secret, _WEB_TOKEN)

    # No project_id (a platform-wide check) must never be satisfied by a token.
    assert check_permission(store, ctx, "manage_users", project_id=None) is False


def test_is_project_member_token_scoped_to_bound_project(store, auth_project, other_project):
    _token, secret = store.create_api_token(auth_project.id, "CI", "viewer")
    ctx = resolve_auth(store, secret, _WEB_TOKEN)

    assert is_project_member(store, ctx, auth_project.id) is True
    assert is_project_member(store, ctx, other_project.id) is False


def test_check_permission_platform_admin_unaffected_by_token_fields(store, auth_project):
    ctx = AuthContext(user_id=None, user_email=None, is_platform_admin=True)

    assert check_permission(store, ctx, "manage_users") is True


def test_check_env_permission_scoped_grant_does_not_leak_to_other_env(store, auth_project):
    env_a = store.create_environment(auth_project.id, "client-a", "dev")
    env_b = store.create_environment(auth_project.id, "client-b", "dev")
    user = store.create_user(f"env-approver-{uuid.uuid4()}@example.com", "pw")
    store.add_environment_member(env_a.id, str(user.id), "client_approver")
    ctx = AuthContext(user_id=user.id, user_email=user.email, is_platform_admin=False)

    assert check_env_permission(store, ctx, "deploy.apply", env_a.id) is True
    assert check_env_permission(store, ctx, "deploy.apply", env_b.id) is False


def test_check_env_permission_project_admin_falls_through_project_wide(store, auth_project):
    env = store.create_environment(auth_project.id, "staging", "staging")
    user = store.create_user(f"env-project-admin-{uuid.uuid4()}@example.com", "pw")
    store.add_project_member(auth_project.id, str(user.id), "project_admin")
    ctx = AuthContext(user_id=user.id, user_email=user.email, is_platform_admin=False)

    assert check_env_permission(store, ctx, "deploy.promote", env.id) is True


def test_check_env_permission_unrelated_user_is_refused(store, auth_project):
    env = store.create_environment(auth_project.id, "staging", "staging")
    user = store.create_user(f"env-unrelated-{uuid.uuid4()}@example.com", "pw")
    ctx = AuthContext(user_id=user.id, user_email=user.email, is_platform_admin=False)

    assert check_env_permission(store, ctx, "deploy.apply", env.id) is False


def test_check_env_permission_returns_false_for_unknown_environment(store, auth_project):
    user = store.create_user(f"env-unknown-{uuid.uuid4()}@example.com", "pw")
    ctx = AuthContext(user_id=user.id, user_email=user.email, is_platform_admin=False)

    assert check_env_permission(store, ctx, "deploy.apply", 999999999) is False
