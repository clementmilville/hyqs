"""Merge stage: serialize per repo, squash-merge the PR, recover from base advance / conflict.

Stage handler for SECURITY → DEPLOY. Extracted from PipelineRunner._advance; logic unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING

from .. import (
    classify,
    conflict_autoresolve,
    decisions,
    github,
    gitops,
    impl_summary,
    merge_validate,
    resources,
)
from ..models import JobStatus, Stage, _now, gate_finding_fingerprint
from ..providers import build_backend
from ._common import _PR_CONFLICT_RE

if TYPE_CHECKING:
    from ..models import Job
    from ..runner import PipelineRunner

log = logging.getLogger("hyqs.runner")

# GitHub's GraphQL rejection when origin/<base> advanced between our local
# base_has_advanced check and the actual squash-merge call — a merge race, not
# a real conflict. Routed into the same worktree-recreate/rebase/phantom-
# conflict-recovery path as a generic "not mergeable" response below.
_BASE_MODIFIED_RE = re.compile(r"base branch was modified", re.IGNORECASE)


def _pr_matches(pr: github.PullRequest, *, base: str, branch: str, head_oid: str) -> bool:
    return pr.matches(base=base, branch=branch, head_oid=head_oid)


def _pr_evidence(action: str, pr: github.PullRequest) -> dict:
    return {
        "action": action,
        "state": pr.state.value,
        "url": pr.url,
        "number": pr.number,
        "base": pr.base_ref_name,
        "head": pr.head_ref_name,
        "head_oid": pr.head_ref_oid,
    }


def _activation_from_plan(plan: object) -> dict[str, bool | str] | None:
    """Return normalized activation guidance from a current-format plan."""
    if not isinstance(plan, Mapping):
        return None
    activation = plan.get("activation")
    if not isinstance(activation, Mapping):
        return None
    return {
        "config_change_required": bool(activation.get("config_change_required", False)),
        "activation_location": str(activation.get("activation_location", "")).strip(),
        "expected_live_effect": str(activation.get("expected_live_effect", "")).strip(),
    }


def _append_activation_note(summary: str, activation: Mapping[str, object]) -> str:
    """Append the plan's activation instructions to an implementation summary."""
    if activation["config_change_required"]:
        note = (
            f"Activation: update {activation['activation_location']}; expected live effect: "
            f"{activation['expected_live_effect']}."
        )
    else:
        note = "Activation: none required; this change alters behaviour unconditionally."
    summary = summary.rstrip()
    return f"{summary} {note}" if summary else note


def _file_activation_followup(
    rn: "PipelineRunner", job: "Job", activation: Mapping[str, object]
) -> "Job":
    """File the operational work needed to activate a merged parent job."""
    location = activation["activation_location"]
    expected_effect = activation["expected_live_effect"]
    idea = (
        f"Activate merged job #{job.id}.\n\n"
        f"Apply the required configuration change at {location}. "
        f"The expected live effect is: {expected_effect}.\n\n"
        f"Done when the configuration at {location} is updated and the live system "
        f"exhibits this effect: {expected_effect}."
    )
    return rn.store.create(
        idea=idea,
        repo_path=job.repo_path,
        chat_id=job.chat_id,
        epic_id=job.epic_id,
        priority=job.priority,
        source=job.source,
        source_actor=job.source_actor,
        source_meta={
            "kind": "activation-followup",
            "activation_follow_up_of": job.id,
            "activation_location": location,
            "expected_live_effect": expected_effect,
        },
    )


