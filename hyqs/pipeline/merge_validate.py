"""Post-merge validation: re-check the tree that will actually land on main.

Gates run against a job's branch tip, so a defect introduced by the merge
operation itself — not by either parent — ships to main unexamined (job
#4073's postmortem). ``validate_merge_result`` is the single deterministic
gate the MERGE stage consults, after absorbing a real base advance and
before the irreversible push/squash-merge, to catch exactly that class of
defect: lint/compile breakage in the merged tree, and same-hunk overlap
between the job's own diff and the base's new commits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import linting, testing
from .collision import check_overlapping_merge_hunks


@dataclass(frozen=True, slots=True)
class MergeValidationResult:
    passed: bool
    lint_errors: list[str] = field(default_factory=list)
    test_result: dict | None = None
    overlap_warnings: list[str] = field(default_factory=list)


async def validate_merge_result(
    worktree: str | Path,
    base: str,
    changed_files: list[str],
    job_patch: str,
    base_patch: str,
    *,
    run_tests: bool,
) -> MergeValidationResult:
    """Validate the already-merged worktree before it is pushed to ``base``.

    Runs lint/compile over ``changed_files`` and checks the job's and base's
    diffs for same-file same-hunk overlap. When ``run_tests`` is True (a real
    3-way merge occurred), also reruns the test suite scoped to
    ``changed_files`` against the merged tree. ``passed`` is False if any of
    lint, overlap, or (when run) tests fail.
    """
    lint_result = await linting.run_lint(worktree, changed_files=changed_files)
    lint_errors = [] if lint_result.get("passed") else [lint_result.get("summary", "lint failed")]

    overlap_warnings = check_overlapping_merge_hunks(job_patch, base_patch)

    test_result: dict | None = None
    if run_tests:
        test_result = await testing.run_tests(worktree, changed_files=changed_files)

    passed = (
        not lint_errors
        and not overlap_warnings
        and (test_result is None or bool(test_result.get("passed")))
    )
    return MergeValidationResult(
        passed=passed,
        lint_errors=lint_errors,
        test_result=test_result,
        overlap_warnings=overlap_warnings,
    )
