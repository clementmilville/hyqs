import asyncio
import os
import time

import pytest

from hyqs.pipeline import resources


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.1) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate():
        await asyncio.sleep(interval)


def _extract_pid(lines: list[str]) -> int | None:
    """Find the grandchild pid among captured lines.

    When systemd-run wraps the command (see measure_subprocess), its own
    "Running as unit: ..." banner is interleaved with the script's output, so
    pick the first line that is purely digits.
    """
    for line in lines:
        stripped = line.strip()
        if stripped.isdigit():
            return int(stripped)
    return None


def test_measure_subprocess_kills_grandchild_on_cancellation(tmp_path):
    """Cancelling the enclosing task must kill the child and its grandchild.

    The shell child backgrounds a `sleep` grandchild and echoes its pid so the
    test can observe it directly, proving killpg (not just proc.kill()) is
    what reaches it.
    """

    async def _run():
        captured: list[str] = []

        async def sink(line: str) -> None:
            captured.append(line)

        task = asyncio.ensure_future(
            resources.measure_subprocess(
                ["sh", "-c", "sleep 30 & child=$!; echo $child; wait $child"],
                cwd=str(tmp_path),
                timeout=30,
                log_sink=sink,
            )
        )

        await _wait_until(lambda: _extract_pid(captured) is not None)
        grandchild_pid = _extract_pid(captured)
        assert grandchild_pid is not None, "did not observe the grandchild pid before cancelling"
        assert _pid_alive(grandchild_pid)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await _wait_until(lambda: not _pid_alive(grandchild_pid))
        assert not _pid_alive(grandchild_pid)

    asyncio.run(_run())


def test_kill_process_group_terminates_grandchild(tmp_path):
    """_kill_process_group must reach a grandchild spawned in the same session."""

    async def _run():
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            "sleep 30 & child=$!; echo $child; wait $child",
            cwd=str(tmp_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        assert proc.stdout is not None
        grandchild_line = await proc.stdout.readline()
        grandchild_pid = int(grandchild_line.decode().strip())
        assert _pid_alive(grandchild_pid)

        await resources._kill_process_group(proc)

        await _wait_until(lambda: not _pid_alive(grandchild_pid))
        assert not _pid_alive(proc.pid)
        assert not _pid_alive(grandchild_pid)

    asyncio.run(_run())


def test_measure_subprocess_kills_process_group_on_timeout(tmp_path):
    """The existing TimeoutError path must also kill the whole process group."""

    async def _run():
        captured: list[str] = []

        async def sink(line: str) -> None:
            captured.append(line)

        returncode, output, _record = await resources.measure_subprocess(
            ["sh", "-c", "sleep 30 & child=$!; echo $child; wait $child"],
            cwd=str(tmp_path),
            timeout=0.5,
            log_sink=sink,
        )

        assert returncode == 1
        assert "timed out" in output
        grandchild_pid = _extract_pid(captured)
        assert grandchild_pid is not None, "did not observe the grandchild pid before timeout"

        await _wait_until(lambda: not _pid_alive(grandchild_pid))
        assert not _pid_alive(grandchild_pid)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# minimal_subprocess_env: shared allowlist for job-controlled subprocesses
# ---------------------------------------------------------------------------


def test_minimal_subprocess_env_excludes_non_allowlisted_secrets(monkeypatch):
    fake_environ = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "ANTHROPIC_API_KEY": "sk-xxx",  # pragma: allowlist secret
        "HYQS_DB_URL": "postgres://host/db",
        "POSTGRES_PASSWORD": "hostsecret",  # pragma: allowlist secret
    }
    monkeypatch.setattr(os, "environ", fake_environ, raising=False)

    env = resources.minimal_subprocess_env()

    assert env == {"PATH": "/usr/bin", "HOME": "/home/x"}


def test_minimal_subprocess_env_passes_through_docker_prefixed_keys(monkeypatch):
    fake_environ = {
        "PATH": "/usr/bin",
        "DOCKER_HOST": "unix:///var/run/docker.sock",
        "DOCKER_CONTEXT": "default",
        "POSTGRES_PASSWORD": "hostsecret",  # pragma: allowlist secret
    }
    monkeypatch.setattr(os, "environ", fake_environ, raising=False)

    env = resources.minimal_subprocess_env()

    assert env == {
        "PATH": "/usr/bin",
        "DOCKER_HOST": "unix:///var/run/docker.sock",
        "DOCKER_CONTEXT": "default",
    }


def test_minimal_subprocess_env_extra_overrides_allowlisted_value(monkeypatch):
    fake_environ = {"PATH": "/usr/bin", "NODE_ENV": "development"}
    monkeypatch.setattr(os, "environ", fake_environ, raising=False)

    env = resources.minimal_subprocess_env(extra={"NODE_ENV": "test", "PORT": "9000"})

    assert env == {"PATH": "/usr/bin", "NODE_ENV": "test", "PORT": "9000"}


# --- the env allowlist must pass test-DB credentials, and only those ---------
#
# Job #4308 narrowed the test/lint subprocess environment to an allowlist. It
# omitted every database credential, and because .env is gitignored a pipeline
# worktree has none either — so conftest fell back to its placeholder DSN and
# every DB-backed test failed to authenticate. Job #4331 spent its whole fix
# budget on that before being failed for it.


def test_allowlist_passes_the_test_database_credentials(monkeypatch):
    monkeypatch.setenv("HYQS_TEST_DB_ADMIN_URL", "postgresql://u:p@localhost/admin")
    monkeypatch.setenv("HYQS_TEST_DB_URL", "postgresql://u:p@localhost/explicit")

    env = resources.minimal_subprocess_env()

    assert env["HYQS_TEST_DB_ADMIN_URL"] == "postgresql://u:p@localhost/admin"
    assert env["HYQS_TEST_DB_URL"] == "postgresql://u:p@localhost/explicit"


def test_allowlist_still_withholds_the_control_plane_and_provider_secrets(monkeypatch):
    """The point of the allowlist: these must never reach a job subprocess."""
    monkeypatch.setenv("HYQS_DB_URL", "postgresql://u:p@localhost/control-plane")
    monkeypatch.setenv("POSTGRES_PASSWORD", "super-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("HYQS_WEB_TOKEN", "break-glass-token")
    monkeypatch.setenv("HYQS_CREDENTIAL_ENCRYPTION_KEY", "fernet-key")

    env = resources.minimal_subprocess_env()

    for leaked in (
        "HYQS_DB_URL",
        "POSTGRES_PASSWORD",
        "ANTHROPIC_API_KEY",
        "HYQS_WEB_TOKEN",
        "HYQS_CREDENTIAL_ENCRYPTION_KEY",
    ):
        assert leaked not in env, f"{leaked} must not reach a job subprocess"
