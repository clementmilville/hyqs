"""Provision a brand-new project: a local git repo + a GitHub repo + registration.

Creating a project is the *new* way to start work (the other being to pick an
existing project). It scaffolds a real repo so subsequent ``/build`` jobs have
somewhere to run. Side-effecting (touches the filesystem and GitHub), so this is
plumbing the web layer calls directly — never the AI pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path

from hyqs.config import Config

from . import nginx_sites
from .blueprints import get_blueprint
from .store import JobStore

log = logging.getLogger("hyqs.provision")

_GITHUB_ORG_RE = re.compile(r"^[A-Za-z0-9]+(-[A-Za-z0-9]+)*$")


def slugify(name: str, *, fallback: str = "project") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or fallback


def _validate_github_org(org: str) -> str:
    """Return `org` unchanged if it is a valid GitHub org login, else raise."""
    if not _GITHUB_ORG_RE.match(org):
        raise ValueError(f"invalid HYQS_GITHUB_ORG: {org!r}")
    return org


async def _run(cwd: Path | None, *args: str) -> tuple[bool, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode == 0, out.decode(errors="replace").strip()


async def provision_project(
    store: JobStore,
    projects_dir: Path,
    name: str,
    description: str = "",
    create_github: bool = True,
    private: bool = True,
    stack: str = "bare",
    spec: dict | None = None,
    creator_id: str | None = None,
    github_org: str | None = None,
) -> dict:
    """Create ~/projects/<slug>, init git + first commit, optionally create the
    GitHub repo and push, then register the Project. Returns a result dict with
    the project and any warnings; raises ValueError on fatal local errors.
    """
    if github_org is None:
        github_org = Config.from_env().github_org
    if github_org:
        _validate_github_org(github_org)

    blueprint = get_blueprint(stack)  # raises ValueError for unknown stacks

    slug = slugify(name)
    root = Path(projects_dir).expanduser()
    path = root / slug
    if path.exists():
        raise ValueError(f"a folder already exists at {path}")

    warnings: list[str] = []
    root.mkdir(parents=True, exist_ok=True)
    path.mkdir()
    try:
        blueprint.scaffold(path, name, description, spec or {})
        for cmd in (
            ("git", "init", "-b", "main"),
            ("git", "add", "-A"),
            ("git", "commit", "-m", "Initial commit"),
        ):
            ok, out = await _run(path, *cmd)
            if not ok:
                raise ValueError(f"`{' '.join(cmd)}` failed: {out[:300]}")
    except Exception:
        shutil.rmtree(path, ignore_errors=True)  # don't leave a half-made repo
        raise

    github_url = ""
    if create_github:
        vis = "--private" if private else "--public"
        repo_name = f"{github_org}/{slug}" if github_org else slug
        ok, out = await _run(
            path,
            "gh",
            "repo",
            "create",
            repo_name,
            vis,
            "--source",
            str(path),
            "--remote",
            "origin",
            "--push",
            *(("--description", description) if description else ()),
        )
        if ok:
            m = re.search(r"https://github\.com/\S+", out)
            github_url = m.group(0) if m else ""
            log.info("provisioned GitHub repo %s", github_url or slug)
        else:
            shutil.rmtree(path, ignore_errors=True)
            raise ValueError(f"GitHub repo creation failed for {slug}: {out[:300]}")

    project = store.create_project(
        name, str(path), description, stack=stack, spec=spec or {}, github_url=github_url
    )

    if creator_id is not None:
        store.add_project_member(project.id, creator_id, "project_admin")

    cfg = json.loads(project.deploy_config) if project.deploy_config else {}
    if "port" not in cfg:
        cfg["port"] = 8100 + project.id
        project = store.update_project(project.id, deploy_config=json.dumps(cfg))

    try:
        port = nginx_sites.normalize_port(cfg["port"])
        await nginx_sites.register_site(slug, port, domains=nginx_sites.resolve_domains(cfg))
    except Exception as exc:
        warnings.append(f"nginx site not registered: {exc}")

    return {"project": project, "path": str(path), "github_url": github_url, "warnings": warnings}
