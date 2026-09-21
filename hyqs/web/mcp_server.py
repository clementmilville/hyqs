"""FastMCP server mounted at /mcp, protected by OAuth 2.1 via Google OIDC proxy,
alongside hyqs project-scoped API tokens.

Auth is handled by a MultiAuth provider (see hyqs.web.mcp_oauth) composing
Google's GoogleProvider (OAuthProxy) with an ApiTokenVerifier for hyqs API
token secrets.  After the Bearer token is validated by FastMCP's
AuthenticationMiddleware, HyqsIdentityMiddleware maps the resulting identity
— either a Google email claim or a resolved API-token claim — to a hyqs
AuthContext stored in request.state.auth_context.  Tool functions read that
context through the existing get_http_request() / request.state pattern and
enforce per-project membership exactly as before.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from fastmcp import Context, FastMCP
from fastmcp.server.dependencies import get_http_request
from starlette.types import Receive, Scope, Send

from hyqs.config import Config
from hyqs.pipeline import github, nginx_sites, remediation
from hyqs.pipeline.models import BacklogItemType, Job, JobSource, JobStatus, Stage
from hyqs.pipeline.provision import slugify
from hyqs.pipeline.store import _UNSET, JobStore, set_db_actor
from hyqs.web.app import (
    _VALID_WEBHOOK_EVENTS,
    WEB_CHAT_ID,
    _active_remediation_conflict,
    _detect_batch_dependency_cycle,
    _job_diff_payload,
    _job_epic_error,
    _job_idea_error,
    _job_priority_error,
    _job_title_error,
    _parse_batch_job_common_fields,
    _requeue_stage_error,
    _resolve_epic,
    _resolve_job_error,
    agent_to_dict,
    backlog_item_to_dict,
    epic_to_dict,
    job_projection_extras,
    job_to_dict,
    job_to_summary_dict,
    project_to_dict,
    promotion_to_dict,
    webhook_to_dict,
)
from hyqs.web.auth import AuthContext, check_permission, is_project_member
from hyqs.web.mcp_oauth import (
    API_TOKEN_CLAIM_KIND,
    build_mcp_auth_provider,
    resolve_oauth_identity,
)

_VALID_EPIC_STATUSES = frozenset({"active", "paused", "done", "archived"})
_VALID_PROJECT_STATUSES = frozenset({"active", "paused", "archived"})
_SLACK_CHANNEL_ID_RE = re.compile(r"^[CG][A-Z0-9]{6,}$")


class HyqsIdentityMiddleware:
    """ASGI middleware: map a validated identity to a hyqs AuthContext.

    Runs after FastMCP's AuthenticationMiddleware (which validates the Bearer
    token — either a Google identity or a hyqs API token secret, see
    hyqs.web.mcp_oauth.build_mcp_auth_provider — and sets scope["user"]).
    Resolves the validated identity to a hyqs AuthContext and stores it in
    scope["state"] so that request.state.auth_context is available to tool
    functions.

    Requests where scope["user"] is not an AuthenticatedUser (e.g. OAuth
    discovery / callback paths) pass through unmodified — FastMCP's own
    RequireAuthMiddleware guards the MCP endpoint separately.
    """

    def __init__(self, app, *, store: JobStore) -> None:
        self.app = app
        self.store = store

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

            user = scope.get("user")
            if isinstance(user, AuthenticatedUser):
                claims = getattr(user.access_token, "claims", None) or {}
                ctx: AuthContext | None = None
                if claims.get("hyqs_auth_kind") == API_TOKEN_CLAIM_KIND:
                    # Already resolved (hashed, looked up, scoped) by
                    # ApiTokenVerifier — just carry it through into an
                    # AuthContext, no re-lookup here.
                    ctx = AuthContext(
                        user_id=None,
                        user_email=None,
                        is_platform_admin=False,
                        token_project_id=claims.get("token_project_id"),
                        token_role=claims.get("token_role"),
                        api_token_id=claims.get("api_token_id"),
                    )
                else:
                    email = claims.get("email")
                    if email:
                        ctx = resolve_oauth_identity(self.store, email)
                if ctx is not None:
                    if ctx.api_token_id is not None:
                        set_db_actor(f"mcp:apitoken:{ctx.api_token_id}")
                    else:
                        set_db_actor(f"mcp:{ctx.user_email}" if ctx.user_email else "mcp:token")
                if ctx is None:
                    # Valid bearer token but no active hyqs identity resolves from it.
                    body = json.dumps(
                        {"error": "forbidden", "detail": "no active hyqs account"}
                    ).encode()
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 403,
                            "headers": [
                                (b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode()),
                            ],
                        }
                    )
                    await send({"type": "http.response.body", "body": body})
                    return
                scope.setdefault("state", {})["auth_context"] = ctx

        await self.app(scope, receive, send)


_CORS_PREFLIGHT_HEADERS = [
    (b"access-control-allow-origin", b"*"),
    (b"access-control-allow-methods", b"GET, POST, OPTIONS"),
    (b"access-control-allow-headers", b"authorization, content-type, mcp-session-id"),
    (b"access-control-max-age", b"86400"),
]


class CorsPreflightASGI:
    """ASGI wrapper: answer CORS preflight OPTIONS requests directly.

    MCP clients (e.g. Claude Code) send an OPTIONS preflight before POST/GET
    /mcp. FastMCP's own stack has no route for OPTIONS, so it falls through to
    a bare 405 with a non-JSON body, which breaks client reconnection. This
    wrapper intercepts OPTIONS on http requests and answers it directly,
    leaving every other method (and FastMCP's OAuth/auth stack) untouched.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "OPTIONS":
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": _CORS_PREFLIGHT_HEADERS,
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return

        await self.app(scope, receive, send)


class PathRewriteASGI:
    """ASGI wrapper: forward a request to `app` with scope["path"] replaced.

    Used to expose FastMCP's internally-mounted OAuth discovery metadata (it
    only serves .well-known/oauth-authorization-server relative to its own
    root, i.e. at /mcp/.well-known/oauth-authorization-server here) at the
    RFC 8414 §3.1 standard location too: {origin}/.well-known/{...}{resource
    path}. Real MCP clients (e.g. Claude Code) only probe the RFC 8414
    canonical path, never FastMCP's mount-relative one — without this, they
    404 out of discovery entirely and fall back to guessing endpoints at the
    bare origin (e.g. POST /register), which 405s.
    """

    def __init__(self, app, path: str) -> None:
        self.app = app
        self.path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            scope = {**scope, "path": self.path, "raw_path": self.path.encode()}
        await self.app(scope, receive, send)


def _dedup_job_response(store: JobStore, duplicate: Job) -> dict:
    """Shape a {"job", "auto_epic", "deduplicated": True} response for a job

    that was returned instead of creating a duplicate — shared by the
    idempotency-key path and the 10-second content-based dedup path.
    """
    dup_epic = store.get_epic(duplicate.epic_id) if duplicate.epic_id else None
    return {
        "job": job_to_dict(duplicate, **job_projection_extras(store, duplicate)),
        "auto_epic": {
            "id": duplicate.epic_id,
            "name": dup_epic.name if dup_epic else "",
            "auto_assigned": False,
            "auto_created": False,
        },
        "deduplicated": True,
    }


async def _create_job_impl(
    store: JobStore,
    ctx: AuthContext,
    *,
    idea: str,
    project_id: int,
    title: str = "",
    epic_id: int | None = None,
    depends_on: list[int] | None = None,
    fixes_job_id: int | None = None,
    allow_parallel_remediation: bool = False,
    idempotency_key: str | None = None,
) -> dict | str:
    """Create a job, mirroring app.py's create_job REST handler. Testable in isolation."""
    idea = (idea or "").strip()
    if not idea:
        return "error: idea is required"
    idempotency_key = (idempotency_key or "").strip() or None

    if not check_permission(store, ctx, "queue_job", project_id=project_id):
        return "error: forbidden — you are not a member of this project"

    project = store.get_project(project_id)
    if project is None:
        return "error: project not found"
    repo = project.repo_path

    if idempotency_key is not None:
        existing = store.get_by_idempotency_key(project_id, idempotency_key)
        if existing is not None:
            return _dedup_job_response(store, existing)

    if fixes_job_id is not None:
        incident_job = store.get(fixes_job_id)
        if incident_job is None:
            return "error: fixes_job_id not found"
        if incident_job.project_id != project_id:
            return "error: fixes_job_id belongs to a different project"
        if not allow_parallel_remediation:
            active_id = store.get_active_remediation(fixes_job_id)
            if active_id is not None:
                active_job = store.get(active_id)
                if active_job is not None and active_job.status not in (
                    JobStatus.DONE,
                    JobStatus.FAILED,
                    JobStatus.CANCELLED,
                ):
                    return remediation.format_conflict_reason(fixes_job_id, active_id)

    duplicate = store.find_recent_duplicate(repo, idea, 10)
    if duplicate is not None:
        return _dedup_job_response(store, duplicate)

    auto_assigned = False
    auto_created = False
    if epic_id is None:
        epic_id, auto_created, _is_fallback = _resolve_epic(store, project.id, idea)
        auto_assigned = True

    source_meta = {"role": "mcp"}
    if fixes_job_id is not None:
        lineage = store.resolve_remediation_lineage(fixes_job_id)
        source_meta["fixes_job_id"] = fixes_job_id
        source_meta["remediation_root_job_id"] = lineage.root_job_id
        source_meta["remediation_depth"] = lineage.depth
        if allow_parallel_remediation:
            source_meta["allow_parallel_remediation"] = True

    try:
        job = store.create(
            idea=idea,
            repo_path=repo,
            chat_id=WEB_CHAT_ID,
            epic_id=epic_id,
            depends_on=depends_on,
            title=title,
            source=JobSource.MCP,
            source_actor=ctx.user_email or "",
            source_meta=source_meta,
            idempotency_key=idempotency_key,
        )
    except ValueError as exc:
        return f"error: {exc}"

    if fixes_job_id is not None:
        store.set_active_remediation(fixes_job_id, job.id)

    assigned_epic = store.get_epic(epic_id)
    return {
        "job": job_to_dict(job, **job_projection_extras(store, job)),
        "auto_epic": {
            "id": epic_id,
            "name": assigned_epic.name if assigned_epic else "",
            "auto_assigned": auto_assigned,
            "auto_created": auto_created,
        },
    }


async def _create_job_wave_impl(
    store: JobStore,
    ctx: AuthContext,
    *,
    project_id: int,
    jobs: list[dict],
    epic_id: int | None = None,
) -> dict | str:
    """Atomically create a wave of interdependent jobs, mirroring app.py's
    create_job_batch REST handler. Testable in isolation."""
    if not isinstance(jobs, list) or not jobs:
        return "error: jobs must be a non-empty list"
    for item in jobs:
        if (
            not isinstance(item, dict)
            or not (item.get("title") or "").strip()
            or not (item.get("idea") or "").strip()
        ):
            return "error: each job must be an object with a non-empty title and idea"

    if not check_permission(store, ctx, "queue_job", project_id=project_id):
        return "error: forbidden — you are not a member of this project"

    project = store.get_project(project_id)
    if project is None:
        return "error: project not found"

    n = len(jobs)
    depends_on_by_index: list[list[int]] = []
    for item in jobs:
        raw = item.get("depends_on")
        depends_on_by_index.append(
            raw if isinstance(raw, list) and all(isinstance(x, int) for x in raw) else []
        )

    try:
        _detect_batch_dependency_cycle(depends_on_by_index)

        jobs_spec: list[dict] = []
        for item in jobs:
            common_fields = _parse_batch_job_common_fields(item, n)
            title = (item.get("title") or "").strip()
            idea = (item.get("idea") or "").strip()

            item_epic_id = epic_id if epic_id is not None else item.get("epic_id")
            if item_epic_id is not None:
                item_epic_id = int(item_epic_id)
            else:
                item_epic_id, _auto_created, _is_fallback = _resolve_epic(store, project.id, idea)

            jobs_spec.append(
                {
                    "idea": idea,
                    "title": title,
                    "epic_id": item_epic_id,
                    "depends_on": common_fields["depends_on"],
                    "target_files": common_fields["target_files"],
                    "priority": common_fields["priority"],
                    "idempotency_key": (item.get("idempotency_key") or "").strip() or None,
                }
            )

        created = store.create_batch(
            project.repo_path,
            jobs_spec,
            chat_id=WEB_CHAT_ID,
            source=JobSource.MCP,
            source_actor=ctx.user_email or "",
        )
    except ValueError as exc:
        return f"error: {exc}"

    return {
        "jobs": [
            {
                "id": j.id,
                "title": j.title,
                "status": j.status.value,
                "depends_on": store.get_dependencies(j.id),
            }
            for j in created
        ]
    }


async def _cancel_job_impl(store: JobStore, ctx: AuthContext, job_id: int) -> dict | str:
    """Cancel a job, mirroring app.py's cancel_job REST handler. Testable in isolation."""
    job = store.get(job_id)
    if job is None:
        return "error: job not found"
    if not check_permission(store, ctx, "cancel_job", project_id=job.project_id):
        return "error: forbidden — you are not a member of this project"
    if not store.cancel(job_id):
        return "error: job already in terminal state"
    updated = store.get(job_id)
    return {"job": job_to_dict(updated, **job_projection_extras(store, updated))}


async def _retry_job_impl(
    store: JobStore, config: Config, ctx: AuthContext, job_id: int, force: bool = False
) -> dict | str:
    """Retry a job, mirroring app.py's retry_job REST handler. Testable in isolation."""
    job = store.get(job_id)
    if job is None:
        return "error: job not found"
    if not check_permission(store, ctx, "retry_job", project_id=job.project_id):
        return "error: forbidden — you are not a member of this project"
    if job.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
        return "error: job is not in a retryable state"
    if job.needs_split and not force:
        return (
            "error: this job's plan was too large even after a re-ask and has been "
            "parked — a bare retry is refused; refile a new, smaller job, or retry "
            "with force=true to bypass the scope gate"
        )

    active_id = store.get_active_remediation(job_id)
    if active_id is not None and active_id != job_id:
        active_job = store.get(active_id)
        if active_job is not None and active_job.status not in (
            JobStatus.DONE,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        ):
            return remediation.format_conflict_reason(job_id, active_id)

    # Clean up git artifacts BEFORE mutating the DB so there is no partial state
    # if cleanup fails.
    worktree = Path(config.data_dir) / "worktrees" / f"job-{job_id}"
    cleanup_error = await github.cleanup_branch_for_retry(job, worktree)
    if cleanup_error:
        return f"error: {cleanup_error}"

    updated = store.retry(job_id, force=force)
    if updated is None:
        return "error: retry failed"
    return {"job": job_to_dict(updated, **job_projection_extras(store, updated))}


async def _requeue_job_impl(
    store: JobStore, ctx: AuthContext, job_id: int, stage: str
) -> dict | str:
    """Requeue a FAILED job at a stage, mirroring app.py's requeue_job REST handler."""
    job = store.get(job_id)
    if job is None:
        return "error: job not found"
    if not check_permission(store, ctx, "resolve_job", project_id=job.project_id):
        return "error: forbidden — you are not a member of this project"
    stage_error = _requeue_stage_error(job, stage)
    if stage_error is not None:
        _, message = stage_error
        return f"error: {message}"
    store.requeue_job_at_stage(job_id, Stage(stage))
    updated = store.get(job_id)
    return {"job": job_to_dict(updated, **job_projection_extras(store, updated))}


async def _resolve_job_impl(
    store: JobStore, ctx: AuthContext, job_id: int, resolution: str = "resolved"
) -> dict | str:
    """Resolve a terminal job, mirroring app.py's resolve_job REST handler."""
    job = store.get(job_id)
    if job is None:
        return "error: job not found"
    if not check_permission(store, ctx, "resolve_job", project_id=job.project_id):
        return "error: forbidden — you are not a member of this project"
    error = _resolve_job_error(job, resolution)
    if error is not None:
        _, message = error
        return f"error: {message}"
    store.set_job_resolution(job_id, resolution)
    updated = store.get(job_id)
    return {"job": job_to_dict(updated, **job_projection_extras(store, updated))}


async def _update_job_impl(
    store: JobStore,
    ctx: AuthContext,
    job_id: int,
    *,
    title: str | None = None,
    idea: str | None = None,
    priority: int | None = None,
    epic_id: int | None = None,
) -> dict | str:
    """Atomically update one or more job fields, mirroring the REST title/idea/
    priority/epic endpoints via the shared app.py validators."""
    job = store.get(job_id)
    if job is None:
        return "error: job not found"
    if not check_permission(store, ctx, "edit_job_deps", project_id=job.project_id):
        return "error: forbidden — you are not a member of this project"
    if title is None and idea is None and priority is None and epic_id is None:
        return "error: at least one of title, idea, priority, epic_id must be supplied"

    updates: dict[str, object] = {}
    if title is not None:
        title = title.strip()
        error = _job_title_error(title)
        if error is not None:
            return f"error: {error[1]}"
        updates["title"] = title
    if idea is not None:
        idea = idea.strip()
        error = _job_idea_error(idea)
        if error is not None:
            return f"error: {error[1]}"
        updates["idea"] = idea
    if priority is not None:
        error = _job_priority_error(priority)
        if error is not None:
            return f"error: {error[1]}"
        updates["priority"] = priority
    if epic_id is not None:
        error = _job_epic_error(store, job, epic_id)
        if error is not None:
            return f"error: {error[1]}"
        updates["epic_id"] = epic_id

    updated = store.update_job_fields(job_id, **updates)
    return {"job": job_to_dict(updated, **job_projection_extras(store, updated))}


async def _fix_forward_job_impl(
    store: JobStore,
    ctx: AuthContext,
    job_id: int,
    *,
    idea: str,
    title: str = "",
    repoint_dependent_ids: list[int] | None = None,
    override_active_remediation: bool = False,
) -> dict | str:
    """File a remediation job for job_id, mirroring app.py's fix_forward_job REST handler."""
    job = store.get(job_id)
    if job is None:
        return "error: job not found"
    if not check_permission(store, ctx, "resolve_job", project_id=job.project_id):
        return "error: forbidden — you are not a member of this project"
    idea = (idea or "").strip()
    title = (title or "").strip()
    if not idea:
        return "error: idea is required"

    conflict = _active_remediation_conflict(store, job.id, override_active_remediation)
    if conflict is not None:
        active_id, message = conflict
        return f"error: {message} (active_remediation_job_id={active_id})"

    lineage = store.resolve_remediation_lineage(job.id)
    source_meta = {"fix_for": job.id}
    if (
        isinstance(lineage.root_job_id, int)
        and isinstance(lineage.depth, int)
        and (lineage.root_job_id != job.id or lineage.depth > 0)
    ):
        source_meta.update(
            {
                "remediation_root_job_id": lineage.root_job_id,
                "remediation_depth": lineage.depth,
            }
        )
    if repoint_dependent_ids:
        fix_job = store.fix_forward_and_repoint(
            job.id, idea, title, ctx.user_email or "", repoint_dependent_ids
        )
    else:
        fix_job = store.create(
            idea=idea,
            repo_path=job.repo_path,
            chat_id=job.chat_id,
            epic_id=job.epic_id,
            title=title,
            source=JobSource.MCP,
            source_actor=ctx.user_email or "",
            source_meta=source_meta,
        )
    store.set_active_remediation(job.id, fix_job.id)
    return {"job": job_to_dict(fix_job, **job_projection_extras(store, fix_job))}


async def _archive_job_impl(
    store: JobStore, config: Config, ctx: AuthContext, job_id: int
) -> dict | str:
    """Archive a job, mirroring app.py's archive_job REST handler. Testable in isolation."""
    job = store.get(job_id)
    if job is None:
        return "error: not found"
    if not check_permission(store, ctx, "archive_job", project_id=job.project_id):
        return "error: forbidden — you are not a member of this project"
    if not store.set_archived(job_id, True):
        return "error: not found"
    job = store.get(job_id)
    # Only tear down artifacts for a retired (terminal) job — never disturb a
    # running job's live worktree/branch.
    if job and job.status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
        await github.cleanup_job_artifacts(config, job)
    return {"job": job_to_dict(job, **job_projection_extras(store, job))}


async def _unarchive_job_impl(store: JobStore, ctx: AuthContext, job_id: int) -> dict | str:
    """Unarchive a job, mirroring app.py's unarchive_job REST handler. Testable in isolation."""
    job = store.get(job_id)
    if job is None:
        return "error: not found"
    if not check_permission(store, ctx, "archive_job", project_id=job.project_id):
        return "error: forbidden — you are not a member of this project"
    if not store.set_archived(job_id, False):
        return "error: not found"
    job = store.get(job_id)
    return {"job": job_to_dict(job, **job_projection_extras(store, job))}


async def _add_job_dependency_impl(
    store: JobStore, ctx: AuthContext, job_id: int, depends_on_job_id: int
) -> dict | str:
    """Add a job dependency, mirroring app.py's add_job_dependency REST handler."""
    job = store.get(job_id)
    if job is None:
        return "error: job not found"
    if not check_permission(store, ctx, "edit_job_deps", project_id=job.project_id):
        return "error: forbidden — you are not a member of this project"
    store.add_job_dependency(job_id, depends_on_job_id)
    return {"depends_on": store.get_dependencies(job_id)}


async def _remove_job_dependency_impl(
    store: JobStore, ctx: AuthContext, job_id: int, depends_on_job_id: int
) -> dict | str:
    """Remove a job dependency, mirroring app.py's delete_job_dependency REST handler."""
    job = store.get(job_id)
    if job is None:
        return "error: job not found"
    if not check_permission(store, ctx, "edit_job_deps", project_id=job.project_id):
        return "error: forbidden — you are not a member of this project"
    store.remove_job_dependency(job_id, depends_on_job_id)
    return {"depends_on": store.get_dependencies(job_id)}


async def _get_job_dependency_graph_impl(
    store: JobStore, ctx: AuthContext, job_id: int
) -> dict | str:
    """Return the full upstream and downstream graph for an authorized job."""
    job = store.get(job_id)
    if job is None:
        return "error: job not found"
    if job.project_id is not None and not is_project_member(store, ctx, job.project_id):
        return "error: forbidden — you are not a member of this project"
    depends_on_jobs = store.get_dependency_jobs(job_id)
    dependents = store.get_dependent_jobs(job_id)
    return {
        "depends_on_jobs": [
            {"id": d.id, "title": d.title, "status": d.status.value} for d in depends_on_jobs
        ],
        "dependents": [
            {"id": d.id, "title": d.title, "status": d.status.value} for d in dependents
        ],
    }


async def _promote_release_impl(
    store: JobStore,
    ctx: AuthContext,
    *,
    project_id: int,
    release_id: int,
    target_env_id: int,
) -> dict | str:
    """Promote a release to an environment, mirroring the promotions store
    orchestration. Testable in isolation."""
    if not is_project_member(store, ctx, project_id):
        return "error: forbidden — you are not a member of this project"
    if not check_permission(store, ctx, "deploy.promote", project_id=project_id):
        return "error: forbidden — you are not a member of this project"

    release = store.get_release(release_id)
    if release is None or release.project_id != project_id:
        return "error: release not found"
    target_env = store.get_environment(target_env_id)
    if target_env is None or target_env.project_id != project_id:
        return "error: environment not found"
    if target_env.kind == "prod" and not check_permission(
        store, ctx, "deploy.promote_prod", project_id=project_id
    ):
        return "error: forbidden — promoting to a prod environment requires deploy.promote_prod"

    promotion = store.promote_release(
        project_id,
        release_id,
        target_env_id,
        requested_by=ctx.user_email or "",
        chat_id=WEB_CHAT_ID,
    )
    if promotion is None:
        return {"promotion": None, "noop": True}
    job = store.get(promotion.deploy_job_id)
    return {"promotion": promotion_to_dict(promotion), "job": job_to_dict(job)}


async def _offer_release_impl(
    store: JobStore,
    ctx: AuthContext,
    *,
    project_id: int,
    release_id: int,
    target_env_id: int,
) -> dict | str:
    """Make a release available for a client-kind environment's own approver
    to pull and apply. Unlike ``_promote_release_impl``, this never enqueues a
    deploy job. Testable in isolation."""
    if not is_project_member(store, ctx, project_id):
        return "error: forbidden — you are not a member of this project"
    if not check_permission(store, ctx, "deploy.offer", project_id=project_id):
        return "error: forbidden — you are not a member of this project"

    release = store.get_release(release_id)
    if release is None or release.project_id != project_id:
        return "error: release not found"
    target_env = store.get_environment(target_env_id)
    if target_env is None or target_env.project_id != project_id:
        return "error: environment not found"

    promotion = store.offer_release(
        project_id,
        release_id,
        target_env_id,
        requested_by=ctx.user_email or "",
    )
    if promotion is None:
        return {"promotion": None, "noop": True}
    return {"promotion": promotion_to_dict(promotion)}


def build_mcp_server(store: JobStore, config: Config) -> FastMCP:
    """Create and return a configured FastMCP server with OAuth 2.1 auth.

    HyqsIdentityMiddleware is NOT added here — it is ASGI/HTTP middleware and
    must be passed to http_app(middleware=[...]) at the mount site (app.py) so
    that it sits inside FastMCP's AuthenticationMiddleware in the ASGI stack.
    Adding it to FastMCP(middleware=[...]) would use FastMCP's MCP-protocol
    middleware layer instead, which does not have access to scope["user"].
    """
    auth_provider = build_mcp_auth_provider(config, store)

    mcp = FastMCP(
        "hyqs",
        auth=auth_provider,
    )

    @mcp.tool()
    async def list_projects() -> list[dict]:
        """List all projects accessible to the authenticated caller."""
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        all_summaries = store.list_projects()
        if ctx.is_platform_admin:
            project_ids = [p["id"] for p in all_summaries]
        elif not ctx.user_id:
            return []
        else:
            allowed_ids = set(store.get_user_project_ids(str(ctx.user_id)))
            project_ids = [p["id"] for p in all_summaries if p["id"] in allowed_ids]
        result = []
        for pid in project_ids:
            project = store.get_project(pid)
            if project is not None:
                result.append(project_to_dict(project))
        return result

    @mcp.tool()
    async def list_jobs(
        project_id: int,
        status: str = "active",
        summary: bool = False,
        cursor: int | None = None,
    ) -> list[dict] | str:
        """List jobs for a project. Caller must be a project member.

        status: active (default), pending, running, deploying, done, failed,
            cancelled, archived, or all.
        summary: if True, return a reduced projection (id/title/status/stage/
            epic_id/project_id/waiting_on/scheduler_wait/branch/archived/
            priority/effective_priority/priority_reasons/created_at/
            updated_at/has_error/failure_code) instead of the full job dict —
            omits idea, plan, review, and other large blobs. Default False
            preserves the existing full-dict response.
        cursor: pass the last job id from a previous page to fetch the next
            strictly-older page (up to 50 jobs). Omit for the first page.
        """
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        if not is_project_member(store, ctx, project_id):
            return "error: forbidden — you are not a member of this project"
        _VALID = frozenset(
            {
                "active",
                "pending",
                "running",
                "deploying",
                "done",
                "failed",
                "cancelled",
                "archived",
                "all",
            }
        )
        if status not in _VALID:
            return f"error: status must be one of {', '.join(sorted(_VALID))}"
        if cursor is not None and cursor <= 0:
            return "error: cursor must be a positive integer"
        page = store.list_jobs_page(project_id, status=status, cursor=cursor)
        if summary:
            return [job_to_summary_dict(j, **job_projection_extras(store, j)) for j in page.jobs]
        return [job_to_dict(j, **job_projection_extras(store, j)) for j in page.jobs]

    @mcp.tool()
    async def get_job(job_id: int, summary: bool = False) -> dict | str:
        """Fetch a single job by ID. Caller must be a member of the job's project.

        summary: if True, return a reduced projection (id/title/status/stage/
            epic_id/project_id/waiting_on/scheduler_wait/branch/archived/
            priority/effective_priority/priority_reasons/created_at/
            updated_at/has_error/failure_code) instead of the full job dict —
            omits idea, plan, review, and other large blobs. Default False
            preserves the existing full-dict response.
        """
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        job = store.get(job_id)
        if job is None:
            return "error: job not found"
        if job.project_id is not None and not is_project_member(store, ctx, job.project_id):
            return "error: forbidden — you are not a member of this project"
        if summary:
            return job_to_summary_dict(job, **job_projection_extras(store, job))
        return job_to_dict(job, **job_projection_extras(store, job))

    @mcp.tool()
    async def list_epics(project_id: int) -> list[dict] | str:
        """List epics for a project. Caller must be a project member."""
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        if not is_project_member(store, ctx, project_id):
            return "error: forbidden — you are not a member of this project"
        return store.list_epics(project_id=project_id)

    @mcp.tool()
    async def job_events(job_id: int) -> list[dict] | str:
        """Return the stage event timeline for a job. Caller must be a project member."""
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        job = store.get(job_id)
        if job is None:
            return "error: job not found"
        if job.project_id is not None and not is_project_member(store, ctx, job.project_id):
            return "error: forbidden — you are not a member of this project"
        return store.list_events(job_id)

    @mcp.tool()
    async def job_supervisor_events(job_id: int) -> list[dict] | str:
        """Return the supervisor remediation timeline (janitor classify/requeue/gate-fix/deploy-fix actions) for a job. Caller must be a project member."""
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        job = store.get(job_id)
        if job is None:
            return "error: job not found"
        if job.project_id is not None and not is_project_member(store, ctx, job.project_id):
            return "error: forbidden — you are not a member of this project"
        return store.list_supervisor_events_for_job(job_id)

    @mcp.tool()
    async def watch_job(job_id: int, timeout_seconds: int = 120, ctx: Context = None) -> dict | str:
        """Stream stage-transition events for a job until it finishes or times out.

        Pushes each new job_events row as an MCP notification (via ctx.info) as it
        is observed, then returns the final job + full event list once the job
        reaches a terminal status (done/failed/cancelled) or timeout_seconds elapses.
        timeout_seconds is clamped to [1, 600]. Caller must be a project member.
        """
        request = get_http_request()
        auth_ctx: AuthContext = request.state.auth_context
        job = store.get(job_id)
        if job is None:
            return "error: job not found"
        if job.project_id is not None and not is_project_member(store, auth_ctx, job.project_id):
            return "error: forbidden — you are not a member of this project"

        timeout_seconds = max(1, min(timeout_seconds, 600))

        after_id = 0
        elapsed = 0.0
        timed_out = True
        while elapsed <= timeout_seconds:
            new_events = store.list_events_after(job_id, after_id)
            for event in new_events:
                after_id = event["id"]
                if ctx is not None:
                    await ctx.info(json.dumps(event, default=str))
            job = store.get(job_id)
            if job is not None and job.status in (
                JobStatus.DONE,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            ):
                timed_out = False
                break
            await asyncio.sleep(1)
            elapsed += 1

        return {
            "job": job_to_dict(job, **job_projection_extras(store, job)),
            "events": store.list_events(job_id),
            "timed_out": timed_out,
        }

    @mcp.tool()
    async def job_diff(job_id: int) -> str:
        """Return the git diff for a job's branch against the default branch.

        Returns an empty string if the job has no branch yet. Caller must be a project member.
        """
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        job = store.get(job_id)
        if job is None:
            return "error: job not found"
        if job.project_id is not None and not is_project_member(store, ctx, job.project_id):
            return "error: forbidden — you are not a member of this project"
        if not job.branch:
            return ""
        payload = await _job_diff_payload(job)
        if "error" in payload:
            return f"error: {payload['error']}"
        return payload["diff"]

    @mcp.tool()
    async def survey_job_queue(project_id: int, candidates: list[dict]) -> dict | str:
        """Check candidate jobs' target files for file-path collisions against
        this project's active job queue (QUEUED/PLANNING/RUNNING/DEPLOYING),
        before any of those candidate jobs are created. Caller must be a
        project member.

        File-path collision detection only — this does NOT do title/brief
        similarity ("capability overlap") matching; that requires fuzzy
        judgment with no deterministic substrate and is explicitly out of
        scope for this tool.

        ``candidates`` is a non-empty list of ``{key, title, target_files}``
        (``key`` is caller-chosen and only used to key the response; ``title``
        is accepted for context but never compared).

        Returns ``{"results": {key: {"overlapping_job_ids": [...],
        "depends_on": [...]}, ...}, "unknown_target_file_jobs": [...]}``.
        ``depends_on`` mirrors ``overlapping_job_ids`` — the same
        file-contention relationship ``create_job_wave`` already auto-chains
        for jobs declared together, now computed against the live queue
        instead. ``unknown_target_file_jobs`` lists active jobs whose target
        files can't be determined (no plan, no idea-declared "Target files:"
        list, no granted scope) — an unknown-scope job must never be assumed
        collision-free, so it is surfaced here rather than silently ignored.
        """
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        if not is_project_member(store, ctx, project_id):
            return "error: forbidden — you are not a member of this project"
        if not isinstance(candidates, list) or not candidates:
            return "error: candidates must be a non-empty list"
        for item in candidates:
            if not isinstance(item, dict) or not str(item.get("key") or "").strip():
                return "error: each candidate must be an object with a non-empty key"

        result = store.survey_active_job_queue(project_id, candidates)
        return {
            "results": {
                key: {"overlapping_job_ids": ids, "depends_on": list(ids)}
                for key, ids in result.overlaps.items()
            },
            "unknown_target_file_jobs": result.unknown_target_file_jobs,
        }

    @mcp.tool()
    async def project_performance(project_id: int, include_operational: bool = False) -> dict | str:
        """Return headline and per-stage performance stats for a project.

        Operational auto-deploy jobs are excluded by default; set
        ``include_operational=True`` to include them. Returns a dict with keys
        ``headline``, ``stage_stats``, and ``excludes_operational``. Caller must
        be a project member.
        """
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        if not is_project_member(store, ctx, project_id):
            return "error: forbidden — you are not a member of this project"
        return {
            "headline": store.performance_headline_stats(
                project_id, include_operational=include_operational
            ),
            "stage_stats": store.performance_stage_stats(
                project_id, include_operational=include_operational
            ),
            "excludes_operational": not include_operational,
        }

    @mcp.tool()
    async def list_agents(project_id: int) -> list[dict] | str:
        """List the agent roster for a project. Caller must be a project member."""
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        if not is_project_member(store, ctx, project_id):
            return "error: forbidden — you are not a member of this project"
        return [agent_to_dict(a) for a in store.list_agents(project_id)]

    @mcp.tool()
    async def agent_stats(project_id: int, include_operational: bool = False) -> dict | str:
        """Return cost, avg duration, and fix-rate stats per agent for a project.

        Operational auto-deploy jobs are excluded by default; set
        ``include_operational=True`` to include them. Returns
        ``{"stats": [...], "excludes_operational": bool}``. Caller must be a
        project member.
        """
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        if not is_project_member(store, ctx, project_id):
            return "error: forbidden — you are not a member of this project"
        return {
            "stats": store.agent_stats(project_id, include_operational=include_operational),
            "excludes_operational": not include_operational,
        }

    @mcp.tool()
    async def create_job(
        idea: str,
        project_id: int,
        title: str = "",
        epic_id: int | None = None,
        depends_on: list[int] | None = None,
        fixes_job_id: int | None = None,
        allow_parallel_remediation: bool = False,
        idempotency_key: str = "",
    ) -> dict | str:
        """Create a new job in a project. Caller must be a member of the project.

        Prefer several small, focused jobs over one large job: big jobs are more
        likely to exhaust the pipeline's fix-attempt budget and land as dangling
        commits needing manual recovery, and gate review is scoped to a job's
        diff so a huge diff is harder to meaningfully review. Use depends_on to
        chain jobs that touch the same files so they run sequentially instead of
        contending.

        Set fixes_job_id to the incident job this job remediates. If another
        non-terminal remediation is already active for that incident, the job
        is rejected with a conflict error instead of being created; otherwise
        this job is registered as the new active remediation for that incident.
        Set allow_parallel_remediation=True to bypass that conflict check for the
        rare intentional case where two remediations should run concurrently —
        this is recorded on the created job's source_meta for auditability.

        Set idempotency_key to a caller-chosen, per-project-unique string to make
        this call safely retryable on any cadence (not just back-to-back retries):
        calling create_job again with the same project_id + idempotency_key
        returns the job already created for that key instead of creating a
        duplicate. Omit it and nothing changes.

        Returns {"job": ..., "auto_epic": ...}, or the same shape plus
        "deduplicated": true when a matching job was already created — either
        because idempotency_key was already used in this project, or (when it
        was omitted) because an identical job was created in the last 10
        seconds. Returns an error string on failure.
        """
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        return await _create_job_impl(
            store,
            ctx,
            idea=idea,
            project_id=project_id,
            title=title,
            epic_id=epic_id,
            depends_on=depends_on,
            fixes_job_id=fixes_job_id,
            allow_parallel_remediation=allow_parallel_remediation,
            idempotency_key=idempotency_key,
        )

    @mcp.tool()
    async def create_job_wave(
        project_id: int,
        jobs: list[dict],
        epic_id: int | None = None,
    ) -> dict | str:
        """Atomically create a wave of interdependent jobs. Caller must be a
        member of the project.

        Each item in ``jobs`` is ``{title, idea, depends_on, target_files,
        priority, epic_id, idempotency_key}`` (only ``title`` and ``idea`` are
        required). ``depends_on`` is a list of 0-based indexes into this same
        ``jobs`` list — not job ids — e.g. ``depends_on: [0]`` means "runs after
        the first job declared in this wave". Jobs that share a ``target_files``
        entry are auto-chained in declaration order even without an explicit
        ``depends_on``. The whole wave is validated for dependency cycles and
        out-of-range indexes and inserted in one atomic transaction: either
        every job is created, or none are.

        Set a per-job ``idempotency_key`` to make individual entries in the wave
        safely retryable: when an entry's key was already used by an existing
        job in this project, that existing job is returned for that entry
        instead of creating a duplicate, while the rest of the wave is created/
        linked normally. Omit it and nothing changes.

        Set ``epic_id`` to assign every job in the wave to the same epic;
        omit it to let each job resolve its own epic the same way
        ``create_job`` does (or set a per-job ``epic_id`` in its dict).

        Returns ``{"jobs": [{"id", "title", "status", "depends_on"}, ...]}``
        where each job's ``depends_on`` lists the real, persisted job ids it
        depends on — including any hot-file auto-chained edges beyond what
        the caller declared. Returns an error string on failure, with zero
        jobs created.
        """
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        return await _create_job_wave_impl(
            store, ctx, project_id=project_id, jobs=jobs, epic_id=epic_id
        )

    @mcp.tool()
    async def cancel_job(job_id: int) -> dict | str:
        """Cancel a pending or running job. Caller must be a member of the job's project."""
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        return await _cancel_job_impl(store, ctx, job_id)

    @mcp.tool()
    async def retry_job(job_id: int, force: bool = False) -> dict | str:
        """Retry a FAILED or CANCELLED job, resetting it to re-run from PLAN.

        Caller must be a member of the job's project. Set ``force=True`` to bypass
        the PLAN-stage scope gate (which fails jobs whose plan has too many stories
        or target_files) when you've confirmed the job's size is legitimate — this
        persists as ``source_meta.scope_gate_bypass`` so it survives the re-plan.
        """
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        return await _retry_job_impl(store, config, ctx, job_id, force=force)

    @mcp.tool()
    async def requeue_job(job_id: int, stage: str) -> dict | str:
        """Requeue a FAILED job to re-run from a specific stage.

        The job must currently be in the FAILED status, and ``stage`` must be
        one of ``incident_analyst.REQUEUEABLE_STAGES`` (queued, lint, build,
        test, review, security, deploy) — any other stage is rejected.
        Caller must hold ``resolve_job`` for the job's project.
        """
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        return await _requeue_job_impl(store, ctx, job_id, stage)

    @mcp.tool()
    async def resolve_job(job_id: int, resolution: str = "resolved") -> dict | str:
        """Mark a FAILED or CANCELLED job as manually resolved.

        ``resolution`` must be one of ``_VALID_JOB_RESOLUTIONS`` (currently just
        ``resolved``); the job must currently be in the FAILED or CANCELLED
        status. Caller must hold ``resolve_job`` for the job's project.
        """
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        return await _resolve_job_impl(store, ctx, job_id, resolution)

    @mcp.tool()
    async def fix_forward_job(
        job_id: int,
        idea: str,
        title: str = "",
        repoint_dependent_ids: list[int] | None = None,
        override_active_remediation: bool = False,
    ) -> dict | str:
        """File a new remediation job linked to ``job_id`` and mark it active.

        Caller must hold ``resolve_job`` for the job's project. ``idea`` is
        required. If ``job_id`` already has a non-terminal active remediation,
        the call is rejected with the conflicting job's id unless
        ``override_active_remediation=True``. When ``repoint_dependent_ids`` is
        given, those jobs' dependency on ``job_id`` is atomically repointed to
        the new remediation job instead of just linking it as the active fix.
        """
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        return await _fix_forward_job_impl(
            store,
            ctx,
            job_id,
            idea=idea,
            title=title,
            repoint_dependent_ids=repoint_dependent_ids,
            override_active_remediation=override_active_remediation,
        )

    @mcp.tool()
    async def update_job(
        job_id: int,
        title: str | None = None,
        idea: str | None = None,
        priority: int | None = None,
        epic_id: int | None = None,
    ) -> dict | str:
        """Atomically update a job's title, idea, priority, and/or epic.

        At least one of title/idea/priority/epic_id must be supplied. Any
        parameter left as None (the default) is not changed — there is no way
        to explicitly null out epic_id through this tool, mirroring
        update_project's max_fix_attempts handling. epic_id must belong to
        job_id's own project. Caller must hold edit_job_deps for the job's
        project.
        """
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        return await _update_job_impl(
            store, ctx, job_id, title=title, idea=idea, priority=priority, epic_id=epic_id
        )

    @mcp.tool()
    async def archive_job(job_id: int) -> dict | str:
        """Archive a job, tearing down its git/GitHub artifacts if it is terminal.

        Caller must be a member of the job's project.
        """
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        return await _archive_job_impl(store, config, ctx, job_id)

    @mcp.tool()
    async def unarchive_job(job_id: int) -> dict | str:
        """Unarchive a job, restoring it to normal visibility.

        Caller must be a member of the job's project.
        """
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        return await _unarchive_job_impl(store, ctx, job_id)

    @mcp.tool()
    async def add_job_dependency(job_id: int, depends_on_job_id: int) -> dict | str:
        """Add a dependency so job_id waits on depends_on_job_id before running.

        Caller must hold edit_job_deps for the job's project. Returns
        {"depends_on": [...]}, mirroring the REST dependency endpoints.
        """
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        return await _add_job_dependency_impl(store, ctx, job_id, depends_on_job_id)

    @mcp.tool()
    async def remove_job_dependency(job_id: int, depends_on_job_id: int) -> dict | str:
        """Remove a dependency so job_id no longer waits on depends_on_job_id.

        Caller must hold edit_job_deps for the job's project. Returns
        {"depends_on": [...]}, mirroring the REST dependency endpoints.
        """
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        return await _remove_job_dependency_impl(store, ctx, job_id, depends_on_job_id)

    @mcp.tool()
    async def get_job_dependency_graph(job_id: int) -> dict | str:
        """Return the full graph even after upstream completion; caller must be a member."""
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        return await _get_job_dependency_graph_impl(store, ctx, job_id)

    @mcp.tool()
    async def create_epic(project_id: int, name: str, description: str = "") -> dict | str:
        """Create a new epic in a project. Caller must be a project member."""
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        if not is_project_member(store, ctx, project_id):
            return "error: forbidden — you are not a member of this project"
        name = name.strip()
        if not name:
            return "error: name is required"
        try:
            epic = store.create_epic(project_id, name, description)
        except ValueError as exc:
            return f"error: {exc}"
        return epic_to_dict(epic)

    @mcp.tool()
    async def update_epic(
        epic_id: int,
        name: str | None = None,
        description: str | None = None,
        status: str | None = None,
        archived: bool | None = None,
    ) -> dict | str:
        """Update an existing epic's name, description, status, and/or archived flag.

        Caller must be a member of the epic's project. status must be one of
        active, paused, done, archived.
        """
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        epic = store.get_epic(epic_id)
        if epic is None:
            return "error: epic not found"
        if not is_project_member(store, ctx, epic.project_id):
            return "error: forbidden — you are not a member of this project"
        if status is not None and status not in _VALID_EPIC_STATUSES:
            return f"error: status must be one of {sorted(_VALID_EPIC_STATUSES)}"
        updated = store.update_epic(epic_id, name=name, description=description, status=status)
        if archived is not None:
            store.set_archived_epic(epic_id, archived)
            updated = store.get_epic(epic_id)
        return epic_to_dict(updated)

    @mcp.tool()
    async def add_backlog_item(
        project_id: int,
        title: str,
        body: str = "",
        type: str = "idea",
        epic_hint: str | None = None,
    ) -> dict | str:
        """Add a backlog item to a project's backlog.

        Caller must be a project member with the propose_backlog permission.
        """
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        if not is_project_member(store, ctx, project_id):
            return "error: forbidden — you are not a member of this project"
        if not check_permission(store, ctx, "propose_backlog", project_id=project_id):
            return "error: forbidden — you are not a member of this project"
        title = title.strip()
        if not title:
            return "error: title is required"
        valid_types = {t.value for t in BacklogItemType}
        if type not in valid_types:
            return f"error: type must be one of {sorted(valid_types)}"
        proposed_by = ctx.user_email or ""
        item = store.create_backlog_item(
            project_id, title, body.strip(), type, proposed_by, epic_hint=epic_hint
        )
        return backlog_item_to_dict(item)

    @mcp.tool()
    async def register_webhook(
        project_id: int, url: str, event_type: str, kind: str = "http"
    ) -> dict | str:
        """Register a webhook subscription for a project's job-complete or deploy events.

        Caller must be a project member with the manage_webhooks permission.
        event_type must be one of job_complete, deploy, needs_attention.
        kind is either "http" (url must be an http(s) URL) or "slack" (url must
        be a Slack channel ID like C0123456).
        """
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        if not is_project_member(store, ctx, project_id):
            return "error: forbidden — you are not a member of this project"
        if not check_permission(store, ctx, "manage_webhooks", project_id=project_id):
            return "error: forbidden — you are not a member of this project"
        url = url.strip()
        if kind == "slack":
            if not _SLACK_CHANNEL_ID_RE.match(url):
                return "error: for kind=slack, url must be a Slack channel ID like C0123456"
        else:
            if not url.startswith("http://") and not url.startswith("https://"):
                return "error: url must start with http:// or https://"
        if event_type not in _VALID_WEBHOOK_EVENTS:
            return f"error: event_type must be one of {sorted(_VALID_WEBHOOK_EVENTS)}"
        try:
            webhook = store.create_webhook(project_id, url, event_type, ctx.user_email or "", kind)
        except ValueError as exc:
            return f"error: {exc}"
        return webhook_to_dict(webhook)

    @mcp.tool()
    async def list_webhooks(project_id: int) -> list[dict] | str:
        """List all webhooks registered for a project. Caller must be a project member."""
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        if not is_project_member(store, ctx, project_id):
            return "error: forbidden — you are not a member of this project"
        return [webhook_to_dict(w) for w in store.list_webhooks(project_id)]

    @mcp.tool()
    async def set_webhook_active(webhook_id: int, active: bool) -> dict | str:
        """Enable or disable a webhook. Caller must be a project member with manage_webhooks."""
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        webhook = store.get_webhook(webhook_id)
        if webhook is None:
            return "error: webhook not found"
        if not is_project_member(store, ctx, webhook.project_id):
            return "error: forbidden — you are not a member of this project"
        if not check_permission(store, ctx, "manage_webhooks", project_id=webhook.project_id):
            return "error: forbidden — you are not a member of this project"
        updated = store.set_webhook_active(webhook_id, active)
        return webhook_to_dict(updated)

    @mcp.tool()
    async def delete_webhook(webhook_id: int) -> dict | str:
        """Delete a webhook. Caller must be a project member with manage_webhooks."""
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        webhook = store.get_webhook(webhook_id)
        if webhook is None:
            return "error: webhook not found"
        if not is_project_member(store, ctx, webhook.project_id):
            return "error: forbidden — you are not a member of this project"
        if not check_permission(store, ctx, "manage_webhooks", project_id=webhook.project_id):
            return "error: forbidden — you are not a member of this project"
        store.delete_webhook(webhook_id)
        return {"deleted": True, "id": webhook_id}

    @mcp.tool()
    async def set_slack_credential(project_id: int, bot_token: str) -> dict | str:
        """Store the Slack bot token used to send notifications for a project.

        Caller must be a project member with the manage_webhooks permission.
        The plaintext token is never echoed back or persisted in plaintext.
        """
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        if not is_project_member(store, ctx, project_id):
            return "error: forbidden — you are not a member of this project"
        if not check_permission(store, ctx, "manage_webhooks", project_id=project_id):
            return "error: forbidden — you are not a member of this project"
        store.set_project_slack_token(project_id, bot_token, ctx.user_email or "")
        return {"project_id": project_id, "configured": True}

    @mcp.tool()
    async def promote_release(project_id: int, release_id: int, target_env_id: int) -> dict | str:
        """Promote a release to an environment, dispatching a DEPLOY-stage job.

        Caller must be a project member with the deploy.promote permission.
        release_id and target_env_id must both belong to project_id.

        Returns {"promotion": ..., "job": ...} with the created promotion ledger
        entry (state="dispatched") and its enqueued job. Returns
        {"promotion": None, "noop": true} instead, creating nothing, when
        release_id already equals the target environment's current release.
        Returns an error string on failure.
        """
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        return await _promote_release_impl(
            store,
            ctx,
            project_id=project_id,
            release_id=release_id,
            target_env_id=target_env_id,
        )

    @mcp.tool()
    async def offer_release(project_id: int, release_id: int, target_env_id: int) -> dict | str:
        """Make a release available for a client environment's own approver to
        apply — no deploy job is enqueued here.

        Caller must be a project member with the deploy.offer permission.
        release_id and target_env_id must both belong to project_id. The
        client's approver runs `hyqs-pipeline apply` on their own host to
        pull, verify, and cut over to the release; secret VALUES never cross
        to us.

        Returns {"promotion": ...} with the created promotion ledger entry
        (state="available"). Returns {"promotion": None, "noop": true}
        instead, creating nothing, when release_id already equals the target
        environment's current release. Returns an error string on failure.
        """
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        return await _offer_release_impl(
            store,
            ctx,
            project_id=project_id,
            release_id=release_id,
            target_env_id=target_env_id,
        )

    @mcp.tool()
    async def get_project_spec(project_id: int) -> dict | str:
        """Return a project's config and agent roster. Caller must be a project member."""
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        if not is_project_member(store, ctx, project_id):
            return "error: forbidden — you are not a member of this project"
        project = store.get_project(project_id)
        if project is None:
            return "error: project not found"
        return {
            "project": project_to_dict(project),
            "agents": [agent_to_dict(a) for a in store.list_agents(project_id)],
        }

    @mcp.tool()
    async def update_project(
        project_id: int,
        name: str | None = None,
        description: str | None = None,
        status: str | None = None,
        stack: str | None = None,
        deploy_config: str | None = None,
        github_url: str | None = None,
        max_fix_attempts: int | None = None,
    ) -> dict | str:
        """Update a project's config. Caller must have the edit_project permission.

        status must be one of active, paused, archived. max_fix_attempts must be
        a positive integer when provided.
        """
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        if not check_permission(store, ctx, "edit_project", project_id=project_id):
            return "error: forbidden — you are not a member of this project"
        if status is not None and status not in _VALID_PROJECT_STATUSES:
            return f"error: status must be one of {sorted(_VALID_PROJECT_STATUSES)}"
        if max_fix_attempts is not None and max_fix_attempts < 1:
            return "error: max_fix_attempts must be a positive integer"
        updated = store.update_project(
            project_id,
            name=name,
            description=description,
            status=status,
            stack=stack,
            deploy_config=deploy_config,
            github_url=github_url,
            max_fix_attempts=(max_fix_attempts if max_fix_attempts is not None else _UNSET),
        )
        if updated is None:
            return "error: project not found"
        return project_to_dict(updated)

    @mcp.tool()
    async def resync_nginx_vhost(project_id: int) -> dict | str:
        """Re-apply a project's nginx vhost from its current deploy_config.

        Idempotent: rewrites the vhost conf with the port + domains
        (the base domain + any extra_domains) implied by deploy_config, safe to
        call repeatedly. Caller must have the edit_project permission.
        """
        request = get_http_request()
        ctx: AuthContext = request.state.auth_context
        if not check_permission(store, ctx, "edit_project", project_id=project_id):
            return "error: forbidden — you are not a member of this project"
        project = store.get_project(project_id)
        if project is None:
            return "error: project not found"
        try:
            deploy_cfg = json.loads(project.deploy_config) if project.deploy_config else {}
        except (TypeError, ValueError):
            deploy_cfg = {}
        if not isinstance(deploy_cfg, dict):
            deploy_cfg = {}
        port = deploy_cfg.get("port")
        if not isinstance(port, int):
            return "error: project deploy_config has no valid port"
        slug = slugify(Path(project.repo_path).name)
        try:
            domains = nginx_sites.resolve_domains(deploy_cfg)
            await nginx_sites.register_site(slug, port, domains=domains)
        except Exception as exc:
            return f"error: nginx resync failed: {exc}"
        return {"slug": slug, "port": port, "domains": domains}

    return mcp
