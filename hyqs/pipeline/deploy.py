"""Deterministic deploy runner — NO AI.

Checks for a repo-defined release command: deploy/release.sh first, else the
HYQS_DEPLOY_CMD config value, else a no-op so repos without a deploy step are
unaffected.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import shlex
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable
from urllib.parse import urlparse

from . import gitops, registry_push, resources
from .models import _now

if TYPE_CHECKING:
    from .models import Job, Release
    from .store import JobStore

log = logging.getLogger("hyqs.deploy")

DEPLOY_TIMEOUT = 300  # seconds

_REGENERABLE_LOCKFILES = ("uv.lock", "package-lock.json", "yarn.lock", "poetry.lock")

# Networks that a health-check URL must not target (SSRF prevention).
# Loopback (127.x/::1) is intentionally allowed — the expected use-case is
# verifying a service on the same host.
_BLOCKED_HEALTH_NETS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("169.254.0.0/16"),  # link-local / cloud metadata endpoints
    ipaddress.ip_network("10.0.0.0/8"),  # RFC-1918
    ipaddress.ip_network("172.16.0.0/12"),  # RFC-1918
    ipaddress.ip_network("192.168.0.0/16"),  # RFC-1918
    ipaddress.ip_network("fc00::/7"),  # IPv6 ULA
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
    ipaddress.ip_network("100.64.0.0/10"),  # Carrier-grade NAT (RFC 6598)
    ipaddress.ip_network("0.0.0.0/8"),  # "This" network
    ipaddress.ip_network("240.0.0.0/4"),  # Reserved
)


def _validate_health_url(url: str) -> None:
    """Raise ValueError if url is not a safe http/https endpoint.

    Rejects non-http/https schemes and RFC-1918/link-local addresses to prevent
    SSRF via user-controlled deploy_config.web_health_url.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"health_url must use http or https, got {parsed.scheme!r}")
    host = parsed.hostname or ""
    if not host:
        raise ValueError(f"health_url {url!r} has no hostname")
    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    try:
        addrs.append(ipaddress.ip_address(host))
    except ValueError:
        # host is a name, not an IP literal — resolve it.
        try:
            addrs = [ipaddress.ip_address(r[4][0]) for r in socket.getaddrinfo(host, None)]
        except OSError as exc:
            raise ValueError(f"health_url hostname {host!r} could not be resolved: {exc}") from exc
    for addr in addrs:
        for net in _BLOCKED_HEALTH_NETS:
            if addr in net:
                raise ValueError(
                    f"health_url {url!r} resolves to a blocked address ({addr}); "
                    "RFC-1918 and link-local targets are not permitted"
                )


def detect_command(repo_path: str | Path, deploy_cmd: str = "") -> list[str] | None:
    """Return the deploy command for this repo, or None for a no-op."""
    root = Path(repo_path)
    if (root / "deploy" / "release.sh").exists():
        return ["bash", "deploy/release.sh"]
    if deploy_cmd.strip():
        return shlex.split(deploy_cmd)
    return None


async def sync_deploy_checkout(repo_path: str | Path, base: str) -> None:
    """Fetch origin and fast-forward the deploy checkout to origin/<base>.

    Raises RuntimeError if the checkout is dirty or any git command fails.
    """
    root = Path(repo_path)

    async def _run(*args: str) -> tuple[bytes, bytes, int]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(root),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            raise
        return out, err, proc.returncode

    _, err, code = await _run("fetch", "origin")
    if code != 0:
        raise RuntimeError(f"deploy checkout sync failed: {err.decode(errors='replace').strip()}")

    for _lf in _REGENERABLE_LOCKFILES:
        _, _, _rc = await _run("checkout", "--", _lf)
        if _rc != 0:
            log.debug("deploy checkout: restore %s skipped (rc=%d)", _lf, _rc)

    out, _, _ = await _run("status", "--porcelain", "--untracked-files=no")
    if out.strip():
        raise RuntimeError(
            "deploy checkout has uncommitted changes; not syncing to avoid data loss"
        )

    _, err, code = await _run("checkout", "--end-of-options", base)
    if code != 0:
        raise RuntimeError(f"deploy checkout sync failed: {err.decode(errors='replace').strip()}")

    _, err, code = await _run("merge", "--ff-only", "--", f"origin/{base}")
    if code != 0:
        raise RuntimeError(f"deploy checkout sync failed: {err.decode(errors='replace').strip()}")


