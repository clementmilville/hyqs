"""Starlette JSON + SSE API and SPA host.

``/api/*`` is guarded by a bearer token: header ``Authorization: Bearer <token>``,
the ``hyqs_session`` cookie set by the OAuth callback, or ``?token=`` on the GET
SSE routes only (EventSource cannot send an Authorization header).
"""

from __future__ import annotations

import asyncio
import collections
import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import socket
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import maxminddb
import uvicorn
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp

from hyqs.core import Orchestrator
from hyqs.intake.agent import interview_turn, stream_interview_turn
from hyqs.intake.gate import check_completeness
from hyqs.intake.models import IntakeSession
from hyqs.pipeline import (
    Job,
    JobStore,
    agents,
    decisions,
    github,
    gitops,
    incident_analyst,
    remediation,
    resolve_current_executor,
)
from hyqs.pipeline import deploy as _deploy
from hyqs.pipeline import docker_deploy as _docker_deploy
from hyqs.pipeline import nginx_sites as _nginx_sites
from hyqs.pipeline.contracts import ResultBlockError, parse_result_block
from hyqs.pipeline.decisions import load_digest
from hyqs.pipeline.impl_summary import generate_range_summary
from hyqs.pipeline.intake_seed import plan_seed_from_spec, seed_project_from_spec
from hyqs.pipeline.models import (
    ALL_TASKS,
    AgentSpec,
    ApiToken,
    BacklogItem,
    BacklogItemStatus,
    BacklogItemType,
    ChatSession,
    Epic,
    JobSource,
    JobStatus,
    Project,
    ProjectMember,
    Promotion,
    ResourceUsage,
    SchedulerWait,
    Stage,
    Webhook,
    _now,
)
from hyqs.pipeline.providers import Role, build_backend, known_providers
from hyqs.pipeline.provision import provision_project
from hyqs.pipeline.store import set_db_actor
from hyqs.pipeline.supervisor import FailureClass, classify_failure
from hyqs.web.auth import AuthContext, check_permission, is_project_member, resolve_auth
from hyqs.web.chat_tools import build_project_tools
from hyqs.web.epic_suggest import suggest_best_epic
from hyqs.web.oauth import SESSION_COOKIE_NAME, build_oauth_routes

log = logging.getLogger("hyqs.web")

# Dedicated conversation id recorded as jobs.chat_id for UI-created jobs
# (job provenance FK — unrelated to the orchestrator's per-caller session id).
WEB_CHAT_ID = 1

# Reserved orchestrator session id for the raw HYQS_WEB_TOKEN bypass path,
# which has no backing users.id row. users.id is a positive serial, so this
# never collides with a real user's session.
_PLATFORM_TOKEN_SESSION_ID = 0

ASSIGNABLE_PLATFORM_PERMISSIONS = frozenset(
    {
        "manage_users",
        "manage_invitations",
        "manage_roles",
        "manage_providers",
        "view_fleet",
        "create_project",
    }
)
FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"

# Absolute path to this repo's root checkout — used to detect self-repo chat.
_SELF_REPO = Path(__file__).resolve().parents[2]


def _is_self_repo(path: str | Path) -> bool:
    return Path(path).resolve() == _SELF_REPO


