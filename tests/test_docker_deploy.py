"""Unit tests for the health-gated Docker deploy cutover.

These tests mock asyncio.create_subprocess_exec (and urllib for HTTP probing)
so no real Docker daemon is required.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

from hyqs.pipeline import docker_deploy, resources
from hyqs.pipeline.docker_deploy import run_or_restart_container

_CONTAINER = "hyqs-testapp"
_PROBE = "hyqs-testapp-probe"
_PORT = 9000
_DEPLOY_CFG = {"port": _PORT}


def _make_proc(stdout_data: bytes = b"", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout_data, b""))
    return proc


def _make_urlopen_mock(status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    resp.status = status
    return resp


def test_healthy_image_promotes_and_removes_old():
    """Healthy probe → old canonical removed, new canonical on 127.0.0.1:PORT, ok=True."""

    recorded_calls: list[tuple] = []

    async def fake_exec(*args, **kwargs):
        recorded_calls.append(args)
        cmd = list(args)
        if "inspect" in cmd and "--format" in cmd:
            fmt = cmd[cmd.index("--format") + 1]
            if "State.Status" in fmt:
                return _make_proc(b"running\n")
            if "NetworkSettings.IPAddress" in fmt:
                return _make_proc(b"172.17.0.2\n")
        return _make_proc(b"abc123\n")

    def _run():
        with (
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch("urllib.request.urlopen", return_value=_make_urlopen_mock(200)),
        ):
            return asyncio.run(
                run_or_restart_container("myimage", _CONTAINER, _DEPLOY_CFG, data_dir=None)
            )

    ok, fail_logs, record = _run()

    assert ok is True
    assert fail_logs == ""
    assert record is not None

    # Probe should have been started (docker run with probe name)
    probe_runs = [c for c in recorded_calls if c[0] == "docker" and c[1] == "run" and _PROBE in c]
    assert probe_runs, "probe docker run not found"

    # Canonical docker run must have -p and --restart unless-stopped
    canonical_runs = [
        c
        for c in recorded_calls
        if c[0] == "docker" and c[1] == "run" and _CONTAINER in c and _PROBE not in c
    ]
    assert canonical_runs, "canonical docker run not found"
    canonical_cmd = canonical_runs[0]
    assert "-p" in canonical_cmd
    assert f"127.0.0.1:{_PORT}:8080" in canonical_cmd
    assert "--restart" in canonical_cmd
    assert "unless-stopped" in canonical_cmd

    # Both probe and canonical should have been stopped/removed
    stop_rm_targets = {c[-1] for c in recorded_calls if c[0] == "docker" and c[1] in ("stop", "rm")}
    assert _PROBE in stop_rm_targets
    assert _CONTAINER in stop_rm_targets


def test_unhealthy_image_leaves_old_running_returns_false():
    """Unhealthy probe (exited) → probe removed, canonical untouched, ok=False with logs."""

    recorded_calls: list[tuple] = []

    async def fake_exec(*args, **kwargs):
        recorded_calls.append(args)
        cmd = list(args)
        if "inspect" in cmd and "--format" in cmd:
            fmt = cmd[cmd.index("--format") + 1]
            if "State.Status" in fmt:
                return _make_proc(b"exited\n")
        if "logs" in cmd:
            return _make_proc(b"startup migration failed: UNIQUE constraint\n")
        return _make_proc(b"abc123\n")

    def _run():
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            return asyncio.run(
                run_or_restart_container("myimage", _CONTAINER, _DEPLOY_CFG, data_dir=None)
            )

    ok, fail_logs, record = _run()

    assert ok is False
    assert fail_logs  # non-empty: contains startup logs

    # Probe should have been removed (stop + rm)
    stop_rm_targets = {c[-1] for c in recorded_calls if c[0] == "docker" and c[1] in ("stop", "rm")}
    assert _PROBE in stop_rm_targets

    # Canonical must NOT have been stopped, removed, or re-run
    assert _CONTAINER not in stop_rm_targets

    canonical_runs = [
        c
        for c in recorded_calls
        if c[0] == "docker" and c[1] == "run" and _CONTAINER in c and _PROBE not in c
    ]
    assert not canonical_runs, "canonical docker run must not be called on unhealthy probe"


def _fake_exec_healthy(recorded_calls: list[tuple]):
    async def fake_exec(*args, **kwargs):
        recorded_calls.append(args)
        cmd = list(args)
        if "inspect" in cmd and "--format" in cmd:
            fmt = cmd[cmd.index("--format") + 1]
            if "State.Status" in fmt:
                return _make_proc(b"running\n")
            if "NetworkSettings.IPAddress" in fmt:
                return _make_proc(b"172.17.0.2\n")
        return _make_proc(b"abc123\n")

    return fake_exec


def test_probe_mounts_throwaway_data_snapshot_not_real_data_dir(tmp_path):
    """Probe gets a writable COPY of data_dir at /data; the real data_dir is untouched
    and no leftover snapshot directory remains once the call returns."""

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    original_file = data_dir / "app.db"
    original_file.write_bytes(b"original-content")

    recorded_calls: list[tuple] = []

    def _run():
        with (
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec_healthy(recorded_calls)),
            patch("urllib.request.urlopen", return_value=_make_urlopen_mock(200)),
        ):
            return asyncio.run(
                run_or_restart_container("myimage", _CONTAINER, _DEPLOY_CFG, data_dir=data_dir)
            )

    ok, fail_logs, record = _run()
    assert ok is True

    probe_runs = [c for c in recorded_calls if c[0] == "docker" and c[1] == "run" and _PROBE in c]
    assert probe_runs, "probe docker run not found"
    probe_cmd = list(probe_runs[0])

    mount_args = [a for a in probe_cmd if a.endswith(":/data")]
    assert mount_args, "probe command missing -v ...:/data mount"
    mounted_path = mount_args[0].removesuffix(":/data")
    assert mounted_path != str(data_dir), "probe must mount a throwaway copy, not the real data_dir"

    # Real data_dir is untouched.
    assert original_file.read_bytes() == b"original-content"

    # No leftover throwaway snapshot directory.
    leftovers = list(tmp_path.glob(".data-probe-*"))
    assert leftovers == [], f"leftover probe data snapshot dirs: {leftovers}"


def test_chmod_world_writable_for_container_sets_permissive_mode(tmp_path):
    path = tmp_path / "somedir"
    path.mkdir()

    docker_deploy._chmod_world_writable_for_container(path)

    assert oct(path.stat().st_mode)[-3:] == "777"


def test_probe_skips_data_mount_when_data_dir_exceeds_size_guard(tmp_path):
    """When data_dir exceeds probe_data_max_bytes, the probe boots with no /data mount."""

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "app.db").write_bytes(b"more-than-one-byte")

    deploy_cfg = {"port": _PORT, "probe_data_max_bytes": 1}
    recorded_calls: list[tuple] = []

    def _run():
        with (
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec_healthy(recorded_calls)),
            patch("urllib.request.urlopen", return_value=_make_urlopen_mock(200)),
        ):
            return asyncio.run(
                run_or_restart_container("myimage", _CONTAINER, deploy_cfg, data_dir=data_dir)
            )

    ok, fail_logs, record = _run()
    assert ok is True

    probe_runs = [c for c in recorded_calls if c[0] == "docker" and c[1] == "run" and _PROBE in c]
    assert probe_runs, "probe docker run not found"
    probe_cmd = list(probe_runs[0])

    assert not any(a.endswith(":/data") for a in probe_cmd), (
        "probe command must have no /data mount when data_dir exceeds the size guard"
    )


def test_probe_ip_lookup_failure_surfaces_crash_logs_not_generic_message():
    """When the probe reports 'running' but its IP can't be read (crashed in between),
    fail_logs must contain the real crash diagnostics, not just the generic string."""

    recorded_calls: list[tuple] = []

    async def fake_exec(*args, **kwargs):
        recorded_calls.append(args)
        cmd = list(args)
        if "inspect" in cmd and "--format" in cmd:
            fmt = cmd[cmd.index("--format") + 1]
            if "exit_code=" in fmt:
                return _make_proc(b"exited exit_code=1 error=\n")
            if "NetworkSettings.IPAddress" in fmt:
                return _make_proc(b"\n")
            if "State.Status" in fmt:
                return _make_proc(b"running\n")
        if "logs" in cmd:
            return _make_proc(b"unable to open database file\n")
        return _make_proc(b"abc123\n")

    def _run():
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            return asyncio.run(
                run_or_restart_container("myimage", _CONTAINER, _DEPLOY_CFG, data_dir=None)
            )

    ok, fail_logs, record = _run()

    assert ok is False
    assert fail_logs != "could not determine probe container IP"
    assert "unable to open database file" in fail_logs


# ---------------------------------------------------------------------------
# S1: compose env must be locked to an allowlist — host secrets never leak in
# ---------------------------------------------------------------------------


def test_minimal_compose_env_allows_only_allowlisted_keys(monkeypatch):
    fake_environ = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "DOCKER_CONTEXT": "default",
        "POSTGRES_PASSWORD": "hostsecret",  # pragma: allowlist secret
        "ANTHROPIC_API_KEY": "sk-xxx",  # pragma: allowlist secret
        "DATABASE_URL": "postgres://host/db",
    }
    monkeypatch.setattr(os, "environ", fake_environ, raising=False)

    env = docker_deploy._minimal_compose_env(9000, "hyqs-testapp")

    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "DOCKER_CONTEXT": "default",
        "PORT": "9000",
        "CONTAINER_NAME": "hyqs-testapp",
    }


def test_build_image_scopes_docker_build_env_to_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-xxx")  # pragma: allowlist secret
    monkeypatch.setenv("POSTGRES_PASSWORD", "hostsecret")  # pragma: allowlist secret

    captured_env: dict[str, str] = {}

    async def fake_measure_subprocess(cmd, cwd, timeout, log_sink=None, env=None):
        captured_env.update(env or {})
        record = resources.ResourceRecord(
            cpu_seconds=None,
            peak_rss_bytes=None,
            io_read_bytes=None,
            io_write_bytes=None,
            wall_seconds=0.1,
            sampled_at="now",
        )
        return 0, "build ok", record

    with patch(
        "hyqs.pipeline.docker_deploy.resources.measure_subprocess",
        side_effect=fake_measure_subprocess,
    ):
        ok, record = asyncio.run(docker_deploy.build_image(tmp_path, "hyqs-testapp"))

    assert ok is True
    assert record is not None
    assert captured_env.get("PATH") == "/usr/bin"
    assert "ANTHROPIC_API_KEY" not in captured_env
    assert "POSTGRES_PASSWORD" not in captured_env


def test_compose_deploy_does_not_leak_host_secrets_into_env(tmp_path, monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "hostsecret")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "docker-compose.yml").write_text("services: {}\n")

    captured_env: dict[str, str] = {}

    async def fake_measure_subprocess(cmd, cwd, timeout, log_sink=None, env=None):
        captured_env.update(env or {})
        record = resources.ResourceRecord(
            cpu_seconds=None,
            peak_rss_bytes=None,
            io_read_bytes=None,
            io_write_bytes=None,
            wall_seconds=0.1,
            sampled_at="now",
        )
        return 0, "compose up ok", record

    async def fake_verify_healthy(compose_file, env, cwd, **kwargs):
        return True, ""

    async def fake_probe_http(port, path, timeout, poll_interval, host="127.0.0.1"):
        return True, ""

    with (
        patch(
            "hyqs.pipeline.docker_deploy.resources.measure_subprocess",
            side_effect=fake_measure_subprocess,
        ),
        patch(
            "hyqs.pipeline.docker_deploy._verify_compose_services_healthy",
            side_effect=fake_verify_healthy,
        ),
        patch("hyqs.pipeline.docker_deploy._probe_http", side_effect=fake_probe_http),
    ):
        result = asyncio.run(docker_deploy.compose_deploy(repo, "{}"))

    assert result["deployed"] is True
    assert captured_env.get("PATH") == "/usr/bin"
    assert captured_env.get("HOME") == str(tmp_path)
    assert captured_env.get("DOCKER_HOST") == "unix:///var/run/docker.sock"
    assert "PORT" in captured_env
    assert "CONTAINER_NAME" in captured_env
    assert "POSTGRES_PASSWORD" not in captured_env


# ---------------------------------------------------------------------------
# S2: compose verify must not depend on a hyqs-<slug> container name, and
# must fail closed when a backend service is unhealthy behind a healthy proxy
# ---------------------------------------------------------------------------

_MULTI_SERVICE_PS_HEALTHY = json.dumps(
    [
        {"Service": "postgres", "State": "running"},
        {"Service": "redis", "State": "running"},
        {"Service": "backend", "State": "running", "Health": "healthy"},
        {"Service": "frontend", "State": "running"},
        {"Service": "celery", "State": "running"},
        {"Service": "proxy", "State": "running"},
    ]
)

_MULTI_SERVICE_PS_BACKEND_DOWN = json.dumps(
    [
        {"Service": "postgres", "State": "running"},
        {"Service": "redis", "State": "running"},
        {"Service": "backend", "State": "exited"},
        {"Service": "frontend", "State": "running"},
        {"Service": "celery", "State": "running"},
        {"Service": "proxy", "State": "running"},
    ]
)


def _fake_compose_ps_logs_exec(ps_output: bytes, log_output: bytes = b""):
    """Fake asyncio.create_subprocess_exec covering `docker compose ps`/`logs` only
    (the `up` step is driven through resources.measure_subprocess in these tests,
    which consumes stdout via async iteration rather than communicate())."""

    async def fake_exec(*args, **kwargs):
        cmd = list(args)
        if "ps" in cmd:
            return _make_proc(ps_output, 0)
        if "logs" in cmd:
            return _make_proc(log_output, 0)
        return _make_proc(b"", 0)

    return fake_exec


async def _fake_measure_subprocess_up_ok(cmd, cwd, timeout, log_sink=None, env=None):
    record = resources.ResourceRecord(
        cpu_seconds=None,
        peak_rss_bytes=None,
        io_read_bytes=None,
        io_write_bytes=None,
        wall_seconds=0.1,
        sampled_at="now",
    )
    return 0, "compose up ok", record


def test_compose_deploy_verified_without_hyqs_slug_container(tmp_path):
    """All compose services running/healthy, no container named hyqs-<slug> exists,
    both health_paths respond 200 -> deployed/verified True (proves BUG1 fixed)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "docker-compose.yml").write_text("services: {}\n")

    urls_hit: list[str] = []

    def fake_urlopen(url, timeout=5):
        urls_hit.append(url)
        return _make_urlopen_mock(200)

    with (
        patch(
            "hyqs.pipeline.docker_deploy.resources.measure_subprocess",
            side_effect=_fake_measure_subprocess_up_ok,
        ),
        patch(
            "asyncio.create_subprocess_exec",
            side_effect=_fake_compose_ps_logs_exec(_MULTI_SERVICE_PS_HEALTHY.encode()),
        ),
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
    ):
        result = asyncio.run(
            docker_deploy.compose_deploy(
                repo,
                json.dumps({"port": 9000, "health_paths": ["/", "/api/health"]}),
            )
        )

    assert result["deployed"] is True
    assert result["verified"] is True
    assert any(u.endswith(":9000/") for u in urls_hit)
    assert any(u.endswith(":9000/api/health") for u in urls_hit)


