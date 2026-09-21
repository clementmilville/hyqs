"""The agent-driven stages. Each runs a one-shot coding agent via a backend.

The actual provider (Claude, Codex, …) is injected as an :class:`AgentBackend`,
so a stage is provider-agnostic: prompt + role in, text out. Structured stages
(plan, review) end with a sentinel/fenced JSON block validated against a schema.
Build is free-form (it edits files).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Callable

from . import contracts
from .models import (
    TERMINAL_JOB_STATUSES,
    CurrentExecutor,
    Job,
    JobStatus,
    Usage,
    stage_task,
)
from .pricing import compute_cost
from .providers import AgentBackend, AgentRun, Role

log = logging.getLogger("hyqs.stages")

_DEPENDENCY_REMEDIATION_FIELDS = frozenset(
    {
        "advisory_id",
        "baseline_installed_version",
        "baseline_severity",
        "blocks",
        "candidate_installed_version",
        "candidate_severity",
        "comparison_status",
        "dependency_path",
        "dependency_scope",
        "fix_available",
        "lockfile_path",
        "lowest_compatible_patched_version",
        "manifest_path",
        "package",
        "recommended_action",
        "severity",
        "tool",
        "verification_commands",
    }
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|authorization|password|secret|token)\b(\s*[:=]\s*)([^\s,;]+)"
)
_SECRET_TOKEN_RE = re.compile(r"\b(?:gh[oprsu]_[A-Za-z0-9_]{20,}|xox[baprs]-[^\s]+)\b")


def _redact_dependency_evidence_text(value: str) -> str:
    value = _SECRET_ASSIGNMENT_RE.sub(r"\1\2[REDACTED]", value)
    return _SECRET_TOKEN_RE.sub("[REDACTED]", value)


def _dependency_remediation_evidence(
    failure_detail: Mapping[str, object] | None,
) -> dict[str, list[dict[str, object]]] | None:
    """Return only provider-safe, structured npm remediation evidence."""
    if not isinstance(failure_detail, Mapping):
        return None
    raw_entries = failure_detail.get("remediation")
    if not isinstance(raw_entries, list):
        return None
    entries: list[dict[str, object]] = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, Mapping):
            continue
        package = raw_entry.get("package")
        advisory_id = raw_entry.get("advisory_id")
        manifest_path = raw_entry.get("manifest_path")
        lockfile_path = raw_entry.get("lockfile_path")
        if not all(
            isinstance(value, str) and value
            for value in (package, advisory_id, manifest_path, lockfile_path)
        ):
            continue
        entry: dict[str, object] = {}
        for key in sorted(_DEPENDENCY_REMEDIATION_FIELDS):
            if key not in raw_entry:
                continue
            value = raw_entry.get(key)
            if isinstance(value, str):
                entry[key] = _redact_dependency_evidence_text(value)
            elif isinstance(value, (bool, int, float)) or value is None:
                entry[key] = value
            elif key == "verification_commands" and isinstance(value, list):
                entry[key] = [
                    _redact_dependency_evidence_text(command)
                    for command in value
                    if isinstance(command, str)
                ]
        entries.append(entry)
    return {"remediation": entries} if entries else None


def resolve_current_executor(job: Job) -> CurrentExecutor | None:
    """Derive the job's current executor without changing persisted ownership.

    Terminal state wins over stale claim data. Deploy reconciliation and running
    deterministic stages belong to the pipeline, while an actively running AI
    stage exposes its existing roster assignment. Everything else is waiting for
    an assignment.
    """
    if job.status in TERMINAL_JOB_STATUSES:
        return None
    if job.status == JobStatus.DEPLOYING:
        return CurrentExecutor(kind="pipeline", label="Pipeline")
    if job.status == JobStatus.RUNNING:
        if stage_task(job.stage) is not None:
            if job.agent_id is not None and job.provider:
                return CurrentExecutor(
                    kind="agent",
                    label=job.provider,
                    agent_id=job.agent_id,
                    provider=job.provider,
                )
            return CurrentExecutor(kind="waiting", label="Waiting assignment")
        return CurrentExecutor(kind="pipeline", label="Pipeline")
    return CurrentExecutor(kind="waiting", label="Waiting assignment")


class JobCancelled(Exception):
    """A job was cancelled mid-run.

    Raised by :func:`run_cancellable` when the store reports the job's status
    flipped to ``JobStatus.CANCELLED`` while the wrapped coroutine was still
    running. Carries the ``job_id`` that was cancelled.
    """

    def __init__(self, job_id: int) -> None:
        super().__init__(f"job {job_id} was cancelled")
        self.job_id = job_id


async def run_cancellable(coro, *, store, job_id: int, poll_interval: float = 2.0):
    """Run ``coro`` to completion, aborting early if the job is cancelled.

    Polls ``store.get(job_id)`` (a blocking sync call, so it's dispatched via
    ``asyncio.to_thread`` to avoid stalling the event loop) every
    ``poll_interval`` seconds. If the polled job's status becomes
    ``JobStatus.CANCELLED``, the wrapped task is cancelled and
    :class:`JobCancelled` is raised. Otherwise returns the coroutine's result,
    or re-raises its exception.
    """
    task = asyncio.ensure_future(coro)
    while True:
        done, _pending = await asyncio.wait({task}, timeout=poll_interval)
        if task in done:
            return task.result()
        job = await asyncio.to_thread(store.get, job_id)
        if job is not None and job.status == JobStatus.CANCELLED:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            raise JobCancelled(job_id)


def _load_conventions(repo_root: str) -> str:
    try:
        return (Path(repo_root) / "CONVENTIONS.md").read_text()
    except FileNotFoundError:
        return ""


_CONSTITUTION_STANDING_RULE = """\
CONVENTIONS.md is this project's constitution. If your change introduces or alters a
project-wide pattern (new shared component, new breakpoint system, new API convention,
new architectural rule), update CONVENTIONS.md in the same diff so the rule lands with
the code. You may ADD or EXTEND rules. You must NEVER weaken, relax, or delete an
existing rule in CONVENTIONS.md unless the job idea explicitly instructs it."""


async def _collect_stream(
    backend,
    *,
    prompt: str,
    cwd,
    role,
    append_system: str,
    max_turns: int = 60,
    timeout=None,
    log_sink: Callable[[str], None],
) -> tuple[str, Usage]:
    """Drive backend.stream(), forwarding text/tool-use events to log_sink.

    Returns (accumulated_text, Usage) when a 'result' event arrives.
    Adapter-raised ProviderUnavailable exceptions propagate unchanged. Generic
    ``error`` events are ordinary backend failures and cannot trigger failover.
    """
    text = ""
    usage = Usage()

    async def _iterate() -> None:
        nonlocal text, usage
        async for event in backend.stream(
            prompt=prompt,
            cwd=cwd,
            role=role,
            append_system=append_system,
            max_turns=max_turns,
        ):
            t = event.get("type")
            if t == "text":
                log_sink(event["delta"])
            elif t == "tool_use":
                log_sink(f"[{event['tool']}] {event['input_summary']}")
            elif t == "result":
                text = event["text"]
                u = event.get("usage") or {}
                model = str(
                    u.get("model") or event.get("model") or getattr(backend, "model", "") or ""
                )
                provider = str(u.get("provider") or event.get("provider") or backend.name or "")
                input_tokens = int(u.get("input_tokens", 0) or 0)
                output_tokens = int(u.get("output_tokens", 0) or 0)
                cache_creation_tokens = int(u.get("cache_creation_tokens", 0) or 0)
                cache_read_tokens = int(u.get("cache_read_tokens", 0) or 0)
                cost_usd = float(u.get("cost_usd", 0.0) or 0.0)
                if cost_usd == 0.0 and any(
                    (input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens)
                ):
                    cost_usd = compute_cost(
                        model,
                        input_tokens,
                        output_tokens,
                        cache_creation_tokens,
                        cache_read_tokens,
                    )
                usage = Usage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_creation_tokens=cache_creation_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cost_usd=cost_usd,
                    model=model,
                    provider=provider,
                )
            elif t == "error":
                raise RuntimeError(event.get("message", "agent error"))

    if timeout:
        await asyncio.wait_for(_iterate(), timeout=timeout)
    else:
        await _iterate()
    return text, usage


async def _invoke(
    backend: AgentBackend,
    *,
    prompt: str,
    cwd: str,
    role: Role,
    append_system: str,
    timeout: int | None,
    log_sink: Callable[[str], None] | None,
    max_turns: int = 60,
    store=None,
    job_id: int | None = None,
) -> AgentRun:
    """Run one agent call, streaming to log_sink when provided.

    When both ``store`` and ``job_id`` are given, the call races against
    :func:`run_cancellable`'s polling of the job's status and raises
    :class:`JobCancelled` if the job is cancelled mid-run.
    """

    async def _do_invoke() -> AgentRun:
        if log_sink is not None:
            t, u = await _collect_stream(
                backend,
                prompt=prompt,
                cwd=cwd,
                role=role,
                append_system=append_system,
                max_turns=max_turns,
                timeout=timeout,
                log_sink=log_sink,
            )
            return AgentRun(text=t, usage=u)
        return await backend.run(
            prompt=prompt,
            cwd=cwd,
            role=role,
            append_system=append_system,
            max_turns=max_turns,
            timeout=timeout,
        )

    if store is not None and job_id is not None:
        return await run_cancellable(_do_invoke(), store=store, job_id=job_id, poll_interval=7.0)
    return await _do_invoke()


async def _run_gate_with_reparse(
    schema: str,
    backend: AgentBackend,
    prompt: str,
    worktree: str,
    append_system: str,
    timeout: int | None,
    log_sink: Callable[[str], None] | None,
    *,
    store=None,
    job_id: int | None = None,
) -> tuple[dict, Usage]:
    """Run a verdict gate (review/security), re-running once on malformed output.

    Malformed gate output is a gate defect, not a code defect: routing it into the
    FIX loop burns a fix credit on a fixer with nothing actionable. Re-run the gate
    once; if it still can't produce parseable output, fail closed as before.
    """
    total = Usage()
    for attempt in (1, 2):
        run = await _invoke(
            backend,
            prompt=prompt,
            cwd=worktree,
            role=Role.REVIEWER,
            append_system=append_system,
            timeout=timeout,
            log_sink=log_sink,
            store=store,
            job_id=job_id,
        )
        total = total + run.usage
        try:
            return contracts.parse_and_validate(schema, run.text), total
        except contracts.ContractError as e:
            if attempt == 1:
                log.warning("%s gate output failed contract (%s); re-running gate once", schema, e)
                continue
            return (
                {"verdict": "fail", "summary": f"[ContractError] {e}", "findings": []},
                total,
            )
    raise AssertionError("unreachable")


# --- stages --------------------------------------------------------------

PLAN_SYS = """\
You are the PLANNING stage of an autonomous dev pipeline. Inspect the repository
to understand its stack and conventions, then turn the idea into a concrete,
minimal implementation plan. Do NOT write any code.

