"""Deterministic git operations for the pipeline.

The runner (not the agent) performs all git plumbing: branching, worktree
creation, committing the agent's output, merging, and cleanup. This keeps the
irreversible operations predictable and auditable.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import resources

log = logging.getLogger("hyqs.gitops")

_ARTIFACT_EXCLUDES = (
    "__pycache__",
    "*.pyc",
    "*.pyo",
    "*.pyd",
    "*$py.class",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    "*.egg-info",
    "build",
    "dist",
    "node_modules",
    ".DS_Store",
    "*.db",
    "*.sqlite3",
    "*.sqlite",
)
_DISPOSABLE_JOB_REF = re.compile(r"^refs/heads/hyqs/job-[^/]+$")
_LOCAL_HEAD_REF = re.compile(r"refs/heads/[A-Za-z0-9._/-]+")
_GITHUB_ORIGIN_RE = re.compile(
    r"(?:https://github\.com/|git@github\.com:)"
    r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?"
)


def _validate_github_origin_url(origin_url: str) -> None:
    """Reject origins that Git could interpret as options or executable transports."""
    if (
        not origin_url
        or origin_url.startswith("-")
        or any(char.isspace() for char in origin_url)
        or _GITHUB_ORIGIN_RE.fullmatch(origin_url) is None
    ):
        raise ValueError(
            "origin_url must be a canonical https://github.com/owner/repo or "
            "git@github.com:owner/repo GitHub URL"
        )


def _is_artifact(path: str) -> bool:
    """Return True if any component of path matches a known generated artifact pattern."""
    for part in Path(path).parts:
        for pattern in _ARTIFACT_EXCLUDES:
            if fnmatch.fnmatch(part, pattern):
                return True
    return False


async def _stage_clean(worktree: str | Path) -> None:
    """Stage all changes in worktree, excluding known generated artifacts.

    Already-tracked artifacts are removed from the index (staged as deletions)
    so polluted repos self-heal on the very next commit — without touching working files.
    """
    await git(worktree, "add", "-A")
    tracked = await git(worktree, "ls-files", "--cached")
    if not (tracked.ok and tracked.stdout.strip()):
        return
    artifacts = [f for f in tracked.stdout.splitlines() if _is_artifact(f)]
    if artifacts:
        await git(worktree, "rm", "--cached", "--ignore-unmatch", "--quiet", "--", *artifacts)


class IsolationViolationError(RuntimeError):
    """Raised when an agentic stage modified the main checkout instead of its worktree."""


@dataclass
class GitResult:
    ok: bool
    stdout: str
    stderr: str
    code: int


async def git(repo: str | Path, *args: str, timeout: float | None = None) -> GitResult:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repo),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning("git %s -> timed out after %ss", args, timeout)
        return GitResult(ok=False, stdout="", stderr=f"timed out after {timeout}s", code=-1)
    res = GitResult(
        ok=proc.returncode == 0,
        stdout=out.decode().strip(),
        stderr=err.decode().strip(),
        code=proc.returncode or 0,
    )
    if not res.ok:
        log.debug("git %s -> %s | %s", args, res.code, res.stderr[:200])
    return res


async def verify_worktree_identity(worktree: Path, expected_branch: str) -> None:
    """Verify the exact checkout root and job-owned branch around agent I/O."""
    expected_root = worktree.resolve()
    root = await git(worktree, "rev-parse", "--show-toplevel")
    branch = await git(worktree, "branch", "--show-current")
    if not root.ok or Path(root.stdout).resolve() != expected_root:
        raise IsolationViolationError(
            f"agent cwd is not its assigned worktree: expected {expected_root}, "
            f"got {root.stdout or root.stderr or 'unknown'}"
        )
    if not branch.ok or branch.stdout != expected_branch:
        raise IsolationViolationError(
            f"agent worktree branch changed: expected {expected_branch!r}, "
            f"got {branch.stdout or branch.stderr or 'unknown'}"
        )


async def default_branch(repo: str | Path) -> str:
    """The repository's default branch — the pipeline's merge target.

    Resolve the *repo's* default (the remote's HEAD, e.g. ``origin/main``), NOT
    whatever branch happens to be checked out. The old code used
    ``symbolic-ref HEAD``, which returns the current branch — so on a checkout
    sitting on a feature branch the pipeline branched off and merged into that
    feature branch instead of main.
    """
    res = await git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if res.ok and res.stdout:
        # e.g. "origin/main" -> "main"
        return res.stdout.split("/", 1)[1] if "/" in res.stdout else res.stdout
    # No remote HEAD (offline / fresh repo): fall back to the usual names.
    for candidate in ("main", "master"):
        if (await git(repo, "rev-parse", "--verify", candidate)).ok:
            return candidate
    return "main"


async def fresh_base(repo: str | Path, base: str, timeout: float = 15.0) -> str:
    """Fetch origin's ``base`` into the local ``base`` ref and return ``base``.

    Diff-scoping gates that compute ``{base}...HEAD`` against a possibly-stale
    local ``base`` ref can pick up commits that landed on the real remote base
    after this branch forked but before the local ref was last updated —
    misattributing other jobs' changes to this one. Fetching immediately before
    scoping (``fetch origin <base>:<base>``, the same direct-refspec trick as
    ``github.sync_base``) keeps the scope accurate without requiring a checkout.

    If fetch identifies one corrupt, disposable local Hyqs job ref, delete only
    that ref after proving it is invalid and unattached to a live worktree, then
    retry once. Never raises: on no remote, fetch failure, timeout, or unsafe
    repair, logs a warning and returns ``base`` unchanged.
    """
    remote_res = await git(repo, "remote")
    if "origin" not in remote_res.stdout.split():
        return base
    res = await git(repo, "fetch", "origin", f"{base}:{base}", timeout=timeout)
    if res.ok:
        return base

    repaired = await _repair_fetch_blocking_ref(repo, res.stderr)
    if repaired:
        log.warning("fresh_base: removed invalid disposable ref %s; retrying fetch", repaired)
        retry = await git(repo, "fetch", "origin", f"{base}:{base}", timeout=timeout)
        if retry.ok:
            return base
        log.warning(
            "fresh_base: fetch origin %s failed after repairing %s for %s: %s",
            base,
            repaired,
            repo,
            retry.stderr[:300],
        )
        return base
    log.warning(
        "fresh_base: fetch origin %s failed for %s; repair refused or unavailable, "
        "using local base: %s",
        base,
        repo,
        res.stderr[:300],
    )
    return base


async def _repair_fetch_blocking_ref(repo: str | Path, diagnostic: str) -> str | None:
    """Remove the one invalid disposable job ref named by a fetch failure."""
    invalid_markers = ("bad object", "bad ref", "not a valid object", "invalid object")
    if not any(marker in diagnostic.lower() for marker in invalid_markers):
        return None
    named_refs = set(_LOCAL_HEAD_REF.findall(diagnostic))
    if len(named_refs) != 1:
        if named_refs:
            log.warning(
                "fresh_base: refusing ambiguous invalid-ref repair; fetch named refs: %s",
                ", ".join(sorted(named_refs)),
            )
        return None
    ref = named_refs.pop().rstrip(".,:;'\"")
    if not _DISPOSABLE_JOB_REF.fullmatch(ref):
        log.warning("fresh_base: refusing repair of protected or non-Hyqs ref %s", ref)
        return None
    if (await git(repo, "rev-parse", "--verify", f"{ref}^{{object}}")).ok:
        log.warning("fresh_base: refusing repair because named ref %s is valid", ref)
        return None

    worktrees = await git(repo, "worktree", "list", "--porcelain")
    if not worktrees.ok:
        log.warning(
            "fresh_base: refusing repair of %s because live worktrees could not be inspected: %s",
            ref,
            worktrees.stderr[:300],
        )
        return None
    attached_refs = {
        line.removeprefix("branch ")
        for line in worktrees.stdout.splitlines()
        if line.startswith("branch ")
    }
    if ref in attached_refs:
        log.warning("fresh_base: refusing repair of %s because it is attached to a worktree", ref)
        return None

    deleted = await git(repo, "update-ref", "-d", ref)
    if not deleted.ok and not await _unlink_broken_loose_ref(repo, ref):
        log.warning(
            "fresh_base: failed to remove invalid disposable ref %s: %s",
            ref,
            deleted.stderr[:300],
        )
        return None
    return ref


async def _unlink_broken_loose_ref(repo: str | Path, ref: str) -> bool:
    """Remove one already-validated loose ref when ``update-ref`` cannot parse it."""
    common_dir_result = await git(repo, "rev-parse", "--git-common-dir")
    if not common_dir_result.ok or not common_dir_result.stdout:
        return False
    common_dir = Path(common_dir_result.stdout)
    if not common_dir.is_absolute():
        common_dir = Path(repo) / common_dir
    common_dir = common_dir.resolve()
    ref_path = (common_dir / ref).resolve()
    if not ref_path.is_relative_to(common_dir) or ref_path.is_symlink():
        return False
    try:
        ref_path.unlink()
    except OSError:
        return False
    return True


async def is_git_repo(repo: str | Path) -> bool:
    return (await git(repo, "rev-parse", "--is-inside-work-tree")).ok


async def create_worktree(
    repo: str | Path,
    branch: str,
    worktree_path: Path,
    base: str = "",
) -> GitResult:
    """Create ``branch`` in an isolated worktree from ``base``.

    Callers that already synchronized a base can pass it explicitly. Other
    callers retain the default-branch discovery behavior.
    """
    if not base:
        base = await default_branch(repo)
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    # Drop stale .git/worktrees/* registrations whose directories no longer exist.
    await git(repo, "worktree", "prune")
    # If the branch was left behind by a previous failed attempt, remove it so
    # `worktree add -b` doesn't hard-fail with "branch already exists".
    if (await git(repo, "show-ref", "--verify", f"refs/heads/{branch}")).ok:
        await git(repo, "branch", "-D", branch)
    # Branch off the current tip of the base branch into an isolated worktree.
    return await git(repo, "worktree", "add", "-b", branch, "--", str(worktree_path), base)


async def seed_remediation_worktree(
    repo: str | Path,
    worktree: str | Path,
    *,
    base: str,
    source_branch: str,
    source_sha: str,
) -> None:
    """Apply the immutable failed candidate delta to a fresh remediation tree.

    The source SHA must still exist and be reachable from the recorded source
    branch. The candidate delta is derived from its merge-base with the fresh
    target base and applied without committing, leaving the deterministic
    runner to commit the builder's eventual result.
    """
    if not source_branch.startswith("hyqs/job-") or not re.fullmatch(
        r"[0-9a-fA-F]{40}", source_sha
    ):
        raise ValueError("invalid remediation source branch or SHA")
    source_ref = f"refs/heads/{source_branch}"
    if not (await git(repo, "rev-parse", "--verify", f"{source_sha}^{{commit}}")).ok:
        raise ValueError(f"remediation source SHA is unavailable: {source_sha}")
    if not (await git(repo, "rev-parse", "--verify", source_ref)).ok:
        raise ValueError(f"remediation source branch is unavailable: {source_branch}")
    if not await is_ancestor(repo, source_sha, source_ref):
        raise ValueError("remediation source SHA does not belong to recorded branch")
    merge_base = await git(repo, "merge-base", base, source_sha)
    if not merge_base.ok or not merge_base.stdout:
        raise ValueError("cannot derive remediation source merge-base")
    applied = await git(
        worktree, "cherry-pick", "--no-commit", f"{merge_base.stdout}..{source_sha}"
    )
    if not applied.ok:
        await git(worktree, "cherry-pick", "--abort")
        raise ValueError(f"cannot apply remediation source diff: {applied.stderr[:300]}")


async def create_detached_worktree(
    repo: str | Path, worktree_path: Path, ref: str = ""
) -> GitResult:
    """Read-only style worktree: detached HEAD at ``ref`` (default branch tip).

    Used by the PLAN stage so the planner never executes in the shared checkout
    — agent Bash there dirtied files that the build-stage isolation guard then
    blamed on concurrent builds (the 'oinbase.py' false-positive incident).
    """
    if not ref:
        ref = await default_branch(repo)
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    await git(repo, "worktree", "prune")
    return await git(repo, "worktree", "add", "--detach", "--", str(worktree_path), ref)


async def has_changes(worktree: str | Path) -> bool:
    res = await git(worktree, "status", "--porcelain")
    return bool(res.stdout.strip())


def _porcelain_path(line: str) -> str:
    """Extract the pathname from one `git status --porcelain` line.

    Format is ``XY <path>`` (``XY`` = 2-char status), with renames rendered as
    ``R  old -> new``; for those we key on the destination path.
    """
    body = line[3:] if len(line) > 3 else line
    if " -> " in body:
        body = body.split(" -> ", 1)[1]
    return body.strip().strip('"')


async def dirty_paths(repo: str | Path) -> set[str]:
    """Set of paths git reports as dirty (modified or untracked) in ``repo``.

    Used to scope the isolation guard to what a stage *newly* dirtied: snapshot
    before the agent runs, snapshot after, and only the difference counts as a
    leak. A plain ``has_changes`` check false-positives on pre-existing dirt in
    the shared checkout (e.g. an unrelated untracked ops script), which would
    fail every job on that checkout regardless of what the agent did.
    """
    res = await git(repo, "status", "--porcelain")
    return {_porcelain_path(ln) for ln in res.stdout.splitlines() if ln.strip()}


async def commit_all(worktree: str | Path, message: str) -> bool:
    """Stage and commit everything in the worktree. Returns True if a commit was made."""
    if not await has_changes(worktree):
        return False
    await _stage_clean(worktree)
    res = await git(worktree, "commit", "-m", message)
    return res.ok


async def patch(worktree: str | Path, rng: str) -> str:
    """The full unified diff for a range (e.g. 'main...HEAD' or 'HEAD~1..HEAD')."""
    res = await git(worktree, "diff", "--end-of-options", rng, "--")
    return res.stdout


async def tracked_files(repo: str | Path, limit: int = 1500) -> list[str]:
    """Repo-relative paths of all git-tracked files — the authoritative file map.

    Fed to the planner so it reads the files that actually exist instead of
    hypothesising paths (backend/coinbase.py, app/routers/…) and thrashing on
    the misses. Gitignored trees (.venv, node_modules) are excluded by git.
    """
    res = await git(repo, "ls-files")
    if not res.ok:
        return []
    return res.stdout.splitlines()[:limit]


async def head_info(worktree: str | Path) -> dict:
    """The current commit's short hash + subject line."""
    res = await git(worktree, "log", "-1", "--format=%h%n%s")
    lines = res.stdout.splitlines()
    return {"hash": lines[0] if lines else "", "subject": lines[1] if len(lines) > 1 else ""}


