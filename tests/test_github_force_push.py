from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock, patch

import pytest

from hyqs.pipeline import github, gitops
from hyqs.pipeline.gitops import GitResult

OID = "a" * 40
PR_URL = "https://github.com/acme/widgets/pull/17"


def _pr_payload(state="OPEN", **changes):
    payload = {
        "url": PR_URL,
        "number": 17,
        "state": state,
        "baseRefName": "main",
        "headRefName": "hyqs/job-17",
        "headRefOid": OID,
    }
    payload.update(changes)
    return json.dumps(payload)


def _pr(state=github.PullRequestState.OPEN):
    return github.PullRequest(state, PR_URL, 17, "main", "hyqs/job-17", OID)


@pytest.mark.parametrize("oid", ["A" * 40, "b" * 64])
def test_local_head_oid_returns_normalized_full_oid(tmp_path, oid):
    result = GitResult(True, oid, "", 0)
    with patch("hyqs.pipeline.gitops.git", new=AsyncMock(return_value=result)) as mock_git:
        actual = asyncio.run(gitops.local_head_oid(tmp_path))
    assert actual == oid.lower()
    mock_git.assert_awaited_once_with(tmp_path, "rev-parse", "--verify", "HEAD^{commit}")


@pytest.mark.parametrize(
    "result",
    [
        GitResult(False, "", "bad revision", 128),
        GitResult(True, "", "", 0),
        GitResult(True, "abc1234", "", 0),
        GitResult(True, "g" * 40, "", 0),
    ],
)
def test_local_head_oid_rejects_unresolved_or_malformed_output(tmp_path, result):
    with patch("hyqs.pipeline.gitops.git", new=AsyncMock(return_value=result)):
        with pytest.raises(RuntimeError, match="local HEAD commit OID"):
            asyncio.run(gitops.local_head_oid(tmp_path))


def test_head_info_keeps_short_display_shape(tmp_path):
    result = GitResult(True, "abc1234\nSubject", "", 0)
    with patch("hyqs.pipeline.gitops.git", new=AsyncMock(return_value=result)):
        assert asyncio.run(gitops.head_info(tmp_path)) == {
            "hash": "abc1234",
            "subject": "Subject",
        }


@pytest.mark.parametrize("state", ["OPEN", "CLOSED", "MERGED"])
def test_inspect_branch_pr_parses_valid_states(tmp_path, state):
    result = GitResult(True, _pr_payload(state), "", 0)
    with patch("hyqs.pipeline.github._gh", new=AsyncMock(return_value=result)) as mock_gh:
        pr = asyncio.run(github.inspect_branch_pr(tmp_path, "hyqs/job-17", base="main"))
    assert pr.state is github.PullRequestState(state)
    assert (pr.url, pr.number, pr.head_ref_oid) == (PR_URL, 17, OID)
    with pytest.raises(FrozenInstanceError):
        pr.number = 18
    mock_gh.assert_awaited_once_with(
        tmp_path,
        "pr",
        "view",
        "hyqs/job-17",
        "--json",
        "url,number,state,baseRefName,headRefName,headRefOid",
    )


def test_inspect_branch_pr_classifies_confirmed_absence(tmp_path):
    result = GitResult(False, "", "no pull requests found for branch", 1)
    with patch("hyqs.pipeline.github._gh", new=AsyncMock(return_value=result)):
        pr = asyncio.run(github.inspect_branch_pr(tmp_path, "hyqs/job-17"))
    assert pr == github.PullRequest(state=github.PullRequestState.MISSING)


@pytest.mark.parametrize(
    "stdout",
    [
        "not json",
        "[]",
        _pr_payload("DRAFT"),
        _pr_payload(number=0),
        _pr_payload(url="https://github.com/acme/widgets/pull/99"),
        _pr_payload(url="https://[malformed/pull/17"),
        _pr_payload(state=[]),
        _pr_payload(baseRefName="develop"),
        _pr_payload(headRefName="another-branch"),
        _pr_payload(headRefOid="abc1234"),
    ],
)
def test_inspect_branch_pr_fails_closed_for_malformed_or_mismatched_data(tmp_path, stdout):
    result = GitResult(True, stdout, "", 0)
    with patch("hyqs.pipeline.github._gh", new=AsyncMock(return_value=result)):
        pr = asyncio.run(github.inspect_branch_pr(tmp_path, "hyqs/job-17", base="main"))
    assert pr == github.PullRequest(state=github.PullRequestState.UNKNOWN)


def test_inspect_branch_pr_classifies_unexpected_command_failure_unknown(tmp_path):
    result = GitResult(False, "", "authentication failed", 1)
    with patch("hyqs.pipeline.github._gh", new=AsyncMock(return_value=result)):
        pr = asyncio.run(github.inspect_branch_pr(tmp_path, "hyqs/job-17"))
    assert pr.state is github.PullRequestState.UNKNOWN


def test_reopen_pr_targets_verified_url(tmp_path):
    result = GitResult(True, "", "", 0)
    with patch("hyqs.pipeline.github._gh", new=AsyncMock(return_value=result)) as mock_gh:
        assert (
            asyncio.run(github.reopen_pr(tmp_path, _pr(github.PullRequestState.CLOSED))) is result
        )
    mock_gh.assert_awaited_once_with(tmp_path, "pr", "reopen", PR_URL)


def test_pr_identity_is_merged_targets_verified_url(tmp_path):
    result = GitResult(True, "MERGED", "", 0)
    with patch("hyqs.pipeline.github._gh", new=AsyncMock(return_value=result)) as mock_gh:
        assert asyncio.run(github.pr_identity_is_merged(tmp_path, _pr())) is True
    mock_gh.assert_awaited_once_with(
        tmp_path, "pr", "view", PR_URL, "--json", "state", "-q", ".state"
    )


def test_merge_pr_squash_targets_verified_url_and_propagates_failure(tmp_path):
    result = GitResult(False, "", "merge blocked", 1)
    with patch("hyqs.pipeline.github._gh", new=AsyncMock(return_value=result)) as mock_gh:
        assert asyncio.run(github.merge_pr_squash(tmp_path, _pr())) is result
    mock_gh.assert_awaited_once_with(tmp_path, "pr", "merge", PR_URL, "--squash", "--delete-branch")


def test_merged_projection_is_idempotent_without_commands(tmp_path):
    with patch("hyqs.pipeline.github._gh", new=AsyncMock()) as mock_gh:
        assert asyncio.run(
            github.pr_identity_is_merged(tmp_path, _pr(github.PullRequestState.MERGED))
        )
        result = asyncio.run(github.merge_pr_squash(tmp_path, _pr(github.PullRequestState.MERGED)))
    assert result.ok
    mock_gh.assert_not_awaited()


def test_identity_operations_reject_untrusted_projection(tmp_path):
    pr = github.PullRequest(state=github.PullRequestState.UNKNOWN)
    with pytest.raises(ValueError, match="identity is not trusted"):
        asyncio.run(github.reopen_pr(tmp_path, pr))


def test_force_push_branch_uses_pipeline_owned_unconditional_force(tmp_path):
    result = GitResult(ok=True, stdout="", stderr="", code=0)
    with patch("hyqs.pipeline.github.git", new=AsyncMock(return_value=result)) as mock_git:
        actual = asyncio.run(github.force_push_branch(tmp_path, "hyqs/job-2701"))

    assert actual is result
    mock_git.assert_awaited_once_with(
        tmp_path,
        "push",
        "--force",
        "-u",
        "origin",
        "hyqs/job-2701",
    )
