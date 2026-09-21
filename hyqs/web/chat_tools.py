"""Project-scoped in-process SDK tools for the job-chat EXPLORER agent.

Each tool is bound to a specific project at build time. The project object is
captured in closures and never appears in tool input schemas — the model
cannot escape its project scope. Any call referencing a job from another
project is denied here in Python before touching the DB further.
"""

from __future__ import annotations

import collections
import json
import time

from claude_agent_sdk import create_sdk_mcp_server, tool

from hyqs.pipeline import gitops
from hyqs.pipeline.docker_deploy import (
    _DEFAULT_PORT,
    _container_name,
    _get_container_logs,
    _parse_deploy_config,
    _probe_http,
    get_container_status,
)
from hyqs.pipeline.models import Project
from hyqs.pipeline.store import JobStore

_VALID_STATUSES = {"active", "done", "failed", "archived", "all"}


def _text(msg: str) -> dict:
    return {"content": [{"type": "text", "text": msg}]}


def _job_dict(job) -> dict:
    return {
        "id": job.id,
        "title": job.title,
        "status": job.status.value,
        "stage": job.stage.value,
        "error": job.error,
        "epic_id": job.epic_id,
        "branch": job.branch,
        "idea": job.idea,
    }


def _validate_probe_path(path: str) -> str | None:
    if "://" in path:
        return "error: absolute URLs are not allowed"
    if path.startswith("//"):
        return "error: scheme-relative URLs are not allowed"
    if not path.startswith("/"):
        return "error: path must start with /"
    return None


class _ProbeRateLimiter:
    def __init__(self, window: float = 60.0, max_hits: int = 10) -> None:
        self._window = window
        self._max_hits = max_hits
        self._queues: dict[int, collections.deque] = {}

    def check(self, project_id: int) -> bool:
        now = time.monotonic()
        q = self._queues.setdefault(project_id, collections.deque())
        while q and now - q[0] > self._window:
            q.popleft()
        if len(q) >= self._max_hits:
            return False
        q.append(now)
        return True


_PROBE_LIMITER = _ProbeRateLimiter()


def build_project_tools(store: JobStore, project: Project):
    """Return (server, handlers) where server is a clean SDK MCP server dict
    suitable for ClaudeAgentOptions and handlers is {name: callable} for tests.
    """
    project_id = project.id

    @tool("list_jobs", "List recent jobs for this project.", {"status": str})
    async def list_jobs(args: dict) -> dict:
        status = args.get("status", "active")
        if status not in _VALID_STATUSES:
            return _text(f"error: status must be one of {sorted(_VALID_STATUSES)}")
        jobs = store.list_active(50, project_id=project_id, status=status)
        return _text(json.dumps([_job_dict(j) for j in jobs]))

    @tool("get_job", "Get details for a single job in this project.", {"job_id": int})
    async def get_job(args: dict) -> dict:
        job = store.get(int(args["job_id"]))
        if job is None or job.project_id != project_id:
            return _text("error: forbidden")
        return _text(json.dumps(_job_dict(job)))

    @tool("job_events", "Get stage timeline events for a job.", {"job_id": int})
    async def job_events(args: dict) -> dict:
        job = store.get(int(args["job_id"]))
        if job is None or job.project_id != project_id:
            return _text("error: forbidden")
        events = store.list_events(int(args["job_id"]))
        return _text(json.dumps(events, default=str))

    @tool(
        "job_diff",
        "Get the git diff for a job's branch against the default branch.",
        {"job_id": int},
    )
    async def job_diff(args: dict) -> dict:
        job = store.get(int(args["job_id"]))
        if job is None or job.project_id != project_id:
            return _text("error: forbidden")
        if not job.branch:
            return _text("")
        base = await gitops.default_branch(job.repo_path)
        result = await gitops.git(job.repo_path, "diff", f"{base}...{job.branch}")
        return _text(result.stdout or "")

    @tool("project_performance", "Get headline performance metrics and per-stage stats.", {})
    async def project_performance(args: dict) -> dict:
        data = {
            "headline": store.performance_headline_stats(project_id),
            "stage_stats": store.performance_stage_stats(project_id),
        }
        return _text(json.dumps(data, default=str))

    @tool("list_epics", "List active epics for this project.", {})
    async def list_epics(args: dict) -> dict:
        epics = store.list_epics(project_id=project_id)
        return _text(json.dumps(epics, default=str))

    @tool("deploy_status", "Get last deploy outcome and container health.", {})
    async def deploy_status(args: dict) -> dict:
        container_status = await get_container_status(project.repo_path)
        last_deploy = store.get_last_deploy(project_id)
        merged = {**container_status, **(last_deploy or {})}
        return _text(json.dumps(merged))

    @tool("container_logs", "Fetch recent container logs.", {"tail": int})
    async def container_logs(args: dict) -> dict:
        tail = min(int(args.get("tail", 50)), 200)
        logs = await _get_container_logs(_container_name(project.repo_path), tail)
        return _text(logs)

    @tool("http_probe", "Probe a path on this project's running container.", {"path": str})
    async def http_probe(args: dict) -> dict:
        path = args.get("path", "/")
        err = _validate_probe_path(path)
        if err:
            return _text(err)
        if not _PROBE_LIMITER.check(project_id):
            return _text("error: rate limit exceeded; try again in 60 s")
        port = int(_parse_deploy_config(project.deploy_config).get("port", _DEFAULT_PORT))
        ok, reason = await _probe_http(port, path, 10, 0.0)
        return _text(json.dumps({"ok": ok, "reason": reason}))

    _tools = [
        list_jobs, get_job, job_events, job_diff, project_performance, list_epics,
        deploy_status, container_logs, http_probe,
    ]
    server = create_sdk_mcp_server(
        name="project-tools",
        version="0.1.0",
        tools=_tools,
    )
    handlers = {t.name: t.handler for t in _tools}
    return server, handlers
