"""Regression test for release.sh not hanging on a slow backgrounded stop.

Job #632 failed because `systemctl --user stop "${unit}" &` (deploy/release.sh)
was backgrounded without redirecting its stdout/stderr. The background process
inherited the fd that hyqs.pipeline.resources.measure_subprocess reads until
EOF, so the deploy stage kept blocking on that pipe until its own timeout even
though release.sh itself had long since finished. This test drives the real
release.sh through the real measure_subprocess with stubbed `npm`/`systemctl`
commands (one of which simulates a slow graceful drain) and asserts the deploy
completes almost immediately instead of waiting for the drain.

This module also covers the web console's own blue-green replacement (job
#3003): release.sh no longer does a bare `systemctl restart hyqs-web` — it
starts a new `hyqs-web@<generation>` alongside the running one, gates on
/api/health answering with the new generation's pid (made possible by
hyqs/web/app.py's SO_REUSEPORT listening socket), then drains the old
instance(s). See the `_web_*` tests below.
"""

from __future__ import annotations

import asyncio
import os
import socket
import stat
import subprocess
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from hyqs.config import Config
from hyqs.pipeline.resources import measure_subprocess
from hyqs.web.app import build_app, serve

_STOP_SLEEP_SECONDS = 6
_MEASURE_TIMEOUT_SECONDS = 3

_NPM_STUB = "#!/usr/bin/env bash\nexit 0\n"

# The list-units stub only fabricates a running unit for the pipeline
# pattern; for the web pattern it prints nothing, so release.sh's web
# blue-green block sees no old unit and skips straight to the (already
# covered) pipeline block below it — these tests are only exercising
# pipeline behavior.
_SYSTEMCTL_STUB = f"""\
#!/usr/bin/env bash
if [[ "$1" == "--user" ]]; then
    shift
fi
cmd="$1"
shift || true
case "$cmd" in
    list-units)
        pattern="$1"
        if [[ "$pattern" == "hyqs-pipeline*" ]]; then
            echo "hyqs-pipeline@111.service loaded active running Hyqs pipeline worker #111"
        fi
        ;;
    show)
        echo "12345"
        ;;
    stop)
        sleep {_STOP_SLEEP_SECONDS}
        ;;
    daemon-reload|start|is-active|restart)
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
"""

_UV_STUB = """\
#!/usr/bin/env bash
if [[ "$1" == "run" && "$2" == "hyqs-pipeline" ]]; then
    exit 0
fi
exit 0
"""


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _build_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    deploy_dir = Path(__file__).resolve().parent.parent / "deploy"
    for name in ("release.sh", "hyqs-pipeline@.service", "hyqs-web@.service"):
        (repo / "deploy" / name).write_text((deploy_dir / name).read_text())
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
    return repo


def test_release_does_not_block_on_slow_backgrounded_stop(tmp_path):
    repo = _build_repo(tmp_path)

    stub_bin = tmp_path / "stub_bin"
    stub_bin.mkdir()
    _write_executable(stub_bin / "npm", _NPM_STUB)
    _write_executable(stub_bin / "systemctl", _SYSTEMCTL_STUB)
    _write_executable(stub_bin / "uv", _UV_STUB)

    home = tmp_path / "home"
    home.mkdir()

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["HYQS_DATA_DIR"] = str(home / ".hyqs" / "data")
    env["PATH"] = f"{stub_bin}:{env['PATH']}"
    env["SKIP_NPM_CI"] = "1"

    returncode, output, _record = asyncio.run(
        measure_subprocess(
            ["bash", "deploy/release.sh"],
            cwd=str(repo),
            timeout=_MEASURE_TIMEOUT_SECONDS,
            env=env,
        )
    )

    assert returncode == 0, output
    assert "timed out" not in output


_HEARTBEAT_GATE_TIMEOUT_SECONDS = 8