def test_compose_deploy_fails_when_backend_unhealthy_though_proxy_ok(tmp_path):
    """Backend service exited while proxy '/' still returns 200 (frontend static) ->
    deployed/verified False with backend failure detail (proves BUG3 fixed)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "docker-compose.yml").write_text("services: {}\n")

    with (
        patch(
            "hyqs.pipeline.docker_deploy.resources.measure_subprocess",
            side_effect=_fake_measure_subprocess_up_ok,
        ),
        patch(
            "asyncio.create_subprocess_exec",
            side_effect=_fake_compose_ps_logs_exec(
                _MULTI_SERVICE_PS_BACKEND_DOWN.encode(),
                log_output=b"asyncpg.InvalidPasswordError: password authentication failed\n",
            ),
        ),
        patch("urllib.request.urlopen", return_value=_make_urlopen_mock(200)),
    ):
        result = asyncio.run(
            docker_deploy.compose_deploy(
                repo,
                json.dumps(
                    {
                        "port": 9000,
                        "health_paths": ["/"],
                        "compose_verify_max_wait": 0.05,
                        "compose_verify_poll_interval": 0.01,
                    }
                ),
            )
        )

    assert result["deployed"] is False
    assert result["verified"] is False
    assert "backend" in result["output"]
    assert "password authentication failed" in result["output"]


def test_compose_deploy_keeps_final_traceback_line_of_long_failure_log(tmp_path):
    """A crashed service's log is a long traceback whose diagnostic final line
    (e.g. an alembic multi-head CommandError) sits well past a 2000-char
    front-slice. compose_deploy must tail-truncate so that line survives into
    'output', or classifiers like supervisor's alembic-multi-head detector can
    never see it (regression for the docker_deploy job-2254 truncation bug)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "docker-compose.yml").write_text("services: {}\n")

    padding = "\n".join(f"traceback frame line {i}" for i in range(200))
    long_log = (
        f"{padding}\nalembic.util.exc.CommandError: Multiple head revisions "
        "are present for given argument 'head'"
    ).encode()
    assert len(long_log) > 2000

    with (
        patch(
            "hyqs.pipeline.docker_deploy.resources.measure_subprocess",
            side_effect=_fake_measure_subprocess_up_ok,
        ),
        patch(
            "asyncio.create_subprocess_exec",
            side_effect=_fake_compose_ps_logs_exec(
                _MULTI_SERVICE_PS_BACKEND_DOWN.encode(),
                log_output=long_log,
            ),
        ),
        patch("urllib.request.urlopen", return_value=_make_urlopen_mock(200)),
    ):
        result = asyncio.run(
            docker_deploy.compose_deploy(
                repo,
                json.dumps(
                    {
                        "port": 9000,
                        "health_paths": ["/"],
                        "compose_verify_max_wait": 0.05,
                        "compose_verify_poll_interval": 0.01,
                    }
                ),
            )
        )

    assert result["deployed"] is False
    assert "Multiple head revisions are present" in result["output"]


