"""Tests for hyqs.pipeline.provision.provision_project's GitHub org targeting.

Mocks asyncio.create_subprocess_exec (pattern from tests/test_docker_deploy.py)
so no real git/gh/nginx side effects are required, and a MagicMock JobStore
stands in for the real store.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hyqs.pipeline.models import Project
from hyqs.pipeline.provision import provision_project


def _make_proc(stdout_data: bytes = b"ok\n", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout_data, b""))
    return proc


def _make_store() -> MagicMock:
    store = MagicMock()
    project = Project(id=1, name="Demo Project", repo_path="/tmp/demo-project")
    store.create_project.return_value = project
    store.update_project.return_value = project
    return store


def test_gh_repo_create_uses_bare_slug_when_org_empty(tmp_path):
    recorded: list[tuple] = []

    async def fake_exec(*args, **kwargs):
        recorded.append(args)
        return _make_proc()

    store = _make_store()
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        asyncio.run(provision_project(store, tmp_path, "Demo Project", github_org=""))

    gh_calls = [c for c in recorded if c and c[0] == "gh"]
    assert gh_calls, "gh repo create was not invoked"
    assert gh_calls[0][3] == "demo-project"


def test_gh_repo_create_uses_org_slug_when_org_set(tmp_path):
    recorded: list[tuple] = []

    async def fake_exec(*args, **kwargs):
        recorded.append(args)
        return _make_proc()

    store = _make_store()
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        asyncio.run(provision_project(store, tmp_path, "Demo Project", github_org="my-org"))

    gh_calls = [c for c in recorded if c and c[0] == "gh"]
    assert gh_calls, "gh repo create was not invoked"
    assert gh_calls[0][3] == "my-org/demo-project"


@pytest.mark.parametrize("bad_org", ["bad org", "bad/org", "-bad", "bad-", "bad_org"])
def test_invalid_org_rejected_before_any_subprocess(tmp_path, bad_org):
    recorded: list[tuple] = []

    async def fake_exec(*args, **kwargs):
        recorded.append(args)
        return _make_proc()

    store = _make_store()
    target_dir = tmp_path / "demo-project"
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        with pytest.raises(ValueError, match="HYQS_GITHUB_ORG"):
            asyncio.run(provision_project(store, tmp_path, "Demo Project", github_org=bad_org))

    assert recorded == []
    assert not target_dir.exists()
