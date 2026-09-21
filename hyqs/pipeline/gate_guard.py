"""Read-only invariant enforcement for REVIEW and SECURITY gate stages.

Gate stages (review, security) must not mutate the worktree — they only emit
a verdict. This module provides a snapshot-and-restore guard that:

1. Takes a snapshot of (HEAD sha, git status) before the gate runs.
2. Runs the gate function.
3. Compares the snapshot afterward.
4. If the worktree was mutated, discards the changes, retries once, and raises
   GateIsolationError if the gate mutates again.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Coroutine

from .collision import EXEMPT_LOCKFILE_BASENAMES
from .gitops import restore_gate_worktree, worktree_snapshot

log = logging.getLogger("hyqs.gate_guard")


class GateIsolationError(RuntimeError):
    """Raised when a gate stage mutated the worktree after two attempts."""


def _status_paths(status: str) -> set[str]:
    """Parse a ``git status --porcelain=v1 -z`` string into the set of paths
    it references.

    Records are NUL-separated. An ordinary record is ``XY PATH``; a rename
    record is ``XY PATH\\0ORIG_PATH`` (the original path is its own,
    separately NUL-terminated field) — both path fields are collected.
    """
    fields = [f for f in status.split("\0") if f]
    paths: set[str] = set()
    expect_rename_source = False
    for field in fields:
        if expect_rename_source:
            paths.add(field)
            expect_rename_source = False
            continue
        code, path = field[:2], field[3:]
        paths.add(path)
        expect_rename_source = "R" in code
    return paths


def _is_lockfile_byproduct_only(before: tuple[str, str], after: tuple[str, str]) -> bool:
    """True if ``before``/``after`` differ only by exempt lockfile basenames,
    with HEAD unchanged — i.e. the gate's only "mutation" is a package
    manager regenerating a lockfile as a side effect, not a real edit."""
    if before[0] != after[0]:
        return False
    changed = _status_paths(before[1]) ^ _status_paths(after[1])
    if not changed:
        return False
    return all(Path(p).name in EXEMPT_LOCKFILE_BASENAMES for p in changed)


async def run_guarded_gate(
    stage_name: str,
    fn: Callable[..., Coroutine[Any, Any, Any]],
    worktree: str | Path,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run fn, asserting the worktree is unchanged before and after.

    If fn mutates the worktree: discards the mutation, retries fn once against
    the clean tree, and raises GateIsolationError if it mutates again. A
    mutation that is only exempt lockfile byproducts (see
    ``EXEMPT_LOCKFILE_BASENAMES``) with HEAD unchanged is tolerated: the
    worktree is restored and fn's result is returned immediately, without
    retrying or raising.
    """
    before = await worktree_snapshot(worktree)
    result = await fn(*args, **kwargs)
    after = await worktree_snapshot(worktree)
    if before == after:
        return result

    if _is_lockfile_byproduct_only(before, after):
        log.info(
            "gate %r produced only lockfile byproducts; restoring worktree",
            stage_name,
        )
        await restore_gate_worktree(worktree)
        return result

    log.error(
        "gate %r mutated the worktree (HEAD %s; status changed); "
        "discarding and retrying once against clean tree",
        stage_name,
        before[0],
    )
    await restore_gate_worktree(worktree)

    result = await fn(*args, **kwargs)
    after2 = await worktree_snapshot(worktree)
    if before == after2:
        return result

    if _is_lockfile_byproduct_only(before, after2):
        log.info(
            "gate %r produced only lockfile byproducts; restoring worktree",
            stage_name,
        )
        await restore_gate_worktree(worktree)
        return result

    await restore_gate_worktree(worktree)
    raise GateIsolationError(
        f"gate {stage_name!r} mutated the worktree on both attempts; changes discarded"
    )
