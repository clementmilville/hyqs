import asyncio
import json
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.server.auth.auth import AccessToken as FastMCPAccessToken
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser, RequireAuthMiddleware

from hyqs.config import Config
from hyqs.pipeline.gitops import GitResult
from hyqs.pipeline.models import (
    JobSource,
    JobStatus,
    SchedulerWait,
    SchedulerWaitReason,
    Stage,
    lock_owner_id,
)
from hyqs.web import mcp_oauth, mcp_server
from hyqs.web.app import (
    _active_remediation_conflict,
    agent_to_dict,
    job_projection_extras,
    job_to_dict,
)
from hyqs.web.auth import AuthContext


class _FakeState:
    def __init__(self, ctx: AuthContext) -> None:
        self.auth_context = ctx


class _FakeRequest:
    def __init__(self, ctx: AuthContext) -> None:
        self.state = _FakeState(ctx)


@pytest.fixture
def mcp(store):
    config = Config(model="sonnet", permission_mode="acceptEdits")
    return mcp_server.build_mcp_server(store, config)


def _set_ctx(monkeypatch, ctx: AuthContext) -> None:
    monkeypatch.setattr(mcp_server, "get_http_request", lambda: _FakeRequest(ctx))


def _call(mcp, tool_name: str, **kwargs):
    async def _run():
        tool = await mcp.get_tool(tool_name)
        return await tool.fn(**kwargs)

    return asyncio.run(_run())


def _member_project(store, role: str = "viewer"):
    project = store.create_project("MCP Test Project", f"/tmp/test-mcp-{uuid.uuid4()}")
    user_id = "1001"
    store.add_project_member(project.id, user_id, role)
    ctx = AuthContext(
        user_id=int(user_id), user_email="member@example.com", is_platform_admin=False
    )
    return project, ctx


def _non_member_ctx():
    return AuthContext(user_id=9999, user_email="outsider@example.com", is_platform_admin=False)


# --- create_epic -------------------------------------------------------------


def test_create_epic_member_success(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "create_epic", project_id=project.id, name="New Epic", description="desc")
    assert result["name"] == "New Epic"
    assert result["project_id"] == project.id
    fetched = store.get_epic(result["id"])
    assert fetched is not None
    assert fetched.name == "New Epic"


def test_create_epic_non_member_forbidden(store, mcp, monkeypatch):
    project, _ = _member_project(store)
    _set_ctx(monkeypatch, _non_member_ctx())
    result = _call(mcp, "create_epic", project_id=project.id, name="New Epic")
    assert result.startswith("error: forbidden")
    assert store.list_epics(project_id=project.id) == []


def test_create_epic_empty_name_rejected(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "create_epic", project_id=project.id, name="  ")
    assert result.startswith("error:")
    assert store.list_epics(project_id=project.id) == []


# --- update_epic ---------------------------------------------------------------


def test_update_epic_member_success(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    epic = store.create_epic(project.id, "Original", "orig desc")
    _set_ctx(monkeypatch, ctx)
    result = _call(
        mcp,
        "update_epic",
        epic_id=epic.id,
        name="Updated",
        description="new desc",
        status="paused",
        archived=True,
    )
    assert result["name"] == "Updated"
    assert result["description"] == "new desc"
    assert result["status"] == "paused"
    assert result["archived"] is True
    fetched = store.get_epic(epic.id)
    assert fetched.name == "Updated"
    assert fetched.archived is True


def test_update_epic_non_member_forbidden(store, mcp, monkeypatch):
    project, _ = _member_project(store)
    epic = store.create_epic(project.id, "Original")
    _set_ctx(monkeypatch, _non_member_ctx())
    result = _call(mcp, "update_epic", epic_id=epic.id, name="Updated")
    assert result.startswith("error: forbidden")
    assert store.get_epic(epic.id).name == "Original"


def test_update_epic_unknown_id_returns_error(store, mcp, monkeypatch):
    _, ctx = _member_project(store)
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "update_epic", epic_id=999999999, name="Updated")
    assert result == "error: epic not found"


def test_update_epic_invalid_status_rejected(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    epic = store.create_epic(project.id, "Original")
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "update_epic", epic_id=epic.id, status="bogus")
    assert result.startswith("error:")
    assert store.get_epic(epic.id).status != "bogus"


# --- add_backlog_item ------------------------------------------------------------


def test_add_backlog_item_member_success(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="viewer")
    _set_ctx(monkeypatch, ctx)
    result = _call(
        mcp,
        "add_backlog_item",
        project_id=project.id,
        title="Do the thing",
        body="details",
        type="idea",
    )
    assert result["title"] == "Do the thing"
    fetched = store.get_backlog_item(result["id"])
    assert fetched is not None
    assert fetched.title == "Do the thing"


def test_add_backlog_item_non_member_forbidden(store, mcp, monkeypatch):
    project, _ = _member_project(store)
    _set_ctx(monkeypatch, _non_member_ctx())
    result = _call(mcp, "add_backlog_item", project_id=project.id, title="Do the thing")
    assert result.startswith("error: forbidden")
    assert store.list_backlog_items(project.id) == []


def test_add_backlog_item_empty_title_rejected(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "add_backlog_item", project_id=project.id, title="   ")
    assert result.startswith("error:")
    assert store.list_backlog_items(project.id) == []