def _heartbeat_gate_systemctl_stub(mainpid_count_file: Path) -> str:
    """A systemctl stub whose MainPID answer changes across successive calls.

    Simulates job #642/#643: the pid that actually needs to be checked
    (standing in for the python child forking into the unit's cgroup after
    `systemctl start` returns) is not the one seen immediately after start.
    The FIRST `show -p MainPID` call (the pre-loop "did it start at all"
    guard) returns pid 111; every call after that returns pid 222 — the pid
    the healthcheck stub below actually accepts. `show -p ControlGroup`
    returns empty so the gate falls back to whatever MainPID was just read,
    isolating the test to the MainPID re-read behavior.
    """
    return f"""\
#!/usr/bin/env bash
if [[ "$1" == "--user" ]]; then
    shift
fi
cmd="$1"
shift || true
case "$cmd" in
    list-units)
        pattern="$1"
        if [[ "$pattern" == "hyqs-pipeline*" ]]; then
            echo "hyqs-pipeline@111.service loaded active running Hyqs pipeline worker #111"
        fi
        ;;
    show)
        if [[ "$*" == *"MainPID"* ]]; then
            count="$(cat {mainpid_count_file} 2>/dev/null || echo 0)"
            count=$((count + 1))
            echo "$count" > {mainpid_count_file}
            if [[ "$count" -le 1 ]]; then
                echo "111"
            else
                echo "222"
            fi
        else
            echo ""
        fi
        ;;
    stop)
        exit 0
        ;;
    daemon-reload|start|is-active|restart)
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
"""


_HEARTBEAT_GATE_UV_STUB = """\
#!/usr/bin/env bash
if [[ "$1" == "run" && "$2" == "hyqs-pipeline" && "$3" == "--healthcheck" ]]; then
    if [[ "$5" == "222" ]]; then
        exit 0
    fi
    exit 1
fi
exit 0
"""


def test_release_reresolves_pid_on_every_gate_poll(tmp_path):
    """The health gate must re-read the unit's pid on every poll iteration.

    Job #642 resolved the cgroup/MainPID pid set exactly once, right after
    `systemctl start`, then reused it for all 90 poll iterations — matching
    only whatever process existed at that instant instead of the process
    that eventually writes the heartbeat. This stubs a MainPID that changes
    between the pre-loop start guard and the polling loop, and a healthcheck
    that only succeeds for the later pid: the release can only succeed if
    pid resolution happens inside the poll loop rather than once before it.
    """
    repo = _build_repo(tmp_path)
    mainpid_count_file = tmp_path / "mainpid_count"

    stub_bin = tmp_path / "stub_bin"
    stub_bin.mkdir()
    _write_executable(stub_bin / "npm", _NPM_STUB)
    _write_executable(stub_bin / "systemctl", _heartbeat_gate_systemctl_stub(mainpid_count_file))
    _write_executable(stub_bin / "uv", _HEARTBEAT_GATE_UV_STUB)

    home = tmp_path / "home"
    home.mkdir()

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["HYQS_DATA_DIR"] = str(home / ".hyqs" / "data")
    env["PATH"] = f"{stub_bin}:{env['PATH']}"
    env["SKIP_NPM_CI"] = "1"

    returncode, output, _record = asyncio.run(
        measure_subprocess(
            ["bash", "deploy/release.sh"],
            cwd=str(repo),
            timeout=_HEARTBEAT_GATE_TIMEOUT_SECONDS,
            env=env,
        )
    )

    assert returncode == 0
    assert "ERROR" not in output
    assert "timed out" not in output


# --- Web blue-green replacement (job #3003) ---------------------------------

_WEB_GATE_TIMEOUT_SECONDS = 8


def _web_gate_systemctl_stub(
    call_log: Path,
    *,
    active_state: str = "active",
    main_pid: str = "555",
) -> str:
    """A systemctl stub for the web block only.

    `list-units` fabricates exactly one running unit, `hyqs-web.service`,
    and only for the web pattern — the pipeline pattern gets nothing, so
    release.sh's pipeline block (already covered above) skips immediately
    and these tests stay scoped to the new web block. `start`/`stop`/
    `restart` append `"<cmd> <unit>"` to `call_log` so tests can assert
    ordering.
    """
    return f"""\
#!/usr/bin/env bash
if [[ "$1" == "--user" ]]; then
    shift
fi
cmd="$1"
shift || true
case "$cmd" in
    list-units)
        pattern="$1"
        if [[ "$pattern" == "hyqs-web*" ]]; then
            echo "hyqs-web.service loaded active running Hyqs web console"
        fi
        ;;
    show)
        if [[ "$*" == *"ActiveState"* ]]; then
            echo "{active_state}"
        elif [[ "$*" == *"MainPID"* ]]; then
            echo "{main_pid}"
        elif [[ "$*" == *"ControlGroup"* ]]; then
            echo ""
        fi
        ;;
    start)
        echo "start $1" >> {call_log}
        exit 0
        ;;
    stop)
        echo "stop $1" >> {call_log}
        exit 0
        ;;
    restart)
        echo "restart $1" >> {call_log}
        exit 0
        ;;
    daemon-reload|is-active)
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
"""