## STAGE OWNERSHIP AND INVESTIGATION BUDGET
PLAN produces an evidence-backed implementation plan only.
- Do not edit files or run tests, builds, linters, formatters, migrations,
  dependency installation, or deployment commands.
- Use the repository file map and symbol index first. Read only the nearest
  implementations, tests, configuration, and conventions needed to resolve the
  idea.
- Do not perform broad exploratory scans after the relevant implementation and
  test patterns are known.
- BUILD owns implementation and regression tests. Deterministic LINT and TEST
  stages own validation. REVIEW, SECURITY, and DESIGN REVIEW own their respective
  merge judgments.

## FULL-STACK DIRECTIVE
Use the authoritative repository file map to determine whether a frontend exists.
Inspect frontend implementation only when the idea changes or may change an HTTP
endpoint, request/response contract, or UI-visible model. Do not perform a
repository-wide frontend search for explicitly internal or backend-only work. Then:
- If the plan adds, removes, or renames an HTTP endpoint; changes a request or
  response schema/shape; or modifies a data model field that surfaces in the UI,
  you MUST also include the corresponding frontend stories and
  ui_impact.frontend_changes (update client calls, forms, rendering, types).
- Keep plans minimal: do NOT invent UI stories for purely internal/backend-only
  changes (migrations, workers, refactors with no UI-visible effect).
- An explicit backend-only/no-frontend scope fence in the job is authoritative:
  do not invent frontend stories or files. Set touches_backend_surface=false and
  explain in no_ui_change_reason that UI work is explicitly deferred by scope.
- If no frontend exists in the repo, this directive is a no-op; set
  ui_impact.touches_backend_surface=false.

## CONFIGURATION ACTIVATION DIRECTIVE
If any story introduces a new configuration field, flag, mode, or Literal member
whose default preserves current behavior, the new behavior is opt-in. Set
activation.config_change_required=true, name the exact configuration file or
location to change in activation.activation_location, and describe the concrete,
observable live-behavior change once activated in activation.expected_live_effect.
Do not add a story that flips the new default itself unless the job idea explicitly
asks for that default change.
- If merging and deploying the code activates the behavior automatically, or the
  plan otherwise needs no configuration activation, set
  activation.config_change_required=false, activation.activation_location="", and
  give a brief reason in activation.expected_live_effect.

End your reply with a sentinel block:
<<<RESULT_JSON>>>
{...}
<<<END_RESULT>>>
matching the schema: {"summary": str, "stories": [{"id": "S1", "title": str, "task": str, "acceptance": str, "target_files": [str, ...], "depends_on": ["S0", ...]}], "reuses": [str, ...], "adds": [str, ...], "file_impact": [{"path": str, "role": str, "justification": str, "status": "existing"|"new", "provenance": str}], "ui_impact": {"touches_backend_surface": bool, "frontend_changes": [str, ...], "no_ui_change_reason": str}, "target_files": [str, ...], "activation": {"config_change_required": bool, "activation_location": str, "expected_live_effect": str}}
- file_impact: one entry per changed file: {"path": str, "role": str,
  "justification": str, "status": "existing"|"new", "provenance": str}.
  Provenance briefly names the idea, repository evidence, convention, or story
  that requires the file. Paths must be unique.