def test_verify_compose_services_healthy_all_healthy_returns_true(tmp_path):
    async def fake_compose_ps(compose_file, env, cwd):
        return [
            {"Service": "backend", "State": "running", "Health": "healthy"},
            {"Service": "proxy", "State": "running"},
        ]

    with patch("hyqs.pipeline.docker_deploy._compose_ps", side_effect=fake_compose_ps):
        ok, detail = asyncio.run(
            docker_deploy._verify_compose_services_healthy(
                tmp_path / "docker-compose.yml", {}, tmp_path, max_wait=0.05, poll_interval=0.01
            )
        )

    assert ok is True
    assert detail == ""


def test_verify_compose_services_healthy_restarting_service_times_out_false(tmp_path):
    async def fake_compose_ps(compose_file, env, cwd):
        return [{"Service": "backend", "State": "restarting"}]

    async def fake_logs(compose_file, env, cwd, service, tail=50):
        return "crash loop"

    with (
        patch("hyqs.pipeline.docker_deploy._compose_ps", side_effect=fake_compose_ps),
        patch("hyqs.pipeline.docker_deploy._compose_service_logs", side_effect=fake_logs),
    ):
        ok, detail = asyncio.run(
            docker_deploy._verify_compose_services_healthy(
                tmp_path / "docker-compose.yml", {}, tmp_path, max_wait=0.05, poll_interval=0.01
            )
        )

    assert ok is False
    assert "backend" in detail
    assert detail  # non-empty