def _curl_stub_with_pid(pid: int) -> str:
    return f"""\
#!/usr/bin/env bash
echo '{{"ok": true, "service": "hyqs", "pid": {pid}}}'
"""


def _curl_stub_delayed_match(counter_file: Path, match_after: int, pid: int) -> str:
    """Mismatches on the first `match_after - 1` calls, then matches — so a
    test can prove the gate actually polled multiple times before the old
    unit was drained, not that it happened to pass on the first try."""
    return f"""\
#!/usr/bin/env bash
count="$(cat {counter_file} 2>/dev/null || echo 0)"
count=$((count + 1))
echo "$count" > {counter_file}
if [[ "$count" -ge {match_after} ]]; then
    echo '{{"ok": true, "service": "hyqs", "pid": {pid}}}'
else
    echo '{{"ok": true, "service": "hyqs", "pid": 999}}'
fi
"""


def _web_stub_bin(
    tmp_path: Path,
    systemctl_stub: str,
    curl_stub: str,
    journal_output: str = "",
) -> Path:
    stub_bin = tmp_path / "stub_bin"
    stub_bin.mkdir()
    _write_executable(stub_bin / "npm", _NPM_STUB)
    _write_executable(stub_bin / "uv", _UV_STUB)
    _write_executable(stub_bin / "systemctl", systemctl_stub)
    _write_executable(stub_bin / "curl", curl_stub)
    _write_executable(
        stub_bin / "journalctl",
        f"#!/usr/bin/env bash\nprintf '%s\\n' {journal_output!r}\n",
    )
    return stub_bin


def _run_release(tmp_path: Path, repo: Path, stub_bin: Path, extra_env: dict) -> tuple:
    home = tmp_path / "home"
    home.mkdir()

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["HYQS_DATA_DIR"] = str(home / ".hyqs" / "data")
    env["PATH"] = f"{stub_bin}:{env['PATH']}"
    env["SKIP_NPM_CI"] = "1"
    env.update(extra_env)

    return asyncio.run(
        measure_subprocess(
            ["bash", "deploy/release.sh"],
            cwd=str(repo),
            timeout=_WEB_GATE_TIMEOUT_SECONDS,
            env=env,
        )
    )


def test_web_block_starts_new_generation_before_stopping_old_unit(tmp_path):
    repo = _build_repo(tmp_path)
    call_log = tmp_path / "calls.log"
    stub_bin = _web_stub_bin(
        tmp_path,
        _web_gate_systemctl_stub(call_log),
        _curl_stub_with_pid(555),
    )

    returncode, output, _record = _run_release(
        tmp_path,
        repo,
        stub_bin,
        {"HYQS_WEB_HEALTH_GATE_RETRIES": "5", "HYQS_WEB_HEALTH_GATE_SLEEP": "0"},
    )

    assert returncode == 0, output
    deadline = time.monotonic() + 1
    lines = call_log.read_text().splitlines() if call_log.exists() else []
    while "stop hyqs-web.service" not in lines and time.monotonic() < deadline:
        time.sleep(0.01)
        lines = call_log.read_text().splitlines() if call_log.exists() else []
    start_idx = next(i for i, line in enumerate(lines) if line.startswith("start hyqs-web@"))
    stop_idx = next(i for i, line in enumerate(lines) if line == "stop hyqs-web.service")
    assert start_idx < stop_idx