async def _recover_pr_identity(
    rn: "PipelineRunner",
    job: "Job",
    *,
    managed: str,
    worktree,
    base: str,
    head_oid: str,
    evidence: list[dict],
    max_attempts: int | None = None,
    attempt_offset: int = 0,
    replacement_state: list[bool] | None = None,
) -> tuple[github.PullRequest | None, str]:
    """Force-refresh and return only an exactly observed, usable PR identity."""
    if replacement_state is None:
        replacement_state = [False]
    attempt_limit = max_attempts or rn.phantom_conflict_max_attempts
    for local_attempt in range(1, attempt_limit + 1):
        attempt = attempt_offset + local_attempt
        pushed = await github.force_push_branch(worktree, job.branch)
        evidence.append(
            {
                "action": "force_push",
                "attempt": attempt,
                "ok": pushed.ok,
                "stderr": pushed.stderr[:300],
            }
        )
        if not pushed.ok:
            return None, f"force-refresh failed on attempt {attempt}: {pushed.stderr[:300]}"
        await asyncio.sleep(rn.phantom_conflict_backoff)
        pr = await github.inspect_branch_pr(managed, job.branch, base=base)
        evidence.append({"attempt": attempt, **_pr_evidence("observe", pr)})
        if _pr_matches(pr, base=base, branch=job.branch, head_oid=head_oid):
            if pr.state is github.PullRequestState.CLOSED:
                reopened = await github.reopen_pr(managed, pr)
                evidence.append(
                    {
                        "action": "reopen",
                        "attempt": attempt,
                        "url": pr.url,
                        "number": pr.number,
                        "ok": reopened.ok,
                        "stderr": reopened.stderr[:300],
                    }
                )
                if reopened.ok:
                    pr = await github.inspect_branch_pr(managed, job.branch, base=base)
                    evidence.append(
                        {"attempt": attempt, **_pr_evidence("inspect_after_reopen", pr)}
                    )
            if pr.state in {
                github.PullRequestState.OPEN,
                github.PullRequestState.MERGED,
            } and _pr_matches(pr, base=base, branch=job.branch, head_oid=head_oid):
                return pr, ""
        if not replacement_state[0]:
            created = await github.pr_create(
                worktree,
                job.branch,
                base,
                title=f"Job #{job.id}: {(job.title or job.idea)[:60]}",
                body=f"Autonomous build by Hyqs for job #{job.id}.\n\n**Idea:** {job.idea}",
            )
            replacement_state[0] = True
            evidence.append(
                {
                    "action": "replacement_create",
                    "attempt": attempt,
                    "ok": created.ok,
                    "stderr": created.stderr[:300],
                }
            )
            replacement = await github.inspect_branch_pr(managed, job.branch, base=base)
            evidence.append(
                {"attempt": attempt, **_pr_evidence("inspect_replacement", replacement)}
            )
            if replacement.state in {
                github.PullRequestState.OPEN,
                github.PullRequestState.MERGED,
            } and _pr_matches(replacement, base=base, branch=job.branch, head_oid=head_oid):
                return replacement, ""
    return None, (
        f"GitHub did not expose an OPEN or MERGED PR for {job.branch} at exact OID "
        f"{head_oid} after {attempt_limit} attempt(s)"
    )