# ---------------------------------------------------------------------------
# Runtime status: published-port parsing + compose-status normalization
# (Epic 49 job 1: GET /api/projects/{id}/runtime backend primitives)
# ---------------------------------------------------------------------------


def test_parse_container_ports_normalizes_docker_inspect_map():
    raw_ports = {
        "8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8364"}],
        "9090/tcp": None,
    }

    parsed = docker_deploy._parse_container_ports(raw_ports)

    assert parsed == [
        {
            "host_ip": "127.0.0.1",
            "host_port": "8364",
            "container_port": "8080",
            "protocol": "tcp",
        }
    ]


def test_parse_container_ports_handles_none_and_empty():
    assert docker_deploy._parse_container_ports(None) == []
    assert docker_deploy._parse_container_ports({}) == []
    assert docker_deploy._parse_container_ports({"8080/tcp": []}) == []


def test_parse_compose_publishers_normalizes_publishers_field():
    publishers = [
        {"URL": "0.0.0.0", "TargetPort": 8080, "PublishedPort": 8080, "Protocol": "tcp"},
    ]

    parsed = docker_deploy._parse_compose_publishers(publishers)

    assert parsed == [
        {
            "host_ip": "0.0.0.0",
            "host_port": "8080",
            "container_port": "8080",
            "protocol": "tcp",
        }
    ]


def test_parse_compose_publishers_handles_none_and_unpublished():
    assert docker_deploy._parse_compose_publishers(None) == []
    assert docker_deploy._parse_compose_publishers([{"TargetPort": 8080}]) == []


