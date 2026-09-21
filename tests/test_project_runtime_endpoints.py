"""Tests for the read-only runtime-status endpoints (Epic 49 job 1):

GET /api/projects/{id}/runtime and GET /api/projects/runtime.

Builds the real Starlette app (hyqs.web.app.build_app) with a MagicMock store
so each route's permission gating and docker/compose normalization can be
checked with starlette.testclient.TestClient, without a live Postgres
connection or a running docker daemon (docker_deploy calls are mocked).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from hyqs.config import Config
from hyqs.pipeline.models import ApiToken, Job, JobSource, Project, Stage, User
from hyqs.web.app import build_app

_TOKEN = "test-web-token"


def _make_project(**overrides) -> Project:
    defaults = dict(
        id=264,
        name="Acme",
        repo_path="/tmp/acme-repo",
        deploy_config='{"port": 8364}',
    )
    defaults.update(overrides)
    return Project(**defaults)


def _project_dict(**overrides) -> dict:
    defaults = dict(
        id=264,
        name="Acme",
        repo_path="/tmp/acme-repo",
        deploy_config='{"port": 8364}',
    )
    defaults.update(overrides)
    return defaults


def _make_client(store: MagicMock, *, permissions: list[str] | None = None) -> TestClient:
    store.list_users.return_value = ["existing-admin"]
    store.get_role_permissions.return_value = {"platform_admin": permissions or []}
    config = Config(model="sonnet", permission_mode="acceptEdits", web_token=_TOKEN)
    orchestrator = MagicMock()
    app = build_app(config, orchestrator, store)
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {_TOKEN}"})
    return client


@pytest.fixture
def store() -> MagicMock:
    s = MagicMock()
    return s


# ---------------------------------------------------------------------------
# GET /api/projects/{id}/runtime
# ---------------------------------------------------------------------------


def test_get_project_runtime_404_for_unknown_project(store):
    store.get_project.return_value = None
    client = _make_client(store)

    resp = client.get("/api/projects/999/runtime")

    assert resp.status_code == 404


def test_get_project_runtime_403_for_non_member_api_token(store):
    store.get_session.return_value = None
    store.get_api_token_by_secret.return_value = ApiToken(
        id=1, project_id=1, name="scoped", role="viewer", last4="abcd"
    )
    store.get_project.return_value = _make_project(id=264)

    config = Config(model="sonnet", permission_mode="acceptEdits", web_token=_TOKEN)
    orchestrator = MagicMock()
    app = build_app(config, orchestrator, store)
    client = TestClient(app)
    client.headers.update({"Authorization": "Bearer scoped-token-secret"})

    resp = client.get("/api/projects/264/runtime")

    assert resp.status_code == 403


def test_get_project_runtime_compose_normalization(store, tmp_path):
    repo = tmp_path / "acme"
    repo.mkdir()
    (repo / "docker-compose.yml").write_text("services: {}\n")
    store.get_project.return_value = _make_project(id=264, repo_path=str(repo))
    client = _make_client(store)

    services = [
        {"name": f"svc{i}", "state": "running", "health": "", "published_ports": []}
        for i in range(5)
    ] + [
        {
            "name": "proxy",
            "state": "running",
            "health": "",
            "published_ports": [
                {
                    "host_ip": "0.0.0.0",
                    "host_port": "8080",
                    "container_port": "80",
                    "protocol": "tcp",
                }
            ],
        }
    ]

    with patch("hyqs.pipeline.docker_deploy.get_compose_status", return_value=services) as mocked:
        resp = client.get("/api/projects/264/runtime")

    assert resp.status_code == 200
    body = resp.json()
    assert body["deploy_mode"] == "compose"
    assert len(body["containers"]) == 6
    assert body["configured_port"] == 8364
    published = [p["host_port"] for c in body["containers"] for p in c.get("published_ports", [])]
    assert "8080" in published
    mocked.assert_called_once()


def test_get_project_runtime_single_container_normalization(store, tmp_path):
    repo = tmp_path / "solo-app"
    repo.mkdir()
    store.get_project.return_value = _make_project(
        id=99, repo_path=str(repo), deploy_config='{"port": 9000}'
    )
    client = _make_client(store)

    container_status = {
        "container_name": "hyqs-solo-app",
        "state": "running",
        "health": "",
        "image_id": "sha256:abc",
        "started_at": "2026-07-18T00:00:00Z",
        "finished_at": "",
        "published_ports": [
            {
                "host_ip": "127.0.0.1",
                "host_port": "9000",
                "container_port": "8080",
                "protocol": "tcp",
            }
        ],
    }

    with (
        patch("hyqs.pipeline.docker_deploy.get_container_status", return_value=container_status),
        patch("hyqs.pipeline.docker_deploy._read_docker_net_bytes", return_value=1234),
    ):
        resp = client.get("/api/projects/99/runtime")

    assert resp.status_code == 200
    body = resp.json()
    assert body["deploy_mode"] == "single"
    assert body["containers"] == [container_status]
    assert body["configured_port"] == 9000
    assert body["net_bytes"] == 1234


def test_get_project_runtime_no_containers_returns_200_not_500(store, tmp_path):
    repo = tmp_path / "empty-app"
    repo.mkdir()
    store.get_project.return_value = _make_project(id=5, repo_path=str(repo), deploy_config="{}")
    client = _make_client(store)

    with (
        patch(
            "hyqs.pipeline.docker_deploy.get_container_status",
            return_value={"container_name": "hyqs-empty-app", "state": "not_found"},
        ),
        patch("hyqs.pipeline.docker_deploy._read_docker_net_bytes", return_value=None),
    ):
        resp = client.get("/api/projects/5/runtime")

    assert resp.status_code == 200
    body = resp.json()
    assert "error" not in body


# ---------------------------------------------------------------------------
# GET /api/projects/runtime (fleet roll-up)
# ---------------------------------------------------------------------------


def test_list_projects_runtime_one_row_per_visible_project(store, tmp_path):
    repo_a = tmp_path / "app-a"
    repo_a.mkdir()
    repo_b = tmp_path / "app-b"
    repo_b.mkdir()
    store.list_projects.return_value = [
        _project_dict(id=1, name="App A", repo_path=str(repo_a), deploy_config='{"port": 8001}'),
        _project_dict(id=2, name="App B", repo_path=str(repo_b), deploy_config='{"port": 8002}'),
    ]
    client = _make_client(store)

    def fake_get_container_status(repo_path):
        return {"container_name": "c", "state": "running", "published_ports": []}

    with (
        patch(
            "hyqs.pipeline.docker_deploy.get_container_status",
            side_effect=fake_get_container_status,
        ),
        patch("hyqs.pipeline.docker_deploy._read_docker_net_bytes", return_value=None),
    ):
        resp = client.get("/api/projects/runtime")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["projects"]) == 2
    project_ids = {row["project_id"] for row in body["projects"]}
    assert project_ids == {1, 2}
    for row in body["projects"]:
        assert row["overall"] == "running"
        assert row["container_count"] == 1
        assert row["running_count"] == 1


def test_list_projects_runtime_no_containers_not_500(store, tmp_path):
    repo = tmp_path / "app-c"
    repo.mkdir()
    store.list_projects.return_value = [
        _project_dict(id=3, name="App C", repo_path=str(repo), deploy_config="{}"),
    ]
    client = _make_client(store)

    with (
        patch(
            "hyqs.pipeline.docker_deploy.get_container_status",
            return_value={"container_name": "c", "state": "not_found"},
        ),
        patch("hyqs.pipeline.docker_deploy._read_docker_net_bytes", return_value=None),
    ):
        resp = client.get("/api/projects/runtime")

    assert resp.status_code == 200
    row = resp.json()["projects"][0]
    assert row["overall"] in ("stopped", "not_found")


def test_list_projects_runtime_isolates_per_project_failure(store, tmp_path):
    """One project's docker call raising must not 500 the whole roll-up, nor
    affect the other project's row."""
    repo_ok = tmp_path / "app-ok"
    repo_ok.mkdir()
    repo_broken = tmp_path / "app-broken"
    repo_broken.mkdir()
    store.list_projects.return_value = [
        _project_dict(id=10, name="OK", repo_path=str(repo_ok), deploy_config="{}"),
        _project_dict(id=11, name="Broken", repo_path=str(repo_broken), deploy_config="{}"),
    ]
    client = _make_client(store)

    def fake_get_container_status(repo_path):
        if repo_path == str(repo_broken):
            raise RuntimeError("docker daemon unreachable")
        return {"container_name": "c", "state": "running", "published_ports": []}

    with (
        patch(
            "hyqs.pipeline.docker_deploy.get_container_status",
            side_effect=fake_get_container_status,
        ),
        patch("hyqs.pipeline.docker_deploy._read_docker_net_bytes", return_value=None),
    ):
        resp = client.get("/api/projects/runtime")

    assert resp.status_code == 200
    rows = {row["project_id"]: row for row in resp.json()["projects"]}
    assert rows[10]["overall"] == "running"
    assert "error" not in rows[10]
    assert rows[11]["overall"] == "unknown"
    assert "error" in rows[11]