async def run(rn: "PipelineRunner", job: "Job") -> None:
    worktree = rn.worktrees / f"job-{job.id}"
    started = _now()
    t_merge_start = time.time()
    managed = await rn._managed_repo(job)
    base = await gitops.default_branch(managed)

    if job.project_id:
        try:
            numstat_entries = await gitops.numstat(worktree, f"{base}...HEAD")
            changed_files = [e["path"] for e in numstat_entries]
            diff_text = await gitops.patch(worktree, f"{base}...HEAD")
        except Exception as exc:
            log.warning("job %s: could not resolve diff for schema-touch check: %s", job.id, exc)
            changed_files, diff_text = [], ""
        extra_patterns = None
        project = rn.store.get_project(job.project_id)
        if project and project.deploy_config:
            try:
                extra_patterns = json.loads(project.deploy_config).get("schema_paths")
            except ValueError:
                extra_patterns = None
        if classify.is_schema_touching(changed_files, diff_text, extra_patterns):
            # Serialize schema-touching jobs merge->deploy: a schema job holds this
            # lock for the whole landing window so a sibling schema job never merges
            # against a not-yet-landed migration (job #700 postmortem: ledger-app
            # outage came from two schema jobs landing out of order).
            if not rn.store.try_acquire_schema_lock(
                job.project_id, f"job-{job.id}", time.time(), rn.schema_lock_ttl
            ):
                job.status = JobStatus.PENDING  # another schema job is landing; retry soon
                job.owner = ""
                job.lease_until = 0.0
                job.rebase_retry_after = time.time() + 5
                rn.store.save(job)
                return

    # Serialize merges per repo: builds/tests/reviews ran in parallel
    # worktrees, but two jobs must not merge into one repo's base at once.
    if not rn.store.try_acquire_merge_lock(
        job.repo_path, job.owner, time.time(), rn.merge_lock_ttl
    ):
        job.status = JobStatus.PENDING  # someone else is merging; retry soon
        job.owner = ""
        job.lease_until = 0.0
        # Short claim backoff (honored by claim's rebase_retry_after check) so the
        # loser isn't re-claimed instantly; frees this worker slot immediately.
        job.rebase_retry_after = time.time() + 5
        rn.store.save(job)
        return
    _owner = job.owner  # capture before _rebase_conflict can clear it for the finally block
    try:
        is_github = await github.has_remote(managed)
        merge_record: "resources.ResourceRecord | None" = None
        base_ref = base

        # Capture the merge-base and this job's own diff *before* absorbing any base
        # advance below, so post-merge validation (after the loop) can compare hunk
        # ranges against a patch computed from the same shared origin as the base's
        # new commits — the two patches are only directly comparable that way.
        merge_base_sha: str | None = None
        job_patch = ""
        try:
            mb_res = await gitops.git(worktree, "merge-base", "HEAD", base_ref)
            if mb_res.ok and mb_res.stdout.strip():
                merge_base_sha = mb_res.stdout.strip()
                job_patch = await gitops.patch(worktree, f"{merge_base_sha}..HEAD")
        except Exception as exc:
            log.warning(
                "job %s: could not resolve merge-base for post-merge validation: %s",
                job.id,
                exc,
            )
        base_advanced = False

        # In-lock loop: absorb clean base advances without releasing the merge serialization
        # lock; only fall out to fix_conflict when real conflict markers are left behind.
        # Clean advances do NOT consume the rebase_attempts budget.
        for _step in range(rn.merge_inlock_steps):
            if is_github:
                await gitops.git(managed, "fetch", "origin", f"{base}:{base}")
            if not await gitops.base_has_advanced(managed, job.branch, base_ref):
                break  # base is current; proceed to the actual merge below
            base_advanced = True
            res = await gitops.merge_base_into_worktree(worktree, base_ref)
            if not res.ok:
                autoresolve = await conflict_autoresolve.autoresolve_conflict(worktree)
                if autoresolve.ok:
                    rn._event(
                        job,
                        "merge",
                        "auto-resolved",
                        started,
                        summary=(
                            f"auto-resolved pure-append conflict updating from {base}: "
                            f"{', '.join(autoresolve.files)}"
                        ),
                    )
                    if is_github:
                        push_res = await gitops.push(worktree, job.branch)
                        if not push_res.ok:
                            log.warning(
                                "job %s: push after base merge failed: %s",
                                job.id,
                                push_res.stderr[:200],
                            )
                    continue
                # Real conflict: record pre-conflict HEAD so MERGE_VERIFY can scope its diff.
                head = await gitops.head_info(worktree)
                job.merge_delta_sha = head.get("hash", "")
                rn._event(
                    job,
                    "merge",
                    "conflict",
                    started,
                    summary=f"conflict updating from {base}: {res.stderr[:300]}",
                )
                conflict_msg = (
                    f"[merge-conflict] Conflict markers (<<<<<<<, =======, >>>>>>>) are present "
                    f"in the worktree after merging {base}. Conflicted files:\n{res.stderr}\n\n"
                    f"Resolve each file by editing out the conflict markers (keep the correct "
                    f"merged code), ensure `git status` shows no UU entries, and make sure all "
                    f"tests pass. Do NOT commit — the pipeline commits for you."
                )
                return await rn._rebase_conflict(job, conflict_msg)
            # Clean advance: push and continue the loop
            if is_github:
                push_res = await gitops.push(worktree, job.branch)
                if not push_res.ok:
                    log.warning(
                        "job %s: push after base merge failed: %s",
                        job.id,
                        push_res.stderr[:200],
                    )
        else:
            # Exhausted in-lock steps on clean advances; release lock and route to MERGE_VERIFY.
            job.merge_delta_sha = None
            job.stage = Stage.MERGE_VERIFY
            job.status = JobStatus.PENDING
            rn.store.save(job)
            await rn.notify(
                job.chat_id,
                f"🔄 Job #{job.id}: base advanced — re-verifying before merge…",
                project_id=job.project_id,
                job_id=job.id,
            )
            return
        # break path: base is current, fall through to the actual merge

        # Post-merge validation (job #4077 postmortem): a clean textual auto-merge is
        # not proof of correctness — gates only ever ran against the branch tip, so a
        # defect introduced by the merge itself (e.g. two independent fixes for the
        # same hunk stacked instead of one chosen) ships to main unexamined. Only pay
        # this cost when a real base advance was absorbed this attempt; a merge where
        # the base never moved needs nothing extra.
        if base_advanced and merge_base_sha:
            try:
                base_patch = await gitops.patch(managed, f"{merge_base_sha}..{base_ref}")
                job_numstat = await gitops.numstat(worktree, f"{merge_base_sha}..HEAD")
                base_numstat = await gitops.numstat(managed, f"{merge_base_sha}..{base_ref}")
                validation_changed_files = sorted(
                    {e["path"] for e in job_numstat} | {e["path"] for e in base_numstat}
                )
            except Exception as exc:
                log.warning(
                    "job %s: could not resolve post-merge validation inputs; skipping: %s",
                    job.id,
                    exc,
                )
            else:
                validation = await merge_validate.validate_merge_result(
                    worktree,
                    base_ref,
                    validation_changed_files,
                    job_patch,
                    base_patch,
                    run_tests=True,
                )
                if not validation.passed:
                    if validation.overlap_warnings:
                        head = await gitops.head_info(worktree)
                        job.merge_delta_sha = head.get("hash", "")
                        rn._event(
                            job,
                            "merge",
                            "post-merge-overlap",
                            started,
                            summary="; ".join(validation.overlap_warnings)[:300],
                        )
                        overlap_msg = (
                            "[merge-conflict] Post-merge validation found the job's own diff "
                            "and the base's new commits (since branch-cut) both modified the "
                            "same original lines of the same file(s), and the merge kept both "
                            "edits instead of choosing one:\n"
                            + "\n".join(validation.overlap_warnings)
                            + f"\n\nThis job's diff since the merge-base:\n{job_patch}\n\n"
                            f"Base's diff since the merge-base:\n{base_patch}\n\n"
                            "Edit the affected file(s) to keep exactly one correct "
                            "implementation for each overlapping hunk (remove the "
                            "duplicated/stacked code), ensure `git status` shows no UU "
                            "entries, and make sure all tests pass. Do NOT commit — the "
                            "pipeline commits for you."
                        )
                        return await rn._rebase_conflict(job, overlap_msg)
                    test_result = validation.test_result
                    if test_result is not None:
                        resource = test_result.get("resource")
                        if resource is not None:
                            rn._record_resource(job, "merge-validate", resource)
                        test_result_safe = {
                            k: test_result[k]
                            for k in ("command", "passed", "summary", "output")
                            if k in test_result
                        }
                    else:
                        test_result_safe = None
                    gate_result = {
                        "summary": "post-merge validation failed",
                        "findings": (
                            [{"severity": "high", "note": e} for e in validation.lint_errors]
                            + (
                                [
                                    {
                                        "severity": "high",
                                        "note": (test_result_safe or {}).get(
                                            "summary", "tests failed"
                                        ),
                                    }
                                ]
                                if test_result_safe and not test_result_safe.get("passed")
                                else []
                            )
                        ),
                    }
                    failure_detail = {
                        "check_id": "merge.post_merge_validation",
                        "lint_errors": validation.lint_errors,
                        "test_result": test_result_safe,
                        "fingerprint": gate_finding_fingerprint(gate_result),
                    }
                    failure_text = "post-merge validation failed on the merged tree:\n" + "\n".join(
                        validation.lint_errors
                    )
                    if test_result_safe and not test_result_safe.get("passed"):
                        failure_text += "\n" + test_result_safe.get("summary", "")
                    rn._event(
                        job,
                        "merge",
                        "post-merge-validation-failed",
                        started,
                        summary=failure_text[:300],
                        detail=failure_detail,
                    )
                    return await rn._retry_or_fail(job, failure_text, failure_detail=failure_detail)

        detail = {"branch": job.branch, "base": base}
        if is_github:
            # GitHub-native: squash-merge the PR, then fast-forward local base.
            head_oid = await gitops.local_head_oid(worktree)
            evidence: list[dict] = []
            identity = await github.inspect_branch_pr(managed, job.branch, base=base)
            evidence.append(_pr_evidence("initial_inspection", identity))
            if not _pr_matches(identity, base=base, branch=job.branch, head_oid=head_oid) or (
                identity.state not in {github.PullRequestState.OPEN, github.PullRequestState.MERGED}
            ):
                identity, reason = await _recover_pr_identity(
                    rn,
                    job,
                    managed=managed,
                    worktree=worktree,
                    base=base,
                    head_oid=head_oid,
                    evidence=evidence,
                )
                if identity is None:
                    detail["github_evidence"] = evidence
                    rn._event(
                        job,
                        "merge",
                        "failed",
                        started,
                        summary=reason,
                        detail=detail,
                    )
                    return await rn._fail(
                        job,
                        f"GitHub PR identity recovery failed: {reason}",
                        failure_detail=detail,
                    )
            pr = identity.url
            detail.update(
                {
                    "pr_url": pr,
                    "pr_number": identity.number,
                    "head_oid": head_oid,
                    "github_evidence": evidence,
                }
            )
            # Drop the worktree *before* merging: `gh pr merge --delete-branch`
            # also deletes the local branch, and git refuses to delete a branch
            # that's still checked out in a worktree. Every commit is already on
            # the PR, so the worktree is no longer needed to complete the merge.
            await gitops.remove_worktree(managed, worktree)
            if await github.pr_identity_is_merged(managed, identity):
                # Job was interrupted after a successful merge but before
                # store.save advanced the stage to DEPLOY; skip the merge.
                log.info(
                    "job %s: PR already merged (idempotency retry); skipping merge call",
                    job.id,
                )
            else:
                res = await github.merge_pr_squash(managed, identity)
                evidence.append(
                    {
                        "action": "merge",
                        "url": identity.url,
                        "number": identity.number,
                        "ok": res.ok,
                        "stderr": res.stderr[:300],
                    }
                )
                phantom_recovered = False
                if not res.ok:
                    if not await github.pr_identity_is_merged(managed, identity):
                        if _PR_CONFLICT_RE.search(res.stderr) or _BASE_MODIFIED_RE.search(
                            res.stderr
                        ):
                            # GitHub rejected the squash-merge because origin/main
                            # advanced between our local base_has_advanced check and
                            # the actual merge call.  Re-enter the rebase self-heal
                            # loop: recreate the worktree, materialise conflict
                            # markers locally, then dispatch to fix_conflict.
                            try:
                                await gitops.git(managed, "worktree", "prune")
                                wt_res = await gitops.git(
                                    managed, "worktree", "add", str(worktree), job.branch
                                )
                                if not wt_res.ok:
                                    raise RuntimeError(f"worktree add failed: {wt_res.stderr}")
                                await gitops.git(worktree, "fetch", "origin", base)
                                merge_res = await gitops.merge_base_into_worktree(
                                    worktree, "FETCH_HEAD"
                                )
                            except Exception:
                                log.exception(
                                    "job %s: failed to recreate worktree for GitHub conflict recovery; falling through to _fail",
                                    job.id,
                                )
                            else:
                                # Only dispatch the conflict-fixer if the local re-merge
                                # actually left unmerged paths. GitHub's "not mergeable"
                                # reflects the *remote* PR branch, which the pipeline's
                                # local resolution never reached (once local/remote
                                # diverge the branch push is non-fast-forward).
                                plumbing = gitops._is_merge_plumbing_error(merge_res.stderr)
                                unmerged = await gitops.has_unmerged_paths(worktree)
                                autoresolve = None
                                if not merge_res.ok and not plumbing and unmerged:
                                    autoresolve = await conflict_autoresolve.autoresolve_conflict(
                                        worktree
                                    )
                                    if autoresolve.ok:
                                        rn._event(
                                            job,
                                            "merge",
                                            "auto-resolved",
                                            started,
                                            summary=(
                                                "auto-resolved pure-append conflict after "
                                                f"GitHub rejected the squash-merge: "
                                                f"{', '.join(autoresolve.files)}"
                                            ),
                                            detail={**detail, "pr_url": pr},
                                        )
                                if merge_res.ok or (autoresolve is not None and autoresolve.ok):
                                    # Phantom conflict (jobs #1216/#1217): the local
                                    # re-merge of a fresh origin/<base> is clean — there
                                    # are no markers to act on, GitHub is just reporting
                                    # against a stale remote PR ref (a sibling landed and
                                    # moved base after our branch was pushed). This is
                                    # not a fix attempt: force-refresh the remote branch
                                    # with the clean local merge result and re-poll,
                                    # bounded and never touching rebase_attempts.
                                    refreshed_oid = await gitops.local_head_oid(worktree)
                                    identity = None
                                    reason = "phantom-conflict recovery was not attempted"
                                    replacement_state = [False]
                                    for recovery_attempt in range(
                                        1, rn.phantom_conflict_max_attempts + 1
                                    ):
                                        identity, reason = await _recover_pr_identity(
                                            rn,
                                            job,
                                            managed=managed,
                                            worktree=worktree,
                                            base=base,
                                            head_oid=refreshed_oid,
                                            evidence=evidence,
                                            max_attempts=1,
                                            attempt_offset=recovery_attempt - 1,
                                            replacement_state=replacement_state,
                                        )
                                        if identity is None:
                                            if reason.startswith("force-refresh failed"):
                                                break
                                            continue
                                        res = await github.merge_pr_squash(managed, identity)
                                        evidence.append(
                                            {
                                                "action": "recovery_merge",
                                                "attempt": recovery_attempt,
                                                "url": identity.url,
                                                "number": identity.number,
                                                "ok": res.ok,
                                                "stderr": res.stderr[:300],
                                            }
                                        )
                                        if res.ok or await github.pr_identity_is_merged(
                                            managed, identity
                                        ):
                                            await gitops.remove_worktree(managed, worktree)
                                            break
                                        reason = (
                                            "verified recovery merge failed on attempt "
                                            f"{recovery_attempt}: {res.stderr[:300]}"
                                        )
                                        identity = None
                                    if identity is not None:
                                        pr = identity.url
                                        detail.update(
                                            {
                                                "pr_url": pr,
                                                "pr_number": identity.number,
                                                "head_oid": refreshed_oid,
                                            }
                                        )
                                        phantom_recovered = True
                                        # fall through to the post-merge success tail below
                                    else:
                                        rn._event(
                                            job,
                                            "merge",
                                            "failed",
                                            started,
                                            summary=(f"phantom conflict recovery failed: {reason}"),
                                            detail=detail,
                                        )
                                        return await rn._fail(
                                            job,
                                            f"GitHub phantom-conflict recovery failed: {reason}",
                                        )
                                    # else: force-push failed or a non-conflict error was
                                    # surfaced; fall through to the generic failure below.
                                elif plumbing or not unmerged:
                                    reason = "plumbing error" if plumbing else "no unmerged paths"
                                    log.warning(
                                        "job %s: GitHub not-mergeable but no local conflict (%s), retrying; stderr: %s",
                                        job.id,
                                        reason,
                                        merge_res.stderr[:200],
                                    )
                                    job.rebase_attempts += 1
                                    if job.rebase_attempts >= rn.rebase_max_attempts:
                                        return await rn._fail(
                                            job,
                                            f"GitHub reported not-mergeable but no local conflict after "
                                            f"{job.rebase_attempts} attempt(s) ({reason}); the remote PR "
                                            f"branch has likely diverged from local: {merge_res.stderr[:300]}",
                                        )
                                    backoff = min(30 * (2 ** (job.rebase_attempts - 1)), 600)
                                    job.rebase_retry_after = time.time() + backoff
                                    job.stage = Stage.SECURITY
                                    job.status = JobStatus.PENDING
                                    job.owner = ""
                                    job.lease_until = 0.0
                                    rn.store.save(job)
                                    return
                                else:
                                    gh_head = await gitops.head_info(worktree)
                                    job.merge_delta_sha = gh_head.get("hash", "")
                                    conflict_msg = (
                                        f"[merge-conflict] Conflict markers (<<<<<<<, =======, >>>>>>>) are present "
                                        f"in the worktree after GitHub rejected the squash-merge against {base}. "
                                        f"Conflicted files:\n{merge_res.stderr}\n\n"
                                        f"Resolve each file by editing out the conflict markers (keep the correct "
                                        f"merged code), ensure `git status` shows no UU entries, and make sure all "
                                        f"tests pass. Do NOT commit — the pipeline commits for you."
                                    )
                                    rn._event(
                                        job,
                                        "merge",
                                        "conflict",
                                        started,
                                        summary=f"GitHub conflict: {res.stderr[:300]}",
                                        detail={**detail, "pr_url": pr},
                                    )
                                    return await rn._rebase_conflict(job, conflict_msg)
                        if not phantom_recovered:
                            rn._event(
                                job,
                                "merge",
                                "failed",
                                started,
                                summary=f"PR merge failed: {res.stderr[:300]}",
                                detail={**detail, "pr_url": pr},
                            )
                            return await rn._fail(
                                job,
                                f"PR merge failed: {res.stderr}",
                                failure_detail={**detail, "pr_url": pr},
                            )
                    log.warning(
                        "pr_merge_squash non-zero but PR already merged; continuing as success"
                    )
            try:
                await github.sync_base(managed, base)
            except Exception as e:
                log.warning("sync_base failed (best-effort): %s", e)
            try:
                await gitops.git(managed, "branch", "-D", job.branch)
            except Exception as e:
                log.warning("local branch delete failed (best-effort): %s", e)
            detail.update({"pr_url": pr, "merged_via": "github"})
            summary = f"squash-merged PR into {base}"
            tail = f"\n{pr}" if pr else ""
        else:
            res, merge_record = await gitops.merge_branch_measured(managed, job.branch)
            if not res.ok:
                rn._event(
                    job,
                    "merge",
                    "failed",
                    started,
                    summary=f"merge failed: {res.stderr[:300]}",
                    detail=detail,
                )
                return await rn._fail(job, f"merge failed: {res.stderr}")
            await gitops.remove_worktree(managed, worktree)
            detail["merged_via"] = "local"
            summary = f"merged into {base} (local)"
            tail = ""
    finally:
        rn.store.release_merge_lock(job.repo_path, _owner)
    # Post-lock: the merge itself is complete. Nothing below may extend the
    # per-repo serialization window — the implementation summary in particular
    # is an AI call and can be slow.
    rn._event(job, "merge", "done", started, summary=summary, detail=detail)
    if merge_record is None:
        merge_record = resources.ResourceRecord(
            cpu_seconds=None,
            peak_rss_bytes=None,
            io_read_bytes=None,
            io_write_bytes=None,
            wall_seconds=time.time() - t_merge_start,
            sampled_at=resources._now_iso(),
        )
    rn._record_resource(job, "merge", merge_record)
    try:
        backend = build_backend("claude", "claude-haiku-4-5-20251001", config=rn.config)
        job.implementation_summary = await impl_summary.generate_implementation_summary(
            managed, base, backend=backend
        )
    except Exception as exc:
        log.warning("job %s: implementation summary failed (best-effort): %s", job.id, exc)
    activation = _activation_from_plan(job.plan)
    if activation is not None:
        job.implementation_summary = _append_activation_note(
            job.implementation_summary or "", activation
        )
        if activation["config_change_required"]:
            try:
                followup = _file_activation_followup(rn, job, activation)
                rn.store.add_event(
                    job.id,
                    "merge",
                    "info",
                    summary=f"filed activation follow-up job #{followup.id}",
                    detail={"activation_follow_up_job_id": followup.id, **activation},
                )
                await rn.notify(
                    job.chat_id,
                    f"⚙️ Job #{job.id}: filed activation follow-up job #{followup.id}.",
                    project_id=job.project_id,
                    job_id=job.id,
                )
            except Exception as exc:
                log.warning("job %s: activation follow-up failed (best-effort): %s", job.id, exc)
    title = job.title or job.idea[:80]
    if not decisions.is_janitorial_job(title=title, source_meta=job.source_meta):
        try:
            files_touched = [
                row["path"] for row in await gitops.numstat(managed, f"{base}^..{base}")
            ]
            await decisions.record_decision(
                managed,
                base,
                rn.worktrees,
                job_id=job.id,
                title=title,
                summary=job.implementation_summary or "",
                files_touched=files_touched,
            )
        except Exception as exc:
            log.warning("job %s: decision recording failed (best-effort): %s", job.id, exc)
    job.stage = Stage.DEPLOY
    job.status = JobStatus.PENDING
    rn.store.save(job)
    n = f" after {job.attempts} fix(es)" if job.attempts else ""
    await rn.notify(
        job.chat_id,
        f"🔀 Job #{job.id} {summary}{n}. Deploying…{tail}",
        project_id=job.project_id,
        job_id=job.id,
    )
    return