def test_web_block_stops_old_unit_only_after_health_gate_passes(tmp_path):
    repo = _build_repo(tmp_path)
    call_log = tmp_path / "calls.log"
    counter_file = tmp_path / "curl_calls"
    stub_bin = _web_stub_bin(
        tmp_path,
        _web_gate_systemctl_stub(call_log),
        _curl_stub_delayed_match(counter_file, match_after=3, pid=555),
    )

    returncode, output, _record = _run_release(
        tmp_path,
        repo,
        stub_bin,
        {"HYQS_WEB_HEALTH_GATE_RETRIES": "10", "HYQS_WEB_HEALTH_GATE_SLEEP": "0"},
    )

    assert returncode == 0, output
    # The gate had to poll at least 3 times (mismatched, mismatched, matched)
    # before the old unit was allowed to stop.
    assert int(counter_file.read_text().strip()) >= 3
    lines = call_log.read_text().splitlines() if call_log.exists() else []
    assert "stop hyqs-web.service" in lines


def test_web_health_gate_exhausted_leaves_old_unit_running_and_fails(tmp_path):
    repo = _build_repo(tmp_path)
    call_log = tmp_path / "calls.log"
    stub_bin = _web_stub_bin(
        tmp_path,
        _web_gate_systemctl_stub(call_log),
        _curl_stub_with_pid(999),
    )

    returncode, output, _record = _run_release(
        tmp_path,
        repo,
        stub_bin,
        {"HYQS_WEB_HEALTH_GATE_RETRIES": "2", "HYQS_WEB_HEALTH_GATE_SLEEP": "0"},
    )

    assert returncode != 0
    lines = call_log.read_text().splitlines() if call_log.exists() else []
    assert "stop hyqs-web.service" not in lines
    assert any(line.startswith("stop hyqs-web@") for line in lines)


def test_web_bind_conflict_falls_back_to_bare_restart(tmp_path):
    repo = _build_repo(tmp_path)
    call_log = tmp_path / "calls.log"
    stub_bin = _web_stub_bin(
        tmp_path,
        _web_gate_systemctl_stub(call_log, active_state="failed"),
        _curl_stub_with_pid(999),
        "OSError: [Errno 98] Address already in use",
    )

    returncode, output, _record = _run_release(
        tmp_path,
        repo,
        stub_bin,
        {"HYQS_WEB_HEALTH_GATE_RETRIES": "5", "HYQS_WEB_HEALTH_GATE_SLEEP": "0"},
    )

    assert returncode == 0, output
    assert "falling back to a bare restart" in output
    lines = call_log.read_text().splitlines() if call_log.exists() else []
    assert "restart hyqs-web" in lines
    assert "stop hyqs-web.service" not in lines


def test_web_failed_for_other_reason_leaves_old_unit_running_and_fails(tmp_path):
    repo = _build_repo(tmp_path)
    call_log = tmp_path / "calls.log"
    stub_bin = _web_stub_bin(
        tmp_path,
        _web_gate_systemctl_stub(call_log, active_state="failed"),
        _curl_stub_with_pid(999),
        "RuntimeError: configuration is invalid",
    )

    returncode, output, _record = _run_release(
        tmp_path,
        repo,
        stub_bin,
        {"HYQS_WEB_HEALTH_GATE_RETRIES": "5", "HYQS_WEB_HEALTH_GATE_SLEEP": "0"},
    )

    assert returncode != 0
    assert "failed without address-already-in-use evidence" in output
    lines = call_log.read_text().splitlines() if call_log.exists() else []
    assert any(line.startswith("stop hyqs-web@") for line in lines)
    assert "restart hyqs-web" not in lines
    assert "stop hyqs-web.service" not in lines


_RELEASE_DECISION_SYSTEMCTL_STUB = """\
#!/usr/bin/env bash
if [[ "$1" == "--user" ]]; then shift; fi
cmd="$1"
shift || true
case "$cmd" in
    list-units)
        if [[ "$1" == "hyqs-web*" ]]; then
            echo "hyqs-web.service loaded active running Hyqs web console"
        elif [[ "$1" == "hyqs-pipeline*" ]]; then
            echo "hyqs-pipeline.service loaded active running Hyqs pipeline"
        fi
        ;;
    show)
        if [[ "$*" == *"ActiveState"* ]]; then echo active;
        elif [[ "$*" == *"MainPID"* ]]; then echo 555;
        else echo ""; fi
        ;;
    start|stop|restart)
        echo "$cmd $1" >> "$RELEASE_CALL_LOG"
        ;;
    daemon-reload) ;;
esac
"""

