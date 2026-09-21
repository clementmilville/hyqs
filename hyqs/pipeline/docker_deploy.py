"""Docker-based deploy for user projects — NO AI, pure subprocess.

Builds a Docker image from the project's Dockerfile then runs/restarts a named
container with sandboxing (non-root user, dropped caps, memory/cpu limits).

Claude subscription credential mount (opt-in via deploy_cfg["claude_subscription"]):
  - The credential file is mounted READ-ONLY.  Only the HOST's Claude Code process
    may refresh the OAuth token; the container must re-read the file on every use.
  - App traffic from a subscribed container shares the SAME subscription rate-limit
    tier as the pipeline itself.  This is intended for personal/own-use deployments.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from . import registry_push, required_env, resources, secret_provider, secrets_contract

if TYPE_CHECKING:
    from .models import Release

log = logging.getLogger("hyqs.docker_deploy")

_DEFAULT_PORT = 8080
_DEFAULT_MEM = "256m"
_DEFAULT_CPUS = "0.5"
_DEFAULT_NETWORK = "bridge"
_DEFAULT_PROBE_DATA_MAX_BYTES = 2 * 1024**3  # 2 GiB


_SI_UNITS: dict[str, int] = {
    "b": 1,
    "kb": 1_000,
    "mb": 1_000_000,
    "gb": 1_000_000_000,
    "tb": 1_000_000_000_000,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}
_SI_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]*)$")


def _parse_si_bytes(s: str) -> int:
    """Parse a Docker-style human-readable byte string ('1.5kB', '123B', '2.3MB') to int bytes."""
    m = _SI_RE.match(s.strip())
    if not m:
        raise ValueError(f"Cannot parse byte string: {s!r}")
    num_s, unit = m.group(1), m.group(2)
    multiplier = _SI_UNITS.get(unit.lower(), 1) if unit else 1
    return int(float(num_s) * multiplier)


async def _read_docker_net_bytes(container_name: str) -> int | None:
    """Return total network bytes (RX+TX) for a container via docker stats --no-stream."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{json .}}",
            container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        data = json.loads(out.decode().strip())
        net_io = data.get("NetIO", "")
        if "/" not in net_io:
            return None
        rx_str, tx_str = net_io.split("/", 1)
        return _parse_si_bytes(rx_str) + _parse_si_bytes(tx_str)
    except Exception:  # noqa: BLE001
        return None


def is_user_project(repo_path: str | Path, projects_dir: str | Path) -> bool:
    """True when repo_path is under projects_dir (resolved to handle symlinks)."""
    if not projects_dir:
        return False
    try:
        Path(repo_path).resolve().relative_to(Path(projects_dir).resolve())
        return True
    except ValueError:
        return False


def _parse_deploy_config(json_str: str) -> dict:
    if not json_str:
        return {}
    try:
        return json.loads(json_str)
    except (TypeError, ValueError):
        return {}


def _credentials_path() -> Path:
    return Path.home() / ".claude" / ".credentials.json"


def _subscription_user_and_mounts(deploy_cfg: dict) -> list[str]:
    if not deploy_cfg.get("claude_subscription"):
        return ["--user", "65534:65534"]
    creds = _credentials_path()
    if not creds.exists():
        raise RuntimeError(f"claude_subscription requested but credential file not found: {creds}")
    return [
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--volume",
        f"{creds}:/home/app/.claude/.credentials.json:ro",
        "--env",
        "HOME=/home/app",
    ]


def _slug(repo_path: str | Path) -> str:
    name = Path(repo_path).name.lower()
    return re.sub(r"[^a-z0-9]+", "-", name).strip("-") or "app"


def _image_name(repo_path: str | Path) -> str:
    return "hyqs-" + _slug(repo_path)


def _container_name(repo_path: str | Path) -> str:
    return "hyqs-" + _slug(repo_path)


async def build_image(
    repo_path: str | Path,
    image_name: str,
    *,
    log_sink: Callable[[str], Awaitable[None]] | None = None,
    timeout: int = 300,
) -> tuple[bool, "resources.ResourceRecord | None"]:
    """Run `docker build -t <image_name> .` in repo_path. Returns (success, resource_record)."""
    try:
        returncode, _output, record = await resources.measure_subprocess(
            ["docker", "build", "-t", image_name, "."],
            cwd=str(repo_path),
            timeout=timeout,
            log_sink=log_sink,
            env=resources.minimal_subprocess_env(),
        )
        return returncode == 0, record
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("docker build failed: %s", e)
        return False, None


def _sum_dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _chmod_world_writable_for_container(path: Path) -> None:
    # The container runs as an arbitrary non-root uid (65534 or the host uid),
    # which the host process cannot chown to ahead of time, so the mount must
    # be world-writable for the container to open/create files under it.
    os.chmod(path, 0o777)  # nosec B103