async def local_head_oid(worktree: str | Path) -> str:
    """Resolve HEAD to its canonical full commit object ID or fail explicitly."""
    res = await git(worktree, "rev-parse", "--verify", "HEAD^{commit}")
    oid = res.stdout.strip().lower()
    if not res.ok:
        detail = res.stderr.strip() or f"git exited with status {res.code}"
        raise RuntimeError(f"could not resolve local HEAD commit OID: {detail}")
    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", oid) is None:
        raise RuntimeError("git returned an invalid full local HEAD commit OID")
    return oid


async def numstat(worktree: str | Path, rng: str) -> list[dict]:
    """Per-file added/deleted line counts for a diff range (e.g. 'main...HEAD')."""
    res = await git(worktree, "diff", "--numstat", "--end-of-options", rng, "--")
    files: list[dict] = []
    for line in res.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            added, deleted, path = parts
            files.append(
                {
                    "path": path,
                    "added": 0 if added == "-" else int(added),
                    "deleted": 0 if deleted == "-" else int(deleted),
                }
            )
    return files


async def is_ancestor(repo: str | Path, ancestor: str, ref: str) -> bool:
    """True if ``ancestor`` is reachable from ``ref`` (a commit is its own ancestor too).

    Any non-zero exit — not-an-ancestor, or an error such as an unresolvable
    commit — is treated as False, the safe 'leave it alone' default for callers
    reconciling stranded state.
    """
    res = await git(repo, "merge-base", "--is-ancestor", "--end-of-options", ancestor, ref)
    return res.ok