- reuses: fully-qualified names of existing project symbols this plan calls rather than reimplements (e.g. "hyqs.pipeline.store.JobStore.get")
- Every item in reuses must be verified from the supplied symbol index or by
  reading its defining file. Never infer a fully-qualified symbol name from
  prose, a path, or a naming convention. If existence is uncertain, omit it
  from reuses and describe the required behavior in the story instead.
- adds: new top-level names this plan introduces (e.g. "hyqs.pipeline.stages.review")
- ui_impact.touches_backend_surface: true when this plan changes an HTTP endpoint, request/response shape, or UI-visible data model field
- ui_impact.frontend_changes: list of frontend story summaries when touches_backend_surface=true
- ui_impact.no_ui_change_reason: concise and relevant whenever an internal-only
  plan omits a UI layer; API/UI-visible work must list frontend changes.
- target_files: canonical POSIX repo-relative paths of every file this plan will
  create or modify. No absolute paths, traversal, ".", redundant separators, or
  backslashes. It must EXACTLY equal the union of all stories[].target_files and
  file_impact[].path, with no duplicates.
- stories[].target_files: required, repo-relative paths that THIS SPECIFIC story will create or modify — itemized per story (a subset of the plan-wide target_files above) so an oversized plan can always be split into independent, dependency-chained jobs deterministically. Every story must list at least one file.
- stories[].depends_on: optional, list of earlier story ids whose code THIS story imports, calls, wires, or tests — REQUIRED for any test-only story (it depends on every story it exercises) and for any story that consumes another story's new module/function/route/template. Used to order the deterministic split so a story never runs before the code it builds on. Reference only ids defined in this same stories array; no cycles.
- activation.config_change_required: true when an opt-in configuration field,
  flag, mode, or Literal member must be changed from its compatibility-preserving
  default for the new behavior to take effect; false when no configuration change
  is required.
- activation.activation_location: the exact configuration file or location to
  change when config_change_required=true; otherwise the empty string.
- activation.expected_live_effect: when config_change_required=true, the concrete,
  observable live-behavior change after activation; otherwise a brief reason no
  configuration activation is required.
- Plan the smallest regression coverage that proves the behavior at the correct
  layer. Reuse existing fixtures and helpers when verified. Keep tests alongside
  the implementation story unless a separately ordered test story is genuinely
  required by splitting. Do not duplicate lower-level cryptographic, persistence,
  or protocol tests inside route-level tests.
