"""GitHub-native pipeline operations via the `gh` CLI + `git push`.

Each build job becomes a PR: its branch is pushed, a PR is opened, the review is
posted as a PR comment, and the merge happens through GitHub (squash). This
keeps GitHub the source of truth. Repos without a remote fall back to the local
merge path in the runner, so nothing here is required for offline repos.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

from .gitops import GitResult, git, remove_worktree

log = logging.getLogger("hyqs.github")

_FULL_OID_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")
_MISSING_PR_MESSAGES = ("no pull requests found", "could not resolve to a pullrequest")


class PullRequestState(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    MERGED = "MERGED"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PullRequest:
    """Immutable, fail-closed projection of a branch-associated pull request."""

    state: PullRequestState
    url: str = ""
    number: int | None = None
    base_ref_name: str = ""
    head_ref_name: str = ""
    head_ref_oid: str = ""

    def matches(self, *, base: str, branch: str, head_oid: str) -> bool:
        """True only when this is a complete identity for the expected PR head."""
        return (
            self.state in {PullRequestState.OPEN, PullRequestState.CLOSED, PullRequestState.MERGED}
            and isinstance(self.number, int)
            and not isinstance(self.number, bool)
            and self.number > 0
            and _valid_pr_url(self.url, self.number)
            and self.base_ref_name == base
            and self.head_ref_name == branch
            and _FULL_OID_RE.fullmatch(self.head_ref_oid) is not None
            and self.head_ref_oid.lower() == head_oid.lower()
        )


def _valid_pr_url(value: object, number: int) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
    except ValueError:
        return False
    parts = parsed.path.strip("/").split("/")
    return (
        parsed.scheme == "https"
        and hostname == "github.com"
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
        and len(parts) == 4
        and parts[2] == "pull"
        and parts[3] == str(number)
        and all(parts[:2])
    )


def _unknown_pr() -> PullRequest:
    return PullRequest(state=PullRequestState.UNKNOWN)


async def inspect_branch_pr(
    repo: str | Path, branch: str, *, base: str | None = None
) -> PullRequest:
    """Inspect and strictly validate the PR associated with ``branch``."""
    fields = "url,number,state,baseRefName,headRefName,headRefOid"
    res = await _gh(repo, "pr", "view", branch, "--json", fields)
    if not res.ok:
        if any(message in res.stderr.lower() for message in _MISSING_PR_MESSAGES):
            return PullRequest(state=PullRequestState.MISSING)
        return _unknown_pr()
    try:
        data = json.loads(res.stdout)
    except (json.JSONDecodeError, TypeError):
        return _unknown_pr()
    if not isinstance(data, dict):
        return _unknown_pr()
    try:
        state = PullRequestState(data.get("state"))
    except (TypeError, ValueError):
        return _unknown_pr()
    number = data.get("number")
    base_ref = data.get("baseRefName")
    head_ref = data.get("headRefName")
    head_oid = data.get("headRefOid")
    if (
        state not in {PullRequestState.OPEN, PullRequestState.CLOSED, PullRequestState.MERGED}
        or not isinstance(number, int)
        or isinstance(number, bool)
        or number <= 0
        or not _valid_pr_url(data.get("url"), number)
        or not isinstance(base_ref, str)
        or not base_ref
        or (base is not None and base_ref != base)
        or head_ref != branch
        or not isinstance(head_oid, str)
        or _FULL_OID_RE.fullmatch(head_oid) is None
    ):
        return _unknown_pr()
    return PullRequest(
        state=state,
        url=data["url"],
        number=number,
        base_ref_name=base_ref,
        head_ref_name=head_ref,
        head_ref_oid=head_oid.lower(),
    )


def _pr_selector(pr: PullRequest) -> str:
    if (
        pr.state in {PullRequestState.MISSING, PullRequestState.UNKNOWN}
        or pr.number is None
        or not _valid_pr_url(pr.url, pr.number)
        or not pr.base_ref_name
        or not pr.head_ref_name
        or _FULL_OID_RE.fullmatch(pr.head_ref_oid) is None
    ):
        raise ValueError("pull request identity is not trusted")
    return pr.url


async def reopen_pr(repo: str | Path, pr: PullRequest) -> GitResult:
    """Reopen one verified pull request without resolving it by branch name."""
    selector = _pr_selector(pr)
    if pr.state is not PullRequestState.CLOSED:
        raise ValueError("only a closed pull request can be reopened")
    return await _gh(repo, "pr", "reopen", selector)


async def pr_identity_is_merged(repo: str | Path, pr: PullRequest) -> bool:
    """Query merged status for one verified PR identity, failing closed."""
    selector = _pr_selector(pr)
    if pr.state is PullRequestState.MERGED:
        return True
    res = await _gh(repo, "pr", "view", selector, "--json", "state", "-q", ".state")
    return res.ok and res.stdout.strip() == PullRequestState.MERGED.value


async def merge_pr_squash(repo: str | Path, pr: PullRequest) -> GitResult:
    """Squash-merge one verified PR, leaving GitHub's merge gates intact."""
    selector = _pr_selector(pr)
    if pr.state is PullRequestState.MERGED:
        return GitResult(ok=True, stdout="already merged", stderr="", code=0)
    if pr.state is not PullRequestState.OPEN:
        raise ValueError("only an open pull request can be squash-merged")
    return await _gh(repo, "pr", "merge", selector, "--squash", "--delete-branch")


