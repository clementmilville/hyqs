"""Regression tests for the manual REST authorization fixes (job #4305).

A security review found 8 state-changing/read REST routes in hyqs/web/app.py
that acted on an object without checking the caller's access to that
object's project. Part A below targets exactly those 8 routes. Part B is the
durable regression: it enumerates every state-changing (POST/PATCH/PUT/
DELETE) ``/api/*`` route registered on the real Starlette app and requires
each one to be either in the request table (proven to 403 a cross-project
caller) or in the explicit allowlist (with a documented reason) -- so a
newly added unguarded route fails the suite loudly instead of shipping
silently.

Follows the established pattern from tests/test_project_runtime_endpoints.py:
a per-test MagicMock store, build_app(config, orchestrator, store) wrapped in
starlette.testclient.TestClient, and a project-scoped API token minted for a
project OTHER than the target object's project. ``store.get_session.return_
value = None`` is required so a bare MagicMock store doesn't authenticate the
token as a platform admin. Per hyqs/web/auth.py's is_project_member/check_
permission, a token's ``token_project_id`` must equal the checked
project_id or the check returns False immediately -- so this deterministically
proves the guard without needing to fake missing permissions.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest
from starlette.routing import Route
from starlette.testclient import TestClient

from hyqs.config import Config
from hyqs.pipeline.models import (
    AgentSpec,
    ApiToken,
    BacklogItem,
    BacklogItemType,
    Epic,
    Job,
    Project,
    Webhook,
)
from hyqs.web.app import build_app

_WEB_TOKEN = "test-web-token"
_SCOPED_SECRET = "scoped-token-secret"  # pragma: allowlist secret

# The target objects below always belong to PROJECT_A. The caller's API token
# is always minted for PROJECT_B -- a project that never owns any of them --
# so every properly guarded route must reject it with 403.
PROJECT_A = 1
PROJECT_B = 2

JOB_ID = 42
EPIC_ID = 7
WEBHOOK_ID = 9
AGENT_ID = 3
TOKEN_ID = 4
ITEM_ID = 8
DEP_ID = 5


def _make_project(**overrides) -> Project:
    defaults = dict(id=PROJECT_A, name="Project A", repo_path="/tmp/project-a")
    defaults.update(overrides)
    return Project(**defaults)


def _make_job(**overrides) -> Job:
    defaults = dict(
        id=JOB_ID, idea="do work", repo_path="/tmp/project-a", chat_id=0, project_id=PROJECT_A
    )
    defaults.update(overrides)
    return Job(**defaults)


def _make_epic(**overrides) -> Epic:
    defaults = dict(id=EPIC_ID, project_id=PROJECT_A, name="Epic A")
    defaults.update(overrides)
    return Epic(**defaults)


def _make_webhook(**overrides) -> Webhook:
    defaults = dict(
        id=WEBHOOK_ID, project_id=PROJECT_A, url="https://example.com/hook", event_type="job.done"
    )
    defaults.update(overrides)
    return Webhook(**defaults)


def _make_agent(**overrides) -> AgentSpec:
    defaults = dict(id=AGENT_ID, project_id=PROJECT_A, name="agent-a")
    defaults.update(overrides)
    return AgentSpec(**defaults)


def _make_backlog_item(**overrides) -> BacklogItem:
    defaults = dict(
        id=ITEM_ID,
        project_id=PROJECT_A,
        title="Item",
        body="",
        type=BacklogItemType.IDEA,
        proposed_by="someone",
    )
    defaults.update(overrides)
    return BacklogItem(**defaults)


def _fully_stubbed_store() -> MagicMock:
    """A MagicMock store pre-loaded so every guarded handler's pre-check
    object load succeeds (finds an object owned by PROJECT_A) -- the caller's
    cross-project token is what must fail the request, not a 404."""
    store = MagicMock()
    store.get_session.return_value = None
    store.get_project.return_value = _make_project(id=PROJECT_A)
    store.get_project_by_repo.return_value = _make_project(id=PROJECT_A)
    store.get.return_value = _make_job(project_id=PROJECT_A)
    store.get_epic.return_value = _make_epic(project_id=PROJECT_A)
    store.get_webhook.return_value = _make_webhook(project_id=PROJECT_A)
    store.get_agent.return_value = _make_agent(project_id=PROJECT_A)
    store.get_backlog_item.return_value = _make_backlog_item(project_id=PROJECT_A)
    return store


def _build_client(store: MagicMock) -> TestClient:
    store.get_api_token_by_secret.return_value = ApiToken(
        id=1, project_id=PROJECT_B, name="scoped", role="project_admin", last4="abcd"
    )
    config = Config(model="sonnet", permission_mode="acceptEdits", web_token=_WEB_TOKEN)
    orchestrator = MagicMock()
    app = build_app(config, orchestrator, store)
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {_SCOPED_SECRET}"})
    return client


@pytest.fixture
def store() -> MagicMock:
    return _fully_stubbed_store()


# ---------------------------------------------------------------------------
# Part A: targeted 403 tests for the 8 routes fixed by this job
# ---------------------------------------------------------------------------


def test_unarchive_job_403_for_cross_project_token(store):
    client = _build_client(store)

    resp = client.post(f"/api/jobs/{JOB_ID}/unarchive")

    assert resp.status_code == 403
    store.set_archived.assert_not_called()


def test_set_job_priority_403_for_cross_project_token(store):
    client = _build_client(store)

    resp = client.post(f"/api/jobs/{JOB_ID}/priority", json={"priority": 5})

    assert resp.status_code == 403
    store.set_priority.assert_not_called()


def test_create_epic_403_for_cross_project_token(store):
    client = _build_client(store)

    resp = client.post("/api/epics", json={"project_id": PROJECT_A, "name": "New Epic"})

    assert resp.status_code == 403
    store.create_epic.assert_not_called()


def test_update_epic_403_for_cross_project_token(store):
    client = _build_client(store)

    resp = client.patch(f"/api/epics/{EPIC_ID}", json={"name": "Renamed"})

    assert resp.status_code == 403
    store.update_epic.assert_not_called()


def test_archive_epic_403_for_cross_project_token(store):
    client = _build_client(store)

    resp = client.post(f"/api/epics/{EPIC_ID}/archive")

    assert resp.status_code == 403
    store.set_archived_epic.assert_not_called()


def test_unarchive_epic_403_for_cross_project_token(store):
    client = _build_client(store)

    resp = client.post(f"/api/epics/{EPIC_ID}/unarchive")

    assert resp.status_code == 403
    store.set_archived_epic.assert_not_called()


def test_suggest_epic_for_job_403_for_cross_project_token(store):
    client = _build_client(store)

    resp = client.post("/api/epics/suggest", json={"project_id": PROJECT_A, "idea": "x"})

    assert resp.status_code == 403
    store.list_epics.assert_not_called()


def test_get_job_backlog_sources_403_for_cross_project_token(store):
    client = _build_client(store)

    resp = client.get(f"/api/jobs/{JOB_ID}/backlog-sources")

    assert resp.status_code == 403
    store.get_backlog_items_for_job.assert_not_called()


def test_job_chat_stream_403_for_cross_project_token(store):
    client = _build_client(store)

    resp = client.post(
        "/api/jobs/chat/stream",
        json={"project_id": PROJECT_A, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 403
    store.create_chat_session.assert_not_called()


# ---------------------------------------------------------------------------
# Part B: enumerate every state-changing /api/* route and require it to be
# either in the request table (proven to 403 a cross-project token) or in the
# allowlist (documented reason it's legitimately unauthenticated/platform-wide,
# or a known pre-existing gap explicitly out of this job's scope).
# ---------------------------------------------------------------------------

_MUTATING_METHODS = {"POST", "PATCH", "PUT", "DELETE"}

_PATH_PARAM_VALUES = {
    "job_id": JOB_ID,
    "id": PROJECT_A,
    "epic_id": EPIC_ID,
    "project_id": PROJECT_A,
    "webhook_id": WEBHOOK_ID,
    "agent_id": AGENT_ID,
    "token_id": TOKEN_ID,
    "item_id": ITEM_ID,
    "dep_id": DEP_ID,
    "user_id": "u1",
}

_PATH_PARAM_RE = re.compile(r"\{(\w+)(?::\w+)?\}")


def _concrete_path(template: str) -> str:
    return _PATH_PARAM_RE.sub(lambda m: str(_PATH_PARAM_VALUES[m.group(1)]), template)


# Routes that are legitimately unauthenticated or platform-wide (never scoped
# to a single project), so "the wrong project" doesn't apply to them.
_ALLOWLIST = {
    ("/api/public/page-view", "POST"): "unauthenticated public page-view beacon",
    ("/api/chat/stream", "POST"): "platform-wide admin_console permission",
    ("/api/chat", "POST"): "platform-wide admin_console permission",
    ("/api/auth/login", "POST"): "unauthenticated: this call establishes the session itself",
    ("/api/auth/logout", "POST"): "session-scoped, not project scoped",
    ("/api/auth/oauth/apple/callback", "POST"): "unauthenticated OAuth callback",
    ("/api/projects", "POST"): "platform-wide create_project permission, no target project yet",
    (
        "/api/projects/provision",
        "POST",
    ): "platform-wide create_project permission, no target project yet",
    ("/api/admin/roles/{role}/permissions", "PATCH"): "platform-wide manage_roles permission",
    ("/api/admin/users", "POST"): "platform-wide manage_users permission",
    (
        "/api/admin/users/{user_id:int}/projects",
        "POST",
    ): "platform-wide manage_users permission",
    ("/api/admin/users/{user_id:int}", "PATCH"): "platform-wide manage_users permission",
    ("/api/admin/users/{user_id:int}", "DELETE"): "platform-wide manage_users permission",
    (
        "/api/admin/users/{user_id:int}/platform-permissions",
        "POST",
    ): "platform_admin only, checked via request.state.is_platform_admin",
    (
        "/api/admin/users/{user_id:int}/platform-permissions/{permission}",
        "DELETE",
    ): "platform_admin only, checked via request.state.is_platform_admin",
    ("/api/admin/invitations", "POST"): "platform-wide manage_invitations permission",
    ("/api/admin/invitations/{token}", "DELETE"): "platform-wide manage_invitations permission",
    (
        "/api/intake/sessions",
        "POST",
    ): "platform-wide create_project permission, no project exists yet",
    (
        "/api/intake/sessions/{session_id}/message",
        "POST",
    ): "scoped to the session's creator, not a project",
    (
        "/api/intake/sessions/{session_id}/stream",
        "POST",
    ): "scoped to the session's creator, not a project",
    (
        "/api/intake/sessions/{session_id}/plan",
        "POST",
    ): "scoped to the session's creator, not a project",
    (
        "/api/intake/sessions/{session_id}/confirm",
        "POST",
    ): "scoped to the session's creator, not a project",
    (
        "/api/intake/sessions/{session_id}/abandon",
        "POST",
    ): "scoped to the session's creator, not a project",
    (
        "/api/intake/sessions/{session_id}/draft_spec",
        "PATCH",
    ): "scoped to the session's creator, not a project",
    ("/api/providers/{provider}/pause", "DELETE"): "platform-wide manage_providers permission",
}

# Every other state-changing /api/* route: the concrete request needed to
# reach its authorization check, plus a store mutation method that must never
# be called once the guard rejects the caller.
_REQUEST_TABLE = {
    ("/api/jobs/batch", "POST"): {
        "body": {"repo": "acme/repo", "jobs": [{"idea": "x"}]},
        "mutation": "ensure_project_for_repo",
    },
    ("/api/jobs", "POST"): {
        "body": {"repo": "acme/repo", "idea": "do the thing"},
        "mutation": "ensure_project_for_repo",
    },
    ("/api/jobs/chat/stream", "POST"): {
        "body": {"project_id": PROJECT_A, "messages": [{"role": "user", "content": "hi"}]},
        "mutation": "create_chat_session",
    },
    ("/api/jobs/archive-terminal", "POST"): {
        "body": {"project_id": PROJECT_A},
        "mutation": "archive_terminal",
    },
    ("/api/jobs/{job_id:int}", "DELETE"): {"mutation": "cancel"},
    ("/api/jobs/{job_id:int}/retry", "POST"): {"mutation": "retry"},
    ("/api/jobs/{job_id:int}/requeue", "POST"): {"mutation": "requeue_job_at_stage"},
    ("/api/jobs/{job_id:int}/fix-forward", "POST"): {"mutation": "fix_forward_and_repoint"},
    ("/api/jobs/{job_id:int}/idea", "PATCH"): {"mutation": "update_job_idea"},
    ("/api/jobs/{job_id:int}/resolve", "POST"): {"mutation": "set_job_resolution"},
    ("/api/jobs/{job_id:int}/priority", "POST"): {
        "body": {"priority": 5},
        "mutation": "set_priority",
    },
    ("/api/jobs/{job_id:int}/archive", "POST"): {"mutation": "set_archived"},
    ("/api/jobs/{job_id:int}/unarchive", "POST"): {"mutation": "set_archived"},
    ("/api/jobs/{job_id:int}/epic", "PATCH"): {"mutation": "update_job_epic"},
    ("/api/jobs/{job_id:int}/dependencies", "POST"): {"mutation": "add_job_dependency"},
    ("/api/jobs/{job_id:int}/dependencies/{dep_id:int}", "DELETE"): {
        "mutation": "remove_job_dependency"
    },
    ("/api/jobs/{job_id:int}/title", "PATCH"): {"mutation": "update_job_title"},
    ("/api/epics", "POST"): {
        "body": {"project_id": PROJECT_A, "name": "Epic"},
        "mutation": "create_epic",
    },
    ("/api/epics/suggest", "POST"): {
        "body": {"project_id": PROJECT_A, "idea": "x"},
        "mutation": "list_epics",
    },
    ("/api/epics/{epic_id:int}", "PATCH"): {"mutation": "update_epic"},
    ("/api/epics/{epic_id:int}/suggest", "POST"): {
        "body": {"goal": "suggest jobs"},
        "mutation": "get_symbol_index_text",
    },
    ("/api/epics/{epic_id:int}/suggest/stream", "POST"): {
        "body": {"goal": "suggest jobs"},
        "mutation": "get_symbol_index_text",
    },
    ("/api/epics/{epic_id:int}/architect-plan/stream", "POST"): {
        "body": {"goal": "plan jobs"},
        "mutation": "get_symbol_index_text",
    },
    ("/api/epics/{epic_id:int}/archive", "POST"): {"mutation": "set_archived_epic"},
    ("/api/epics/{epic_id:int}/unarchive", "POST"): {"mutation": "set_archived_epic"},
    ("/api/projects/{project_id:int}", "PATCH"): {"mutation": "update_project"},
    ("/api/projects/{project_id:int}", "DELETE"): {"mutation": "delete_project"},
    ("/api/projects/{id:int}/deploy", "POST"): {"mutation": "create"},
    ("/api/projects/{id:int}/restart", "POST"): {"mutation": "create"},
    ("/api/projects/{id:int}/stop", "POST"): {
        "body": {"confirm": True},
        "patch_target": "hyqs.pipeline.docker_deploy.is_user_project",
    },
    ("/api/projects/{id:int}/start", "POST"): {
        "patch_target": "hyqs.pipeline.docker_deploy.is_user_project",
    },
    ("/api/projects/{id:int}/backlog/refine", "POST"): {"mutation": "get_project"},
    ("/api/projects/{id:int}/backlog", "POST"): {"mutation": "create_backlog_item"},
    ("/api/backlog/{item_id:int}/vote", "POST"): {"mutation": "vote_backlog_item"},
    ("/api/backlog/{item_id:int}", "PATCH"): {"mutation": "patch_backlog_item"},
    ("/api/projects/{project_id:int}/agents", "POST"): {"mutation": "create_agent"},
    ("/api/agents/{agent_id:int}", "PATCH"): {"mutation": "update_agent"},
    ("/api/agents/{agent_id:int}", "DELETE"): {"mutation": "delete_agent"},
    ("/api/projects/{project_id:int}/members", "POST"): {"mutation": "add_project_member"},
    ("/api/projects/{project_id:int}/members/{user_id}", "DELETE"): {
        "mutation": "remove_project_member"
    },
    ("/api/projects/{project_id:int}/members/{user_id}", "PATCH"): {
        "mutation": "update_member_role"
    },
    ("/api/projects/{project_id:int}/tokens", "POST"): {"mutation": "create_api_token"},
    ("/api/projects/{project_id:int}/tokens/{token_id:int}", "DELETE"): {
        "mutation": "revoke_api_token"
    },
    ("/api/projects/{project_id:int}/webhooks", "POST"): {"mutation": "create_webhook"},
    ("/api/webhooks/{webhook_id:int}/active", "POST"): {"mutation": "set_webhook_active"},
    ("/api/webhooks/{webhook_id:int}", "DELETE"): {"mutation": "delete_webhook"},
    ("/api/projects/{project_id:int}/slack-credentials", "POST"): {
        "mutation": "set_project_slack_token"
    },
}


def _iter_mutating_api_routes():
    store = _fully_stubbed_store()
    config = Config(model="sonnet", permission_mode="acceptEdits", web_token=_WEB_TOKEN)
    app = build_app(config, MagicMock(), store)
    # build_app wraps the real Starlette app in CorsPreflightASGI for OPTIONS
    # handling; unwrap to reach the object that actually holds .routes.
    starlette_app = getattr(app, "app", app)
    seen = []
    for route in starlette_app.routes:
        if not isinstance(route, Route):
            continue
        if not route.path.startswith("/api/"):
            continue
        methods = route.methods or set()
        for method in methods & _MUTATING_METHODS:
            seen.append((route.path, method))
    return seen


def test_every_mutating_api_route_is_guarded_or_allowlisted():
    unhandled = [
        key
        for key in _iter_mutating_api_routes()
        if key not in _REQUEST_TABLE and key not in _ALLOWLIST
    ]
    assert not unhandled, (
        "New/unrecognized state-changing /api/* route(s) with no authorization "
        f"coverage in this regression test: {unhandled}. Add a request-table "
        "entry proving it 403s a cross-project token, or an allowlist entry "
        "with a documented reason."
    )


@pytest.mark.parametrize(
    "path_template,method",
    sorted(_REQUEST_TABLE.keys()),
    ids=lambda v: f"{v}" if isinstance(v, str) else None,
)
def test_route_forbidden_for_cross_project_token(path_template, method):
    row = _REQUEST_TABLE[(path_template, method)]
    store = _fully_stubbed_store()
    client = _build_client(store)
    path = _concrete_path(path_template)
    kwargs = {"json": row["body"]} if "body" in row else {}

    patch_target = row.get("patch_target")
    if patch_target:
        with patch(patch_target) as mocked:
            resp = client.request(method, path, **kwargs)
        mocked.assert_not_called()
    else:
        resp = client.request(method, path, **kwargs)

    assert resp.status_code == 403, f"{method} {path} expected 403, got {resp.status_code}"
    mutation = row.get("mutation")
    if mutation:
        getattr(store, mutation).assert_not_called()