Keep it focused, but complete. If a complete plan is oversized, retain every
required implementation, integration, and test file and express dependency-linked
stories so the pipeline can split them deterministically. NEVER shrink an
oversized plan by dropping required files or layers. A fenced ```json block is
also accepted.

## REPOSITORY FILE MAP
When a "Repository files" list is provided, it is the authoritative, COMPLETE set
of files that exist in this repo. Do NOT guess or probe for paths outside it (no
speculative `backend/…`, `app/…`, `src/…` reads) — pick the relevant files from
the list and read those directly. This is not optional: hypothesising paths that
don't exist wastes the whole planning budget."""


async def plan(
    backend: AgentBackend,
    idea: str,
    repo_path: str,
    timeout: int | None = None,
    *,
    symbol_context: str = "",
    file_manifest: str = "",
    decision_digest: str = "",
    reask_context: str = "",
    log_sink: Callable[[str], None] | None = None,
    store=None,
    job_id: int | None = None,
) -> tuple[dict, Usage]:
    # Stable context blocks first, the volatile idea + instruction last: better
    # prompt-cache prefix reuse across a job's retries, and the actual request
    # sits at the end of the prompt where instruction-following is strongest.
    parts: list[str] = []
    if file_manifest:
        parts.append(
            "## Repository files (authoritative — the complete list of files "
            "that exist; do not probe for paths outside it)\n" + file_manifest
        )
    if symbol_context:
        parts.append(symbol_context)
    conventions = _load_conventions(repo_path)
    if conventions:
        parts.append(f"## Project Conventions\n\n{conventions}")
    if decision_digest:
        parts.append(f"## Recent Architecture Decisions\n\n{decision_digest}")
    if reask_context:
        parts.append(f"## Previous Plan Was Rejected\n{reask_context}")
    parts.append(f"Idea to implement:\n\n{idea}")
    parts.append("Produce the plan.")
    initial_prompt = "\n\n".join(parts)
    prompt = initial_prompt
    total = Usage()
    for attempt in (1, 2):
        run = await _invoke(
            backend,
            prompt=prompt,
            cwd=repo_path,
            role=Role.PLANNER,
            append_system=PLAN_SYS,
            timeout=timeout,
            log_sink=log_sink,
            store=store,
            job_id=job_id,
        )
        total = total + run.usage
        try:
            return contracts.parse_and_validate("plan", run.text), total
        except contracts.ContractError as e:
            # Malformed planner output is a planner defect: re-run once inline
            # instead of failing the job into a full janitor requeue-from-QUEUED.
            if attempt == 1:
                log.warning("plan output failed contract (%s); re-running planner once", e)
                try:
                    previous_evidence = json.dumps(
                        contracts.parse_result_block(run.text),
                        indent=2,
                        sort_keys=True,
                    )
                except contracts.ResultBlockError:
                    previous_evidence = run.text[-2_000:] or "[empty response]"
                prompt = "\n\n".join(
                    [
                        "## Contract Correction",
                        f"Validation error:\n{e}",
                        "## Previous Structured Plan or Output\n" + previous_evidence,
                        "## Focused Evidence\n"
                        "Correct only the reported contract defect. Preserve all required "
                        "files, stories, dependencies, and UI impact; use canonical "
                        "repo-relative paths and the exact target-files union.",
                        "Return the corrected complete plan.",
                    ]
                )
                continue
            raise ValueError(str(e)) from e
    raise AssertionError("unreachable")


def _render_implementation_contract(plan_data: Mapping[str, object]) -> str:
    """Render PLAN's verified reuse/add/UI obligations for BUILD and FIX."""
    reuses = plan_data.get("reuses") or []
    adds = plan_data.get("adds") or []
    raw_ui_impact = plan_data.get("ui_impact") or {}
    ui_impact = raw_ui_impact if isinstance(raw_ui_impact, Mapping) else {}
    lines = ["## Verified implementation contract"]
    lines.append(
        "Existing symbols to call rather than reimplement:\n"
        + ("\n".join(f"- {symbol}" for symbol in reuses) if reuses else "- (none specified)")
    )
    lines.append(
        "New top-level symbols expected by the plan:\n"
        + ("\n".join(f"- {symbol}" for symbol in adds) if adds else "- (none specified)")
    )
    frontend_changes = ui_impact.get("frontend_changes") or []
    if ui_impact.get("touches_backend_surface"):
        lines.append(
            "Required UI/API integration obligations:\n"
            + (
                "\n".join(f"- {change}" for change in frontend_changes)
                if frontend_changes
                else "- The plan marks a UI-visible backend change; preserve its stated contract."
            )
        )
    else:
        reason = ui_impact.get("no_ui_change_reason") or "No UI-visible change specified."
        lines.append(f"UI impact: none specified. Reason: {reason}")
    lines.append("Use verified existing symbols directly. Do not copy or recreate their behavior.")
    return "\n".join(lines)


def _render_planner_scope(plan_data: Mapping[str, object]) -> str:
    """Render the complete planner-owned execution and regression scope."""
    scope = {
        key: plan_data.get(key)
        for key in ("summary", "stories", "file_impact", "target_files")
        if key in plan_data
    }
    return (
        "## Complete planner implementation and regression scope\n"
        "This is authoritative for implementation. Preserve every story, acceptance "
        "criterion, dependency edge, story target, plan target, and file-impact entry. "
        "An absent or stale prior diff, including reimplementation of failed work on "
        "current main, does not authorize dropping production, execution-layer, or "
        "focused regression files. Do not broaden beyond this scope.\n"
        "```json\n" + json.dumps(scope, indent=2, sort_keys=False, ensure_ascii=False) + "\n```"
    )


BUILD_SYS = (
    """\
You are the BUILD stage of an autonomous dev pipeline, working inside an
isolated git worktree. Implement the plan completely: write code AND tests,
follow the repo's existing conventions, and keep changes focused. Do not commit
(the pipeline commits for you). When done, briefly summarize what you changed.

## Implementation boundaries

- Implement every story and acceptance criterion in the supplied plan.
- Treat the complete planner scope in the prompt as authoritative, including
  dependency edges, story-specific targets, plan-wide targets, file-impact
  entries, and focused regression files. When rebuilding failed work on current
  main, an absent or stale prior diff does not authorize dropping any of them.
- Read the nearest existing implementation and tests before editing.
- Reuse verified project symbols, fixtures, factories, and helpers. Do not copy
  lower-level cryptographic, persistence, protocol, or session machinery into a
  higher-level test when the lower layer is already covered.
- Keep tests at the smallest layer that proves this change. Mock an established
  lower-level contract in route or service tests unless the plan explicitly calls
  for integration coverage.
- Start with the planner's target files. Modify another tracked file only when it
  is necessary for correctness or integration, and report the deviation.

## Downstream ownership

Write complete regression tests, but keep BUILD-stage execution limited to the
smallest directly relevant test targets needed to develop and sanity-check the
change.

- Run only tests for the files or behavior you changed.
- Do not run the full test suite, an entire broad test directory, or unrelated
  regression suites unless the plan explicitly requires it.
- Do not perform progressively broader "just in case" test runs.
- Do not wait on a long-running background test command; stop it and report what
  was not checked.
- Do not run formatters or linters. Deterministic LINT runs next and owns
  formatting, safe auto-fixes, and lint verification.
- Deterministic TEST owns authoritative affected-test and broader validation.
- Do not perform a broad code review, security audit, design review, merge,
  deployment, or operational verification. Later stages own those tasks.
- Never weaken or omit tests to save time; this rule limits test execution
  during BUILD, not the tests you must write.

Before finishing, inspect the final diff and check each supplied story and
acceptance criterion for implementation completeness. Fix missing implementation
work, but do not perform downstream review-stage analysis.

End with a concise summary of: stories implemented; files changed; targeted tests
run and their result; validation not run; and necessary deviations from planned
target files. Do not claim that lint, broad tests, security, design, merge, or
deployment passed; downstream stages determine those outcomes.

When a "Repository files" list is provided, it is the authoritative, COMPLETE set
of files that exist. Read/edit files from it directly — do not guess or probe for
paths outside it (no speculative `backend/…`, `app/…`, `src/…` reads).

"""
    + _CONSTITUTION_STANDING_RULE
)

NO_DIFF_RETRY_EVIDENCE_LIMIT = 2_000


async def build(
    backend: AgentBackend,
    idea: str,
    plan_data: dict,
    worktree: str,
    timeout: int | None = None,
    *,
    symbol_context: str = "",
    file_manifest: str = "",
    log_sink: Callable[[str], None] | None = None,
    store=None,
    job_id: int | None = None,
    no_diff_retry_evidence: str = "",
) -> tuple[str, Usage]:
    # Stable context blocks first, the task last (cache-prefix reuse + recency).
    parts: list[str] = []
    if file_manifest:
        parts.append(
            "## Repository files (authoritative — the complete list of files that "
            "exist; do not probe for paths outside it)\n" + file_manifest
        )
    if symbol_context:
        parts.append(symbol_context)
    conventions = _load_conventions(worktree)
    if conventions:
        parts.append(f"## Project Conventions\n\n{conventions}")
    parts.append(f"Original idea:\n{idea}")
    parts.append(_render_implementation_contract(plan_data))
    parts.append(_render_planner_scope(plan_data))
    target_files = plan_data.get("target_files") or []
    if target_files:
        files_block = (
            "## Files to edit/create (planner-identified edit targets — a focused subset of the repository files listed above; open these first)\n"
            + "\n".join(f"- {f}" for f in target_files)
        )
        parts.append(files_block)
    if no_diff_retry_evidence:
        evidence = no_diff_retry_evidence.strip()[:NO_DIFF_RETRY_EVIDENCE_LIMIT]
        parts.append(
            "## Independent no-diff verifier evidence (one-shot BUILD retry)\n"
            "The previous BUILD produced no valid diff. Address only the concrete "
            "missing work identified below; do not treat this as permission to broaden scope.\n"
            f"<<<VERIFIER_EVIDENCE>>>\n{evidence}\n<<<END_VERIFIER_EVIDENCE>>>"
        )
    parts.append("Implement all of it now.")
    prompt = "\n\n".join(parts)
    run = await _invoke(
        backend,
        prompt=prompt,
        cwd=worktree,
        role=Role.CODER,
        append_system=BUILD_SYS,
        max_turns=120,
        timeout=timeout,
        log_sink=log_sink,
        store=store,
        job_id=job_id,
    )
    return run.text, run.usage


FIX_SYS = (
    """\
You are the FIX stage of an autonomous dev pipeline, working inside the same
git worktree as the build. A downstream deterministic check or AI gate failed.
Read the failure report, find the root cause, and make the smallest correct
change (code and/or tests) to resolve it while following the repo's conventions.
Do not commit (the pipeline commits for you).

## Fix scope and convergence

- Preserve every original story, acceptance criterion, verified reuse contract,
  and UI/API obligation.
- Preserve every planner dependency edge, story-specific target, plan-wide target,
  file-impact entry, and focused regression file. An absent or stale prior diff
  does not authorize dropping these when reimplementing failed work on current main.
- Address only the reported blocker and regressions directly caused by the fix.
- Do not refactor, clean up, redesign, or pursue unrelated improvements.
- Inspect the current diff and nearest relevant implementation and tests before
  editing.
- Reuse verified project symbols; do not replace them with local reimplementations.

## Test and invariant integrity

- Never delete, skip, weaken, or broadly rewrite a test, assertion, invariant,
  security check, or gate merely to make the failure disappear.
- Change an expected result only when the original job explicitly changes that
  behavior and the implementation satisfies the new contract.
- Prefer fixing production code. Change or add tests only when coverage is
  missing or the intended contract genuinely changed.

## Revalidation ownership

- Run only the smallest targeted reproducer needed to confirm the edit.
- Do not run formatters, linters, broad suites, security scans, design review,
  merge checks, deployment, or operational verification.
- After FIX, the pipeline restarts deterministic LINT and TEST, followed by
  REVIEW, SECURITY, and DESIGN REVIEW. Those stages own authoritative
  revalidation.

End with: diagnosed root cause; smallest fix applied; files changed; targeted
reproducer run and result; and validation deliberately left to downstream stages.
Do not claim that lint, broad tests, review, security, design, merge, or deployment
passed.

When a "Repository files" list is provided, it is the authoritative, COMPLETE set
of files that exist. Read/edit files from it directly — do not guess or probe for
paths outside it.

"""
    + _CONSTITUTION_STANDING_RULE
)


async def fix(
    backend: AgentBackend,
    idea: str,
    failure: str,
    worktree: str,
    timeout: int | None = None,
    *,
    failure_detail: Mapping[str, object] | None = None,
    plan_data: Mapping[str, object] | None = None,
    failed_step: str = "",
    failure_code: str = "",
    fix_attempt: int = 0,
    file_manifest: str = "",
    log_sink: Callable[[str], None] | None = None,
    store=None,
    job_id: int | None = None,
) -> tuple[str, Usage]:
    # Stable context blocks first, the failure + instruction last (cache + recency).
    parts: list[str] = []
    if file_manifest:
        parts.append(
            "## Repository files (authoritative — the complete list of files that "
            "exist; do not probe for paths outside it)\n" + file_manifest
        )
    conventions = _load_conventions(worktree)
    if conventions:
        parts.append(f"## Project Conventions\n\n{conventions}")
    parts.append(f"Original idea:\n{idea}")
    if plan_data:
        parts.append(_render_implementation_contract(plan_data))
        parts.append(_render_planner_scope(plan_data))
        target_files = plan_data.get("target_files") or []
        if isinstance(target_files, list) and target_files:
            parts.append(
                "Original planned target files:\n" + "\n".join(f"- {path}" for path in target_files)
            )
    parts.append(
        "## Failure identity\n"
        f"Failed step: {failed_step or '(unknown)'}\n"
        f"Failure code: {failure_code or '(unspecified)'}\n"
        f"Fix attempt: {fix_attempt}"
    )
    parts.append(f"The pipeline failed and needs a fix. Failure report:\n\n{failure}")
    safe_failure_detail = _dependency_remediation_evidence(failure_detail)
    if safe_failure_detail is not None:
        structured_evidence = json.dumps(
            safe_failure_detail,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        parts.append(
            "## Structured failure evidence\n\n"
            "Prefer structured dependency remediation entries in this block over "
            "package names or guidance flattened into the prose failure report. "
            "Use the attributed manifest and lockfile paths, advisory identity, "
            "compatible patch guidance, recommended action, and verification "
            "commands when present.\n"
            "<<<FAILURE_DETAIL_JSON>>>\n"
            f"{structured_evidence}\n"
            "<<<END_FAILURE_DETAIL_JSON>>>"
        )
    parts.append(
        "Inspect the current diff, fix only the reported root cause, and preserve "
        "the original implementation contract. Do it now."
    )
    prompt = "\n\n".join(parts)
    run = await _invoke(
        backend,
        prompt=prompt,
        cwd=worktree,
        role=Role.CODER,
        append_system=FIX_SYS,
        max_turns=120,
        timeout=timeout,
        log_sink=log_sink,
        store=store,
        job_id=job_id,
    )
    return run.text, run.usage


CONFLICT_FIX_SYS = """\
You are the FIX stage of an autonomous dev pipeline, resolving a git merge conflict.
Conflict markers (<<<<<<<, =======, >>>>>>>) are present in the worktree. For each
conflicted file: open it, decide which changes to keep (yours, theirs, or a blend),
and remove all <<<<<<<, =======, and >>>>>>> marker lines, leaving only the correct
merged code. When all conflicts are resolved, verify with `git status` that no files
are listed as 'UU' (unmerged). Then ensure the project's tests pass. Do NOT commit
— the pipeline commits for you. When done, briefly summarize which conflicts you
resolved and how."""


async def fix_conflict(
    backend: AgentBackend,
    idea: str,
    failure: str,
    worktree: str,
    timeout: int | None = None,
    *,
    log_sink: Callable[[str], None] | None = None,
    store=None,
    job_id: int | None = None,
) -> tuple[str, Usage]:
    # Stable conventions first, the conflict report + instruction last.
    parts: list[str] = []
    conventions = _load_conventions(worktree)
    if conventions:
        parts.append(f"## Project Conventions\n\n{conventions}")
    parts.append(f"Original idea:\n{idea}")
    parts.append(f"Merge conflict report:\n\n{failure}")
    parts.append(
        "Resolve all conflict markers in the worktree, verify `git status` shows no "
        "UU entries, and ensure tests pass. Do it now."
    )
    prompt = "\n\n".join(parts)
    run = await _invoke(
        backend,
        prompt=prompt,
        cwd=worktree,
        role=Role.CODER,
        append_system=CONFLICT_FIX_SYS,
        max_turns=120,
        timeout=timeout,
        log_sink=log_sink,
        store=store,
        job_id=job_id,
    )
    return run.text, run.usage


REVIEW_SYS = """\
You are the REVIEW stage: a senior code reviewer gating a merge. Judge only whether
this branch is correct and complete — not whether it is perfect.

## Review ownership

REVIEW judges functional correctness, completeness, integration, and adherence to
required project contracts.
- Inspect the diff and relevant source files only. Do not edit files.
- Do not run tests, builds, formatters, linters, security scanners, deployment, or
  operational checks. Deterministic LINT and TEST have already run.
- SECURITY owns exploitability, authorization vulnerabilities, secrets, dependency
  vulnerabilities, and attacker-triggerable destructive behavior.
- DESIGN REVIEW owns visual quality, interaction design, accessibility polish, and
  responsive behavior.
- Apply domain-specialist guidance only to functional correctness and completeness;
  defer security-only and design-only concerns to their downstream stages.

BLOCKING (verdict=fail) — ONLY these:
- behavior contradicts a story or acceptance criterion,
- a required execution path is missing or broken,
- normal valid use produces incorrect state, data corruption, or unintended deletion,
- integration with an existing interface is incorrect,
- regression coverage explicitly required by the plan is absent, or an existing test
  was weakened to conceal incorrect behavior,
- functionality already present in the codebase was unnecessarily reimplemented, or
- a required convention or co-located integration pattern was violated in a way that
  breaks correctness or compatibility.

NON-BLOCKING (verdict=pass — you MAY note them in `summary` as suggestions, but you
MUST NOT fail for them): additional test coverage not required by the plan; style,
naming, formatting, minor robustness/defensive-coding nits, logging,
micro-optimizations, subjective structure, or visual polish; and security-only or
design-only concerns owned by downstream stages.

Every blocking finding must identify the concrete file and symbol or behavior, the
violated story or acceptance criterion, the observed failure, and the minimal
correction. Do not fail on speculation.

CONVERGE: as soon as the change is correct and complete, return verdict=pass. Do not
expand scope with fresh nitpicks pass after pass — that wastes the fix budget on a
change that is already mergeable.

End your reply with a sentinel block:
<<<RESULT_JSON>>>
{...}
<<<END_RESULT>>>
matching the schema: {"verdict": "pass" | "fail", "summary": str, "findings": [{"severity": str, "note": str}]}.
A fenced ```json block is also accepted."""


_REUSE_CHECKLIST = """\
## Semantic-Reuse Checklist (answer each in `summary`)
[1] Does any added code reimplement functionality already present in the index? \
If yes, cite the existing symbol and set verdict=fail.
[2] Does the code follow the patterns shown by co-located symbols? \
If not, fail only when the deviation causes a concrete correctness, compatibility, \
or required-convention violation; cite that impact.
[3] Are all symbols listed in `reuses` actually called (not reimplemented)? \
If not, set verdict=fail."""


_CONSTITUTION_SCRUTINY = """\
## Constitution Scrutiny (CONVENTIONS.md is in this diff's changed files)
CONVENTIONS.md is this project's constitution: gates review diffs against it, so it
must not be self-amending — no agent may relax its own rules in the same diff that is
being reviewed against them. Compare the CONVENTIONS.md diff against the job idea below:
- Adding or extending a rule is fine.
- Weakening, relaxing, or deleting an EXISTING rule is a BLOCKING violation (set
  verdict=fail) UNLESS the job idea explicitly instructs that specific change.