async def base_has_advanced(repo: str | Path, branch: str, base_ref: str) -> bool:
    """Return True if base_ref has advanced since branch was cut from it."""
    merge_base = await git(repo, "merge-base", "--end-of-options", branch, base_ref)
    if not merge_base.ok:
        return False
    # --verify is REQUIRED here, not cosmetic: plain `rev-parse --end-of-options
    # <ref>` echoes the literal "--end-of-options" as its first output line, so
    # the comparison below could never be equal and this function returned True
    # forever — every job bounced MERGE -> MERGE_VERIFY in an endless loop
    # (job #4308). --verify makes rev-parse emit exactly one object id.
    base_tip = await git(repo, "rev-parse", "--verify", "--end-of-options", base_ref)
    if not base_tip.ok:
        return False
    return merge_base.stdout.strip() != base_tip.stdout.strip()


_MERGE_PLUMBING_RE = re.compile(
    r"not something we can merge|not a valid object name|couldn't find remote ref|does not point to a commit",
    re.IGNORECASE,
)


async def has_unmerged_paths(worktree: str | Path) -> bool:
    res = await git(worktree, "status", "--porcelain")
    return any(line[:2] in ("UU", "AA", "DD") for line in res.stdout.splitlines())


def _is_merge_plumbing_error(stderr: str) -> bool:
    return bool(_MERGE_PLUMBING_RE.search(stderr))