async def _gh(cwd: str | Path, *args: str) -> GitResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh",
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        log.debug("gh %s -> OSError: %s", args, exc)
        return GitResult(ok=False, stdout="", stderr=str(exc), code=-1)
    out, _ = await proc.communicate()
    text = out.decode(errors="replace").strip()
    ok = proc.returncode == 0
    if not ok:
        log.debug("gh %s -> %s | %s", args, proc.returncode, text[:200])
    return GitResult(
        ok=ok, stdout=text if ok else "", stderr="" if ok else text, code=proc.returncode or 0
    )


async def has_remote(repo: str | Path) -> bool:
    """True if the repo has an 'origin' (or any) git remote we can push to."""
    res = await git(repo, "remote")
    return "origin" in res.stdout.split() if res.ok else False


async def force_push_branch(worktree: str | Path, branch: str) -> GitResult:
    """Overwrite a pipeline-owned job branch after BUILD/FIX updates it.

    Job branches are uniquely derived from immutable job IDs and are never
    user-owned. A lease depends on a local remote-tracking ref that concurrent
    retry cleanup may refresh or delete, producing a false ``stale info``
    rejection after a valid FIX. Plain force is safe for this ownership model
    and matches :func:`gitops.push_branch_measured`.
    """
    return await git(worktree, "push", "--force", "-u", "origin", branch)


async def pr_create(
    worktree: str | Path, branch: str, base: str, title: str, body: str
) -> GitResult:
    return await _gh(
        worktree,
        "pr",
        "create",
        "--head",
        branch,
        "--base",
        base,
        "--title",
        title[:240],
        "--body",
        body,
    )


async def pr_url(worktree: str | Path, branch: str) -> str:
    res = await _gh(worktree, "pr", "view", branch, "--json", "url", "-q", ".url")
    return res.stdout if res.ok else ""


async def pr_comment(worktree: str | Path, branch: str, body: str) -> GitResult:
    return await _gh(worktree, "pr", "comment", branch, "--body", body)


async def pr_merge_squash(repo: str | Path, branch: str) -> GitResult:
    return await _gh(repo, "pr", "merge", branch, "--squash", "--delete-branch")


async def pr_is_merged(repo: str | Path, branch: str) -> bool:
    """True if the GitHub PR for *branch* is already in MERGED state.

    Never raises: any subprocess or parse failure returns False.
    """
    try:
        res = await _gh(repo, "pr", "view", branch, "--json", "state", "-q", ".state")
        return res.stdout.strip() == "MERGED"
    except Exception:
        log.debug("pr_is_merged(%s): unexpected error", branch, exc_info=True)
        return False


async def sync_base(repo: str | Path, base: str) -> GitResult:
    """After a remote merge, fast-forward the local base branch to origin.

    Uses ``git fetch origin <base>:<base>`` which works in both bare and non-bare
    repos without requiring a working-tree checkout.
    """
    return await git(repo, "fetch", "origin", f"{base}:{base}")