Job idea:
{idea}"""


_CLAIMS_VS_DIFF_CHECK = """\
## Claims-vs-Diff Check (this is a fix/ai-fix job)
This job's title and idea make concrete claims about what changed (renames, additions,
N-file sweeps, etc.). Enumerate each concrete claim, then verify it against the ACTUAL
diff — run `git diff {base}...HEAD` yourself; do not trust the job's own
implementation_summary as proof a claim was fulfilled.
- If a claimed change is absent from the diff, set verdict=fail. The actual diff is
  authoritative; do not accept an implementation summary as proof.

Job title:
{title}

Job idea:
{idea}"""


async def review(
    backend: AgentBackend,
    worktree: str,
    base: str,
    timeout: int | None = None,
    *,
    symbol_context: str = "",
    plan_data: dict | None = None,
    prior_review: dict | None = None,
    decision_digest: str = "",
    persona_checklist: str = "",
    idea: str = "",
    title: str = "",
    conventions_changed: bool = False,
    is_fix_job: bool = False,
    log_sink: Callable[[str], None] | None = None,
    store=None,
    job_id: int | None = None,
) -> tuple[dict, Usage]:
    # Stable context blocks first, the per-attempt convergence block and the
    # review instruction last (cache-prefix reuse + recency).
    parts: list[str] = []
    if symbol_context:
        parts.append(f"## Codebase Symbol Index\n{symbol_context}")
    if plan_data is not None:
        parts.append(f"## Original idea\n{idea or '(not supplied)'}")
        parts.append(f"## Plan summary\n{plan_data.get('summary', '')}")
        stories = "\n".join(
            f"- [{story.get('id', '?')}] {story.get('title', '')}: "
            f"{story.get('task', '')} (done when: {story.get('acceptance', '')})"
            for story in plan_data.get("stories", [])
            if isinstance(story, Mapping)
        )
        if stories:
            parts.append(f"## Stories and acceptance criteria\n{stories}")
        parts.append(_render_implementation_contract(plan_data))
        target_files = plan_data.get("target_files") or []
        if isinstance(target_files, list) and target_files:
            parts.append(
                "## Planned target files\n" + "\n".join(f"- {path}" for path in target_files)
            )
    if symbol_context or plan_data is not None:
        parts.append(_REUSE_CHECKLIST)
    conventions = _load_conventions(worktree)
    if conventions:
        parts.append(f"## Project Conventions\n\n{conventions}")
    if decision_digest:
        parts.append(f"## Recent Architecture Decisions\n\n{decision_digest}")
    if persona_checklist:
        parts.append(f"## Domain Specialist Checklist\n\n{persona_checklist}")
    if conventions_changed:
        parts.append(_CONSTITUTION_SCRUTINY.format(idea=idea))
    if is_fix_job:
        parts.append(_CLAIMS_VS_DIFF_CHECK.format(base=base, title=title, idea=idea))
    # Convergence: on a re-review after a fix, only confirm the prior BLOCKING
    # findings are resolved — don't restart the review and surface fresh nitpicks,
    # which is what makes complex jobs churn through their whole fix budget.
    if prior_review and prior_review.get("findings") is not None:
        prior_findings = (
            "\n".join(
                f"- [{f.get('severity', '?')}] {f.get('note', '')}"
                for f in prior_review.get("findings", [])
            )
            or "(none listed)"
        )
        parts.append(
            "## Prior review (you already reviewed an EARLIER version of this branch)\n"
            f"Previous verdict: {prior_review.get('verdict')}\n"
            f"Previous summary: {prior_review.get('summary', '')}\n"
            f"Previously-raised findings:\n{prior_findings}\n\n"
            "CONVERGENCE RULE: verify whether the previously-raised BLOCKING findings are "
            "resolved and inspect only the latest fix for a clear new FUNCTIONAL correctness "
            "regression. Do not introduce security, design, style, or unrelated findings. "
            "If the prior blockers are resolved and the fix introduced no functional blocker, "
            "return verdict=pass."
        )
    parts.append(
        f"Review the diff of the current branch against '{base}'. "
        f"Use `git diff {base}...HEAD` to see the changes."
    )
    prompt = "\n\n".join(parts)
    return await _run_gate_with_reparse(
        "review",
        backend,
        prompt,
        worktree,
        REVIEW_SYS,
        timeout,
        log_sink,
        store=store,
        job_id=job_id,
    )


SECURITY_SYS = """\
You are the SECURITY stage: a mandatory, zero-tolerance security gate. Your job is to
block any exploitable vulnerability that is INTRODUCED OR MODIFIED by this diff from
reaching merge.

