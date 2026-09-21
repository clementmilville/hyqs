"""AI incident analyst: diagnose escalated failures, recommend a whitelisted action.

The deterministic janitor stays the control plane (the standing rule: control
logic must not depend on AI — AI is exactly what's unavailable when a provider
is rate-limited). This module is an ADVISORY layer on top: when the janitor
escalates a judgment-class failure (unknown, isolation_leak, genuine_code,
fix_no_changes, merge_conflict_exhausted), the analyst reads the evidence and
recommends ONE action from a closed menu. The janitor validates the action
against the whitelist and executes it deterministically — the AI never invents
commands, and every diagnosis is recorded in supervisor_events.

Actions:
- {"type": "requeue_at_stage", "stage": "<stage>"}   — bounded by the supervisor
  requeue budget; for failures the analyst judges transient/mis-classified.
- {"type": "repair_in_place", "idea": "...", "allowed_paths": [...]} — continue
  the existing branch at FIX, with an audited scope expansion, when the defect
  is causally part of the original ask and does not warrant another job.
- {"type": "file_fix_job", "idea": "..."}            — one per source job; for
  failures with a diagnosable code/infra root cause worth its own pipeline job.
- {"type": "file_fix_jobs", "jobs": [...]}           — like file_fix_job, but for
  fixes that plausibly need more than one job: a bounded (1-4) chain of jobs
  with backward-only depends_on indices, so the analyst can express the full
  fix instead of dropping follow-up work in prose.
- {"type": "escalate"}                               — nothing automated is safe;
  the human notification already sent stands.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from . import contracts
from .models import Stage, Usage
from .providers import Role

if TYPE_CHECKING:
    from .models import Job
    from .providers import AgentBackend

log = logging.getLogger("hyqs.analyst")

ANALYST_SYS = """\
You are the INCIDENT ANALYST of an autonomous dev pipeline. A job failed and the
deterministic supervisor could not remediate it automatically. Diagnose the root
cause from the evidence given (job metadata, error, failure report, event
timeline). You may inspect the repository and run READ-ONLY shell commands
(git log/diff, docker ps/logs, ls) to confirm a hypothesis — never modify
anything.

Then recommend EXACTLY ONE action from this closed menu:
- {"type": "requeue_at_stage", "stage": "queued|lint|build|test|review|security|deploy"}
  when the failure looks transient or environmental and a re-run from that
  checkpoint should succeed. Choose the earliest stage that must re-run, not
  QUEUED by default.
- {"type": "repair_in_place", "idea": "<precise correction instructions>",
  "allowed_paths": ["<repository-relative path>", ...]} when the existing job
  branch already contains useful implementation and the remaining defect is
  causally within the original outcome. Include every additional source or
  test path needed by the correction. Prefer this over file_fix_job: a missed
  consumer, test fixture, configuration contract, or narrowly related module
  is not an independent deliverable. Do not use it for unrelated baseline
  debt or a materially separate feature.
- {"type": "file_fix_job", "idea": "<a precise, self-contained job description
  with the evidence embedded>", "covers_stories": [<optional list of story ids
  from the failed job's plan, present ONLY when the plan (given in the
  evidence) shows a story this fix directly addresses>]} when a code or
  configuration defect needs its own pipeline job. Write the idea for an
  autonomous coding agent: name files, errors, and the acceptance criterion.
- {"type": "file_fix_jobs", "jobs": [{"idea": "<self-contained idea for an
  autonomous coding agent, small enough to plausibly fit hyqs's own job-scope
  gate — roughly 5 stories/8 target files>", "depends_on": [<0-based indices
  into this same jobs array, EARLIER entries only>], "covers_stories":
  [<optional list of story ids from the failed job's plan this chain entry
  directly addresses>]}, ...]} when the full fix
  genuinely needs more than one job. Prefer a single file_fix_job whenever the
  fix plausibly fits in one job's scope-gate budget; only use file_fix_jobs
  when your own reasoning concludes it doesn't — and when you do, describe
  ALL the jobs the fix needs, not just the first one deferring the rest to
  prose. Group stories/files that don't overlap into independent jobs (empty
  depends_on), and chain jobs that share files via depends_on so they run
  sequentially instead of contending — mirrors the same splitting guidance
  given to human/MCP job submitters. Cap the chain at 4 jobs.
- {"type": "escalate"} when nothing automated is safe or the cause needs a human
  decision.