def test_add_backlog_item_invalid_type_rejected(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    _set_ctx(monkeypatch, ctx)
    result = _call(
        mcp, "add_backlog_item", project_id=project.id, title="Do the thing", type="task"
    )
    assert result.startswith("error:")
    assert store.list_backlog_items(project.id) == []


# --- register_webhook -------------------------------------------------------------


def test_register_webhook_admin_success(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    _set_ctx(monkeypatch, ctx)
    result = _call(
        mcp,
        "register_webhook",
        project_id=project.id,
        url="https://example.com/hook",
        event_type="job_complete",
    )
    assert result["url"] == "https://example.com/hook"
    assert result["event_type"] == "job_complete"
    fetched = store.list_webhooks(project.id)
    assert len(fetched) == 1
    assert fetched[0].url == "https://example.com/hook"


def test_register_webhook_needs_attention_accepted(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    _set_ctx(monkeypatch, ctx)
    result = _call(
        mcp,
        "register_webhook",
        project_id=project.id,
        url="https://example.com/hook",
        event_type="needs_attention",
    )
    assert result["event_type"] == "needs_attention"
    fetched = store.list_webhooks(project.id)
    assert len(fetched) == 1
    assert fetched[0].url == "https://example.com/hook"


def test_register_webhook_viewer_forbidden_missing_permission(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="viewer")
    _set_ctx(monkeypatch, ctx)
    result = _call(
        mcp,
        "register_webhook",
        project_id=project.id,
        url="https://example.com/hook",
        event_type="job_complete",
    )
    assert result.startswith("error: forbidden")
    assert store.list_webhooks(project.id) == []


def test_register_webhook_non_member_forbidden(store, mcp, monkeypatch):
    project, _ = _member_project(store)
    _set_ctx(monkeypatch, _non_member_ctx())
    result = _call(
        mcp,
        "register_webhook",
        project_id=project.id,
        url="https://example.com/hook",
        event_type="job_complete",
    )
    assert result.startswith("error: forbidden")
    assert store.list_webhooks(project.id) == []


def test_register_webhook_invalid_event_type_rejected(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    _set_ctx(monkeypatch, ctx)
    result = _call(
        mcp,
        "register_webhook",
        project_id=project.id,
        url="https://example.com/hook",
        event_type="bogus",
    )
    assert result.startswith("error:")
    assert store.list_webhooks(project.id) == []


def test_register_webhook_invalid_url_rejected(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    _set_ctx(monkeypatch, ctx)
    result = _call(
        mcp,
        "register_webhook",
        project_id=project.id,
        url="not-a-url",
        event_type="job_complete",
    )
    assert result.startswith("error:")
    assert store.list_webhooks(project.id) == []


# --- list_webhooks -----------------------------------------------------------------


def test_list_webhooks_member_success(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="viewer")
    store.create_webhook(project.id, "https://example.com/hook", "deploy", "admin@example.com")
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "list_webhooks", project_id=project.id)
    assert len(result) == 1
    assert result[0]["url"] == "https://example.com/hook"


def test_list_webhooks_non_member_forbidden(store, mcp, monkeypatch):
    project, _ = _member_project(store)
    store.create_webhook(project.id, "https://example.com/hook", "deploy")
    _set_ctx(monkeypatch, _non_member_ctx())
    result = _call(mcp, "list_webhooks", project_id=project.id)
    assert result.startswith("error: forbidden")


def test_register_webhook_http_kind_unchanged(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    _set_ctx(monkeypatch, ctx)
    result = _call(
        mcp,
        "register_webhook",
        project_id=project.id,
        url="https://example.com/hook",
        event_type="job_complete",
    )
    assert result["url"] == "https://example.com/hook"
    assert result["event_type"] == "job_complete"
    assert result["kind"] == "http"
    fetched = store.list_webhooks(project.id)
    assert len(fetched) == 1
    assert fetched[0].kind == "http"


def test_register_webhook_http_kind_explicit_unchanged(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    _set_ctx(monkeypatch, ctx)
    result = _call(
        mcp,
        "register_webhook",
        project_id=project.id,
        url="https://example.com/hook",
        event_type="job_complete",
        kind="http",
    )
    assert result["url"] == "https://example.com/hook"
    assert result["kind"] == "http"


def test_register_webhook_slack_kind_success(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    _set_ctx(monkeypatch, ctx)
    result = _call(
        mcp,
        "register_webhook",
        project_id=project.id,
        url="C0123456",
        event_type="job_complete",
        kind="slack",
    )
    assert result["url"] == "C0123456"
    assert result["kind"] == "slack"
    fetched = store.list_webhooks(project.id)
    assert len(fetched) == 1
    assert fetched[0].kind == "slack"


def test_register_webhook_slack_kind_invalid_url_rejected(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    _set_ctx(monkeypatch, ctx)
    result = _call(
        mcp,
        "register_webhook",
        project_id=project.id,
        url="https://evil.example",
        event_type="job_complete",
        kind="slack",
    )
    assert result.startswith("error:")
    assert store.list_webhooks(project.id) == []


# --- set_webhook_active / delete_webhook ------------------------------------------


def test_set_webhook_active_member_success(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    webhook = store.create_webhook(
        project.id, "https://example.com/hook", "deploy", "admin@example.com"
    )
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "set_webhook_active", webhook_id=webhook.id, active=False)
    assert result["active"] is False
    assert store.get_webhook(webhook.id).active is False


def test_set_webhook_active_unknown_id_returns_error(store, mcp, monkeypatch):
    _, ctx = _member_project(store, role="project_admin")
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "set_webhook_active", webhook_id=999999999, active=False)
    assert result == "error: webhook not found"


def test_set_webhook_active_non_member_forbidden(store, mcp, monkeypatch):
    project, _ = _member_project(store, role="project_admin")
    webhook = store.create_webhook(
        project.id, "https://example.com/hook", "deploy", "admin@example.com"
    )
    _set_ctx(monkeypatch, _non_member_ctx())
    result = _call(mcp, "set_webhook_active", webhook_id=webhook.id, active=False)
    assert result.startswith("error: forbidden")
    assert store.get_webhook(webhook.id).active is True


def test_delete_webhook_member_success(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    webhook = store.create_webhook(
        project.id, "https://example.com/hook", "deploy", "admin@example.com"
    )
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "delete_webhook", webhook_id=webhook.id)
    assert result == {"deleted": True, "id": webhook.id}
    assert store.get_webhook(webhook.id) is None


def test_delete_webhook_unknown_id_returns_error(store, mcp, monkeypatch):
    _, ctx = _member_project(store, role="project_admin")
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "delete_webhook", webhook_id=999999999)
    assert result == "error: webhook not found"


def test_delete_webhook_non_member_forbidden(store, mcp, monkeypatch):
    project, _ = _member_project(store, role="project_admin")
    webhook = store.create_webhook(
        project.id, "https://example.com/hook", "deploy", "admin@example.com"
    )
    _set_ctx(monkeypatch, _non_member_ctx())
    result = _call(mcp, "delete_webhook", webhook_id=webhook.id)
    assert result.startswith("error: forbidden")
    assert store.get_webhook(webhook.id) is not None


# --- set_slack_credential ----------------------------------------------------------


def test_set_slack_credential_member_success(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    _set_ctx(monkeypatch, ctx)
    result = _call(
        mcp, "set_slack_credential", project_id=project.id, bot_token="xoxb-super-secret-token"
    )
    assert result == {"project_id": project.id, "configured": True}
    assert "xoxb-super-secret-token" not in str(result)
    assert store.get_project_slack_token(project.id) == "xoxb-super-secret-token"


def test_set_slack_credential_viewer_forbidden_missing_permission(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="viewer")
    _set_ctx(monkeypatch, ctx)
    result = _call(
        mcp, "set_slack_credential", project_id=project.id, bot_token="xoxb-super-secret-token"
    )
    assert result.startswith("error: forbidden")
    assert "xoxb-super-secret-token" not in str(result)
    assert store.get_project_slack_token(project.id) is None


def test_set_slack_credential_non_member_forbidden(store, mcp, monkeypatch):
    project, _ = _member_project(store)
    _set_ctx(monkeypatch, _non_member_ctx())
    result = _call(
        mcp, "set_slack_credential", project_id=project.id, bot_token="xoxb-super-secret-token"
    )
    assert result.startswith("error: forbidden")
    assert "xoxb-super-secret-token" not in str(result)
    assert store.get_project_slack_token(project.id) is None


# --- get_project_spec ---------------------------------------------------------


def test_get_project_spec_member_success(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    store.create_agent(project.id, "Agent One", "claude", "sonnet")
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "get_project_spec", project_id=project.id)
    assert result["project"]["id"] == project.id
    assert result["agents"] == [agent_to_dict(a) for a in store.list_agents(project.id)]


def test_get_project_spec_non_member_forbidden(store, mcp, monkeypatch):
    project, _ = _member_project(store)
    _set_ctx(monkeypatch, _non_member_ctx())
    result = _call(mcp, "get_project_spec", project_id=project.id)
    assert result.startswith("error: forbidden")


def test_get_project_spec_unknown_project_returns_error(store, mcp, monkeypatch):
    admin_ctx = AuthContext(user_id=None, user_email=None, is_platform_admin=True)
    _set_ctx(monkeypatch, admin_ctx)
    result = _call(mcp, "get_project_spec", project_id=999999999)
    assert result == "error: project not found"


# --- update_project -----------------------------------------------------------


def test_update_project_admin_success(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    _set_ctx(monkeypatch, ctx)
    result = _call(
        mcp,
        "update_project",
        project_id=project.id,
        name="Renamed",
        description="new desc",
        status="paused",
        stack="node",
    )
    assert result["name"] == "Renamed"
    assert result["status"] == "paused"
    fetched = store.get_project(project.id)
    assert fetched.name == "Renamed"
    assert fetched.description == "new desc"
    assert fetched.status == "paused"
    assert fetched.stack == "node"


def test_update_project_viewer_forbidden(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="viewer")
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "update_project", project_id=project.id, name="Renamed")
    assert result.startswith("error: forbidden")
    assert store.get_project(project.id).name == project.name


def test_update_project_invalid_status_rejected(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "update_project", project_id=project.id, status="bogus")
    assert result.startswith("error:")
    assert store.get_project(project.id).status != "bogus"


def test_update_project_invalid_max_fix_attempts_rejected(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "update_project", project_id=project.id, max_fix_attempts=0)
    assert result.startswith("error:")
    assert store.get_project(project.id).max_fix_attempts != 0


def test_update_project_unknown_project_returns_error(store, mcp, monkeypatch):
    admin_ctx = AuthContext(user_id=None, user_email=None, is_platform_admin=True)
    _set_ctx(monkeypatch, admin_ctx)
    result = _call(mcp, "update_project", project_id=999999999, name="Renamed")
    assert result == "error: project not found"


# --- resync_nginx_vhost ---------------------------------------------------------


def test_resync_nginx_vhost_admin_success_writes_all_configured_domains(
    store, mcp, monkeypatch, tmp_path
):
    # Pin the domain allowlist: it is read from HYQS_NGINX_DOMAINS at call time,
    # so without this the test would depend on the host's own .env.
    monkeypatch.setenv("HYQS_NGINX_DOMAINS", "example.com,example.org")
    project, ctx = _member_project(store, role="project_admin")
    store.update_project(
        project.id,
        deploy_config=json.dumps({"port": 9001, "extra_domains": ["example.org"]}),
    )
    _set_ctx(monkeypatch, ctx)

    calls: list[dict] = []

    async def _fake_register_site(slug, port, *, domains=None, **kwargs):
        calls.append({"slug": slug, "port": port, "domains": domains})
        (tmp_path / f"{slug}.conf").write_text(
            mcp_server.nginx_sites.render_site(slug, port, domains=domains)
        )

    monkeypatch.setattr(mcp_server.nginx_sites, "register_site", _fake_register_site)

    result = _call(mcp, "resync_nginx_vhost", project_id=project.id)

    assert result["port"] == 9001
    assert result["domains"] == ["example.com", "example.org"]
    assert calls == [
        {
            "slug": result["slug"],
            "port": 9001,
            "domains": ["example.com", "example.org"],
        }
    ]
    slug = result["slug"]
    content = (tmp_path / f"{slug}.conf").read_text()
    assert f"server_name {slug}.example.com;" in content
    assert f"server_name {slug}.example.org;" in content


def test_resync_nginx_vhost_sanitizes_slug_from_repo_path(store, mcp, monkeypatch, tmp_path):
    malicious_repo_dir = f"evil.com;}} server{{ #{uuid.uuid4()}"
    project = store.create_project("Evil Project", f"/tmp/{malicious_repo_dir}")
    user_id = "1001"
    store.add_project_member(project.id, user_id, "project_admin")
    ctx = AuthContext(
        user_id=int(user_id), user_email="member@example.com", is_platform_admin=False
    )
    store.update_project(project.id, deploy_config=json.dumps({"port": 9001}))
    _set_ctx(monkeypatch, ctx)

    calls: list[dict] = []

    async def _fake_register_site(slug, port, *, domains=None, **kwargs):
        calls.append({"slug": slug, "port": port, "domains": domains})

    monkeypatch.setattr(mcp_server.nginx_sites, "register_site", _fake_register_site)

    result = _call(mcp, "resync_nginx_vhost", project_id=project.id)

    assert result["slug"] == mcp_server.slugify(malicious_repo_dir)
    for forbidden in (";", "}", "{", " "):
        assert forbidden not in result["slug"]
    assert calls[0]["slug"] == result["slug"]


def test_resync_nginx_vhost_viewer_forbidden(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="viewer")
    store.update_project(project.id, deploy_config=json.dumps({"port": 9001}))
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "resync_nginx_vhost", project_id=project.id)
    assert result.startswith("error: forbidden")


def test_resync_nginx_vhost_unknown_project_returns_error(store, mcp, monkeypatch):
    admin_ctx = AuthContext(user_id=None, user_email=None, is_platform_admin=True)
    _set_ctx(monkeypatch, admin_ctx)
    result = _call(mcp, "resync_nginx_vhost", project_id=999999999)
    assert result == "error: project not found"


def test_resync_nginx_vhost_missing_port_returns_error(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    store.update_project(project.id, deploy_config=json.dumps({}))
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "resync_nginx_vhost", project_id=project.id)
    assert result.startswith("error:")
    assert "port" in result


def test_resync_nginx_vhost_unknown_extra_domain_returns_error(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    store.update_project(
        project.id,
        deploy_config=json.dumps({"port": 9001, "extra_domains": ["not-a-domain.com"]}),
    )
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "resync_nginx_vhost", project_id=project.id)
    assert result.startswith("error: nginx resync failed: no SSL snippet configured")


def test_resync_nginx_vhost_invalid_extra_domains_returns_error(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    store.update_project(
        project.id,
        deploy_config=json.dumps({"port": 9001, "extra_domains": [123]}),
    )
    _set_ctx(monkeypatch, ctx)

    result = _call(mcp, "resync_nginx_vhost", project_id=project.id)

    assert result == "error: nginx resync failed: each nginx domain must be a string"


# --- watch_job -----------------------------------------------------------------


class _StubCtx:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def info(self, message, logger_name=None, extra=None):
        self.messages.append(message)


def test_watch_job_member_success_returns_final_job_and_events(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    _set_ctx(monkeypatch, ctx)
    job = store.create(
        idea="watch me", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    store.add_event(job.id, "plan", "done")
    store.add_event(job.id, "build", "done")
    job.status = JobStatus.DONE
    store.save(job)
    stub_ctx = _StubCtx()
    try:
        result = _call(mcp, "watch_job", job_id=job.id, timeout_seconds=5, ctx=stub_ctx)
        assert result["job"]["id"] == job.id
        assert result["job"]["status"] == "done"
        assert len(result["events"]) == 2
        assert result["timed_out"] is False
        assert len(stub_ctx.messages) == 2
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_events WHERE job_id = %s", (job.id,))
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_watch_job_detects_done_partway_through_polling(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    _set_ctx(monkeypatch, ctx)
    job = store.create(
        idea="watch me later", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )

    async def _flip_to_done_soon():
        await asyncio.sleep(0.1)
        job.status = JobStatus.DONE
        store.save(job)

    async def _run():
        task = asyncio.create_task(_flip_to_done_soon())
        tool = await mcp.get_tool("watch_job")
        try:
            return await tool.fn(job_id=job.id, timeout_seconds=5)
        finally:
            await task

    try:
        result = asyncio.run(_run())
        assert result["timed_out"] is False
        assert result["job"]["status"] == "done"
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_events WHERE job_id = %s", (job.id,))
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_watch_job_never_terminal_times_out(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    _set_ctx(monkeypatch, ctx)
    job = store.create(
        idea="never finishes", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    try:
        result = _call(mcp, "watch_job", job_id=job.id, timeout_seconds=1)
        assert result["timed_out"] is True
        assert result["job"]["status"] == "pending"
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_events WHERE job_id = %s", (job.id,))
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_watch_job_non_member_forbidden(store, mcp, monkeypatch):
    project, _ = _member_project(store)
    job = store.create(
        idea="not yours", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_ctx(monkeypatch, _non_member_ctx())
    try:
        result = _call(mcp, "watch_job", job_id=job.id)
        assert result.startswith("error: forbidden")
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_events WHERE job_id = %s", (job.id,))
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_watch_job_unknown_job_returns_error(store, mcp, monkeypatch):
    _, ctx = _member_project(store)
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "watch_job", job_id=999999999)
    assert result == "error: job not found"


def test_watch_job_clamps_oversized_timeout(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    _set_ctx(monkeypatch, ctx)
    job = store.create(
        idea="huge timeout", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    real_sleep = asyncio.sleep
    sleep_calls = []

    async def _fast_sleep(seconds):
        sleep_calls.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    try:
        result = _call(mcp, "watch_job", job_id=job.id, timeout_seconds=10_000_000)
        assert result["timed_out"] is True
        assert len(sleep_calls) == 601
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_events WHERE job_id = %s", (job.id,))
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


# --- job_supervisor_events ----------------------------------------------------


def test_job_supervisor_events_member_success(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    _set_ctx(monkeypatch, ctx)
    job = store.create(
        idea="janitor watched me", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    store.record_supervisor_event(job.id, action="requeue", failure_class="transient", detail="")
    store.record_supervisor_event(job.id, action="file_gate_fix", failure_class="lint", detail="")
    try:
        result = _call(mcp, "job_supervisor_events", job_id=job.id)
        expected = store.list_supervisor_events_for_job(job.id)
        assert len(result) == 2
        assert [r["action"] for r in result] == [e["action"] for e in expected]
        assert [r["failure_class"] for r in result] == [e["failure_class"] for e in expected]
        assert all(r["job_id"] == job.id for r in result)
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM supervisor_events WHERE job_id = %s", (job.id,))
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_job_supervisor_events_non_member_forbidden(store, mcp, monkeypatch):
    project, _ = _member_project(store)
    job = store.create(
        idea="not yours", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_ctx(monkeypatch, _non_member_ctx())
    try:
        result = _call(mcp, "job_supervisor_events", job_id=job.id)
        assert result.startswith("error: forbidden")
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_job_supervisor_events_unknown_job_returns_error(store, mcp, monkeypatch):
    _, ctx = _member_project(store)
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "job_supervisor_events", job_id=999999999)
    assert result == "error: job not found"


# --- waiting_on (get_job / list_jobs) -----------------------------------------


def test_get_job_returns_real_waiting_on(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    blocker = store.create(
        idea="blocker", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    job = store.create(idea="blocked", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP)
    store.add_job_dependency(job.id, blocker.id)
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "get_job", job_id=job.id)
        assert result["waiting_on"] == [blocker.id]
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_dependencies WHERE job_id = %s", (job.id,))
            conn.execute("DELETE FROM jobs WHERE id IN (%s, %s)", (job.id, blocker.id))


def test_get_job_dependency_graph_keeps_satisfied_upstream_edge(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    upstream = store.create(
        idea="completed upstream", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    job = store.create(
        idea="consumer", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    store.add_job_dependency(job.id, upstream.id)
    with store._pool.connection() as conn:
        conn.execute(
            "UPDATE jobs SET status = %s WHERE id = %s", (JobStatus.DONE.value, upstream.id)
        )
    _set_ctx(monkeypatch, ctx)
    try:
        graph = _call(mcp, "get_job_dependency_graph", job_id=job.id)
        fetched_job = _call(mcp, "get_job", job_id=job.id)
        assert graph["depends_on_jobs"] == [
            {"id": upstream.id, "title": upstream.title, "status": JobStatus.DONE.value}
        ]
        assert graph["dependents"] == []
        assert fetched_job["waiting_on"] == []
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_dependencies WHERE job_id = %s", (job.id,))
            conn.execute("DELETE FROM jobs WHERE id IN (%s, %s)", (job.id, upstream.id))


def test_get_job_dependency_graph_returns_empty_lists_without_edges(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    job = store.create(
        idea="isolated", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_ctx(monkeypatch, ctx)
    try:
        assert _call(mcp, "get_job_dependency_graph", job_id=job.id) == {
            "depends_on_jobs": [],
            "dependents": [],
        }
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_list_jobs_returns_real_waiting_on(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    blocker = store.create(
        idea="blocker", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    job = store.create(idea="blocked", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP)
    store.add_job_dependency(job.id, blocker.id)
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "list_jobs", project_id=project.id, status="all")
        by_id = {j["id"]: j for j in result}
        assert by_id[job.id]["waiting_on"] == [blocker.id]
        assert by_id[blocker.id]["waiting_on"] == []
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_dependencies WHERE job_id = %s", (job.id,))
            conn.execute("DELETE FROM jobs WHERE id IN (%s, %s)", (job.id, blocker.id))


def test_scheduler_wait_plumbs_through_query_and_mutation_tools(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    job = store.create(
        idea="a job that might be scheduler-blocked",
        repo_path=project.repo_path,
        chat_id=1,
        source=JobSource.MCP,
    )
    wait = SchedulerWait(
        reason=SchedulerWaitReason.PROVIDER_CAPACITY,
        summary="waiting on provider capacity",
        blocking_job_ids=[],
        conflicting_paths=[],
    )
    monkeypatch.setattr(store, "get_scheduler_wait", lambda j, waiting_on: wait)
    _set_ctx(monkeypatch, ctx)
    expected = wait.to_dict()
    try:
        assert _call(mcp, "get_job", job_id=job.id)["scheduler_wait"] == expected
        assert _call(mcp, "get_job", job_id=job.id, summary=True)["scheduler_wait"] == expected

        summary_list = _call(mcp, "list_jobs", project_id=project.id, status="all", summary=True)
        assert next(j for j in summary_list if j["id"] == job.id)["scheduler_wait"] == expected

        full_list = _call(mcp, "list_jobs", project_id=project.id, status="all")
        assert next(j for j in full_list if j["id"] == job.id)["scheduler_wait"] == expected

        # job is still PENDING here, so archive_job skips the real git/GitHub
        # cleanup it would otherwise run for a terminal job.
        archive_result = _call(mcp, "archive_job", job_id=job.id)
        assert archive_result["job"]["scheduler_wait"] == expected

        cancel_result = _call(mcp, "cancel_job", job_id=job.id)
        assert cancel_result["job"]["scheduler_wait"] == expected
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_get_job_forbidden_for_non_member_never_computes_scheduler_wait(store, mcp, monkeypatch):
    project, _ = _member_project(store)
    job = store.create(
        idea="a job blocked by another",
        repo_path=project.repo_path,
        chat_id=1,
        source=JobSource.MCP,
    )

    def _boom(*args, **kwargs):
        raise AssertionError("get_scheduler_wait must not run for a forbidden caller")

    monkeypatch.setattr(store, "get_scheduler_wait", _boom)
    _set_ctx(monkeypatch, _non_member_ctx())
    try:
        result = _call(mcp, "get_job", job_id=job.id)
        assert result == "error: forbidden — you are not a member of this project"
        assert "scheduler_wait" not in json.dumps(result)
        assert "blocking_job_ids" not in json.dumps(result)
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_get_job_and_list_jobs_report_scheduler_wait_for_file_overlap(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    running = store.create(
        idea="Touch store.py and other.py",
        repo_path=project.repo_path,
        chat_id=1,
        source=JobSource.MCP,
        source_meta={
            "scope": {"allowed_paths": ["hyqs/pipeline/store.py", "hyqs/pipeline/other.py"]}
        },
    )
    running.status = JobStatus.RUNNING
    store.save(running)
    pending = store.create(
        idea="Also touch store.py",
        repo_path=project.repo_path,
        chat_id=1,
        source=JobSource.MCP,
        source_meta={"scope": {"allowed_paths": ["hyqs/pipeline/store.py"]}},
    )
    _set_ctx(monkeypatch, ctx)
    try:
        for result in (
            _call(mcp, "get_job", job_id=pending.id),
            _call(mcp, "get_job", job_id=pending.id, summary=True),
        ):
            wait = result["scheduler_wait"]
            assert wait["reason"] == "file_overlap"
            assert wait["blocking_job_ids"] == [running.id]
            assert wait["conflicting_paths"] == ["hyqs/pipeline/store.py"]
            assert f"#{running.id}" in wait["summary"]

        for summary in (False, True):
            jobs = _call(mcp, "list_jobs", project_id=project.id, status="all", summary=summary)
            by_id = {j["id"]: j for j in jobs}
            wait = by_id[pending.id]["scheduler_wait"]
            assert wait["reason"] == "file_overlap"
            assert wait["blocking_job_ids"] == [running.id]
            assert wait["conflicting_paths"] == ["hyqs/pipeline/store.py"]
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id IN (%s, %s)", (running.id, pending.id))


def test_get_job_reports_scheduler_wait_for_schema_lock(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    other = store.create(
        idea="holds schema lock", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    job = store.create(
        idea="wants to merge", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    job.stage = Stage.MERGE
    store.save(job)
    now = time.time()
    store.try_acquire_schema_lock(project.id, lock_owner_id(other.id), now, 300)
    _set_ctx(monkeypatch, ctx)
    try:
        for wait in (
            _call(mcp, "get_job", job_id=job.id)["scheduler_wait"],
            _call(mcp, "get_job", job_id=job.id, summary=True)["scheduler_wait"],
            next(
                item
                for item in _call(mcp, "list_jobs", project_id=project.id, status="all")
                if item["id"] == job.id
            )["scheduler_wait"],
            next(
                item
                for item in _call(
                    mcp, "list_jobs", project_id=project.id, status="all", summary=True
                )
                if item["id"] == job.id
            )["scheduler_wait"],
        ):
            assert wait["reason"] == "schema_lock"
            assert wait["blocking_job_ids"] == [other.id]
    finally:
        store.release_schema_lock(project.id, lock_owner_id(other.id))
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id IN (%s, %s)", (job.id, other.id))


def test_get_job_reports_scheduler_wait_for_merge_lock(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    other = store.create(
        idea="holds merge lock", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    job = store.create(
        idea="wants to merge", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    job.stage = Stage.MERGE
    store.save(job)
    now = time.time()
    store.try_acquire_merge_lock(job.repo_path, lock_owner_id(other.id), now, 300)
    _set_ctx(monkeypatch, ctx)
    try:
        for wait in (
            _call(mcp, "get_job", job_id=job.id)["scheduler_wait"],
            _call(mcp, "get_job", job_id=job.id, summary=True)["scheduler_wait"],
            next(
                item
                for item in _call(mcp, "list_jobs", project_id=project.id, status="all")
                if item["id"] == job.id
            )["scheduler_wait"],
            next(
                item
                for item in _call(
                    mcp, "list_jobs", project_id=project.id, status="all", summary=True
                )
                if item["id"] == job.id
            )["scheduler_wait"],
        ):
            assert wait["reason"] == "merge_lock"
            assert wait["blocking_job_ids"] == [other.id]
    finally:
        store.release_merge_lock(job.repo_path, lock_owner_id(other.id))
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id IN (%s, %s)", (job.id, other.id))


def test_get_job_reports_scheduler_wait_for_provider_capacity(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    job = store.create(idea="plan me", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP)
    store.set_provider_pause("claude", 0.0)
    _set_ctx(monkeypatch, ctx)
    try:
        store.set_provider_pause("claude", time.time() + 3600)
        for wait in (
            _call(mcp, "get_job", job_id=job.id)["scheduler_wait"],
            _call(mcp, "get_job", job_id=job.id, summary=True)["scheduler_wait"],
            next(
                item
                for item in _call(mcp, "list_jobs", project_id=project.id, status="all")
                if item["id"] == job.id
            )["scheduler_wait"],
            next(
                item
                for item in _call(
                    mcp, "list_jobs", project_id=project.id, status="all", summary=True
                )
                if item["id"] == job.id
            )["scheduler_wait"],
        ):
            assert wait["reason"] == "provider_capacity"
            assert "claude" in wait["summary"]
    finally:
        store.set_provider_pause("claude", 0.0)
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_get_job_reports_scheduler_wait_for_project_slot_occupied(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    agent = store.list_agents(project.id)[0]
    occupant = store.create(
        idea="running now", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    occupant.status = JobStatus.RUNNING
    occupant.agent_id = agent.id
    occupant.provider = "claude"
    store.save(occupant)
    pending = store.create(
        idea="waiting for a slot", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    store.set_provider_pause("claude", 0.0)
    _set_ctx(monkeypatch, ctx)
    try:
        for wait in (
            _call(mcp, "get_job", job_id=pending.id)["scheduler_wait"],
            _call(mcp, "get_job", job_id=pending.id, summary=True)["scheduler_wait"],
            next(
                item
                for item in _call(mcp, "list_jobs", project_id=project.id, status="all")
                if item["id"] == pending.id
            )["scheduler_wait"],
            next(
                item
                for item in _call(
                    mcp, "list_jobs", project_id=project.id, status="all", summary=True
                )
                if item["id"] == pending.id
            )["scheduler_wait"],
        ):
            assert wait["reason"] == "project_slot_occupied"
            assert wait["blocking_job_ids"] == [occupant.id]

        occupant.status = JobStatus.DONE
        store.save(occupant)

        cleared = _call(mcp, "get_job", job_id=pending.id)
        assert cleared["scheduler_wait"] is None
    finally:
        store.set_provider_pause("claude", 0.0)
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id IN (%s, %s)", (pending.id, occupant.id))


def test_explicit_waiting_on_suppresses_scheduler_wait(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    blocker = store.create(
        idea="blocker", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    lock_holder = store.create(
        idea="holds schema lock", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    job = store.create(
        idea="blocked and lock-contended",
        repo_path=project.repo_path,
        chat_id=1,
        source=JobSource.MCP,
    )
    store.add_job_dependency(job.id, blocker.id)
    job.stage = Stage.MERGE
    store.save(job)
    now = time.time()
    store.try_acquire_schema_lock(project.id, lock_owner_id(lock_holder.id), now, 300)
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "get_job", job_id=job.id)
        assert result["waiting_on"] == [blocker.id]
        assert result["scheduler_wait"] is None

        listed = _call(mcp, "list_jobs", project_id=project.id, status="all")
        entry = next(j for j in listed if j["id"] == job.id)
        assert entry["waiting_on"] == [blocker.id]
        assert entry["scheduler_wait"] is None
    finally:
        store.release_schema_lock(project.id, lock_owner_id(lock_holder.id))
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_dependencies WHERE job_id = %s", (job.id,))
            conn.execute(
                "DELETE FROM jobs WHERE id IN (%s, %s, %s)",
                (job.id, blocker.id, lock_holder.id),
            )


def test_scheduler_wait_disappears_after_schema_lock_release(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    other = store.create(
        idea="holds schema lock", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    job = store.create(
        idea="wants to merge", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    job.stage = Stage.MERGE
    store.save(job)
    now = time.time()
    store.try_acquire_schema_lock(project.id, lock_owner_id(other.id), now, 300)
    _set_ctx(monkeypatch, ctx)
    try:
        held = _call(mcp, "get_job", job_id=job.id)
        assert held["scheduler_wait"] is not None
        assert held["scheduler_wait"]["reason"] == "schema_lock"

        store.release_schema_lock(project.id, lock_owner_id(other.id))

        released = _call(mcp, "get_job", job_id=job.id)
        assert released["scheduler_wait"] is None
    finally:
        store.release_schema_lock(project.id, lock_owner_id(other.id))
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id IN (%s, %s)", (job.id, other.id))


def test_scheduler_wait_blocking_ids_never_leak_across_projects(store, mcp, monkeypatch):
    project_a, ctx_a = _member_project(store)
    project_b = store.create_project("MCP Test Project B", f"/tmp/test-mcp-{uuid.uuid4()}")
    running_a = store.create(
        idea="Touch store.py in A",
        repo_path=project_a.repo_path,
        chat_id=1,
        source=JobSource.MCP,
        source_meta={"scope": {"allowed_paths": ["hyqs/pipeline/store.py"]}},
    )
    running_a.status = JobStatus.RUNNING
    store.save(running_a)
    pending_a = store.create(
        idea="Also touch store.py in A",
        repo_path=project_a.repo_path,
        chat_id=1,
        source=JobSource.MCP,
        source_meta={"scope": {"allowed_paths": ["hyqs/pipeline/store.py"]}},
    )
    running_b = store.create(
        idea="Touch store.py in B",
        repo_path=project_b.repo_path,
        chat_id=1,
        source=JobSource.MCP,
        source_meta={"scope": {"allowed_paths": ["hyqs/pipeline/store.py"]}},
    )
    running_b.status = JobStatus.RUNNING
    store.save(running_b)
    pending_b = store.create(
        idea="Also touch store.py in B",
        repo_path=project_b.repo_path,
        chat_id=1,
        source=JobSource.MCP,
        source_meta={"scope": {"allowed_paths": ["hyqs/pipeline/store.py"]}},
    )
    _set_ctx(monkeypatch, ctx_a)
    try:
        listed = _call(mcp, "list_jobs", project_id=project_a.id, status="all")
        pending_a_entry = next(j for j in listed if j["id"] == pending_a.id)
        list_wait = pending_a_entry["scheduler_wait"]
        assert list_wait is not None
        assert list_wait["blocking_job_ids"] == [running_a.id]
        assert running_b.id not in list_wait["blocking_job_ids"]
        assert list_wait["conflicting_paths"] == ["hyqs/pipeline/store.py"]

        fetched = _call(mcp, "get_job", job_id=pending_a.id)
        get_wait = fetched["scheduler_wait"]
        assert get_wait is not None
        assert get_wait["blocking_job_ids"] == [running_a.id]
        assert running_b.id not in get_wait["blocking_job_ids"]

        forbidden = _call(mcp, "get_job", job_id=pending_b.id)
        assert forbidden == "error: forbidden — you are not a member of this project"
    finally:
        with store._pool.connection() as conn:
            conn.execute(
                "DELETE FROM jobs WHERE id IN (%s, %s, %s, %s)",
                (running_a.id, pending_a.id, running_b.id, pending_b.id),
            )
        store.delete_project(project_b.id)


_SUMMARY_KEYS = {
    "id",
    "title",
    "status",
    "stage",
    "epic_id",
    "project_id",
    "waiting_on",
    "scheduler_wait",
    "branch",
    "archived",
    "priority",
    "effective_priority",
    "priority_reasons",
    "created_at",
    "updated_at",
    "has_error",
    "failure_code",
}
_BLOB_KEYS = {"idea", "plan", "review", "source_meta", "resolution"}


def test_get_job_summary_true_returns_reduced_fields(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    job = store.create(
        idea="a job with a non-trivial idea",
        repo_path=project.repo_path,
        chat_id=1,
        source=JobSource.MCP,
    )
    with store._pool.connection() as conn:
        conn.execute(
            "UPDATE jobs SET plan=%s WHERE id=%s",
            (json.dumps({"stories": ["do a thing"]}), job.id),
        )
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "get_job", job_id=job.id, summary=True)
        assert set(result.keys()) == _SUMMARY_KEYS
        assert not (_BLOB_KEYS & result.keys())
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_get_job_summary_false_matches_full_dict(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    job = store.create(
        idea="a job with a non-trivial idea",
        repo_path=project.repo_path,
        chat_id=1,
        source=JobSource.MCP,
    )
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "get_job", job_id=job.id)
        assert "idea" in result
        assert "plan" in result
        result_explicit = _call(mcp, "get_job", job_id=job.id, summary=False)
        assert result_explicit == result
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_list_jobs_summary_true_returns_reduced_fields(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    job = store.create(
        idea="a job with a non-trivial idea",
        repo_path=project.repo_path,
        chat_id=1,
        source=JobSource.MCP,
    )
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "list_jobs", project_id=project.id, status="all", summary=True)
        assert result
        for item in result:
            assert set(item.keys()) == _SUMMARY_KEYS
            assert not (_BLOB_KEYS & item.keys())
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_get_job_summary_has_error_true_omits_raw_text(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    job = store.create(
        idea="a failing job", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    failure_text = "boom: something went wrong in a very specific way"
    with store._pool.connection() as conn:
        conn.execute("UPDATE jobs SET failure=%s WHERE id=%s", (failure_text, job.id))
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "get_job", job_id=job.id, summary=True)
        assert result["has_error"] is True
        assert "failure" not in result
        assert "error" not in result
        assert failure_text not in json.dumps(result)
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


_FAILURE_FIELDS = (
    "failed_step",
    "failure_code",
    "failure_origin",
    "retry_disposition",
    "failure_detail",
)


def test_get_job_full_dict_surfaces_failure_classification_fields(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    job = store.create(
        idea="a failing job", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    detail = {"reason": "gate rejected the diff"}
    with store._pool.connection() as conn:
        conn.execute(
            "UPDATE jobs SET failed_step=%s, failure_code=%s, failure_origin=%s, "
            "retry_disposition=%s, failure_detail=%s WHERE id=%s",
            ("test", "test_failure", "gate", "retryable", json.dumps(detail), job.id),
        )
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "get_job", job_id=job.id)
        assert result["failed_step"] == "test"
        assert result["failure_code"] == "test_failure"
        assert result["failure_origin"] == "gate"
        assert result["retry_disposition"] == "retryable"
        assert result["failure_detail"] == detail
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_get_job_full_dict_failure_fields_default_to_none(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    job = store.create(
        idea="a fresh job", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "get_job", job_id=job.id)
        for field in _FAILURE_FIELDS:
            assert field in result
            assert result[field] is None
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_list_jobs_full_dict_surfaces_failure_classification_fields(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    job = store.create(
        idea="a failing job", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    detail = {"reason": "gate rejected the diff"}
    with store._pool.connection() as conn:
        conn.execute(
            "UPDATE jobs SET failed_step=%s, failure_code=%s, failure_origin=%s, "
            "retry_disposition=%s, failure_detail=%s WHERE id=%s",
            ("test", "test_failure", "gate", "retryable", json.dumps(detail), job.id),
        )
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "list_jobs", project_id=project.id, status="all")
        by_id = {j["id"]: j for j in result}
        entry = by_id[job.id]
        assert entry["failed_step"] == "test"
        assert entry["failure_code"] == "test_failure"
        assert entry["failure_origin"] == "gate"
        assert entry["retry_disposition"] == "retryable"
        assert entry["failure_detail"] == detail
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_list_jobs_full_dict_failure_fields_default_to_none(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    job = store.create(
        idea="a fresh job", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "list_jobs", project_id=project.id, status="all")
        by_id = {j["id"]: j for j in result}
        entry = by_id[job.id]
        for field in _FAILURE_FIELDS:
            assert field in entry
            assert entry[field] is None
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_get_job_summary_true_surfaces_failure_code(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    job = store.create(
        idea="a failing job", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    with store._pool.connection() as conn:
        conn.execute("UPDATE jobs SET failure_code=%s WHERE id=%s", ("test_failure", job.id))
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "get_job", job_id=job.id, summary=True)
        assert result["failure_code"] == "test_failure"
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_get_job_summary_true_failure_code_defaults_to_none(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    job = store.create(
        idea="a fresh job", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "get_job", job_id=job.id, summary=True)
        assert result["failure_code"] is None
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


# --- job_diff: falls back to the landed commit once the branch is gone ------


def test_job_diff_returns_diff_when_branch_resolves(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    job = store.create(idea="a job with a live branch", repo_path=project.repo_path, chat_id=1)
    with store._pool.connection() as conn:
        conn.execute("UPDATE jobs SET branch=%s WHERE id=%s", ("job-branch", job.id))
    _set_ctx(monkeypatch, ctx)
    branch_diff = GitResult(ok=True, stdout="diff --git a/x b/x\n", stderr="", code=0)
    try:
        with (
            patch(
                "hyqs.web.app.gitops.default_branch", new_callable=AsyncMock, return_value="main"
            ),
            patch("hyqs.web.app.gitops.git", new_callable=AsyncMock, return_value=branch_diff),
        ):
            result = _call(mcp, "job_diff", job_id=job.id)
        assert result == "diff --git a/x b/x\n"
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_job_diff_falls_back_to_deployed_commit_when_branch_gone(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    job = store.create(
        idea="a job whose branch got deleted at merge",
        repo_path=project.repo_path,
        chat_id=1,
    )
    with store._pool.connection() as conn:
        conn.execute(
            "UPDATE jobs SET branch=%s, deployed_commit=%s WHERE id=%s",
            ("job-branch", "abc123", job.id),
        )
    _set_ctx(monkeypatch, ctx)
    branch_diff_failed = GitResult(ok=False, stdout="", stderr="fatal: ambiguous argument", code=1)
    commit_diff = GitResult(ok=True, stdout="diff --git a/y b/y\n", stderr="", code=0)
    try:
        with (
            patch(
                "hyqs.web.app.gitops.default_branch", new_callable=AsyncMock, return_value="main"
            ),
            patch(
                "hyqs.web.app.gitops.git",
                new_callable=AsyncMock,
                side_effect=[branch_diff_failed, commit_diff],
            ),
        ):
            result = _call(mcp, "job_diff", job_id=job.id)
        assert result == "diff --git a/y b/y\n"
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_job_diff_returns_structured_error_when_branch_and_commit_both_unavailable(
    store, mcp, monkeypatch
):
    project, ctx = _member_project(store)
    job = store.create(
        idea="a job whose branch got deleted with no recorded commit",
        repo_path=project.repo_path,
        chat_id=1,
    )
    with store._pool.connection() as conn:
        conn.execute("UPDATE jobs SET branch=%s WHERE id=%s", ("job-branch", job.id))
    _set_ctx(monkeypatch, ctx)
    branch_diff_failed = GitResult(ok=False, stdout="", stderr="fatal: ambiguous argument", code=1)
    try:
        with (
            patch(
                "hyqs.web.app.gitops.default_branch", new_callable=AsyncMock, return_value="main"
            ),
            patch(
                "hyqs.web.app.gitops.git", new_callable=AsyncMock, return_value=branch_diff_failed
            ),
        ):
            result = _call(mcp, "job_diff", job_id=job.id)
        assert result.startswith("error: ")
        assert "fatal: ambiguous argument" not in result
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_get_job_full_dict_surfaces_commit_fields(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    job = store.create(
        idea="a landed job", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    with store._pool.connection() as conn:
        conn.execute(
            "UPDATE jobs SET merge_delta_sha=%s, deployed_commit=%s WHERE id=%s",
            ("deadbeef", "abc123", job.id),
        )
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "get_job", job_id=job.id)
        assert result["merge_delta_sha"] == "deadbeef"
        assert result["deployed_commit"] == "abc123"
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_get_job_full_dict_commit_fields_default_to_none(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    job = store.create(
        idea="a fresh job", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "get_job", job_id=job.id)
        assert result["merge_delta_sha"] is None
        assert result["deployed_commit"] is None
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_list_jobs_full_dict_surfaces_commit_fields(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    job = store.create(
        idea="a landed job", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    with store._pool.connection() as conn:
        conn.execute(
            "UPDATE jobs SET merge_delta_sha=%s, deployed_commit=%s WHERE id=%s",
            ("deadbeef", "abc123", job.id),
        )
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "list_jobs", project_id=project.id, status="all")
        by_id = {j["id"]: j for j in result}
        entry = by_id[job.id]
        assert entry["merge_delta_sha"] == "deadbeef"
        assert entry["deployed_commit"] == "abc123"
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_get_job_full_dict_surfaces_effective_priority(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    job = store.create(
        idea="a job with default priority",
        repo_path=project.repo_path,
        chat_id=1,
        source=JobSource.MCP,
    )
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "get_job", job_id=job.id)
        assert result["effective_priority"] == job.priority
        assert result["priority_reasons"] == []
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_get_job_summary_true_surfaces_effective_priority(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    job = store.create(
        idea="a job with default priority",
        repo_path=project.repo_path,
        chat_id=1,
        source=JobSource.MCP,
    )
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "get_job", job_id=job.id, summary=True)
        assert result["effective_priority"] == job.priority
        assert result["priority_reasons"] == []
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_list_jobs_full_dict_surfaces_effective_priority(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    job = store.create(
        idea="a job with default priority",
        repo_path=project.repo_path,
        chat_id=1,
        source=JobSource.MCP,
    )
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "list_jobs", project_id=project.id, status="all")
        by_id = {j["id"]: j for j in result}
        entry = by_id[job.id]
        assert entry["effective_priority"] == job.priority
        assert entry["priority_reasons"] == []
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_list_jobs_summary_true_surfaces_effective_priority(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    job = store.create(
        idea="a job with default priority",
        repo_path=project.repo_path,
        chat_id=1,
        source=JobSource.MCP,
    )
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "list_jobs", project_id=project.id, status="all", summary=True)
        by_id = {j["id"]: j for j in result}
        entry = by_id[job.id]
        assert entry["effective_priority"] == job.priority
        assert entry["priority_reasons"] == []
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_get_job_and_list_jobs_projections_surface_remediation_priority_boost(
    store, mcp, monkeypatch
):
    project, ctx = _member_project(store)
    parent = store.create(
        idea="a job needing remediation",
        repo_path=project.repo_path,
        chat_id=1,
        source=JobSource.MCP,
    )
    child = store.create(
        idea="a pending fix-forward job",
        repo_path=project.repo_path,
        chat_id=1,
        priority=7,
        source=JobSource.MCP,
        source_meta={"fix_for": parent.id},
    )
    assert child.status == JobStatus.PENDING
    _set_ctx(monkeypatch, ctx)
    try:
        projections = [
            _call(mcp, "get_job", job_id=child.id, summary=False),
            _call(mcp, "get_job", job_id=child.id, summary=True),
        ]
        for summary in (False, True):
            jobs = _call(
                mcp,
                "list_jobs",
                project_id=project.id,
                status="all",
                summary=summary,
            )
            projections.append(next(job for job in jobs if job["id"] == child.id))

        for projection in projections:
            assert projection["effective_priority"] > child.priority
            assert any(
                reason["reason"] == "remediation" for reason in projection["priority_reasons"]
            )
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id IN (%s, %s)", (parent.id, child.id))


def test_list_jobs_summary_true_surfaces_failure_code(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    job = store.create(
        idea="a failing job", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    with store._pool.connection() as conn:
        conn.execute("UPDATE jobs SET failure_code=%s WHERE id=%s", ("test_failure", job.id))
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "list_jobs", project_id=project.id, status="all", summary=True)
        by_id = {j["id"]: j for j in result}
        assert by_id[job.id]["failure_code"] == "test_failure"
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


# --- list_jobs: expanded status vocabulary + cursor pagination ---------------


def test_list_jobs_status_filters_preserve_complete_vocabulary_semantics(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    jobs = {
        status: store.create(
            idea=f"a {status} job",
            repo_path=project.repo_path,
            chat_id=1,
            source=JobSource.MCP,
        )
        for status in ("pending", "running", "deploying", "done", "failed", "cancelled")
    }
    archived = store.create(
        idea="an archived running job",
        repo_path=project.repo_path,
        chat_id=1,
        source=JobSource.MCP,
    )
    with store._pool.connection() as conn:
        for status, job in jobs.items():
            conn.execute("UPDATE jobs SET status=%s WHERE id=%s", (status, job.id))
        conn.execute("UPDATE jobs SET status='running', archived=TRUE WHERE id=%s", (archived.id,))
    _set_ctx(monkeypatch, ctx)
    try:
        expected = {
            "pending": {jobs["pending"].id},
            "running": {jobs["running"].id},
            "deploying": {jobs["deploying"].id},
            "done": {jobs["done"].id},
            "failed": {jobs["failed"].id},
            "cancelled": {jobs["cancelled"].id},
            "active": {jobs[status].id for status in ("pending", "running", "deploying")},
            "archived": {archived.id},
            "all": {job.id for job in jobs.values()} | {archived.id},
        }
        for status, expected_ids in expected.items():
            result = _call(mcp, "list_jobs", project_id=project.id, status=status)
            assert {job["id"] for job in result} == expected_ids
    finally:
        with store._pool.connection() as conn:
            conn.execute(
                "DELETE FROM jobs WHERE id = ANY(%s)",
                ([job.id for job in jobs.values()] + [archived.id],),
            )


def test_list_jobs_invalid_status_names_all_nine_values(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "list_jobs", project_id=project.id, status="bogus")
    assert result == (
        "error: status must be one of active, all, archived, cancelled, deploying, "
        "done, failed, pending, running"
    )


def test_list_jobs_cursor_walk_is_complete_ordered_and_project_scoped(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    other_project = store.create_project("Other MCP Project", f"/tmp/test-mcp-{uuid.uuid4()}")
    jobs = [
        store.create(
            idea=f"page job {i}", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
        )
        for i in range(123)
    ]
    distractors = [
        store.create(
            idea=f"other project job {i}",
            repo_path=other_project.repo_path,
            chat_id=1,
            source=JobSource.MCP,
        )
        for i in range(4)
    ]
    statuses = ("pending", "running", "deploying", "done", "failed", "cancelled")
    with store._pool.connection() as conn:
        for index, job in enumerate(jobs):
            conn.execute("UPDATE jobs SET status=%s WHERE id=%s", (statuses[index % 6], job.id))
    _set_ctx(monkeypatch, ctx)
    try:
        pages = []
        cursor = None
        while True:
            kwargs = {"project_id": project.id, "status": "all"}
            if cursor is not None:
                kwargs["cursor"] = cursor
            page = _call(mcp, "list_jobs", **kwargs)
            assert len(page) <= 50
            if not page:
                break
            pages.append(page)
            cursor = page[-1]["id"]

        result_ids = [job["id"] for page in pages for job in page]
        expected_ids = sorted((job.id for job in jobs), reverse=True)
        assert [len(page) for page in pages] == [50, 50, 23]
        assert result_ids == expected_ids
        assert len(result_ids) == len(set(result_ids))
        assert not ({job.id for job in distractors} & set(result_ids))
        assert all(job["project_id"] == project.id for page in pages for job in page)
    finally:
        with store._pool.connection() as conn:
            conn.execute(
                "DELETE FROM jobs WHERE id = ANY(%s)",
                ([job.id for job in jobs + distractors],),
            )


@pytest.mark.parametrize("cursor", [0, -1])
def test_list_jobs_non_positive_cursor_returns_error_string(store, mcp, monkeypatch, cursor):
    project, ctx = _member_project(store)
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "list_jobs", project_id=project.id, status="all", cursor=cursor)
    assert result == "error: cursor must be a positive integer"


def test_list_jobs_omitting_cursor_returns_bare_list_unchanged(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    jobs = [
        store.create(
            idea=f"legacy response job {index}",
            repo_path=project.repo_path,
            chat_id=1,
            source=JobSource.MCP,
        )
        for index in range(55)
    ]
    statuses = ("pending", "running", "deploying", "done", "failed", "cancelled")
    with store._pool.connection() as conn:
        for index, job in enumerate(jobs):
            conn.execute("UPDATE jobs SET status=%s WHERE id=%s", (statuses[index % 6], job.id))
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "list_jobs", project_id=project.id, status="all")
        expected_jobs = list(reversed(jobs))[:50]
        expected = [
            job_to_dict(store.get(job.id), **job_projection_extras(store, store.get(job.id)))
            for job in expected_jobs
        ]
        assert result == expected
        assert isinstance(result, list)
        assert len(result) == 50
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = ANY(%s)", ([job.id for job in jobs],))


# --- create_job idempotency key -------------------------------------------------


def test_create_job_idempotency_key_dedups_second_call(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="contributor")
    _set_ctx(monkeypatch, ctx)
    try:
        key = str(uuid.uuid4())
        first = _call(
            mcp, "create_job", idea="ship the thing", project_id=project.id, idempotency_key=key
        )
        second = _call(
            mcp,
            "create_job",
            idea="ship a different thing",
            project_id=project.id,
            idempotency_key=key,
        )

        assert first["job"]["id"] == second["job"]["id"]
        assert second.get("deduplicated") is True
        with store._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE project_id = %s AND idempotency_key = %s",
                (project.id, key),
            ).fetchall()
        assert len(rows) == 1
    finally:
        store.delete_project(project.id)


def test_create_job_different_idempotency_keys_creates_distinct_jobs(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="contributor")
    _set_ctx(monkeypatch, ctx)
    try:
        first = _call(
            mcp,
            "create_job",
            idea="job one",
            project_id=project.id,
            idempotency_key=str(uuid.uuid4()),
        )
        second = _call(
            mcp,
            "create_job",
            idea="job two",
            project_id=project.id,
            idempotency_key=str(uuid.uuid4()),
        )

        assert first["job"]["id"] != second["job"]["id"]
        assert not first.get("deduplicated")
        assert not second.get("deduplicated")
    finally:
        store.delete_project(project.id)


def test_create_job_omitted_idempotency_key_preserves_content_based_dedup(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="contributor")
    _set_ctx(monkeypatch, ctx)
    try:
        idea = f"repeat me {uuid.uuid4()}"
        first = _call(mcp, "create_job", idea=idea, project_id=project.id)
        second = _call(mcp, "create_job", idea=idea, project_id=project.id)

        assert first["job"]["id"] == second["job"]["id"]
        assert second.get("deduplicated") is True
        with store._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE project_id = %s AND idea = %s", (project.id, idea)
            ).fetchall()
        assert len(rows) == 1
    finally:
        store.delete_project(project.id)


def test_create_job_wave_idempotency_key_reuses_existing_job(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="contributor")
    _set_ctx(monkeypatch, ctx)
    try:
        key = str(uuid.uuid4())
        existing = _call(
            mcp,
            "create_job",
            idea="pre-existing wave job",
            project_id=project.id,
            idempotency_key=key,
        )
        existing_id = existing["job"]["id"]

        result = _call(
            mcp,
            "create_job_wave",
            project_id=project.id,
            jobs=[
                {"title": "Wave A", "idea": "Do A"},
                {"title": "Dedup slot", "idea": "Do dedup", "idempotency_key": key},
            ],
        )

        jobs = result["jobs"]
        assert len(jobs) == 2
        assert jobs[1]["id"] == existing_id
        assert jobs[0]["id"] != existing_id
        with store._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE project_id = %s AND idempotency_key = %s",
                (project.id, key),
            ).fetchall()
        assert len(rows) == 1
    finally:
        store.delete_project(project.id)


# --- create_job_wave -----------------------------------------------------------


def test_create_job_wave_linear_chain_resolves_real_ids(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="contributor")
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(
            mcp,
            "create_job_wave",
            project_id=project.id,
            jobs=[
                {"title": "Wave A", "idea": "Do A"},
                {"title": "Wave B", "idea": "Do B", "depends_on": [0]},
            ],
        )
        assert isinstance(result, dict)
        jobs = result["jobs"]
        assert len(jobs) == 2
        a_id, b_id = jobs[0]["id"], jobs[1]["id"]
        assert jobs[1]["depends_on"] == [a_id]
        assert store.get_dependencies(b_id) == [a_id]
    finally:
        store.delete_project(project.id)


def test_create_job_wave_hot_file_auto_chains(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="contributor")
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(
            mcp,
            "create_job_wave",
            project_id=project.id,
            jobs=[
                {"title": "Wave A", "idea": "Do A", "target_files": ["shared.py"]},
                {"title": "Wave B", "idea": "Do B", "target_files": ["shared.py"]},
            ],
        )
        jobs = result["jobs"]
        a_id, b_id = jobs[0]["id"], jobs[1]["id"]
        assert jobs[1]["depends_on"] == [a_id]
        assert store.get_dependencies(b_id) == [a_id]
    finally:
        store.delete_project(project.id)


def test_create_job_wave_non_member_forbidden(store, mcp, monkeypatch):
    project, _ = _member_project(store)
    _set_ctx(monkeypatch, _non_member_ctx())
    try:
        result = _call(
            mcp,
            "create_job_wave",
            project_id=project.id,
            jobs=[{"title": "Wave A", "idea": "Do A"}],
        )
        assert result.startswith("error: forbidden")
        with store._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE project_id = %s", (project.id,)
            ).fetchall()
        assert rows == []
    finally:
        store.delete_project(project.id)


def test_create_job_wave_cyclic_rejected(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="contributor")
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(
            mcp,
            "create_job_wave",
            project_id=project.id,
            jobs=[
                {"title": "Wave A", "idea": "Do A", "depends_on": [1]},
                {"title": "Wave B", "idea": "Do B", "depends_on": [0]},
            ],
        )
        assert result.startswith("error:")
        with store._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE project_id = %s", (project.id,)
            ).fetchall()
        assert rows == []
    finally:
        store.delete_project(project.id)


def test_create_job_wave_out_of_range_depends_on_rejected(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="contributor")
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(
            mcp,
            "create_job_wave",
            project_id=project.id,
            jobs=[
                {"title": "Wave A", "idea": "Do A"},
                {"title": "Wave B", "idea": "Do B", "depends_on": [5]},
            ],
        )
        assert result.startswith("error:")
        with store._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE project_id = %s", (project.id,)
            ).fetchall()
        assert rows == []
    finally:
        store.delete_project(project.id)


def test_create_job_wave_epic_id_assigns_all_jobs(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="contributor")
    epic = store.create_epic(project.id, "Wave Epic")
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(
            mcp,
            "create_job_wave",
            project_id=project.id,
            jobs=[
                {"title": "Wave A", "idea": "Do A"},
                {"title": "Wave B", "idea": "Do B"},
            ],
            epic_id=epic.id,
        )
        for entry in result["jobs"]:
            job = store.get(entry["id"])
            assert job.epic_id == epic.id
    finally:
        store.delete_project(project.id)


def test_create_job_wave_omitted_epic_id_auto_resolves_per_job(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="contributor")
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(
            mcp,
            "create_job_wave",
            project_id=project.id,
            jobs=[{"title": "Wave A", "idea": "Do A"}],
        )
        job = store.get(result["jobs"][0]["id"])
        assert job.epic_id is not None
        epic = store.get_epic(job.epic_id)
        assert epic.name == "General"
    finally:
        store.delete_project(project.id)


# --- add_job_dependency / remove_job_dependency --------------------------------


def test_add_job_dependency_member_success(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="contributor")
    blocker = store.create(
        idea="blocker", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    job = store.create(idea="blocked", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP)
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "add_job_dependency", job_id=job.id, depends_on_job_id=blocker.id)
        assert result == {"depends_on": [blocker.id]}
        assert store.get_dependencies(job.id) == [blocker.id]
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_dependencies WHERE job_id = %s", (job.id,))
            conn.execute("DELETE FROM jobs WHERE id IN (%s, %s)", (job.id, blocker.id))


def test_add_job_dependency_viewer_forbidden_missing_permission(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="viewer")
    blocker = store.create(
        idea="blocker", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    job = store.create(idea="blocked", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP)
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "add_job_dependency", job_id=job.id, depends_on_job_id=blocker.id)
        assert result.startswith("error: forbidden")
        assert store.get_dependencies(job.id) == []
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_dependencies WHERE job_id = %s", (job.id,))
            conn.execute("DELETE FROM jobs WHERE id IN (%s, %s)", (job.id, blocker.id))


def test_add_job_dependency_non_member_forbidden(store, mcp, monkeypatch):
    project, _ = _member_project(store)
    blocker = store.create(
        idea="blocker", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    job = store.create(idea="blocked", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP)
    _set_ctx(monkeypatch, _non_member_ctx())
    try:
        result = _call(mcp, "add_job_dependency", job_id=job.id, depends_on_job_id=blocker.id)
        assert result.startswith("error: forbidden")
        assert store.get_dependencies(job.id) == []
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_dependencies WHERE job_id = %s", (job.id,))
            conn.execute("DELETE FROM jobs WHERE id IN (%s, %s)", (job.id, blocker.id))


def test_add_job_dependency_unknown_job_returns_error(store, mcp, monkeypatch):
    _, ctx = _member_project(store, role="contributor")
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "add_job_dependency", job_id=999999999, depends_on_job_id=1)
    assert result == "error: job not found"


def test_remove_job_dependency_member_success(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="contributor")
    blocker = store.create(
        idea="blocker", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    job = store.create(idea="blocked", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP)
    store.add_job_dependency(job.id, blocker.id)
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "remove_job_dependency", job_id=job.id, depends_on_job_id=blocker.id)
        assert result == {"depends_on": []}
        assert store.get_dependencies(job.id) == []
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_dependencies WHERE job_id = %s", (job.id,))
            conn.execute("DELETE FROM jobs WHERE id IN (%s, %s)", (job.id, blocker.id))


def test_remove_job_dependency_viewer_forbidden_missing_permission(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="viewer")
    blocker = store.create(
        idea="blocker", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    job = store.create(idea="blocked", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP)
    store.add_job_dependency(job.id, blocker.id)
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "remove_job_dependency", job_id=job.id, depends_on_job_id=blocker.id)
        assert result.startswith("error: forbidden")
        assert store.get_dependencies(job.id) == [blocker.id]
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_dependencies WHERE job_id = %s", (job.id,))
            conn.execute("DELETE FROM jobs WHERE id IN (%s, %s)", (job.id, blocker.id))


def test_remove_job_dependency_non_member_forbidden(store, mcp, monkeypatch):
    project, _ = _member_project(store)
    blocker = store.create(
        idea="blocker", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    job = store.create(idea="blocked", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP)
    store.add_job_dependency(job.id, blocker.id)
    _set_ctx(monkeypatch, _non_member_ctx())
    try:
        result = _call(mcp, "remove_job_dependency", job_id=job.id, depends_on_job_id=blocker.id)
        assert result.startswith("error: forbidden")
        assert store.get_dependencies(job.id) == [blocker.id]
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_dependencies WHERE job_id = %s", (job.id,))
            conn.execute("DELETE FROM jobs WHERE id IN (%s, %s)", (job.id, blocker.id))


def test_remove_job_dependency_unknown_job_returns_error(store, mcp, monkeypatch):
    _, ctx = _member_project(store, role="contributor")
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "remove_job_dependency", job_id=999999999, depends_on_job_id=1)
    assert result == "error: job not found"


# --- get_job_dependency_graph -------------------------------------------------


def test_get_job_dependency_graph_linear_chain(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="viewer")
    blocker = store.create(
        idea="blocker", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    job = store.create(idea="blocked", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP)
    store.add_job_dependency(job.id, blocker.id)
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "get_job_dependency_graph", job_id=job.id)
        assert result == {
            "depends_on_jobs": [
                {"id": blocker.id, "title": blocker.title, "status": blocker.status.value}
            ],
            "dependents": [],
        }
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_dependencies WHERE job_id = %s", (job.id,))
            conn.execute("DELETE FROM jobs WHERE id IN (%s, %s)", (job.id, blocker.id))


def test_get_job_dependency_graph_diamond_multi_parent(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="viewer")
    upstream_a = store.create(
        idea="upstream a", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    upstream_b = store.create(
        idea="upstream b", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    job = store.create(idea="middle", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP)
    downstream_a = store.create(
        idea="downstream a", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    downstream_b = store.create(
        idea="downstream b", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    store.add_job_dependency(job.id, upstream_a.id)
    store.add_job_dependency(job.id, upstream_b.id)
    store.add_job_dependency(downstream_a.id, job.id)
    store.add_job_dependency(downstream_b.id, job.id)
    _set_ctx(monkeypatch, ctx)
    job_ids = (upstream_a.id, upstream_b.id, job.id, downstream_a.id, downstream_b.id)
    try:
        result = _call(mcp, "get_job_dependency_graph", job_id=job.id)
        assert {d["id"] for d in result["depends_on_jobs"]} == {upstream_a.id, upstream_b.id}
        assert {d["id"] for d in result["dependents"]} == {downstream_a.id, downstream_b.id}
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_dependencies WHERE job_id = ANY(%s)", (list(job_ids),))
            conn.execute("DELETE FROM jobs WHERE id = ANY(%s)", (list(job_ids),))


def test_get_job_dependency_graph_non_member_forbidden(store, mcp, monkeypatch):
    project, _ = _member_project(store)
    job = store.create(
        idea="graph forbidden", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_ctx(monkeypatch, _non_member_ctx())
    try:
        result = _call(mcp, "get_job_dependency_graph", job_id=job.id)
        assert result == "error: forbidden — you are not a member of this project"
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_get_job_dependency_graph_unknown_job_returns_error(store, mcp, monkeypatch):
    _, ctx = _member_project(store, role="viewer")
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "get_job_dependency_graph", job_id=999999999)
    assert result == "error: job not found"


# --- survey_job_queue --------------------------------------------------------


def test_survey_job_queue_non_member_forbidden(store, mcp, monkeypatch):
    project, _ = _member_project(store)
    _set_ctx(monkeypatch, _non_member_ctx())
    try:
        result = _call(
            mcp,
            "survey_job_queue",
            project_id=project.id,
            candidates=[{"key": "c1", "title": "t", "target_files": ["a.py"]}],
        )
        assert result.startswith("error: forbidden")
    finally:
        store.delete_project(project.id)


# --- performance statistics --------------------------------------------------


@pytest.mark.parametrize(
    ("include_operational", "excludes_operational"),
    [(False, True), (True, False)],
)
def test_project_performance_forwards_operational_filter(
    store, mcp, monkeypatch, include_operational, excludes_operational
):
    project, ctx = _member_project(store)
    _set_ctx(monkeypatch, ctx)
    headline = {"completed_jobs": 4}
    stage_stats = [{"stage": "build", "runs": 2}]

    with (
        patch.object(store, "performance_headline_stats", return_value=headline) as headline_mock,
        patch.object(store, "performance_stage_stats", return_value=stage_stats) as stages_mock,
    ):
        kwargs = {"project_id": project.id}
        if include_operational:
            kwargs["include_operational"] = True
        result = _call(mcp, "project_performance", **kwargs)

    assert result == {
        "headline": headline,
        "stage_stats": stage_stats,
        "excludes_operational": excludes_operational,
    }
    headline_mock.assert_called_once_with(project.id, include_operational=include_operational)
    stages_mock.assert_called_once_with(project.id, include_operational=include_operational)


def test_project_performance_excludes_operational_jobs_end_to_end(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    _set_ctx(monkeypatch, ctx)
    try:
        ordinary = store.create(
            idea="ordinary job",
            repo_path=project.repo_path,
            chat_id=1,
            source=JobSource.CLI,
        )
        current_operational = store.create(
            idea="current operational job",
            repo_path=project.repo_path,
            chat_id=1,
            source_actor="auto-deploy",
        )
        legacy_operational = store.create(
            idea="legacy operational job",
            repo_path=project.repo_path,
            chat_id=1,
            source=JobSource.SUPERVISOR,
            title="Auto-deploy: legacy",
        )
        for job, tokens, cost, ended_at in (
            (ordinary, 100, 1.0, "2026-01-01T00:01:00+00:00"),
            (current_operational, 200, 2.0, "2026-01-01T00:02:00+00:00"),
            (legacy_operational, 300, 3.0, "2026-01-01T00:03:00+00:00"),
        ):
            store.add_event(
                job.id,
                "build",
                "done",
                tokens=tokens,
                cost_usd=cost,
                started_at="2026-01-01T00:00:00+00:00",
                ended_at=ended_at,
            )

        excluded = _call(mcp, "project_performance", project_id=project.id)
        assert excluded == {
            "headline": store.performance_headline_stats(project.id, include_operational=False),
            "stage_stats": store.performance_stage_stats(project.id, include_operational=False),
            "excludes_operational": True,
        }
        assert excluded["headline"]["total_jobs"] == 1
        assert excluded["headline"]["total_tokens"] == 100
        assert excluded["stage_stats"][0]["run_count"] == 1

        included = _call(
            mcp, "project_performance", project_id=project.id, include_operational=True
        )
        assert included == {
            "headline": store.performance_headline_stats(project.id, include_operational=True),
            "stage_stats": store.performance_stage_stats(project.id, include_operational=True),
            "excludes_operational": False,
        }
        assert included["headline"]["total_jobs"] == 3
        assert included["headline"]["total_tokens"] == 600
        assert included["stage_stats"][0]["run_count"] == 3
    finally:
        store.delete_project(project.id)


def test_project_performance_non_member_forbidden_before_stats(store, mcp, monkeypatch):
    project, _ = _member_project(store)
    _set_ctx(monkeypatch, _non_member_ctx())

    with (
        patch.object(store, "performance_headline_stats") as headline_mock,
        patch.object(store, "performance_stage_stats") as stages_mock,
    ):
        result = _call(mcp, "project_performance", project_id=project.id)

    assert result == "error: forbidden — you are not a member of this project"
    headline_mock.assert_not_called()
    stages_mock.assert_not_called()


@pytest.mark.parametrize(
    ("include_operational", "excludes_operational"),
    [(False, True), (True, False)],
)
def test_agent_stats_returns_envelope_and_forwards_operational_filter(
    store, mcp, monkeypatch, include_operational, excludes_operational
):
    project, ctx = _member_project(store)
    _set_ctx(monkeypatch, ctx)
    stats = [{"agent_id": 7, "total_cost": 1.25}]

    with patch.object(store, "agent_stats", return_value=stats) as stats_mock:
        kwargs = {"project_id": project.id}
        if include_operational:
            kwargs["include_operational"] = True
        result = _call(mcp, "agent_stats", **kwargs)

    assert result == {
        "stats": stats,
        "excludes_operational": excludes_operational,
    }
    stats_mock.assert_called_once_with(project.id, include_operational=include_operational)


def test_agent_stats_excludes_operational_jobs_end_to_end(store, mcp, monkeypatch):
    project, ctx = _member_project(store)
    _set_ctx(monkeypatch, ctx)
    try:
        agent = store.create_agent(project.id, "MCP Stats Agent", provider="claude", model="sonnet")
        ordinary = store.create(
            idea="ordinary job",
            repo_path=project.repo_path,
            chat_id=1,
            source=JobSource.CLI,
        )
        current_operational = store.create(
            idea="current operational job",
            repo_path=project.repo_path,
            chat_id=1,
            source_actor="auto-deploy",
        )
        legacy_operational = store.create(
            idea="legacy operational job",
            repo_path=project.repo_path,
            chat_id=1,
            source=JobSource.SUPERVISOR,
            title="Auto-deploy: legacy",
        )
        for job, cost, ended_at in (
            (ordinary, 1.0, "2026-01-01T00:01:00+00:00"),
            (current_operational, 2.0, "2026-01-01T00:02:00+00:00"),
            (legacy_operational, 3.0, "2026-01-01T00:03:00+00:00"),
        ):
            store.add_event(
                job.id,
                "build",
                "done",
                cost_usd=cost,
                tokens=int(cost * 100),
                started_at="2026-01-01T00:00:00+00:00",
                ended_at=ended_at,
                agent_id=agent.id,
            )

        excluded = _call(mcp, "agent_stats", project_id=project.id)
        assert excluded == {
            "stats": store.agent_stats(project.id, include_operational=False),
            "excludes_operational": True,
        }
        excluded_by_agent = {row["agent_id"]: row for row in excluded["stats"]}
        assert excluded_by_agent[agent.id]["total_cost_usd"] == 1.0
        assert excluded_by_agent[agent.id]["avg_duration_s"] == 60.0

        included = _call(mcp, "agent_stats", project_id=project.id, include_operational=True)
        assert included == {
            "stats": store.agent_stats(project.id, include_operational=True),
            "excludes_operational": False,
        }
        included_by_agent = {row["agent_id"]: row for row in included["stats"]}
        assert included_by_agent[agent.id]["total_cost_usd"] == 6.0
        assert included_by_agent[agent.id]["avg_duration_s"] == 120.0
    finally:
        store.delete_project(project.id)


def test_agent_stats_non_member_forbidden_before_stats(store, mcp, monkeypatch):
    project, _ = _member_project(store)
    _set_ctx(monkeypatch, _non_member_ctx())

    with patch.object(store, "agent_stats") as stats_mock:
        result = _call(mcp, "agent_stats", project_id=project.id)

    assert result == "error: forbidden — you are not a member of this project"
    stats_mock.assert_not_called()


def test_survey_job_queue_reports_colliding_and_non_colliding_candidates(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="contributor")
    _set_ctx(monkeypatch, ctx)
    try:
        active_job = store.create(
            idea="Fix a bug. Target files: hyqs/pipeline/foo.py",
            repo_path=project.repo_path,
            chat_id=1,
        )

        result = _call(
            mcp,
            "survey_job_queue",
            project_id=project.id,
            candidates=[
                {
                    "key": "colliding",
                    "title": "Touch foo.py",
                    "target_files": ["hyqs/pipeline/foo.py"],
                },
                {"key": "clean", "title": "Touch bar.py", "target_files": ["hyqs/pipeline/bar.py"]},
            ],
        )

        assert result["results"]["colliding"]["overlapping_job_ids"] == [active_job.id]
        assert result["results"]["colliding"]["depends_on"] == [active_job.id]
        assert result["results"]["clean"]["overlapping_job_ids"] == []
        assert result["results"]["clean"]["depends_on"] == []
        assert result["unknown_target_file_jobs"] == []
    finally:
        store.delete_project(project.id)


def test_survey_job_queue_surfaces_unknown_scope_jobs(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="contributor")
    _set_ctx(monkeypatch, ctx)
    try:
        vague_job = store.create(idea="A vague idea", repo_path=project.repo_path, chat_id=1)

        result = _call(
            mcp,
            "survey_job_queue",
            project_id=project.id,
            candidates=[{"key": "c1", "title": "t", "target_files": ["hyqs/pipeline/foo.py"]}],
        )

        assert result["unknown_target_file_jobs"] == [vague_job.id]
    finally:
        store.delete_project(project.id)


def test_survey_job_queue_empty_candidates_returns_error(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="contributor")
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "survey_job_queue", project_id=project.id, candidates=[])
        assert result.startswith("error:")
    finally:
        store.delete_project(project.id)


def test_survey_job_queue_malformed_candidates_returns_error(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="contributor")
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(
            mcp,
            "survey_job_queue",
            project_id=project.id,
            candidates=[{"title": "no key here", "target_files": ["a.py"]}],
        )
        assert result.startswith("error:")
    finally:
        store.delete_project(project.id)


def test_build_mcp_auth_provider_sets_long_client_token_expiry(store, monkeypatch):
    fake_google_provider = MagicMock()
    monkeypatch.setattr("fastmcp.server.auth.providers.google.GoogleProvider", fake_google_provider)
    config = Config(
        model="sonnet",
        permission_mode="acceptEdits",
        google_client_id="client-id",
        google_client_secret="client-secret",
        mcp_resource_url="https://hyqs.example.com/mcp",
    )

    mcp_oauth.build_mcp_auth_provider(config, store)

    assert fake_google_provider.call_count == 1
    kwargs = fake_google_provider.call_args.kwargs
    assert kwargs["fastmcp_access_token_expiry_seconds"] == 60 * 60 * 24 * 30


def test_build_mcp_auth_provider_allows_api_token_through_shared_scope_gate(store, monkeypatch):
    fake_google_provider = MagicMock()
    fake_google_provider.return_value.base_url = "https://hyqs.example.com/mcp"
    fake_google_provider.return_value.resource_base_url = "https://hyqs.example.com"
    fake_google_provider.return_value.required_scopes = ["openid", "email"]
    monkeypatch.setattr("fastmcp.server.auth.providers.google.GoogleProvider", fake_google_provider)
    config = Config(
        model="sonnet",
        permission_mode="acceptEdits",
        google_client_id="client-id",
        google_client_secret="client-secret",
        mcp_resource_url="https://hyqs.example.com/mcp",
    )
    provider = mcp_oauth.build_mcp_auth_provider(config, store)
    access_token = FastMCPAccessToken(
        token="hpat_test",
        client_id="api_token:1",
        scopes=[],
        claims={"hyqs_auth_kind": mcp_oauth.API_TOKEN_CLAIM_KIND},
    )
    events = []

    async def app(scope, receive, send):
        events.append("forwarded")

    async def receive():
        return {"type": "http.request"}

    async def send(message):
        events.append(message)

    scope = {
        "type": "http",
        "user": AuthenticatedUser(access_token),
        "auth": AuthenticatedUser(access_token),
    }
    middleware = RequireAuthMiddleware(app, required_scopes=provider.required_scopes)

    asyncio.run(middleware(scope, receive, send))

    assert provider.required_scopes == []
    assert events == ["forwarded"]


# --- requeue_job ---------------------------------------------------------------


def _set_status_and_stage(store, job_id: int, status: JobStatus, stage: str) -> None:
    with store._pool.connection() as conn:
        conn.execute(
            "UPDATE jobs SET status=%s, stage=%s WHERE id=%s", (status.value, stage, job_id)
        )


def test_requeue_job_forbidden_for_non_member(store, mcp, monkeypatch):
    project, _ = _member_project(store)
    job = store.create(
        idea="not yours", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_status_and_stage(store, job.id, JobStatus.FAILED, "build")
    _set_ctx(monkeypatch, _non_member_ctx())
    try:
        result = _call(mcp, "requeue_job", job_id=job.id, stage="build")
        assert result.startswith("error: forbidden")
        unchanged = store.get(job.id)
        assert unchanged.status == JobStatus.FAILED
        assert unchanged.stage.value == "build"
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_requeue_job_unknown_job_returns_error(store, mcp, monkeypatch):
    _, ctx = _member_project(store)
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "requeue_job", job_id=999999999, stage="build")
    assert result == "error: job not found"


def test_requeue_job_rejects_non_failed_job(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    job = store.create(
        idea="still running", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_status_and_stage(store, job.id, JobStatus.RUNNING, "build")
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "requeue_job", job_id=job.id, stage="build")
        assert result == "error: job is not in a failed state"
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_requeue_job_rejects_stage_outside_whitelist(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    job = store.create(
        idea="failed job", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_status_and_stage(store, job.id, JobStatus.FAILED, "security")
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "requeue_job", job_id=job.id, stage="merge")
        assert result == "error: stage 'merge' is not requeueable"
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_requeue_job_succeeds_and_reports_waiting_on(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    blocker = store.create(
        idea="blocker", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    job = store.create(
        idea="failed job with a dependency",
        repo_path=project.repo_path,
        chat_id=1,
        source=JobSource.MCP,
    )
    store.add_job_dependency(job.id, blocker.id)
    _set_status_and_stage(store, job.id, JobStatus.FAILED, "security")
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "requeue_job", job_id=job.id, stage="build")
        assert result["job"]["id"] == job.id
        assert result["job"]["status"] == "pending"
        assert result["job"]["stage"] == "build"
        assert result["job"]["waiting_on"] == [blocker.id]
        updated = store.get(job.id)
        assert updated.status == JobStatus.PENDING
        assert updated.stage.value == "build"
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_dependencies WHERE job_id = %s", (job.id,))
            conn.execute("DELETE FROM jobs WHERE id IN (%s, %s)", (job.id, blocker.id))


def test_requeue_job_rest_and_mcp_agree_on_eligibility(store, mcp, monkeypatch):
    """Guard against future drift: the shared helper decides both entry points."""
    from hyqs.pipeline.models import Job
    from hyqs.web.app import _requeue_stage_error

    project, ctx = _member_project(store, role="project_admin")
    _set_ctx(monkeypatch, ctx)

    matrix = [
        (JobStatus.FAILED, "build", True),
        (JobStatus.FAILED, "merge", False),
        (JobStatus.RUNNING, "build", False),
        (JobStatus.DONE, "test", False),
    ]
    for status, stage, expect_accept in matrix:
        job = store.create(
            idea=f"parity {status.value} {stage}",
            repo_path=project.repo_path,
            chat_id=1,
            source=JobSource.MCP,
        )
        _set_status_and_stage(store, job.id, status, stage)
        try:
            reloaded: Job = store.get(job.id)
            helper_verdict = _requeue_stage_error(reloaded, stage) is None
            assert helper_verdict is expect_accept

            mcp_result = _call(mcp, "requeue_job", job_id=job.id, stage=stage)
            mcp_accepted = isinstance(mcp_result, dict)
            assert mcp_accepted is expect_accept
        finally:
            with store._pool.connection() as conn:
                conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


# --- resolve_job ---------------------------------------------------------------


@pytest.mark.parametrize("status", [JobStatus.FAILED, JobStatus.CANCELLED])
def test_resolve_job_succeeds_for_each_terminal_status(store, mcp, monkeypatch, status):
    project, ctx = _member_project(store, role="project_admin")
    job = store.create(
        idea="terminal job", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_status_and_stage(store, job.id, status, "build")
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "resolve_job", job_id=job.id, resolution="resolved")
        assert isinstance(result, dict)
        assert result["job"]["id"] == job.id
        updated = store.get(job.id)
        assert updated.resolution == "resolved"
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_resolve_job_rejects_unsupported_resolution_value(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    job = store.create(
        idea="failed job", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_status_and_stage(store, job.id, JobStatus.FAILED, "build")
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "resolve_job", job_id=job.id, resolution="bogus")
        assert result.startswith("error:")
        assert "resolution must be one of" in result
        updated = store.get(job.id)
        assert updated.resolution == ""
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_resolve_job_rejects_non_terminal_job(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    job = store.create(
        idea="still running", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_status_and_stage(store, job.id, JobStatus.RUNNING, "build")
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "resolve_job", job_id=job.id, resolution="resolved")
        assert result == "error: job is not in a terminal state"
        updated = store.get(job.id)
        assert updated.resolution == ""
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_resolve_job_forbidden_for_non_member(store, mcp, monkeypatch):
    project, _ = _member_project(store)
    job = store.create(
        idea="not yours", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_status_and_stage(store, job.id, JobStatus.FAILED, "build")
    _set_ctx(monkeypatch, _non_member_ctx())
    try:
        result = _call(mcp, "resolve_job", job_id=job.id, resolution="resolved")
        assert result.startswith("error: forbidden")
        updated = store.get(job.id)
        assert updated.resolution == ""
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_resolve_job_unknown_job_returns_error(store, mcp, monkeypatch):
    _, ctx = _member_project(store)
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "resolve_job", job_id=999999999, resolution="resolved")
    assert result == "error: job not found"


def test_resolve_job_rest_and_mcp_agree_on_eligibility(store, mcp, monkeypatch):
    """Guard against future drift: the shared helper decides both entry points."""
    from hyqs.pipeline.models import Job
    from hyqs.web.app import _resolve_job_error

    project, ctx = _member_project(store, role="project_admin")
    _set_ctx(monkeypatch, ctx)

    matrix = [
        (JobStatus.FAILED, "resolved", True),
        (JobStatus.CANCELLED, "resolved", True),
        (JobStatus.RUNNING, "resolved", False),
        (JobStatus.DONE, "resolved", False),
        (JobStatus.FAILED, "bogus", False),
    ]
    for status, resolution, expect_accept in matrix:
        job = store.create(
            idea=f"parity {status.value} {resolution}",
            repo_path=project.repo_path,
            chat_id=1,
            source=JobSource.MCP,
        )
        _set_status_and_stage(store, job.id, status, "build")
        try:
            reloaded: Job = store.get(job.id)
            helper_verdict = _resolve_job_error(reloaded, resolution) is None
            assert helper_verdict is expect_accept

            mcp_result = _call(mcp, "resolve_job", job_id=job.id, resolution=resolution)
            mcp_accepted = isinstance(mcp_result, dict)
            assert mcp_accepted is expect_accept
        finally:
            with store._pool.connection() as conn:
                conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


# --- fix_forward_job ----------------------------------------------------------


def test_fix_forward_job_authorized_creates_job_and_links_active_remediation(
    store, mcp, monkeypatch
):
    project, ctx = _member_project(store, role="project_admin")
    source_job = store.create(
        idea="original bug", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_status_and_stage(store, source_job.id, JobStatus.FAILED, "test")
    _set_ctx(monkeypatch, ctx)
    new_job_id = None
    try:
        result = _call(mcp, "fix_forward_job", job_id=source_job.id, idea="fix the bug")
        assert isinstance(result, dict)
        new_job_id = result["job"]["id"]
        assert new_job_id != source_job.id
        assert store.get_active_remediation(source_job.id) == new_job_id
    finally:
        with store._pool.connection() as conn:
            ids = [source_job.id] + ([new_job_id] if new_job_id else [])
            conn.execute("DELETE FROM jobs WHERE id = ANY(%s)", (ids,))


def test_fix_forward_job_forbidden_for_non_member(store, mcp, monkeypatch):
    project, _ = _member_project(store)
    source_job = store.create(
        idea="not yours", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_ctx(monkeypatch, _non_member_ctx())
    try:
        result = _call(mcp, "fix_forward_job", job_id=source_job.id, idea="fix it")
        assert result.startswith("error: forbidden")
        assert store.get_active_remediation(source_job.id) is None
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (source_job.id,))


def test_fix_forward_job_unknown_job_returns_error(store, mcp, monkeypatch):
    _, ctx = _member_project(store)
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "fix_forward_job", job_id=999999999, idea="fix it")
    assert result == "error: job not found"


def test_fix_forward_job_rejects_empty_idea(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    source_job = store.create(
        idea="needs a fix", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "fix_forward_job", job_id=source_job.id, idea="   ")
        assert result == "error: idea is required"
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (source_job.id,))


def test_fix_forward_job_blocked_by_active_remediation_then_override_succeeds(
    store, mcp, monkeypatch
):
    project, ctx = _member_project(store, role="project_admin")
    source_job = store.create(
        idea="flaky test", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    active_job = store.create(
        idea="in-flight fix", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_status_and_stage(store, active_job.id, JobStatus.RUNNING, "build")
    store.set_active_remediation(source_job.id, active_job.id)
    _set_ctx(monkeypatch, ctx)
    new_job_id = None
    try:
        blocked = _call(mcp, "fix_forward_job", job_id=source_job.id, idea="fix again")
        assert isinstance(blocked, str)
        assert blocked.startswith("error:")
        assert str(active_job.id) in blocked
        assert store.get_active_remediation(source_job.id) == active_job.id

        overridden = _call(
            mcp,
            "fix_forward_job",
            job_id=source_job.id,
            idea="fix again",
            override_active_remediation=True,
        )
        assert isinstance(overridden, dict)
        new_job_id = overridden["job"]["id"]
        assert store.get_active_remediation(source_job.id) == new_job_id
    finally:
        with store._pool.connection() as conn:
            ids = [source_job.id, active_job.id] + ([new_job_id] if new_job_id else [])
            conn.execute("DELETE FROM jobs WHERE id = ANY(%s)", (ids,))


def test_fix_forward_job_repoints_dependents_to_new_job(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    source_job = store.create(
        idea="broken shared lib", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    dependent = store.create(
        idea="depends on the lib", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    store.add_job_dependency(dependent.id, source_job.id)
    _set_ctx(monkeypatch, ctx)
    new_job_id = None
    try:
        result = _call(
            mcp,
            "fix_forward_job",
            job_id=source_job.id,
            idea="fix the lib",
            repoint_dependent_ids=[dependent.id],
        )
        assert isinstance(result, dict)
        new_job_id = result["job"]["id"]
        assert store.get_dependencies(dependent.id) == [new_job_id]
        reloaded_source = store.get(source_job.id)
        assert reloaded_source.archived is True
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_dependencies WHERE job_id = %s", (dependent.id,))
            ids = [source_job.id, dependent.id] + ([new_job_id] if new_job_id else [])
            conn.execute("DELETE FROM jobs WHERE id = ANY(%s)", (ids,))


def test_fix_forward_job_rest_and_mcp_agree_on_active_remediation_conflict(store, mcp, monkeypatch):
    """Guard against future drift: the shared helper decides both entry points."""
    project, ctx = _member_project(store, role="project_admin")
    _set_ctx(monkeypatch, ctx)

    matrix = [
        (False, None, False, True),
        (True, JobStatus.RUNNING, False, False),
        (True, JobStatus.RUNNING, True, True),
        (True, JobStatus.PENDING, False, False),
        (True, JobStatus.DONE, False, True),
        (True, JobStatus.FAILED, False, True),
        (True, JobStatus.CANCELLED, False, True),
    ]
    for has_active, active_status, override, expect_accept in matrix:
        source_job = store.create(
            idea=f"parity source ({active_status}, override={override})",
            repo_path=project.repo_path,
            chat_id=1,
            source=JobSource.MCP,
        )
        active_job = None
        new_job_id = None
        try:
            if has_active:
                active_job = store.create(
                    idea="parity active remediation",
                    repo_path=project.repo_path,
                    chat_id=1,
                    source=JobSource.MCP,
                )
                _set_status_and_stage(store, active_job.id, active_status, "build")
                store.set_active_remediation(source_job.id, active_job.id)

            helper_verdict = _active_remediation_conflict(store, source_job.id, override) is None
            assert helper_verdict is expect_accept

            mcp_result = _call(
                mcp,
                "fix_forward_job",
                job_id=source_job.id,
                idea="parity fix",
                override_active_remediation=override,
            )
            mcp_accepted = isinstance(mcp_result, dict)
            assert mcp_accepted is expect_accept
            if mcp_accepted:
                new_job_id = mcp_result["job"]["id"]
        finally:
            with store._pool.connection() as conn:
                ids = [source_job.id]
                if active_job is not None:
                    ids.append(active_job.id)
                if new_job_id is not None:
                    ids.append(new_job_id)
                conn.execute("DELETE FROM jobs WHERE id = ANY(%s)", (ids,))


# --- update_job -----------------------------------------------------------


def test_update_job_title_only_persists(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    job = store.create(
        idea="original idea", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "update_job", job_id=job.id, title="new title")
        assert isinstance(result, dict)
        assert result["job"]["title"] == "new title"
        updated = store.get(job.id)
        assert updated.title == "new title"
        assert updated.idea == "original idea"
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_update_job_idea_only_persists(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    job = store.create(
        idea="original idea", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "update_job", job_id=job.id, idea="new idea")
        assert isinstance(result, dict)
        updated = store.get(job.id)
        assert updated.idea == "new idea"
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_update_job_priority_only_persists(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    job = store.create(
        idea="original idea", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "update_job", job_id=job.id, priority=7)
        assert isinstance(result, dict)
        updated = store.get(job.id)
        assert updated.priority == 7
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_update_job_epic_only_persists(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    epic = store.create_epic(project.id, "Target epic")
    job = store.create(
        idea="original idea", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "update_job", job_id=job.id, epic_id=epic.id)
        assert isinstance(result, dict)
        updated = store.get(job.id)
        assert updated.epic_id == epic.id
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_update_job_multi_field_update_applies_atomically(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    epic = store.create_epic(project.id, "Multi-field epic")
    job = store.create(
        idea="original idea", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(
            mcp,
            "update_job",
            job_id=job.id,
            title="new title",
            idea="new idea",
            priority=3,
            epic_id=epic.id,
        )
        assert isinstance(result, dict)
        updated = store.get(job.id)
        assert updated.title == "new title"
        assert updated.idea == "new idea"
        assert updated.priority == 3
        assert updated.epic_id == epic.id
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_update_job_no_fields_rejected_and_leaves_job_unchanged(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    job = store.create(
        idea="original idea", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(mcp, "update_job", job_id=job.id)
        assert result.startswith("error:")
        updated = store.get(job.id)
        assert updated.idea == "original idea"
        assert updated.title == job.title
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_update_job_atomic_rollback_on_invalid_priority(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    job = store.create(
        idea="original idea", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(
            mcp, "update_job", job_id=job.id, title="valid new title", priority="not-an-int"
        )
        assert result.startswith("error:")
        updated = store.get(job.id)
        assert updated.title == job.title
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_update_job_cross_project_epic_rejected_and_not_persisted(store, mcp, monkeypatch):
    project, ctx = _member_project(store, role="project_admin")
    other_project, _ = _member_project(store, role="project_admin")
    other_epic = store.create_epic(other_project.id, "Other project's epic")
    job = store.create(
        idea="original idea", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_ctx(monkeypatch, ctx)
    try:
        result = _call(
            mcp, "update_job", job_id=job.id, title="valid new title", epic_id=other_epic.id
        )
        assert result.startswith("error:")
        assert "different project" in result
        updated = store.get(job.id)
        assert updated.title == job.title
        assert updated.epic_id is None
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_update_job_forbidden_for_non_member(store, mcp, monkeypatch):
    project, _ = _member_project(store)
    job = store.create(
        idea="not yours", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
    )
    _set_ctx(monkeypatch, _non_member_ctx())
    try:
        result = _call(mcp, "update_job", job_id=job.id, title="hijacked title")
        assert result.startswith("error: forbidden")
        updated = store.get(job.id)
        assert updated.title == job.title
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


def test_update_job_unknown_job_returns_error(store, mcp, monkeypatch):
    _, ctx = _member_project(store)
    _set_ctx(monkeypatch, ctx)
    result = _call(mcp, "update_job", job_id=999999999, title="doesn't matter")
    assert result == "error: job not found"


def test_update_job_rest_and_mcp_agree_on_field_validation(store, mcp, monkeypatch):
    """Guard against future drift: the shared helpers decide both entry points."""
    from hyqs.web.app import _job_epic_error, _job_idea_error, _job_priority_error, _job_title_error

    project, ctx = _member_project(store, role="project_admin")
    other_project, _ = _member_project(store, role="project_admin")
    other_epic = store.create_epic(other_project.id, "Foreign epic")
    same_epic = store.create_epic(project.id, "Own epic")
    _set_ctx(monkeypatch, ctx)

    title_matrix = [("valid title", True), ("", False), ("x" * 201, False)]
    idea_matrix = [("valid idea", True), ("", False)]
    priority_matrix = [(3, True), ("not-an-int", False), (None, False)]
    epic_matrix = [(same_epic.id, True), (other_epic.id, False)]

    for title, expect_accept in title_matrix:
        job = store.create(
            idea="parity title job", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
        )
        try:
            helper_verdict = _job_title_error(title) is None
            assert helper_verdict is expect_accept
            mcp_result = _call(mcp, "update_job", job_id=job.id, title=title)
            assert isinstance(mcp_result, dict) is expect_accept
        finally:
            with store._pool.connection() as conn:
                conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))

    for idea, expect_accept in idea_matrix:
        job = store.create(
            idea="parity idea job", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
        )
        try:
            helper_verdict = _job_idea_error(idea) is None
            assert helper_verdict is expect_accept
            mcp_result = _call(mcp, "update_job", job_id=job.id, idea=idea)
            assert isinstance(mcp_result, dict) is expect_accept
        finally:
            with store._pool.connection() as conn:
                conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))

    for priority, expect_accept in priority_matrix:
        job = store.create(
            idea="parity priority job",
            repo_path=project.repo_path,
            chat_id=1,
            source=JobSource.MCP,
        )
        try:
            helper_verdict = _job_priority_error(priority) is None
            assert helper_verdict is expect_accept
            mcp_result = _call(mcp, "update_job", job_id=job.id, priority=priority)
            assert isinstance(mcp_result, dict) is expect_accept
        finally:
            with store._pool.connection() as conn:
                conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))

    for epic_id, expect_accept in epic_matrix:
        job = store.create(
            idea="parity epic job", repo_path=project.repo_path, chat_id=1, source=JobSource.MCP
        )
        try:
            reloaded = store.get(job.id)
            helper_verdict = _job_epic_error(store, reloaded, epic_id) is None
            assert helper_verdict is expect_accept
            mcp_result = _call(mcp, "update_job", job_id=job.id, epic_id=epic_id)
            assert isinstance(mcp_result, dict) is expect_accept
        finally:
            with store._pool.connection() as conn:
                conn.execute("DELETE FROM jobs WHERE id = %s", (job.id,))


# --- MCP identity resolution: Google OAuth + hyqs API tokens ------------------


def _run_identity_middleware(store, access_token: FastMCPAccessToken):
    """Drive HyqsIdentityMiddleware with a pre-validated AccessToken, mirroring
    what FastMCP's AuthenticationMiddleware sets on scope["user"] after a
    successful ApiTokenVerifier/GoogleProvider verify_token() call.

    Returns the forwarded ASGI state dict on success, or None if the
    middleware short-circuited with a forbidden response.
    """
    user = AuthenticatedUser(access_token)
    scope = {"type": "http", "user": user}
    events = []

    async def app(inner_scope, receive, send):
        events.append(("forwarded", inner_scope.get("state")))

    async def receive():
        return {"type": "http.request"}

    async def send(message):
        events.append(("send", message))

    middleware = mcp_server.HyqsIdentityMiddleware(app, store=store)
    asyncio.run(middleware(scope, receive, send))
    forwarded = next((state for kind, state in events if kind == "forwarded"), None)
    return forwarded


def test_api_token_verifier_resolves_valid_secret(store):
    project = store.create_project("MCP ApiToken Verify", f"/tmp/test-mcp-atv-{uuid.uuid4()}")
    try:
        _token, secret = store.create_api_token(project.id, "CI", "contributor")

        access_token = asyncio.run(mcp_oauth.ApiTokenVerifier(store).verify_token(secret))

        assert access_token is not None
        assert access_token.claims["hyqs_auth_kind"] == mcp_oauth.API_TOKEN_CLAIM_KIND
        assert access_token.claims["token_project_id"] == project.id
        assert access_token.claims["token_role"] == "contributor"
    finally:
        store.delete_project(project.id)


def test_api_token_verifier_rejects_unknown_secret(store):
    assert asyncio.run(mcp_oauth.ApiTokenVerifier(store).verify_token("hpat_not-real")) is None


def test_api_token_verifier_rejects_revoked_secret(store):
    project = store.create_project("MCP ApiToken Revoked", f"/tmp/test-mcp-atv2-{uuid.uuid4()}")
    try:
        token, secret = store.create_api_token(project.id, "CI", "viewer")
        store.revoke_api_token(project.id, token.id)

        assert asyncio.run(mcp_oauth.ApiTokenVerifier(store).verify_token(secret)) is None
    finally:
        store.delete_project(project.id)


def test_mcp_api_token_resolves_scoped_identity_and_grants_permitted_tool(store, mcp, monkeypatch):
    """A contributor-role token can create_job (queue_job) in its own project."""
    project = store.create_project("MCP Token Granted", f"/tmp/test-mcp-tg-{uuid.uuid4()}")
    try:
        _token, secret = store.create_api_token(project.id, "CI", "contributor")
        access_token = asyncio.run(mcp_oauth.ApiTokenVerifier(store).verify_token(secret))

        state = _run_identity_middleware(store, access_token)
        assert state is not None
        ctx = state["auth_context"]
        assert ctx.user_id is None
        assert ctx.is_platform_admin is False
        assert ctx.token_project_id == project.id
        assert ctx.token_role == "contributor"

        _set_ctx(monkeypatch, ctx)
        result = _call(mcp, "create_job", idea="ship the thing", project_id=project.id)
        assert isinstance(result, dict)
        assert result["job"]["project_id"] == project.id
    finally:
        store.delete_project(project.id)


def test_mcp_api_token_denies_tool_requiring_ungranted_permission(store, mcp, monkeypatch):
    """The same contributor token lacks edit_project, so update_project is forbidden."""
    project = store.create_project("MCP Token Denied", f"/tmp/test-mcp-td-{uuid.uuid4()}")
    try:
        _token, secret = store.create_api_token(project.id, "CI", "contributor")
        access_token = asyncio.run(mcp_oauth.ApiTokenVerifier(store).verify_token(secret))
        ctx = _run_identity_middleware(store, access_token)["auth_context"]

        _set_ctx(monkeypatch, ctx)
        result = _call(mcp, "update_project", project_id=project.id, name="Renamed")

        assert result.startswith("error: forbidden")
        assert store.get_project(project.id).name == "MCP Token Denied"
    finally:
        store.delete_project(project.id)


def test_mcp_api_token_scoped_to_project_a_forbidden_on_project_b(store, mcp, monkeypatch):
    project_a = store.create_project("MCP Token A", f"/tmp/test-mcp-a-{uuid.uuid4()}")
    project_b = store.create_project("MCP Token B", f"/tmp/test-mcp-b-{uuid.uuid4()}")
    try:
        _token, secret = store.create_api_token(project_a.id, "CI", "project_admin")
        access_token = asyncio.run(mcp_oauth.ApiTokenVerifier(store).verify_token(secret))
        ctx = _run_identity_middleware(store, access_token)["auth_context"]

        _set_ctx(monkeypatch, ctx)
        result = _call(mcp, "create_job", idea="cross project idea", project_id=project_b.id)

        assert result.startswith("error: forbidden")
    finally:
        store.delete_project(project_a.id)
        store.delete_project(project_b.id)


def test_mcp_google_oauth_identity_path_unaffected_by_api_token_wiring(store, mcp, monkeypatch):
    project = store.create_project("MCP OAuth Project", f"/tmp/test-mcp-oauth-{uuid.uuid4()}")
    user = store.create_user(f"mcp-oauth-{uuid.uuid4()}@example.com", "pw")
    store.add_project_member(project.id, str(user.id), "contributor")
    try:
        access_token = FastMCPAccessToken(
            token="google-token", client_id="google-client", scopes=[], claims={"email": user.email}
        )

        state = _run_identity_middleware(store, access_token)

        assert state is not None
        ctx = state["auth_context"]
        assert ctx.user_id == user.id
        assert ctx.user_email == user.email
        assert ctx.is_platform_admin is False
        assert ctx.token_project_id is None
        assert ctx.token_role is None
        assert ctx.api_token_id is None

        _set_ctx(monkeypatch, ctx)
        result = _call(mcp, "create_job", idea="oauth path idea", project_id=project.id)
        assert isinstance(result, dict)
        assert result["job"]["project_id"] == project.id
    finally:
        store.delete_project(project.id)


def test_mcp_identity_middleware_forbidden_for_unknown_oauth_email(store):
    access_token = FastMCPAccessToken(
        token="google-token",
        client_id="google-client",
        scopes=[],
        claims={"email": "nobody@example.com"},
    )

    assert _run_identity_middleware(store, access_token) is None