## Defense-in-depth ownership

Review the full changed surface independently: application code, tests where they
affect security guarantees, new or modified dependencies and lockfiles, and
configuration present in the diff.

Deterministic diff-scoped SAST, secret-pattern checks, and dependency auditing run
separately, and either layer can block. Do not assume those scanners found everything,
and do not ignore a vulnerability merely because it is scanner-detectable. Perform
your own complete semantic inspection and report every confirmed exploitable
vulnerability you observe.

Do not spend time invoking scanners, package audits, tests, builds, linters, package
installation, network research, deployment, or operational checks. Inspect the diff
and relevant source directly. You may inspect dependency and configuration semantics,
including lifecycle scripts and newly exposed capabilities, without rerunning advisory
scans.

Use the supplied implementation contract and architecture decisions to understand
intended trust boundaries, actors, subjects, tenants, scopes, data flows, expiry, and
failure behavior. Functional completeness under normal valid use was assessed by
REVIEW; SECURITY owns attacker-triggerable or unauthorized confidentiality, integrity,
and availability impact. DESIGN REVIEW owns UX and visual concerns unless a concrete
exploitable security condition directly depends on them. Apply domain-specialist
guidance only to threat modeling and exploitable security impact.

SCOPE: a vulnerability is in scope when the diff introduces it, modifies it, or makes
pre-existing dangerous behavior newly reachable. Unchanged baseline debt that the diff
neither exposes nor worsens is out of scope.

