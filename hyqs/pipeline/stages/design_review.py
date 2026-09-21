"""Design/UX review stage: deterministic orphan-class check + diff-scoped AI UX gate.

Runs only when the job's diff touches the frontend (see ``_is_ui_diff``). Backend-only
jobs skip this stage entirely so they are not slowed down. The deterministic check runs
first as a cheap pre-filter, catching the exact bug that motivated this stage: a shipped
className with no matching CSS selector.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .. import agents, gitops, personas, resources
from ..classify import UI_EXTS, classify_diff
from ..collision import normalize_scope_amendment_path
from ..gate_guard import GateIsolationError, run_guarded_gate
from ..models import JobStatus, Stage, _now

if TYPE_CHECKING:
    from ..models import Job
    from ..runner import PipelineRunner

log = logging.getLogger("hyqs.runner")

_CLASS_ATTR_RE = re.compile(r'className=["\']([^"\']+)["\']')
_CSS_SELECTOR_RE = re.compile(r"\.([a-zA-Z_-][\w-]*)\s*[{,:]")
_TAILWIND_CSS_RE = re.compile(r"@tailwind\b|@import\s+[\"']tailwindcss[\"']")
_PATH_METADATA_RE = re.compile(r"[*?\[\]{}(),:;|\"'<>]")

DESIGN_REVIEW_CHECK_ID = "design_review.findings"


def _is_ui_diff(plan_data: dict, changed_files: list[str]) -> bool:
    """True when this job's diff touches the frontend; fail-open (False) on error."""
    try:
        ui_impact = (plan_data or {}).get("ui_impact") or {}
        if ui_impact.get("touches_backend_surface") is True and ui_impact.get("frontend_changes"):
            return True
        return any(Path(f).suffix in UI_EXTS for f in changed_files)
    except Exception:
        return False


def _check_orphan_classes(worktree: Path, diff_text: str) -> list[dict]:
    """Return findings for className references in the diff with no matching CSS selector.

    Fail-closed on real orphan classes, but fail-open (return []) on any tooling error —
    this half must never route a scan failure into the FIX loop.
    """
    try:
        referenced: dict[str, set[str]] = {}
        companion_paths: set[str] = set()
        current_file = ""
        for line in diff_text.splitlines():
            if line.startswith("+++ "):
                current_file = line[4:].strip().removeprefix("b/")
                if Path(current_file).suffix == ".css":
                    companion_paths.add(current_file)
                continue
            if not line.startswith("+") or line.startswith("+++"):
                continue
            if "{" in line:
                continue  # template expression (e.g. className={styles.foo}), not a literal
            if Path(current_file).suffix not in {".jsx", ".tsx", ".vue"}:
                continue
            for match in _CLASS_ATTR_RE.finditer(line):
                for name in match.group(1).split():
                    referenced.setdefault(name, set()).add(current_file)

        if not referenced:
            return []

        defined: set[str] = set()
        for css_path in worktree.rglob("*.css"):
            try:
                text = css_path.read_text(encoding="utf-8")
            except OSError:
                continue
            defined.update(_CSS_SELECTOR_RE.findall(text))

        orphans = sorted(set(referenced) - defined)
        return [
            {
                "severity": "error",
                "note": f"className '{name}' is referenced in the diff but has no "
                "matching CSS selector defined in the worktree.",
                "path": sorted(referenced[name])[0],
                "companion_paths": sorted(companion_paths),
            }
            for name in orphans
        ]
    except Exception as exc:
        log.warning("design_review: orphan-class scan raised unexpected exception: %s", exc)
        return []


def _is_tailwind_project(worktree: Path) -> bool:
    """True if the worktree is a Tailwind project; fail-open (False) on any tooling error.

    Tailwind synthesizes utility classes (``bg-accent/40``, ``md:grid-cols-2``, …) from
    theme tokens at build time — they never appear as literal ``.classname { }`` selectors
    in source CSS, so ``_check_orphan_classes`` must be skipped for these projects.
    """
    try:
        for css_path in worktree.rglob("*.css"):
            if "node_modules" in css_path.parts:
                continue
            try:
                text = css_path.read_text(encoding="utf-8")
            except OSError:
                continue
            if _TAILWIND_CSS_RE.search(text):
                return True

        for pkg_path in worktree.rglob("package.json"):
            if "node_modules" in pkg_path.parts:
                continue
            try:
                text = pkg_path.read_text(encoding="utf-8")
            except OSError:
                continue
            if '"tailwindcss"' in text:
                return True

        return False
    except Exception as exc:
        log.warning("design_review: tailwind detection raised unexpected exception: %s", exc)
        return False


