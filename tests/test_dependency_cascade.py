"""Integration tests: dependency-cascade failures self-heal without re-filing.

A 7-job epic chain was entirely wiped out when its ROOT job was superseded-by-split
and one split child dead-lettered: every downstream job cascaded to terminal FAILED
("blocked: dependency #N failed and can no longer complete") and only came back
after the whole chain was re-filed by hand.

Two gaps made that possible:

1. Multi-hop chains (A -> B -> C) only healed one hop per recovery signal — fixing
   the root required a human to also nudge each downstream job. Covered by
   ``test_two_hop_chain_recovers_end_to_end``.
2. ``list_split_parents_with_pending_dependents``/``repoint_split_dependents``
   (see hyqs/pipeline/store.py) only ever ran once, from runner.py's
   ``_reconcile_startup`` at process boot. A dependent whose edge went stale
   *after* boot (the split parent archived later, or a split child dead-lettering
   after boot) had no periodic path back to a live edge, so
   ``_reconcile_blocked_dependents`` treated the archived split parent as a
   genuinely terminal, replacement-less dependency and killed the dependent for
   good. Covered by ``test_split_child_dead_letter_then_lineage_completion_recovers_dependent``.

``supervisor._repoint_split_orphans`` now runs every janitor pass (before
``_reconcile_blocked_dependents``), repointing any dependent still edged to a
superseded-by-split parent onto its recorded children first. A dependency with no
split lineage at all is untouched by that step: an archived dependency no longer
terminally fails the dependent (a human may restore or re-point it later) — the
dependent is left PENDING and alerted instead — covered by
``test_archived_replacement_less_dependency_leaves_dependent_pending_and_alerts``.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hyqs.pipeline import supervisor
from hyqs.pipeline.models import JobStatus, Stage
from hyqs.pipeline.supervisor import FailureClass, classify_failure


@pytest.fixture
def dep_env(store):
    """A per-test fake repo path + cleanup of every row/meta key created under it."""
    repo_path = f"/tmp/test-dep-cascade-{uuid.uuid4()}"
    created: list[int] = []

    def make_job(idea: str = "idea", **kwargs):
        job = store.create(idea=idea, repo_path=repo_path, chat_id=1, **kwargs)
        created.append(job.id)
        return job

    yield make_job

    with store._pool.connection() as conn:
        if created:
            conn.execute(
                "DELETE FROM job_dependencies WHERE job_id = ANY(%s) OR depends_on_job_id = ANY(%s)",
                (created, created),
            )
            conn.execute("DELETE FROM supervisor_events WHERE job_id = ANY(%s)", (created,))
            conn.execute("DELETE FROM job_events WHERE job_id = ANY(%s)", (created,))
            meta_keys = (
                [f"supervisor_requeue:{jid}" for jid in created]
                + [f"supervisor_notified:{jid}" for jid in created]
                + [f"archived_dependency_notified:{jid}" for jid in created]
            )
            conn.execute("DELETE FROM meta WHERE key = ANY(%s)", (meta_keys,))
            conn.execute("DELETE FROM jobs WHERE id = ANY(%s)", (created,))
        row = conn.execute("SELECT id FROM projects WHERE repo_path = %s", (repo_path,)).fetchone()
        if row:
            conn.execute("DELETE FROM project_agents WHERE project_id = %s", (row["id"],))
            conn.execute("DELETE FROM projects WHERE id = %s", (row["id"],))


def _run_janitor_scan(store) -> AsyncMock:
    """Run the real janitor scan with host-touching sweeps stubbed out.

    _janitor_scan unconditionally runs gitops.gc_orphaned_artifacts (git worktree/
    branch GC) and _docker_sweep (docker ps/rm) at the end of every pass, and can
    reach _ai_diagnose_and_act (a real AI provider call) for judgment-class
    failures. None of those are scoped to this test's rows — they act on the whole
    host/DB — so they must never run for real from a test. Return the notification
    mock so callers can inspect alerts emitted by the scan.
    """
    notify = AsyncMock()
    with (
        patch("hyqs.pipeline.supervisor._docker_sweep", new=AsyncMock()),
        patch("hyqs.pipeline.gitops.gc_orphaned_artifacts", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._ai_diagnose_and_act", new=AsyncMock(return_value=False)),
    ):
        asyncio.run(supervisor._janitor_scan(store, config=MagicMock(), notify=notify))
    return notify


def _mark_dead_lettered(store, job_id: int) -> None:
    store.mark_supervisor_notified(job_id)
    store.record_supervisor_event(job_id, "dead_lettered", "genuine_code", detail="test")


def test_two_hop_chain_recovers_end_to_end(store, dep_env):
    a = dep_env(idea="A (root)")
    b = dep_env(idea="B (depends on A)")
    c = dep_env(idea="C (depends on B)")
    store.add_job_dependency(b.id, a.id)
    store.add_job_dependency(c.id, b.id)

    a.status = JobStatus.FAILED
    store.save(a)
    _mark_dead_lettered(store, a.id)

    # One or two passes is enough to cascade the whole chain: each pass re-fetches
    # dependency status live, so B failing (and getting notified) in the same pass
    # unblocks C's classification too.
    for _ in range(2):
        _run_janitor_scan(store)

    for dep in (b, c):
        refreshed = store.get(dep.id)
        assert refreshed.status == JobStatus.FAILED
        assert classify_failure(refreshed) == FailureClass.dependency_blocked

    # Root is retried (out of band) and lands DONE. No manual per-dependent retry.
    store.reconcile_to_done(a.id)
    _run_janitor_scan(store)

    refreshed_b = store.get(b.id)
    assert refreshed_b.status == JobStatus.PENDING
    assert refreshed_b.stage == Stage.QUEUED
    assert store.get_unsatisfied_deps(b.id) == []

    # C only unblocks once B actually lands DONE — simulate that completion.
    refreshed_b.status = JobStatus.DONE
    store.save(refreshed_b)
    _run_janitor_scan(store)

    refreshed_c = store.get(c.id)
    assert refreshed_c.status == JobStatus.PENDING
    assert refreshed_c.stage == Stage.QUEUED
    assert store.get_unsatisfied_deps(c.id) == []


def test_manual_resolution_satisfies_dependency_and_requeues_blocked_job(store, dep_env):
    dependency = dep_env(idea="manually repaired dependency")
    dependent = dep_env(idea="dependent waiting on manual repair")
    store.add_job_dependency(dependent.id, dependency.id)

    dependency.status = JobStatus.FAILED
    store.save(dependency)
    _mark_dead_lettered(store, dependency.id)
    _run_janitor_scan(store)
    assert store.get(dependent.id).status == JobStatus.FAILED

    assert store.set_job_resolution(dependency.id, "resolved") is True
    assert store.get_unsatisfied_deps(dependent.id) == []
    _run_janitor_scan(store)

    refreshed = store.get(dependent.id)
    assert refreshed.status == JobStatus.PENDING
    assert refreshed.stage == Stage.QUEUED


def test_manual_resolution_does_not_requeue_resolved_blocked_job(store, dep_env):
    prerequisite = dep_env(idea="prerequisite repaired manually")
    resolved_job = dep_env(idea="resolved job formerly waiting on prerequisite")
    store.add_job_dependency(resolved_job.id, prerequisite.id)

    prerequisite.status = JobStatus.FAILED
    store.save(prerequisite)
    _mark_dead_lettered(store, prerequisite.id)
    _run_janitor_scan(store)
    assert store.get(resolved_job.id).status == JobStatus.FAILED

    assert store.set_job_resolution(resolved_job.id, "resolved") is True
    assert store.set_job_resolution(prerequisite.id, "resolved") is True
    _run_janitor_scan(store)

    refreshed = store.get(resolved_job.id)
    assert refreshed.status == JobStatus.FAILED
    assert refreshed.resolution == "resolved"


def test_successful_replacement_resolution_requeues_original_dependent(store, dep_env):
    incident = dep_env(idea="failed incident")
    dependent = dep_env(idea="dependent on failed incident")
    replacement = dep_env(
        idea="successful replacement",
        source_meta={"fix_for": incident.id},
    )
    store.add_job_dependency(dependent.id, incident.id)

    incident.status = JobStatus.FAILED
    store.save(incident)
    _mark_dead_lettered(store, incident.id)
    _run_janitor_scan(store)
    assert store.get(dependent.id).status == JobStatus.FAILED

    replacement.status = JobStatus.DONE
    store.save(replacement)
    assert store.get(incident.id).resolution == "resolved"
    assert store.get_unsatisfied_deps(dependent.id) == []
    _run_janitor_scan(store)

    assert store.get(dependent.id).status == JobStatus.PENDING


def test_split_child_dead_letter_then_lineage_completion_recovers_dependent(store, dep_env):
    parent = dep_env(idea="P (split parent)")
    child1 = dep_env(idea="C1 (split child)")
    child2 = dep_env(idea="C2 (split child)")
    dependent = dep_env(idea="D (depends on P)")
    store.add_job_dependency(dependent.id, parent.id)

    # Simulate P being superseded-by-split and later archived, leaving D's edge
    # stale — exactly the scenario that used to only get healed once, at boot.
    parent.status = JobStatus.CANCELLED
    parent.resolution = "superseded-by-split"
    store.save(parent)
    store.add_event(
        parent.id,
        "plan",
        "cancelled",
        summary="plan split into 2 job(s)",
        detail={"superseded_by": [child1.id, child2.id]},
    )
    store.set_archived(parent.id, True)

    _run_janitor_scan(store)

    # D was repointed onto the live split children before the archived-dependency
    # terminal check ran — it must not be dead with a "was archived" failure.
    refreshed_dependent = store.get(dependent.id)
    assert refreshed_dependent.status == JobStatus.PENDING
    assert store.get_dependencies(dependent.id) == sorted([child1.id, child2.id])

    # One split child dead-letters.
    child1_row = store.get(child1.id)
    child1_row.status = JobStatus.FAILED
    store.save(child1_row)
    _mark_dead_lettered(store, child1.id)

    _run_janitor_scan(store)

    refreshed_dependent = store.get(dependent.id)
    assert refreshed_dependent.status == JobStatus.FAILED
    assert "archived" not in refreshed_dependent.failure
    assert f"dependency #{child1.id}" in refreshed_dependent.failure
    assert classify_failure(refreshed_dependent) == FailureClass.dependency_blocked

    # The lineage is fixed: both split children land DONE.
    store.reconcile_to_done(child1.id)
    child2_row = store.get(child2.id)
    child2_row.status = JobStatus.DONE
    store.save(child2_row)

    _run_janitor_scan(store)

    refreshed_dependent = store.get(dependent.id)
    assert refreshed_dependent.status == JobStatus.PENDING
    assert refreshed_dependent.stage == Stage.QUEUED
    assert store.get_unsatisfied_deps(dependent.id) == []


def test_archived_replacement_less_dependency_leaves_dependent_pending_and_alerts(store, dep_env):
    dependency = dep_env(idea="dependency with no split lineage")
    dependent = dep_env(idea="dependent on a plain archived job")
    store.add_job_dependency(dependent.id, dependency.id)

    store.set_archived(dependency.id, True)

    assert store.get_meta(f"archived_dependency_notified:{dependency.id}", "0") == "0"
    notify = _run_janitor_scan(store)

    # The dependent is left untouched (still PENDING, no failure recorded) and
    # a throttled alert was sent instead of a terminal failure.
    refreshed = store.get(dependent.id)
    assert refreshed.status == JobStatus.PENDING
    assert refreshed.failure_code != "dependency_blocked"
    assert store.get_meta(f"archived_dependency_notified:{dependency.id}", "0") != "0"
    notify.assert_awaited_once()
    notification = notify.await_args.args[1]
    assert f"dependency #{dependency.id}" in notification
    assert notify.await_args.kwargs["event_type"] == "needs_attention"
    assert notify.await_args.kwargs["reason"] == "archived_dependency"

    # The persisted cooldown prevents a duplicate alert on a second pass.
    second_notify = _run_janitor_scan(store)
    second_notify.assert_not_awaited()
    refreshed_again = store.get(dependent.id)
    assert refreshed_again.status == JobStatus.PENDING


def test_archived_dependency_chain_recovers_after_rewire_without_manual_retry(store, dep_env):
    a = dep_env(idea="A (archived root)")
    b = dep_env(idea="B (depends on A)")
    c = dep_env(idea="C (depends on B)")
    d = dep_env(idea="D (depends on C)")
    store.add_job_dependency(b.id, a.id)
    store.add_job_dependency(c.id, b.id)
    store.add_job_dependency(d.id, c.id)

    store.set_archived(a.id, True)
    _run_janitor_scan(store)

    for dependent in (b, c, d):
        refreshed = store.get(dependent.id)
        assert refreshed.status == JobStatus.PENDING
        assert refreshed.failure_code != "dependency_blocked"

    replacement_a = dep_env(idea="A-prime (replacement root)")
    store.remove_job_dependency(b.id, a.id)
    store.add_job_dependency(b.id, replacement_a.id)
    replacement_a.status = JobStatus.DONE
    store.save(replacement_a)
    _run_janitor_scan(store)

    refreshed_b = store.get(b.id)
    assert refreshed_b.status == JobStatus.PENDING
    assert refreshed_b.stage == Stage.QUEUED
    assert store.get_unsatisfied_deps(b.id) == []

    refreshed_b.status = JobStatus.DONE
    store.save(refreshed_b)
    _run_janitor_scan(store)

    refreshed_c = store.get(c.id)
    assert refreshed_c.status == JobStatus.PENDING
    assert refreshed_c.stage == Stage.QUEUED
    assert store.get_unsatisfied_deps(c.id) == []

    refreshed_c.status = JobStatus.DONE
    store.save(refreshed_c)
    _run_janitor_scan(store)

    refreshed_d = store.get(d.id)
    assert refreshed_d.status == JobStatus.PENDING
    assert refreshed_d.stage == Stage.QUEUED
    assert store.get_unsatisfied_deps(d.id) == []