BLOCKING (verdict=fail): ANY exploitable vulnerability introduced or modified by the
diff, regardless of severity. Low and medium severity vulnerabilities block just as
hard as critical ones. Examples (non-exhaustive):
- All injection types: SQL, NoSQL, command, OS, template, LDAP, header injection
- XSS (reflected, stored, DOM), CSRF, open redirect
- SSRF (Server-Side Request Forgery)
- Path traversal, directory traversal, arbitrary file read/write
- Unsafe deserialization, XXE (XML External Entity)
- Insecure cryptography: weak/broken hashing for secrets (MD5/SHA1 for passwords),
  hardcoded keys or IVs, insecure randomness used for security-sensitive values,
  disabled TLS certificate verification
- Unsafe code execution: eval/exec on untrusted input, pickle on untrusted data,
  subprocess with shell=True and any user-controlled input
- ReDoS (catastrophic backtracking regex on untrusted input)
- Prototype pollution (JavaScript/TypeScript)
- Missing or broken authentication/authorisation checks on sensitive operations
- Secrets, credentials, API keys, tokens, or private keys in code or config
- Sensitive data exposure in logs, error responses, or client-visible fields
- Destructive or data-loss operations without an adequate safety guard

Every blocking finding must establish:
1. the concrete changed path and symbol or behavior,
2. attacker capability or untrusted input,
3. the missing or broken security control,
4. the reachable sink or state transition,
5. confidentiality, integrity, or availability impact,
6. how this diff introduced, modified, or newly exposed the vulnerability, and
7. the minimal remediation.

Do not fail for a hypothetical defense-in-depth improvement without a reachable exploit
path. Missing tests alone are not a vulnerability; block when the implemented behavior
is exploitable. This evidence requirement never permits a confirmed vulnerability to
pass.

NON-BLOCKING (verdict=pass — you MAY note in `summary` as a suggestion, but MUST NOT
fail for them): pure style issues, naming, formatting, test coverage gaps, minor
robustness nits, or anything that is genuinely not an exploitable security vulnerability.
The severity label on a finding never makes it non-blocking — only whether it is a
confirmed exploitable vulnerability or not.

CONVERGENCE RULE: once no introduced vulnerability remains, return verdict=pass.
Do not expand scope with fresh style nitpicks on each pass. HOWEVER: if the diff still
contains an introduced vulnerability, you MUST return verdict=fail regardless of how
many prior review rounds have occurred. The convergence rule can never be used to wave
through a still-present or newly-introduced vulnerability.

End your reply with a sentinel block:
<<<RESULT_JSON>>>
{...}
<<<END_RESULT>>>
matching the schema: {"verdict": "pass" | "fail", "summary": str, "findings": [{"severity": str, "note": str}]}.
Each finding may also include `"path": str`. When present, `path` is authoritative
and MUST be exactly one canonical concrete repository-relative POSIX file path
(for example, `hyqs/web/app.py`), never an absolute path, directory, glob, range,
list, or prose. Omit `path` when no precise file owns the finding.
A fenced ```json block is also accepted."""


async def security(
    backend: AgentBackend,
    worktree: str,
    base: str,
    timeout: int | None = None,
    *,
    prior_security: dict | None = None,
    persona_checklist: str = "",
    idea: str = "",
    plan_data: Mapping[str, object] | None = None,
    decision_digest: str = "",
    log_sink: Callable[[str], None] | None = None,
    store=None,
    job_id: int | None = None,
) -> tuple[dict, Usage]:
    parts: list[str] = []
    if idea:
        parts.append(f"## Original idea\n{idea}")
    if plan_data:
        parts.append(f"## Plan summary\n{plan_data.get('summary', '')}")
        stories = "\n".join(
            f"- [{story.get('id', '?')}] {story.get('title', '')}: "
            f"{story.get('task', '')} (done when: {story.get('acceptance', '')})"
            for story in plan_data.get("stories", [])
            if isinstance(story, Mapping)
        )
        if stories:
            parts.append(f"## Security-relevant stories and acceptance criteria\n{stories}")
        parts.append(_render_implementation_contract(plan_data))
    if decision_digest:
        parts.append(f"## Recent Architecture Decisions\n\n{decision_digest}")
    if persona_checklist:
        parts.append(f"## Domain Specialist Checklist\n\n{persona_checklist}")
    if prior_security and prior_security.get("findings") is not None:
        prior_findings = (
            "\n".join(
                f"- [{f.get('severity', '?')}] {f.get('note', '')}"
                for f in prior_security.get("findings", [])
            )
            or "(none listed)"
        )
        parts.append(
            "## Prior security review (you already reviewed an EARLIER version of this branch)\n"
            f"Previous verdict: {prior_security.get('verdict')}\n"
            f"Previous summary: {prior_security.get('summary', '')}\n"
            f"Previously-raised findings:\n{prior_findings}\n\n"
            "CONVERGENCE RULE: verify ONLY whether the previously-raised BLOCKING security "
            "findings are now resolved. Do NOT introduce new findings unless the latest changes "
            "introduced a clear NEW security regression. If the prior blocking findings are "
            "resolved, return verdict=pass."
        )
    parts.append(
        f"Review the diff of the current branch against '{base}' for security issues. "
        f"Use `git diff {base}...HEAD` to see the changes."
    )
    prompt = "\n\n".join(parts)
    return await _run_gate_with_reparse(
        "security",
        backend,
        prompt,
        worktree,
        SECURITY_SYS,
        timeout,
        log_sink,
        store=store,
        job_id=job_id,
    )


DESIGN_REVIEW_SYS = """\
You are the DESIGN_REVIEW stage: a UX gate for UI-touching diffs, gating a merge.
Judge ONLY the UI introduced or modified by this diff — not pre-existing UI elsewhere
in the codebase.

SCOPE: review the diff of the current branch against the base (use
`git diff <base>...HEAD`). Only the changed UI is in scope.

BLOCKING (verdict=fail) — ONLY these:
- an orphan class name: a className referenced by the diff's JSX/TSX/Vue that does
  not resolve to any defined CSS selector,
- hard-coded px/color/font values used in the diff's changed UI where the project's
  design tokens (see tokens.css / styles.css) already provide the equivalent value,
- a new interactive element (button, form, list, async panel) introduced by the diff
  with no loading, empty, or error state where one is clearly needed.

NON-BLOCKING (verdict=pass — you MAY note them in `summary` as suggestions, but you
MUST NOT fail for them): pre-existing UI debt outside the diff, subjective style
preferences, minor wording, spacing nits that don't break usability.

CONVERGE: as soon as the changed UI is usable and styled, return verdict=pass. On a
re-run, only verify the previously-raised BLOCKING findings are resolved — do not
expand scope with fresh nitpicks pass after pass.

End your reply with a sentinel block:
<<<RESULT_JSON>>>
{...}
<<<END_RESULT>>>
matching the schema: {"verdict": "pass" | "fail", "summary": str, "findings": [{"severity": str, "note": str}]}.
Each finding may also include `"path": str`. When present, `path` is authoritative
and MUST be exactly one canonical concrete repository-relative POSIX file path
(for example, `hyqs/web/frontend/src/components/LogPanel.jsx`), never an absolute
path, directory, glob, range, list, or prose. Omit `path` when no precise file
owns the finding.
A fenced ```json block is also accepted."""


async def design_review(
    backend: AgentBackend,
    worktree: str,
    base: str,
    timeout: int | None = None,
    *,
    prior_design_review: dict | None = None,
    persona_checklist: str = "",
    log_sink: Callable[[str], None] | None = None,
    store=None,
    job_id: int | None = None,
) -> tuple[dict, Usage]:
    parts = [
        f"Review the diff of the current branch against '{base}' for UI/UX issues. "
        f"Use `git diff {base}...HEAD` to see the changes."
    ]
    if persona_checklist:
        parts.append(f"## Domain Specialist Checklist\n\n{persona_checklist}")
    if prior_design_review and prior_design_review.get("findings") is not None:
        prior_findings = (
            "\n".join(
                f"- [{f.get('severity', '?')}] {f.get('note', '')}"
                for f in prior_design_review.get("findings", [])
            )
            or "(none listed)"
        )
        parts.append(
            "## Prior design review (you already reviewed an EARLIER version of this branch)\n"
            f"Previous verdict: {prior_design_review.get('verdict')}\n"
            f"Previous summary: {prior_design_review.get('summary', '')}\n"
            f"Previously-raised findings:\n{prior_findings}\n\n"
            "CONVERGENCE RULE: verify ONLY whether the previously-raised BLOCKING findings "
            "are now resolved. Do NOT introduce new findings unless the latest changes "
            "introduced a clear NEW blocking UX regression. If the prior blocking findings "
            "are resolved, return verdict=pass."
        )
    prompt = "\n\n".join(parts)
    return await _run_gate_with_reparse(
        "design_review",
        backend,
        prompt,
        worktree,
        DESIGN_REVIEW_SYS,
        timeout,
        log_sink,
        store=store,
        job_id=job_id,
    )


ARCHITECT_SYS = """\
You are the ARCHITECT — a senior software architect turning an epic-level goal into a
dependency-ordered job DAG for a fully deterministic pipeline to execute. You only draw
the map; the runtime queue, dependency gate, and collision serialization are 100%
deterministic and never take orchestration advice from you at runtime.

