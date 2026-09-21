"""Plan stage: run the planner, refresh the symbol index, validate the reuse contract.

Stage handler for QUEUED → PLAN. Extracted from PipelineRunner._advance; logic unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING

from .. import agents, collision, contracts, decisions, gitops, resources
from ..models import JobStatus, Stage, _now

if TYPE_CHECKING:
    from ..models import Job
    from ..runner import PipelineRunner

log = logging.getLogger("hyqs.runner")

_MAX_PLAN_STORIES = 5
_MAX_PLAN_TARGET_FILES = 8


def _group_by_shared_files(story_files: dict[str, set[str]]) -> list[set[str]]:
    """Union stories that share at least one file; a story with no files is its own group."""
    groups: list[set[str]] = []
    for sid, files in story_files.items():
        merged = False
        for group in groups:
            group_files: set[str] = set()
            for other_sid in group:
                group_files |= story_files.get(other_sid, set())
            if files and group_files & files:
                group.add(sid)
                merged = True
                break
        if not merged:
            groups.append({sid})
    return groups


def _group_by_shared_files_or_deps(
    story_files: dict[str, set[str]], story_deps: dict[str, set[str]]
) -> list[set[str]]:
    """Union stories that share a target_file OR declare each other via depends_on.

    Extends _group_by_shared_files with the declared-dependency edges: a story that
    depends_on another story is pulled into that story's connected component even if
    they share no files (e.g. a file-disjoint test story that exercises several
    earlier stories). Implemented as union-find over both edge types.
    """
    parent: dict[str, str] = {sid: sid for sid in story_files}

    def find(sid: str) -> str:
        while parent[sid] != sid:
            parent[sid] = parent[parent[sid]]
            sid = parent[sid]
        return sid

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    ids = list(story_files)
    for i, sid in enumerate(ids):
        for other in ids[i + 1 :]:
            shares_file = bool(story_files[sid] and story_files[sid] & story_files[other])
            declares_dep = other in story_deps.get(sid, set()) or sid in story_deps.get(
                other, set()
            )
            if shares_file or declares_dep:
                union(sid, other)

    components: dict[str, set[str]] = {}
    for sid in ids:
        components.setdefault(find(sid), set()).add(sid)
    return list(components.values())


def _topo_order(group: set[str], story_deps: dict[str, set[str]]) -> list[str]:
    """Stable topological sort of ``group`` by declared depends_on edges.

    A story is ordered after every declared dependency that is also in this group.
    Ties (including stories with no declared deps) break by ascending story-id,
    which reproduces today's plain-sorted-id ordering when no depends_on is present.
    Cycle-safe: if the declared edges within this group contain a cycle, falls back
    to sorted-id order for the whole group rather than raising or looping.
    """
    remaining = set(group)
    deps = {sid: (story_deps.get(sid, set()) & group) for sid in group}
    ordered: list[str] = []
    while remaining:
        ready = sorted(sid for sid in remaining if deps[sid] <= set(ordered))
        if not ready:
            return sorted(group)  # cycle among declared deps: fall back to sorted-id order
        for sid in ready:
            ordered.append(sid)
            remaining.discard(sid)
    return ordered


def _split_dag_predecessors(
    group: set[str],
    story_order: list[str],
    story_files: dict[str, set[str]],
    story_deps: dict[str, set[str]],
) -> dict[str, set[str]]:
    """Direct predecessor edges within one connected component (no transitive closure).

    Two stories in ``group`` get a direct edge only when they share a target_file
    (direction tie-broken by ``story_order`` — the earlier story becomes predecessor
    of the later one) or when one declares depends_on the other (the declared
    direction wins over the file tie-break for that specific pair). A story with two
    file-disjoint, dependency-free successors (a fork) leaves those successors
    edge-free between themselves; a story with two independent predecessors that both
    share a file with it (a join) records both as predecessors.
    """
    ordered = [sid for sid in story_order if sid in group]
    predecessors: dict[str, set[str]] = {sid: set() for sid in group}
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            a_depends_on_b = b in story_deps.get(a, set())
            b_depends_on_a = a in story_deps.get(b, set())
            if a_depends_on_b:
                predecessors[a].add(b)
            if b_depends_on_a:
                predecessors[b].add(a)
            if not a_depends_on_b and not b_depends_on_a:
                if story_files.get(a) and story_files[a] & story_files.get(b, set()):
                    predecessors[b].add(a)
    return predecessors


def _topo_order_multi(group: set[str], predecessors: dict[str, set[str]]) -> list[str] | None:
    """Kahn's-algorithm order over ``predecessors`` (a story may have several).

    Ties break by ascending story-id. Returns None if the edges contain a cycle,
    leaving cycle handling to the caller (mirrors ``_dependency_topo_order``'s
    None-on-cycle contract in store.py).
    """
    remaining = set(group)
    ordered: list[str] = []
    ordered_set: set[str] = set()
    while remaining:
        ready = sorted(sid for sid in remaining if predecessors[sid] <= ordered_set)
        if not ready:
            return None
        for sid in ready:
            ordered.append(sid)
            ordered_set.add(sid)
            remaining.discard(sid)
    return ordered


_MIGRATION_PATH_RES = (
    re.compile(r"(^|/)(alembic|migrations)/versions/.+\.py$"),
    re.compile(r"(^|/)versions/[0-9]{3,4}_.+\.py$"),
)


def _plan_touches_migration(plan_data: dict) -> bool:
    """True if any story's or the plan-level target_files include an Alembic migration.

    A migration is an atomic schema change coupled to the model/code that uses it —
    it cannot be safely decomposed into separate jobs by _compute_split_chains, since
    the model, the migration, and its callers share no target_file with each other.
    """
    paths: list[str] = list(plan_data.get("target_files") or [])
    for story in plan_data.get("stories", []):
        paths.extend(story.get("target_files") or [])
    return any(pattern.search(path) for path in paths for pattern in _MIGRATION_PATH_RES)


def _has_per_story_files(stories: list[dict]) -> bool:
    """True if the planner itemized target_files for every story.

    Single source of truth for whether a per-story split is even possible — used by
    both _scope_gate_message (to decide what to render) and _compute_split_chains
    (to decide whether a deterministic split is safe).
    """
    return bool(stories) and all(story.get("target_files") for story in stories)


def _scope_gate_message(plan_data: dict) -> str:
    """Render a scope-gate failure explaining why the plan is too big and how to split it.

    Lists every story's id/title (and its target_files, when the planner itemized
    them per-story) plus the plan-wide target_files list, then suggests a concrete
    split: group stories that share no target_files into independent jobs, and
    stories that do share target_files into a depends_on chain (mirrors the
    guidance already in create_job's docstring).
    """
    stories = plan_data.get("stories", [])
    target_files = list(plan_data.get("target_files") or [])
    lines = [
        f"plan is too large to build safely: {len(stories)} stories, "
        f"{len(target_files)} target_files "
        f"(limits: {_MAX_PLAN_STORIES} stories, {_MAX_PLAN_TARGET_FILES} target_files).",
        "",
        "Stories in this plan:",
    ]
    has_per_story_files = _has_per_story_files(stories)
    story_files: dict[str, set[str]] = {}
    for story in stories:
        sid = story.get("id", "?")
        title = story.get("title", "")
        files = list(story.get("target_files") or [])
        story_files[sid] = set(files)
        files_str = ", ".join(files) if files else "(not itemized per-story)"
        lines.append(f"  - {sid}: {title} — target_files: {files_str}")

    if target_files:
        lines.append("")
        lines.append(f"Plan-wide target_files ({len(target_files)}): {', '.join(target_files)}")

    lines.append("")
    lines.append(
        "Suggested split: prefer several small, focused jobs over one large job — "
        "group stories that share no target_files into independent jobs, and use "
        "depends_on to chain stories that do share target_files so they run "
        "sequentially instead of contending."
    )
    if has_per_story_files:
        groups = _group_by_shared_files(story_files)
        independent = [sorted(g)[0] for g in groups if len(g) == 1]
        chained = [sorted(g) for g in groups if len(g) > 1]
        if independent:
            lines.append(f"  - Independent jobs (no shared target_files): {', '.join(independent)}")
        for chain in chained:
            lines.append(f"  - Chain via depends_on (share target_files): {' -> '.join(chain)}")
    else:
        lines.append(
            "  - The planner did not itemize target_files per story; compare each "
            "story's task against the plan-wide target_files list above to decide "
            "the grouping."
        )
    lines.append(
        "If this size is intentional, retry the job with force=true to bypass this "
        "check (sets source_meta.scope_gate_bypass)."
    )
    return "\n".join(lines)


def _build_reask_context(plan_data: dict) -> str:
    """Render the one-shot re-ask prompt section for an oversized, unsplittable plan.

    Names the story blocking a deterministic split and requires dependency-linked
    decomposition which preserves every declared file.
    """
    stories = plan_data.get("stories", [])
    target_files = list(plan_data.get("target_files") or [])
    lines = [
        f"Your previous plan was rejected: {len(stories)} stories, "
        f"{len(target_files)} target_files "
        f"(limits: {_MAX_PLAN_STORIES} stories, {_MAX_PLAN_TARGET_FILES} target_files), "
        "and it could not be split into independent jobs because the following "
        "story(ies) block a deterministic split:",
        "",
    ]
    blocking = []
    for story in stories:
        sid = story.get("id", "?")
        files = list(story.get("target_files") or [])
        if not files:
            blocking.append((sid, story.get("title", ""), 0, "no target_files were itemized"))
        elif len(files) > _MAX_PLAN_TARGET_FILES:
            blocking.append(
                (
                    sid,
                    story.get("title", ""),
                    len(files),
                    f"{len(files)} target_files exceeds the per-story limit of "
                    f"{_MAX_PLAN_TARGET_FILES}",
                )
            )
    for sid, title, _count, reason in blocking:
        lines.append(f"  - {sid}: {title} — {reason}")
    lines.append("")
    lines.append(
        "Return a dependency-linked revision which preserves every target file. "
        "Split blocking work into smaller stories, use depends_on for required "
        "ordering, itemize every story's target_files, and keep each story at or "
        f"below {_MAX_PLAN_TARGET_FILES} files. Do not shrink or drop scope."
    )
    return "\n".join(lines)


def _completeness_reask_context(result: collision.ScopeCompletenessResult) -> str:
    """Compact, non-sensitive correction evidence for the single PLAN re-ask."""
    lines = [
        "Your previous plan failed deterministic scope-completeness validation.",
        f"Reason codes: {', '.join(result.reason_codes)}",
    ]
    if result.missing_paths:
        lines.append(f"Missing/invalid paths: {', '.join(result.missing_paths)}")
    if result.candidate_paths:
        lines.append(f"Repository candidates: {', '.join(result.candidate_paths)}")
    lines.append(
        "Return a complete revision: preserve required work, itemize canonical "
        "repo-relative paths per story and file_impact, and make target_files exactly "
        "their union. Mark a nonexistent path new only in its matching file_impact."
    )
    return "\n".join(lines)


def _planning_metadata(
    result: collision.ScopeCompletenessResult,
    *,
    candidate_count: int,
    correction_count: int,
    usage,
) -> dict:
    """Return additive, numeric/non-sensitive planning observations."""
    metadata = {
        "candidate_count": candidate_count,
        "final_manifest_count": len(result.manifest),
        "correction_count": correction_count,
        "reason_codes": list(result.reason_codes),
        "provenance": list(result.provenance),
    }
    for name in (
        "input_tokens",
        "output_tokens",
        "cache_creation_tokens",
        "cache_read_tokens",
    ):
        value = getattr(usage, name, None)
        if isinstance(value, int):
            metadata[name] = value
    total = getattr(usage, "total_tokens", None)
    if isinstance(total, int):
        metadata["total_tokens"] = total
    return metadata


def _compute_split_chains(plan_data: dict) -> list[dict] | None:
    """Split an oversized plan into a minimal per-component dependency DAG, one job per story.

    Mirrors the grouping ``_scope_gate_message`` already recommends in prose
    (``_group_by_shared_files_or_deps``, unchanged) but, within each connected
    component, no longer collapses every story into one serial chain. Direct
    predecessor edges are computed per-pair by ``_split_dag_predecessors``: two
    stories are ordered against each other only when they share a target_file or one
    declares depends_on the other. This lets independent branches inside one
    component (e.g. two file-disjoint successors of a shared setup story) become
    concurrently claimable instead of forced into a single chain. A declared
    depends_on cycle within a component falls back to a deterministic sorted-id
    linear chain for that component only, matching the previous ``_topo_order``
    safety net. Returns None — meaning no safe deterministic split exists, caller
    should fall back to failing with the human-readable message — when the planner
    didn't itemize target_files per story, or when any single story is still
    oversized on its own (a component maps every story to its own job, so that story
    would just hit the same gate again).

    Each returned component is ``{"stories": [...], "predecessors": {...}, "terminal_ids": [...]}``:
    ``stories`` is the topological creation order (predecessors before dependents);
    ``predecessors`` maps a story id to the sorted list of its immediate predecessor
    story ids within this component; ``terminal_ids`` lists the story ids no other
    story in the component lists as a predecessor (the component's sink/last-to-finish
    stories).
    """
    stories = plan_data.get("stories", [])
    if not _has_per_story_files(stories):
        return None
    story_by_id = {story.get("id", "?"): story for story in stories}
    story_order = [story.get("id", "?") for story in stories]
    story_files: dict[str, set[str]] = {
        story.get("id", "?"): set(story.get("target_files") or []) for story in stories
    }
    story_deps: dict[str, set[str]] = {
        story.get("id", "?"): {dep for dep in (story.get("depends_on") or []) if dep in story_by_id}
        for story in stories
    }
    groups = _group_by_shared_files_or_deps(story_files, story_deps)
    components: list[dict] = []
    for group in groups:
        predecessors = _split_dag_predecessors(group, story_order, story_files, story_deps)
        order = _topo_order_multi(group, predecessors)
        if order is None:
            # Declared depends_on cycle: fall back to a deterministic linear chain,
            # mirroring the previous _topo_order cycle safety net.
            order = sorted(group)
            predecessors = {
                sid: ({order[i - 1]} if i > 0 else set()) for i, sid in enumerate(order)
            }
        referenced = {p for preds in predecessors.values() for p in preds}
        terminal_ids = [sid for sid in order if sid not in referenced]
        chain_stories = [story_by_id[sid] for sid in order]
        for story in chain_stories:
            if len(story.get("target_files") or []) > _MAX_PLAN_TARGET_FILES:
                return None
        components.append(
            {
                "stories": chain_stories,
                "predecessors": {sid: sorted(predecessors[sid]) for sid in order},
                "terminal_ids": terminal_ids,
            }
        )
    return components


async def _supersede_with_split(
    rn: "PipelineRunner",
    job: "Job",
    started: str,
    plan_data: dict,
    components: list[dict],
    planning_metadata: dict | None = None,
) -> None:
    """Create one job per story (DAG-ordered via depends_on within each component) and
    cancel the original.

    The split jobs ARE the original's work, so there is nothing left for the original
    to do once they land (unlike the AI incident analyst's file_fix_jobs chains, which
    unblock a distinct prerequisite). Each story's job depends_on the already-created
    job ids of ALL of its immediate predecessors within its component (per
    ``_compute_split_chains``'s DAG, not just the previous story in a flat chain).
    """
    created: list["Job"] = []
    terminal_job_ids: list[int] = []
    for component in components:
        job_id_by_story: dict[str, int] = {}
        for story in component["stories"]:
            sid = story.get("id", "?")
            target_files = list(story.get("target_files") or [])
            idea = (
                f"{story.get('title', '')}\n\n"
                f"Task: {story.get('task', '')}\n"
                f"Acceptance: {story.get('acceptance', '')}\n"
                f"Target files: {', '.join(target_files)}"
            )
            pred_ids = [job_id_by_story[p] for p in component["predecessors"].get(sid, [])]
            new_job = rn.store.create(
                idea=idea,
                repo_path=job.repo_path,
                chat_id=job.chat_id,
                epic_id=job.epic_id,
                source=job.source,
                source_actor=job.source_actor,
                source_meta={
                    "split_from": job.id,
                    "story_id": sid,
                    "planning": dict(planning_metadata or {}),
                },
                depends_on=pred_ids or None,
            )
            job_id_by_story[sid] = new_job.id
            created.append(new_job)
        terminal_job_ids.extend(job_id_by_story[sid] for sid in component["terminal_ids"])

    ids = [j.id for j in created]
    repointed = rn.store.repoint_split_dependents(job.id, ids)
    for dep_id in repointed:
        rn.store.add_event(
            dep_id,
            "plan",
            "info",
            summary=f"dependency #{job.id} re-pointed to split children {ids}",
            detail={"repointed_from": job.id, "repointed_to": ids},
        )
    terminal_str = ", ".join(f"#{i}" for i in terminal_job_ids)
    rn._event(
        job,
        "plan",
        "cancelled",
        started,
        summary=f"plan split into {len(created)} job(s)",
        detail={
            "superseded_by": ids,
            "terminal_job_ids": terminal_job_ids,
            "planning": dict(planning_metadata or {}),
        },
    )
    job.status = JobStatus.CANCELLED
    job.resolution = "superseded-by-split"
    job.error = f"superseded by jobs #{ids[0]}..#{ids[-1]} (terminal: {terminal_str})"
    rn.store.save(job)
    await rn.notify(
        job.chat_id,
        f"✂️ Job #{job.id}: plan too large; split into {len(created)} job(s) "
        f"(terminal: {terminal_str}).",
        project_id=job.project_id,
        job_id=job.id,
    )


async def run(rn: "PipelineRunner", job: "Job") -> None:
    backend = rn._backend(job)
    started = _now()
    if not await gitops.is_git_repo(job.repo_path):
        rn._event(
            job,
            "plan",
            "failed",
            started,
            summary=f"{job.repo_path} is not a git repository",
        )
        return await rn._fail(job, f"{job.repo_path} is not a git repository")
    symbol_context = ""
    symbol_index: dict[str, list[dict]] = {}
    if job.project_id is not None:
        try:
            await asyncio.to_thread(rn.store.refresh_symbol_index, job.project_id, job.repo_path)
            symbol_context = rn.store.get_symbol_index_text(job.project_id)
            symbol_index = rn.store.get_symbol_index_names(job.project_id)
        except Exception:
            log.warning("symbol index refresh failed for job %s; continuing", job.id, exc_info=True)
    # Isolate the planner in its own detached worktree: the planner has Bash, and
    # running it in the shared checkout let stray writes (test dbs, typo'd files)
    # dirty the checkout — which the build-stage isolation guard then blamed on
    # concurrent builds (three false-positive isolation_leak failures). Fall back
    # to the shared checkout only if the worktree can't be created.
    managed = await rn._managed_repo(job)
    plan_worktree = rn.worktrees / f"job-{job.id}-plan"
    plan_cwd = job.repo_path
    plan_wt_created = False
    try:
        if plan_worktree.exists():
            await gitops.remove_worktree(managed, plan_worktree)
        base = await gitops.default_branch(managed)
        synchronized_base = await gitops.fresh_base(managed, base)
        res = await gitops.create_detached_worktree(managed, plan_worktree, ref=synchronized_base)
        if res.ok:
            plan_cwd = str(plan_worktree)
            plan_wt_created = True
        else:
            log.warning(
                "plan worktree create failed for job %s (%s); planning in shared checkout",
                job.id,
                res.stderr[:200],
            )
    except Exception:
        log.warning("plan worktree setup failed for job %s; continuing", job.id, exc_info=True)

    try:
        # The authoritative file map: hand the planner the exact set of tracked files
        # so it reads what exists instead of hypothesising paths and thrashing on the
        # misses (the planner doom-loop that wedged the fleet).
        file_manifest = ""
        files: list[str] = []
        try:
            files = await gitops.tracked_files(plan_cwd)
            if files:
                file_manifest = "\n".join(files)
        except Exception:
            log.warning("tracked_files failed for job %s; continuing", job.id, exc_info=True)

        candidates = collision.build_planning_candidates(files, symbol_index, job.idea)
        focused_context = "\n".join(
            f"{index}. {item.path} [{item.status}] provenance={','.join(item.reasons)}"
            for index, item in enumerate(candidates, 1)
        )
        if focused_context:
            file_manifest = (
                "## Focused planning candidates (ranked evidence, not an "
                "authoritative scope restriction)\n"
                f"{focused_context}\n\n"
                "## Complete tracked repository fallback\n"
                f"{file_manifest}"
            )

        decision_digest = ""
        try:
            decision_digest = decisions.load_digest(plan_cwd)
        except Exception:
            log.warning("decision digest load failed for job %s; continuing", job.id, exc_info=True)

        await rn.notify(
            job.chat_id,
            f"🧭 Job #{job.id}: planning…",
            project_id=job.project_id,
            job_id=job.id,
        )

        def _plan_sink(line):
            rn.store.append_log(job.id, "plan", line, job.attempts)

        _plan_t0 = time.monotonic()
        plan_data, usage = await agents.plan(
            backend,
            job.idea,
            plan_cwd,
            timeout=rn.timeout,
            symbol_context=symbol_context,
            file_manifest=file_manifest,
            decision_digest=decision_digest,
            log_sink=_plan_sink,
            store=rn.store,
            job_id=job.id,
        )
        completeness = collision.validate_plan_scope_completeness(plan_data, files, candidates)
        observed_reason_codes = set(completeness.reason_codes)
        if not completeness.passed and job.plan_reask_attempts < 1:
            job.plan_reask_attempts += 1
            rn.store.spend_plan_correction(job.id)
            rn.store.merge_planning_metadata(
                job.id,
                {
                    "candidate_count": len(candidates),
                    "correction_count": job.plan_reask_attempts,
                    "reason_codes": list(completeness.reason_codes),
                    "provenance": list(completeness.provenance),
                },
            )
            rn.store.add_event(
                job.id,
                "plan",
                "correction",
                summary="scope completeness correction requested",
                detail=completeness.to_dict(),
            )
            plan_data, correction_usage = await agents.plan(
                backend,
                job.idea,
                plan_cwd,
                timeout=rn.timeout,
                symbol_context=symbol_context,
                file_manifest=file_manifest,
                decision_digest=decision_digest,
                reask_context=_completeness_reask_context(completeness),
                log_sink=_plan_sink,
                store=rn.store,
                job_id=job.id,
            )
            usage = usage + correction_usage
            completeness = collision.validate_plan_scope_completeness(plan_data, files, candidates)
            observed_reason_codes.update(completeness.reason_codes)

        # Downstream splitting and persistence consume precisely the validated
        # canonical plan-wide manifest.
        plan_data["target_files"] = list(completeness.manifest)
        n_stories = len(plan_data.get("stories", []))
        n_target_files = len(plan_data.get("target_files") or [])
        oversized = (
            n_stories > _MAX_PLAN_STORIES or n_target_files > _MAX_PLAN_TARGET_FILES
        ) and not _plan_touches_migration(plan_data)
        bypassed = bool((job.source_meta or {}).get("scope_gate_bypass"))
        chains: list[dict] | None = None
        if oversized and not bypassed:
            chains = _compute_split_chains(plan_data)
            # Bounded one-shot re-ask: only when no safe deterministic split exists
            # and this job hasn't already spent its re-ask budget. Runs against the
            # same live plan worktree, still inside this try/finally.
            if chains is None and job.plan_reask_attempts < 1:
                job.plan_reask_attempts += 1
                rn.store.spend_plan_correction(job.id)
                reask_context = _build_reask_context(plan_data)
                reask_plan_data, reask_usage = await agents.plan(
                    backend,
                    job.idea,
                    plan_cwd,
                    timeout=rn.timeout,
                    symbol_context=symbol_context,
                    file_manifest=file_manifest,
                    decision_digest=decision_digest,
                    reask_context=reask_context,
                    log_sink=_plan_sink,
                    store=rn.store,
                    job_id=job.id,
                )
                usage = usage + reask_usage
                plan_data = reask_plan_data
                completeness = collision.validate_plan_scope_completeness(
                    plan_data, files, candidates
                )
                observed_reason_codes.update(completeness.reason_codes)
                plan_data["target_files"] = list(completeness.manifest)
                n_stories = len(plan_data.get("stories", []))
                n_target_files = len(plan_data.get("target_files") or [])
                oversized = (
                    n_stories > _MAX_PLAN_STORIES or n_target_files > _MAX_PLAN_TARGET_FILES
                ) and not _plan_touches_migration(plan_data)
                chains = _compute_split_chains(plan_data) if oversized else None
    finally:
        if plan_wt_created:
            try:
                await gitops.remove_worktree(managed, plan_worktree)
            except Exception:
                log.warning("plan worktree cleanup failed for job %s", job.id, exc_info=True)
    rn.store.record_usage("plan", usage, job.id)
    # cgroup fields null: AI work runs on provider, only local wall-time is captured
    rn._record_resource(
        job,
        "plan",
        resources.ResourceRecord(
            cpu_seconds=None,
            peak_rss_bytes=None,
            io_read_bytes=None,
            io_write_bytes=None,
            wall_seconds=time.monotonic() - _plan_t0,
            sampled_at=resources._now_iso(),
        ),
    )
    unknown_reuses: list[str] = []
    shadowed_adds: list[str] = []
    if job.project_id is not None:
        try:
            index = rn.store.get_symbol_index_names(job.project_id)
            if index:
                unknown_reuses = contracts.validate_reuse_contract(
                    plan_data.get("reuses", []), index
                )
                shadowed_adds = contracts.validate_adds_contract(plan_data.get("adds", []), index)
                for sym in unknown_reuses:
                    log.warning(
                        "job %s: plan cites unknown reuse symbol %r; stripping", job.id, sym
                    )
                for sym in shadowed_adds:
                    log.warning(
                        "job %s: plan adds symbol %r that already exists in index",
                        job.id,
                        sym,
                    )
                if unknown_reuses:
                    plan_data["reuses"] = [
                        r for r in plan_data.get("reuses", []) if r not in unknown_reuses
                    ]
        except Exception:
            log.warning(
                "reuse contract validation failed for job %s; continuing",
                job.id,
                exc_info=True,
            )
    job.plan = plan_data
    planning_metadata = _planning_metadata(
        completeness,
        candidate_count=len(candidates),
        correction_count=job.plan_reask_attempts,
        usage=usage,
    )
    planning_metadata["reason_codes"] = sorted(observed_reason_codes)
    if not completeness.passed:
        rn.store.merge_planning_metadata(job.id, planning_metadata)
        rn._event(
            job,
            "plan",
            "failed",
            started,
            summary="scope-incomplete",
            detail={
                **completeness.to_dict(),
                "planning": planning_metadata,
            },
        )
        evidence = ", ".join(completeness.missing_paths) or "(no concrete path)"
        return await rn._fail(
            job,
            "scope-incomplete: "
            f"reason_codes={','.join(completeness.reason_codes)}; paths={evidence}",
        )
    if oversized and not bypassed:
        if chains is not None:
            rn.store.merge_planning_metadata(job.id, planning_metadata)
            await _supersede_with_split(rn, job, started, plan_data, chains, planning_metadata)
            return
        # Reached only after the bounded one-shot re-ask (see above) already ran and
        # still couldn't produce a splittable plan: park the job rather than leaving
        # it FAILED-and-retryable, since a bare retry would just re-run the same
        # doomed plan → re-ask → still-oversized cycle.
        rn.store.mark_needs_split(job.id)
        job.needs_split = True
        message = (
            _scope_gate_message(plan_data)
            + "\n\nThis job has been parked: a bare retry is refused; refile a new, "
            "smaller job, or retry with force=true to bypass the scope gate."
        )
        rn._event(job, "plan", "failed", started, summary="plan exceeds scope-gate limits")
        return await rn._fail(job, message)
    added = rn.store.reconcile_plan_scope(job.id, job.idea, plan_data)
    rn.store.freeze_validated_plan_scope(job.id, list(completeness.manifest), planning_metadata)
    if added:
        rn.store.add_event(
            job.id,
            "scope-manifest",
            "reconciled",
            summary=f"scope reconciled to include plan-declared paths: {', '.join(added)}",
        )
    rn._event(
        job,
        "plan",
        "done",
        started,
        summary=plan_data.get("summary", ""),
        detail={
            "stories": plan_data.get("stories", []),
            "unknown_reuses": unknown_reuses,
            "shadowed_adds": shadowed_adds,
            "planning": planning_metadata,
        },
        usage=usage,
    )
    job.stage = Stage.PLAN
    job.status = JobStatus.PENDING
    rn.store.save(job)
    n = len(plan_data.get("stories", []))
    await rn.notify(
        job.chat_id,
        f"📋 Job #{job.id}: planned {n} story(ies). Building…",
        project_id=job.project_id,
        job_id=job.id,
    )
    return