async def _pipeline_self_update(repo_path: Path, base: str = "HEAD~1") -> bool:
    """Return True if the sync range (base..HEAD) touched hyqs/pipeline/ source files."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(repo_path),
            "diff",
            "--name-only",
            "--end-of-options",
            base,
            "HEAD",
            "--",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            return False
        return any(line.startswith("hyqs/pipeline/") for line in out.decode().splitlines())
    except Exception:  # noqa: BLE001
        return False


async def verify_http_health(url: str, max_tries: int = 10, poll_interval: float = 3.0) -> bool:
    """Poll GET <url> until curl exits 0 (HTTP 2xx) or max_tries exhausted.

    Raises ValueError for unsafe URLs (non-http/https scheme or RFC-1918/link-local address).
    """
    if not url:
        return True
    _validate_health_url(url)  # raises ValueError for unsafe targets
    for attempt in range(max_tries):
        try:
            proc = await asyncio.create_subprocess_exec(
                "curl",
                "-fs",
                "--max-time",
                "5",
                url,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode == 0:
                return True
        except (OSError, asyncio.TimeoutError) as exc:
            log.debug("verify_http_health: attempt %d failed: %s", attempt, exc)
        if attempt < max_tries - 1:
            await asyncio.sleep(poll_interval)
    return False


async def check_public_health(url: str, timeout: float = 5.0) -> dict:
    """Single-shot HTTP status probe for a project's public URL.

    Unlike verify_http_health (which polls until success or exhaustion, for
    gating a deploy), this is a status read: it never raises and never
    retries, returning {'http_status', 'ok', 'reason'} best-effort.
    """
    if not url:
        return {"http_status": None, "ok": True, "reason": ""}
    try:
        _validate_health_url(url)
    except ValueError as exc:
        return {"http_status": None, "ok": False, "reason": str(exc)}
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl",
            "-s",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "--max-time",
            str(timeout),
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout + 5)
    except asyncio.TimeoutError:
        return {"http_status": None, "ok": False, "reason": "timeout"}
    except OSError as exc:
        return {"http_status": None, "ok": False, "reason": f"unreachable: {exc}"}
    if proc.returncode != 0:
        return {"http_status": None, "ok": False, "reason": "unreachable"}
    try:
        status = int(out.decode().strip())
    except ValueError:
        return {"http_status": None, "ok": False, "reason": "unreachable"}
    ok = 200 <= status < 400
    return {"http_status": status, "ok": ok, "reason": "" if ok else f"http {status}"}


async def lockfile_changed(
    repo_path: str | Path, prev_commit: str, lockfile: str = "package-lock.json"
) -> bool:
    """Return True if lockfile was modified between prev_commit and HEAD."""
    if not prev_commit:
        return True
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(repo_path),
            "diff",
            "--name-only",
            "--end-of-options",
            prev_commit,
            "HEAD",
            "--",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            return True
        return any(Path(line).name == lockfile for line in out.decode().splitlines() if line)
    except Exception:
        return True


async def verify_web_service_running(
    service: str, max_tries: int = 3, interval: float = 2.0
) -> bool:
    """Return True iff ``service`` is active according to systemctl --user."""
    if not service:
        return True
    for attempt in range(max_tries):
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl",
                "--user",
                "is-active",
                service,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode == 0:
                return True
        except Exception:
            pass
        if attempt < max_tries - 1:
            await asyncio.sleep(interval)
    return False


async def run_deploy(
    repo_path: str | Path,
    deploy_cmd: str = "",
    *,
    projects_dir: str | Path = "",
    deploy_config: str = "",
    log_sink: Callable[[str], Awaitable[None]] | None = None,
    base_branch: str = "",
    web_service: str = "",
    prev_commit: str = "",
    health_url: str = "",
    release: "Release | None" = None,
) -> dict:
    """Detect + run the deploy command. Returns {deployed, command, output, self_update, verified}."""
    if base_branch:
        try:
            await sync_deploy_checkout(repo_path, base_branch)
        except (RuntimeError, asyncio.TimeoutError) as e:
            return {
                "deployed": False,
                "command": "sync",
                "output": str(e),
                "self_update": False,
                "verified": False,
                "resource": None,
            }
    if projects_dir:
        from hyqs.pipeline import docker_deploy as _docker

        if _docker.is_user_project(repo_path, projects_dir):
            if release is not None and release.image_digest and registry_push.pull_configured():
                return await _docker.pull_deploy(
                    repo_path, release, deploy_config, log_sink=log_sink
                )
            return await _docker.docker_deploy(repo_path, deploy_config, log_sink=log_sink)
    cmd = detect_command(repo_path, deploy_cmd)
    if cmd is None:
        return {
            "deployed": True,
            "command": "(none)",
            "output": "no deploy command configured",
            "self_update": False,
            "verified": True,
            "resource": None,
        }
    log.info("deploying in %s: %s", repo_path, " ".join(cmd))
    _env: dict[str, str] | None = None
    if cmd == ["bash", "deploy/release.sh"] and prev_commit:
        _lf_changed = await lockfile_changed(repo_path, prev_commit)
        if not _lf_changed:
            log.info("deploy: lockfile unchanged since %s; skipping npm ci", prev_commit[:12])
            _env = {**os.environ, "SKIP_NPM_CI": "1"}
    try:
        returncode, output, record = await resources.measure_subprocess(
            cmd, cwd=str(repo_path), timeout=DEPLOY_TIMEOUT, log_sink=log_sink, env=_env
        )
        deployed = returncode == 0
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        return {
            "deployed": False,
            "command": " ".join(cmd),
            "output": f"could not run deploy: {e}",
            "verified": False,
            "resource": None,
        }

    lines = output.strip().splitlines()
    full = "\n".join(lines[-400:])
    result: dict = {
        "deployed": deployed,
        "command": " ".join(cmd),
        "output": full or "(no output)",
        "resource": record,
    }
    if deployed:
        result["self_update"] = await _pipeline_self_update(
            Path(repo_path), prev_commit or "HEAD~1"
        )
        svc_ok = await verify_web_service_running(web_service)
        http_ok = await verify_http_health(health_url) if health_url else True
        result["verified"] = svc_ok and http_ok
    return result


_WEB_SWAP_CONFIRMATION_MARKER = "is healthy (matching pid confirmed via"
_RELEASE_SH_COMMAND = "bash deploy/release.sh"


def web_swap_confirmed(result: dict) -> bool:
    """True iff a platform self-deploy actually swapped the hyqs-web process.

    Only ``deploy/release.sh`` (the platform self-deploy path) can swap the web
    process — user/managed projects always go through docker_deploy. Even on
    that path, release.sh may have skipped the web replacement entirely (no
    changed paths require it, or no active hyqs-web* unit was found), so this
    also requires release.sh's own positive confirmation line for the web
    block, distinct from the pipeline block's confirmation further down.
    """
    if result.get("command") != _RELEASE_SH_COMMAND:
        return False
    return _WEB_SWAP_CONFIRMATION_MARKER in result.get("output", "")


async def reconcile_deploying_job(store: "JobStore", job: "Job") -> bool:
    """Finalize a DEPLOYING job once its deployed_commit is verified live.

    'Live' means job.deployed_commit is an ancestor of (or equal to) the repo's
    current HEAD — not strict equality. A self-deploy job merged just before
    ANOTHER job merges and the fleet restarts would otherwise see HEAD move past
    its own deployed_commit and never satisfy an equality check, wedging it in
    DEPLOYING forever with a dead owner/expired lease (jobs #656/#684/#718/#720/#721).

    Returns True if the job was finalized to DONE; False if it should be left
    DEPLOYING (commit unreadable, or not yet/no-longer an ancestor of HEAD).
    """
    if not job.deployed_commit:
        return False
    head = await gitops.git(job.repo_path, "rev-parse", "HEAD")
    if not head.ok:
        return False
    current_commit = head.stdout.strip()
    if not await gitops.is_ancestor(job.repo_path, job.deployed_commit, current_commit):
        return False

    _environment_id = (job.source_meta or {}).get("environment_id")
    _release_id = (job.source_meta or {}).get("release_id")

    store.finalize_deploying_job(job.id, deployed_commit=current_commit)
    if job.project_id:
        prev = store.get_last_deploy(job.project_id, environment_id=_environment_id)
        prev_sha = prev.get("deployed_commit") if prev else None
        prev_is_newer = bool(prev_sha) and await gitops.is_ancestor(
            job.repo_path, current_commit, prev_sha
        )
        if prev_sha != current_commit and not prev_is_newer:
            store.record_deploy(
                job.project_id,
                current_commit,
                prev_sha,
                "pipeline_job",
                job_id=job.id,
                environment_id=_environment_id,
            )
        store.release_schema_lock(job.project_id, f"job-{job.id}")
        if _environment_id and _release_id:
            store.advance_promotion_deployed(job.id, _environment_id, _release_id)
    store.add_event(
        job.id,
        "deploy",
        "done",
        summary="post-restart verification passed",
        started_at=_now(),
        ended_at=_now(),
    )
    return True
