"""Per-stage hardware resource accounting via cgroup v2 / systemd scopes.

Each subprocess stage is optionally wrapped in a transient ``systemd-run --user
--scope`` unit so its cpu/memory/io usage is isolated in a named cgroup slice.
After the subprocess exits we read the cgroup stats and return them alongside
the output. Degrades gracefully to null stats when systemd-run is unavailable
(CI, macOS, bare Docker containers, etc.).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable


@dataclass(slots=True)
class ResourceRecord:
    """Hardware stats captured for one subprocess invocation."""

    cpu_seconds: float | None
    peak_rss_bytes: int | None
    io_read_bytes: int | None
    io_write_bytes: int | None
    wall_seconds: float
    sampled_at: str
    net_bytes: int | None = None
    net_bytes_approx: bool = False

    def __add__(self, other: "ResourceRecord") -> "ResourceRecord":
        """Combine two records: sum cpu/io/net/wall, take the peak RSS maximum."""

        def _sum(a: float | int | None, b: float | int | None) -> float | int | None:
            if a is None and b is None:
                return None
            return (a or 0) + (b or 0)

        def _max(a: int | None, b: int | None) -> int | None:
            if a is None and b is None:
                return None
            if a is None:
                return b
            if b is None:
                return a
            return max(a, b)

        return ResourceRecord(
            cpu_seconds=_sum(self.cpu_seconds, other.cpu_seconds),
            peak_rss_bytes=_max(self.peak_rss_bytes, other.peak_rss_bytes),
            io_read_bytes=_sum(self.io_read_bytes, other.io_read_bytes),
            io_write_bytes=_sum(self.io_write_bytes, other.io_write_bytes),
            wall_seconds=self.wall_seconds + other.wall_seconds,
            sampled_at=other.sampled_at,
            net_bytes=_sum(self.net_bytes, other.net_bytes),
            net_bytes_approx=self.net_bytes_approx or other.net_bytes_approx,
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Shared minimal-environment allowlist for job-controlled subprocesses
# ---------------------------------------------------------------------------

# asyncio.create_subprocess_exec inherits the FULL parent environment by
# default (measure_subprocess's own `env: dict | None = None` default above
# relies on exactly that for callers who don't opt in here). Every job-
# controlled command (pytest, npm ci/build, ruff, eslint, docker build) is
# untrusted code from the pipeline worker's point of view, so it must never
# see the worker's own secrets (the Anthropic credential, HYQS_DB_URL,
# POSTGRES_PASSWORD, ...). Callers that need a restricted environment build
# one from this allowlist instead of `dict(os.environ)`.
_MINIMAL_ENV_ALLOWLIST_KEYS = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "TZ",
    "CI",
    "NODE_ENV",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "npm_config_cache",
    # Test databases, NOT the control plane. A pipeline job runs its tests in a
    # git worktree, and .env is gitignored — so a worktree checkout has no .env
    # for load_dotenv() to read. Before the env allowlist existed, the test
    # subprocess inherited HYQS_DB_URL from the pipeline process; once it stopped
    # inheriting, tests/conftest.py silently fell back to its hardcoded
    # postgresql://hyqs:hyqs@localhost DSN and every DB-backed test failed with
    # "password authentication failed", which the gate then blamed on the job's
    # own diff. Job #4331 burned its entire fix budget on that.
    #
    # These two are deliberately NOT HYQS_DB_URL: that DSN owns the shared
    # multi-tenant control plane and is exactly what the allowlist exists to keep
    # out of job-controlled subprocesses. A credential scoped to test databases
    # is a far smaller thing to hand over.
    "HYQS_TEST_DB_ADMIN_URL",
    "HYQS_TEST_DB_URL",
)
_MINIMAL_ENV_ALLOWLIST_PREFIXES = ("DOCKER_",)


def minimal_subprocess_env(*, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a subprocess env from only allowlisted host keys, plus ``extra``.

    Mirrors the allowlist pattern already used for compose deploys
    (``docker_deploy._minimal_compose_env``), generalized for every job-
    controlled subprocess (TEST/LINT/build). ``extra`` is applied last and
    overrides any allowlisted value on key collision.
    """
    env = {key: os.environ[key] for key in _MINIMAL_ENV_ALLOWLIST_KEYS if key in os.environ}
    for key, value in os.environ.items():
        if key.startswith(_MINIMAL_ENV_ALLOWLIST_PREFIXES):
            env[key] = value
    if extra:
        env.update(extra)
    return env


# ---------------------------------------------------------------------------
# cgroup v2 stat parsers (extracted for unit-testability)
# ---------------------------------------------------------------------------


def _parse_cpu_stat(text: str) -> float | None:
    """Parse ``usage_usec`` from ``cpu.stat`` text; returns seconds."""
    for line in text.splitlines():
        if line.startswith("usage_usec "):
            try:
                return int(line.split()[1]) / 1_000_000
            except (IndexError, ValueError):
                return None
    return None


def _parse_memory_peak(text: str) -> int | None:
    """Parse byte count from ``memory.peak`` text."""
    try:
        return int(text.strip())
    except ValueError:
        return None


