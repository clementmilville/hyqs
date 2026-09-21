"""Unit tests for hyqs.pipeline.merge_validate.validate_merge_result."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from hyqs.pipeline import merge_validate


def _run(*args, **kwargs):
    return asyncio.run(merge_validate.validate_merge_result(*args, **kwargs))


def test_validate_merge_result_passes_when_clean_and_tests_skipped():
    with (
        patch.object(
            merge_validate.linting,
            "run_lint",
            AsyncMock(return_value={"passed": True, "summary": ""}),
        ),
        patch.object(merge_validate, "check_overlapping_merge_hunks", return_value=[]),
        patch.object(merge_validate.testing, "run_tests", AsyncMock()) as run_tests,
    ):
        result = _run("/fake/worktree", "main", ["f.py"], "job diff", "base diff", run_tests=False)

    run_tests.assert_not_called()
    assert result.passed is True
    assert result.lint_errors == []
    assert result.overlap_warnings == []
    assert result.test_result is None


def test_validate_merge_result_fails_on_lint_error():
    with (
        patch.object(
            merge_validate.linting,
            "run_lint",
            AsyncMock(return_value={"passed": False, "summary": "ruff: E999 syntax error"}),
        ),
        patch.object(merge_validate, "check_overlapping_merge_hunks", return_value=[]),
        patch.object(merge_validate.testing, "run_tests", AsyncMock()) as run_tests,
    ):
        result = _run("/fake/worktree", "main", ["f.py"], "job diff", "base diff", run_tests=False)

    run_tests.assert_not_called()
    assert result.passed is False
    assert result.lint_errors == ["ruff: E999 syntax error"]
    assert result.overlap_warnings == []


def test_validate_merge_result_fails_on_overlap_even_with_clean_lint():
    overlap = ["job and base both modified 'f.py' around original line 3"]
    with (
        patch.object(
            merge_validate.linting,
            "run_lint",
            AsyncMock(return_value={"passed": True, "summary": ""}),
        ),
        patch.object(merge_validate, "check_overlapping_merge_hunks", return_value=overlap),
        patch.object(
            merge_validate.testing,
            "run_tests",
            AsyncMock(return_value={"passed": True, "summary": ""}),
        ),
    ):
        result = _run("/fake/worktree", "main", ["f.py"], "job diff", "base diff", run_tests=True)

    assert result.passed is False
    assert result.lint_errors == []
    assert result.overlap_warnings == overlap


def test_validate_merge_result_fails_on_failing_tests():
    with (
        patch.object(
            merge_validate.linting,
            "run_lint",
            AsyncMock(return_value={"passed": True, "summary": ""}),
        ),
        patch.object(merge_validate, "check_overlapping_merge_hunks", return_value=[]),
        patch.object(
            merge_validate.testing,
            "run_tests",
            AsyncMock(return_value={"passed": False, "summary": "1 failed"}),
        ) as run_tests,
    ):
        result = _run("/fake/worktree", "main", ["f.py"], "job diff", "base diff", run_tests=True)

    run_tests.assert_awaited_once()
    assert result.passed is False
    assert result.lint_errors == []
    assert result.overlap_warnings == []
    assert result.test_result == {"passed": False, "summary": "1 failed"}