async def merge_base_into_worktree(worktree: str | Path, base_ref: str) -> GitResult:
    """Merge base_ref into worktree.

    On conflict: leave conflict markers in place and return the failed result with
    the list of conflicted filenames appended to ``stderr``.
    """
    res = await git(worktree, "merge", "--no-ff", "--", base_ref)
    if not res.ok:
        conflicted = await git(worktree, "diff", "--name-only", "--diff-filter=U")
        files = conflicted.stdout.strip() if conflicted.ok else ""
        stderr = (res.stderr + ("\n" + files if files else "")).strip()
        return GitResult(ok=False, stdout=res.stdout, stderr=stderr, code=res.code)
    return res


async def commit_all_measured(
    worktree: str | Path,
    message: str,
    timeout: float = 60.0,
) -> "tuple[bool, resources.ResourceRecord]":
    """Like commit_all but wraps git add + commit in measure_subprocess for cgroup stats.

    Returns (committed, ResourceRecord). Falls back to null cgroup fields when
    systemd-run is unavailable (graceful degradation).
    """
    from . import resources  # local import avoids top-level circular concern

    _empty = resources.ResourceRecord(
        cpu_seconds=None,
        peak_rss_bytes=None,
        io_read_bytes=None,
        io_write_bytes=None,
        wall_seconds=0.0,
        sampled_at=resources._now_iso(),
    )
    if not await has_changes(worktree):
        return False, _empty
    await _stage_clean(worktree)
    rc, _, commit_record = await resources.measure_subprocess(
        ["git", "-C", str(worktree), "commit", "-m", message],
        cwd=str(worktree),
        timeout=timeout,
    )
    return rc == 0, commit_record


