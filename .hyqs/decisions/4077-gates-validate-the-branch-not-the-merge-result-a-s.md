# Job #4077: Gates validate the branch, not the merge result — a SyntaxError reached main

**Date:** 2026-08-19

This diff adds post-merge validation to catch defects introduced by the merge operation itself rather than by either parent (job #4077 postmortem). A new `check_overlapping_merge_hunks()` function detects when the job's diff and the base's new commits both modified the same line ranges of the same file—the merge-stacking shape that shipped unexamined because gates only ran against the branch tip. The validation module runs lint, tests, and overlap checks on the already-merged worktree only when a real base advance occurred, reporting failures back to conflict recovery before the irreversible push/squash-merge call. Tests cover the overlap detection, the validation integration paths (fast-forward skip, passing validation, lint/test/overlap failures), and ensure the merge stage properly captures diffs against a shared merge-base for comparison.
This diff adds post-merge validation to catch defects introduced by the merge operation itself rather than by either parent (job #4077 postmortem). A new `check_overlapping_merge_hunks()` function detects when the job's diff and the base's new commits both modified the same line ranges of the same file—the merge-stacking shape that shipped unexamined because gates only ran against the branch tip. The validation module runs lint, tests, and overlap checks on the already-merged worktree only when a real base advance occurred, reporting failures back to conflict recovery before the irreversible push/squash-merge call. Tests cover the overlap detection, the validation integration paths (fast-forward skip, passing validation, lint/test/overlap failures), and ensure the merge stage properly captures diffs against a shared merge-base for comparison.

## Files touched
- hyqs/pipeline/collision.py
- hyqs/pipeline/merge_validate.py
- hyqs/pipeline/stages/merge.py
- tests/test_collision.py
- tests/test_merge_validate.py
- tests/test_stages_merge_phantom_conflict.py
- tests/test_stages_merge_post_merge_validation.py
