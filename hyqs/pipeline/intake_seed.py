"""Theme classification and project seeding from a confirmed intake spec.

Pure deterministic logic — no AI. Called by the confirm route to materialise
epics, jobs, and cross-epic dependencies from a completed spec.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from hyqs.pipeline.models import JobSource
from hyqs.pipeline.provision import provision_project
from hyqs.pipeline.store import JobStore

if TYPE_CHECKING:
    from hyqs.intake.models import IntakeSession

FOUNDATION_THEMES: frozenset[str] = frozenset({"Auth & Access", "Data & Model"})

PRIORITY_FOUNDATION: int = 20
PRIORITY_USER: int = 10
PRIORITY_SUGGESTED: int = 5


def classify_themes(features: list[dict]) -> dict[str, list[dict]]:
    """Group features by theme, preserving insertion order, omitting empty themes."""
    result: dict[str, list[dict]] = {}
    for feature in features:
        theme = feature.get("theme") or "General"
        if theme not in result:
            result[theme] = []
        result[theme].append(feature)
    return result


def plan_seed_from_spec(spec: dict) -> dict:
    """Return the epics/jobs plan for a spec without any side effects.

    Returns:
        {
            "epics": [{"name": theme}, ...],
            "jobs": [
                {
                    "title": acceptance_criteria,
                    "epic": theme,
                    "is_foundation": bool,
                    "depends_on_titles": [...],
                    "priority": int,
                    "source": str,
                },
                ...
            ],
        }

    Foundation jobs have depends_on_titles=[] and priority=PRIORITY_FOUNDATION.
    Non-foundation jobs list every foundation job title in depends_on_titles;
    priority is PRIORITY_USER for source=='user', PRIORITY_SUGGESTED otherwise.
    """
    themes = classify_themes(spec.get("features", []))

    epics = [{"name": theme} for theme in themes]

    jobs: list[dict] = []
    foundation_titles: list[str] = []

    for theme, features in themes.items():
        if theme not in FOUNDATION_THEMES:
            continue
        for feature in features:
            title = feature["acceptance_criteria"]
            source = feature.get("source", "user")
            jobs.append(
                {
                    "title": title,
                    "epic": theme,
                    "is_foundation": True,
                    "depends_on_titles": [],
                    "priority": PRIORITY_FOUNDATION,
                    "source": source,
                }
            )
            foundation_titles.append(title)

    for theme, features in themes.items():
        if theme in FOUNDATION_THEMES:
            continue
        for feature in features:
            source = feature.get("source", "user")
            priority = PRIORITY_USER if source == "user" else PRIORITY_SUGGESTED
            jobs.append(
                {
                    "title": feature["acceptance_criteria"],
                    "epic": theme,
                    "is_foundation": False,
                    "depends_on_titles": list(foundation_titles),
                    "priority": priority,
                    "source": source,
                }
            )

    return {"epics": epics, "jobs": jobs}


async def seed_project_from_spec(
    store: JobStore,
    projects_dir: Path,
    spec: dict,
    chat_id: int = 0,
    session: "IntakeSession | None" = None,
    creator_id: str | None = None,
) -> dict:
    """Provision a project, seed one epic per non-empty theme, one job per feature.

    Foundation themes (Auth & Access, Data & Model) run first — non-foundation
    jobs depend on ALL foundation job IDs so they block until foundations are DONE.
    """
    result = await provision_project(
        store,
        projects_dir,
        name=spec["name"],
        description=spec.get("one_liner", ""),
        stack=spec.get("stack", "bare"),
        spec=spec,
        create_github=True,
        private=True,
        creator_id=creator_id,
    )
    project = result["project"]

    plan = plan_seed_from_spec(spec)

    epics: dict[str, object] = {}
    for epic_plan in plan["epics"]:
        theme = epic_plan["name"]
        epic = store.create_epic(project.id, name=theme)
        epics[theme] = epic

    _source_actor = session.created_by if session else ""
    _source_meta = {"intake_session_id": session.session_id} if session else {}

    foundation_job_ids: list[int] = []
    jobs: list[object] = []

    for job_plan in plan["jobs"]:
        if not job_plan["is_foundation"]:
            continue
        epic = epics[job_plan["epic"]]
        job = store.create(
            idea=job_plan["title"],
            repo_path=project.repo_path,
            chat_id=chat_id,
            epic_id=epic.id,
            priority=job_plan["priority"],
            source=JobSource.INTAKE,
            source_actor=_source_actor,
            source_meta=_source_meta,
        )
        foundation_job_ids.append(job.id)
        jobs.append(job)

    for job_plan in plan["jobs"]:
        if job_plan["is_foundation"]:
            continue
        epic = epics[job_plan["epic"]]
        job = store.create(
            idea=job_plan["title"],
            repo_path=project.repo_path,
            chat_id=chat_id,
            epic_id=epic.id,
            depends_on=foundation_job_ids if foundation_job_ids else None,
            priority=job_plan["priority"],
            source=JobSource.INTAKE,
            source_actor=_source_actor,
            source_meta=_source_meta,
        )
        jobs.append(job)

    epicless = [j.id for j in jobs if getattr(j, "epic_id", None) is None]
    if epicless:
        raise RuntimeError(f"seed produced jobs without epic_id: {epicless}")

    return {
        "project": project,
        "epics": list(epics.values()),
        "jobs": jobs,
    }
