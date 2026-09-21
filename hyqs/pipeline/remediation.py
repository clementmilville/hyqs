"""Pure formatting helpers for remediation state — no store/DB access.

Used by the web UI and janitor logs to render what
:meth:`hyqs.pipeline.store.JobStore.get_active_remediation` reports.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .collision import (
    PlanningCandidate,
    check_manifest_conflict,
    extract_scope_from_idea,
    job_declared_scope_paths,
)

_PATH_REFERENCE_RE = re.compile(r"(?<![\w/])([\w.-]+(?:/[\w.()\-[\]]+)+\.[A-Za-z0-9]+)")


def extract_path_references(value: object) -> list[str]:
    """Extract path-shaped references from structured failure/review evidence."""
    if isinstance(value, dict):
        text = "\n".join(str(item) for item in value.values())
    elif isinstance(value, (list, tuple)):
        text = "\n".join(str(item) for item in value)
    else:
        text = str(value or "")
    return list(dict.fromkeys(_PATH_REFERENCE_RE.findall(text)))


def collect_remediation_candidate_evidence(
    *,
    parent_idea: str,
    parent_plan: dict | None,
    covers_stories: list[str] | None,
    failure_text: str,
    failure_detail: dict | None = None,
    reviews: list[dict | None] | None = None,
    events: list[dict] | None = None,
) -> dict[str, list[str]]:
    """Assemble deterministic path evidence; analyst prose is intentionally absent."""
    covered = set(covers_stories or [])
    covered_paths = [
        path
        for story in (parent_plan or {}).get("stories") or []
        if story.get("id") in covered
        for path in story.get("target_files") or []
    ]
    reviewer_paths = [path for review in reviews or [] for path in extract_path_references(review)]
    rejected_paths: list[str] = []
    for event in events or []:
        detail = event.get("detail") or {}
        if any(key in detail for key in ("unexpected_paths", "rejected_paths", "changed_files")):
            for key in ("unexpected_paths", "rejected_paths", "changed_files"):
                rejected_paths.extend(extract_path_references(detail.get(key)))
    failure_paths = extract_path_references(failure_text)
    traceback_paths = [
        path
        for path in failure_paths + extract_path_references(failure_detail)
        if "test" in Path(path).name or ".py" in path
    ]
    return {
        "parent_manifest": job_declared_scope_paths(parent_idea, parent_plan),
        "covered_story": covered_paths,
        "failure_path": failure_paths,
        "traceback_path": traceback_paths,
        "reviewer_path": reviewer_paths,
        "rejected_diff": rejected_paths,
    }


def format_candidate_map(candidates: list[PlanningCandidate]) -> str:
    lines = ["### Focused candidate files (evidence, not authorization)"]
    lines.extend(
        f"- {candidate.path} [{candidate.status}] provenance={','.join(candidate.reasons)}"
        for candidate in candidates
    )
    return "\n".join(lines)


def format_remediation_badge(remediation_job_id: int | None) -> str:
    if remediation_job_id is None:
        return "No active remediation"
    return f"Remediation in progress (job #{remediation_job_id})"


def format_conflict_reason(incident_job_id: int, remediation_job_id: int) -> str:
    return (
        f"Skipped filing a new remediation for job #{incident_job_id}: "
        f"job #{remediation_job_id} is already in flight for this incident."
    )


def build_ai_fix_idea(
    job_id: int,
    stage: str,
    failure_text: str,
    analyst_idea: str,
    stories: list[dict] | None,
    covers_stories: list[str] | None,
    candidates: list[PlanningCandidate] | None = None,
) -> str:
    """Compose an ai-fix job idea that embeds the failed job's own plan-story text.

    The first line is a short "[ai-fix] <label>" title derived only from
    analyst_idea's own first line, so a multi-line analyst idea can't bleed
    into (and truncate) the job title. Always names the failed job and
    carries the analyst's idea verbatim under "Analyst notes:"; when the
    analyst names which plan stories the fix covers, their exact
    task/acceptance text is appended so the fix job inherits the original
    acceptance criteria rather than a paraphrase.
    """
    label = analyst_idea.split("\n", 1)[0].strip()
    sections = [f"[ai-fix] {label}"]

    preamble = f"Source job #{job_id} failed at stage '{stage}'."
    if failure_text:
        preamble += f"\nFailure: {failure_text[:500]}"
    sections.append(preamble)

    covered_ids = set(covers_stories or [])
    for story in stories or []:
        if story.get("id") not in covered_ids:
            continue
        sections.append(
            f"### {story['id']}: {story.get('title', '')}\n"
            f"Task: {story.get('task', '')}\n"
            f"Acceptance: {story.get('acceptance', '')}"
        )

    if candidates:
        sections.append(format_candidate_map(candidates))
    sections.append(f"Analyst notes:\n{analyst_idea}")

    return "\n\n".join(sections)


def generate_alembic_merge_revision(heads: list[str]) -> tuple[str, str]:
    """Build a deterministic alembic merge-revision file for ``heads``.

    The revision id is a sha1 of the *sorted* heads, so the same head set
    always produces the same (filename, content) regardless of input order —
    letting the caller dedup/retry without generating a new revision each time.
    Content stays parseable by ``collision._parse_alembic_revision`` (plain
    literal ``revision``/``down_revision`` assignments, no f-strings).
    """
    sorted_heads = sorted(heads)
    digest = hashlib.sha1("".join(sorted_heads).encode()).hexdigest()[:12]
    filename = f"{digest}_merge_heads.py"
    down_revision = tuple(sorted_heads)
    content = (
        '"""merge heads\n\n'
        f"Revision ID: {digest}\n"
        f"Revises: {', '.join(sorted_heads)}\n"
        "Create Date: auto-generated by hyqs supervisor\n"
        '"""\n\n'
        "from alembic import op\n"
        "import sqlalchemy as sa\n\n\n"
        f'revision = "{digest}"\n'
        f"down_revision = {down_revision!r}\n"
        "branch_labels = None\n"
        "depends_on = None\n\n\n"
        "def upgrade():\n"
        "    pass\n\n\n"
        "def downgrade():\n"
        "    pass\n"
    )
    return filename, content


def compute_chain_dependencies(entries: list[dict]) -> list[list[int]]:
    """Re-derive a file_fix_jobs chain's dependency edges from each entry's own
    target files, on top of whatever the analyst already declared.

    For each entry, its target files are parsed from its idea text via
    :func:`extract_scope_from_idea`. Any *earlier* entry (index j < i) whose
    files overlap entry i's files (per :func:`check_manifest_conflict`) is
    added to entry i's dependency set — serializing siblings that would
    otherwise race to land on the same files. Only backward edges (j < i) are
    ever added, so the result can never contain a cycle regardless of how many
    entries overlap. Each entry's own declared ``depends_on`` is preserved.
    Returns one sorted, deduped list per input entry, index-aligned.
    """
    files_by_index = [extract_scope_from_idea(entry.get("idea", "")) for entry in entries]
    result: list[list[int]] = []
    for i, entry in enumerate(entries):
        deps = set(entry.get("depends_on") or [])
        for j in range(i):
            if check_manifest_conflict(files_by_index[i], files_by_index[j]):
                deps.add(j)
        result.append(sorted(deps))
    return result