_RELEASE_DECISION_NPM_STUB = """\
#!/usr/bin/env bash
echo "npm $*" >> "$RELEASE_CALL_LOG"
if [[ "${FAIL_NPM:-0}" == "1" ]]; then exit 23; fi
"""


def _commit_path(repo: Path, path: str, content: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    subprocess.run(["git", "add", path], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", f"change {path}"], cwd=repo, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _release_decision_harness(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, str]]:
    repo = _build_repo(tmp_path)
    stub_bin = tmp_path / "release_stub_bin"
    stub_bin.mkdir()
    _write_executable(stub_bin / "npm", _RELEASE_DECISION_NPM_STUB)
    _write_executable(stub_bin / "systemctl", _RELEASE_DECISION_SYSTEMCTL_STUB)
    _write_executable(stub_bin / "uv", _UV_STUB)
    _write_executable(stub_bin / "curl", _curl_stub_with_pid(555))
    call_log = tmp_path / "release_calls.log"
    data_dir = tmp_path / "hyqs_data"
    home = tmp_path / "release_home"
    home.mkdir()
    env = dict(os.environ)
    env.update(
        HOME=str(home),
        HYQS_DATA_DIR=str(data_dir),
        PATH=f"{stub_bin}:{env['PATH']}",
        RELEASE_CALL_LOG=str(call_log),
        HYQS_WEB_HEALTH_GATE_RETRIES="2",
        HYQS_WEB_HEALTH_GATE_SLEEP="0",
    )
    return repo, call_log, data_dir / "release-success-commit", env


def _run_release_sync(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "deploy/release.sh"], cwd=repo, env=env, capture_output=True, text=True
    )


def test_allowlisted_release_skips_frontend_and_web_but_runs_pipeline(tmp_path):
    repo, call_log, marker, env = _release_decision_harness(tmp_path)
    baseline = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    marker.parent.mkdir(parents=True)
    marker.write_text(f"{baseline}\n")
    head = _commit_path(repo, ".hyqs/decisions/999-test.md", "decision\n")

    result = _run_release_sync(repo, env)

    assert result.returncode == 0, result.stderr
    calls = call_log.read_text()
    assert "npm " not in calls
    assert "start hyqs-web@" not in calls
    assert "start hyqs-pipeline@" in calls
    assert "SKIP_WEB_RELEASE:" in result.stdout
    assert marker.read_text().strip() == head


def test_python_release_performs_frontend_and_web_swap(tmp_path):
    repo, call_log, marker, env = _release_decision_harness(tmp_path)
    baseline = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    marker.parent.mkdir(parents=True)
    marker.write_text(f"{baseline}\n")
    _commit_path(repo, "hyqs/change.py", "VALUE = 1\n")

    result = _run_release_sync(repo, env)

    assert result.returncode == 0, result.stderr
    calls = call_log.read_text()
    assert "npm --prefix hyqs/web/frontend ci" in calls
    assert "npm --prefix hyqs/web/frontend run build" in calls
    assert "start hyqs-web@" in calls
    assert "start hyqs-pipeline@" in calls


def test_missing_release_marker_fails_safe_to_full_swap(tmp_path):
    repo, call_log, marker, env = _release_decision_harness(tmp_path)

    result = _run_release_sync(repo, env)

    assert result.returncode == 0, result.stderr
    calls = call_log.read_text()
    assert "npm --prefix hyqs/web/frontend run build" in calls
    assert "start hyqs-web@" in calls
    assert marker.exists()


def test_release_marker_advances_only_after_success(tmp_path):
    repo, _call_log, marker, env = _release_decision_harness(tmp_path)
    baseline = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    marker.parent.mkdir(parents=True)
    marker.write_text(f"{baseline}\n")
    head = _commit_path(repo, "hyqs/change.py", "VALUE = 2\n")

    failed = _run_release_sync(repo, {**env, "FAIL_NPM": "1"})
    assert failed.returncode == 23
    assert marker.read_text().strip() == baseline

    succeeded = _run_release_sync(repo, env)
    assert succeeded.returncode == 0, succeeded.stderr
    assert marker.read_text().strip() == head


def test_standalone_web_run_propagates_immediate_server_failure(tmp_path):
    from hyqs.web import __main__ as web_main

    config = MagicMock()
    config.data_dir = tmp_path
    config.db_url = "postgresql://unused"
    memory = MagicMock()
    jobs = MagicMock()
    orchestrator = MagicMock()
    orchestrator.aclose = AsyncMock()

    async def fail_immediately(*_args):
        raise OSError(98, "Address already in use")

    with (
        patch.object(web_main.Config, "from_env", return_value=config),
        patch.object(web_main, "MemoryStore", return_value=memory),
        patch.object(web_main, "JobStore", return_value=jobs),
        patch.object(web_main, "Orchestrator", return_value=orchestrator),
        patch.object(web_main.web, "serve", new=fail_immediately),
    ):
        with pytest.raises(OSError, match="Address already in use"):
            asyncio.run(asyncio.wait_for(web_main.run(), timeout=1))

    orchestrator.aclose.assert_awaited_once()
    memory.close.assert_called_once()
    jobs.close.assert_called_once()


def test_web_listener_enables_address_and_port_reuse():
    config = Config(
        model="sonnet",
        permission_mode="acceptEdits",
        web_host="127.0.0.1",
        web_port=0,
        web_token="test-web-token",
    )
    observed_options = None

    async def inspect_listener(*, sockets):
        nonlocal observed_options
        listener = sockets[0]
        observed_options = (
            listener.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR),
            listener.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT),
        )
        listener.close()

    server = MagicMock()
    server.serve = AsyncMock(side_effect=inspect_listener)
    with (
        patch("hyqs.web.app.build_app", return_value=MagicMock()),
        patch("hyqs.web.app.uvicorn.Server", return_value=server),
    ):
        asyncio.run(serve(config, MagicMock(), MagicMock()))

    assert observed_options == (1, 1)