async def push_branch_measured(
    worktree: str | Path,
    branch: str,
    timeout: float = 120.0,
    *,
    force: bool = False,
) -> "tuple[GitResult, resources.ResourceRecord]":
    """Like push but wraps git push in measure_subprocess for cgroup stats.

    ``force`` uses a plain ``--force`` (not ``--force-with-lease``): the caller is
    overwriting a pipeline-owned ``hyqs/job-N`` branch that only this job writes to,
    and a fresh worktree has no remote-tracking ref for a lease to check against.

    Returns (GitResult, ResourceRecord). Falls back to null cgroup fields when
    systemd-run is unavailable (graceful degradation).
    """
    from . import resources  # local import avoids top-level circular concern

    cmd = ["git", "-C", str(worktree), "push"]
    if force:
        cmd.append("--force")
    cmd += ["origin", branch]
    returncode, output, record = await resources.measure_subprocess(
        cmd, cwd=str(worktree), timeout=timeout
    )
    ok = returncode == 0
    result = GitResult(
        ok=ok,
        stdout=output if ok else "",
        stderr="" if ok else output,
        code=returncode,
    )
    return result, record


async def merge_branch_measured(
    repo: str | Path,
    branch: str,
    timeout: float = 120.0,
) -> "tuple[GitResult, resources.ResourceRecord]":
    """Merge branch into the repo's default branch, measured via measure_subprocess.

    Returns (GitResult, ResourceRecord). Falls back to null cgroup fields when
    systemd-run is unavailable (graceful degradation).
    """
    from . import resources  # local import avoids top-level circular concern

    base = await default_branch(repo)
    co = await git(repo, "checkout", "--end-of-options", base)
    if not co.ok:
        return co, resources.ResourceRecord(
            cpu_seconds=None,
            peak_rss_bytes=None,
            io_read_bytes=None,
            io_write_bytes=None,
            wall_seconds=0.0,
            sampled_at=resources._now_iso(),
        )

    cmd = [
        "git",
        "-C",
        str(repo),
        "merge",
        "--no-ff",
        "-m",
        f"Merge {branch}",
        "--",
        branch,
    ]
    returncode, output, record = await resources.measure_subprocess(
        cmd, cwd=str(repo), timeout=timeout
    )
    ok = returncode == 0
    result = GitResult(
        ok=ok,
        stdout=output if ok else "",
        stderr="" if ok else output,
        code=returncode,
    )
    return result, record