def _copy_probe_data_dir(data_dir: Path) -> Path:
    dest = data_dir.parent / f".{data_dir.name}-probe-{os.getpid()}-{int(time.time() * 1000)}"
    shutil.copytree(data_dir, dest, dirs_exist_ok=True)
    # This is a throwaway copy under the projects dir, not the real data_dir,
    # and is deleted after the probe exits.
    _chmod_world_writable_for_container(dest)
    return dest


async def _prepare_probe_data_dir(data_dir: Path, deploy_cfg: dict) -> Path | None:
    """Snapshot data_dir into a throwaway sibling dir for the probe to mount at /data.

    Apps that open/create files (e.g. SQLite DBs) or run migrations under /data at
    startup need a real, writable data dir to boot — a read-only mount of the real
    data_dir is not enough, and mounting the real data_dir directly risks the
    throwaway probe corrupting prod data. Returns None (skipping the /data mount
    entirely) when data_dir is too large to cheaply copy or on any OS error.
    """
    max_bytes = int(deploy_cfg.get("probe_data_max_bytes", _DEFAULT_PROBE_DATA_MAX_BYTES))
    try:
        size = await asyncio.to_thread(_sum_dir_size, data_dir)
        if size > max_bytes:
            log.warning(
                "data_dir %s is %d bytes (> %d byte probe limit); skipping probe data snapshot",
                data_dir,
                size,
                max_bytes,
            )
            return None
        return await asyncio.to_thread(_copy_probe_data_dir, data_dir)
    except OSError as exc:
        log.warning("failed to snapshot data_dir %s for probe: %s", data_dir, exc)
        return None


async def _cleanup_probe_data_dir(path: Path) -> None:
    await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)