def compose_design_review_failure_detail(
    detail: dict[str, object],
    orphan_findings: list[dict[str, object]],
    reviewer_findings: list[dict[str, object]],
) -> dict[str, object]:
    """Attach path authority from orphan metadata and explicit reviewer paths only."""
    from . import compose_authorized_gate_failure_detail_if_valid

    candidates: list[object] = []
    for finding in orphan_findings:
        candidates.append(finding.get("path"))
        companion_paths = finding.get("companion_paths")
        if isinstance(companion_paths, list):
            candidates.extend(companion_paths)
    candidates.extend(finding.get("path") for finding in reviewer_findings)
    failing_paths = sorted(
        {
            normalized
            for candidate in candidates
            if isinstance(candidate, str)
            if (normalized := normalize_scope_amendment_path(candidate)) is not None
            if not _PATH_METADATA_RE.search(candidate)
            if not candidate.startswith("~")
        }
    )
    return compose_authorized_gate_failure_detail_if_valid(
        detail,
        Stage.DESIGN_REVIEW,
        DESIGN_REVIEW_CHECK_ID,
        DESIGN_REVIEW_CHECK_ID,
        failing_paths,
        ["symbol_paths"],
    )


def _design_sink(rn: "PipelineRunner", job: "Job"):
    def _sink(line: str) -> None:
        rn.store.append_log(job.id, "design_review", line, job.attempts)

    return _sink


async def run(rn: "PipelineRunner", job: "Job") -> None:
    worktree = rn.worktrees / f"job-{job.id}"
    backend = rn._backend(job)
    started = _now()
    managed = await rn._managed_repo(job)
    base = await gitops.default_branch(managed)

    try:
        numstat_entries = await gitops.numstat(worktree, f"{base}...HEAD")
        changed_files = [e["path"] for e in numstat_entries]
    except Exception as exc:
        log.warning("design_review: could not resolve changed files: %s", exc)
        changed_files = []

    if not _is_ui_diff(job.plan or {}, changed_files):
        log.info("design_review: job %s is backend-only; skipping", job.id)
        job.stage = Stage.DESIGN_REVIEW
        job.status = JobStatus.PENDING
        rn.store.save(job)
        return

    try:
        diff_text = await gitops.patch(worktree, f"{base}...HEAD")
    except Exception as exc:
        log.warning("design_review: could not resolve diff text: %s", exc)
        diff_text = ""

    orphan_findings = (
        [] if _is_tailwind_project(worktree) else _check_orphan_classes(worktree, diff_text)
    )

    try:
        persona_checklist = personas.build_checklist(classify_diff(changed_files, diff_text))
    except Exception:
        persona_checklist = ""

    prior_design_review = job.design_review if isinstance(job.design_review, dict) else None
    _design_t0 = time.monotonic()
    try:
        design_data, usage = await run_guarded_gate(
            "design_review",
            agents.design_review,
            str(worktree),
            backend,
            str(worktree),
            base,
            timeout=rn.timeout,
            prior_design_review=prior_design_review,
            persona_checklist=persona_checklist,
            log_sink=_design_sink(rn, job),
            store=rn.store,
            job_id=job.id,
        )
    except GateIsolationError as e:
        return await rn._retry_or_fail(job, str(e))
    rn.store.record_usage("design_review", usage, job.id)
    rn._record_resource(
        job,
        "design_review",
        resources.ResourceRecord(
            cpu_seconds=None,
            peak_rss_bytes=None,
            io_read_bytes=None,
            io_write_bytes=None,
            wall_seconds=time.monotonic() - _design_t0,
            sampled_at=resources._now_iso(),
        ),
    )

    ai_findings = design_data.get("findings", [])
    all_findings = orphan_findings + ai_findings
    design_data = dict(design_data)
    design_data["findings"] = all_findings

    job.design_review = design_data
    ai_verdict = design_data.get("verdict")
    verdict = "pass" if (ai_verdict == "pass" and not orphan_findings) else "fail"

    detail = {"verdict": verdict, "findings": all_findings}
    if verdict != "pass":
        detail = compose_design_review_failure_detail(detail, orphan_findings, ai_findings)
        rn._event(
            job,
            "design_review",
            "failed",
            started,
            summary=design_data.get("summary", ""),
            detail=detail,
            usage=usage,
        )
        findings_text = "\n".join(
            f"- [{f.get('severity', '?')}] {f.get('note', '')}" for f in all_findings
        )
        return await rn._retry_or_fail(
            job,
            f"design review rejected: {design_data.get('summary', '')}\n{findings_text}".strip(),
            failure_detail=detail,
        )

    rn._event(
        job,
        "design_review",
        "done",
        started,
        summary=design_data.get("summary", ""),
        detail=detail,
        usage=usage,
    )
    job.stage = Stage.DESIGN_REVIEW
    job.status = JobStatus.PENDING
    rn.store.save(job)
    await rn.notify(
        job.chat_id,
        f"🎨 Job #{job.id}: design review passed. Merging…",
        project_id=job.project_id,
        job_id=job.id,
    )
    return