# ---------------------------------------------------------------------------
# health + drift wiring (Epic 49 job 2)
# ---------------------------------------------------------------------------


def test_get_project_runtime_matched_ports_no_drift(store, tmp_path):
    repo = tmp_path / "acme"
    repo.mkdir()
    store.get_project.return_value = _make_project(id=264, repo_path=str(repo))
    store.list_projects.return_value = [_project_dict(id=264, repo_path=str(repo))]
    client = _make_client(store)

    container_status = {
        "container_name": "hyqs-acme",
        "state": "running",
        "published_ports": [{"host_port": "8364", "container_port": "8080"}],
    }

    with (
        patch("hyqs.pipeline.docker_deploy.get_container_status", return_value=container_status),
        patch("hyqs.pipeline.docker_deploy._read_docker_net_bytes", return_value=None),
        patch(
            "hyqs.pipeline.deploy.check_public_health",
            return_value={"http_status": 200, "ok": True},
        ),
        patch("hyqs.pipeline.nginx_sites.parse_vhost_port", return_value=8364),
    ):
        resp = client.get("/api/projects/264/runtime")
        rollup = client.get("/api/projects/runtime")

    assert resp.status_code == 200
    body = resp.json()
    assert body["drift"]["mismatch"] is False
    assert body["health"]["ok"] is True

    assert rollup.status_code == 200
    row = rollup.json()["projects"][0]
    assert row["drift_mismatch"] is False
    assert row["health_ok"] is True


