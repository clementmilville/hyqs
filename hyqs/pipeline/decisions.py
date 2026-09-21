"""AUTO-ADR: durable per-job architecture decision records.

On a successful merge, ``record_decision`` writes ONE markdown file per job to
``.hyqs/decisions/<job-id>-<slug>.md`` in the project repo and pushes it to the
base branch — reusing the implementation summary already generated at merge
time (no new AI call here). ``load_digest`` reads those files back into a
compact block the PLANNER and REVIEWER prompts can include, so future jobs see
what past jobs decided instead of re-deriving (or contradicting) it.

Pure functions only touch the filesystem; no store access, no AI calls.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from . import github, gitops, provision

log = logging.getLogger("hyqs.decisions")

_DECISIONS_SUBDIR = Path(".hyqs") / "decisions"
_RETENTION = 200
_MAX_PUSH_ATTEMPTS = 5
_FILENAME_RE = re.compile(r"^(\d+)-")
FILENAME_RE = re.compile(r"^(\d+)(-[a-z0-9-]+)?\.md$")
_JOB_TITLE_PREFIX_RE = re.compile(r"^Job #\d+:\s*")
_GATE_FIX_PREFIX = "[gate-fix]"


def decisions_dir(repo_root: str | Path) -> Path:
    return Path(repo_root) / _DECISIONS_SUBDIR


def decision_filename(job_id: int, title: str) -> str:
    slug = provision.slugify(title, fallback="")[:50]
    return f"{job_id}-{slug}.md" if slug else f"{job_id}.md"


def is_janitorial_job(*, title: str, source_meta: dict | None) -> bool:
    """True for auto-filed janitor jobs (e.g. gate-fix) that shouldn't get a decision record.

    Prefers the structured ``source_meta["kind"] == "gate-fix"`` marker set at filing
    time; falls back to the ``[gate-fix]`` title prefix for jobs filed before that
    marker existed (title may carry a leading ``Job #<id>: `` header, as parsed from
    an on-disk decision file).
    """
    if source_meta and source_meta.get("kind") == "gate-fix":
        return True
    stripped = _JOB_TITLE_PREFIX_RE.sub("", title or "")
    return stripped.startswith(_GATE_FIX_PREFIX)


def render_decision(
    *, job_id: int, title: str, date: str, summary: str, files_touched: list[str]
) -> str:
    files_block = "\n".join(f"- {f}" for f in files_touched) or "(none recorded)"
    return (
        f"# Job #{job_id}: {title}\n\n"
        f"**Date:** {date}\n\n"
        f"{summary.strip()}\n\n"
        f"## Files touched\n{files_block}\n"
    )


def _decision_sort_key(path: Path) -> int:
    m = _FILENAME_RE.match(path.name)
    return int(m.group(1)) if m else 0


def _prune(decisions_dir: Path, retention: int) -> None:
    """Delete the oldest files (by leading job id) beyond ``retention``."""
    files = sorted(decisions_dir.glob("*.md"), key=_decision_sort_key)
    if len(files) <= retention:
        return
    for p in files[: len(files) - retention]:
        p.unlink()


def parse_decision_meta(path: Path) -> dict | None:
    """Extract {filename, job_id, title, date, first_line} from a decision file.

    Returns None if the file cannot be read or its filename doesn't match the
    expected ``<job-id>[-<slug>].md`` pattern.
    """
    m = FILENAME_RE.match(path.name)
    if not m:
        return None
    try:
        text = path.read_text()
    except OSError:
        return None
    title, date, first_line = "", "", ""
    for ln in text.splitlines():
        if not title and ln.startswith("# "):
            title = ln[2:].strip()
        elif not date and ln.startswith("**Date:**"):
            date = ln[len("**Date:**") :].strip()
        elif (
            not first_line
            and ln.strip()
            and not ln.startswith("#")
            and not ln.startswith("**")
            and not ln.startswith("-")
        ):
            first_line = ln.strip()
        if title and date and first_line:
            break
    return {
        "filename": path.name,
        "job_id": int(m.group(1)),
        "title": title,
        "date": date,
        "first_line": first_line,
    }


def list_decisions(repo_root: str | Path) -> list[dict]:
    """All decisions under ``repo_root``, newest-first. [] if none exist."""
    d = decisions_dir(repo_root)
    if not d.is_dir():
        return []
    files = sorted(d.glob("*.md"), key=_decision_sort_key, reverse=True)
    results = []
    for p in files:
        meta = parse_decision_meta(p)
        if meta is not None:
            results.append(meta)
    return results


def resolve_decision_path(repo_root: str | Path, filename: str) -> Path | None:
    """The safe path to ``filename`` within ``repo_root``'s decisions dir, or None.

    Rejects anything that doesn't match ``FILENAME_RE`` or that would resolve
    outside the decisions directory (traversal guard).
    """
    if not FILENAME_RE.match(filename):
        return None
    d = decisions_dir(repo_root).resolve()
    candidate = (d / filename).resolve()
    if candidate.parent != d:
        return None
    return candidate


def filter_by_epic(
    decisions: list[dict], job_epic_map: dict[int, int | None], epic_id: int
) -> list[dict]:
    """Only the entries whose job_id maps to ``epic_id`` in ``job_epic_map``."""
    return [d for d in decisions if job_epic_map.get(d["job_id"]) == epic_id]


def load_digest(repo_root: str | Path, *, limit: int = 15, char_cap: int = 2000) -> str:
    """A compact digest of the newest ``limit`` decision files under ``repo_root``.

    Each entry is one line: date, title, first summary line. Capped at
    ``char_cap`` characters total. Returns "" when no decisions exist.
    """
    d = decisions_dir(repo_root)
    if not d.is_dir():
        return ""
    files = sorted(d.glob("*.md"), key=_decision_sort_key, reverse=True)
    lines: list[str] = []
    for p in files:
        if len(lines) >= limit:
            break
        meta = parse_decision_meta(p)
        if meta is None or is_janitorial_job(title=meta["title"], source_meta=None):
            continue
        lines.append(f"- {meta['date']} — {meta['title']}: {meta['first_line']}")
    if not lines:
        return ""
    digest = "\n".join(lines)
    if len(digest) > char_cap:
        digest = digest[:char_cap].rsplit("\n", 1)[0] + "\n…(truncated)"
    return digest


async def record_decision(
    managed_repo: str | Path,
    base: str,
    worktree_root: str | Path,
    *,
    job_id: int,
    title: str,
    summary: str,
    files_touched: list[str],
    retention: int = _RETENTION,
    max_attempts: int = _MAX_PUSH_ATTEMPTS,
) -> bool:
    """Write one decision file and land it on ``base``. Best-effort: never raises.

    Uses a disposable detached worktree (always cleaned up) so the merged
    checkout is never touched. On a lost race against another job's decision
    commit, refetches the current tip and retries — up to ``max_attempts``.
    """
    decision_worktree = Path(worktree_root) / f"job-{job_id}-decision"
    try:
        is_remote = await github.has_remote(managed_repo)
        for attempt in range(1, max_attempts + 1):
            if decision_worktree.exists():
                await gitops.remove_worktree(managed_repo, decision_worktree)
            if is_remote:
                await gitops.git(managed_repo, "fetch", "origin", base)
                ref = "FETCH_HEAD"
            else:
                ref = base
            res = await gitops.create_detached_worktree(managed_repo, decision_worktree, ref)
            if not res.ok:
                log.warning("job %s: decision worktree create failed: %s", job_id, res.stderr[:200])
                return False
            try:
                ddir = decision_worktree / _DECISIONS_SUBDIR
                ddir.mkdir(parents=True, exist_ok=True)
                content = render_decision(
                    job_id=job_id,
                    title=title,
                    date=datetime.now(timezone.utc).date().isoformat(),
                    summary=summary,
                    files_touched=files_touched,
                )
                (ddir / decision_filename(job_id, title)).write_text(content)
                _prune(ddir, retention)
                if not await gitops.commit_all(
                    decision_worktree, f"docs: record decision for job #{job_id}"
                ):
                    return False
                if is_remote:
                    push_res = await gitops.git(decision_worktree, "push", "origin", f"HEAD:{base}")
                    if push_res.ok:
                        return True
                    log.info("job %s: decision push race on attempt %s; retrying", job_id, attempt)
                    continue
                head = await gitops.git(decision_worktree, "rev-parse", "HEAD")
                merge_res = await gitops.git(
                    managed_repo, "merge", "--ff-only", head.stdout.strip()
                )
                if merge_res.ok:
                    return True
                log.info("job %s: decision ff-merge race on attempt %s; retrying", job_id, attempt)
            finally:
                await gitops.remove_worktree(managed_repo, decision_worktree)
        log.warning("job %s: record_decision exhausted %s attempts", job_id, max_attempts)
        return False
    except Exception:
        log.warning("job %s: record_decision failed (best-effort)", job_id, exc_info=True)
        return False