async def cleanup_branch_for_retry(job, worktree_path: "str | Path") -> str | None:
    """Best-effort git teardown run BEFORE a FAILED/CANCELLED job is retried.

    Removes the job's worktree, prunes stale worktree registrations, and deletes
    both the local and remote copies of its branch, so the retry attempt starts
    from a clean checkout instead of colliding with the previous run's branch.
    Runs before the job's DB row is reset so there is no partial state if
    cleanup fails.

    Returns an error string only when the remote branch delete fails for a
    reason other than the ref already being gone (auth/connectivity) — any
    other failure is non-fatal (the branch may never have been created) and
    is swallowed. Returns None on success, including when the repo has no
    remote or no repo_path (nothing to clean up).
    """
    if not job.repo_path:
        return None
    branch = job.branch or f"hyqs/job-{job.id}"

    # remove_worktree deregisters the worktree from .git/worktrees AND deletes the
    # directory; shutil.rmtree alone leaves a stale registration that prevents
    # deleting the branch and blocks create_worktree on retry.
    await remove_worktree(job.repo_path, Path(worktree_path))
    # remove_worktree early-returns when the path is already gone, which leaves a
    # stale .git/worktrees/job-N registration that makes git consider the branch
    # "checked out" and refuse branch -D. Prune clears that.
    await git(job.repo_path, "worktree", "prune")

    # Local branch delete is non-fatal — it may never have been created.
    await git(job.repo_path, "branch", "-D", branch)

    # Remote branch delete: skip entirely for local-only repos (no origin).
    # Non-fatal only when the ref never existed on the remote; any other
    # failure (auth, connectivity) would leave a stale branch that causes the
    # retry worker to fail on push, so we surface it.
    if await has_remote(job.repo_path):
        res = await git(job.repo_path, "push", "origin", "--delete", branch)
        if not res.ok and "remote ref does not exist" not in res.stderr:
            return f"could not delete remote branch {branch}: {res.stderr}"
    return None


async def cleanup_job_artifacts(config, job, managed_repo: "Path | None" = None) -> list[str]:
    """Best-effort teardown of a job's git/GitHub artifacts when it is archived.

    Closes the job's PR, deletes the remote and local branch, and removes its
    worktree — so retiring a failed job doesn't leave an open PR and dangling
    branches behind. Never raises: each step is independent and logged, and the
    underlying ``git``/``gh`` helpers already swallow non-zero exits, so archiving
    still succeeds when the repo has no remote, the branch is already gone, or the
    PR was never opened. Returns the list of artifacts actually removed.

    Pass ``managed_repo`` to route git operations against the pipeline-owned bare
    clone rather than ``job.repo_path``. When omitted, the function auto-discovers
    the managed clone under ``config.data_dir/repos/<project_id>`` if it exists,
    falling back to ``job.repo_path``.
    """
    if managed_repo is None and job.project_id is not None:
        candidate = Path(config.data_dir) / "repos" / str(job.project_id)
        if candidate.exists():
            managed_repo = candidate
    repo = managed_repo if managed_repo is not None else Path(job.repo_path)
    branch = job.branch or f"hyqs/job-{job.id}"
    worktree = Path(config.data_dir) / "worktrees" / f"job-{job.id}"
    cleaned: list[str] = []

    # Close the PR — this also deletes the remote branch when a PR exists.
    if (await _gh(repo, "pr", "close", branch, "--delete-branch")).ok:
        cleaned.append("pr-closed")

    # Ensure the remote branch is gone even when there was no PR to close.
    if (await git(repo, "push", "origin", "--delete", branch)).ok:
        cleaned.append("remote-branch")

    # Remove the worktree before deleting the local branch: git refuses to delete
    # a branch that is still checked out in a worktree.
    if worktree.exists():
        try:
            await remove_worktree(repo, worktree)
            cleaned.append("worktree")
        except Exception:
            log.debug("cleanup: remove_worktree failed for job %s", job.id, exc_info=True)

    if (await git(repo, "branch", "-D", branch)).ok:
        cleaned.append("local-branch")

    if cleaned:
        log.info("archive-cleanup: job %s -> %s", job.id, ", ".join(cleaned))
    return cleaned