def test_get_project_runtime_published_vs_vhost_mismatch(store, tmp_path):
    repo = tmp_path / "acme"
    repo.mkdir()
    store.get_project.return_value = _make_project(id=264, repo_path=str(repo))
    store.list_projects.return_value = [_project_dict(id=264, repo_path=str(repo))]
    client = _make_client(store)

    container_status = {
        "container_name": "hyqs-acme",
        "state": "running",
        "published_ports": [{"host_port": "8080", "container_port": "8080"}],
    }

    with (
        patch("hyqs.pipeline.docker_deploy.get_container_status", return_value=container_status),
        patch("hyqs.pipeline.docker_deploy._read_docker_net_bytes", return_value=None),
        patch(
            "hyqs.pipeline.deploy.check_public_health",
            return_value={"http_status": 502, "ok": False},
        ),
        patch("hyqs.pipeline.nginx_sites.parse_vhost_port", return_value=8364),
    ):
        resp = client.get("/api/projects/264/runtime")
        rollup = client.get("/api/projects/runtime")

    assert resp.status_code == 200
    body = resp.json()
    assert body["drift"]["mismatch"] is True
    assert "8080" in body["drift"]["detail"]
    assert "8364" in body["drift"]["detail"]

    assert rollup.status_code == 200
    row = rollup.json()["projects"][0]
    assert row["drift_mismatch"] is True


