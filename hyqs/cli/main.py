"""``hyqs-jobs``: an argparse CLI wrapping the hyqs MCP job-operations tools.

Every subcommand prints its tool's JSON payload to stdout and exits with the
code ``hyqs.cli.client.call_tool`` classified the response as (0 success, 1
generic tool error, 2 transport failure, 3 forbidden, 4 not found). This is a
job-operations CLI only — it never boots the web/pipeline/scheduler service
that the ``hyqs`` console script does.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from hyqs.cli.client import API_TOKEN_ENV_VAR, build_client, call_tool


def _read_idea_file(path: str) -> str:
    return Path(path).read_text()


def _parse_depends_on(raw: str) -> list[int]:
    return [int(part.strip()) for part in raw.split(",") if part.strip()]


async def _watch_log_handler(message: Any) -> None:
    data = message.data
    text = data if isinstance(data, str) else json.dumps(data, default=str)
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hyqs-jobs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            f"Set {API_TOKEN_ENV_VAR} to a project-scoped API token to authenticate\n"
            "non-interactively (skips the browser OAuth flow). Unset: falls back to\n"
            "interactive OAuth."
        ),
    )
    subparsers = parser.add_subparsers(dest="resource")

    projects_parser = subparsers.add_parser("projects", help="Project operations.")
    projects_actions = projects_parser.add_subparsers(dest="action")
    projects_actions.add_parser("list", help="List projects accessible to the caller.")

    epics_parser = subparsers.add_parser("epics", help="Epic operations.")
    epics_actions = epics_parser.add_subparsers(dest="action")
    epics_list = epics_actions.add_parser("list", help="List epics for a project.")
    epics_list.add_argument("--project", type=int, required=True, dest="project_id")

    jobs_parser = subparsers.add_parser("jobs", help="Job operations.")
    jobs_actions = jobs_parser.add_subparsers(dest="action")

    jobs_list = jobs_actions.add_parser("list", help="List jobs for a project.")
    jobs_list.add_argument("--project", type=int, required=True, dest="project_id")
    jobs_list.add_argument(
        "--status",
        choices=["active", "done", "failed", "archived", "all"],
        default="active",
    )
    jobs_list.add_argument("--summary", action="store_true")

    jobs_get = jobs_actions.add_parser("get", help="Fetch a single job by ID.")
    jobs_get.add_argument("job_id", type=int)
    jobs_get.add_argument("--summary", action="store_true")

    jobs_create = jobs_actions.add_parser("create", help="Create a new job in a project.")
    jobs_create.add_argument("--project", type=int, required=True, dest="project_id")
    jobs_create.add_argument("--epic", type=int, default=None, dest="epic_id")
    jobs_create.add_argument("--title", type=str, required=True)
    jobs_create.add_argument("--idea-file", type=str, required=True, dest="idea_file")
    jobs_create.add_argument("--depends-on", type=str, default="", dest="depends_on")

    jobs_watch = jobs_actions.add_parser(
        "watch", help="Stream a job's stage events until it finishes or times out."
    )
    jobs_watch.add_argument("job_id", type=int)
    jobs_watch.add_argument("--timeout", type=int, default=120, dest="timeout_seconds")

    return parser


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.resource is None:
        parser.error("a resource is required (projects, epics, jobs)")
    if getattr(args, "action", None) is None:
        parser.error(f"an action is required for '{args.resource}'")
    return args


def _build_call(args: argparse.Namespace) -> tuple[str, dict]:
    """The MCP tool name + argument dict for one parsed CLI invocation."""
    if args.resource == "projects" and args.action == "list":
        return "list_projects", {}
    if args.resource == "epics" and args.action == "list":
        return "list_epics", {"project_id": args.project_id}
    if args.resource == "jobs" and args.action == "list":
        return "list_jobs", {
            "project_id": args.project_id,
            "status": args.status,
            "summary": args.summary,
        }
    if args.resource == "jobs" and args.action == "get":
        return "get_job", {"job_id": args.job_id, "summary": args.summary}
    if args.resource == "jobs" and args.action == "create":
        return "create_job", {
            "idea": _read_idea_file(args.idea_file),
            "project_id": args.project_id,
            "title": args.title,
            "epic_id": args.epic_id,
            "depends_on": _parse_depends_on(args.depends_on),
        }
    if args.resource == "jobs" and args.action == "watch":
        return "watch_job", {"job_id": args.job_id, "timeout_seconds": args.timeout_seconds}
    raise ValueError(f"unknown command: {args.resource} {args.action}")


async def _run(args: argparse.Namespace) -> tuple[Any, int]:
    tool_name, arguments = _build_call(args)
    log_handler = _watch_log_handler if tool_name == "watch_job" else None
    client = build_client(log_handler=log_handler)
    return await call_tool(client, tool_name, arguments)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    payload, exit_code = asyncio.run(_run(args))
    print(json.dumps(payload, default=str))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