async def run_or_restart_container(
    image_name: str,
    container_name: str,
    deploy_cfg: dict,
    *,
    env_file: Path | None = None,
    data_dir: Path | None = None,
    repo_path: str | Path | None = None,
    log_sink: Callable[[str], Awaitable[None]] | None = None,
    timeout: int = 60,
    health_path: str = "/",
    http_timeout: float = 30.0,
    http_poll_interval: float = 2.0,
) -> tuple[bool, str, "resources.ResourceRecord | None"]:
    """Health-gated cutover: start a probe container, verify it is healthy,
    then and only then remove the old canonical container and promote the new image."""
    port = int(deploy_cfg.get("port") or _DEFAULT_PORT)
    mem = str(deploy_cfg.get("mem") or _DEFAULT_MEM)
    cpus = str(deploy_cfg.get("cpus") or _DEFAULT_CPUS)
    network = str(deploy_cfg.get("network") or _DEFAULT_NETWORK)

    t0 = time.monotonic()

    if data_dir is not None:
        data_dir.mkdir(parents=True, exist_ok=True)
        _chmod_world_writable_for_container(data_dir)

    probe_name = f"{container_name}-probe"

    sandbox_flags = [
        *_subscription_user_and_mounts(deploy_cfg),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        mem,
        "--cpus",
        cpus,
        "--network",
        network,
    ]

    # Probe: no port binding, no restart policy. Mounts a THROWAWAY COPY of
    # data_dir (never the real data_dir) so apps that open/create files under
    # /data at startup (e.g. SQLite + migrations) boot successfully with no
    # risk of the probe mutating prod data.
    probe_data_dir: Path | None = None
    if data_dir is not None:
        probe_data_dir = await _prepare_probe_data_dir(data_dir, deploy_cfg)

    # Pre-clean: a crashed prior deploy can leave a container holding the probe
    # name; `docker run --name` would then fail on the conflict forever.
    await _remove_container(probe_name)

    probe_cmd = ["docker", "run", "-d", "--name", probe_name, *sandbox_flags]
    if env_file is not None and env_file.is_file():
        probe_cmd += ["--env-file", str(env_file)]
    if probe_data_dir is not None:
        probe_cmd += ["-v", f"{probe_data_dir}:/data"]
    probe_cmd.append(image_name)

    log.info("starting probe container: %s", " ".join(probe_cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *probe_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            fail_msg = out.decode(errors="replace") or "probe container failed to start"
            wall_seconds = time.monotonic() - t0
            if probe_data_dir is not None:
                await _cleanup_probe_data_dir(probe_data_dir)
            return (
                False,
                fail_msg,
                resources.ResourceRecord(
                    cpu_seconds=None,
                    peak_rss_bytes=None,
                    io_read_bytes=None,
                    io_write_bytes=None,
                    wall_seconds=wall_seconds,
                    sampled_at=resources._now_iso(),
                ),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("probe docker run failed: %s", exc)
        wall_seconds = time.monotonic() - t0
        if probe_data_dir is not None:
            await _cleanup_probe_data_dir(probe_data_dir)
        return (
            False,
            f"probe container failed to start: {exc}",
            resources.ResourceRecord(
                cpu_seconds=None,
                peak_rss_bytes=None,
                io_read_bytes=None,
                io_write_bytes=None,
                wall_seconds=wall_seconds,
                sampled_at=resources._now_iso(),
            ),
        )

    # Verify probe reaches 'running'; capture logs on crash.
    running, fail_logs = await verify_container_running(probe_name, port=None)

    if running:
        ip = await _get_container_ip(probe_name)
        if ip:
            http_ok, http_reason = await _probe_http(
                8080, health_path, http_timeout, http_poll_interval, host=ip
            )
            if not http_ok:
                probe_logs = await _capture_container_logs(probe_name)
                fail_logs = f"{probe_logs}\n{http_reason}".strip() if probe_logs else http_reason
                running = False
        else:
            probe_logs = await _capture_container_logs(probe_name)
            exit_info = await _probe_exit_info(probe_name)
            pieces = [p for p in (probe_logs, exit_info) if p]
            fail_logs = "\n".join(pieces) if pieces else "could not determine probe container IP"
            running = False

    if running and repo_path is not None:
        required_vars = _load_required_env(repo_path)
        if required_vars:
            probe_env = await _read_container_env_by_name(probe_name)
            missing = required_env.find_missing_required_env(
                required_vars, {container_name: probe_env}
            )
            if missing:
                fail_logs = required_env.format_missing_env_message(missing, [container_name])
                running = False

    wall_seconds = time.monotonic() - t0
    record = resources.ResourceRecord(
        cpu_seconds=None,
        peak_rss_bytes=None,
        io_read_bytes=None,
        io_write_bytes=None,
        wall_seconds=wall_seconds,
        sampled_at=resources._now_iso(),
    )

    # Always remove the probe container and its throwaway data snapshot.
    await _remove_container(probe_name)
    if probe_data_dir is not None:
        await _cleanup_probe_data_dir(probe_data_dir)

    if not running:
        # Old canonical is untouched and still serving.
        return False, fail_logs, record

    # Healthy: stop old canonical, then promote the new image.
    await _remove_container(container_name)

    canonical_cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        container_name,
        "--restart",
        "unless-stopped",
        "-p",
        f"127.0.0.1:{port}:8080",
        *sandbox_flags,
    ]
    if env_file is not None and env_file.is_file():
        canonical_cmd += ["--env-file", str(env_file)]
    if data_dir is not None:
        canonical_cmd += ["-v", f"{data_dir}:/data"]
    canonical_cmd.append(image_name)

    log.info("starting canonical container: %s", " ".join(canonical_cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *canonical_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if log_sink is not None:
            for line in out.decode(errors="replace").splitlines():
                await log_sink(line)
        if proc.returncode != 0:
            return False, out.decode(errors="replace") or "canonical docker run failed", record
    except Exception as exc:  # noqa: BLE001
        log.warning("canonical docker run failed: %s", exc)
        return False, f"canonical docker run failed: {exc}", record

    return True, "", record


async def verify_container_running(
    container_name: str,
    *,
    poll_interval: float = 2.0,
    max_wait: float = 10.0,
    port: int | None = None,
    health_path: str = "/",
    http_timeout: float = 30.0,
    http_poll_interval: float = 2.0,
) -> tuple[bool, str]:
    """Poll docker inspect until the container is running or has crashed.

    Returns (True, "") when the container reaches Running status within
    max_wait seconds AND passes the HTTP health probe (when port is set).
    Returns (False, logs) when it exits, crash-loops, or never serves HTTP.
    """
    deadline = time.monotonic() + max_wait
    while True:
        status = ""
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "inspect",
                "--format",
                "{{.State.Status}}",
                container_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            status = out.decode().strip()
        except Exception:
            pass

        if status == "running":
            if port is None:
                return True, ""
            ok, reason = await _probe_http(port, health_path, http_timeout, http_poll_interval)
            if not ok:
                logs = await _capture_container_logs(container_name)
                detail = f"{logs}\n{reason}" if logs else reason
                return False, detail
            return True, ""

        if status in ("exited", "dead"):
            logs = await _capture_container_logs(container_name)
            return False, logs

        if time.monotonic() >= deadline:
            # Timed out — treat non-running as crashed and capture logs
            logs = await _capture_container_logs(container_name)
            return False, logs or f"container status={status!r} after {max_wait}s"

        await asyncio.sleep(poll_interval)


async def _capture_container_logs(container_name: str) -> str:
    """Return the last 50 log lines from a container (stdout+stderr combined)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "logs",
            "--tail",
            "50",
            container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        return out.decode(errors="replace")
    except Exception:
        return ""


async def _remove_container(name: str) -> None:
    """Stop then remove a container, suppressing all errors."""
    for sub_cmd in (["docker", "stop", name], ["docker", "rm", name]):
        try:
            proc = await asyncio.create_subprocess_exec(
                *sub_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30)
        except Exception:
            pass


async def _container_unowned_by_compose(name: str) -> bool:
    """True when a container with ``name`` exists but has no compose-project label.

    Such a container was created outside compose (legacy `docker run` deploys);
    `docker compose up` cannot replace it and dies on the name conflict.
    Returns False when the container doesn't exist or inspection fails.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "inspect",
            name,
            "--format",
            '{{index .Config.Labels "com.docker.compose.project"}}',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            return False  # no such container
        return out.decode(errors="replace").strip() == ""
    except Exception:  # noqa: BLE001 - inspection is best-effort
        return False


async def _get_container_ip(container_name: str) -> str | None:
    """Return the container's bridge IP address, or None on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "inspect",
            "--format",
            "{{.NetworkSettings.IPAddress}}",
            container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        ip = out.decode().strip()
        return ip if ip else None
    except Exception:
        return None


async def _read_container_env_by_name(name: str) -> dict[str, str]:
    """Return a container's env as a dict, parsed from `docker inspect .Config.Env`."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "inspect",
            "--format",
            "{{json .Config.Env}}",
            name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            return {}
        raw = json.loads(out.decode(errors="replace").strip())
    except Exception:  # noqa: BLE001
        return {}
    env: dict[str, str] = {}
    for entry in raw or []:
        key, sep, value = str(entry).partition("=")
        if sep:
            env[key] = value
    return env


def _load_required_env(repo_path: str | Path) -> list["required_env.RequiredEnvVar"]:
    """Read `<repo_path>/deploy/required-env`, or [] (no-op) if absent."""
    manifest = Path(repo_path) / "deploy" / "required-env"
    if not manifest.is_file():
        return []
    return required_env.parse_required_env(manifest.read_text())


async def _probe_exit_info(container_name: str) -> str:
    """Return the container's status/exit-code/error via docker inspect, or '' on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "inspect",
            "--format",
            "{{.State.Status}} exit_code={{.State.ExitCode}} error={{.State.Error}}",
            container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        return out.decode(errors="replace").strip()
    except Exception:
        return ""


async def _probe_http(
    port: int,
    health_path: str,
    timeout: float,
    poll_interval: float,
    host: str = "127.0.0.1",
) -> tuple[bool, str]:
    """Poll http://{host}:{port}{health_path} until a non-5xx response or deadline."""
    url = f"http://{host}:{port}{health_path}"
    deadline = time.monotonic() + timeout
    last_reason = f"no response from {url} within {timeout}s"
    loop = asyncio.get_running_loop()

    while True:

        def _fetch() -> int | None:
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    return resp.status
            except urllib.error.HTTPError as e:
                return e.code
            except (urllib.error.URLError, OSError):
                return None

        status = await loop.run_in_executor(None, _fetch)
        if status is not None and status < 500:
            return True, ""
        if status is not None:
            last_reason = f"HTTP {status} from {url}"

        if time.monotonic() >= deadline:
            return False, last_reason

        await asyncio.sleep(poll_interval)


async def _get_container_logs(container_name: str, tail: int = 50) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "logs",
            "--tail",
            str(tail),
            container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        return out.decode(errors="replace").strip()
    except Exception:
        return ""


def _parse_container_ports(raw_ports: dict | None) -> list[dict]:
    """Normalize docker inspect's `NetworkSettings.Ports` map to a flat list.

    Input shape: {'8080/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '8364'}], '9090/tcp': None}.
    Unpublished entries (null or empty list) are skipped.
    """
    if not raw_ports:
        return []
    published: list[dict] = []
    for port_proto, bindings in raw_ports.items():
        if not bindings:
            continue
        container_port, _, protocol = port_proto.partition("/")
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            host_port = binding.get("HostPort", "")
            if not host_port:
                continue
            published.append(
                {
                    "host_ip": binding.get("HostIp", ""),
                    "host_port": host_port,
                    "container_port": container_port,
                    "protocol": protocol or "tcp",
                }
            )
    return published


def _parse_compose_publishers(publishers: list | None) -> list[dict]:
    """Normalize a compose service's `Publishers` field to the same shape as
    `_parse_container_ports`."""
    if not publishers:
        return []
    published: list[dict] = []
    for pub in publishers:
        if not isinstance(pub, dict):
            continue
        published_port = pub.get("PublishedPort")
        if not published_port:
            continue
        published.append(
            {
                "host_ip": pub.get("URL", ""),
                "host_port": str(published_port),
                "container_port": str(pub.get("TargetPort", "")),
                "protocol": pub.get("Protocol", "") or "tcp",
            }
        )
    return published


async def get_container_status(repo_path: str | Path) -> dict:
    container_name = _container_name(repo_path)
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "inspect",
            "--format",
            "{{json .}}",
            container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        raw = json.loads(out.decode().strip())
        if isinstance(raw, list):
            raw = raw[0]
        state = raw.get("State", {})
        network_settings = raw.get("NetworkSettings", {}) or {}
        return {
            "container_name": container_name,
            "state": state.get("Status", "unknown"),
            "health": state.get("Health", {}).get("Status", ""),
            "image_id": raw.get("Image", ""),
            "started_at": state.get("StartedAt", ""),
            "finished_at": state.get("FinishedAt", ""),
            "published_ports": _parse_container_ports(network_settings.get("Ports")),
        }
    except Exception:  # noqa: BLE001
        return {"container_name": container_name, "state": "not_found"}


async def get_compose_status(repo_path: str | Path, deploy_config_json: str = "") -> list[dict]:
    """Best-effort per-service status for a compose-deployed project.

    Never raises: any subprocess/parse failure yields an empty list, matching
    the file's existing best-effort style.
    """
    deploy_cfg = _parse_deploy_config(deploy_config_json)
    repo = Path(repo_path)
    port = int(deploy_cfg.get("port") or _DEFAULT_PORT)
    container = _container_name(repo_path)
    compose_file = repo / "docker-compose.yml"
    env = _minimal_compose_env(port, container)
    try:
        services = await _compose_ps(compose_file, env, repo)
    except Exception:  # noqa: BLE001
        return []
    statuses: list[dict] = []
    for svc in services:
        statuses.append(
            {
                "name": svc.get("Service") or svc.get("Name") or "unknown",
                "state": str(svc.get("State", "")).lower() or "unknown",
                "health": str(svc.get("Health", "")).lower(),
                "started_at": svc.get("CreatedAt", ""),
                "image_id": svc.get("Image", ""),
                "published_ports": _parse_compose_publishers(svc.get("Publishers")),
            }
        )
    return statuses


def _ensure_env_secrets(repo_path: Path, slug: str, secrets_dir: Path) -> Path | None:
    """Symlink <secrets_dir>/<slug>.env as <repo>/.env.secrets. Returns path or None."""
    src = secrets_dir / f"{slug}.env"
    if not src.exists():
        return None
    dest = repo_path / ".env.secrets"
    if dest.is_symlink() and dest.resolve() == src.resolve():
        return dest
    if dest.exists() or dest.is_symlink():
        dest.unlink()
    dest.symlink_to(src)
    return dest


def _minimal_compose_env(port: int, container: str) -> dict[str, str]:
    """Build the env passed to `docker compose` from only allowlisted host keys.

    The pipeline worker's own .env (POSTGRES_PASSWORD, ANTHROPIC_*, HYQS_DB_URL,
    etc.) must never leak into a project's containers via compose var-substitution
    precedence — the project's own .env/.env.secrets must be the source of truth.
    """
    return resources.minimal_subprocess_env(extra={"PORT": str(port), "CONTAINER_NAME": container})


def _parse_compose_ps_output(text: str) -> list[dict]:
    """Parse `docker compose ps --format json` output: a JSON array or NDJSON."""
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        return [data]
    services: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(obj, dict):
            services.append(obj)
    return services


async def _compose_ps(compose_file: Path, env: dict[str, str], cwd: str | Path) -> list[dict]:
    """Run `docker compose -f <file> ps --all --format json`; [] on any error."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "ps",
            "--all",
            "--format",
            "json",
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            return []
        return _parse_compose_ps_output(out.decode(errors="replace"))
    except Exception:  # noqa: BLE001
        return []


async def _compose_service_logs(
    compose_file: Path, env: dict[str, str], cwd: str | Path, service: str, tail: int = 50
) -> str:
    """Return the last `tail` log lines for a single compose service."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "logs",
            "--tail",
            str(tail),
            service,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        return out.decode(errors="replace").strip()
    except Exception:  # noqa: BLE001
        return ""


async def _verify_compose_services_healthy(
    compose_file: Path,
    env: dict[str, str],
    cwd: str | Path,
    *,
    max_wait: float = 30.0,
    poll_interval: float = 2.0,
) -> tuple[bool, str]:
    """Poll `docker compose ps` until every service is running (and healthy, if
    it reports a Health field). Returns (True, '') once all pass.

    If `_compose_ps` cannot be inspected at all during the poll window (older
    compose without JSON `ps` support, transient error), this gate is
    inconclusive and returns (True, '') — the HTTP probe remains the hard gate
    in that case, so we never block a deploy purely because we can't introspect it.
    """
    deadline = time.monotonic() + max_wait
    ever_inspected = False
    last_bad: list[tuple[str, str]] = []

    while True:
        services = await _compose_ps(compose_file, env, cwd)
        if services:
            ever_inspected = True
            bad = []
            for svc in services:
                name = svc.get("Service") or svc.get("Name") or "unknown"
                state = str(svc.get("State", "")).lower()
                health = str(svc.get("Health", "")).lower()
                if "running" not in state:
                    bad.append((name, f"state={state or 'unknown'}"))
                elif health and health != "healthy":
                    bad.append((name, f"health={health}"))
            if not bad:
                return True, ""
            last_bad = bad

        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(poll_interval)

    if not ever_inspected:
        return True, ""

    details = []
    for name, reason in last_bad:
        logs = await _compose_service_logs(compose_file, env, cwd, name)
        detail = f"{name}: {reason}"
        if logs:
            detail += f"\n{logs}"
        details.append(detail)
    return False, "\n\n".join(details)


async def compose_deploy(
    repo_path: str | Path,
    deploy_config_json: str = "",
    *,
    log_sink: Callable[[str], Awaitable[None]] | None = None,
) -> dict:
    """Deploy via the repo's own docker-compose.yml. Returns the same shape as docker_deploy."""
    deploy_cfg = _parse_deploy_config(deploy_config_json)
    repo = Path(repo_path)
    slug = _slug(repo_path)
    port = int(deploy_cfg.get("port") or _DEFAULT_PORT)
    secrets_dir = Path(deploy_cfg.get("secrets_dir") or Path.home() / ".hyqs" / "secrets")
    container = _container_name(repo_path)
    compose_file = repo / "docker-compose.yml"
    health_paths = deploy_cfg.get("health_paths") or [str(deploy_cfg.get("health_path") or "/")]
    http_timeout = float(deploy_cfg.get("http_timeout") or 30)
    http_poll_interval = float(deploy_cfg.get("http_poll_interval") or 2)
    compose_verify_max_wait = float(deploy_cfg.get("compose_verify_max_wait") or 30)
    compose_verify_poll_interval = float(deploy_cfg.get("compose_verify_poll_interval") or 2)

    _ensure_env_secrets(repo, slug, secrets_dir)

    # Takeover pre-clean: if a container already holds the target name but is
    # NOT owned by this compose project (e.g. created by the legacy docker-run
    # deploy path), `compose up` refuses to replace it and every deploy fails
    # with a name conflict — ledger-app froze for 11h this way. Remove the
    # unowned container so compose can own the name from here on. Containers
    # compose already owns are left for compose to recreate in place.
    if await _container_unowned_by_compose(container):
        log.warning(
            "compose deploy: removing container %r not owned by compose (legacy deploy path)",
            container,
        )
        await _remove_container(container)

    cmd = ["docker", "compose", "-f", str(compose_file), "up", "-d", "--build"]
    env = _minimal_compose_env(port, container)

    returncode, output, record = await resources.measure_subprocess(
        cmd,
        cwd=str(repo),
        timeout=600,
        log_sink=log_sink,
        env=env,
    )

    if returncode != 0:
        return {
            "deployed": False,
            "command": " ".join(cmd),
            "output": output or "docker compose up failed",
            "self_update": False,
            "verified": False,
            "resource": record,
        }

    services_ok, services_fail = await _verify_compose_services_healthy(
        compose_file,
        env,
        repo,
        max_wait=compose_verify_max_wait,
        poll_interval=compose_verify_poll_interval,
    )
    if not services_ok:
        detail = services_fail[-3000:] if services_fail else "compose service unhealthy"
        output = f"{output}\n{detail}" if output else detail
        return {
            "deployed": False,
            "command": " ".join(cmd),
            "output": output,
            "self_update": False,
            "verified": False,
            "resource": record,
        }

    required_vars = _load_required_env(repo)
    if required_vars:
        services = await _compose_ps(compose_file, env, repo)
        service_envs: dict[str, dict[str, str]] = {}
        for svc in services:
            name = svc.get("Service") or svc.get("Name") or "unknown"
            ref = svc.get("Name") or svc.get("ID")
            if ref:
                service_envs[name] = await _read_container_env_by_name(ref)
        missing = required_env.find_missing_required_env(required_vars, service_envs)
        if missing:
            detail = required_env.format_missing_env_message(missing, list(service_envs.keys()))
            output = f"{output}\n{detail}" if output else detail
            return {
                "deployed": False,
                "command": " ".join(cmd),
                "output": output,
                "self_update": False,
                "verified": False,
                "resource": record,
            }

    verified = True
    for path in health_paths:
        ok, reason = await _probe_http(port, path, http_timeout, http_poll_interval)
        if not ok:
            verified = False
            output = f"{output}\n{reason}" if output else reason
            break

    return {
        "deployed": verified,
        "command": " ".join(cmd),
        "output": output or ("container started" if verified else "container not running"),
        "self_update": False,
        "verified": verified,
        "resource": record,
    }


async def compose_down(repo_path: str | Path, deploy_config_json: str = "") -> dict:
    """Stop and remove a compose project's containers. Best-effort: never raises."""
    deploy_cfg = _parse_deploy_config(deploy_config_json)
    repo = Path(repo_path)
    port = int(deploy_cfg.get("port") or _DEFAULT_PORT)
    container = _container_name(repo_path)
    compose_file = repo / "docker-compose.yml"
    env = _minimal_compose_env(port, container)
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "down",
            cwd=str(repo),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        return {"stopped": proc.returncode == 0, "output": out.decode(errors="replace")}
    except Exception as exc:  # noqa: BLE001
        log.warning("compose down failed: %s", exc)
        return {"stopped": False, "output": str(exc)}


async def compose_up_existing(
    repo_path: str | Path,
    deploy_config_json: str = "",
    *,
    log_sink: Callable[[str], Awaitable[None]] | None = None,
) -> dict:
    """Bring a stopped compose project's containers back up without rebuilding.

    Reuses the same env-building (_minimal_compose_env / _ensure_env_secrets) as
    compose_deploy so PORT/CONTAINER_NAME injection can't drift between deploy
    and start. Best-effort: never raises.
    """
    deploy_cfg = _parse_deploy_config(deploy_config_json)
    repo = Path(repo_path)
    slug = _slug(repo_path)
    port = int(deploy_cfg.get("port") or _DEFAULT_PORT)
    secrets_dir = Path(deploy_cfg.get("secrets_dir") or Path.home() / ".hyqs" / "secrets")
    container = _container_name(repo_path)
    compose_file = repo / "docker-compose.yml"

    _ensure_env_secrets(repo, slug, secrets_dir)

    cmd = ["docker", "compose", "-f", str(compose_file), "up", "-d"]
    env = _minimal_compose_env(port, container)
    try:
        returncode, output, _record = await resources.measure_subprocess(
            cmd,
            cwd=str(repo),
            timeout=120,
            log_sink=log_sink,
            env=env,
        )
        return {"started": returncode == 0, "output": output}
    except Exception as exc:  # noqa: BLE001
        log.warning("compose up (start) failed: %s", exc)
        return {"started": False, "output": str(exc)}


async def stop_single_container(repo_path: str | Path) -> dict:
    """Stop a single-container (non-compose) deploy. Best-effort: never raises."""
    container = _container_name(repo_path)
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "stop",
            container,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        return {"stopped": proc.returncode == 0, "output": out.decode(errors="replace")}
    except Exception as exc:  # noqa: BLE001
        log.warning("docker stop failed: %s", exc)
        return {"stopped": False, "output": str(exc)}


async def start_single_container(repo_path: str | Path) -> dict:
    """Start a previously-stopped single container. Best-effort: never raises."""
    container = _container_name(repo_path)
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "start",
            container,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        return {"started": proc.returncode == 0, "output": out.decode(errors="replace")}
    except Exception as exc:  # noqa: BLE001
        log.warning("docker start failed: %s", exc)
        return {"started": False, "output": str(exc)}


async def docker_deploy(
    repo_path: str | Path,
    deploy_config_json: str = "",
    *,
    log_sink: Callable[[str], Awaitable[None]] | None = None,
) -> dict:
    """Build image then run/restart container. Returns {deployed, command, output, self_update, verified, resource}.

    Dispatches to compose_deploy when the repo contains docker-compose.yml; otherwise
    uses the legacy docker build + docker run path, which also best-effort pushes and
    signs the built image to the registry (image_ref/image_digest/signed keys — '' /
    None / False when the registry isn't configured or the push/sign fails).
    """
    if (Path(repo_path) / "docker-compose.yml").exists():
        return await compose_deploy(repo_path, deploy_config_json, log_sink=log_sink)

    deploy_cfg = _parse_deploy_config(deploy_config_json)
    image = _image_name(repo_path)
    container = _container_name(repo_path)
    port = int(deploy_cfg.get("port") or _DEFAULT_PORT)
    health_path = str(deploy_cfg.get("health_path") or "/")
    http_timeout = float(deploy_cfg.get("http_timeout") or 30)
    http_poll_interval = float(deploy_cfg.get("http_poll_interval") or 2)

    slug = _slug(repo_path)
    secrets_dir = Path(deploy_cfg.get("secrets_dir") or "~/.hyqs/secrets").expanduser()
    env_file_candidate = secrets_dir / f"{slug}.env"
    env_file = env_file_candidate if env_file_candidate.is_file() else None

    appdata_dir = Path(deploy_cfg.get("appdata_dir") or "~/.hyqs/appdata").expanduser()
    data_dir = appdata_dir / slug

    built, build_record = await build_image(repo_path, image, log_sink=log_sink)
    if not built:
        return {
            "deployed": False,
            "command": f"docker build -t {image} .",
            "output": "docker build failed",
            "self_update": False,
            "verified": False,
            "resource": None,
            "image_ref": "",
            "image_digest": None,
            "signed": False,
        }

    ok, fail_logs, run_record = await run_or_restart_container(
        image,
        container,
        deploy_cfg,
        env_file=env_file,
        data_dir=data_dir,
        repo_path=repo_path,
        log_sink=log_sink,
        health_path=health_path,
        http_timeout=http_timeout,
        http_poll_interval=http_poll_interval,
    )

    if ok and run_record is not None:
        net = await _read_docker_net_bytes(container)
        if net is not None:
            run_record.net_bytes = net
            run_record.net_bytes_approx = False

    if build_record is not None and run_record is not None:
        combined: "resources.ResourceRecord | None" = build_record + run_record
    else:
        combined = build_record or run_record

    if not ok:
        return {
            "deployed": False,
            "command": f"docker run --name {container} -p 127.0.0.1:{port}:8080 {image}",
            "output": fail_logs[-3000:] if fail_logs else "docker run failed",
            "self_update": False,
            "verified": False,
            "resource": combined,
            "image_ref": "",
            "image_digest": None,
            "signed": False,
        }

    try:
        artifact = await registry_push.push_and_sign(image, slug, log_sink=log_sink)
    except Exception as exc:  # noqa: BLE001 - a registry problem must never fail the deploy
        log.warning("registry push/sign failed: %s", exc)
        artifact = None
    image_ref = artifact["image_ref"] if artifact else ""
    image_digest = artifact["image_digest"] if artifact else None
    signed = artifact["signed"] if artifact else False

    return {
        "deployed": True,
        "command": f"docker run --name {container} -p 127.0.0.1:{port}:8080 {image}",
        "output": "container started",
        "self_update": False,
        "verified": True,
        "resource": combined,
        "image_ref": image_ref,
        "image_digest": image_digest,
        "signed": signed,
    }


async def pull_deploy(
    repo_path: str | Path,
    release: "Release",
    deploy_config_json: str = "",
    *,
    log_sink: Callable[[str], Awaitable[None]] | None = None,
) -> dict:
    """Pull a pre-built, cosign-signed release image by digest and cut over to it.

    The apply mechanic for promotion-driven deploys to hosts with no source to
    build from (a remote/client deploy host). Every step is fail-closed: a
    pull, signature-verify, or missing-secret failure aborts before any
    container is touched, returning deployed=verified=signed=False. Returns
    the same shape as docker_deploy().
    """
    deploy_cfg = _parse_deploy_config(deploy_config_json)
    container = _container_name(repo_path)
    health_path = str(deploy_cfg.get("health_path") or "/")
    http_timeout = float(deploy_cfg.get("http_timeout") or 30)
    http_poll_interval = float(deploy_cfg.get("http_poll_interval") or 2)

    slug = _slug(repo_path)
    secrets_dir = Path(deploy_cfg.get("secrets_dir") or "~/.hyqs/secrets").expanduser()
    env_file_candidate = secrets_dir / f"{slug}.env"
    env_file = env_file_candidate if env_file_candidate.is_file() else None

    appdata_dir = Path(deploy_cfg.get("appdata_dir") or "~/.hyqs/appdata").expanduser()
    data_dir = appdata_dir / slug

    ref_with_digest = f"{release.image_ref}@{release.image_digest}"
    abort = {
        "deployed": False,
        "command": f"docker pull {ref_with_digest}",
        "output": "",
        "self_update": False,
        "verified": False,
        "resource": None,
        "image_ref": release.image_ref,
        "image_digest": release.image_digest,
        "signed": False,
    }

    if not await registry_push.pull_image_by_digest(
        release.image_ref, release.image_digest, log_sink=log_sink
    ):
        return {**abort, "output": f"docker pull failed for {ref_with_digest}"}

    if not await registry_push.verify_signature(ref_with_digest, log_sink=log_sink):
        return {
            **abort,
            "command": f"cosign verify {ref_with_digest}",
            "output": f"cosign signature verification failed for {ref_with_digest}",
        }

    schema_path = Path(repo_path) / "secrets.schema.json"
    if schema_path.is_file():
        try:
            contract = secrets_contract.parse_secrets_contract(schema_path.read_text())
        except secrets_contract.SecretsContractError as exc:
            return {
                **abort,
                "command": "validate secrets contract",
                "output": f"invalid secrets.schema.json: {exc}",
            }
        provider = secret_provider.FileSecretProvider(env_file_candidate)
        missing = secrets_contract.find_missing_secrets(contract, provider)
        if missing:
            names = ", ".join(m.name for m in missing)
            return {
                **abort,
                "command": "validate secrets contract",
                "output": f"missing required secrets: {names}",
            }

    ok, fail_logs, run_record = await run_or_restart_container(
        ref_with_digest,
        container,
        deploy_cfg,
        env_file=env_file,
        data_dir=data_dir,
        repo_path=repo_path,
        log_sink=log_sink,
        health_path=health_path,
        http_timeout=http_timeout,
        http_poll_interval=http_poll_interval,
    )

    if not ok:
        return {
            **abort,
            "command": f"docker run --name {container} {ref_with_digest}",
            "output": fail_logs[-3000:] if fail_logs else "docker run failed",
            "resource": run_record,
        }

    return {
        "deployed": True,
        "command": f"docker run --name {container} {ref_with_digest}",
        "output": "container started",
        "self_update": False,
        "verified": True,
        "resource": run_record,
        "image_ref": release.image_ref,
        "image_digest": release.image_digest,
        "signed": True,
    }