def test_get_project_runtime_health_probe_failure_still_returns_200(store, tmp_path):
    repo = tmp_path / "acme"
    repo.mkdir()
    store.get_project.return_value = _make_project(id=264, repo_path=str(repo))
    store.list_projects.return_value = [_project_dict(id=264, repo_path=str(repo))]
    client = _make_client(store)

    container_status = {
        "container_name": "hyqs-acme",
        "state": "running",
        "published_ports": [],
    }

    with (
        patch("hyqs.pipeline.docker_deploy.get_container_status", return_value=container_status),
        patch("hyqs.pipeline.docker_deploy._read_docker_net_bytes", return_value=None),
        patch(
            "hyqs.pipeline.deploy.check_public_health",
            return_value={"http_status": 502, "ok": False, "reason": "http 502"},
        ),
        patch("hyqs.pipeline.nginx_sites.parse_vhost_port", return_value=None),
    ):
        resp = client.get("/api/projects/264/runtime")
        rollup = client.get("/api/projects/runtime")

    assert resp.status_code == 200
    body = resp.json()
    assert body["health"]["ok"] is False
    assert body["health"]["http_status"] == 502

    assert rollup.status_code == 200
    row = rollup.json()["projects"][0]
    assert row["health_ok"] is False


def test_get_project_runtime_disallowed_health_url_returns_200_not_500(store, tmp_path):
    repo = tmp_path / "acme"
    repo.mkdir()
    store.get_project.return_value = _make_project(id=264, repo_path=str(repo))
    client = _make_client(store)

    container_status = {
        "container_name": "hyqs-acme",
        "state": "running",
        "published_ports": [],
    }

    with (
        patch("hyqs.pipeline.docker_deploy.get_container_status", return_value=container_status),
        patch("hyqs.pipeline.docker_deploy._read_docker_net_bytes", return_value=None),
        patch(
            "hyqs.pipeline.deploy.check_public_health",
            return_value={"http_status": None, "ok": False, "reason": "health_url disallowed"},
        ),
        patch("hyqs.pipeline.nginx_sites.parse_vhost_port", return_value=None),
    ):
        resp = client.get("/api/projects/264/runtime")

    assert resp.status_code == 200
    assert resp.json()["health"]["ok"] is False


def test_list_projects_runtime_empty_for_anonymous_non_admin(store):
    store.list_users.return_value = ["existing-admin"]
    store.get_role_permissions.return_value = {"platform_admin": []}
    store.get_api_token_by_secret.return_value = ApiToken(
        id=2, project_id=1, name="scoped", role="viewer", last4="abcd"
    )
    store.get_session.return_value = None
    store.list_projects.return_value = [_project_dict(id=1)]

    config = Config(model="sonnet", permission_mode="acceptEdits", web_token=_TOKEN)
    orchestrator = MagicMock()
    app = build_app(config, orchestrator, store)
    client = TestClient(app)
    client.headers.update({"Authorization": "Bearer scoped-token-secret"})

    resp = client.get("/api/projects/runtime")

    assert resp.status_code == 200
    assert resp.json()["projects"] == []


# ---------------------------------------------------------------------------
# POST /api/projects/{id}/restart (Epic 49 job 4: lifecycle controls)
# ---------------------------------------------------------------------------


def _make_job(**overrides) -> Job:
    defaults = dict(id=42, idea="Restart: redeploy current main", repo_path="/tmp/repo", chat_id=0)
    defaults.update(overrides)
    return Job(**defaults)


def test_post_project_restart_files_deploy_job(store):
    store.get_project.return_value = _make_project(id=264)
    store.get_active_deploy_job.return_value = None
    store.create.return_value = _make_job(id=99)
    store.get_session.return_value = User(id=1, email="admin@example.com", is_platform_admin=True)
    client = _make_client(store, permissions=["queue_job"])

    resp = client.post("/api/projects/264/restart")

    assert resp.status_code == 200
    assert resp.json() == {"job_id": 99}
    kwargs = store.create.call_args.kwargs
    assert kwargs["initial_stage"] == Stage.DEPLOY
    assert kwargs["repo_path"] == "/tmp/acme-repo"
    assert kwargs["source"] == JobSource.UI
    assert kwargs["source_actor"] == "admin@example.com"


