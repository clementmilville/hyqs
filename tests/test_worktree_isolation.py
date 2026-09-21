from __future__ import annotations

import asyncio
import subprocess
from unittest.mock import AsyncMock, patch

import pytest

from hyqs.pipeline import gitops


def _git(path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def test_verify_worktree_identity_accepts_exact_root_and_branch(tmp_path):
    _git(tmp_path, "init", "-b", "hyqs/job-42")

    asyncio.run(gitops.verify_worktree_identity(tmp_path, "hyqs/job-42"))


def test_verify_worktree_identity_rejects_wrong_branch(tmp_path):
    _git(tmp_path, "init", "-b", "hyqs/job-42")

    with pytest.raises(gitops.IsolationViolationError, match="branch changed"):
        asyncio.run(gitops.verify_worktree_identity(tmp_path, "hyqs/job-99"))


def test_verify_worktree_identity_rejects_subdirectory(tmp_path):
    _git(tmp_path, "init", "-b", "hyqs/job-42")
    nested = tmp_path / "nested"
    nested.mkdir()

    with pytest.raises(gitops.IsolationViolationError, match="assigned worktree"):
        asyncio.run(gitops.verify_worktree_identity(nested, "hyqs/job-42"))


@pytest.mark.parametrize(
    "origin_url",
    ["https://github.com/openai/codex.git", "git@github.com:openai/codex.git"],
)
def test_ensure_managed_repo_allows_github_origins_with_hardened_clone(tmp_path, origin_url):
    proc = AsyncMock()
    proc.communicate.return_value = (b"", b"")
    proc.returncode = 0

    with patch("hyqs.pipeline.gitops.asyncio.create_subprocess_exec", return_value=proc) as spawn:
        path = asyncio.run(gitops.ensure_managed_repo(tmp_path, 42, origin_url))

    assert path == tmp_path / "repos" / "42"
    spawn.assert_called_once_with(
        "git",
        "-c",
        "protocol.ext.allow=never",
        "clone",
        "--bare",
        "--",
        origin_url,
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


@pytest.mark.parametrize(
    "origin_url",
    [
        "ext::sh -c id",
        "ssh://git@github.com/-oProxyCommand=sh/repo",
        "git@github.com:owner/repo -oProxyCommand=id",
        "-https://github.com/owner/repo",
        "https://gitlab.com/owner/repo",
        "https://github.com/",
    ],
)
def test_ensure_managed_repo_rejects_unsafe_origin_before_side_effects(tmp_path, origin_url):
    with patch("hyqs.pipeline.gitops.asyncio.create_subprocess_exec") as spawn:
        with pytest.raises(ValueError, match="canonical"):
            asyncio.run(gitops.ensure_managed_repo(tmp_path, 42, origin_url))

    spawn.assert_not_called()
    assert not (tmp_path / "repos").exists()


def test_merge_base_into_worktree_terminates_options_before_ref(tmp_path):
    result = gitops.GitResult(ok=True, stdout="", stderr="", code=0)
    with patch("hyqs.pipeline.gitops.git", new=AsyncMock(return_value=result)) as run_git:
        asyncio.run(gitops.merge_base_into_worktree(tmp_path, "-malicious"))

    run_git.assert_awaited_once_with(tmp_path, "merge", "--no-ff", "--", "-malicious")


def test_diff_helpers_terminate_options_before_range(tmp_path):
    result = gitops.GitResult(ok=True, stdout="", stderr="", code=0)
    with patch("hyqs.pipeline.gitops.git", new=AsyncMock(return_value=result)) as run_git:
        asyncio.run(gitops.patch(tmp_path, "-malicious"))
        asyncio.run(gitops.numstat(tmp_path, "-malicious"))

    assert run_git.await_args_list == [
        ((tmp_path, "diff", "--end-of-options", "-malicious", "--"), {}),
        (
            (
                tmp_path,
                "diff",
                "--numstat",
                "--end-of-options",
                "-malicious",
                "--",
            ),
            {},
        ),
    ]


def test_merge_base_helpers_terminate_options_before_refs(tmp_path):
    result = gitops.GitResult(ok=True, stdout="same\n", stderr="", code=0)
    with patch("hyqs.pipeline.gitops.git", new=AsyncMock(return_value=result)) as run_git:
        assert asyncio.run(gitops.is_ancestor(tmp_path, "-ancestor", "-ref"))
        assert not asyncio.run(gitops.base_has_advanced(tmp_path, "-branch", "-base"))

    assert run_git.await_args_list == [
        (
            (
                tmp_path,
                "merge-base",
                "--is-ancestor",
                "--end-of-options",
                "-ancestor",
                "-ref",
            ),
            {},
        ),
        (
            (
                tmp_path,
                "merge-base",
                "--end-of-options",
                "-branch",
                "-base",
            ),
            {},
        ),
        ((tmp_path, "rev-parse", "--verify", "--end-of-options", "-base"), {}),
    ]