async def push(repo: str | Path, branch: str) -> GitResult:
    return await git(repo, "push", "origin", branch)


async def remove_worktree(repo: str | Path, worktree_path: Path) -> None:
    # Already idempotent: early-return when the path is gone (retry-safe).
    if not Path(worktree_path).exists():
        return
    await git(repo, "worktree", "remove", "--force", str(worktree_path))


async def delete_local_branch(repo: str | Path, branch: str) -> None:
    """Delete a local branch. Best-effort: caller should swallow errors."""
    await git(repo, "branch", "-D", branch)


async def ensure_managed_repo(data_dir: Path, project_id: int, origin_url: str) -> Path:
    """Return a pipeline-owned bare clone for *project_id*, creating or updating it.

    Path: data_dir/repos/<project_id> (bare clone).
    First call clones; subsequent calls run ``git remote update`` to fetch.
    If the directory exists but is not a valid bare repo, it is deleted and re-cloned.
    """
    _validate_github_origin_url(origin_url)
    path = data_dir / "repos" / str(project_id)

    if path.exists():
        res = await git(path, "rev-parse", "--is-bare-repository")
        if res.ok and res.stdout.strip() == "true":
            await git(path, "remote", "update")
            return path
        shutil.rmtree(path)

    path.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
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
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"ensure_managed_repo: clone failed for project {project_id}: "
            f"{err.decode().strip()[:500]}"
        )
    return path