def test_post_project_deploy_files_deploy_job(store):
    store.get_project.return_value = _make_project(id=264)
    store.get_active_deploy_job.return_value = None
    store.create.return_value = _make_job(id=100)
    store.get_session.return_value = User(id=1, email="admin@example.com", is_platform_admin=True)
    client = _make_client(store, permissions=["queue_job"])

    resp = client.post("/api/projects/264/deploy")

    assert resp.status_code == 200
    assert resp.json() == {"job_id": 100}
    kwargs = store.create.call_args.kwargs
    assert kwargs["initial_stage"] == Stage.DEPLOY
    assert kwargs["repo_path"] == "/tmp/acme-repo"
    assert kwargs["source"] == JobSource.UI
    assert kwargs["source_actor"] == "admin@example.com"


def test_post_project_restart_409s_when_deploy_already_in_flight(store):
    store.get_project.return_value = _make_project(id=264)
    store.get_active_deploy_job.return_value = _make_job(id=7)
    client = _make_client(store, permissions=["queue_job"])

    resp = client.post("/api/projects/264/restart")

    assert resp.status_code == 409
    assert resp.json()["job_id"] == 7


def test_post_project_restart_forbidden_without_queue_job_permission(store):
    store.get_project.return_value = _make_project(id=264)
    client = _make_client(store, permissions=[])

    resp = client.post("/api/projects/264/restart")

    assert resp.status_code == 403


def test_post_project_restart_404_for_unknown_project(store):
    store.get_project.return_value = None
    client = _make_client(store, permissions=["queue_job"])

    resp = client.post("/api/projects/999/restart")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/projects/{id}/stop
# ---------------------------------------------------------------------------


def test_post_project_stop_forbidden_without_permission(store, tmp_path):
    store.get_project.return_value = _make_project(id=264, repo_path=str(tmp_path))
    client = _make_client(store, permissions=[])

    resp = client.post("/api/projects/264/stop", json={"confirm": True})

    assert resp.status_code == 403


def test_post_project_stop_requires_confirm_flag(store, tmp_path):
    store.get_project.return_value = _make_project(id=264, repo_path=str(tmp_path))
    client = _make_client(store, permissions=["queue_job"])

    resp = client.post("/api/projects/264/stop", json={})

    assert resp.status_code == 400


def test_post_project_stop_single_container_with_confirm(store, tmp_path):
    repo = tmp_path / "solo-app"
    repo.mkdir()
    store.get_project.return_value = _make_project(id=264, repo_path=str(repo))
    client = _make_client(store, permissions=["queue_job"])

    with (
        patch("hyqs.pipeline.docker_deploy.is_user_project", return_value=True),
        patch(
            "hyqs.pipeline.docker_deploy.stop_single_container",
            new=AsyncMock(return_value={"stopped": True, "output": ""}),
        ) as mocked_stop,
        patch(
            "hyqs.pipeline.docker_deploy.get_container_status",
            return_value={"container_name": "hyqs-solo-app", "state": "exited"},
        ),
        patch("hyqs.pipeline.docker_deploy._read_docker_net_bytes", return_value=None),
    ):
        resp = client.post("/api/projects/264/stop", json={"confirm": True})

    assert resp.status_code == 200
    body = resp.json()
    assert body["deploy_mode"] == "single"
    mocked_stop.assert_awaited_once_with(str(repo))


def test_post_project_stop_compose_project_with_confirm(store, tmp_path):
    repo = tmp_path / "compose-app"
    repo.mkdir()
    (repo / "docker-compose.yml").write_text("services: {}\n")
    store.get_project.return_value = _make_project(id=264, repo_path=str(repo))
    client = _make_client(store, permissions=["queue_job"])

    with (
        patch("hyqs.pipeline.docker_deploy.is_user_project", return_value=True),
        patch(
            "hyqs.pipeline.docker_deploy.compose_down",
            new=AsyncMock(return_value={"stopped": True, "output": ""}),
        ) as mocked_down,
        patch("hyqs.pipeline.docker_deploy.get_compose_status", return_value=[]),
    ):
        resp = client.post("/api/projects/264/stop", json={"confirm": True})

    assert resp.status_code == 200
    assert resp.json()["deploy_mode"] == "compose"
    mocked_down.assert_awaited_once()


