"""Deterministic auto-resolution for the trivial "pure two-sided append" merge
conflict shape (job #2272).

Postmortem: three acme-identity jobs each failed to merge repeatedly
against the same hot files because unrelated concurrent jobs each appended a
new member to the same growing list/enum. Every conflict was the simplest
shape possible — both sides only ADD lines after an identical anchor line,
neither side edits or deletes anything — yet each job burned a full
plan->build->lint->test->review->security->merge cycle only to land on the
identical unresolved shape again, because a fresh AI replan has no way to
know "someone else also appended to this list".

This module recognizes that one shape and resolves it without AI, mirroring
``collision.py``'s pure-helper pattern: parse first (no I/O), then a thin
async orchestration layer that reads/writes the worktree.  Fails closed on
anything else — including diff3 ``|||||||`` ancestor markers, malformed
marker counts, and any hunk where one side's block is empty (a pure add vs
delete) — and never partially resolves a multi-file conflict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import gitops

_CONFLICT_START = "<<<<<<<"
_CONFLICT_ANCESTOR = "|||||||"
_CONFLICT_MID = "======="
_CONFLICT_END = ">>>>>>>"


def resolve_pure_append_conflicts(text: str) -> str | None:
    """Return ``text`` with every conflict hunk resolved, or ``None`` if any
    hunk is not a safe "pure append" shape.

    A hunk is a safe pure append when both its "ours" block (between
    ``<<<<<<<`` and ``=======``) and its "theirs" block (between ``=======``
    and ``>>>>>>>``) are non-empty. Diff3-style ``|||||||`` ancestor markers,
    or an unbalanced/malformed marker sequence, cause the whole file to be
    rejected (return ``None``) — this never guesses at semantic equivalence.

    Resolution concatenates the ours-block then the theirs-block, both fully
    retained; downstream lint/fix-stage machinery already reliably handles
    any resulting cosmetic reordering (confirmed in the job #2272 incident's
    own job_events).
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    n = len(lines)
    saw_hunk = False

    while i < n:
        line = lines[i]
        stripped = line.rstrip("\n").rstrip("\r")
        if not stripped.startswith(_CONFLICT_START):
            out.append(line)
            i += 1
            continue

        # Found a hunk start. Collect ours-block until '=======' (rejecting
        # '|||||||' ancestor markers along the way), then theirs-block until
        # the matching '>>>>>>>'.
        saw_hunk = True
        i += 1
        ours: list[str] = []
        while i < n and not lines[i].rstrip("\n").rstrip("\r").startswith(
            (_CONFLICT_MID, _CONFLICT_ANCESTOR)
        ):
            if lines[i].rstrip("\n").rstrip("\r").startswith(_CONFLICT_START):
                return None  # nested/unbalanced marker — fail closed
            ours.append(lines[i])
            i += 1
        if i >= n:
            return None  # ran off the end without a '=======' — malformed
        if lines[i].rstrip("\n").rstrip("\r").startswith(_CONFLICT_ANCESTOR):
            return None  # diff3 ancestor marker present — never guess

        i += 1  # consume '======='
        theirs: list[str] = []
        while i < n and not lines[i].rstrip("\n").rstrip("\r").startswith(_CONFLICT_END):
            if lines[i].rstrip("\n").rstrip("\r").startswith(
                (_CONFLICT_START, _CONFLICT_MID, _CONFLICT_ANCESTOR)
            ):
                return None  # unbalanced marker — fail closed
            theirs.append(lines[i])
            i += 1
        if i >= n:
            return None  # ran off the end without a '>>>>>>>' — malformed
        i += 1  # consume '>>>>>>>'

        if not ours or not theirs:
            return None  # pure add-vs-delete — not a safe append
        if len(ours) == len(theirs):
            # Without diff3 ancestor markers there is no way to tell "both sides
            # added a different new line" from "both sides edited the same
            # original line" purely from marker text — a modify/modify conflict
            # on N original lines produces exactly N-vs-N line blocks, which is
            # indistinguishable at this level from an N-vs-N pure append. Fail
            # closed rather than guess; genuine multi-line appends from two
            # concurrent branches (the shape this module targets) essentially
            # never coincide on an identical line count.
            return None

        out.extend(ours)
        out.extend(theirs)

    if not saw_hunk:
        return None  # no conflict markers at all — nothing for this path to do
    return "".join(out)


@dataclass
class AutoresolveResult:
    ok: bool
    files: list[str] = field(default_factory=list)
    reason: str = ""


async def autoresolve_conflict(worktree: str | Path) -> AutoresolveResult:
    """Resolve every conflicted file in ``worktree`` if — and only if — every
    one of them is entirely composed of safe pure-append hunks (S1).

    Zero side effects on any failure path: nothing is written, staged, or
    committed unless *every* conflicted file resolves cleanly, leaving the
    working tree exactly as the failed merge left it (ready for the existing
    AI ``fix_conflict`` path). On success, writes the resolved text, stages
    the resolved files, and completes the in-progress merge with
    ``git commit --no-edit``.
    """
    listed = await gitops.git(worktree, "diff", "--name-only", "--diff-filter=U")
    if not listed.ok:
        return AutoresolveResult(ok=False, reason=f"could not list conflicted files: {listed.stderr}")
    conflicted = [f for f in listed.stdout.splitlines() if f.strip()]
    if not conflicted:
        return AutoresolveResult(ok=False, reason="no conflicted files")

    root = Path(worktree)
    resolved: dict[str, str] = {}
    for rel_path in conflicted:
        full_path = root / rel_path
        try:
            original = full_path.read_text()
        except (OSError, UnicodeDecodeError) as exc:
            return AutoresolveResult(ok=False, reason=f"could not read {rel_path}: {exc}")
        new_text = resolve_pure_append_conflicts(original)
        if new_text is None:
            return AutoresolveResult(
                ok=False, reason=f"{rel_path} has a non-pure-append conflict hunk"
            )
        resolved[rel_path] = new_text

    # Only now, with every file verified resolvable, apply any writes.
    for rel_path, new_text in resolved.items():
        (root / rel_path).write_text(new_text)

    add_res = await gitops.git(worktree, "add", "--", *resolved.keys())
    if not add_res.ok:
        return AutoresolveResult(ok=False, reason=f"git add failed: {add_res.stderr}")

    commit_res = await gitops.git(worktree, "commit", "--no-edit")
    if not commit_res.ok:
        return AutoresolveResult(ok=False, reason=f"git commit failed: {commit_res.stderr}")

    return AutoresolveResult(ok=True, files=sorted(resolved.keys()))