def test_get_container_status_includes_published_ports_and_health(monkeypatch):
    """A running single container started with `-p 127.0.0.1:8364:8080` reports
    a non-empty published_ports list with the correct host/container ports."""
    raw = {
        "State": {"Status": "running", "Health": {"Status": "healthy"}},
        "Image": "sha256:abc123",
        "NetworkSettings": {"Ports": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8364"}]}},
    }

    async def fake_exec(*args, **kwargs):
        return _make_proc(json.dumps(raw).encode())

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        status = asyncio.run(docker_deploy.get_container_status("/tmp/some-repo"))

    assert status["state"] == "running"
    assert status["health"] == "healthy"
    assert status["published_ports"] == [
        {
            "host_ip": "127.0.0.1",
            "host_port": "8364",
            "container_port": "8080",
            "protocol": "tcp",
        }
    ]


def test_get_container_status_never_raises_on_docker_failure():
    async def fake_exec(*args, **kwargs):
        raise OSError("docker not found")

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        status = asyncio.run(docker_deploy.get_container_status("/tmp/some-repo"))

    assert status["state"] == "not_found"


def test_get_compose_status_normalizes_one_entry_per_service(tmp_path):
    ps_output = json.dumps(
        [
            {
                "Service": "proxy",
                "State": "running",
                "CreatedAt": "2026-07-18T00:00:00Z",
                "Image": "myapp-proxy:latest",
                "Publishers": [
                    {"URL": "0.0.0.0", "TargetPort": 80, "PublishedPort": 8080, "Protocol": "tcp"}
                ],
            },
            {
                "Service": "backend",
                "State": "running",
                "Health": "healthy",
                "Image": "myapp-backend:latest",
            },
        ]
    ).encode()

    async def fake_exec(*args, **kwargs):
        return _make_proc(ps_output)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        statuses = asyncio.run(
            docker_deploy.get_compose_status(tmp_path, json.dumps({"port": 8364}))
        )

    assert len(statuses) == 2
    proxy = next(s for s in statuses if s["name"] == "proxy")
    assert proxy["state"] == "running"
    assert proxy["published_ports"] == [
        {"host_ip": "0.0.0.0", "host_port": "8080", "container_port": "80", "protocol": "tcp"}
    ]
    backend = next(s for s in statuses if s["name"] == "backend")
    assert backend["health"] == "healthy"
    assert backend["published_ports"] == []


def test_get_compose_status_never_raises_returns_empty_on_failure(tmp_path):
    async def fake_exec(*args, **kwargs):
        raise OSError("docker not found")

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        statuses = asyncio.run(docker_deploy.get_compose_status(tmp_path, "{}"))

    assert statuses == []


def test_verify_compose_services_healthy_inconclusive_when_ps_unsupported(tmp_path):
    """When _compose_ps returns [] for the whole poll window, the gate must not
    block the deploy — the HTTP probe remains the hard gate in that case."""

    async def fake_compose_ps(compose_file, env, cwd):
        return []

    with patch("hyqs.pipeline.docker_deploy._compose_ps", side_effect=fake_compose_ps):
        ok, detail = asyncio.run(
            docker_deploy._verify_compose_services_healthy(
                tmp_path / "docker-compose.yml", {}, tmp_path, max_wait=0.05, poll_interval=0.01
            )
        )

    assert ok is True
    assert detail == ""


# ---------------------------------------------------------------------------
# Lifecycle primitives: compose_down / compose_up_existing / start|stop_single_container
# (Epic 49 job 4: Start/Stop/Restart controls)
# ---------------------------------------------------------------------------


def test_compose_up_existing_injects_port_and_container_name_env(tmp_path):
    """Regression guard for the 8080-vs-configured-port drift: compose_up_existing
    must pass PORT=<deploy_config.port> and CONTAINER_NAME=<container> to `docker
    compose up -d`, exactly like compose_deploy does."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "docker-compose.yml").write_text("services: {}\n")

    captured_env: dict[str, str] = {}
    captured_cmd: list[str] = []

    async def fake_measure_subprocess(cmd, cwd, timeout, log_sink=None, env=None):
        captured_cmd.extend(cmd)
        captured_env.update(env or {})
        record = resources.ResourceRecord(
            cpu_seconds=None,
            peak_rss_bytes=None,
            io_read_bytes=None,
            io_write_bytes=None,
            wall_seconds=0.1,
            sampled_at="now",
        )
        return 0, "compose up ok", record

    with patch(
        "hyqs.pipeline.docker_deploy.resources.measure_subprocess",
        side_effect=fake_measure_subprocess,
    ):
        result = asyncio.run(docker_deploy.compose_up_existing(repo, json.dumps({"port": 9364})))

    assert result["started"] is True
    assert captured_env["PORT"] == "9364"
    assert captured_env["CONTAINER_NAME"] == "hyqs-repo"
    assert "--build" not in captured_cmd
    assert "up" in captured_cmd and "-d" in captured_cmd


def test_compose_up_existing_returns_false_on_subprocess_failure_without_raising(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "docker-compose.yml").write_text("services: {}\n")

    async def fake_measure_subprocess(cmd, cwd, timeout, log_sink=None, env=None):
        raise OSError("docker not found")

    with patch(
        "hyqs.pipeline.docker_deploy.resources.measure_subprocess",
        side_effect=fake_measure_subprocess,
    ):
        result = asyncio.run(docker_deploy.compose_up_existing(repo, "{}"))

    assert result == {"started": False, "output": "docker not found"}


def test_compose_down_returns_true_on_success():
    async def fake_exec(*args, **kwargs):
        return _make_proc(b"Removing container...\n", 0)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = asyncio.run(
            docker_deploy.compose_down("/tmp/some-repo", json.dumps({"port": 9000}))
        )

    assert result["stopped"] is True


def test_compose_down_returns_false_on_subprocess_failure_without_raising():
    async def fake_exec(*args, **kwargs):
        raise OSError("docker not found")

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = asyncio.run(docker_deploy.compose_down("/tmp/some-repo"))

    assert result == {"stopped": False, "output": "docker not found"}


def test_stop_single_container_calls_docker_stop_with_container_name():
    recorded_calls: list[tuple] = []

    async def fake_exec(*args, **kwargs):
        recorded_calls.append(args)
        return _make_proc(b"hyqs-some-repo\n", 0)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = asyncio.run(docker_deploy.stop_single_container("/tmp/some-repo"))

    assert result["stopped"] is True
    assert recorded_calls == [("docker", "stop", "hyqs-some-repo")]


def test_stop_single_container_returns_false_on_failure_without_raising():
    async def fake_exec(*args, **kwargs):
        raise OSError("docker not found")

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = asyncio.run(docker_deploy.stop_single_container("/tmp/some-repo"))

    assert result == {"stopped": False, "output": "docker not found"}


def test_start_single_container_calls_docker_start_with_container_name():
    recorded_calls: list[tuple] = []

    async def fake_exec(*args, **kwargs):
        recorded_calls.append(args)
        return _make_proc(b"hyqs-some-repo\n", 0)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = asyncio.run(docker_deploy.start_single_container("/tmp/some-repo"))

    assert result["started"] is True
    assert recorded_calls == [("docker", "start", "hyqs-some-repo")]


def test_start_single_container_returns_false_on_failure_without_raising():
    async def fake_exec(*args, **kwargs):
        raise OSError("docker not found")

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = asyncio.run(docker_deploy.start_single_container("/tmp/some-repo"))

    assert result == {"started": False, "output": "docker not found"}


# ---------------------------------------------------------------------------
# docker_deploy(): registry push/sign wiring on the non-compose (single
# container) path
# ---------------------------------------------------------------------------

_DEPLOY_RECORD = resources.ResourceRecord(
    cpu_seconds=None,
    peak_rss_bytes=None,
    io_read_bytes=None,
    io_write_bytes=None,
    wall_seconds=0.1,
    sampled_at="now",
)


@contextlib.contextmanager
def _patch_docker_deploy_success(*, push_and_sign_mock):
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch(
                "hyqs.pipeline.docker_deploy.build_image",
                new=AsyncMock(return_value=(True, _DEPLOY_RECORD)),
            )
        )
        stack.enter_context(
            patch(
                "hyqs.pipeline.docker_deploy.run_or_restart_container",
                new=AsyncMock(return_value=(True, "", _DEPLOY_RECORD)),
            )
        )
        stack.enter_context(
            patch(
                "hyqs.pipeline.docker_deploy._read_docker_net_bytes",
                new=AsyncMock(return_value=None),
            )
        )
        stack.enter_context(
            patch(
                "hyqs.pipeline.docker_deploy.registry_push.push_and_sign",
                new=push_and_sign_mock,
            )
        )
        yield


def test_docker_deploy_no_registry_configured_returns_noop_artifact_fields(tmp_path):
    """No registry env vars set -> push_and_sign returns None -> noop artifact defaults,
    and deployed/verified/etc. are unaffected."""
    push_and_sign_mock = AsyncMock(return_value=None)

    with _patch_docker_deploy_success(push_and_sign_mock=push_and_sign_mock):
        result = asyncio.run(docker_deploy.docker_deploy(tmp_path, "{}"))

    assert result["deployed"] is True
    assert result["verified"] is True
    assert result["self_update"] is False
    assert result["output"] == "container started"
    assert result["image_ref"] == ""
    assert result["image_digest"] is None
    assert result["signed"] is False
    push_and_sign_mock.assert_awaited_once()


def test_docker_deploy_registry_configured_merges_artifact_fields(tmp_path):
    """push_and_sign succeeds -> its image_ref/image_digest/signed land in the result,
    deployed/verified remain True."""
    push_and_sign_mock = AsyncMock(
        return_value={
            "image_ref": "reg/app",
            "image_digest": "sha256:abc",
            "signed": True,
            "output": "pushed and signed",
        }
    )

    with _patch_docker_deploy_success(push_and_sign_mock=push_and_sign_mock):
        result = asyncio.run(docker_deploy.docker_deploy(tmp_path, "{}"))

    assert result["deployed"] is True
    assert result["verified"] is True
    assert result["image_ref"] == "reg/app"
    assert result["image_digest"] == "sha256:abc"
    assert result["signed"] is True


def test_docker_deploy_registry_push_failure_does_not_fail_deploy(tmp_path):
    """push_and_sign returns a failure dict -> deployed/verified stay True (whatever
    run_or_restart_container produced); only the artifact fields reflect the failure."""
    push_and_sign_mock = AsyncMock(
        return_value={
            "image_ref": "reg/app",
            "image_digest": None,
            "signed": False,
            "output": "docker push failed",
        }
    )

    with _patch_docker_deploy_success(push_and_sign_mock=push_and_sign_mock):
        result = asyncio.run(docker_deploy.docker_deploy(tmp_path, "{}"))

    assert result["deployed"] is True
    assert result["verified"] is True
    assert result["image_ref"] == "reg/app"
    assert result["image_digest"] is None
    assert result["signed"] is False


def test_docker_deploy_registry_push_raising_does_not_fail_deploy(tmp_path):
    """push_and_sign raising an unexpected exception must not abort docker_deploy() or
    flip deployed/verified to False."""
    push_and_sign_mock = AsyncMock(side_effect=RuntimeError("registry unreachable"))

    with _patch_docker_deploy_success(push_and_sign_mock=push_and_sign_mock):
        result = asyncio.run(docker_deploy.docker_deploy(tmp_path, "{}"))

    assert result["deployed"] is True
    assert result["verified"] is True
    assert result["image_ref"] == ""
    assert result["image_digest"] is None
    assert result["signed"] is False


def test_docker_deploy_skips_registry_push_when_run_fails(tmp_path):
    """run_or_restart_container fails (ok=False) -> push_and_sign is never called and
    the returned dict has the no-op artifact defaults."""
    push_and_sign_mock = AsyncMock(return_value=None)

    with (
        patch(
            "hyqs.pipeline.docker_deploy.build_image",
            new=AsyncMock(return_value=(True, _DEPLOY_RECORD)),
        ),
        patch(
            "hyqs.pipeline.docker_deploy.run_or_restart_container",
            new=AsyncMock(return_value=(False, "container crashed", _DEPLOY_RECORD)),
        ),
        patch(
            "hyqs.pipeline.docker_deploy.registry_push.push_and_sign",
            new=push_and_sign_mock,
        ),
    ):
        result = asyncio.run(docker_deploy.docker_deploy(tmp_path, "{}"))

    assert result["deployed"] is False
    assert result["verified"] is False
    assert result["image_ref"] == ""
    assert result["image_digest"] is None
    assert result["signed"] is False
    push_and_sign_mock.assert_not_awaited()


def test_docker_deploy_keeps_final_line_of_long_fail_logs(tmp_path):
    """run_or_restart_container fails with a long fail_logs traceback whose
    diagnostic final line sits past a 2000-char front-slice. docker_deploy must
    tail-truncate so that line survives into 'output' (regression for the
    docker_deploy job-2254 truncation bug)."""
    padding = "\n".join(f"traceback frame line {i}" for i in range(200))
    long_fail_logs = (
        f"{padding}\nalembic.util.exc.CommandError: Multiple head revisions "
        "are present for given argument 'head'"
    )
    assert len(long_fail_logs) > 2000

    with (
        patch(
            "hyqs.pipeline.docker_deploy.build_image",
            new=AsyncMock(return_value=(True, _DEPLOY_RECORD)),
        ),
        patch(
            "hyqs.pipeline.docker_deploy.run_or_restart_container",
            new=AsyncMock(return_value=(False, long_fail_logs, _DEPLOY_RECORD)),
        ),
    ):
        result = asyncio.run(docker_deploy.docker_deploy(tmp_path, "{}"))

    assert result["deployed"] is False
    assert "Multiple head revisions are present" in result["output"]


def test_docker_deploy_skips_registry_push_when_build_fails(tmp_path):
    """build_image fails -> push_and_sign is never called and the returned dict has
    the no-op artifact defaults."""
    push_and_sign_mock = AsyncMock(return_value=None)

    with (
        patch(
            "hyqs.pipeline.docker_deploy.build_image",
            new=AsyncMock(return_value=(False, None)),
        ),
        patch(
            "hyqs.pipeline.docker_deploy.registry_push.push_and_sign",
            new=push_and_sign_mock,
        ),
    ):
        result = asyncio.run(docker_deploy.docker_deploy(tmp_path, "{}"))

    assert result["deployed"] is False
    assert result["image_ref"] == ""
    assert result["image_digest"] is None
    assert result["signed"] is False
    push_and_sign_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# pull_deploy(): pull-by-digest apply path for promotion-driven deploys
# (Epic 81 step 4). Every step is fail-closed.
# ---------------------------------------------------------------------------


def _make_release(
    image_ref: str = "registry.example.com/proj/app", image_digest: str = "sha256:abc123"
):
    from hyqs.pipeline.models import Release

    return Release(
        id=1,
        project_id=1,
        source_commit="deadbeef",
        image_ref=image_ref,
        image_digest=image_digest,
    )


def test_pull_deploy_aborts_before_any_other_call_on_pull_failure(tmp_path):
    verify_mock = AsyncMock(return_value=True)
    run_mock = AsyncMock()

    with (
        patch(
            "hyqs.pipeline.docker_deploy.registry_push.pull_image_by_digest",
            new=AsyncMock(return_value=False),
        ) as pull_mock,
        patch("hyqs.pipeline.docker_deploy.registry_push.verify_signature", new=verify_mock),
        patch("hyqs.pipeline.docker_deploy.run_or_restart_container", new=run_mock),
    ):
        result = asyncio.run(docker_deploy.pull_deploy(tmp_path, _make_release(), "{}"))

    pull_mock.assert_awaited_once_with(
        "registry.example.com/proj/app", "sha256:abc123", log_sink=None
    )
    verify_mock.assert_not_awaited()
    run_mock.assert_not_awaited()
    assert result["deployed"] is False
    assert result["verified"] is False
    assert result["signed"] is False
    assert result["image_ref"] == "registry.example.com/proj/app"
    assert result["image_digest"] == "sha256:abc123"


def test_pull_deploy_aborts_before_secrets_and_container_on_verify_failure(tmp_path):
    run_mock = AsyncMock()

    with (
        patch(
            "hyqs.pipeline.docker_deploy.registry_push.pull_image_by_digest",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "hyqs.pipeline.docker_deploy.registry_push.verify_signature",
            new=AsyncMock(return_value=False),
        ) as verify_mock,
        patch("hyqs.pipeline.docker_deploy.run_or_restart_container", new=run_mock),
    ):
        result = asyncio.run(docker_deploy.pull_deploy(tmp_path, _make_release(), "{}"))

    verify_mock.assert_awaited_once_with(
        "registry.example.com/proj/app@sha256:abc123", log_sink=None
    )
    run_mock.assert_not_awaited()
    assert result["deployed"] is False
    assert result["verified"] is False
    assert result["signed"] is False
    assert "registry.example.com/proj/app@sha256:abc123" in result["output"]


def test_pull_deploy_aborts_on_missing_required_secret(tmp_path):
    (tmp_path / "secrets.schema.json").write_text(
        json.dumps({"secrets": [{"name": "API_KEY", "required": True}]})
    )
    run_mock = AsyncMock()

    with (
        patch(
            "hyqs.pipeline.docker_deploy.registry_push.pull_image_by_digest",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "hyqs.pipeline.docker_deploy.registry_push.verify_signature",
            new=AsyncMock(return_value=True),
        ),
        patch("hyqs.pipeline.docker_deploy.run_or_restart_container", new=run_mock),
    ):
        deploy_cfg = json.dumps({"secrets_dir": str(tmp_path / "nonexistent-secrets")})
        result = asyncio.run(docker_deploy.pull_deploy(tmp_path, _make_release(), deploy_cfg))

    run_mock.assert_not_awaited()
    assert result["deployed"] is False
    assert "API_KEY" in result["output"]


def test_pull_deploy_skips_validation_when_no_schema_file(tmp_path):
    run_mock = AsyncMock(return_value=(True, "", None))

    with (
        patch(
            "hyqs.pipeline.docker_deploy.registry_push.pull_image_by_digest",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "hyqs.pipeline.docker_deploy.registry_push.verify_signature",
            new=AsyncMock(return_value=True),
        ),
        patch("hyqs.pipeline.docker_deploy.run_or_restart_container", new=run_mock),
    ):
        result = asyncio.run(docker_deploy.pull_deploy(tmp_path, _make_release(), "{}"))

    run_mock.assert_awaited_once()
    assert result["deployed"] is True


def test_pull_deploy_success_uses_digest_ref_and_returns_release_fields(tmp_path):
    run_mock = AsyncMock(return_value=(True, "", _DEPLOY_RECORD))

    with (
        patch(
            "hyqs.pipeline.docker_deploy.registry_push.pull_image_by_digest",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "hyqs.pipeline.docker_deploy.registry_push.verify_signature",
            new=AsyncMock(return_value=True),
        ),
        patch("hyqs.pipeline.docker_deploy.run_or_restart_container", new=run_mock),
    ):
        release = _make_release()
        result = asyncio.run(docker_deploy.pull_deploy(tmp_path, release, "{}"))

    run_call = run_mock.await_args
    assert run_call.args[0] == "registry.example.com/proj/app@sha256:abc123"
    assert result == {
        "deployed": True,
        "command": f"docker run --name {docker_deploy._container_name(tmp_path)} "
        "registry.example.com/proj/app@sha256:abc123",
        "output": "container started",
        "self_update": False,
        "verified": True,
        "resource": _DEPLOY_RECORD,
        "image_ref": "registry.example.com/proj/app",
        "image_digest": "sha256:abc123",
        "signed": True,
    }


# ---------------------------------------------------------------------------
# Opt-in deploy/required-env manifest gate: single-container (run_or_restart_
# container) and compose (compose_deploy) paths.
# ---------------------------------------------------------------------------


def _fake_exec_healthy_with_env(recorded_calls: list[tuple], env_list: list[str]):
    async def fake_exec(*args, **kwargs):
        recorded_calls.append(args)
        cmd = list(args)
        if "inspect" in cmd and "--format" in cmd:
            fmt = cmd[cmd.index("--format") + 1]
            if "State.Status" in fmt:
                return _make_proc(b"running\n")
            if "NetworkSettings.IPAddress" in fmt:
                return _make_proc(b"172.17.0.2\n")
            if "Config.Env" in fmt:
                return _make_proc(json.dumps(env_list).encode() + b"\n")
        return _make_proc(b"abc123\n")

    return fake_exec


def test_run_or_restart_container_no_manifest_is_a_complete_noop(tmp_path):
    """No deploy/required-env file -> the container-env inspect step never runs,
    and cutover proceeds exactly as before repo_path was threaded in."""
    repo = tmp_path / "repo"
    repo.mkdir()
    recorded_calls: list[tuple] = []

    def _run():
        with (
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=_fake_exec_healthy_with_env(recorded_calls, []),
            ),
            patch("urllib.request.urlopen", return_value=_make_urlopen_mock(200)),
        ):
            return asyncio.run(
                run_or_restart_container(
                    "myimage", _CONTAINER, _DEPLOY_CFG, data_dir=None, repo_path=repo
                )
            )

    ok, fail_logs, record = _run()

    assert ok is True
    assert fail_logs == ""
    env_inspects = [
        c
        for c in recorded_calls
        if c[0] == "docker"
        and c[1] == "inspect"
        and "--format" in c
        and "Config.Env" in c[c.index("--format") + 1]
    ]
    assert env_inspects == []


def test_run_or_restart_container_manifest_var_present_passes(tmp_path):
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    (repo / "deploy" / "required-env").write_text("FOO\n")
    recorded_calls: list[tuple] = []

    def _run():
        with (
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=_fake_exec_healthy_with_env(recorded_calls, ["FOO=bar"]),
            ),
            patch("urllib.request.urlopen", return_value=_make_urlopen_mock(200)),
        ):
            return asyncio.run(
                run_or_restart_container(
                    "myimage", _CONTAINER, _DEPLOY_CFG, data_dir=None, repo_path=repo
                )
            )

    ok, fail_logs, record = _run()

    assert ok is True
    assert fail_logs == ""


def test_run_or_restart_container_manifest_var_missing_fails_before_cutover(tmp_path):
    """Missing required var -> cutover aborts, canonical container is never touched
    (last-good preserved), and the failure names the missing var."""
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    (repo / "deploy" / "required-env").write_text("FOO\n")
    recorded_calls: list[tuple] = []

    def _run():
        with (
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=_fake_exec_healthy_with_env(recorded_calls, []),
            ),
            patch("urllib.request.urlopen", return_value=_make_urlopen_mock(200)),
        ):
            return asyncio.run(
                run_or_restart_container(
                    "myimage", _CONTAINER, _DEPLOY_CFG, data_dir=None, repo_path=repo
                )
            )

    ok, fail_logs, record = _run()

    assert ok is False
    assert "FOO" in fail_logs

    stop_rm_targets = {c[-1] for c in recorded_calls if c[0] == "docker" and c[1] in ("stop", "rm")}
    assert _CONTAINER not in stop_rm_targets

    canonical_runs = [
        c
        for c in recorded_calls
        if c[0] == "docker" and c[1] == "run" and _CONTAINER in c and _PROBE not in c
    ]
    assert not canonical_runs, (
        "canonical docker run must not be called when a required var is missing"
    )


def _fake_compose_manifest_exec(ps_output: bytes, env_by_container: dict[str, list[str]]):
    async def fake_exec(*args, **kwargs):
        cmd = list(args)
        if "ps" in cmd:
            return _make_proc(ps_output, 0)
        if "inspect" in cmd and "--format" in cmd:
            fmt = cmd[cmd.index("--format") + 1]
            if "Config.Env" in fmt:
                name = cmd[-1]
                return _make_proc(json.dumps(env_by_container.get(name, [])).encode(), 0)
        if "logs" in cmd:
            return _make_proc(b"", 0)
        return _make_proc(b"", 0)

    return fake_exec


_COMPOSE_PS_TWO_SERVICES = json.dumps(
    [
        {"Service": "backend", "Name": "repo-backend-1", "State": "running", "Health": "healthy"},
        {"Service": "frontend", "Name": "repo-frontend-1", "State": "running"},
    ]
).encode()


def _run_compose_deploy_with_manifest(
    repo, manifest_text, env_by_container, ps_output=_COMPOSE_PS_TWO_SERVICES
):
    (repo / "docker-compose.yml").write_text("services: {}\n")
    (repo / "deploy").mkdir(parents=True, exist_ok=True)
    (repo / "deploy" / "required-env").write_text(manifest_text)

    async def fake_verify_healthy(compose_file, env, cwd, **kwargs):
        return True, ""

    async def fake_probe_http(port, path, timeout, poll_interval, host="127.0.0.1"):
        return True, ""

    with (
        patch(
            "hyqs.pipeline.docker_deploy.resources.measure_subprocess",
            side_effect=_fake_measure_subprocess_up_ok,
        ),
        patch(
            "hyqs.pipeline.docker_deploy._verify_compose_services_healthy",
            side_effect=fake_verify_healthy,
        ),
        patch("hyqs.pipeline.docker_deploy._probe_http", side_effect=fake_probe_http),
        patch(
            "asyncio.create_subprocess_exec",
            side_effect=_fake_compose_manifest_exec(ps_output, env_by_container),
        ),
    ):
        return asyncio.run(docker_deploy.compose_deploy(repo, json.dumps({"port": 9000})))


def test_compose_deploy_no_manifest_deploys_unchanged(tmp_path):
    """No deploy/required-env file -> no `docker compose ps` / env inspect for the
    required-env gate is ever issued; deploy proceeds unchanged."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "docker-compose.yml").write_text("services: {}\n")

    recorded_calls: list[tuple] = []

    async def fake_exec(*args, **kwargs):
        recorded_calls.append(args)
        return _make_proc(b"", 0)

    async def fake_verify_healthy(compose_file, env, cwd, **kwargs):
        return True, ""

    async def fake_probe_http(port, path, timeout, poll_interval, host="127.0.0.1"):
        return True, ""

    with (
        patch(
            "hyqs.pipeline.docker_deploy.resources.measure_subprocess",
            side_effect=_fake_measure_subprocess_up_ok,
        ),
        patch(
            "hyqs.pipeline.docker_deploy._verify_compose_services_healthy",
            side_effect=fake_verify_healthy,
        ),
        patch("hyqs.pipeline.docker_deploy._probe_http", side_effect=fake_probe_http),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
    ):
        result = asyncio.run(docker_deploy.compose_deploy(repo, json.dumps({"port": 9000})))

    assert result["deployed"] is True
    ps_calls = [c for c in recorded_calls if "ps" in c]
    assert ps_calls == []


def test_compose_deploy_manifest_var_present_passes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    result = _run_compose_deploy_with_manifest(
        repo,
        "FOO\n",
        {"repo-backend-1": ["FOO=bar"], "repo-frontend-1": []},
    )

    assert result["deployed"] is True


def test_compose_deploy_manifest_var_missing_fails_naming_var(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    result = _run_compose_deploy_with_manifest(
        repo,
        "FOO\n",
        {"repo-backend-1": [], "repo-frontend-1": []},
    )

    assert result["deployed"] is False
    assert result["verified"] is False
    assert "FOO" in result["output"]


def test_compose_deploy_service_qualified_checks_only_named_service(tmp_path):
    """frontend:FOO must be checked against the frontend container only — it must
    fail even though backend happens to declare FOO."""
    repo = tmp_path / "repo"
    repo.mkdir()

    result = _run_compose_deploy_with_manifest(
        repo,
        "frontend:FOO\n",
        {"repo-backend-1": ["FOO=bar"], "repo-frontend-1": []},
    )

    assert result["deployed"] is False
    assert "FOO" in result["output"]
    assert "frontend" in result["output"]


def test_compose_deploy_service_qualified_present_in_named_service_passes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    result = _run_compose_deploy_with_manifest(
        repo,
        "backend:FOO\n",
        {"repo-backend-1": ["FOO=bar"], "repo-frontend-1": []},
    )

    assert result["deployed"] is True


def test_compose_deploy_manifest_comments_and_blanks_ignored(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    result = _run_compose_deploy_with_manifest(
        repo,
        "\n# a comment\nFOO\n\n  # trailing comment\n",
        {"repo-backend-1": ["FOO=bar"], "repo-frontend-1": []},
    )

    assert result["deployed"] is True


def test_pull_deploy_returns_false_on_container_cutover_failure(tmp_path):
    run_mock = AsyncMock(return_value=(False, "probe crashed", _DEPLOY_RECORD))

    with (
        patch(
            "hyqs.pipeline.docker_deploy.registry_push.pull_image_by_digest",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "hyqs.pipeline.docker_deploy.registry_push.verify_signature",
            new=AsyncMock(return_value=True),
        ),
        patch("hyqs.pipeline.docker_deploy.run_or_restart_container", new=run_mock),
    ):
        result = asyncio.run(docker_deploy.pull_deploy(tmp_path, _make_release(), "{}"))

    assert result["deployed"] is False
    assert result["verified"] is False
    assert result["signed"] is False
    assert "probe crashed" in result["output"]