def test_post_project_stop_rejects_non_docker_project(store, tmp_path):
    store.get_project.return_value = _make_project(id=264, repo_path=str(tmp_path))
    client = _make_client(store, permissions=["queue_job"])

    with patch("hyqs.pipeline.docker_deploy.is_user_project", return_value=False):
        resp = client.post("/api/projects/264/stop", json={"confirm": True})

    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/projects/{id}/start
# ---------------------------------------------------------------------------


def test_post_project_start_forbidden_without_permission(store, tmp_path):
    store.get_project.return_value = _make_project(id=264, repo_path=str(tmp_path))
    client = _make_client(store, permissions=[])

    resp = client.post("/api/projects/264/start")

    assert resp.status_code == 403


def test_post_project_start_injects_port_from_deploy_config(store, tmp_path):
    """Regression guard for the 8080-vs-configured-port drift: starting a
    stopped compose app must inject PORT=<deploy_config.port> exactly as a
    fresh deploy does."""
    repo = tmp_path / "compose-app"
    repo.mkdir()
    (repo / "docker-compose.yml").write_text("services: {}\n")
    store.get_project.return_value = _make_project(
        id=264, repo_path=str(repo), deploy_config='{"port": 9364}'
    )
    client = _make_client(store, permissions=["queue_job"])

    captured_env: dict[str, str] = {}

    async def fake_measure_subprocess(cmd, cwd, timeout, log_sink=None, env=None):
        captured_env.update(env or {})
        from hyqs.pipeline import resources

        record = resources.ResourceRecord(
            cpu_seconds=None,
            peak_rss_bytes=None,
            io_read_bytes=None,
            io_write_bytes=None,
            wall_seconds=0.1,
            sampled_at="now",
        )
        return 0, "compose up ok", record

    with (
        patch("hyqs.pipeline.docker_deploy.is_user_project", return_value=True),
        patch(
            "hyqs.pipeline.docker_deploy.resources.measure_subprocess",
            side_effect=fake_measure_subprocess,
        ),
        patch("hyqs.pipeline.docker_deploy.get_compose_status", return_value=[]),
    ):
        resp = client.post("/api/projects/264/start")

    assert resp.status_code == 200
    assert captured_env["PORT"] == "9364"


def test_post_project_start_single_container(store, tmp_path):
    repo = tmp_path / "solo-app"
    repo.mkdir()
    store.get_project.return_value = _make_project(id=264, repo_path=str(repo))
    client = _make_client(store, permissions=["queue_job"])

    with (
        patch("hyqs.pipeline.docker_deploy.is_user_project", return_value=True),
        patch(
            "hyqs.pipeline.docker_deploy.start_single_container",
            new=AsyncMock(return_value={"started": True, "output": ""}),
        ) as mocked_start,
        patch(
            "hyqs.pipeline.docker_deploy.get_container_status",
            return_value={"container_name": "hyqs-solo-app", "state": "running"},
        ),
        patch("hyqs.pipeline.docker_deploy._read_docker_net_bytes", return_value=None),
    ):
        resp = client.post("/api/projects/264/start")

    assert resp.status_code == 200
    mocked_start.assert_awaited_once_with(str(repo))


# ---------------------------------------------------------------------------
# GET /api/projects/{id}/logs (job #1109)
# ---------------------------------------------------------------------------


def test_get_project_logs_404_for_unknown_project(store):
    store.get_project.return_value = None
    client = _make_client(store)

    resp = client.get("/api/projects/999/logs")

    assert resp.status_code == 404