Be conservative: if the same failure would plainly recur, do NOT recommend a
requeue. End your reply with a sentinel block:
<<<RESULT_JSON>>>
{...}
<<<END_RESULT>>>
matching the schema: {"diagnosis": str, "root_cause": str, "confidence": float,
"action": {"type": str, ...}}. confidence is 0.0-1.0 for the ACTION being the
right call (not for the diagnosis). A fenced ```json block is also accepted."""

REQUEUEABLE_STAGES = {"queued", "lint", "build", "test", "review", "security", "deploy"}


def _valid_allowed_paths(paths: object) -> bool:
    if not isinstance(paths, list) or not paths or len(paths) > 16:
        return False
    for path in paths:
        if not isinstance(path, str) or not path.strip() or path.startswith("/"):
            return False
        if ".." in path.split("/") or any(char in path for char in ("*", "?", "[")):
            return False
    return True


def _valid_covers_stories(covers_stories: object) -> bool:
    """covers_stories is optional; when present it must be a list of story id strings."""
    if covers_stories is None:
        return True
    return isinstance(covers_stories, list) and all(isinstance(s, str) for s in covers_stories)


def validate_action(action: object) -> tuple[bool, str]:
    """Pure whitelist check for an analyst-recommended action."""
    if not isinstance(action, dict):
        return False, "action is not an object"
    typ = action.get("type")
    if typ == "requeue_at_stage":
        stage = action.get("stage", "")
        if stage not in REQUEUEABLE_STAGES:
            return False, f"stage {stage!r} not requeueable"
        return True, ""
    if typ == "repair_in_place":
        idea = action.get("idea", "")
        if not isinstance(idea, str) or len(idea.strip()) < 40:
            return False, "in-place repair idea missing or too thin"
        if not _valid_allowed_paths(action.get("allowed_paths")):
            return False, "allowed_paths must contain 1-16 exact repository-relative paths"
        return True, ""
    if typ == "file_fix_job":
        idea = action.get("idea", "")
        if not isinstance(idea, str) or len(idea.strip()) < 40:
            return False, "fix-job idea missing or too thin"
        if not _valid_covers_stories(action.get("covers_stories")):
            return False, "covers_stories must be a list of story id strings"
        return True, ""
    if typ == "file_fix_jobs":
        chain = action.get("jobs")
        if not isinstance(chain, list) or not chain:
            return False, "fix-job chain missing or empty"
        if len(chain) > 4:
            return False, "fix-job chain exceeds 4 jobs"
        for i, entry in enumerate(chain):
            if not isinstance(entry, dict):
                return False, f"chain entry {i} is not an object"
            idea = entry.get("idea", "")
            if not isinstance(idea, str) or len(idea.strip()) < 40:
                return False, f"chain entry {i} idea missing or too thin"
            depends_on = entry.get("depends_on", [])
            if not isinstance(depends_on, list) or any(
                not isinstance(d, int) or not (0 <= d < i) for d in depends_on
            ):
                return False, f"chain entry {i} depends_on must reference only earlier indices"
            if not _valid_covers_stories(entry.get("covers_stories")):
                return False, f"chain entry {i} covers_stories must be a list of story id strings"
        return True, ""
    if typ == "escalate":
        return True, ""
    return False, f"unknown action type {typ!r}"


def _evidence(job: "Job", failure_class: str, events: list[dict]) -> str:
    lines = [
        f"Job #{job.id} — stage={job.stage.value} class={failure_class}",
        f"Branch: {job.branch or '(none)'}",
        f"attempts={job.attempts} rebase={job.rebase_attempts} timeouts={job.timeout_attempts}",
        f"Idea: {job.idea[:400]}",
        f"Error: {(job.error or '')[:1200]}",
    ]
    scope = (job.source_meta or {}).get("scope") or {}
    if scope.get("allowed_paths"):
        lines.append(f"Current allowed paths: {scope['allowed_paths']}")
    if job.failure:
        lines.append(f"Failure report: {job.failure[:1200]}")
    stories = (job.plan or {}).get("stories") or []
    if stories:
        lines.append("Plan stories (reference by id in covers_stories when a fix addresses one):")
        for s in stories:
            lines.append(f"- {s.get('id')}: {s.get('title', '')} — {(s.get('task') or '')[:200]}")
    if events:
        lines.append("Recent events (oldest first):")
        for e in events[-12:]:
            lines.append(f"- {e.get('stage')}/{e.get('status')}: {(e.get('summary') or '')[:220]}")
    return "\n".join(lines)


async def diagnose(
    backend: "AgentBackend",
    job: "Job",
    failure_class: str,
    events: list[dict],
    *,
    timeout: int = 480,
) -> tuple[dict, Usage]:
    """Run the analyst once. Raises on malformed output (caller treats as escalate)."""
    prompt = (
        _evidence(job, failure_class, events)
        + "\n\nDiagnose the root cause and recommend one action from the menu."
    )
    run = await backend.run(
        prompt=prompt,
        cwd=job.repo_path,
        role=Role.REVIEWER,
        append_system=ANALYST_SYS,
        max_turns=40,
        timeout=timeout,
    )
    data = contracts.parse_result_block(run.text)
    if not isinstance(data, dict) or "action" not in data:
        raise ValueError("analyst output missing 'action'")
    return data, run.usage


def stage_from_action(action: dict) -> Stage:
    return Stage(action["stage"])