def project_to_dict(project: Project) -> dict:
    return {
        "id": project.id,
        "name": project.name,
        "repo_path": project.repo_path,
        "description": project.description,
        "status": project.status,
        "max_fix_attempts": project.max_fix_attempts,
        "stack": project.stack,
        "spec": project.spec,
        "deploy_config": project.deploy_config,
        "github_url": project.github_url,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


def epic_to_dict(epic: Epic) -> dict:
    return {
        "id": epic.id,
        "project_id": epic.project_id,
        "name": epic.name,
        "description": epic.description,
        "status": epic.status,
        "archived": epic.archived,
        "created_at": epic.created_at,
        "updated_at": epic.updated_at,
    }


def agent_to_dict(agent: AgentSpec) -> dict:
    return {
        "id": agent.id,
        "project_id": agent.project_id,
        "name": agent.name,
        "provider": agent.provider,
        "model": agent.model,
        "allowed_tasks": agent.allowed_tasks,
        "max_concurrency": agent.max_concurrency,
        "enabled": agent.enabled,
        "created_at": agent.created_at,
        "updated_at": agent.updated_at,
    }


def member_to_dict(member: ProjectMember) -> dict:
    return {
        "id": member.id,
        "user_id": member.user_id,
        "project_id": member.project_id,
        "role": member.role,
        "created_at": member.created_at,
    }


def api_token_to_dict(token: ApiToken) -> dict:
    """Never include token_hash or the plaintext secret."""
    return {
        "id": token.id,
        "project_id": token.project_id,
        "name": token.name,
        "role": token.role,
        "last4": token.last4,
        "created_by": token.created_by,
        "created_at": token.created_at,
        "last_used_at": token.last_used_at,
        "revoked_at": token.revoked_at,
    }


def resource_usage_to_dict(r: ResourceUsage) -> dict:
    return {
        "id": r.id,
        "job_id": r.job_id,
        "stage": r.stage,
        "attempt": r.attempt,
        "cpu_seconds": r.cpu_seconds,
        "peak_rss_bytes": r.peak_rss_bytes,
        "io_read_bytes": r.io_read_bytes,
        "io_write_bytes": r.io_write_bytes,
        "wall_seconds": r.wall_seconds,
        "sampled_at": r.sampled_at,
        "net_bytes": r.net_bytes,
        "net_bytes_approx": r.net_bytes_approx,
        "disk_bytes": r.disk_bytes,
    }


def webhook_to_dict(webhook: Webhook) -> dict:
    return {
        "id": webhook.id,
        "project_id": webhook.project_id,
        "url": webhook.url,
        "event_type": webhook.event_type,
        "created_by": webhook.created_by,
        "active": webhook.active,
        "created_at": webhook.created_at,
        "kind": webhook.kind,
    }


def promotion_to_dict(promotion: Promotion) -> dict:
    return {
        "id": promotion.id,
        "project_id": promotion.project_id,
        "release_id": promotion.release_id,
        "source_env_id": promotion.source_env_id,
        "target_env_id": promotion.target_env_id,
        "kind": promotion.kind.value,
        "state": promotion.state.value,
        "requested_by": promotion.requested_by,
        "requested_at": promotion.requested_at,
        "approved_by": promotion.approved_by,
        "applied_by": promotion.applied_by,
        "applied_at": promotion.applied_at,
        "deploy_job_id": promotion.deploy_job_id,
        "created_at": promotion.created_at,
        "updated_at": promotion.updated_at,
    }


_VALID_WEBHOOK_EVENTS = frozenset({"job_complete", "deploy", "needs_attention"})
_SLACK_CHANNEL_ID_RE = re.compile(r"^[CG][A-Z0-9]{6,}$")


def _validate_webhook_config(url: str, event_type: str, kind: str) -> str | None:
    """Return an error string if the config is invalid, else None."""
    url = url.strip()
    if not url:
        return "url is required"
    if kind == "slack":
        if not _SLACK_CHANNEL_ID_RE.match(url):
            return "for kind=slack, url must be a Slack channel ID like C0123456"
    else:
        if not url.startswith("http://") and not url.startswith("https://"):
            return "url must start with http:// or https://"
    if event_type not in _VALID_WEBHOOK_EVENTS:
        return f"event_type must be one of {sorted(_VALID_WEBHOOK_EVENTS)}"
    return None


def backlog_item_to_dict(item: BacklogItem) -> dict:
    return {
        "id": item.id,
        "project_id": item.project_id,
        "title": item.title,
        "body": item.body,
        "type": item.type.value,
        "proposed_by": item.proposed_by,
        "status": item.status.value,
        "votes": item.votes,
        "epic_hint": item.epic_hint,
        "linked_job_ids": item.linked_job_ids,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def job_to_dict(
    job: Job,
    waiting_on: list[int] | None = None,
    scheduler_wait: SchedulerWait | None = None,
    effective_priority: int | None = None,
    priority_reasons: list[dict] | None = None,
) -> dict:
    current_executor = resolve_current_executor(job)
    return {
        "id": job.id,
        "idea": job.idea,
        "title": job.title,
        "repo_path": job.repo_path,
        "project_id": job.project_id,
        "epic_id": job.epic_id,
        "branch": job.branch,
        "stage": job.stage.value,
        "status": job.status.value,
        "plan": job.plan,
        "review": job.review,
        "error": job.error,
        "failure": job.failure,
        "resolution": job.resolution,
        "failed_step": job.failed_step,
        "failure_code": job.failure_code,
        "failure_origin": job.failure_origin,
        "retry_disposition": job.retry_disposition,
        "failure_detail": job.failure_detail,
        "archived": job.archived,
        "needs_split": job.needs_split,
        "priority": job.priority,
        "effective_priority": (
            effective_priority if effective_priority is not None else job.priority
        ),
        "priority_reasons": priority_reasons if priority_reasons is not None else [],
        "agent_id": job.agent_id,
        "provider": job.provider,
        "current_executor": (
            {
                "kind": current_executor.kind,
                "label": current_executor.label,
                "agent_id": current_executor.agent_id,
                "provider": current_executor.provider,
            }
            if current_executor is not None
            else None
        ),
        "waiting_on": waiting_on if waiting_on is not None else [],
        "scheduler_wait": scheduler_wait.to_dict() if scheduler_wait is not None else None,
        "source": job.source.value,
        "source_actor": job.source_actor,
        "source_meta": job.source_meta or {},
        "implementation_summary": job.implementation_summary,
        "merge_delta_sha": job.merge_delta_sha,
        "deployed_commit": job.deployed_commit,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def job_to_summary_dict(
    job: Job,
    waiting_on: list[int] | None = None,
    scheduler_wait: SchedulerWait | None = None,
    effective_priority: int | None = None,
    priority_reasons: list[dict] | None = None,
) -> dict:
    return {
        "id": job.id,
        "title": job.title,
        "status": job.status.value,
        "stage": job.stage.value,
        "epic_id": job.epic_id,
        "project_id": job.project_id,
        "waiting_on": waiting_on if waiting_on is not None else [],
        "scheduler_wait": scheduler_wait.to_dict() if scheduler_wait is not None else None,
        "branch": job.branch,
        "archived": job.archived,
        "priority": job.priority,
        "effective_priority": (
            effective_priority if effective_priority is not None else job.priority
        ),
        "priority_reasons": priority_reasons if priority_reasons is not None else [],
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "has_error": bool(job.error or job.failure),
        "failure_code": job.failure_code,
    }


def job_projection_extras(store: JobStore, job: Job) -> dict:
    """The waiting_on + scheduler_wait + effective-priority diagnostics shared by every job projection."""
    waiting_on = store.get_unsatisfied_deps(job.id)
    scheduler_wait = store.get_scheduler_wait(job, waiting_on)
    effective_priority, priority_reasons = store.get_effective_priority(job)
    return {
        "waiting_on": waiting_on,
        "scheduler_wait": scheduler_wait,
        "effective_priority": effective_priority,
        "priority_reasons": priority_reasons,
    }


def intake_session_to_dict(session: IntakeSession) -> dict:
    return {
        "id": session.id,
        "session_id": session.session_id,
        "created_by": session.created_by,
        "draft_spec": session.draft_spec,
        "messages": session.messages,
        "status": session.status,
        "project_id": session.project_id,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "missing_required": check_completeness(session.draft_spec),
    }


# GET SSE routes: EventSource cannot set an Authorization header, so these
# alone may still authenticate via ?token=. Every other route requires the
# bearer header or the session cookie.
_SSE_QUERY_TOKEN_PATHS = re.compile(
    r"^/api/(jobs/stream|jobs/\d+/stream|jobs/\d+/logs/stream|workers/stream|supervisor/stream)$"
)


class _RequestBodyTooLarge(Exception):
    """Raised mid-stream once a chunked body's running size exceeds the cap."""


class MaxRequestBodySize(BaseHTTPMiddleware):
    """Reject any request whose body exceeds ``max_bytes`` with a 413.

    A declared ``Content-Length`` over the cap is rejected before the body is
    read at all. A chunked body with no ``Content-Length`` is capped by
    counting bytes as they stream through ``receive()``, since Starlette/ASGI
    give no other hook to bound an unbounded upload before it is buffered.
    """

    def __init__(self, app, max_bytes: int) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = None
            if declared is not None and declared > self.max_bytes:
                return JSONResponse({"error": "payload too large"}, status_code=413)

        max_bytes = self.max_bytes
        received = 0
        original_receive = request._receive

        async def _limited_receive():
            nonlocal received
            message = await original_receive()
            if message["type"] == "http.request":
                received += len(message.get("body") or b"")
                if received > max_bytes:
                    raise _RequestBodyTooLarge()
            return message

        request._receive = _limited_receive

        try:
            return await call_next(request)
        except _RequestBodyTooLarge:
            return JSONResponse({"error": "payload too large"}, status_code=413)


class TokenAuth(BaseHTTPMiddleware):
    def __init__(self, app, token: str, store) -> None:
        super().__init__(app)
        self.token = token
        self.store = store

    async def dispatch(self, request: Request, call_next):
        request.state.store = self.store
        path = request.url.path
        _UNAUTHED_PATHS = frozenset(
            {
                "/api/health",
                "/api/auth/login",
                "/api/auth/logout",
                "/api/auth/providers",
                "/api/auth/oauth/google/redirect",
                "/api/auth/oauth/google/callback",
                "/api/auth/oauth/apple/redirect",
                "/api/auth/oauth/apple/callback",
                "/api/public/page-view",
                "/api/public/site-stats",
            }
        )
        if not (path.startswith("/api/") and path not in _UNAUTHED_PATHS):
            request.state.user_id = None
            request.state.user_email = None
            request.state.user_display_name = None
            request.state.is_platform_admin = False
            request.state.token_project_id = None
            request.state.token_role = None
            request.state.api_token_id = None
            set_db_actor("web:anon")
            return await call_next(request)

        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            supplied = header[7:]
        elif _SSE_QUERY_TOKEN_PATHS.match(path):
            supplied = request.query_params.get("token", "")
        else:
            supplied = request.cookies.get(SESSION_COOKIE_NAME, "")

        ctx = resolve_auth(self.store, supplied, self.token)
        if ctx is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        request.state.user_id = ctx.user_id
        request.state.user_email = ctx.user_email
        request.state.user_display_name = ctx.display_name
        request.state.is_platform_admin = ctx.is_platform_admin
        request.state.token_project_id = ctx.token_project_id
        request.state.token_role = ctx.token_role
        request.state.api_token_id = ctx.api_token_id
        # Audit attribution: every DB write in this request is stamped with the
        # caller (ContextVar — isolated per request task).
        if ctx.api_token_id is not None:
            set_db_actor(f"apitoken:{ctx.api_token_id}")
        else:
            set_db_actor(f"user:{ctx.user_email}" if ctx.user_email else "web:token")
        return await call_next(request)


class SecurityHeaders(BaseHTTPMiddleware):
    """Stamp baseline security response headers on every response.

    Registered outermost (added last — see the comment at its
    ``app.add_middleware`` call in ``build_app``) so these headers land on
    every response, including ``TokenAuth``'s 401s and
    ``MaxRequestBodySize``'s 413s, plus SSE streams and the SPA/static
    mount. The CSP is Report-Only: the bundled SPA and the public
    /how-it-works pages carry inline scripts/styles that an enforcing
    policy would break. Promoting it to enforcing is separate follow-up
    work that requires actually loading the app and the public pages and
    checking for violations first.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy-Report-Only"] = "default-src 'self'"
        return response


_SUGGEST_SYS = """\
You are a product planning assistant. Given an epic name, description, and project context,
suggest 3-7 concrete, implementable sub-features to build as part of this epic.
You may inspect the repository to understand the codebase if helpful.
End your reply with a single fenced ```json block matching exactly:
{"suggestions": [{"title": str, "description": str}]}
Keep titles short (8 words max). Descriptions should be one sentence."""

JOB_CHAT_SYS = """\
You are a senior software architect helping a developer plan work for their project.

HARD CONSTRAINTS — read before anything else:
1. You CANNOT create, queue, or file jobs. You have no tools to do so. Jobs are only created
   when the USER clicks the "Create N jobs" button that appears below your proposals.
2. Never claim to have created, queued, or filed jobs. Never write phrases like
   "Creating jobs now", "All jobs queued", "I've filed those", or similar.
3. Never reference tools that do not exist in this context (e.g. TaskCreate).
4. When the user asks you to "queue", "create", "file", or "submit" jobs, you must
   RE-EMIT the full current set of proposed jobs in the sentinel block below (never an
   empty list) and explicitly tell the user to click the "Create N jobs" button to proceed.

== MODES ==

Select exactly one mode per turn based on the user's message:

DIAGNOSE mode — use when the user reports a symptom, bug, or broken behaviour:
1. Inspect the repository to locate the root cause (use the symbol index and file tools).
2. State the root cause clearly with at least one file:line citation (e.g. `app.py:263`).
3. Summarise the evidence before proposing any jobs.
4. Propose fix jobs, each carrying ≥2 meaningfully different alternatives (different
   approaches, not just rewordings).

FEATURE mode — use when the user requests new capability:
1. Briefly state the key tradeoffs between approaches (scope, complexity, user impact).
2. Then propose implementation jobs, each with ≥2 meaningfully different alternatives.

== ALTERNATIVES ==

Every proposed job MUST include an `alternatives` array with at least 2 entries.
Each alternative is a completely different way to satisfy the same underlying need —
different in scope, architecture, or approach, not just rephrased titles.
Format each alternative with `title`, `description`, and `acceptance_criteria`.
The top-level job fields (title, description, acceptance_criteria) represent the
recommended option; the alternatives array lists the other viable approaches.

== EPICS ==

Every proposed job MUST include either:
- `epic_id`: an integer id taken from the "Active epics" list provided in the prompt, OR
- `new_epic`: {"name": str, "description": str} to create a new epic for this job.
Never leave both null. Never invent an epic_id that is not in the Active epics list.

== DEPENDENCIES ==

`depends_on` contains 0-based indexes into the current jobs array for this batch.
Use it when a job cannot start until another job in the same proposal is done.
If a job has no dependencies, use an empty list [].

Engage with the user's ideas conversationally. Ask clarifying questions or provide analysis as needed.
When you are ready to propose concrete jobs, conclude your reply with a sentinel block in this exact format:

<<<RESULT_JSON>>>
{"jobs": [{"title": str, "description": str, "acceptance_criteria": str, "alternatives": [{"title": str, "description": str, "acceptance_criteria": str}], "epic_id": int|null, "new_epic": {"name": str, "description": str}|null, "depends_on": [int]}]}
<<<END_RESULT>>>

Rules:
- For analysis-only turns where no jobs are ready to propose yet, use an empty list: {"jobs": []}
- When the user asks to queue/create/file jobs: re-emit the full proposed list (non-empty) and remind them to click "Create N jobs"
- Propose at most 5 jobs per turn
- Job titles must be under 80 characters
- Each job should be independently implementable
- Every job must have ≥2 alternatives and a non-null epic_id or new_epic
- Omit the sentinel block only if you have not finished gathering information yet"""


BACKLOG_REFINE_SYS = """\
You are a senior software architect helping a team refine their product backlog into an actionable job set.

HARD CONSTRAINTS:
1. You CANNOT create, queue, or file jobs. Jobs are only created when the user confirms.
2. Never claim to have created jobs. Never write phrases like "Creating jobs now" or similar.
3. Deduplicate overlapping asks across the provided items into a minimal coherent set.
4. Sequence jobs with depends_on using 0-based indexes into the proposed jobs array.

== ITEM TYPE TO MODE MAPPING ==
- type 'bug' → DIAGNOSE mode: root-cause the symptom, propose targeted fix jobs.
- type 'feature', 'idea', or 'chore' → FEATURE mode: propose implementation jobs.

== ALTERNATIVES ==
Every proposed job MUST include an `alternatives` array with at least 2 entries.
Each alternative must differ meaningfully in scope, architecture, or approach.
Format each with `title`, `description`, and `acceptance_criteria`.
The top-level job fields represent the recommended option; alternatives list other viable approaches.

== EPICS ==
Every proposed job MUST include either:
- `epic_id`: an integer from the Active epics list, OR
- `new_epic`: {"name": str, "description": str} for a new epic.
Never leave both null. Never invent an epic_id not in the Active epics list.

== DEPENDENCIES ==
`depends_on` contains 0-based indexes into the proposed jobs array.
Use it when a job cannot start until another is complete. Use [] for no dependencies.

Analyze the backlog items, deduplicate overlapping asks, and propose a dependency-ordered job set.
Conclude with a fenced JSON block:

```json
{"jobs": [{"title": str, "description": str, "acceptance_criteria": str, "alternatives": [{"title": str, "description": str, "acceptance_criteria": str}], "epic_id": int|null, "new_epic": {"name": str, "description": str}|null, "depends_on": [int]}]}
```

Rules:
- Propose at most 8 jobs total
- Job titles must be under 80 characters
- Every job must have ≥2 alternatives and a non-null epic_id or new_epic
- Every job must have depends_on (use [] if no dependencies)"""


_DEFAULT_EPIC_NAME = "General"

_TERMINAL_BACKLOG_STATUSES = {
    BacklogItemStatus.DECLINED.value,
    BacklogItemStatus.COMPLETED.value,
    BacklogItemStatus.CONVERTED.value,
}


def _detect_batch_dependency_cycle(depends_on_by_index: list[list[int]]) -> None:
    """Raise ValueError if the batch's index-based depends_on graph has a cycle."""
    n = len(depends_on_by_index)
    adj: list[list[int]] = [[] for _ in range(n)]
    in_degree = [0] * n
    for i, deps in enumerate(depends_on_by_index):
        for dep in deps:
            if 0 <= dep < n:
                adj[dep].append(i)
                in_degree[i] += 1
    queue = [i for i in range(n) if in_degree[i] == 0]
    topo: list[int] = []
    while queue:
        v = queue.pop()
        topo.append(v)
        for u in adj[v]:
            in_degree[u] -= 1
            if in_degree[u] == 0:
                queue.append(u)
    if len(topo) != n:
        raise ValueError("dependency cycle detected in batch")


def _parse_batch_job_common_fields(item: dict, n: int) -> dict:
    """Validate/normalize one batch job's depends_on, target_files, priority."""
    depends_on_raw = item.get("depends_on")
    depends_on: list[int] = []
    if depends_on_raw is not None:
        if not isinstance(depends_on_raw, list) or not all(
            isinstance(x, int) for x in depends_on_raw
        ):
            raise ValueError("depends_on must be a list of integers")
        depends_on = depends_on_raw

    target_files_raw = item.get("target_files")
    target_files: list[str] = []
    if target_files_raw is not None:
        if not isinstance(target_files_raw, list):
            raise ValueError("target_files must be a list")
        target_files = [str(f) for f in target_files_raw]

    priority_raw = item.get("priority")
    priority = 0
    if priority_raw is not None:
        try:
            priority = int(priority_raw)
        except (TypeError, ValueError):
            raise ValueError("priority must be an integer") from None

    return {"depends_on": depends_on, "target_files": target_files, "priority": priority}


def _requeue_stage_error(job: Job, stage: str) -> tuple[int, str] | None:
    """Shared REST/MCP requeue-eligibility check. None means the job is eligible."""
    if job.status != JobStatus.FAILED:
        return 409, "job is not in a failed state"
    if stage not in incident_analyst.REQUEUEABLE_STAGES:
        return 400, f"stage {stage!r} is not requeueable"
    return None


_VALID_JOB_RESOLUTIONS = frozenset({"resolved"})


def _resolve_job_error(job: Job, resolution: str) -> tuple[int, str] | None:
    """Shared REST/MCP resolve-eligibility check. None means the job is eligible."""
    if resolution not in _VALID_JOB_RESOLUTIONS:
        return 400, f"resolution must be one of {sorted(_VALID_JOB_RESOLUTIONS)}"
    if job.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
        return 409, "job is not in a terminal state"
    return None


def _job_title_error(title: str) -> tuple[int, str] | None:
    """Shared REST/MCP job-title validation. None means the title is valid."""
    if not title:
        return 400, "title must be a non-empty string"
    if len(title) > 200:
        return 400, "title must be 200 characters or fewer"
    return None


def _job_idea_error(idea: str) -> tuple[int, str] | None:
    """Shared REST/MCP job-idea validation. None means the idea is valid."""
    if not idea:
        return 400, "idea must be a non-empty string"
    return None


def _job_priority_error(priority: object) -> tuple[int, str] | None:
    """Shared REST/MCP job-priority validation. None means the priority is valid."""
    if priority is None or not isinstance(priority, int):
        return 400, "priority must be an integer"
    return None


def _job_epic_error(store: JobStore, job: Job, epic_id: int | None) -> tuple[int, str] | None:
    """Shared REST/MCP job-epic validation. None means epic_id is valid for this job."""
    if epic_id is None:
        return None
    epic = store.get_epic(epic_id)
    if epic is None:
        return 404, "epic not found"
    if epic.project_id != job.project_id:
        return 400, "epic belongs to a different project"
    return None


def _active_remediation_conflict(
    store: JobStore, job_id: int, override: bool
) -> tuple[int, str] | None:
    """Shared REST/MCP active-remediation-conflict check.

    None means there is no blocking conflict — either ``override`` is set, or
    ``job_id`` has no non-terminal active remediation. Otherwise returns the
    conflicting job's id and a formatted reason.
    """
    if override:
        return None
    active_id = store.get_active_remediation(job_id)
    if active_id is None:
        return None
    active_job = store.get(active_id)
    if active_job is None or active_job.status in (
        JobStatus.DONE,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    ):
        return None
    return active_id, remediation.format_conflict_reason(job_id, active_id)


def _resolve_epic(store: JobStore, project_id: int, idea: str) -> tuple[int, bool, bool]:
    """Return (epic_id, auto_created, is_fallback). Never raises."""
    try:
        epics = store.list_epics(project_id=project_id, include_archived=False)
        suggestion = suggest_best_epic(idea, epics)
        if "epic_id" in suggestion:
            eid = int(suggestion["epic_id"])
            if any(e["id"] == eid for e in epics):
                return eid, False, False
        if "proposed_name" in suggestion and suggestion["proposed_name"]:
            new_epic = store.create_epic(project_id, suggestion["proposed_name"])
            return new_epic.id, True, False
        existing_general = next((e for e in epics if e["name"] == _DEFAULT_EPIC_NAME), None)
        if existing_general:
            return existing_general["id"], False, True
        fallback = store.create_epic(project_id, _DEFAULT_EPIC_NAME)
        return fallback.id, True, True
    except Exception:
        log.debug("_resolve_epic failed, attempting bare fallback", exc_info=True)
        try:
            epics = store.list_epics(project_id=project_id, include_archived=False)
            existing_general = next((e for e in epics if e["name"] == _DEFAULT_EPIC_NAME), None)
            if existing_general:
                return existing_general["id"], False, True
            fallback = store.create_epic(project_id, _DEFAULT_EPIC_NAME)
            return fallback.id, True, True
        except Exception:
            log.exception("_resolve_epic bare fallback also failed")
            raise


def _build_refine_prompt(items: list, epics: list[dict]) -> str:
    _type_labels = {"bug": "BUG", "feature": "FEATURE", "chore": "CHORE", "idea": "IDEA"}
    lines = ["## Backlog items to refine\n"]
    for item in items:
        label = _type_labels.get(item.type.value, item.type.value.upper())
        lines.append(f"[{label}] {item.title}")
        if item.body:
            lines.append(item.body)
        lines.append("")
    lines.append("## Active epics\n")
    if epics:
        for e in epics:
            lines.append(f"- id={e['id']}: {e['name']}")
    else:
        lines.append("(none — use new_epic for all proposed jobs)")
    return "\n".join(lines)


class SPAStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if path.startswith("assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response


_JOB_RE = re.compile(r"Job #(\d+)")


def _parse_changelog_commits(
    git_log_output: str, store: JobStore, decisions_by_job: dict | None = None
) -> list[dict]:
    """Turn ``git log --format=%H\x1f%s\x1f%an`` output into attributed change entries."""
    decisions_by_job = decisions_by_job or {}
    changes: list[dict] = []
    for line in git_log_output.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\x1f", 2)
        if len(parts) != 3:
            continue
        sha, subject, author = parts
        m = _JOB_RE.search(subject)
        if m:
            job_id = int(m.group(1))
            job = store.get(job_id)
            change = {
                "kind": "job",
                "job_id": job_id,
                "title": job.title if job else subject,
                "summary": job.implementation_summary if job else None,
            }
            decision = decisions_by_job.get(job_id)
            if decision:
                change["decision_filename"] = decision["filename"]
                change["decision_first_line"] = decision["first_line"]
            changes.append(change)
        else:
            changes.append({"kind": "commit", "sha": sha, "subject": subject, "author": author})
    return changes


def _fleet_snapshot(store: JobStore, config) -> dict:
    """Live worker fleet snapshot: workers, pauses, merge locks, and stats."""
    now = time.time()
    stale = float(getattr(config, "pipeline_lease_ttl", 90)) * 1.5
    workers = []
    for w in store.list_workers(now, stale):
        idea = ""
        job = None
        if w.get("job_id"):
            job = store.get(w["job_id"])
            idea = job.idea if job else ""
        busy = w["status"] == "busy" and w["alive"]
        workers.append(
            {
                "id": w["id"],
                "host": w["host"],
                "pid": w["pid"],
                "status": w["status"],
                "alive": w["alive"],
                "job_id": w["job_id"],
                "project_id": job.project_id if job else None,
                "idea": idea,
                "stage": w["stage"],
                "provider": w["provider"],
                "role": w.get("role", "worker"),
                "busy_for": max(0.0, now - (w["started_at"] or now)) if busy else 0.0,
                "last_seen_ago": max(0.0, now - (w["last_seen"] or now)),
            }
        )
    active = store.list_active(200)
    return {
        "workers": workers,
        "pauses": store.list_provider_pauses(now),
        "merge_locks": store.list_merge_locks(),
        "stats": {
            "workers": len(workers),
            "alive": sum(1 for w in workers if w["alive"]),
            "busy": sum(1 for w in workers if w["status"] == "busy" and w["alive"]),
            "running": sum(1 for j in active if j.status.value == "running"),
            "pending": sum(1 for j in active if j.status.value == "pending"),
        },
        "now": now,
    }


async def _add_prose_to_clusters(changes: list[dict], repo_path: str, backend) -> None:
    """Add a ``prose`` field to contiguous out-of-band commit clusters in-place."""
    i = 0
    while i < len(changes):
        if changes[i]["kind"] != "commit":
            i += 1
            continue
        j = i
        while j + 1 < len(changes) and changes[j + 1]["kind"] == "commit":
            j += 1
        newest_sha = changes[i]["sha"]
        oldest_sha = changes[j]["sha"]
        range_str = f"{oldest_sha}^..{newest_sha}"
        try:
            prose = await asyncio.wait_for(
                generate_range_summary(repo_path, range_str, backend=backend),
                15,
            )
            for k in range(i, j + 1):
                changes[k]["prose"] = prose
        except Exception:
            pass
        i = j + 1


async def _job_diff_payload(job: Job) -> dict:
    """Resolve a job's diff, falling back to its landed commit once the
    branch is gone (squash-merge deletes it). Never surfaces raw git
    stderr as if it were diff content."""
    base = await gitops.default_branch(job.repo_path)
    res = await gitops.git(job.repo_path, "diff", f"{base}...{job.branch}")
    if res.ok:
        return {"diff": res.stdout, "base": base, "branch": job.branch, "commit": None}
    commit = job.deployed_commit
    if not commit:
        return {
            "error": (
                f"diff unavailable: branch '{job.branch}' no longer exists "
                "and no landed commit is recorded for this job"
            )
        }
    commit_res = await gitops.git(job.repo_path, "diff", f"{commit}^..{commit}")
    if commit_res.ok:
        return {"diff": commit_res.stdout, "base": base, "branch": None, "commit": commit}
    stderr = (commit_res.stderr or "").strip()
    return {
        "error": (
            f"diff unavailable: branch '{job.branch}' is gone and the landed "
            f"commit {commit} could not be diffed: {stderr}"
        )
    }


# --- public page-view beacon helpers --------------------------------------

_PAGE_VIEW_MAX_BODY_BYTES = 2048
_PAGE_VIEW_RATE_LIMIT_WINDOW_SECONDS = 60.0
_PAGE_VIEW_RATE_LIMIT_MAX_PER_WINDOW = 30
# Upper bound on distinct IPs tracked at once. Without this, a caller that
# rotates its (attacker-controlled) X-Forwarded-For value on every request
# would grow the rate-limit dict without bound (memory-exhaustion DoS).
_PAGE_VIEW_RATE_LIMIT_MAX_TRACKED_IPS = 5000
_PAGE_VIEW_BOT_UA_MARKERS = (
    "bot",
    "crawler",
    "spider",
    "curl",
    "wget",
    "python-requests",
    "headless",
)

# ip -> accepted-beacon timestamps within the current window. In-memory,
# best-effort only; never persisted and reset on process restart. An
# OrderedDict so the least-recently-touched IP can be evicted once the
# tracked-IP cap is hit, bounding memory regardless of how many distinct
# (possibly spoofed) IPs are seen.
_page_view_rate_limit_hits: "collections.OrderedDict[str, list[float]]" = collections.OrderedDict()
_SITE_STATS_CACHE: dict = {"data": None, "expires_at": 0.0}
# Guards the cache-miss path in get_public_site_stats so concurrent requests
# during a miss (or at each 60s expiry) coalesce into a single expensive
# JobStore.site_stats() call instead of each hammering the DB pool.
_SITE_STATS_LOCK = asyncio.Lock()
# Per-IP throttle for the public site-stats endpoint, mirroring the
# page-view beacon's rate limiter so an unauthenticated caller can't force
# repeated aggregation queries by spraying requests across the cache window.
_SITE_STATS_RATE_LIMIT_WINDOW_SECONDS = 60.0
_SITE_STATS_RATE_LIMIT_MAX_PER_WINDOW = 10
_SITE_STATS_RATE_LIMIT_MAX_TRACKED_IPS = 5000
_site_stats_rate_limit_hits: "collections.OrderedDict[str, list[float]]" = collections.OrderedDict()

# --- login rate limiting --------------------------------------------------

# Two independent progressive tiers (short burst + longer sustained), each
# a (window_seconds, max_per_window) pair. A key is throttled the moment
# either tier is at capacity.
_LOGIN_RATE_LIMIT_TIERS = ((60.0, 5), (900.0, 15))
_LOGIN_RATE_LIMIT_MAX_TRACKED_KEYS = 5000
_LOGIN_RATE_LIMIT_LONGEST_WINDOW_SECONDS = max(window for window, _ in _LOGIN_RATE_LIMIT_TIERS)
# Independent throttles by source IP and by normalized-lowercase email, so a
# credential-stuffing run spread across many IPs is still caught per-email,
# and a spray of emails from one IP is still caught per-IP.
_login_rate_limit_hits_by_ip: "collections.OrderedDict[str, list[float]]" = (
    collections.OrderedDict()
)
_login_rate_limit_hits_by_email: "collections.OrderedDict[str, list[float]]" = (
    collections.OrderedDict()
)


def _resolve_source_ip(request: Request, trusted_proxy_depth: int) -> str:
    """Source IP trusting exactly ``trusted_proxy_depth`` X-Forwarded-For hops.

    Counted from the right (the hops closest to this server, appended by our
    own trusted reverse-proxy chain): ``index = len(hops) - trusted_proxy_depth``.
    Falls back to ``request.client.host`` when the header is absent, empty, has
    fewer hops than the configured depth, or ``trusted_proxy_depth`` is not
    positive — an attacker-supplied header must never be trusted past the
    depth this deployment's actual proxy chain can vouch for. This is
    intentionally stricter than the page-view/site-stats beacons' naive
    first-entry parsing (untouched above), which is fine for best-effort
    analytics but not for a security-sensitive throttle key.
    """
    if trusted_proxy_depth > 0:
        forwarded = request.headers.get("x-forwarded-for", "")
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()] if forwarded else []
        if len(hops) >= trusted_proxy_depth:
            return hops[len(hops) - trusted_proxy_depth]
    return request.client.host if request.client else ""


def _login_throttled(
    hits_by_key: "collections.OrderedDict[str, list[float]]", key: str, now: float
) -> float | None:
    """``None`` (and records this attempt) if ``key`` is under every tier's cap.

    Otherwise the Retry-After seconds of the first tier at capacity, and the
    attempt is NOT recorded — a blocked probe must not itself extend the
    window it's already failing.
    """
    hits = [
        t for t in hits_by_key.get(key, []) if now - t < _LOGIN_RATE_LIMIT_LONGEST_WINDOW_SECONDS
    ]
    for window_seconds, max_per_window in _LOGIN_RATE_LIMIT_TIERS:
        recent = [t for t in hits if now - t < window_seconds]
        if len(recent) >= max_per_window:
            if hits:
                hits_by_key[key] = hits
                hits_by_key.move_to_end(key)
            else:
                hits_by_key.pop(key, None)
            return window_seconds
    hits.append(now)
    hits_by_key[key] = hits
    hits_by_key.move_to_end(key)
    while len(hits_by_key) > _LOGIN_RATE_LIMIT_MAX_TRACKED_KEYS:
        hits_by_key.popitem(last=False)
    return None


# Lazily-opened maxminddb readers, keyed by db path. A cached ``None`` means
# opening previously failed, so we log once instead of once per request.
_geoip_readers: dict[str, object | None] = {}
_geoip_logged_paths: set[str] = set()

_UA_FAMILY_PATTERNS = (
    ("Edge", re.compile(r"Edg/", re.IGNORECASE)),
    ("Chrome", re.compile(r"Chrome/", re.IGNORECASE)),
    ("Firefox", re.compile(r"Firefox/", re.IGNORECASE)),
    ("Safari", re.compile(r"Safari/", re.IGNORECASE)),
)

_OS_FAMILY_PATTERNS = (
    ("Windows", re.compile(r"Windows", re.IGNORECASE)),
    ("iOS", re.compile(r"iPhone|iPad|iPod", re.IGNORECASE)),
    ("macOS", re.compile(r"Mac OS X|Macintosh", re.IGNORECASE)),
    ("Android", re.compile(r"Android", re.IGNORECASE)),
    ("Linux", re.compile(r"Linux", re.IGNORECASE)),
)


def _today_utc_date_str() -> str:
    """Today's UTC date, isolated so tests can freeze it for hash stability checks."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def _is_bot_user_agent(user_agent: str) -> bool:
    lowered = user_agent.lower()
    return any(marker in lowered for marker in _PAGE_VIEW_BOT_UA_MARKERS)


def _is_valid_page_view_path(path: str) -> bool:
    return path.startswith("/") and ".." not in path and not path.startswith("/api")


def _ua_family(user_agent: str) -> str:
    for name, pattern in _UA_FAMILY_PATTERNS:
        if pattern.search(user_agent):
            return name
    return "Other"


def _os_family(user_agent: str) -> str:
    for name, pattern in _OS_FAMILY_PATTERNS:
        if pattern.search(user_agent):
            return name
    return "Other"


def _screen_bucket(width: int) -> str:
    if width < 480:
        return "xs"
    if width < 768:
        return "sm"
    if width < 1024:
        return "md"
    if width < 1440:
        return "lg"
    return "xl"


def _referrer_host(value: str | None) -> str | None:
    if not value:
        return None
    host = urlparse(value).netloc
    return host or None


def _normalize_page_view_int(value: object, lo: int, hi: int) -> int | str:
    """Return an in-range int, or ``''`` when ``value`` is missing/invalid."""
    if isinstance(value, bool):
        return ""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return ""
    return parsed if lo <= parsed <= hi else ""


def _normalize_page_view_float(value: object, lo: float, hi: float) -> float | str:
    if isinstance(value, bool):
        return ""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return ""
    return parsed if lo <= parsed <= hi else ""


def _normalize_page_view_str(value: object, max_len: int) -> str:
    return value if isinstance(value, str) and len(value) <= max_len else ""


def _normalize_page_view_allowed(value: object, allowed: set[str]) -> str:
    return value if isinstance(value, str) and value in allowed else ""


def _sliding_window_hit(
    hits_by_key: "collections.OrderedDict[str, list[float]]",
    key: str,
    now: float,
    *,
    window_seconds: float,
    max_per_window: int,
    max_tracked_keys: int,
) -> bool:
    """True if ``key`` is already at the cap for this window; else records a hit.

    Shared bounded-OrderedDict sliding-window primitive: prunes hits outside
    ``window_seconds``, evicts the key entirely once it has none left (so
    callers that stop showing up don't linger in the dict forever), and caps
    total tracked keys via LRU eviction once ``max_tracked_keys`` is exceeded
    — bounding memory regardless of how many distinct (possibly spoofed) keys
    are seen. Used by the page-view beacon, site-stats, and login throttles.
    """
    hits = [t for t in hits_by_key.get(key, []) if now - t < window_seconds]
    if not hits:
        hits_by_key.pop(key, None)
    else:
        hits_by_key[key] = hits
        hits_by_key.move_to_end(key)
    if len(hits) >= max_per_window:
        return True
    hits.append(now)
    hits_by_key[key] = hits
    hits_by_key.move_to_end(key)
    while len(hits_by_key) > max_tracked_keys:
        hits_by_key.popitem(last=False)
    return False


def _page_view_rate_limited(ip: str, now: float) -> bool:
    """True if ``ip`` is already at the accepted-beacon cap for this window."""
    return _sliding_window_hit(
        _page_view_rate_limit_hits,
        ip,
        now,
        window_seconds=_PAGE_VIEW_RATE_LIMIT_WINDOW_SECONDS,
        max_per_window=_PAGE_VIEW_RATE_LIMIT_MAX_PER_WINDOW,
        max_tracked_keys=_PAGE_VIEW_RATE_LIMIT_MAX_TRACKED_IPS,
    )


def _site_stats_rate_limited(ip: str, now: float) -> bool:
    """True if ``ip`` is already at the request cap for the site-stats endpoint."""
    return _sliding_window_hit(
        _site_stats_rate_limit_hits,
        ip,
        now,
        window_seconds=_SITE_STATS_RATE_LIMIT_WINDOW_SECONDS,
        max_per_window=_SITE_STATS_RATE_LIMIT_MAX_PER_WINDOW,
        max_tracked_keys=_SITE_STATS_RATE_LIMIT_MAX_TRACKED_IPS,
    )


def _get_geoip_reader(db_path: Path):
    key = str(db_path)
    if key in _geoip_readers:
        return _geoip_readers[key]
    try:
        reader = maxminddb.open_database(str(db_path))
    except (FileNotFoundError, OSError, maxminddb.InvalidDatabaseError) as exc:
        if key not in _geoip_logged_paths:
            _geoip_logged_paths.add(key)
            log.warning("geoip db unavailable at %s: %s", db_path, exc)
        reader = None
    _geoip_readers[key] = reader
    return reader


def _geoip_lookup(ip: str, db_path: Path) -> tuple[str | None, str | None, str | None]:
    """Best-effort country/region/city lookup; never raises."""
    reader = _get_geoip_reader(db_path)
    if reader is None:
        return None, None, None
    try:
        result = reader.get(ip)
    except (ValueError, OSError) as exc:
        key = str(db_path)
        if key not in _geoip_logged_paths:
            _geoip_logged_paths.add(key)
            log.warning("geoip lookup failed for db %s: %s", db_path, exc)
        return None, None, None
    if not result:
        return None, None, None
    country = (result.get("country") or {}).get("names", {}).get("en")
    subdivisions = result.get("subdivisions") or []
    region = subdivisions[0].get("names", {}).get("en") if subdivisions else None
    city = (result.get("city") or {}).get("names", {}).get("en")
    return country, region, city


def build_app(config, orchestrator: Orchestrator, store: JobStore) -> ASGIApp:
    _suggest_backend = build_backend("claude", config=config)
    _intake_backend = build_backend("claude", config=config)

    # Bootstrap first platform admin from env when no users exist yet.
    _admin_email = os.environ.get("HYQS_ADMIN_EMAIL", "")
    _admin_password = os.environ.get("HYQS_ADMIN_PASSWORD", "")
    if _admin_email and _admin_password and not store.list_users():
        store.create_user(
            _admin_email, _admin_password, display_name="Admin", is_platform_admin=True
        )
        log.info("bootstrapped platform admin: %s", _admin_email)

    def _ctx_from_request(request: Request) -> AuthContext:
        return AuthContext(
            user_id=getattr(request.state, "user_id", None),
            user_email=getattr(request.state, "user_email", None),
            is_platform_admin=getattr(request.state, "is_platform_admin", False),
            token_project_id=getattr(request.state, "token_project_id", None),
            token_role=getattr(request.state, "token_role", None),
            api_token_id=getattr(request.state, "api_token_id", None),
        )

    def _caller_has_permission(
        request: Request, permission: str, project_id: int | None = None
    ) -> bool:
        ctx = _ctx_from_request(request)
        return check_permission(store, ctx, permission, project_id)

    def _is_project_member(request: Request, project_id: int) -> bool:
        ctx = _ctx_from_request(request)
        return is_project_member(store, ctx, project_id)

    def _orchestrator_session_id(request: Request) -> int:
        # Keys the console orchestrator's per-caller Claude session (see
        # hyqs/core/agent.py's Orchestrator) so distinct principals never share
        # conversation history. `user_id` is None only for the raw
        # HYQS_WEB_TOKEN bypass path (no backing user row), which maps to a
        # reserved sentinel — users.id is a positive serial, so 0 never
        # collides with a real user. This is unrelated to WEB_CHAT_ID, which
        # remains the `jobs.chat_id` provenance FK for UI-created jobs.
        user_id = getattr(request.state, "user_id", None)
        return user_id if user_id is not None else _PLATFORM_TOKEN_SESSION_ID

    async def health(request: Request):
        return JSONResponse({"ok": True, "service": "hyqs", "pid": os.getpid()})

    async def chat(request: Request):
        # The console orchestrator runs with Bash/Write/Edit on the host, so
        # reaching it is equivalent to a shell as the hyqs service user. Gate it
        # on the platform-wide `admin_console` permission: project-scoped API
        # tokens can never satisfy a platform-wide check, and a project role
        # (project_admin included) does not carry it.
        if not _caller_has_permission(request, "admin_console"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        body = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "empty message"}, status_code=400)
        reply = await orchestrator.ask(_orchestrator_session_id(request), text)
        return JSONResponse({"reply": reply})

    _VALID_JOB_STATUSES = frozenset({"active", "done", "failed", "archived", "all"})

    async def list_jobs(request: Request):
        status_param = request.query_params.get("status", "active")
        if status_param not in _VALID_JOB_STATUSES:
            return JSONResponse(
                {"error": "status must be one of: active, done, failed, archived, all"},
                status_code=400,
            )
        pid_param = request.query_params.get("project_id")
        eid_param = request.query_params.get("epic_id")
        if pid_param is not None:
            try:
                pid = int(pid_param)
            except ValueError:
                return JSONResponse({"error": "invalid project_id"}, status_code=400)
            if not _is_project_member(request, pid):
                return JSONResponse({"error": "forbidden"}, status_code=403)
            jobs = store.list_active(50, project_id=pid, status=status_param)
        elif eid_param is not None:
            try:
                eid = int(eid_param)
            except ValueError:
                return JSONResponse({"error": "invalid epic_id"}, status_code=400)
            _epic = store.get_epic(eid)
            if _epic and not _is_project_member(request, _epic.project_id):
                return JSONResponse({"error": "forbidden"}, status_code=403)
            jobs = store.list_active(50, epic_id=eid, status=status_param)
        else:
            jobs = store.list_active(50, status=status_param)
            if not getattr(request.state, "is_platform_admin", False):
                jobs = [
                    job
                    for job in jobs
                    if job.project_id is not None and _is_project_member(request, job.project_id)
                ]
        return JSONResponse(
            {"jobs": [job_to_dict(j, **job_projection_extras(store, j)) for j in jobs]}
        )

    async def archive_terminal(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        project_id_raw = body.get("project_id") if isinstance(body, dict) else None
        project_id = None
        if project_id_raw is not None:
            try:
                project_id = int(project_id_raw)
            except (TypeError, ValueError):
                return JSONResponse({"error": "project_id must be an integer"}, status_code=400)
        # This archives jobs AND fans out github.cleanup_job_artifacts (closes
        # PRs, deletes branches/worktrees), so it needs archive_job. Passing
        # project_id=None checks it platform-wide, which only a platform_admin
        # (or an explicit platform-wide grant) can satisfy — a project role or a
        # project-scoped API token cannot trigger the cross-project sweep.
        if not _caller_has_permission(request, "archive_job", project_id=project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        archived = store.archive_terminal(project_id=project_id)
        # Tear down each archived job's PR/branches/worktree (best-effort, in parallel).
        await asyncio.gather(
            *(github.cleanup_job_artifacts(config, j) for j in archived),
            return_exceptions=True,
        )
        return JSONResponse({"archived": len(archived)})

    async def archive_job(request: Request):
        from hyqs.pipeline.models import JobStatus

        job_id = int(request.path_params["job_id"])
        _job_pre = store.get(job_id)
        if _job_pre is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "archive_job", project_id=_job_pre.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if not store.set_archived(job_id, True):
            return JSONResponse({"error": "not found"}, status_code=404)
        job = store.get(job_id)
        # Only tear down artifacts for a retired (terminal) job — never disturb a
        # running job's live worktree/branch.
        if job and job.status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
            await github.cleanup_job_artifacts(config, job)
        return JSONResponse({"job": job_to_dict(job, **job_projection_extras(store, job))})

    async def unarchive_job(request: Request):
        job_id = int(request.path_params["job_id"])
        _job_pre = store.get(job_id)
        if _job_pre is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "archive_job", project_id=_job_pre.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if not store.set_archived(job_id, False):
            return JSONResponse({"error": "not found"}, status_code=404)
        job = store.get(job_id)
        return JSONResponse({"job": job_to_dict(job, **job_projection_extras(store, job))})

    async def patch_job_epic(request: Request):
        job_id = int(request.path_params["job_id"])
        _job = store.get(job_id)
        if _job is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "edit_job_deps", project_id=_job.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        epic_id_raw = body.get("epic_id")
        epic_id = None
        if epic_id_raw is not None:
            try:
                epic_id = int(epic_id_raw)
            except (TypeError, ValueError):
                return JSONResponse(
                    {"error": "epic_id must be an integer or null"}, status_code=400
                )
        epic_error = _job_epic_error(store, _job, epic_id)
        if epic_error is not None:
            status_code, message = epic_error
            return JSONResponse({"error": message}, status_code=status_code)
        if not store.update_job_epic(job_id, epic_id):
            return JSONResponse({"error": "not found"}, status_code=404)
        job = store.get(job_id)
        return JSONResponse({"job": job_to_dict(job, **job_projection_extras(store, job))})

    async def cancel_job(request: Request):
        job_id = int(request.path_params["job_id"])
        _job = store.get(job_id)
        if _job is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "cancel_job", project_id=_job.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if store.cancel(job_id):
            return Response(status_code=204)
        return JSONResponse({"error": "job already in terminal state"}, status_code=409)

    async def retry_job(request: Request):
        job_id = int(request.path_params["job_id"])
        job = store.get(job_id)
        if job is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "retry_job", project_id=job.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        from hyqs.pipeline.models import JobStatus

        if job.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
            return JSONResponse({"error": "job is not in a retryable state"}, status_code=409)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        force = bool(body.get("force", False))
        override = bool(body.get("override_active_remediation", False))

        if job.needs_split and not force:
            return JSONResponse(
                {
                    "error": (
                        "this job's plan was too large even after a re-ask and has "
                        "been parked — a bare retry is refused; refile a new, "
                        "smaller job, or retry with force=true to bypass the "
                        "scope gate"
                    )
                },
                status_code=409,
            )

        if not override:
            active_id = store.get_active_remediation(job_id)
            if active_id is not None and active_id != job_id:
                active_job = store.get(active_id)
                if active_job is not None and active_job.status not in (
                    JobStatus.DONE,
                    JobStatus.FAILED,
                    JobStatus.CANCELLED,
                ):
                    return JSONResponse(
                        {
                            "error": remediation.format_conflict_reason(job_id, active_id),
                            "active_remediation_job_id": active_id,
                        },
                        status_code=409,
                    )

        # Clean up git artifacts BEFORE mutating the DB so there is no partial state
        # if cleanup fails.
        worktree = Path(config.data_dir) / "worktrees" / f"job-{job_id}"
        cleanup_error = await github.cleanup_branch_for_retry(job, worktree)
        if cleanup_error:
            return JSONResponse({"error": cleanup_error}, status_code=500)

        updated = store.retry(job_id, force=force)
        if updated is None:
            return JSONResponse({"error": "retry failed"}, status_code=500)
        return JSONResponse({"job": job_to_dict(updated, **job_projection_extras(store, updated))})

    async def requeue_job(request: Request):
        job_id = int(request.path_params["job_id"])
        job = store.get(job_id)
        if job is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "resolve_job", project_id=job.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        stage = body.get("stage")
        stage_error = _requeue_stage_error(job, stage)
        if stage_error is not None:
            status_code, message = stage_error
            return JSONResponse({"error": message}, status_code=status_code)
        store.requeue_job_at_stage(job_id, Stage(stage))
        updated = store.get(job_id)
        return JSONResponse({"job": job_to_dict(updated, **job_projection_extras(store, updated))})

    async def fix_forward_job(request: Request):
        job_id = int(request.path_params["job_id"])
        job = store.get(job_id)
        if job is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "resolve_job", project_id=job.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        idea = (body.get("idea") or "").strip()
        title = (body.get("title") or "").strip()
        if not idea:
            return JSONResponse({"error": "idea is required"}, status_code=400)
        override = bool(body.get("override_active_remediation", False))

        conflict = _active_remediation_conflict(store, job.id, override)
        if conflict is not None:
            active_id, message = conflict
            return JSONResponse(
                {"error": message, "active_remediation_job_id": active_id},
                status_code=409,
            )

        repoint_dependent_ids = body.get("repoint_dependent_ids") or []
        source_actor = getattr(request.state, "user_email", None) or ""
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
                job.id, idea, title, source_actor, repoint_dependent_ids
            )
        else:
            fix_job = store.create(
                idea=idea,
                repo_path=job.repo_path,
                chat_id=job.chat_id,
                epic_id=job.epic_id,
                title=title,
                source=JobSource.UI,
                source_actor=source_actor,
                source_meta=source_meta,
            )
        store.set_active_remediation(job.id, fix_job.id)
        return JSONResponse({"job": job_to_dict(fix_job, **job_projection_extras(store, fix_job))})

    async def patch_job_idea(request: Request):
        job_id = int(request.path_params["job_id"])
        job = store.get(job_id)
        if job is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "edit_job_deps", project_id=job.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        idea = (body.get("idea") or "").strip()
        idea_error = _job_idea_error(idea)
        if idea_error is not None:
            status_code, message = idea_error
            return JSONResponse({"error": message}, status_code=status_code)
        store.update_job_idea(job_id, idea)
        updated = store.get(job_id)
        return JSONResponse({"job": job_to_dict(updated, **job_projection_extras(store, updated))})

    async def resolve_job(request: Request):
        job_id = int(request.path_params["job_id"])
        job = store.get(job_id)
        if job is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "resolve_job", project_id=job.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        resolution = (body.get("resolution") or "resolved").strip()
        error = _resolve_job_error(job, resolution)
        if error is not None:
            status_code, message = error
            return JSONResponse({"error": message}, status_code=status_code)
        store.set_job_resolution(job_id, resolution)
        updated = store.get(job_id)
        return JSONResponse({"job": job_to_dict(updated, **job_projection_extras(store, updated))})

    async def job_events(request: Request):
        job_id = request.path_params["job_id"]
        job = store.get(job_id)
        if job is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if job.project_id is not None and not _is_project_member(request, job.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse(
            {
                "events": store.list_events(job_id),
                "supervisor_events": store.list_supervisor_events_for_job(job_id),
            }
        )

    async def get_job(request: Request) -> JSONResponse:
        job_id = request.path_params["job_id"]
        job = store.get(job_id)
        if job is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if job.project_id is not None and not _is_project_member(request, job.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse({"job": job_to_dict(job, **job_projection_extras(store, job))})

    async def get_job_resources(request: Request) -> JSONResponse:
        job_id = int(request.path_params["id"])
        job = store.get(job_id)
        if job is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if job.project_id is not None and not _is_project_member(request, job.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        rows = store.get_resource_usage(job_id)
        return JSONResponse([resource_usage_to_dict(r) for r in rows])

    async def get_job_usage(request: Request) -> JSONResponse:
        job_id = int(request.path_params["job_id"])
        job = store.get(job_id)
        if job is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if job.project_id is not None and not _is_project_member(request, job.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse(store.get_job_usage(job_id))

    async def get_job_dependencies(request: Request) -> JSONResponse:
        job_id = int(request.path_params["job_id"])
        job = store.get(job_id)
        if job is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if job.project_id is not None and not _is_project_member(request, job.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse({"depends_on": store.get_dependencies(job_id)})

    async def get_job_dependents(request: Request) -> JSONResponse:
        job_id = int(request.path_params["job_id"])
        job = store.get(job_id)
        if job is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if job.project_id is not None and not _is_project_member(request, job.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        dependents = store.get_dependent_jobs(job_id)
        return JSONResponse(
            {
                "dependents": [
                    {"id": d.id, "title": d.title, "status": d.status.value} for d in dependents
                ]
            }
        )

    async def get_job_depends_on_jobs(request: Request) -> JSONResponse:
        job_id = int(request.path_params["job_id"])
        job = store.get(job_id)
        if job is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if job.project_id is not None and not _is_project_member(request, job.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        depends_on_jobs = store.get_dependency_jobs(job_id)
        return JSONResponse(
            {
                "depends_on_jobs": [
                    {"id": d.id, "title": d.title, "status": d.status.value}
                    for d in depends_on_jobs
                ]
            }
        )

    async def add_job_dependency(request: Request) -> JSONResponse:
        job_id = int(request.path_params["job_id"])
        _job = store.get(job_id)
        if _job is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "edit_job_deps", project_id=_job.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
            dep_id = body.get("depends_on_job_id")
            if not isinstance(dep_id, int):
                raise ValueError("depends_on_job_id must be an int")
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        store.add_job_dependency(job_id, dep_id)
        return JSONResponse({"depends_on": store.get_dependencies(job_id)})

    async def delete_job_dependency(request: Request) -> JSONResponse:
        job_id = int(request.path_params["job_id"])
        dep_id = int(request.path_params["dep_id"])
        _job = store.get(job_id)
        if _job is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "edit_job_deps", project_id=_job.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        store.remove_job_dependency(job_id, dep_id)
        return JSONResponse({"depends_on": store.get_dependencies(job_id)})

    async def job_diff(request: Request):
        job_id = request.path_params["job_id"]
        job = store.get(job_id)
        if job is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if job.project_id is not None and not _is_project_member(request, job.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if not job.branch:
            return JSONResponse({"diff": "", "truncated": False, "note": "no branch yet"})
        payload = await _job_diff_payload(job)
        if "error" in payload:
            return JSONResponse({"error": payload["error"]}, status_code=404)
        diff = payload["diff"]
        return JSONResponse(
            {
                "diff": diff[:100_000],
                "truncated": len(diff) > 100_000,
                "base": payload["base"],
                "branch": payload["branch"],
                "commit": payload["commit"],
            }
        )

    async def create_job_batch(request: Request):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        repo = (body.get("repo") or config.default_repo or "").strip()
        if not repo:
            return JSONResponse(
                {"error": "no repo specified and no default configured"}, status_code=400
            )
        jobs_raw = body.get("jobs")
        batch_session_id = body.get("session_id")
        if not isinstance(jobs_raw, list) or not jobs_raw:
            return JSONResponse({"error": "jobs must be a non-empty list"}, status_code=400)
        _proj = store.get_project_by_repo(repo)
        if not _caller_has_permission(request, "queue_job", project_id=_proj.id if _proj else None):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        source_actor = getattr(request.state, "user_email", None) or ""
        try:
            _proj = store.ensure_project_for_repo(repo)
            # Cycle detection on raw batch indexes before per-job validation
            _n = len(jobs_raw)
            _raw_deps: list[list[int]] = []
            for _item in jobs_raw:
                if isinstance(_item, dict):
                    _d = _item.get("depends_on")
                    _raw_deps.append(
                        _d if isinstance(_d, list) and all(isinstance(x, int) for x in _d) else []
                    )
                else:
                    _raw_deps.append([])
            _detect_batch_dependency_cycle(_raw_deps)
            jobs_spec: list[dict] = []
            for item in jobs_raw:
                if not isinstance(item, dict):
                    return JSONResponse({"error": "each job must be an object"}, status_code=400)
                title = (item.get("title") or "").strip()
                if not title:
                    return JSONResponse({"error": "each job must have a title"}, status_code=400)
                description = (item.get("description") or "").strip()
                ac = (item.get("acceptance_criteria") or "").strip()
                idea_parts = [title]
                if description:
                    idea_parts.append(description)
                if ac:
                    idea_parts.append(f"Acceptance criteria: {ac}")
                idea = ": ".join(idea_parts[:2])
                if ac:
                    idea = (
                        f"{title}: {description}\n\nAcceptance criteria: {ac}"
                        if description
                        else f"{title}\n\nAcceptance criteria: {ac}"
                    )

                epic_id: int | None = None
                epic_id_raw = item.get("epic_id")
                new_epic_raw = item.get("new_epic")
                if epic_id_raw is not None:
                    try:
                        epic_id = int(epic_id_raw)
                    except (TypeError, ValueError):
                        return JSONResponse(
                            {"error": "epic_id must be an integer"}, status_code=400
                        )
                elif new_epic_raw is not None:
                    if (
                        not isinstance(new_epic_raw, dict)
                        or not new_epic_raw.get("name", "").strip()
                    ):
                        return JSONResponse(
                            {"error": "new_epic.name is required when creating an epic inline"},
                            status_code=400,
                        )
                    _new_epic = store.create_epic(
                        _proj.id,
                        new_epic_raw["name"].strip(),
                        (new_epic_raw.get("description") or "").strip(),
                    )
                    epic_id = _new_epic.id
                else:
                    return JSONResponse(
                        {
                            "error": f"job '{title}' has no epic_id or new_epic — every batch job must carry an epic"
                        },
                        status_code=400,
                    )

                common_fields = _parse_batch_job_common_fields(item, _n)
                depends_on = common_fields["depends_on"]
                target_files = common_fields["target_files"]
                priority = common_fields["priority"]

                scope_raw = item.get("scope")
                scope: dict | None = None
                if scope_raw is not None:
                    if not isinstance(scope_raw, dict):
                        return JSONResponse({"error": "scope must be an object"}, status_code=400)
                    allowed_paths_raw = scope_raw.get("allowed_paths")
                    if allowed_paths_raw is not None and not isinstance(allowed_paths_raw, list):
                        return JSONResponse(
                            {"error": "scope.allowed_paths must be a list"}, status_code=400
                        )
                    scope = {
                        "allowed_paths": [str(p) for p in (allowed_paths_raw or [])],
                        "interfaces": str(scope_raw.get("interfaces") or ""),
                    }

                jobs_spec.append(
                    {
                        "idea": idea,
                        "title": title,
                        "epic_id": epic_id,
                        "depends_on": depends_on,
                        "target_files": target_files,
                        "priority": priority,
                        "scope": scope,
                    }
                )

            batch_source_meta: dict = {}
            if batch_session_id:
                batch_source_meta["chat_session_id"] = batch_session_id
            created = store.create_batch(
                repo,
                jobs_spec,
                chat_id=WEB_CHAT_ID,
                source=JobSource.UI,
                source_actor=source_actor,
                source_meta=batch_source_meta if batch_source_meta else None,
            )
            if batch_session_id and created:
                try:
                    store.update_chat_session(batch_session_id, status="confirmed")
                except Exception:
                    log.exception("failed to confirm chat session %s", batch_session_id)
                try:
                    refine_session = store.get_chat_session(batch_session_id)
                    if refine_session is not None and refine_session.source_backlog_item_ids:
                        created_ids = [j.id for j in created]
                        for _item_id in refine_session.source_backlog_item_ids:
                            try:
                                store.link_backlog_jobs(_item_id, created_ids)
                            except Exception:
                                log.exception(
                                    "failed to link backlog item %s to batch jobs", _item_id
                                )
                except Exception:
                    log.exception(
                        "failed to load refine session %s for backlog linking", batch_session_id
                    )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        return JSONResponse(
            {
                "jobs": [job_to_dict(j, **job_projection_extras(store, j)) for j in created],
                "count": len(created),
            },
            status_code=201,
        )

    async def create_job(request: Request):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        idea = (body.get("idea") or "").strip()
        repo = (body.get("repo") or config.default_repo or "").strip()
        title = (body.get("title") or "").strip()
        if repo:
            _proj = store.get_project_by_repo(repo)
            if not _caller_has_permission(
                request, "queue_job", project_id=_proj.id if _proj else None
            ):
                return JSONResponse({"error": "forbidden"}, status_code=403)
        epic_id_raw = body.get("epic_id")
        new_epic_raw = body.get("new_epic")
        epic_id = None
        if epic_id_raw is not None:
            try:
                epic_id = int(epic_id_raw)
            except (TypeError, ValueError):
                return JSONResponse({"error": "epic_id must be an integer"}, status_code=400)
        depends_on_raw = body.get("depends_on")
        depends_on = None
        if depends_on_raw is not None:
            if not isinstance(depends_on_raw, list) or not all(
                isinstance(x, int) for x in depends_on_raw
            ):
                return JSONResponse(
                    {"error": "depends_on must be a list of integers"}, status_code=400
                )
            depends_on = depends_on_raw
        if not idea:
            return JSONResponse({"error": "idea is required"}, status_code=400)
        if not repo:
            return JSONResponse(
                {"error": "no repo specified and no default configured"}, status_code=400
            )
        auto_assigned = False
        auto_created = False
        is_fallback = False
        if epic_id is None and new_epic_raw is not None:
            if not isinstance(new_epic_raw, dict) or not new_epic_raw.get("name", "").strip():
                return JSONResponse(
                    {"error": "new_epic.name is required when creating an epic inline"},
                    status_code=400,
                )
            try:
                _proj = store.ensure_project_for_repo(repo)
                _new_epic = store.create_epic(
                    _proj.id,
                    new_epic_raw["name"].strip(),
                    (new_epic_raw.get("description") or "").strip(),
                )
                epic_id = _new_epic.id
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
        elif epic_id is None:
            _proj = store.ensure_project_for_repo(repo)
            epic_id, auto_created, is_fallback = _resolve_epic(store, _proj.id, idea)
            auto_assigned = True
        forwarded = request.headers.get("x-forwarded-for", "")
        ip = (
            forwarded.split(",")[0].strip()
            if forwarded
            else (request.client.host if request.client else "")
        )
        duplicate = store.find_recent_duplicate(repo, idea, 10)
        if duplicate is not None:
            dup_epic = store.get_epic(duplicate.epic_id) if duplicate.epic_id else None
            return JSONResponse(
                {
                    "job": job_to_dict(duplicate, **job_projection_extras(store, duplicate)),
                    "auto_epic": {
                        "id": duplicate.epic_id,
                        "name": dup_epic.name if dup_epic else "",
                        "auto_assigned": False,
                        "auto_created": False,
                    },
                    "deduplicated": True,
                },
                status_code=200,
            )
        try:
            job = store.create(
                idea=idea,
                repo_path=repo,
                chat_id=WEB_CHAT_ID,
                epic_id=epic_id,
                depends_on=depends_on,
                title=title,
                source=JobSource.UI,
                source_actor=getattr(request.state, "user_email", None) or "",
                source_meta={
                    "ip": ip,
                    "user_agent": request.headers.get("user-agent", ""),
                    "role": "platform_admin"
                    if getattr(request.state, "is_platform_admin", False)
                    else "user",
                    "path": request.url.path,
                },
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        log.info("web created job #%s", job.id)
        assigned_epic = store.get_epic(epic_id)
        auto_epic = {
            "id": epic_id,
            "name": assigned_epic.name if assigned_epic else "",
            "auto_assigned": auto_assigned,
            "auto_created": auto_created,
        }
        return JSONResponse(
            {
                "job": job_to_dict(job, **job_projection_extras(store, job)),
                "auto_epic": auto_epic,
            },
            status_code=201,
        )

    async def set_job_priority(request: Request):
        job_id = int(request.path_params["job_id"])
        _job = store.get(job_id)
        if _job is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "edit_job_deps", project_id=_job.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        priority_raw = body.get("priority")
        priority_error = _job_priority_error(priority_raw)
        if priority_error is not None:
            status_code, message = priority_error
            return JSONResponse({"error": message}, status_code=status_code)
        if not store.set_priority(job_id, priority_raw):
            return JSONResponse({"error": "not found"}, status_code=404)
        job = store.get(job_id)
        return JSONResponse({"job": job_to_dict(job, **job_projection_extras(store, job))})

    _VALID_EPIC_STATUSES = frozenset({"active", "paused", "done", "archived"})

    async def list_epics(request: Request):
        pid_param = request.query_params.get("project_id")
        project_id = None
        if pid_param is not None:
            try:
                project_id = int(pid_param)
            except ValueError:
                return JSONResponse({"error": "invalid project_id"}, status_code=400)
            if not _is_project_member(request, project_id):
                return JSONResponse({"error": "forbidden"}, status_code=403)
        include_archived = request.query_params.get("include_archived", "false").lower() == "true"
        return JSONResponse(
            {"epics": store.list_epics(project_id=project_id, include_archived=include_archived)}
        )

    async def create_epic(request: Request):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        project_id_raw = body.get("project_id")
        name = (body.get("name") or "").strip()
        description = (body.get("description") or "").strip()
        if project_id_raw is None:
            return JSONResponse({"error": "project_id is required"}, status_code=400)
        try:
            project_id = int(project_id_raw)
        except (TypeError, ValueError):
            return JSONResponse({"error": "project_id must be an integer"}, status_code=400)
        if not _is_project_member(request, project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if not name:
            return JSONResponse({"error": "name is required"}, status_code=400)
        try:
            epic = store.create_epic(project_id, name, description)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"epic": epic_to_dict(epic)}, status_code=201)

    async def get_epic(request: Request):
        epic_id = request.path_params["epic_id"]
        epic = store.get_epic(epic_id)
        if epic is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _is_project_member(request, epic.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse({"epic": epic_to_dict(epic)})

    async def update_epic(request: Request):
        epic_id = request.path_params["epic_id"]
        epic = store.get_epic(epic_id)
        if epic is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _is_project_member(request, epic.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        name = body.get("name")
        description = body.get("description")
        status = body.get("status")
        archived = body.get("archived")
        if status is not None and status not in _VALID_EPIC_STATUSES:
            return JSONResponse(
                {"error": f"status must be one of {sorted(_VALID_EPIC_STATUSES)}"},
                status_code=400,
            )
        if archived is not None and not isinstance(archived, bool):
            return JSONResponse({"error": "archived must be a boolean"}, status_code=400)
        updated = store.update_epic(epic_id, name=name, description=description, status=status)
        if updated is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if archived is not None:
            store.set_archived_epic(int(epic_id), archived)
            updated = store.get_epic(epic_id)
        return JSONResponse({"epic": epic_to_dict(updated)})

    async def archive_epic(request: Request):
        epic_id = int(request.path_params["epic_id"])
        epic = store.get_epic(epic_id)
        if epic is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _is_project_member(request, epic.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if not store.set_archived_epic(epic_id, True):
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"ok": True})

    async def unarchive_epic(request: Request):
        epic_id = int(request.path_params["epic_id"])
        epic = store.get_epic(epic_id)
        if epic is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _is_project_member(request, epic.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if not store.set_archived_epic(epic_id, False):
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"ok": True})

    async def suggest_epic_for_job(request: Request):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        project_id_raw = body.get("project_id")
        idea = (body.get("idea") or "").strip()
        if project_id_raw is None:
            return JSONResponse({"error": "project_id is required"}, status_code=400)
        try:
            project_id = int(project_id_raw)
        except (TypeError, ValueError):
            return JSONResponse({"error": "project_id must be an integer"}, status_code=400)
        if not _is_project_member(request, project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        epics = store.list_epics(project_id=project_id)
        result = suggest_best_epic(idea, epics)
        return JSONResponse(result)

    async def suggest_epic_features(request: Request):
        epic_id = request.path_params["epic_id"]
        epic = store.get_epic(epic_id)
        if epic is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        # These routes run an agent with Bash inside project.repo_path, on an
        # attacker-controllable prompt — membership is mandatory before that.
        if not _is_project_member(request, epic.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        project = store.get_project(epic.project_id)
        if project is None:
            return JSONResponse({"error": "project not found"}, status_code=404)
        cwd = project.repo_path
        prompt = (
            f"Project: {project.name if project else 'unknown'}\n"
            f"Repository: {cwd}\n"
            f"Epic name: {epic.name}\n"
            f"Epic description: {epic.description or '(none provided)'}\n\n"
            "Suggest 3-7 concrete sub-features to build for this epic."
        )
        try:
            run = await _suggest_backend.run(
                prompt=prompt,
                cwd=cwd,
                role=Role.PLANNER,
                append_system=_SUGGEST_SYS,
                max_turns=10,
            )
        except Exception as exc:
            log.exception("suggest failed for epic %s", epic_id)
            return JSONResponse({"error": str(exc)}, status_code=500)
        try:
            data = parse_result_block(run.text)
        except ResultBlockError:
            data = None
        if not data or not isinstance(data.get("suggestions"), list):
            return JSONResponse(
                {"error": "model returned no valid suggestions", "raw": run.text[:500]},
                status_code=500,
            )
        return JSONResponse({"suggestions": data["suggestions"]})

    # --- admin: role permissions --------------------------------------------

    async def get_role_permissions_route(request: Request) -> JSONResponse:
        if not _caller_has_permission(request, "manage_roles"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse(store.get_role_permissions())

    async def set_role_permission_route(request: Request) -> JSONResponse:
        if not _caller_has_permission(request, "manage_roles"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        role = request.path_params["role"]
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        permission = body.get("permission")
        enabled = body.get("enabled")
        if not isinstance(permission, str) or not permission:
            return JSONResponse({"error": "permission must be a non-empty string"}, status_code=400)
        if not isinstance(enabled, bool):
            return JSONResponse({"error": "enabled must be a boolean"}, status_code=400)
        try:
            store.set_role_permission(role, permission, enabled)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True})

    async def list_projects(request: Request):
        is_pa = getattr(request.state, "is_platform_admin", False)
        if is_pa:
            return JSONResponse({"projects": store.list_projects()})
        user_id = getattr(request.state, "user_id", None)
        if not user_id:
            return JSONResponse({"projects": []})
        accessible = set(store.get_user_project_ids(str(user_id)))
        return JSONResponse(
            {"projects": [p for p in store.list_projects() if p["id"] in accessible]}
        )

    async def create_project_route(request: Request) -> JSONResponse:
        if not _caller_has_permission(request, "create_project"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        name = (body.get("name") or "").strip()
        repo = (body.get("repo") or "").strip()
        if not name:
            return JSONResponse({"error": "name is required"}, status_code=400)
        if not repo:
            return JSONResponse({"error": "repo is required"}, status_code=400)
        if not _docker_deploy.is_user_project(repo, config.projects_dir):
            return JSONResponse(
                {"error": "repo must be inside the projects directory"}, status_code=400
            )
        project = store.create_project_exclusive(name, repo)
        if project is None:
            return JSONResponse(
                {"error": "a project with this repo path already exists"}, status_code=409
            )
        user_id = getattr(request.state, "user_id", None)
        if user_id is not None:
            store.add_project_member(project.id, str(user_id), "project_admin")
        return JSONResponse({"project": project_to_dict(project)}, status_code=201)

    async def provision_project_route(request: Request):
        """Create a NEW project: scaffold a local repo + GitHub repo, then register it."""
        if not _caller_has_permission(request, "create_project"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        name = (body.get("name") or "").strip()
        description = (body.get("description") or "").strip()
        private = bool(body.get("private", True))
        create_github = bool(body.get("github", True))
        stack = body.get("stack", "bare") or "bare"
        spec = body.get("spec")
        if not name:
            return JSONResponse({"error": "name is required"}, status_code=400)
        user_id = getattr(request.state, "user_id", None)
        try:
            result = await provision_project(
                store,
                config.projects_dir,
                name,
                description,
                create_github=create_github,
                private=private,
                stack=stack,
                spec=spec,
                creator_id=str(user_id) if user_id is not None else None,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            log.exception("project provisioning failed")
            return JSONResponse({"error": f"provisioning failed: {exc}"}, status_code=500)
        return JSONResponse(
            {
                "project": project_to_dict(result["project"]),
                "github_url": result["github_url"],
                "warnings": result["warnings"],
            },
            status_code=201,
        )

    _VALID_PROJECT_STATUSES = frozenset({"active", "paused", "archived"})

    async def update_project(request: Request):
        from hyqs.pipeline.store import _UNSET as _STORE_UNSET

        project_id = int(request.path_params["project_id"])
        if not _caller_has_permission(request, "edit_project", project_id=project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        name = body.get("name")
        description = body.get("description")
        status = body.get("status")
        if status is not None and status not in _VALID_PROJECT_STATUSES:
            return JSONResponse(
                {"error": f"status must be one of {sorted(_VALID_PROJECT_STATUSES)}"},
                status_code=400,
            )
        max_fix_attempts = _STORE_UNSET
        if "max_fix_attempts" in body:
            raw = body["max_fix_attempts"]
            if raw is None:
                max_fix_attempts = None
            else:
                try:
                    val = int(raw)
                except (TypeError, ValueError):
                    return JSONResponse(
                        {"error": "max_fix_attempts must be a positive integer or null"},
                        status_code=400,
                    )
                if val < 1:
                    return JSONResponse(
                        {"error": "max_fix_attempts must be a positive integer or null"},
                        status_code=400,
                    )
                max_fix_attempts = val
        updated = store.update_project(
            project_id,
            name=name,
            description=description,
            status=status,
            max_fix_attempts=max_fix_attempts,
        )
        if updated is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"project": project_to_dict(updated)})

    async def delete_project_route(request: Request):
        project_id = int(request.path_params["project_id"])
        if not _caller_has_permission(request, "delete_project", project_id=project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        project = store.get_project(project_id)
        if project is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        container_name = _docker_deploy._container_name(project.repo_path)
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "stop",
                container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30)
        except Exception as exc:
            log.warning("delete_project: docker stop %s failed: %s", container_name, exc)
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "rm",
                container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30)
        except Exception as exc:
            log.warning("delete_project: docker rm %s failed: %s", container_name, exc)
        slug = Path(project.repo_path).name
        try:
            await _nginx_sites.remove_site(slug)
        except Exception as exc:
            log.warning("delete_project: remove_site %s failed: %s", slug, exc)
        store.delete_project(project_id)
        return JSONResponse({"deleted": True})

    # --- agent roster -----------------------------------------------------
    def _validate_agent(body: dict, *, partial: bool) -> tuple[dict, str | None]:
        """Pull + validate agent fields from a request body. Returns (fields, error)."""
        fields: dict = {}
        if "name" in body:
            name = (body.get("name") or "").strip()
            if not name and not partial:
                return {}, "name is required"
            if name:
                fields["name"] = name
        elif not partial:
            return {}, "name is required"
        if "provider" in body:
            provider = (body.get("provider") or "").strip().lower()
            if provider not in known_providers():
                return {}, f"provider must be one of {known_providers()}"
            fields["provider"] = provider
        if "model" in body:
            fields["model"] = (body.get("model") or "").strip()
        if "allowed_tasks" in body:
            tasks = body.get("allowed_tasks")
            if not isinstance(tasks, list) or any(t not in ALL_TASKS for t in tasks):
                return {}, f"allowed_tasks must be a subset of {ALL_TASKS}"
            fields["allowed_tasks"] = tasks
        if "max_concurrency" in body:
            try:
                mc = int(body.get("max_concurrency"))
            except (TypeError, ValueError):
                return {}, "max_concurrency must be an integer"
            if mc < 1:
                return {}, "max_concurrency must be >= 1"
            fields["max_concurrency"] = mc
        if "enabled" in body:
            fields["enabled"] = bool(body.get("enabled"))
        return fields, None

    async def agent_meta(request: Request):
        return JSONResponse({"providers": known_providers(), "tasks": ALL_TASKS})

    async def list_agents(request: Request):
        project_id = int(request.path_params["project_id"])
        if store.get_project(project_id) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _is_project_member(request, project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse({"agents": [agent_to_dict(a) for a in store.list_agents(project_id)]})

    async def create_agent(request: Request):
        project_id = int(request.path_params["project_id"])
        if not _caller_has_permission(request, "edit_agents", project_id=project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        fields, err = _validate_agent(body, partial=False)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        try:
            agent = store.create_agent(project_id, **fields)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"agent": agent_to_dict(agent)}, status_code=201)

    async def update_agent(request: Request):
        agent_id = int(request.path_params["agent_id"])
        _agent = store.get_agent(agent_id)
        if _agent is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "edit_agents", project_id=_agent.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        fields, err = _validate_agent(body, partial=True)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        updated = store.update_agent(agent_id, **fields)
        if updated is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"agent": agent_to_dict(updated)})

    async def delete_agent(request: Request):
        agent_id = int(request.path_params["agent_id"])
        _agent = store.get_agent(agent_id)
        if _agent is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "edit_agents", project_id=_agent.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if not store.delete_agent(agent_id):
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"ok": True})

    async def agent_stats(request: Request):
        project_id = int(request.path_params["project_id"])
        if store.get_project(project_id) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _is_project_member(request, project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        include_operational = request.query_params.get("include_operational") == "true"
        stats = store.agent_stats(project_id, include_operational=include_operational)
        return JSONResponse({"stats": stats, "excludes_operational": not include_operational})

    def _parse_perf_filters(request: Request):
        from_date = request.query_params.get("from")
        to_date = request.query_params.get("to")
        stage_filter = request.query_params.get("stage")
        epic_id_str = request.query_params.get("epic_id")
        status_filter = request.query_params.get("status")
        provider_filter = request.query_params.get("provider") or None
        include_operational = request.query_params.get("include_operational") == "true"

        epic_id = None
        if epic_id_str is not None:
            try:
                epic_id = int(epic_id_str)
            except ValueError:
                return None, JSONResponse({"error": "epic_id must be an integer"}, status_code=400)

        if from_date is not None:
            try:
                datetime.date.fromisoformat(from_date)
            except ValueError:
                return None, JSONResponse(
                    {"error": "from must be a valid ISO date (YYYY-MM-DD)"}, status_code=400
                )

        if to_date is not None:
            try:
                datetime.date.fromisoformat(to_date)
            except ValueError:
                return None, JSONResponse(
                    {"error": "to must be a valid ISO date (YYYY-MM-DD)"}, status_code=400
                )

        return {
            "from_date": from_date,
            "to_date": to_date,
            "stage": stage_filter,
            "epic_id": epic_id,
            "status": status_filter,
            "provider": provider_filter,
            "include_operational": include_operational,
        }, None

    async def perf_stage_stats(request: Request):
        project_id = int(request.path_params["project_id"])
        if store.get_project(project_id) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _is_project_member(request, project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        filters, err = _parse_perf_filters(request)
        if err is not None:
            return err
        return JSONResponse({"stages": store.performance_stage_stats(project_id, **filters)})

    async def perf_slowest_jobs(request: Request):
        project_id = int(request.path_params["project_id"])
        if store.get_project(project_id) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _is_project_member(request, project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        limit_param = request.query_params.get("limit", "20")
        try:
            limit = int(limit_param)
        except ValueError:
            return JSONResponse({"error": "limit must be an integer"}, status_code=400)
        filters, err = _parse_perf_filters(request)
        if err is not None:
            return err
        return JSONResponse({"jobs": store.performance_slowest_jobs(project_id, limit, **filters)})

    async def perf_headline(request: Request):
        project_id = int(request.path_params["project_id"])
        if store.get_project(project_id) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _is_project_member(request, project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        filters, err = _parse_perf_filters(request)
        if err is not None:
            return err
        return JSONResponse(store.performance_headline_stats(project_id, **filters))

    async def perf_trend(request: Request):
        project_id = int(request.path_params["project_id"])
        if store.get_project(project_id) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _is_project_member(request, project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        filters, err = _parse_perf_filters(request)
        if err is not None:
            return err
        include_operational = request.query_params.get("include_operational") == "true"
        return JSONResponse(
            store.performance_trend(
                project_id,
                from_date=filters["from_date"],
                to_date=filters["to_date"],
                include_operational=include_operational,
            )
        )

    # --- project members -------------------------------------------------

    _VALID_MEMBER_ROLES = frozenset({"viewer", "contributor", "project_admin", "automation_client"})

    def _resolve_user_ref(ref: str):
        try:
            return store.get_user_by_id(int(ref))
        except ValueError:
            return store.get_user_by_email(ref)

    async def list_project_members_route(request: Request) -> JSONResponse:
        project_id = int(request.path_params["project_id"])
        if store.get_project(project_id) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "manage_members", project_id=project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        enriched = []
        for m in store.list_project_members(project_id):
            d = member_to_dict(m)
            try:
                user = store.get_user_by_id(int(m.user_id))
            except ValueError:
                user = None
            d["user_email"] = user.email if user else None
            d["user_display_name"] = user.display_name if user else None
            d["user_not_found"] = user is None
            enriched.append(d)
        return JSONResponse({"members": enriched})

    async def add_project_member_route(request: Request) -> JSONResponse:
        project_id = int(request.path_params["project_id"])
        if store.get_project(project_id) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "manage_members", project_id=project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        user_ref = (body.get("user_id") or "").strip()
        role = (body.get("role") or "").strip()
        if not user_ref:
            return JSONResponse({"error": "user_id is required"}, status_code=400)
        if role == "platform_admin":
            return JSONResponse(
                {"error": "cannot assign platform_admin via this endpoint"}, status_code=400
            )
        if role not in _VALID_MEMBER_ROLES:
            return JSONResponse(
                {"error": f"role must be one of {sorted(_VALID_MEMBER_ROLES)}"}, status_code=400
            )
        resolved = _resolve_user_ref(user_ref)
        if resolved is None:
            return JSONResponse({"error": "user not found"}, status_code=400)
        member = store.add_project_member(project_id, str(resolved.id), role)
        return JSONResponse({"member": member_to_dict(member)}, status_code=201)

    async def remove_project_member_route(request: Request) -> JSONResponse:
        project_id = int(request.path_params["project_id"])
        user_id = request.path_params["user_id"]
        if store.get_project(project_id) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "manage_members", project_id=project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if not store.remove_project_member(project_id, user_id):
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"ok": True})

    async def update_member_role_route(request: Request) -> JSONResponse:
        project_id = int(request.path_params["project_id"])
        user_id = request.path_params["user_id"]
        if store.get_project(project_id) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "manage_members", project_id=project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        role = (body.get("role") or "").strip()
        if role == "platform_admin":
            return JSONResponse(
                {"error": "cannot assign platform_admin via this endpoint"}, status_code=400
            )
        if role not in _VALID_MEMBER_ROLES:
            return JSONResponse(
                {"error": f"role must be one of {sorted(_VALID_MEMBER_ROLES)}"}, status_code=400
            )
        member = store.update_member_role(project_id, user_id, role)
        if member is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"member": member_to_dict(member)})

    # --- project API tokens (scoped, least-privilege) ---------------------

    async def list_api_tokens_route(request: Request) -> JSONResponse:
        project_id = int(request.path_params["project_id"])
        if store.get_project(project_id) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "manage_api_tokens", project_id=project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        tokens = store.list_api_tokens(project_id)
        return JSONResponse({"tokens": [api_token_to_dict(t) for t in tokens]})

    async def create_api_token_route(request: Request) -> JSONResponse:
        project_id = int(request.path_params["project_id"])
        if store.get_project(project_id) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "manage_api_tokens", project_id=project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        name = (body.get("name") or "").strip()
        role = (body.get("role") or "").strip()
        if not name:
            return JSONResponse({"error": "name is required"}, status_code=400)
        if role == "platform_admin":
            return JSONResponse(
                {"error": "cannot mint a platform_admin token via this endpoint"}, status_code=400
            )
        if role not in _VALID_MEMBER_ROLES:
            return JSONResponse(
                {"error": f"role must be one of {sorted(_VALID_MEMBER_ROLES)}"}, status_code=400
            )
        user_email = getattr(request.state, "user_email", None)
        created_by = f"user:{user_email}" if user_email else "web:token"
        token, secret = store.create_api_token(project_id, name, role, created_by=created_by)
        payload = api_token_to_dict(token)
        payload["token"] = secret
        return JSONResponse({"token": payload}, status_code=201)

    async def revoke_api_token_route(request: Request) -> JSONResponse:
        project_id = int(request.path_params["project_id"])
        token_id = int(request.path_params["token_id"])
        if store.get_project(project_id) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "manage_api_tokens", project_id=project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if not store.revoke_api_token(project_id, token_id):
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"ok": True})

    # --- project webhooks (job-complete / deploy notifications) -----------

    async def list_webhooks_route(request: Request) -> JSONResponse:
        project_id = int(request.path_params["project_id"])
        if store.get_project(project_id) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "manage_webhooks", project_id=project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse(
            {"webhooks": [webhook_to_dict(w) for w in store.list_webhooks(project_id)]}
        )

    async def create_webhook_route(request: Request) -> JSONResponse:
        project_id = int(request.path_params["project_id"])
        if store.get_project(project_id) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "manage_webhooks", project_id=project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        url = body.get("url") or ""
        event_type = body.get("event_type") or ""
        kind = body.get("kind") or "http"
        error = _validate_webhook_config(url, event_type, kind)
        if error:
            return JSONResponse({"error": error}, status_code=400)
        user_email = getattr(request.state, "user_email", None)
        created_by = f"user:{user_email}" if user_email else "web:token"
        webhook = store.create_webhook(project_id, url.strip(), event_type, created_by, kind)
        return JSONResponse({"webhook": webhook_to_dict(webhook)}, status_code=201)

    async def set_webhook_active_route(request: Request) -> JSONResponse:
        webhook_id = int(request.path_params["webhook_id"])
        webhook = store.get_webhook(webhook_id)
        if webhook is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "manage_webhooks", project_id=webhook.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        active = body.get("active")
        if not isinstance(active, bool):
            return JSONResponse({"error": "active must be a boolean"}, status_code=400)
        updated = store.set_webhook_active(webhook_id, active)
        return JSONResponse({"webhook": webhook_to_dict(updated)})

    async def delete_webhook_route(request: Request) -> JSONResponse:
        webhook_id = int(request.path_params["webhook_id"])
        webhook = store.get_webhook(webhook_id)
        if webhook is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "manage_webhooks", project_id=webhook.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        store.delete_webhook(webhook_id)
        return JSONResponse({"deleted": True, "id": webhook_id})

    # --- project Slack credentials (bot token config) ---------------------

    async def get_slack_credentials_route(request: Request) -> JSONResponse:
        project_id = int(request.path_params["project_id"])
        if store.get_project(project_id) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "manage_webhooks", project_id=project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        token = store.get_project_slack_token(project_id)
        return JSONResponse(
            {"configured": bool(token), "status": "authenticated" if token else "not_configured"}
        )

    async def set_slack_credentials_route(request: Request) -> JSONResponse:
        project_id = int(request.path_params["project_id"])
        if store.get_project(project_id) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "manage_webhooks", project_id=project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        bot_token = body.get("bot_token") or ""
        if not isinstance(bot_token, str) or not bot_token.strip():
            return JSONResponse({"error": "bot_token is required"}, status_code=400)
        user_email = getattr(request.state, "user_email", None)
        created_by = f"user:{user_email}" if user_email else "web:token"
        store.set_project_slack_token(project_id, bot_token.strip(), created_by)
        return JSONResponse({"configured": True, "status": "authenticated"})

    # --- live worker fleet (command & control) ---------------------------
    _JUDGMENT_CLASSES = frozenset(
        {
            FailureClass.genuine_code,
            FailureClass.merge_conflict_exhausted,
            FailureClass.config_deploy,
            FailureClass.isolation_leak,
            FailureClass.unknown,
        }
    )

    def _supervisor() -> dict:
        now = time.time()
        stale = float(getattr(config, "pipeline_lease_ttl", 90)) * 1.5
        supervisors = []
        for w in store.list_workers(now, stale):
            if w.get("role") != "supervisor":
                continue
            supervisors.append(
                {
                    "id": w["id"],
                    "host": w["host"],
                    "pid": w["pid"],
                    "status": w["status"],
                    "alive": w["alive"],
                    "last_seen_ago": max(0.0, now - (w["last_seen"] or now)),
                }
            )
        needs_judgment = []
        dead_lettered = []
        for job in store.get_failed_jobs():
            fc = classify_failure(job)
            requeue_count = store.supervisor_requeue_count(job.id)
            is_notified = store.is_supervisor_notified(job.id)
            entry = {
                "job_id": job.id,
                "project_id": job.project_id,
                "idea": job.idea,
                "stage": job.stage.value,
                "failure_class": fc.value,
                "supervisor_requeue_count": requeue_count,
                "is_supervisor_notified": is_notified,
            }
            if fc in _JUDGMENT_CLASSES:
                needs_judgment.append(entry)
            elif fc == FailureClass.transient and requeue_count >= 5:
                dead_lettered.append(entry)
        remediation_feed = store.list_supervisor_events(limit=100)
        auto_actions = []
        for r in store.list_supervisor_requeues():
            job = store.get(r["job_id"])
            auto_actions.append(
                {
                    "job_id": r["job_id"],
                    "idea": job.idea if job else "",
                    "count": r["count"],
                }
            )
        return {
            "supervisors": supervisors,
            "needs_judgment": needs_judgment,
            "dead_lettered": dead_lettered,
            "auto_actions": auto_actions,
            "remediation_feed": remediation_feed,
            "now": now,
        }

    async def get_me_inner(request: Request) -> JSONResponse:
        is_pa = getattr(request.state, "is_platform_admin", False)
        user_id = getattr(request.state, "user_id", None)
        memberships: dict[str, str] = {}
        if not is_pa and user_id is not None:
            mems = store.get_user_memberships(user_id)
            memberships = {str(m.project_id): m.role for m in mems}
        if is_pa:
            platform_permissions = sorted(ASSIGNABLE_PLATFORM_PERMISSIONS)
        elif user_id is not None:
            platform_permissions = store.list_platform_permissions(user_id)
        else:
            platform_permissions = []
        return JSONResponse(
            {
                "id": user_id,
                "email": getattr(request.state, "user_email", None),
                "display_name": getattr(request.state, "user_display_name", None) or "",
                "is_platform_admin": is_pa,
                "role": "platform_admin" if is_pa else "viewer",
                "memberships": memberships,
                "platform_permissions": platform_permissions,
            }
        )

    async def supervisor_snapshot(request: Request):
        if not _caller_has_permission(request, "view_fleet"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse(_supervisor())

    async def supervisor_stream(request: Request):
        if not _caller_has_permission(request, "view_fleet"):
            return JSONResponse({"error": "forbidden"}, status_code=403)

        async def gen():
            while not await request.is_disconnected():
                yield {"event": "supervisor", "data": json.dumps(_supervisor())}
                await asyncio.sleep(1.5)

        return EventSourceResponse(gen())

    async def workers(request: Request):
        if not _caller_has_permission(request, "view_fleet"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse(_fleet_snapshot(store, config))

    async def workers_stream(request: Request):
        if not _caller_has_permission(request, "view_fleet"):
            return JSONResponse({"error": "forbidden"}, status_code=403)

        async def gen():
            while not await request.is_disconnected():
                try:
                    payload = json.dumps(_fleet_snapshot(store, config))
                except Exception:
                    log.exception("fleet snapshot tick failed, skipping")
                    await asyncio.sleep(1.5)
                    continue
                yield {"event": "workers", "data": payload}
                await asyncio.sleep(1.5)

        return EventSourceResponse(gen())

    async def usage(request: Request):
        since = request.query_params.get("since") or None
        project_id_param = request.query_params.get("project_id") or None
        project_id = int(project_id_param) if project_id_param is not None else None
        if project_id is not None:
            if not _is_project_member(request, project_id):
                return JSONResponse({"error": "forbidden"}, status_code=403)
        elif not _caller_has_permission(request, "view_audit"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        include_operational = request.query_params.get("include_operational") == "true"
        return JSONResponse(
            store.usage_summary(
                since=since, project_id=project_id, include_operational=include_operational
            )
        )

    async def jobs_filed_by_breakdown_route(request: Request) -> JSONResponse:
        if not _caller_has_permission(request, "view_audit"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        since = request.query_params.get("since") or None
        return JSONResponse({"breakdown": store.jobs_filed_by_breakdown(since=since)})

    async def deployment_reliability_route(request: Request) -> JSONResponse:
        if not _caller_has_permission(request, "view_audit"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        since = request.query_params.get("since") or None
        return JSONResponse({"breakdown": store.deployment_reliability_breakdown(since=since)})

    async def list_providers_route(request: Request) -> JSONResponse:
        if not _caller_has_permission(request, "manage_providers"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        now = time.time()
        paused = store.paused_providers(now)
        pauses_list = store.list_provider_pauses(now)
        pauses_map = {p["provider"]: p for p in pauses_list}
        result = []
        for p in known_providers():
            pause = pauses_map.get(p)
            paused_until_iso = (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(pause["until"])) if pause else None
            )
            result.append(
                {"provider": p, "paused": p in paused, "paused_until_iso": paused_until_iso}
            )
        return JSONResponse({"providers": result})

    async def clear_provider_pause(request: Request) -> JSONResponse:
        if not _caller_has_permission(request, "manage_providers"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        provider = request.path_params["provider"]
        if provider not in known_providers():
            return JSONResponse({"error": "unknown provider"}, status_code=404)
        store.set_provider_pause(provider, 0.0)
        return JSONResponse({"ok": True})

    async def jobs_stream(request: Request):
        async def gen():
            last = None
            while not await request.is_disconnected():
                jobs = store.list_active(50)
                if not getattr(request.state, "is_platform_admin", False):
                    jobs = [
                        job
                        for job in jobs
                        if job.project_id is not None
                        and _is_project_member(request, job.project_id)
                    ]
                payload = json.dumps(
                    [job_to_dict(j, **job_projection_extras(store, j)) for j in jobs],
                    sort_keys=True,
                )
                if payload != last:
                    last = payload
                    yield {"event": "jobs", "data": payload}
                await asyncio.sleep(2)

        return EventSourceResponse(gen())

    async def job_logs_stream(request: Request):
        job_id = int(request.path_params["job_id"])
        try:
            after_id = int(request.query_params.get("after_id", 0))
        except ValueError:
            return Response("after_id must be an integer", status_code=400)

        _job = store.get(job_id)
        if _job is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if _job.project_id is not None and not _is_project_member(request, _job.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)

        async def gen():
            nonlocal after_id
            while not await request.is_disconnected():
                rows = store.tail_logs(job_id, after_id)
                if rows:
                    after_id = rows[-1]["id"]
                    yield {"event": "log", "data": json.dumps(rows)}
                else:
                    job = store.get(job_id)
                    if job is None or job.status.value in ("done", "failed", "cancelled"):
                        return
                await asyncio.sleep(0.3)

        return EventSourceResponse(gen())

    async def job_detail_stream(request: Request):
        job_id = int(request.path_params["job_id"])
        job = store.get(job_id)
        if job is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if job.project_id is not None and not _is_project_member(request, job.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)

        async def gen():
            last = None
            while not await request.is_disconnected():
                current = store.get(job_id)
                if current is None:
                    return
                payload = json.dumps(
                    {
                        "job": job_to_dict(current, **job_projection_extras(store, current)),
                        "events": store.list_events(job_id),
                    },
                    sort_keys=True,
                )
                if payload != last:
                    last = payload
                    yield {"event": "job", "data": payload}
                if current.status in (
                    JobStatus.DONE,
                    JobStatus.FAILED,
                    JobStatus.CANCELLED,
                ):
                    return
                await asyncio.sleep(2)

        return EventSourceResponse(gen())

    # --- streaming AI endpoints (SSE) ---------------------------------------

    async def chat_stream(request: Request):
        # Same host-shell exposure as `chat` above — same gate.
        if not _caller_has_permission(request, "admin_console"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        text = (body.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "empty message"}, status_code=400)

        async def gen():
            try:
                async for event in orchestrator.stream_ask(_orchestrator_session_id(request), text):
                    yield {"data": json.dumps(event)}
            except Exception as exc:
                yield {"data": json.dumps({"type": "error", "message": str(exc)})}

        return EventSourceResponse(gen())

    _ALLOWED_IMAGE_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    _MAX_B64_BYTES = 5 * 1024 * 1024

    def _validate_content(content):
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    if not isinstance(block.get("text"), str):
                        raise ValueError("text block 'text' field must be a string")
                elif btype == "image":
                    source = block.get("source", {})
                    if source.get("type") != "base64":
                        raise ValueError("image block source.type must be 'base64'")
                    media_type = source.get("media_type")
                    if media_type not in _ALLOWED_IMAGE_MEDIA_TYPES:
                        raise ValueError(
                            f"unsupported image media_type {media_type!r}; "
                            f"allowed: {sorted(_ALLOWED_IMAGE_MEDIA_TYPES)}"
                        )
                    data = source.get("data", "")
                    if len(data) > _MAX_B64_BYTES:
                        raise ValueError(
                            f"image base64 data exceeds 5 MB limit ({len(data)} bytes)"
                        )
                else:
                    raise ValueError(f"unsupported content block type: {btype!r}")
            return content
        raise ValueError("content must be a string or list of content blocks")

    def _content_to_text(content):
        if isinstance(content, str):
            return content
        return " ".join(block["text"] for block in content if block.get("type") == "text")

    async def job_chat_stream(request: Request):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        project_id = body.get("project_id")
        messages = body.get("messages") or []
        epic_id = body.get("epic_id")
        session_id_in = body.get("session_id")
        if not project_id:
            return JSONResponse({"error": "project_id is required"}, status_code=400)
        if not messages:
            return JSONResponse({"error": "messages is required"}, status_code=400)
        try:
            _validate_content(messages[-1].get("content", ""))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        project = store.get_project(int(project_id))
        if project is None:
            return JSONResponse({"error": "project not found"}, status_code=404)
        if not _is_project_member(request, project.id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        user_email = getattr(request.state, "user_email", None) or ""
        if epic_id is not None:
            epic = store.get_epic(int(epic_id))
            if epic is None:
                return JSONResponse({"error": "epic not found"}, status_code=404)
        is_new_session = session_id_in is None
        if is_new_session:
            _sess = store.create_chat_session(int(project_id), user_email)
            session_id = _sess.session_id
        else:
            session_id = session_id_in
        project_tools, _ = build_project_tools(store, project)
        epics = store.list_epics(project_id=int(project_id), include_archived=False)
        epics_block = ""
        if epics:
            epics_block = "Active epics:\n" + "\n".join(
                f"  - [{e['id']}] {e['name']}"
                + (f": {e['description']}" if e.get("description") else "")
                for e in epics
            )
        if epic_id is not None:
            epics_block += f"\nCurrent epic: [{epic.id}] {epic.name}"
        prior_messages = messages[:-1]
        history_block = ""
        if prior_messages:
            lines = []
            for m in prior_messages:
                label = "User" if m.get("role") == "user" else "Assistant"
                lines.append(f"{label}: {_content_to_text(m.get('content', ''))}")
            history_block = (
                "--- Prior conversation ---\n"
                + "\n\n".join(lines)
                + "\n--- End prior conversation ---\n\n"
            )
        wt_path = None
        if _is_self_repo(project.repo_path):
            branch = await gitops.default_branch(project.repo_path)
            wt_path = config.data_dir / "chat-wt" / str(uuid.uuid4())[:8]
            wt_path.parent.mkdir(parents=True, exist_ok=True)
            await gitops.git(
                project.repo_path,
                "worktree",
                "add",
                "--detach",
                str(wt_path),
                f"origin/{branch}",
            )
            chat_cwd = wt_path
        else:
            chat_cwd = Path(project.repo_path)

        symbol_index_text = store.get_symbol_index_text(int(project_id))
        prompt = f"Project: {project.name}\nRepo: {str(chat_cwd)}\n"
        if epics_block:
            prompt += epics_block + "\n"
        if symbol_index_text:
            prompt += (
                "\nSymbol index (file:line symbols for DIAGNOSE citations):\n"
                + symbol_index_text
                + "\n"
            )
        if history_block:
            prompt += "\n" + history_block
        last_content = messages[-1].get("content", "")
        if isinstance(last_content, str):
            prompt += f"User: {last_content}"
        else:
            context_block = {"type": "text", "text": prompt.rstrip()}
            prompt = [context_block] + last_content

        if not prior_messages:
            symbol_text = store.get_symbol_index_text(int(project_id))
            if symbol_text:
                symbol_header = "## Project symbol index\n" + symbol_text + "\n\n"
                if isinstance(prompt, str):
                    prompt = symbol_header + prompt
                else:
                    prompt = [{"type": "text", "text": symbol_header.rstrip()}] + prompt

        async def gen():
            try:
                if is_new_session:
                    yield {"data": json.dumps({"type": "session", "session_id": session_id})}
                assembled_text = ""
                result_jobs: list[dict] = []
                try:
                    async for event in _suggest_backend.stream(
                        prompt=prompt,
                        cwd=chat_cwd,
                        role=Role.EXPLORER,
                        append_system=JOB_CHAT_SYS,
                        mcp_servers={"project-tools": project_tools},
                    ):
                        if event["type"] == "text":
                            assembled_text += event.get("delta", "")
                            yield {"data": json.dumps(event)}
                        elif event["type"] != "result":
                            yield {"data": json.dumps(event)}
                        else:
                            try:
                                data = parse_result_block(event["text"])
                                result_jobs = data.get("jobs", [])
                                if not isinstance(result_jobs, list):
                                    result_jobs = []
                            except ResultBlockError:
                                result_jobs = []
                            yield {
                                "data": json.dumps(
                                    {"type": "result", "jobs": result_jobs, "raw": event["text"]}
                                )
                            }
                    full_messages = list(messages) + [
                        {"role": "assistant", "content": assembled_text}
                    ]
                    try:
                        store.update_chat_session(
                            session_id,
                            messages=full_messages,
                            proposed_jobs=result_jobs,
                        )
                    except Exception:
                        log.exception("failed to persist chat session %s", session_id)
                except Exception as exc:
                    log.exception("job chat stream failed")
                    yield {"data": json.dumps({"type": "error", "message": str(exc)})}
            finally:
                if wt_path is not None:
                    await gitops.remove_worktree(project.repo_path, wt_path)

        return EventSourceResponse(gen())

    def _chat_session_to_dict(sess: ChatSession) -> dict:
        return {
            "id": sess.id,
            "session_id": sess.session_id,
            "project_id": sess.project_id,
            "created_by": sess.created_by,
            "messages": sess.messages,
            "proposed_jobs": sess.proposed_jobs,
            "status": sess.status,
            "created_at": sess.created_at,
            "updated_at": sess.updated_at,
        }

    async def list_project_chat_sessions(request: Request):
        project_id = int(request.path_params["id"])
        if not _is_project_member(request, project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        user_email = getattr(request.state, "user_email", None) or ""
        sessions = store.list_chat_sessions(project_id, user_email)
        return JSONResponse({"sessions": [_chat_session_to_dict(s) for s in sessions]})

    async def get_chat_session_detail(request: Request):
        session_id = request.path_params["session_id"]
        sess = store.get_chat_session(session_id)
        if sess is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        user_email = getattr(request.state, "user_email", None) or ""
        if sess.created_by != user_email:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse(_chat_session_to_dict(sess))

    async def suggest_epic_features_stream(request: Request):
        epic_id = request.path_params["epic_id"]
        epic = store.get_epic(epic_id)
        if epic is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        # These routes run an agent with Bash inside project.repo_path, on an
        # attacker-controllable prompt — membership is mandatory before that.
        if not _is_project_member(request, epic.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        project = store.get_project(epic.project_id)
        if project is None:
            return JSONResponse({"error": "project not found"}, status_code=404)
        cwd = project.repo_path
        prompt = (
            f"Project: {project.name if project else 'unknown'}\n"
            f"Repository: {cwd}\n"
            f"Epic name: {epic.name}\n"
            f"Epic description: {epic.description or '(none provided)'}\n\n"
            "Suggest 3-7 concrete sub-features to build for this epic."
        )

        async def gen():
            try:
                async for event in _suggest_backend.stream(
                    prompt=prompt,
                    cwd=cwd,
                    role=Role.PLANNER,
                    append_system=_SUGGEST_SYS,
                    max_turns=10,
                ):
                    if event["type"] != "result":
                        yield {"data": json.dumps(event)}
                    else:
                        try:
                            data = parse_result_block(event["text"])
                        except ResultBlockError:
                            data = None
                        if not data or not isinstance(data.get("suggestions"), list):
                            yield {
                                "data": json.dumps(
                                    {
                                        "type": "error",
                                        "message": "model returned no valid suggestions",
                                    }
                                )
                            }
                            return
                        yield {
                            "data": json.dumps(
                                {
                                    "type": "result",
                                    "suggestions": data["suggestions"],
                                    "raw": event["text"],
                                }
                            )
                        }
            except Exception as exc:
                log.exception("suggest stream failed for epic %s", epic_id)
                yield {"data": json.dumps({"type": "error", "message": str(exc)})}

        return EventSourceResponse(gen())

    async def architect_plan_stream(request: Request):
        epic_id = request.path_params["epic_id"]
        epic = store.get_epic(epic_id)
        if epic is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        # These routes run an agent with Bash inside project.repo_path, on an
        # attacker-controllable prompt — membership is mandatory before that.
        if not _is_project_member(request, epic.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        project = store.get_project(epic.project_id)
        if project is None:
            return JSONResponse({"error": "project not found"}, status_code=404)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        goal = (body.get("goal") or "").strip()
        if not goal:
            return JSONResponse({"error": "goal is required"}, status_code=400)
        cwd = project.repo_path
        symbol_index_text = store.get_symbol_index_text(project.id)
        decision_digest = load_digest(cwd)
        prompt = agents._build_architect_prompt(
            project.name,
            cwd,
            epic.name,
            epic.description,
            goal,
            symbol_index_text,
            decision_digest,
        )

        async def gen():
            try:
                async for event in agents.architect(_suggest_backend, prompt, cwd):
                    yield {"data": json.dumps(event)}
            except Exception as exc:
                log.exception("architect plan stream failed for epic %s", epic_id)
                yield {"data": json.dumps({"type": "error", "message": str(exc)})}

        return EventSourceResponse(gen())

    # --- auth endpoints (login / logout) ------------------------------------

    async def login(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        email = (body.get("email") or "").strip()
        password = body.get("password") or ""
        if not email or not password:
            return JSONResponse({"error": "email and password are required"}, status_code=400)
        now = time.time()
        source_ip = _resolve_source_ip(request, config.trusted_proxy_depth)
        retry_after = _login_throttled(_login_rate_limit_hits_by_ip, source_ip, now)
        if retry_after is None:
            retry_after = _login_throttled(_login_rate_limit_hits_by_email, email.lower(), now)
        if retry_after is not None:
            return JSONResponse(
                {"error": "too many login attempts"},
                status_code=429,
                headers={"Retry-After": str(int(retry_after))},
            )
        user = store.authenticate_user(email, password)
        if user is None:
            return JSONResponse({"error": "invalid credentials"}, status_code=401)
        token = store.create_session(user.id)
        return JSONResponse(
            {
                "token": token,
                "user": {
                    "id": user.id,
                    "email": user.email,
                    "display_name": user.display_name,
                    "is_platform_admin": user.is_platform_admin,
                },
            }
        )

    async def logout(request: Request) -> Response:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:]
        else:
            token = request.query_params.get("token", "") or request.cookies.get(
                SESSION_COOKIE_NAME, ""
            )
        if token:
            store.delete_session(token)
        resp = Response(status_code=204)
        resp.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return resp

    # --- admin user-management routes (S4) ----------------------------------

    async def admin_audit_log(request: Request) -> JSONResponse:
        if not _caller_has_permission(request, "view_audit"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        q = request.query_params
        try:
            limit = int(q.get("limit", "200"))
            before_id = int(q["before_id"]) if "before_id" in q else None
        except ValueError:
            return JSONResponse({"error": "limit/before_id must be integers"}, status_code=400)
        rows = store.list_audit(
            table=q.get("table") or None,
            actor=q.get("actor") or None,
            row_pk=q.get("row_pk") or None,
            limit=limit,
            before_id=before_id,
        )
        return JSONResponse({"entries": rows})

    async def admin_page_views(request: Request) -> JSONResponse:
        if not _caller_has_permission(request, "view_audit"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        q = request.query_params
        now = datetime.datetime.now(datetime.timezone.utc)
        since = q.get("since") or (now - datetime.timedelta(days=30)).isoformat()
        until = q.get("until") or now.isoformat()
        summary = store.page_view_admin_summary(
            since=since, until=until, path=q.get("path") or None
        )
        return JSONResponse(summary.to_dict())

    async def list_admin_users(request: Request) -> JSONResponse:
        if not _caller_has_permission(request, "manage_users"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        users = store.list_users()
        return JSONResponse(
            {
                "users": [
                    {
                        "id": u.id,
                        "email": u.email,
                        "display_name": u.display_name,
                        "is_platform_admin": u.is_platform_admin,
                        "is_active": u.is_active,
                        "created_at": u.created_at,
                        "platform_permissions": store.list_platform_permissions(u.id),
                    }
                    for u in users
                ]
            }
        )

    async def grant_user_platform_permission(request: Request) -> JSONResponse:
        if not getattr(request.state, "is_platform_admin", False):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        target_id = int(request.path_params["user_id"])
        if store.get_user_by_id(target_id) is None:
            return JSONResponse({"error": "user not found"}, status_code=404)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        permission = (body.get("permission") or "").strip()
        if permission not in ASSIGNABLE_PLATFORM_PERMISSIONS:
            return JSONResponse({"error": "unknown permission"}, status_code=400)
        granted_by = getattr(request.state, "user_email", "") or ""
        store.grant_platform_permission(target_id, permission, granted_by)
        return JSONResponse({"ok": True})

    async def revoke_user_platform_permission(request: Request) -> JSONResponse:
        if not getattr(request.state, "is_platform_admin", False):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        target_id = int(request.path_params["user_id"])
        permission = request.path_params["permission"]
        if permission not in ASSIGNABLE_PLATFORM_PERMISSIONS:
            return JSONResponse({"error": "unknown permission"}, status_code=400)
        if store.get_user_by_id(target_id) is None:
            return JSONResponse({"error": "user not found"}, status_code=404)
        store.revoke_platform_permission(target_id, permission)
        return JSONResponse({"ok": True})

    async def create_admin_user(request: Request) -> JSONResponse:
        if not _caller_has_permission(request, "manage_users"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        email = (body.get("email") or "").strip()
        password = body.get("password") or ""
        display_name = (body.get("display_name") or "").strip()
        is_platform_admin = bool(body.get("is_platform_admin", False))
        if not email:
            return JSONResponse({"error": "email is required"}, status_code=400)
        if not password:
            return JSONResponse({"error": "password is required"}, status_code=400)
        if store.get_user_by_email(email) is not None:
            return JSONResponse({"error": "email already exists"}, status_code=409)
        u = store.create_user(
            email, password, display_name=display_name, is_platform_admin=is_platform_admin
        )
        return JSONResponse(
            {
                "id": u.id,
                "email": u.email,
                "display_name": u.display_name,
                "is_platform_admin": u.is_platform_admin,
                "is_active": u.is_active,
                "created_at": u.created_at,
            },
            status_code=201,
        )

    async def update_admin_user(request: Request) -> JSONResponse:
        if not _caller_has_permission(request, "manage_users"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        target_id = int(request.path_params["user_id"])
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        kwargs: dict = {}
        if "display_name" in body:
            kwargs["display_name"] = (body["display_name"] or "").strip()
        if "is_active" in body:
            kwargs["is_active"] = bool(body["is_active"])
        if "is_platform_admin" in body:
            kwargs["is_platform_admin"] = bool(body["is_platform_admin"])
        u = store.update_user(target_id, **kwargs)
        if u is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if kwargs.get("is_active") is False:
            store.delete_user_sessions(target_id)
        return JSONResponse(
            {
                "id": u.id,
                "email": u.email,
                "display_name": u.display_name,
                "is_platform_admin": u.is_platform_admin,
                "is_active": u.is_active,
                "created_at": u.created_at,
            }
        )

    async def get_user_memberships_route(request: Request) -> JSONResponse:
        if not _caller_has_permission(request, "manage_users"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        target_id = int(request.path_params["user_id"])
        if store.get_user_by_id(target_id) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        memberships = []
        for m in store.get_user_memberships(target_id):
            project = store.get_project(m.project_id)
            memberships.append(
                {
                    "project_id": m.project_id,
                    "project_name": project.name if project else None,
                    "role": m.role,
                }
            )
        return JSONResponse({"memberships": memberships})

    async def delete_admin_user_route(request: Request) -> JSONResponse:
        if not _caller_has_permission(request, "manage_users"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        target_id = int(request.path_params["user_id"])
        if int(getattr(request.state, "user_id", None) or -1) == target_id:
            return JSONResponse({"error": "cannot delete your own account"}, status_code=403)
        if store.get_user_by_id(target_id) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        store.delete_user(target_id)
        return JSONResponse({"deleted": True})

    async def assign_user_to_project_route(request: Request) -> JSONResponse:
        if not _caller_has_permission(request, "manage_users"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        target_user_id = int(request.path_params["user_id"])
        if store.get_user_by_id(target_user_id) is None:
            return JSONResponse({"error": "user not found"}, status_code=404)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        project_id = body.get("project_id")
        role = (body.get("role") or "").strip()
        if not isinstance(project_id, int):
            return JSONResponse({"error": "project_id is required"}, status_code=400)
        if store.get_project(project_id) is None:
            return JSONResponse({"error": "project not found"}, status_code=404)
        if role == "platform_admin":
            return JSONResponse(
                {"error": "cannot assign platform_admin via this endpoint"}, status_code=400
            )
        if role not in _VALID_MEMBER_ROLES:
            return JSONResponse(
                {"error": f"role must be one of {sorted(_VALID_MEMBER_ROLES)}"}, status_code=400
            )
        member = store.add_project_member(project_id, str(target_user_id), role)
        return JSONResponse({"member": member_to_dict(member)}, status_code=201)

    # --- admin invitation endpoints -----------------------------------------

    def _invitation_to_dict(inv) -> dict:
        return {
            "id": inv.id,
            "token": inv.token,
            "email": inv.email,
            "invited_by": inv.invited_by,
            "expires_at": inv.expires_at,
            "consumed_at": inv.consumed_at,
            "created_at": inv.created_at,
        }

    async def create_invitation_route(request: Request) -> JSONResponse:
        if not _caller_has_permission(request, "manage_invitations"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        email = (body.get("email") or "").strip().lower()
        if not email:
            return JSONResponse({"error": "email is required"}, status_code=400)
        ttl_days = int(body.get("ttl_days") or 7)
        invited_by = getattr(request.state, "user_email", "") or ""
        inv = store.create_invitation(email, invited_by=invited_by, ttl_days=ttl_days)
        return JSONResponse({"invitation": _invitation_to_dict(inv)}, status_code=201)

    async def list_invitations_route(request: Request) -> JSONResponse:
        if not _caller_has_permission(request, "manage_invitations"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        invs = store.list_invitations()
        return JSONResponse({"invitations": [_invitation_to_dict(i) for i in invs]})

    async def revoke_invitation_route(request: Request) -> Response:
        if not _caller_has_permission(request, "manage_invitations"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        token = request.path_params["token"]
        if store.revoke_invitation(token):
            return JSONResponse({"ok": True})
        return JSONResponse({"error": "not found"}, status_code=404)

    # --- intake session endpoints -------------------------------------------

    async def create_intake_session_route(request: Request) -> JSONResponse:
        if not _caller_has_permission(request, "create_project"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        created_by = str(getattr(request.state, "user_id", None) or "web")
        session = store.create_intake_session(created_by)
        return JSONResponse({"session": intake_session_to_dict(session)}, status_code=201)

    async def list_intake_sessions_route(request: Request) -> JSONResponse:
        if not _caller_has_permission(request, "create_project"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        created_by = str(getattr(request.state, "user_id", None) or "web")
        sessions = store.list_intake_sessions(created_by)
        return JSONResponse({"sessions": [intake_session_to_dict(s) for s in sessions]})

    async def get_intake_session_route(request: Request) -> JSONResponse:
        if not _caller_has_permission(request, "create_project"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        session_id = request.path_params["session_id"]
        session = store.get_intake_session(session_id)
        if session is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        is_pa = getattr(request.state, "is_platform_admin", False)
        if not is_pa:
            caller_id = str(getattr(request.state, "user_id", None) or "web")
            if session.created_by != caller_id:
                return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"session": intake_session_to_dict(session)})

    async def intake_message_route(request: Request) -> JSONResponse:
        if not _caller_has_permission(request, "create_project"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        session_id = request.path_params["session_id"]
        session = store.get_intake_session(session_id)
        if session is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        is_pa = getattr(request.state, "is_platform_admin", False)
        if not is_pa:
            caller_id = str(getattr(request.state, "user_id", None) or "web")
            if session.created_by != caller_id:
                return JSONResponse({"error": "not found"}, status_code=404)
        if session.status != "in_progress":
            return JSONResponse({"error": "session is not in_progress"}, status_code=409)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        message = (body.get("message") or "").strip()
        if not message:
            return JSONResponse({"error": "message is required"}, status_code=400)
        try:
            reply, new_spec, phase, complete = await interview_turn(
                _intake_backend,
                session.messages,
                session.draft_spec,
                message,
            )
        except Exception as exc:
            log.exception("interview_turn failed for session %s", session_id)
            return JSONResponse({"error": str(exc)}, status_code=500)
        new_messages = session.messages + [
            {"role": "user", "content": message},
            {"role": "assistant", "content": reply},
        ]
        store.update_intake_session(
            session_id,
            draft_spec=new_spec,
            messages=new_messages,
        )
        updated_session = store.get_intake_session(session_id)
        missing = check_completeness(new_spec)
        return JSONResponse(
            {
                "reply": reply,
                "draft_spec": new_spec,
                "missing": missing,
                "phase": phase,
                "session": intake_session_to_dict(updated_session),
            }
        )

    async def plan_intake_session_route(request: Request) -> JSONResponse:
        if not _caller_has_permission(request, "create_project"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        session_id = request.path_params["session_id"]
        session = store.get_intake_session(session_id)
        if session is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        is_pa = getattr(request.state, "is_platform_admin", False)
        if not is_pa:
            caller_id = str(getattr(request.state, "user_id", None) or "web")
            if session.created_by != caller_id:
                return JSONResponse({"error": "not found"}, status_code=404)
        missing = check_completeness(session.draft_spec)
        if missing:
            return JSONResponse({"error": "spec incomplete", "missing": missing}, status_code=400)
        plan = plan_seed_from_spec(session.draft_spec)
        return JSONResponse(plan)

    async def confirm_intake_session_route(request: Request) -> JSONResponse:
        if not _caller_has_permission(request, "create_project"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        session_id = request.path_params["session_id"]
        session = store.get_intake_session(session_id)
        if session is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        is_pa = getattr(request.state, "is_platform_admin", False)
        if not is_pa:
            caller_id = str(getattr(request.state, "user_id", None) or "web")
            if session.created_by != caller_id:
                return JSONResponse({"error": "not found"}, status_code=404)
        if session.status != "in_progress":
            return JSONResponse({"error": "session is not in_progress"}, status_code=409)
        missing = check_completeness(session.draft_spec)
        if missing:
            return JSONResponse(
                {"error": "spec is incomplete", "missing": missing}, status_code=422
            )
        user_id = getattr(request.state, "user_id", None)
        try:
            result = await seed_project_from_spec(
                store,
                config.projects_dir,
                session.draft_spec,
                chat_id=0,
                session=session,
                creator_id=str(user_id) if user_id is not None else None,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            log.exception("seed_project_from_spec failed in intake confirm")
            return JSONResponse({"error": f"provisioning failed: {exc}"}, status_code=500)
        project = result["project"]
        if user_id is not None:
            store.add_project_member(project.id, str(user_id), "project_admin")
        store.confirm_intake_session(session_id, project.id)
        return JSONResponse(
            {
                "project": project_to_dict(project),
                "board_url": f"/projects/{project.id}",
                "plan": plan_seed_from_spec(session.draft_spec),
            },
            status_code=201,
        )

    async def abandon_intake_session_route(request: Request) -> JSONResponse:
        if not _caller_has_permission(request, "create_project"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        session_id = request.path_params["session_id"]
        session = store.get_intake_session(session_id)
        if session is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        is_pa = getattr(request.state, "is_platform_admin", False)
        if not is_pa:
            caller_id = str(getattr(request.state, "user_id", None) or "web")
            if session.created_by != caller_id:
                return JSONResponse({"error": "not found"}, status_code=404)
        store.update_intake_session(session_id, status="abandoned")
        return JSONResponse({"ok": True})

    async def patch_intake_draft_spec(request: Request) -> JSONResponse:
        if not _caller_has_permission(request, "create_project"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        session_id = request.path_params["session_id"]
        session = store.get_intake_session(session_id)
        if session is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        is_pa = getattr(request.state, "is_platform_admin", False)
        if not is_pa:
            caller_id = str(getattr(request.state, "user_id", None) or "web")
            if session.created_by != caller_id:
                return JSONResponse({"error": "not found"}, status_code=404)
        if session.status != "in_progress":
            return JSONResponse({"error": "session is not in_progress"}, status_code=409)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        draft_spec_patch = body.get("draft_spec")
        base_updated_at = body.get("base_updated_at")
        if not isinstance(draft_spec_patch, dict):
            return JSONResponse({"error": "draft_spec must be a dict"}, status_code=400)
        if base_updated_at != session.updated_at:
            return JSONResponse(
                {"error": "stale base_updated_at; wait for the AI turn to finish"},
                status_code=409,
            )
        merged = {**session.draft_spec, **draft_spec_patch}
        store.update_intake_session(session_id, draft_spec=merged)
        updated_session = store.get_intake_session(session_id)
        missing = check_completeness(merged)
        return JSONResponse(
            {"session": intake_session_to_dict(updated_session), "missing": missing}
        )

    async def patch_job_title(request: Request) -> JSONResponse:
        job_id = int(request.path_params["job_id"])
        _job = store.get(job_id)
        if _job is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "edit_job_deps", project_id=_job.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        title = (body.get("title") or "").strip()
        title_error = _job_title_error(title)
        if title_error is not None:
            status_code, message = title_error
            return JSONResponse({"error": message}, status_code=status_code)
        store.update_job_title(job_id, title)
        job = store.get(job_id)
        return JSONResponse({"job": job_to_dict(job, **job_projection_extras(store, job))})

    async def intake_message_stream_route(request: Request):
        if not _caller_has_permission(request, "create_project"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        session_id = request.path_params["session_id"]
        session = store.get_intake_session(session_id)
        if session is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        is_pa = getattr(request.state, "is_platform_admin", False)
        if not is_pa:
            caller_id = str(getattr(request.state, "user_id", None) or "web")
            if session.created_by != caller_id:
                return JSONResponse({"error": "not found"}, status_code=404)
        if session.status != "in_progress":
            return JSONResponse({"error": "session is not in progress"}, status_code=400)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        message = (body.get("message") or "").strip()
        if not message:
            return JSONResponse({"error": "message is required"}, status_code=400)

        async def gen():
            try:
                async for event in stream_interview_turn(
                    _intake_backend,
                    session.messages,
                    session.draft_spec,
                    message,
                ):
                    if event["type"] == "result":
                        new_spec = event.get("draft_spec", session.draft_spec)
                        new_messages = [
                            *session.messages,
                            {"role": "user", "content": message},
                            {"role": "assistant", "content": event.get("text", "")},
                        ]
                        updated_session = store.update_intake_session(
                            session_id,
                            draft_spec=new_spec,
                            messages=new_messages,
                        )
                        event = {**event, "session_updated_at": updated_session.updated_at}
                    yield {"data": json.dumps(event)}
            except Exception as exc:
                log.exception("intake stream failed for session %s", session_id)
                yield {"data": json.dumps({"type": "error", "message": str(exc)})}

        return EventSourceResponse(gen())

    # --- deploy status endpoint ----------------------------------------------

    async def get_project_deploy_status(request: Request) -> JSONResponse:
        project_id = int(request.path_params["id"])
        project = store.get_project(project_id)
        if project is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _is_project_member(request, project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        last_deploy = store.get_last_deploy(project_id)
        base = "main"
        main_tip = ""
        try:
            fetch_proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                project.repo_path,
                "fetch",
                "origin",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(fetch_proc.communicate(), timeout=15)
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                project.repo_path,
                "rev-parse",
                f"origin/{base}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode == 0:
                main_tip = out.decode().strip()
        except (OSError, asyncio.TimeoutError) as exc:
            log.debug("get_project_deploy_status: git failed: %s", exc)
        active_deploy = store.get_active_deploy_job(project_id)
        deployed_sha = last_deploy["deployed_commit"] if last_deploy else None
        deployed_at = last_deploy["finished_at"] if last_deploy else None
        is_stale = bool(main_tip) and deployed_sha != main_tip
        return JSONResponse(
            {
                "deployed_sha": deployed_sha,
                "deployed_at": deployed_at,
                "main_tip": main_tip,
                "is_stale": is_stale,
                "deploy_in_flight": active_deploy is not None,
                "deploy_job_id": active_deploy.id if active_deploy else None,
            }
        )

    # --- runtime status endpoints (read-only, live docker/compose state) -----

    def _default_public_url(slug: str) -> str:
        """Where a project is expected to be served, from the configured domain.

        Returns "" when no domain is configured — the health probe then reports
        "not configured" rather than probing a guessed hostname that belongs to
        somebody else.
        """
        try:
            return f"https://{slug}.{_nginx_sites.base_domain()}"
        except ValueError:
            return ""

    async def _project_health_and_drift(
        project: Project, deploy_cfg: dict, configured_port, containers: list[dict]
    ) -> tuple[dict, dict]:
        slug = Path(project.repo_path).name
        public_url = deploy_cfg.get("web_health_url") or _default_public_url(slug)
        try:
            probe = await _deploy.check_public_health(public_url)
        except Exception as exc:  # noqa: BLE001
            probe = {"http_status": None, "ok": False, "reason": str(exc)}
        health = {
            "public_url": public_url,
            "http_status": probe.get("http_status"),
            "ok": probe.get("ok", False),
            "checked_at": _now(),
        }

        published_port = None
        for c in containers:
            for p in c.get("published_ports") or []:
                host_port = p.get("host_port")
                if host_port:
                    published_port = int(host_port)
                    break
            if published_port is not None:
                break
        try:
            vhost_port = _nginx_sites.parse_vhost_port(slug)
        except Exception:  # noqa: BLE001
            vhost_port = None
        drift = _nginx_sites.compute_port_drift(
            configured_port, published_port, vhost_port, repo_path=project.repo_path
        )
        return health, drift

    async def _project_runtime_status(project: Project) -> dict:
        deploy_cfg = _docker_deploy._parse_deploy_config(project.deploy_config)
        configured_port = deploy_cfg.get("port")
        repo_path = Path(project.repo_path)
        try:
            if not repo_path.is_dir():
                deploy_mode = "unknown"
            elif (repo_path / "docker-compose.yml").exists():
                deploy_mode = "compose"
            else:
                deploy_mode = "single"

            net_bytes = None
            if deploy_mode == "compose":
                containers = await _docker_deploy.get_compose_status(
                    project.repo_path, project.deploy_config
                )
            elif deploy_mode == "single":
                containers = [await _docker_deploy.get_container_status(project.repo_path)]
                container_name = _docker_deploy._container_name(project.repo_path)
                net_bytes = await _docker_deploy._read_docker_net_bytes(container_name)
            else:
                containers = []

            health, drift = await _project_health_and_drift(
                project, deploy_cfg, configured_port, containers
            )

            return {
                "deploy_mode": deploy_mode,
                "containers": containers,
                "configured_port": configured_port,
                "net_bytes": net_bytes,
                "health": health,
                "drift": drift,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "deploy_mode": "unknown",
                "containers": [],
                "configured_port": configured_port,
                "net_bytes": None,
                "error": str(exc),
                "health": {
                    "public_url": "",
                    "http_status": None,
                    "ok": False,
                    "checked_at": _now(),
                },
                "drift": {
                    "configured_port": configured_port,
                    "published_port": None,
                    "vhost_port": None,
                    "mismatch": False,
                    "detail": "",
                },
            }

    async def get_project_runtime(request: Request) -> JSONResponse:
        project_id = int(request.path_params["id"])
        project = store.get_project(project_id)
        if project is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _is_project_member(request, project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        status = await _project_runtime_status(project)
        return JSONResponse(status)

    def _runtime_overall(containers: list[dict]) -> str:
        if not containers:
            return "not_found"
        states = [str(c.get("state", "")).lower() for c in containers]
        if any(s == "not_found" for s in states):
            return "not_found" if len(states) == 1 else "partial"
        running = sum(1 for s in states if s == "running")
        if running == len(states):
            return "running"
        if running == 0:
            return "stopped"
        return "partial"

    _RUNTIME_ROLLUP_CONCURRENCY = 8
    _RUNTIME_ROLLUP_TIMEOUT = 10.0

    async def list_projects_runtime(request: Request) -> JSONResponse:
        is_pa = getattr(request.state, "is_platform_admin", False)
        all_projects = store.list_projects()
        if is_pa:
            visible = all_projects
        else:
            user_id = getattr(request.state, "user_id", None)
            if not user_id:
                return JSONResponse({"projects": []})
            accessible = set(store.get_user_project_ids(str(user_id)))
            visible = [p for p in all_projects if p["id"] in accessible]

        semaphore = asyncio.Semaphore(_RUNTIME_ROLLUP_CONCURRENCY)

        async def _one(p: dict) -> dict:
            project = Project(
                id=p["id"],
                name=p["name"],
                repo_path=p["repo_path"],
                deploy_config=p.get("deploy_config") or "",
            )
            async with semaphore:
                try:
                    status = await asyncio.wait_for(
                        _project_runtime_status(project), timeout=_RUNTIME_ROLLUP_TIMEOUT
                    )
                except Exception as exc:  # noqa: BLE001
                    return {
                        "project_id": p["id"],
                        "name": p["name"],
                        "deploy_mode": "unknown",
                        "overall": "unknown",
                        "configured_port": None,
                        "container_count": 0,
                        "running_count": 0,
                        "error": str(exc),
                        "health_ok": False,
                        "drift_mismatch": False,
                    }
            containers = status.get("containers") or []
            has_error = "error" in status
            row = {
                "project_id": p["id"],
                "name": p["name"],
                "deploy_mode": status.get("deploy_mode", "unknown"),
                "overall": "unknown" if has_error else _runtime_overall(containers),
                "configured_port": status.get("configured_port"),
                "container_count": len(containers),
                "running_count": sum(
                    1 for c in containers if str(c.get("state", "")).lower() == "running"
                ),
                "health_ok": status.get("health", {}).get("ok"),
                "drift_mismatch": status.get("drift", {}).get("mismatch"),
            }
            if has_error:
                row["error"] = status["error"]
            return row

        rows = await asyncio.gather(*(_one(p) for p in visible))
        return JSONResponse({"projects": list(rows)})

    # --- deploy trigger endpoint ---------------------------------------------

    async def _file_deploy_job(request: Request, project: Project, idea: str) -> JSONResponse:
        if not _caller_has_permission(request, "queue_job", project_id=project.id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        active = store.get_active_deploy_job(project.id)
        if active is not None:
            return JSONResponse(
                {"error": "deploy already in flight", "job_id": active.id}, status_code=409
            )
        source_actor = getattr(request.state, "user_email", None) or ""
        job = store.create(
            idea=idea,
            repo_path=project.repo_path,
            chat_id=0,
            initial_stage=Stage.DEPLOY,
            source=JobSource.UI,
            source_actor=source_actor,
        )
        return JSONResponse({"job_id": job.id})

    async def post_project_deploy(request: Request) -> JSONResponse:
        project_id = int(request.path_params["id"])
        project = store.get_project(project_id)
        if project is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return await _file_deploy_job(request, project, "Deploy-only: pull and deploy latest main")

    async def post_project_restart(request: Request) -> JSONResponse:
        # Restart == redeploy current main through the same health-gated path —
        # never a raw destroy-then-recreate (fix-forward invariant).
        project_id = int(request.path_params["id"])
        project = store.get_project(project_id)
        if project is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return await _file_deploy_job(request, project, "Restart: redeploy current main")

    def _project_deploy_mode(project: Project) -> str:
        return "compose" if (Path(project.repo_path) / "docker-compose.yml").exists() else "single"

    async def post_project_stop(request: Request) -> JSONResponse:
        project_id = int(request.path_params["id"])
        project = store.get_project(project_id)
        if project is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "queue_job", project_id=project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        if not isinstance(body, dict) or body.get("confirm") is not True:
            return JSONResponse({"error": "confirm required"}, status_code=400)
        if not _docker_deploy.is_user_project(project.repo_path, config.projects_dir):
            return JSONResponse({"error": "not a docker-deployed project"}, status_code=400)

        deploy_mode = _project_deploy_mode(project)
        actor = getattr(request.state, "user_email", None) or "web:token"
        # No DB audit trail seam exists for runtime lifecycle actions today
        # (deploy_config is unchanged by a stop), so this loud warning log is
        # the audit record for taking a project offline.
        log.warning(
            "project stop requested: project_id=%s name=%r actor=%s mode=%s",
            project_id,
            project.name,
            actor,
            deploy_mode,
        )
        if deploy_mode == "compose":
            await _docker_deploy.compose_down(project.repo_path, project.deploy_config)
        else:
            await _docker_deploy.stop_single_container(project.repo_path)

        status = await _project_runtime_status(project)
        return JSONResponse(status)

    async def post_project_start(request: Request) -> JSONResponse:
        project_id = int(request.path_params["id"])
        project = store.get_project(project_id)
        if project is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _caller_has_permission(request, "queue_job", project_id=project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if not _docker_deploy.is_user_project(project.repo_path, config.projects_dir):
            return JSONResponse({"error": "not a docker-deployed project"}, status_code=400)

        deploy_mode = _project_deploy_mode(project)
        if deploy_mode == "compose":
            await _docker_deploy.compose_up_existing(project.repo_path, project.deploy_config)
        else:
            await _docker_deploy.start_single_container(project.repo_path)

        status = await _project_runtime_status(project)
        return JSONResponse(status)

    async def get_project_logs(request: Request) -> JSONResponse:
        project_id = int(request.path_params["id"])
        project = store.get_project(project_id)
        if project is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _is_project_member(request, project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)

        try:
            tail = int(request.query_params.get("tail") or 200)
        except ValueError:
            tail = 200
        tail = max(1, min(tail, 500))
        service = request.query_params.get("service") or ""

        deploy_mode = _project_deploy_mode(project)
        if deploy_mode == "compose":
            statuses = await _docker_deploy.get_compose_status(
                project.repo_path, project.deploy_config
            )
            valid_services = {s["name"] for s in statuses}
            if not service or service not in valid_services:
                return JSONResponse({"error": "unknown service"}, status_code=404)
            deploy_cfg = _docker_deploy._parse_deploy_config(project.deploy_config)
            port = int(deploy_cfg.get("port") or _docker_deploy._DEFAULT_PORT)
            container = _docker_deploy._container_name(project.repo_path)
            compose_file = Path(project.repo_path) / "docker-compose.yml"
            env = _docker_deploy._minimal_compose_env(port, container)
            text = await _docker_deploy._compose_service_logs(
                compose_file, env, project.repo_path, service, tail=tail
            )
            result_name = service
        else:
            container = _docker_deploy._container_name(project.repo_path)
            text = await _docker_deploy._get_container_logs(container, tail=tail)
            result_name = container

        lines = text.splitlines() if text else []
        return JSONResponse(
            {
                "service": result_name,
                "tail": tail,
                "lines": lines,
                "truncated": len(lines) >= tail,
            }
        )

    # --- backlog endpoints ---------------------------------------------------

    async def list_backlog(request: Request) -> JSONResponse:
        project_id = int(request.path_params["id"])
        if not _is_project_member(request, project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        status_filter = request.query_params.get("status") or None
        type_filter = request.query_params.get("type") or None
        items = store.list_backlog_items(project_id, status=status_filter, type=type_filter)
        return JSONResponse({"items": [backlog_item_to_dict(i) for i in items]})

    async def create_backlog_item_route(request: Request) -> JSONResponse:
        project_id = int(request.path_params["id"])
        if not _is_project_member(request, project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if not _caller_has_permission(request, "propose_backlog", project_id=project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        title = (body.get("title") or "").strip()
        if not title:
            return JSONResponse({"error": "title is required"}, status_code=400)
        item_body = (body.get("body") or "").strip()
        item_type = (body.get("type") or "idea").strip()
        valid_types = {t.value for t in BacklogItemType}
        if item_type not in valid_types:
            return JSONResponse(
                {"error": f"invalid type '{item_type}': must be one of {sorted(valid_types)}"},
                status_code=400,
            )
        proposed_by = getattr(request.state, "user_email", None) or ""
        try:
            item = store.create_backlog_item(project_id, title, item_body, item_type, proposed_by)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(backlog_item_to_dict(item), status_code=201)

    async def vote_backlog_item_route(request: Request) -> JSONResponse:
        item_id = int(request.path_params["item_id"])
        item = store.get_backlog_item(item_id)
        if item is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _is_project_member(request, item.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if not _caller_has_permission(request, "propose_backlog", project_id=item.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        voter = getattr(request.state, "user_email", None) or ""
        new_votes = store.vote_backlog_item(item_id, voter)
        return JSONResponse({"votes": new_votes})

    async def patch_backlog_item_route(request: Request) -> JSONResponse:
        item_id = int(request.path_params["item_id"])
        item = store.get_backlog_item(item_id)
        if item is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _is_project_member(request, item.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if item.status in _TERMINAL_BACKLOG_STATUSES and ("status" in body or "epic_hint" in body):
            return JSONResponse(
                {
                    "error": (
                        f"backlog item {item.id} is terminal (status='{item.status.value}') "
                        "and cannot be edited"
                    )
                },
                status_code=409,
            )
        is_mark_completed_only = (
            set(body.keys()) <= {"status"}
            and body.get("status") == BacklogItemStatus.COMPLETED.value
        )
        required_permission = "propose_backlog" if is_mark_completed_only else "triage_backlog"
        if not _caller_has_permission(request, required_permission, project_id=item.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        new_status = body.get("status")
        if new_status is not None:
            valid = {s.value for s in BacklogItemStatus}
            if new_status not in valid:
                return JSONResponse({"error": f"invalid status '{new_status}'"}, status_code=400)
        kwargs: dict = {}
        if new_status is not None:
            kwargs["status"] = new_status
        if "epic_hint" in body:
            kwargs["epic_hint"] = body["epic_hint"]
        store.patch_backlog_item(item_id, **kwargs)
        refreshed = store.get_backlog_item(item_id)
        assert refreshed is not None
        return JSONResponse(backlog_item_to_dict(refreshed))

    async def refine_backlog_route(request: Request) -> JSONResponse:
        project_id = int(request.path_params["id"])
        if not _caller_has_permission(request, "queue_job", project_id=project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        project = store.get_project(project_id)
        if project is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        item_ids = body.get("item_ids")
        if not isinstance(item_ids, list) or not item_ids:
            return JSONResponse({"error": "item_ids must be a non-empty list"}, status_code=400)
        items = []
        terminal_ids = []
        for iid in item_ids:
            item = store.get_backlog_item(iid)
            if item is None:
                return JSONResponse({"error": f"backlog item {iid} not found"}, status_code=404)
            if item.project_id != project_id:
                return JSONResponse(
                    {"error": f"backlog item {iid} belongs to a different project"},
                    status_code=400,
                )
            if item.status in _TERMINAL_BACKLOG_STATUSES:
                terminal_ids.append(iid)
            items.append(item)
        if terminal_ids:
            return JSONResponse(
                {
                    "error": (
                        f"backlog item(s) {', '.join(str(i) for i in terminal_ids)} "
                        "are terminal and cannot be refined"
                    )
                },
                status_code=400,
            )
        user_email = getattr(request.state, "user_email", None) or ""
        session = store.create_chat_session(project_id, user_email)
        epics = store.list_epics(project_id=project_id, include_archived=False)
        seed_prompt = _build_refine_prompt(items, epics)
        try:
            run = await _suggest_backend.run(
                prompt=seed_prompt,
                cwd=project.repo_path,
                role=Role.PLANNER,
                append_system=BACKLOG_REFINE_SYS,
            )
        except Exception:
            log.exception("refine_backlog agent run failed for project %s", project_id)
            return JSONResponse({"error": "agent run failed"}, status_code=500)
        try:
            result = parse_result_block(run.text)
            proposed_jobs = result.get("jobs") or []
        except ResultBlockError:
            proposed_jobs = []
        store.update_chat_session(
            session.session_id,
            messages=[
                {"role": "user", "content": seed_prompt},
                {"role": "assistant", "content": run.text},
            ],
            proposed_jobs=proposed_jobs,
            source_backlog_item_ids=item_ids,
        )
        return JSONResponse({"session_id": session.session_id, "proposed_jobs": proposed_jobs})

    async def get_job_backlog_sources(request: Request) -> JSONResponse:
        job_id = int(request.path_params["id"])
        job = store.get(job_id)
        if job is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if job.project_id is not None and not _is_project_member(request, job.project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        items = store.get_backlog_items_for_job(job_id)
        return JSONResponse({"items": [backlog_item_to_dict(item) for item in items]})

    async def get_project_changelog(request: Request) -> JSONResponse:
        project_id = int(request.path_params["id"])
        project = store.get_project(project_id)
        if project is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _is_project_member(request, project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)

        include_prose = request.query_params.get("include_prose") == "true"
        deploys = store.list_deploys(project_id)
        releases: list[dict] = []

        decision_items = decisions.list_decisions(project.repo_path)
        decisions_by_job: dict[int, dict] = {}
        for d in decision_items:
            decisions_by_job.setdefault(d["job_id"], d)

        for row in deploys:
            head = row["deployed_commit"]
            prev = row["previous_commit"]
            base = {
                "deployed_commit": head,
                "previous_commit": prev,
                "deployed_at": row["deployed_at"],
                "trigger": row["trigger"],
                "verified": row["verified"],
            }
            if prev is None:
                releases.append({**base, "initial_deploy": True, "changes": []})
                continue

            result = await gitops.git(
                project.repo_path,
                "log",
                "--format=%H\x1f%s\x1f%an",
                f"{prev}..{head}",
            )
            changes = _parse_changelog_commits(
                result.stdout, store, decisions_by_job=decisions_by_job
            )
            if include_prose and changes:
                await _add_prose_to_clusters(changes, project.repo_path, _suggest_backend)
            releases.append({**base, "changes": changes})

        return JSONResponse({"releases": releases})

    # --- decisions endpoints --------------------------------------------------

    async def list_project_decisions(request: Request) -> JSONResponse:
        project_id = int(request.path_params["id"])
        project = store.get_project(project_id)
        if project is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _is_project_member(request, project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        items = decisions.list_decisions(project.repo_path)
        epic_id_param = request.query_params.get("epic_id")
        if epic_id_param is not None:
            try:
                epic_id = int(epic_id_param)
            except ValueError:
                return JSONResponse({"error": "invalid epic_id"}, status_code=400)
            job_epic_map = {}
            for item in items:
                job = store.get(item["job_id"])
                job_epic_map[item["job_id"]] = job.epic_id if job else None
            items = decisions.filter_by_epic(items, job_epic_map, epic_id)
        return JSONResponse({"decisions": items})

    async def get_project_decision(request: Request) -> JSONResponse:
        project_id = int(request.path_params["id"])
        project = store.get_project(project_id)
        if project is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _is_project_member(request, project_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        filename = request.path_params["filename"]
        path = decisions.resolve_decision_path(project.repo_path, filename)
        if path is None or not path.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        content = path.read_text()
        return JSONResponse({"filename": path.name, "content": content})

    async def record_public_page_view(request: Request) -> Response:
        try:
            raw_body = await request.body()
            if len(raw_body) > _PAGE_VIEW_MAX_BODY_BYTES:
                return Response(status_code=204)
            try:
                body = json.loads(raw_body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return Response(status_code=204)
            if not isinstance(body, dict):
                return Response(status_code=204)

            path = str(body.get("path") or "")
            if not _is_valid_page_view_path(path):
                return Response(status_code=204)

            user_agent = request.headers.get("user-agent", "")
            if _is_bot_user_agent(user_agent):
                return Response(status_code=204)

            forwarded = request.headers.get("x-forwarded-for", "")
            ip = (
                forwarded.split(",")[0].strip()
                if forwarded
                else (request.client.host if request.client else "")
            )
            if _page_view_rate_limited(ip, time.time()):
                return Response(status_code=204)

            screen_w = _normalize_page_view_int(body.get("screen_w"), 200, 10000)
            screen_h = _normalize_page_view_int(body.get("screen_h"), 200, 10000)
            dpr = _normalize_page_view_float(body.get("dpr"), 0.5, 5.0)
            hw = _normalize_page_view_int(body.get("hw"), 1, 128)
            tz = _normalize_page_view_str(body.get("tz"), 64)
            lang = _normalize_page_view_str(body.get("lang"), 16)
            langs = _normalize_page_view_str(body.get("langs"), 64)
            os_version = _normalize_page_view_str(body.get("os_version"), 24)
            arch = _normalize_page_view_str(body.get("arch"), 24)
            platform = _normalize_page_view_str(body.get("platform"), 32)
            win_w = _normalize_page_view_int(body.get("win_w"), 200, 10000)
            win_h = _normalize_page_view_int(body.get("win_h"), 200, 10000)
            avail_w = _normalize_page_view_int(body.get("avail_w"), 200, 10000)
            avail_h = _normalize_page_view_int(body.get("avail_h"), 200, 10000)
            mem = _normalize_page_view_float(body.get("mem"), 0.25, 64)
            touch = _normalize_page_view_int(body.get("touch"), 0, 20)
            color_scheme = _normalize_page_view_allowed(body.get("scheme"), {"dark", "light"})
            hour_cycle = _normalize_page_view_allowed(
                body.get("hour_cycle"), {"h11", "h12", "h23", "h24"}
            )
            ref = str(body.get("ref") or "")[:64]

            win_bucket = (
                f"{round(win_w / 100) * 100}x{round(win_h / 100) * 100}"
                if isinstance(win_w, int) and isinstance(win_h, int)
                else ""
            )
            avail_bucket = (
                f"{round(avail_w / 100) * 100}x{round(avail_h / 100) * 100}"
                if isinstance(avail_w, int) and isinstance(avail_h, int)
                else ""
            )
            device_traits = "|".join(
                str(value)
                for value in (
                    user_agent,
                    f"{screen_w}x{screen_h}",
                    dpr,
                    hw,
                    tz,
                    lang,
                    langs,
                    os_version,
                    arch,
                    platform,
                    mem,
                    touch,
                    hour_cycle,
                    win_bucket,
                    avail_bucket,
                )
            )
            salt_secret = store.get_or_create_page_view_salt()
            daily_salt = hmac.new(
                salt_secret.encode(), _today_utc_date_str().encode(), hashlib.sha256
            ).hexdigest()
            visitor_hash = hashlib.sha256(f"{daily_salt}|{ip}|{device_traits}".encode()).hexdigest()

            referrer_value = body.get("referrer") or request.headers.get("referer", "")

            try:
                country, region, city = _geoip_lookup(ip, config.geoip_db)
            except Exception:
                country, region, city = None, None, None

            store.record_page_view(
                path=path,
                ref=ref,
                visitor_hash=visitor_hash,
                country=country,
                region=region,
                city=city,
                referrer_host=_referrer_host(referrer_value),
                ua_family=_ua_family(user_agent),
                os_family=_os_family(user_agent),
                lang=lang,
                tz=tz,
                screen=_screen_bucket(screen_w) if isinstance(screen_w, int) else "",
                os_version=os_version,
                arch=arch,
                platform=platform,
                langs=langs,
                win=win_bucket,
                viewport=_screen_bucket(win_w) if isinstance(win_w, int) else "",
                color_scheme=color_scheme,
                device_memory=f"{mem:g}" if isinstance(mem, float) else "",
                touch=("0" if touch == 0 else "1" if touch == 1 else "many")
                if isinstance(touch, int)
                else "",
                hour_cycle=hour_cycle,
            )
        except Exception:
            log.warning("page-view beacon failed (best-effort)", exc_info=True)
        return Response(status_code=204)

    async def get_public_site_stats(request: Request) -> JSONResponse:
        now = time.time()
        cached_data = _SITE_STATS_CACHE["data"]
        if cached_data is not None and now < _SITE_STATS_CACHE["expires_at"]:
            return JSONResponse(cached_data, headers={"Cache-Control": "public, max-age=60"})

        forwarded = request.headers.get("x-forwarded-for", "")
        ip = (
            forwarded.split(",")[0].strip()
            if forwarded
            else (request.client.host if request.client else "")
        )
        if _site_stats_rate_limited(ip, now):
            payload = (
                cached_data
                if cached_data is not None
                else {"generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
            )
            return JSONResponse(payload, headers={"Cache-Control": "public, max-age=60"})

        async with _SITE_STATS_LOCK:
            # Another caller may have refreshed the cache while we waited on
            # the lock; recheck before paying for another DB aggregation.
            now = time.time()
            cached_data = _SITE_STATS_CACHE["data"]
            if cached_data is not None and now < _SITE_STATS_CACHE["expires_at"]:
                return JSONResponse(cached_data, headers={"Cache-Control": "public, max-age=60"})

            try:
                payload = request.state.store.site_stats().to_dict()
            except Exception:
                # This unauthenticated endpoint must remain available without
                # exposing database or serialization failures to public callers.
                log.warning("public site stats generation failed", exc_info=True)
                payload = (
                    cached_data
                    if cached_data is not None
                    else {"generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
                )
            else:
                _SITE_STATS_CACHE["data"] = payload
                _SITE_STATS_CACHE["expires_at"] = time.time() + 60

        return JSONResponse(payload, headers={"Cache-Control": "public, max-age=60"})

    routes = [
        Route("/api/health", health),
        Route("/api/public/page-view", record_public_page_view, methods=["POST"]),
        Route("/api/public/site-stats", get_public_site_stats, methods=["GET"]),
        Route("/api/chat/stream", chat_stream, methods=["POST"]),
        Route("/api/chat", chat, methods=["POST"]),
        Route("/api/jobs", list_jobs, methods=["GET"]),
        Route("/api/jobs/batch", create_job_batch, methods=["POST"]),
        Route("/api/jobs", create_job, methods=["POST"]),
        Route("/api/jobs/stream", jobs_stream),
        Route("/api/jobs/chat/stream", job_chat_stream, methods=["POST"]),
        Route("/api/jobs/archive-terminal", archive_terminal, methods=["POST"]),
        Route("/api/jobs/{job_id:int}", cancel_job, methods=["DELETE"]),
        Route("/api/jobs/{job_id:int}", get_job, methods=["GET"]),
        Route("/api/jobs/{job_id:int}/retry", retry_job, methods=["POST"]),
        Route("/api/jobs/{job_id:int}/requeue", requeue_job, methods=["POST"]),
        Route("/api/jobs/{job_id:int}/fix-forward", fix_forward_job, methods=["POST"]),
        Route("/api/jobs/{job_id:int}/idea", patch_job_idea, methods=["PATCH"]),
        Route("/api/jobs/{job_id:int}/resolve", resolve_job, methods=["POST"]),
        Route("/api/jobs/{job_id:int}/priority", set_job_priority, methods=["POST"]),
        Route("/api/jobs/{job_id:int}/archive", archive_job, methods=["POST"]),
        Route("/api/jobs/{job_id:int}/unarchive", unarchive_job, methods=["POST"]),
        Route("/api/jobs/{job_id:int}/epic", patch_job_epic, methods=["PATCH"]),
        Route("/api/jobs/{job_id:int}/events", job_events, methods=["GET"]),
        Route("/api/jobs/{job_id:int}/diff", job_diff, methods=["GET"]),
        Route("/api/jobs/{job_id:int}/logs/stream", job_logs_stream),
        Route("/api/jobs/{job_id:int}/stream", job_detail_stream),
        Route("/api/jobs/{id:int}/resources", get_job_resources, methods=["GET"]),
        Route("/api/jobs/{job_id:int}/usage", get_job_usage, methods=["GET"]),
        Route("/api/jobs/{id:int}/backlog-sources", get_job_backlog_sources, methods=["GET"]),
        Route("/api/jobs/{job_id:int}/title", patch_job_title, methods=["PATCH"]),
        Route("/api/jobs/{job_id:int}/dependents", get_job_dependents, methods=["GET"]),
        Route("/api/jobs/{job_id:int}/depends-on-jobs", get_job_depends_on_jobs, methods=["GET"]),
        Route("/api/jobs/{job_id:int}/dependencies", get_job_dependencies, methods=["GET"]),
        Route("/api/jobs/{job_id:int}/dependencies", add_job_dependency, methods=["POST"]),
        Route(
            "/api/jobs/{job_id:int}/dependencies/{dep_id:int}",
            delete_job_dependency,
            methods=["DELETE"],
        ),
        Route("/api/epics", list_epics, methods=["GET"]),
        Route("/api/epics", create_epic, methods=["POST"]),
        Route("/api/epics/suggest", suggest_epic_for_job, methods=["POST"]),
        Route("/api/epics/{epic_id:int}", get_epic, methods=["GET"]),
        Route("/api/epics/{epic_id:int}", update_epic, methods=["PATCH"]),
        Route("/api/epics/{epic_id:int}/archive", archive_epic, methods=["POST"]),
        Route("/api/epics/{epic_id:int}/unarchive", unarchive_epic, methods=["POST"]),
        Route(
            "/api/epics/{epic_id:int}/suggest/stream",
            suggest_epic_features_stream,
            methods=["POST"],
        ),
        Route("/api/epics/{epic_id:int}/suggest", suggest_epic_features, methods=["POST"]),
        Route(
            "/api/epics/{epic_id:int}/architect-plan/stream",
            architect_plan_stream,
            methods=["POST"],
        ),
        Route("/api/projects", list_projects, methods=["GET"]),
        Route("/api/projects", create_project_route, methods=["POST"]),
        Route("/api/projects/provision", provision_project_route, methods=["POST"]),
        Route("/api/projects/runtime", list_projects_runtime, methods=["GET"]),
        Route("/api/projects/{project_id:int}", update_project, methods=["PATCH"]),
        Route("/api/projects/{project_id:int}", delete_project_route, methods=["DELETE"]),
        Route("/api/projects/{id:int}/deploy-status", get_project_deploy_status, methods=["GET"]),
        Route("/api/projects/{id:int}/runtime", get_project_runtime, methods=["GET"]),
        Route("/api/projects/{id:int}/deploy", post_project_deploy, methods=["POST"]),
        Route("/api/projects/{id:int}/restart", post_project_restart, methods=["POST"]),
        Route("/api/projects/{id:int}/stop", post_project_stop, methods=["POST"]),
        Route("/api/projects/{id:int}/start", post_project_start, methods=["POST"]),
        Route("/api/projects/{id:int}/logs", get_project_logs, methods=["GET"]),
        Route("/api/projects/{id:int}/changelog", get_project_changelog, methods=["GET"]),
        Route("/api/projects/{id:int}/decisions", list_project_decisions, methods=["GET"]),
        Route("/api/projects/{id:int}/decisions/{filename}", get_project_decision, methods=["GET"]),
        Route("/api/projects/{id:int}/backlog/refine", refine_backlog_route, methods=["POST"]),
        Route("/api/projects/{id:int}/backlog", list_backlog, methods=["GET"]),
        Route("/api/projects/{id:int}/backlog", create_backlog_item_route, methods=["POST"]),
        Route("/api/projects/{id:int}/chat_sessions", list_project_chat_sessions, methods=["GET"]),
        Route("/api/chat_sessions/{session_id}", get_chat_session_detail, methods=["GET"]),
        Route("/api/backlog/{item_id:int}/vote", vote_backlog_item_route, methods=["POST"]),
        Route("/api/backlog/{item_id:int}", patch_backlog_item_route, methods=["PATCH"]),
        Route("/api/agent-meta", agent_meta, methods=["GET"]),
        Route("/api/projects/{project_id:int}/agents", list_agents, methods=["GET"]),
        Route("/api/projects/{project_id:int}/agents", create_agent, methods=["POST"]),
        Route("/api/projects/{project_id:int}/agent-stats", agent_stats, methods=["GET"]),
        Route(
            "/api/projects/{project_id:int}/performance/stage-stats",
            perf_stage_stats,
            methods=["GET"],
        ),
        Route(
            "/api/projects/{project_id:int}/performance/slowest-jobs",
            perf_slowest_jobs,
            methods=["GET"],
        ),
        Route(
            "/api/projects/{project_id:int}/performance/headline",
            perf_headline,
            methods=["GET"],
        ),
        Route(
            "/api/projects/{project_id:int}/performance/trend",
            perf_trend,
            methods=["GET"],
        ),
        Route("/api/agents/{agent_id:int}", update_agent, methods=["PATCH"]),
        Route("/api/agents/{agent_id:int}", delete_agent, methods=["DELETE"]),
        Route(
            "/api/projects/{project_id:int}/members", list_project_members_route, methods=["GET"]
        ),
        Route("/api/projects/{project_id:int}/members", add_project_member_route, methods=["POST"]),
        Route(
            "/api/projects/{project_id:int}/members/{user_id}",
            remove_project_member_route,
            methods=["DELETE"],
        ),
        Route(
            "/api/projects/{project_id:int}/members/{user_id}",
            update_member_role_route,
            methods=["PATCH"],
        ),
        Route("/api/projects/{project_id:int}/tokens", list_api_tokens_route, methods=["GET"]),
        Route("/api/projects/{project_id:int}/tokens", create_api_token_route, methods=["POST"]),
        Route(
            "/api/projects/{project_id:int}/tokens/{token_id:int}",
            revoke_api_token_route,
            methods=["DELETE"],
        ),
        Route("/api/projects/{project_id:int}/webhooks", list_webhooks_route, methods=["GET"]),
        Route("/api/projects/{project_id:int}/webhooks", create_webhook_route, methods=["POST"]),
        Route(
            "/api/webhooks/{webhook_id:int}/active",
            set_webhook_active_route,
            methods=["POST"],
        ),
        Route("/api/webhooks/{webhook_id:int}", delete_webhook_route, methods=["DELETE"]),
        Route(
            "/api/projects/{project_id:int}/slack-credentials",
            get_slack_credentials_route,
            methods=["GET"],
        ),
        Route(
            "/api/projects/{project_id:int}/slack-credentials",
            set_slack_credentials_route,
            methods=["POST"],
        ),
        Route("/api/auth/login", login, methods=["POST"]),
        Route("/api/auth/logout", logout, methods=["POST"]),
        Route("/api/admin/roles/permissions", get_role_permissions_route, methods=["GET"]),
        Route("/api/admin/roles/{role}/permissions", set_role_permission_route, methods=["PATCH"]),
        Route("/api/admin/audit", admin_audit_log, methods=["GET"]),
        Route("/api/admin/page-views", admin_page_views, methods=["GET"]),
        Route("/api/admin/users", list_admin_users, methods=["GET"]),
        Route("/api/admin/users", create_admin_user, methods=["POST"]),
        Route(
            "/api/admin/users/{user_id:int}/projects",
            assign_user_to_project_route,
            methods=["POST"],
        ),
        Route("/api/admin/users/{user_id:int}", update_admin_user, methods=["PATCH"]),
        Route("/api/admin/users/{user_id:int}", delete_admin_user_route, methods=["DELETE"]),
        Route(
            "/api/admin/users/{user_id:int}/memberships",
            get_user_memberships_route,
            methods=["GET"],
        ),
        Route(
            "/api/admin/users/{user_id:int}/platform-permissions",
            grant_user_platform_permission,
            methods=["POST"],
        ),
        Route(
            "/api/admin/users/{user_id:int}/platform-permissions/{permission}",
            revoke_user_platform_permission,
            methods=["DELETE"],
        ),
        Route("/api/admin/invitations", list_invitations_route, methods=["GET"]),
        Route("/api/admin/invitations", create_invitation_route, methods=["POST"]),
        Route("/api/admin/invitations/{token}", revoke_invitation_route, methods=["DELETE"]),
        *build_oauth_routes(config, store),
        Route("/api/intake/sessions", create_intake_session_route, methods=["POST"]),
        Route("/api/intake/sessions", list_intake_sessions_route, methods=["GET"]),
        Route("/api/intake/sessions/{session_id}", get_intake_session_route, methods=["GET"]),
        Route(
            "/api/intake/sessions/{session_id}/message",
            intake_message_route,
            methods=["POST"],
        ),
        Route(
            "/api/intake/sessions/{session_id}/stream",
            intake_message_stream_route,
            methods=["POST"],
        ),
        Route(
            "/api/intake/sessions/{session_id}/plan",
            plan_intake_session_route,
            methods=["POST"],
        ),
        Route(
            "/api/intake/sessions/{session_id}/confirm",
            confirm_intake_session_route,
            methods=["POST"],
        ),
        Route(
            "/api/intake/sessions/{session_id}/abandon",
            abandon_intake_session_route,
            methods=["POST"],
        ),
        Route(
            "/api/intake/sessions/{session_id}/draft_spec",
            patch_intake_draft_spec,
            methods=["PATCH"],
        ),
        Route("/api/me", get_me_inner),
        Route("/api/workers", workers, methods=["GET"]),
        Route("/api/workers/stream", workers_stream),
        Route("/api/supervisor", supervisor_snapshot),
        Route("/api/supervisor/stream", supervisor_stream),
        Route("/api/usage", usage),
        Route("/api/jobs/filed-by-breakdown", jobs_filed_by_breakdown_route),
        Route("/api/deployment-reliability", deployment_reliability_route),
        Route("/api/providers", list_providers_route, methods=["GET"]),
        Route("/api/providers/{provider}/pause", clear_provider_pause, methods=["DELETE"]),
    ]

    # MCP server — mounted at /mcp, protected by OAuth 2.1 via FastMCP GoogleProvider.
    import urllib.parse

    from starlette.middleware import Middleware as StarletteMiddleware

    from hyqs.web.mcp_server import (
        CorsPreflightASGI,
        HyqsIdentityMiddleware,
        PathRewriteASGI,
        build_mcp_server,
    )

    _mcp_server = build_mcp_server(store, config)
    # Pass HyqsIdentityMiddleware via http_app() so it lands as ASGI middleware
    # inside FastMCP's AuthenticationMiddleware (which sets scope["user"]).
    # Using FastMCP(middleware=[...]) would apply MCP-protocol-level middleware
    # instead, which has no access to scope["user"].
    _mcp_asgi = _mcp_server.http_app(
        middleware=[StarletteMiddleware(HyqsIdentityMiddleware, store=store)]
    )

    # RFC 9728 Protected Resource Metadata served at the root-level well-known URL.
    # FastMCP's OAuthProxy generates the discovery path relative to its own app root;
    # when mounted at /mcp that's /mcp/.well-known/... which is wrong per the spec.
    # Forward to FastMCP's real, dynamically-computed metadata instead of
    # hand-duplicating it — a hand-rolled copy here previously hardcoded
    # scopes_supported to the short-form "email", which drifted from the
    # long-form Google scope URI (https://www.googleapis.com/auth/userinfo.email)
    # that DCR-registered clients actually get, so every /authorize request
    # requesting "email" failed with invalid_scope.
    _mcp_resource_url = config.resolved_mcp_resource_url()
    _mcp_resource_path = urllib.parse.urlparse(_mcp_resource_url).path.rstrip("/")
    _prm_path = f"/.well-known/oauth-protected-resource{_mcp_resource_path}"
    routes.append(Route(_prm_path, endpoint=PathRewriteASGI(_mcp_asgi, _prm_path)))

    # RFC 8414 §3.1 Authorization Server Metadata, at the standard location
    # (origin + well-known + resource path). FastMCP only serves this relative
    # to its own mount root (i.e. /mcp/.well-known/oauth-authorization-server),
    # which real clients never probe — they go straight for the path above,
    # 404 out of discovery, and fall back to guessing endpoints at the bare
    # origin (e.g. POST /register, which 405s). Forward to FastMCP's real,
    # dynamically-computed metadata instead of hand-duplicating it so the two
    # can never drift out of sync. The openid-configuration aliases exist
    # because some clients probe those well-known names instead/as well; the
    # documents overlap enough that serving the same metadata is safe.
    _mcp_asmd_internal_path = "/.well-known/oauth-authorization-server"
    for _asmd_path in (
        f"/.well-known/oauth-authorization-server{_mcp_resource_path}",
        f"/.well-known/openid-configuration{_mcp_resource_path}",
        f"{_mcp_resource_path}/.well-known/openid-configuration",
    ):
        routes.append(
            Route(_asmd_path, endpoint=PathRewriteASGI(_mcp_asgi, _mcp_asmd_internal_path))
        )

    # Route handles the exact path /mcp (no trailing slash); Mount handles /mcp/...
    # Both are needed because Starlette's Mount regex requires a trailing slash, so
    # a static-files catch-all would otherwise intercept POST /mcp before the redirect.
    routes.append(Route("/mcp", endpoint=_mcp_asgi))
    routes.append(Mount("/mcp", app=_mcp_asgi))

    # Serve the built SPA if present; otherwise a helpful placeholder.
    if FRONTEND_DIST.is_dir():
        routes.append(Mount("/", app=SPAStaticFiles(directory=str(FRONTEND_DIST), html=True)))
    else:

        async def placeholder(request: Request):
            return PlainTextResponse(
                "Hyqs API is running. Frontend not built yet:\n"
                "  cd hyqs/web/frontend && npm install && npm run build\n"
            )

        routes.append(Route("/", placeholder))

    app = Starlette(routes=routes, lifespan=_mcp_asgi.lifespan)
    # Starlette.add_middleware() inserts at the front of the stack, and the
    # front of the stack is the OUTERMOST layer — so the middleware added
    # last here (SecurityHeaders) wraps everything added before it, and
    # therefore sees TokenAuth's 401s and MaxRequestBodySize's 413s too.
    app.add_middleware(TokenAuth, token=config.web_token, store=store)
    app.add_middleware(MaxRequestBodySize, max_bytes=config.max_request_body_bytes)
    app.add_middleware(SecurityHeaders)

    # Answer every OPTIONS preflight at the outermost layer, before routing.
    # MCP clients (e.g. Claude Code) send OPTIONS against /mcp *and* several
    # OAuth-discovery well-known URLs (RFC 8414/9728 allow multiple valid URL
    # constructions — with or without the resource path segment, plus an
    # openid-configuration alias). Enumerating each one as its own route is
    # fragile: any variant not explicitly routed falls through to the
    # static-file/placeholder catch-all, which 405s in plain text on OPTIONS
    # and breaks MCP client reconnection (it expects a JSON error body).
    return CorsPreflightASGI(app)


async def serve(config, orchestrator: Orchestrator, store: JobStore) -> None:
    if not config.web_token:
        raise RuntimeError(
            "HYQS_WEB_TOKEN is required; refusing to start to avoid open unauthenticated access"
        )
    app = build_app(config, orchestrator, store)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    # SO_REUSEADDR restores Uvicorn's default restart behavior: a fresh
    # listener may bind after the previous one leaves connections in
    # TIME_WAIT. SO_REUSEPORT serves a separate purpose, letting a new
    # generation bind alongside the current listener during a blue-green
    # self-deploy (see deploy/release.sh) so existing connections can drain.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind((config.web_host, config.web_port))
    sock.listen()
    log.info("web UI on http://%s:%s", config.web_host, config.web_port)
    await server.serve(sockets=[sock])