def test_get_project_logs_403_for_non_member_api_token(store):
    store.get_session.return_value = None
    store.get_api_token_by_secret.return_value = ApiToken(
        id=1, project_id=1, name="scoped", role="viewer", last4="abcd"
    )
    store.get_project.return_value = _make_project(id=264)

    config = Config(model="sonnet", permission_mode="acceptEdits", web_token=_TOKEN)
    orchestrator = MagicMock()
    app = build_app(config, orchestrator, store)
    client = TestClient(app)
    client.headers.update({"Authorization": "Bearer scoped-token-secret"})

    resp = client.get("/api/projects/264/logs")

    assert resp.status_code == 403


def test_get_project_logs_single_container_returns_lines(store, tmp_path):
    repo = tmp_path / "solo-app"
    repo.mkdir()
    store.get_project.return_value = _make_project(id=264, repo_path=str(repo))
    client = _make_client(store)

    with patch(
        "hyqs.pipeline.docker_deploy._get_container_logs", return_value="line one\nline two"
    ) as mocked:
        resp = client.get("/api/projects/264/logs")

    assert resp.status_code == 200
    body = resp.json()
    assert body["lines"] == ["line one", "line two"]
    assert body["service"] == "hyqs-solo-app"
    mocked.assert_awaited_once()


def test_get_project_logs_single_container_missing_returns_empty_lines(store, tmp_path):
    repo = tmp_path / "solo-app"
    repo.mkdir()
    store.get_project.return_value = _make_project(id=264, repo_path=str(repo))
    client = _make_client(store)

    with patch("hyqs.pipeline.docker_deploy._get_container_logs", return_value=""):
        resp = client.get("/api/projects/264/logs")

    assert resp.status_code == 200
    body = resp.json()
    assert body["lines"] == []


def test_get_project_logs_compose_service_returns_lines(store, tmp_path):
    repo = tmp_path / "compose-app"
    repo.mkdir()
    (repo / "docker-compose.yml").write_text("services: {}\n")
    store.get_project.return_value = _make_project(id=264, repo_path=str(repo))
    client = _make_client(store)

    services = [{"name": "web", "state": "running"}, {"name": "worker", "state": "running"}]

    with (
        patch("hyqs.pipeline.docker_deploy.get_compose_status", return_value=services),
        patch(
            "hyqs.pipeline.docker_deploy._compose_service_logs", return_value="worker log line"
        ) as mocked,
    ):
        resp = client.get("/api/projects/264/logs?service=worker")

    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "worker"
    assert body["lines"] == ["worker log line"]
    mocked.assert_awaited_once()


def test_get_project_logs_compose_unknown_service_404(store, tmp_path):
    repo = tmp_path / "compose-app"
    repo.mkdir()
    (repo / "docker-compose.yml").write_text("services: {}\n")
    store.get_project.return_value = _make_project(id=264, repo_path=str(repo))
    client = _make_client(store)

    services = [{"name": "web", "state": "running"}]

    with patch("hyqs.pipeline.docker_deploy.get_compose_status", return_value=services):
        resp = client.get("/api/projects/264/logs?service=nope")

    assert resp.status_code == 404


def test_get_project_logs_compose_missing_service_param_404(store, tmp_path):
    repo = tmp_path / "compose-app"
    repo.mkdir()
    (repo / "docker-compose.yml").write_text("services: {}\n")
    store.get_project.return_value = _make_project(id=264, repo_path=str(repo))
    client = _make_client(store)

    services = [{"name": "web", "state": "running"}]

    with patch("hyqs.pipeline.docker_deploy.get_compose_status", return_value=services):
        resp = client.get("/api/projects/264/logs")

    assert resp.status_code == 404


def test_get_project_logs_tail_clamped_to_max(store, tmp_path):
    repo = tmp_path / "solo-app"
    repo.mkdir()
    store.get_project.return_value = _make_project(id=264, repo_path=str(repo))
    client = _make_client(store)

    with patch("hyqs.pipeline.docker_deploy._get_container_logs", return_value="") as mocked:
        resp = client.get("/api/projects/264/logs?tail=99999")

    assert resp.status_code == 200
    assert resp.json()["tail"] == 500
    _, kwargs = mocked.call_args
    assert kwargs["tail"] <= 500