def _parse_io_stat(text: str) -> tuple[int, int]:
    """Sum ``rbytes`` and ``wbytes`` across all devices in ``io.stat`` text."""
    rbytes = 0
    wbytes = 0
    for line in text.splitlines():
        for part in line.split():
            if part.startswith("rbytes="):
                try:
                    rbytes += int(part[7:])
                except ValueError:
                    pass
            elif part.startswith("wbytes="):
                try:
                    wbytes += int(part[7:])
                except ValueError:
                    pass
    return rbytes, wbytes


def _read_proc_net_bytes() -> int | None:
    """Sum RX+TX bytes across all non-loopback interfaces from /proc/net/dev."""
    try:
        text = Path("/proc/net/dev").read_text()
    except OSError:
        return None
    total = 0
    lines = text.splitlines()
    for line in lines[2:]:  # first two lines are headers
        parts = line.split()
        if len(parts) < 10:
            continue
        iface = parts[0].rstrip(":")
        if iface == "lo":
            continue
        try:
            total += int(parts[1]) + int(parts[9])
        except (IndexError, ValueError):
            continue
    return total


def _read_cgroup_stats(
    scope_name: str,
) -> tuple[float | None, int | None, int | None, int | None]:
    """Return (cpu_s, peak_rss_bytes, io_read_bytes, io_write_bytes) for the scope.

    Walks ``/sys/fs/cgroup`` for a directory whose name matches *scope_name*,
    then reads ``cpu.stat``, ``memory.peak``, and ``io.stat``. Returns all-None
    on any error (missing filesystem, missing scope directory, parse failure).
    """
    try:
        cgroup_root = Path("/sys/fs/cgroup")
        if not cgroup_root.exists():
            return None, None, None, None
        cgroup: Path | None = None
        for p in cgroup_root.rglob(scope_name):
            if p.is_dir():
                cgroup = p
                break
        if cgroup is None:
            return None, None, None, None

        cpu_seconds: float | None = None
        cpu_stat = cgroup / "cpu.stat"
        if cpu_stat.exists():
            cpu_seconds = _parse_cpu_stat(cpu_stat.read_text())

        peak_rss: int | None = None
        mem_peak = cgroup / "memory.peak"
        if mem_peak.exists():
            peak_rss = _parse_memory_peak(mem_peak.read_text())

        io_read: int | None = None
        io_write: int | None = None
        io_stat = cgroup / "io.stat"
        if io_stat.exists():
            io_read, io_write = _parse_io_stat(io_stat.read_text())

        return cpu_seconds, peak_rss, io_read, io_write
    except (OSError, FileNotFoundError, ValueError):
        return None, None, None, None


async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Kill *proc*'s whole process group so grandchildren cannot outlive it.

    Falls back to killing just the direct child if the group is already gone
    (e.g. the process was already reaped). Cleanup itself is bounded so it can
    never hang the caller.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        pass


async def measure_subprocess(
    cmd: list[str],
    cwd: str,
    timeout: float,
    log_sink: Callable[[str], Awaitable[None]] | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str, ResourceRecord]:
    """Run *cmd* in a transient systemd scope (if available) and measure resources.

    Returns ``(returncode, combined_output, ResourceRecord)``.  Handles timeouts
    internally (kills the process, sets returncode=1).  Cgroup fields in the
    record are ``None`` when systemd-run is unavailable or the scope is not found.
    """
    scope_name = f"hyqs-{uuid.uuid4().hex[:8]}.scope"
    actual_cmd = list(cmd)
    if shutil.which("systemd-run") is not None:
        actual_cmd = [
            "systemd-run",
            "--user",
            "--scope",
            f"--unit={scope_name}",
        ] + actual_cmd

    buf: list[str] = []
    t0 = time.monotonic()
    net_before = _read_proc_net_bytes()

    proc = await asyncio.create_subprocess_exec(
        *actual_cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
        **({"env": env} if env is not None else {}),
    )

    async def _drain() -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip("\n")
            buf.append(line)
            if log_sink is not None:
                await log_sink(line)
        await proc.wait()

    timed_out = False
    try:
        await asyncio.wait_for(_drain(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        await _kill_process_group(proc)
    except asyncio.CancelledError:
        await _kill_process_group(proc)
        raise

    wall_seconds = time.monotonic() - t0
    net_after = _read_proc_net_bytes()
    output = "\n".join(buf)
    if timed_out:
        msg = f"[timed out after {timeout}s]"
        output = f"{output}\n{msg}" if output else msg
        returncode = 1
    else:
        returncode = proc.returncode if proc.returncode is not None else 1

    net_bytes: int | None = None
    net_bytes_approx = False
    if net_before is not None and net_after is not None and net_after >= net_before:
        net_bytes = net_after - net_before
        net_bytes_approx = True

    cpu_seconds, peak_rss, io_read, io_write = _read_cgroup_stats(scope_name)
    record = ResourceRecord(
        cpu_seconds=cpu_seconds,
        peak_rss_bytes=peak_rss,
        io_read_bytes=io_read,
        io_write_bytes=io_write,
        wall_seconds=wall_seconds,
        sampled_at=_now_iso(),
        net_bytes=net_bytes,
        net_bytes_approx=net_bytes_approx,
    )
    return returncode, output, record