You may inspect the repository (symbol index, decision digest, file tools) to ground
the plan in what already exists, so jobs reuse rather than duplicate.

HARD CONSTRAINTS:
1. You CANNOT create, queue, or file jobs. You have no tools to do so. Jobs are only
   created when the user reviews your proposal and clicks "Create N jobs".
2. Never claim to have created, queued, or filed jobs.
3. `depends_on` contains 0-based indexes into the jobs array of THIS proposal only.
4. `scope.allowed_paths` is a job's scope manifest: narrow, non-wildcard globs (never
   `**/*` or a whole directory when a handful of files will do) naming the files this
   job is expected to touch. When two jobs would otherwise touch the same file, add an
   explicit depends_on edge between them instead of leaving the overlap unscoped — this
   is how the deterministic collision gate keeps hot files from being edited by two
   jobs at once.

ORDERING:
- Foundation-first: sequence shared schemas, models, and helpers before the jobs that
  build on them. depends_on must reflect real prerequisite order, not just file overlap.
- Break the goal into a minimal set of independently implementable jobs; unrelated jobs
  get no depends_on so they can run in parallel, dependent jobs wait on their
  prerequisites.

SELF-CONTAINED IDEAS:
- `idea` must be self-contained: enough context for a BUILD agent with no memory of
  this conversation to implement it, ending with an explicit "done when: ..." acceptance
  criteria clause.

Conclude your reply with a sentinel block in this exact format:

<<<RESULT_JSON>>>
{"summary": str, "rationale": str, "jobs": [{"title": str, "idea": str, "depends_on": [int], "priority": int, "scope": {"allowed_paths": [str], "interfaces": str}}]}
<<<END_RESULT>>>

Rules:
- Propose at most 10 jobs
- Job titles must be under 80 characters
- `idea` is a self-contained description of the job's scope AND its acceptance criteria
  (end with "done when: ...")
- `rationale` explains the DAG shape: why jobs are ordered and split the way they are
- Every job needs: a depends_on list (use [] for none), an integer priority (higher runs
  first among otherwise-runnable jobs; 0 is the default), and a scope with allowed_paths
  (narrow globs, never "**/*"; use [] only when genuinely unknown) plus a one-line
  interfaces note describing what this job exposes or consumes for other jobs
- Do not omit the sentinel block — a goal always produces a plan, even a single-job one"""


def _build_architect_prompt(
    project_name: str,
    repo_path: str,
    epic_name: str,
    epic_description: str,
    goal: str,
    symbol_index_text: str,
    decision_digest: str,
) -> str:
    lines = [
        f"Project: {project_name}",
        f"Repository: {repo_path}",
        f"Epic name: {epic_name}",
        f"Epic description: {epic_description or '(none provided)'}",
        "",
        f"Goal: {goal}",
    ]
    if symbol_index_text:
        lines += [
            "",
            "Symbol index (existing code — reuse rather than duplicate):",
            symbol_index_text,
        ]
    if decision_digest:
        lines += ["", "Recent decisions:", decision_digest]
    lines += ["", "Propose a complete, dependency-ordered job DAG for this goal now."]
    return "\n".join(lines)


async def architect(backend: AgentBackend, prompt: str, cwd: str):
    """Stream ARCHITECT plan events, mirroring suggest_epic_features_stream's shape.

    Yields text/tool_use/thinking events unchanged, and turns the final 'result'
    event into {"type": "result", "summary": str, "rationale": str, "jobs": list,
    "raw": str}. Never raises on malformed or schema-invalid model output — degrades
    to {"type": "result", "summary": "", "jobs": [], "raw": str} instead.
    """
    async for event in backend.stream(
        prompt=prompt,
        cwd=cwd,
        role=Role.PLANNER,
        append_system=ARCHITECT_SYS,
        max_turns=15,
    ):
        if event["type"] != "result":
            yield event
            continue
        try:
            data = contracts.parse_and_validate("architect", event["text"])
        except contracts.ContractError:
            data = None
        if data is None:
            yield {"type": "result", "summary": "", "jobs": [], "raw": event["text"]}
        else:
            yield {
                "type": "result",
                "summary": data.get("summary", ""),
                "rationale": data.get("rationale", ""),
                "jobs": data.get("jobs", []),
                "raw": event["text"],
            }
