"""Tests for POST /api/projects (hyqs.web.app.create_project_route).

Builds the real Starlette app (hyqs.web.app.build_app) with a MagicMock store
so the route's 409-before-membership-write ordering and its projects_dir
scope check can be verified with starlette.testclient.TestClient, without a
live Postgres connection.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from hyqs.config import Config
from hyqs.pipeline.models import Project, User
from hyqs.web.app import build_app

_TOKEN = "test-web-token"


def _make_client(
    store: MagicMock, projects_dir, *, permissions: list[str] | None = None
) -> TestClient:
    store.list_users.return_value = ["existing-admin"]
    store.get_role_permissions.return_value = {"platform_admin": permissions or []}
    store.get_session.return_value = User(id=1, email="admin@example.com", is_platform_admin=True)
    config = Config(
        model="sonnet", permission_mode="acceptEdits", web_token=_TOKEN, projects_dir=projects_dir
    )
    orchestrator = MagicMock()
    app = build_app(config, orchestrator, store)
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {_TOKEN}"})
    return client


@pytest.fixture
def store() -> MagicMock:
    return MagicMock()


def test_create_project_route_returns_409_when_repo_path_already_registered(store, tmp_path):
    repo = tmp_path / "existing-repo"
    repo.mkdir()
    store.create_project_exclusive.return_value = None
    client = _make_client(store, tmp_path, permissions=["create_project"])

    resp = client.post("/api/projects", json={"name": "Attacker", "repo": str(repo)})

    assert resp.status_code == 409
    store.create_project_exclusive.assert_called_once_with("Attacker", str(repo))
    store.add_project_member.assert_not_called()


def test_create_project_route_returns_400_for_repo_outside_projects_dir(store, tmp_path):
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    outside_repo = tmp_path / "outside" / "victim-repo"
    outside_repo.mkdir(parents=True)
    client = _make_client(store, projects_dir, permissions=["create_project"])

    resp = client.post("/api/projects", json={"name": "Attacker", "repo": str(outside_repo)})

    assert resp.status_code == 400
    store.create_project_exclusive.assert_not_called()
    store.add_project_member.assert_not_called()


def test_create_project_route_creates_project_and_grants_membership(store, tmp_path):
    repo = tmp_path / "new-repo"
    repo.mkdir()
    project = Project(id=7, name="New Project", repo_path=str(repo))
    store.create_project_exclusive.return_value = project
    client = _make_client(store, tmp_path, permissions=["create_project"])

    resp = client.post("/api/projects", json={"name": "New Project", "repo": str(repo)})

    assert resp.status_code == 201
    store.create_project_exclusive.assert_called_once_with("New Project", str(repo))
    store.add_project_member.assert_called_once_with(7, "1", "project_admin")