async def gc_orphaned_artifacts(
    config, active_job_ids: set[int], repos_dir: "Path | None" = None
) -> None:
    """Remove worktrees and local/remote branches for jobs no longer active.

    Protected set: any ID in active_job_ids (PENDING or RUNNING). Everything
    else — terminal jobs and IDs absent from the DB entirely — is fair game.
    Tolerates non-zero exit from branch / push commands (remote may be gone).
    """
    worktrees_dir = Path(config.data_dir) / "worktrees"

    # Collect candidate IDs from disk (job-N build worktrees and job-N-plan
    # planner worktrees alike).
    disk_ids: set[int] = set()
    if worktrees_dir.exists():
        for p in worktrees_dir.iterdir():
            m = re.fullmatch(r"job-(\d+)(-plan)?", p.name)
            if m:
                disk_ids.add(int(m.group(1)))

    # Find a stable git context (managed bare clone, active worktree, or configured
    # repo) that won't be removed during this sweep and can be used for branch ops.
    stable_ctx: Path | None = None
    if repos_dir is not None and repos_dir.exists():
        for d in sorted(repos_dir.iterdir()):
            if d.is_dir():
                res = await git(d, "rev-parse", "--is-bare-repository")
                if res.ok and res.stdout.strip() == "true":
                    stable_ctx = d
                    break
    if stable_ctx is None:
        for jid in sorted(active_job_ids):
            p = worktrees_dir / f"job-{jid}"
            if p.exists():
                stable_ctx = p
                break
    if stable_ctx is None:
        rp = getattr(config, "repo_path", None)
        if rp:
            stable_ctx = Path(rp)

    # Collect candidate IDs from local branches (uses stable_ctx or first disk candidate).
    branch_ids: set[int] = set()
    listing_ctx = stable_ctx
    if listing_ctx is None and disk_ids:
        candidate = worktrees_dir / f"job-{min(disk_ids)}"
        if candidate.exists():
            listing_ctx = candidate
    if listing_ctx is not None and listing_ctx.exists():
        res = await git(listing_ctx, "branch", "--list", "hyqs/job-*")
        if res.ok:
            for line in res.stdout.splitlines():
                branch = line.strip().lstrip("* ")
                m = re.fullmatch(r"hyqs/job-(\d+)", branch)
                if m:
                    branch_ids.add(int(m.group(1)))

    for job_id in sorted(disk_ids | branch_ids):
        if job_id in active_job_ids:
            continue

        worktree_path = worktrees_dir / f"job-{job_id}"
        branch = f"hyqs/job-{job_id}"
        removed: list[str] = []

        # Prefer stable_ctx for git ops; fall back to the orphan's own dir (before removal).
        ctx: Path | None = stable_ctx
        if (ctx is None or not ctx.exists()) and worktree_path.exists():
            ctx = worktree_path

        # Branch ops first — while ctx still exists — before the worktree dir is removed.
        if ctx is not None and ctx.exists():
            await git(ctx, "branch", "-D", branch)
            removed.append("local-branch")
            await git(ctx, "push", "origin", "--delete", branch)
            removed.append("remote-branch")

        for wt_path, label in (
            (worktree_path, "worktree"),
            (worktrees_dir / f"job-{job_id}-plan", "plan-worktree"),
        ):
            if not wt_path.exists():
                continue
            use_stable = (
                stable_ctx is not None
                and stable_ctx.exists()
                and stable_ctx.resolve() != wt_path.resolve()
            )
            if use_stable:
                await remove_worktree(stable_ctx, wt_path)  # type: ignore[arg-type]
            else:
                # No separate git context available (ctx IS this worktree); remove via filesystem.
                shutil.rmtree(wt_path, ignore_errors=True)
            removed.append(label)

        if removed:
            log.info("gc: removed %s for job %s", ", ".join(removed), job_id)


async def worktree_snapshot(worktree: str | Path) -> tuple[str, str]:
    """Return (HEAD sha, porcelain status) for the worktree — used to detect mutations."""
    sha_res = await git(worktree, "rev-parse", "HEAD")
    status_res = await git(worktree, "status", "--porcelain=v1", "-z")
    return sha_res.stdout.strip(), status_res.stdout


async def restore_gate_worktree(worktree: str | Path) -> None:
    """Discard any worktree mutations: hard-reset tracked files and remove untracked."""
    await git(worktree, "reset", "--hard", "HEAD")
    await git(worktree, "clean", "-fdq")