def test_web_listener_rebinds_after_closed_connection():
    previous_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    previous_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    previous_listener.bind(("127.0.0.1", 0))
    previous_listener.listen()
    port = previous_listener.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port))
    connection, _address = previous_listener.accept()

    # Close the accepted server endpoint first so its local address enters
    # TIME_WAIT after the peer observes the FIN and closes its side.
    connection.close()
    assert client.recv(1) == b""
    client.close()
    previous_listener.close()

    config = Config(
        model="sonnet",
        permission_mode="acceptEdits",
        web_host="127.0.0.1",
        web_port=port,
        web_token="test-web-token",
    )

    async def close_listener(*, sockets):
        sockets[0].close()

    server = MagicMock()
    server.serve = AsyncMock(side_effect=close_listener)
    with (
        patch("hyqs.web.app.build_app", return_value=MagicMock()),
        patch("hyqs.web.app.uvicorn.Server", return_value=server),
    ):
        asyncio.run(serve(config, MagicMock(), MagicMock()))

    server.serve.assert_awaited_once()


def test_pipeline_units_defer_concurrency_to_env_and_keep_memory_caps():
    """Keep concurrency centralized in .env while preserving containment."""
    deploy_dir = Path(__file__).resolve().parent.parent / "deploy"
    for name in ("hyqs-pipeline.service", "hyqs-pipeline@.service"):
        active_lines = [
            line.strip()
            for line in (deploy_dir / name).read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

        assert not any(
            line.startswith("Environment=HYQS_PIPELINE_CONCURRENCY=") for line in active_lines
        ), f"{name} must defer pipeline concurrency to .env"

        assert "MemoryAccounting=yes" in active_lines, f"{name} dropped MemoryAccounting"
        assert "MemoryMax=14G" in active_lines, f"{name} dropped MemoryMax"


def test_health_endpoint_returns_serving_process_pid():
    store = MagicMock()
    store.list_users.return_value = ["existing-admin"]
    store.get_role_permissions.return_value = {"platform_admin": []}
    config = Config(model="sonnet", permission_mode="acceptEdits", web_token="test-web-token")
    orchestrator = MagicMock()
    app = build_app(config, orchestrator, store)
    client = TestClient(app)

    resp = client.get("/api/health")

    assert resp.status_code == 200
    assert resp.json()["pid"] == os.getpid()
