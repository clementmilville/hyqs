"""Integration tests: deterministic auto-unblock of dependency-blocked jobs.

The 2026-07-18 #1135 incident showed a root job's failure permanently killing its
dependents with no recovery path. The control logic that fixes this already
exists — classify_failure()'s dependency_blocked rule, remediate_dependency_blocked(),
and _reconcile_blocked_dependents() (see hyqs/pipeline/supervisor.py) — and is
already unit-tested with a MagicMock JobStore in tests/test_auto_requeue_after_fix.py
and tests/test_pipeline_loop_fixes.py. What was missing is proof the full chain
works together against a real store: root fails -> dependents auto-blocked -> root
retried to done -> dependents auto-requeue with no manual retry call.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hyqs.pipeline import supervisor
from hyqs.pipeline.models import JobStatus, Stage, Usage
from hyqs.pipeline.runner import PipelineRunner
from hyqs.pipeline.supervisor import FailureClass, classify_failure


@pytest.fixture
def dep_env(store):
    """A per-test fake repo path + cleanup of every row/meta key created under it."""
    repo_path = f"/tmp/test-dep-unblock-{uuid.uuid4()}"
    created: list[int] = []

    def make_job(idea: str = "idea"):
        job = store.create(idea=idea, repo_path=repo_path, chat_id=1)
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
            meta_keys = [f"supervisor_requeue:{jid}" for jid in created] + [
                f"supervisor_notified:{jid}" for jid in created
            ]
            conn.execute("DELETE FROM meta WHERE key = ANY(%s)", (meta_keys,))
            conn.execute("DELETE FROM jobs WHERE id = ANY(%s)", (created,))
        row = conn.execute("SELECT id FROM projects WHERE repo_path = %s", (repo_path,)).fetchone()
        if row:
            conn.execute("DELETE FROM project_agents WHERE project_id = %s", (row["id"],))
            conn.execute("DELETE FROM projects WHERE id = %s", (row["id"],))


def _run_janitor_scan(store) -> None:
    """Run the real janitor scan with host-touching sweeps stubbed out.

    _janitor_scan unconditionally runs gitops.gc_orphaned_artifacts (git worktree/
    branch GC) and _docker_sweep (docker ps/rm) at the end of every pass, and can
    reach _ai_diagnose_and_act (a real AI provider call) for judgment-class
    failures. None of those are scoped to this test's rows — they act on the whole
    host/DB — so they must never run for real from a test.
    """
    with (
        patch("hyqs.pipeline.supervisor._docker_sweep", new=AsyncMock()),
        patch("hyqs.pipeline.gitops.gc_orphaned_artifacts", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._ai_diagnose_and_act", new=AsyncMock(return_value=False)),
    ):
        asyncio.run(supervisor._janitor_scan(store, config=MagicMock(), notify=AsyncMock()))


def _mark_dead_lettered(store, job_id: int) -> None:
    store.mark_supervisor_notified(job_id)
    store.record_supervisor_event(job_id, "dead_lettered", "genuine_code", detail="test")


def test_root_retried_to_done_auto_requeues_blocked_dependents(store, dep_env):
    root = dep_env(idea="root")
    dep_a = dep_env(idea="dependent a")
    dep_b = dep_env(idea="dependent b")
    store.add_job_dependency(dep_a.id, root.id)
    store.add_job_dependency(dep_b.id, root.id)

    # Root fails; the janitor's dependency-propagation pass terminally fails
    # both dependents with a propagated "blocked: dependency #N" failure.
    root.status = JobStatus.FAILED
    store.save(root)
    _mark_dead_lettered(store, root.id)

    asyncio.run(supervisor._reconcile_blocked_dependents(store, AsyncMock()))

    expected_failure = f"blocked: dependency #{root.id} failed and can no longer complete"
    for dep in (dep_a, dep_b):
        refreshed = store.get(dep.id)
        assert refreshed.status == JobStatus.FAILED
        assert refreshed.failure == expected_failure
        assert classify_failure(refreshed) == FailureClass.dependency_blocked

    # Root is retried (out of band) and lands DONE.
    store.reconcile_to_done(root.id)

    # One janitor pass, no manual per-dependent retry call.
    _run_janitor_scan(store)

    for dep in (dep_a, dep_b):
        refreshed = store.get(dep.id)
        assert refreshed.status == JobStatus.PENDING
        assert refreshed.stage == Stage.QUEUED
        assert store.get_unsatisfied_deps(dep.id) == []
        assert root.id in store.get_dependencies(dep.id)


def test_supervisor_notification_alone_does_not_fail_dependent(store, dep_env):
    root = dep_env(idea="root under active remediation")
    dependent = dep_env(idea="dependent waiting for root")
    store.add_job_dependency(dependent.id, root.id)
    root.status = JobStatus.FAILED
    store.save(root)
    store.mark_supervisor_notified(root.id)

    asyncio.run(supervisor._reconcile_blocked_dependents(store, AsyncMock()))

    refreshed = store.get(dependent.id)
    assert refreshed.status == JobStatus.PENDING
    assert refreshed.failure_code is None

    _mark_dead_lettered(store, root.id)
    asyncio.run(supervisor._reconcile_blocked_dependents(store, AsyncMock()))

    refreshed = store.get(dependent.id)
    assert refreshed.status == JobStatus.FAILED
    assert refreshed.failure_code == "dependency_blocked"


def test_dependent_own_failure_not_auto_requeued_by_unrelated_root(store, dep_env):
    unrelated_root = dep_env(idea="unrelated root")

    dep_done = dep_env(idea="already-done dependency")
    dep_done.status = JobStatus.DONE
    store.save(dep_done)

    job = dep_env(idea="job with its own genuine failure")
    store.add_job_dependency(job.id, dep_done.id)
    job.status = JobStatus.FAILED
    job.error = "gave up after 3 fix attempt(s)."
    store.save(job)
    assert classify_failure(store.get(job.id)) == FailureClass.genuine_code

    unrelated_root.status = JobStatus.FAILED
    store.save(unrelated_root)
    store.reconcile_to_done(unrelated_root.id)

    _run_janitor_scan(store)

    refreshed = store.get(job.id)
    assert refreshed.status == JobStatus.FAILED
    assert classify_failure(refreshed) != FailureClass.dependency_blocked


def test_dependency_blocked_auto_unblock_stops_at_dead_letter_cap(store, dep_env):
    job = dep_env(idea="flaky dependent")
    failure_text = "blocked: dependency #999999 failed and can no longer complete"
    notify = AsyncMock()

    for _ in range(2):
        current = store.get(job.id)
        current.status = JobStatus.FAILED
        current.failure = failure_text
        store.save(current)

        asyncio.run(
            supervisor.remediate_dependency_blocked(
                store, store.get(job.id), MagicMock(), notify, dead_letter_cap=2
            )
        )
        assert store.get(job.id).status == JobStatus.PENDING

    assert store.supervisor_requeue_count(job.id) == 2

    # Third failure hits the cap: no further requeue, dead-lettered instead.
    # Fresh mock — the successful-requeue notifications above aren't the ones
    # under test here.
    notify = AsyncMock()
    current = store.get(job.id)
    current.status = JobStatus.FAILED
    current.failure = failure_text
    store.save(current)

    asyncio.run(
        supervisor.remediate_dependency_blocked(
            store, store.get(job.id), MagicMock(), notify, dead_letter_cap=2
        )
    )
    assert store.get(job.id).status == JobStatus.FAILED
    notify.assert_awaited_once()

    # A further call at the cap remains a no-op and does not re-notify.
    asyncio.run(
        supervisor.remediate_dependency_blocked(
            store, store.get(job.id), MagicMock(), notify, dead_letter_cap=2
        )
    )
    assert store.get(job.id).status == JobStatus.FAILED
    notify.assert_awaited_once()

    events = store.list_supervisor_events_for_job(job.id)
    assert any(e["action"] == "dead_lettered" for e in events)


def test_unblock_ready_dependents_fires_immediately_without_janitor_scan(store, dep_env):
    root = dep_env(idea="root")
    dep_a = dep_env(idea="dependent a")
    dep_b = dep_env(idea="dependent b")
    store.add_job_dependency(dep_a.id, root.id)
    store.add_job_dependency(dep_b.id, root.id)

    root.status = JobStatus.FAILED
    store.save(root)
    _mark_dead_lettered(store, root.id)
    asyncio.run(supervisor._reconcile_blocked_dependents(store, AsyncMock()))
    for dep in (dep_a, dep_b):
        assert classify_failure(store.get(dep.id)) == FailureClass.dependency_blocked

    # Root recovers (e.g. a manual retry lands DONE) — call the immediate
    # unblock hook directly, with no janitor scan in between.
    store.reconcile_to_done(root.id)
    asyncio.run(supervisor.unblock_ready_dependents(store, root.id, MagicMock(), AsyncMock()))

    for dep in (dep_a, dep_b):
        refreshed = store.get(dep.id)
        assert refreshed.status == JobStatus.PENDING
        assert refreshed.stage == Stage.QUEUED


def test_unblock_ready_dependents_leaves_still_blocked_dependent_alone(store, dep_env):
    root = dep_env(idea="root")
    other_root = dep_env(idea="other root, still failing")
    dependent = dep_env(idea="dependent on both roots")
    store.add_job_dependency(dependent.id, root.id)
    store.add_job_dependency(dependent.id, other_root.id)

    other_root.status = JobStatus.FAILED
    store.save(other_root)
    _mark_dead_lettered(store, other_root.id)

    dependent.status = JobStatus.FAILED
    dependent.failure = f"blocked: dependency #{other_root.id} failed and can no longer complete"
    store.save(dependent)
    assert classify_failure(store.get(dependent.id)) == FailureClass.dependency_blocked

    # Only `root` completes; `other_root` is still failed, so the dependent's
    # deps are not all satisfied yet. reconcile_to_done only flips a FAILED
    # job to DONE, so `root` must be FAILED first.
    root.status = JobStatus.FAILED
    store.save(root)
    store.reconcile_to_done(root.id)
    asyncio.run(supervisor.unblock_ready_dependents(store, root.id, MagicMock(), AsyncMock()))

    refreshed = store.get(dependent.id)
    assert refreshed.status == JobStatus.FAILED
    assert store.get_unsatisfied_deps(dependent.id) == [other_root.id]


def test_unblock_ready_dependents_is_idempotent_on_double_fire(store, dep_env):
    root = dep_env(idea="root")
    dep = dep_env(idea="dependent")
    store.add_job_dependency(dep.id, root.id)

    root.status = JobStatus.FAILED
    store.save(root)
    _mark_dead_lettered(store, root.id)
    asyncio.run(supervisor._reconcile_blocked_dependents(store, AsyncMock()))
    assert classify_failure(store.get(dep.id)) == FailureClass.dependency_blocked

    store.reconcile_to_done(root.id)

    # Simulate two workers observing the same completion concurrently: call
    # the hook twice in a row.
    asyncio.run(supervisor.unblock_ready_dependents(store, root.id, MagicMock(), AsyncMock()))
    asyncio.run(supervisor.unblock_ready_dependents(store, root.id, MagicMock(), AsyncMock()))

    refreshed = store.get(dep.id)
    assert refreshed.status == JobStatus.PENDING
    assert refreshed.stage == Stage.QUEUED
    # The second call found the dependent no longer FAILED/dependency_blocked,
    # so it was a no-op — the requeue counter only incremented once.
    assert store.supervisor_requeue_count(dep.id) == 1


@pytest.mark.parametrize("terminal_status", [JobStatus.FAILED, JobStatus.CANCELLED])
@pytest.mark.parametrize("archived", [False, True])
def test_terminal_parent_edge_survives_until_done_and_then_recovers(
    store, dep_env, terminal_status, archived
):
    root = dep_env(idea="recoverable root")
    dependent = dep_env(idea="blocked dependent")
    store.add_job_dependency(dependent.id, root.id)
    root.status = terminal_status
    store.save(root)
    if archived:
        store.set_archived(root.id, True)

    dependent.status = JobStatus.FAILED
    dependent.failure = f"blocked: dependency #{root.id} failed and can no longer complete"
    store.save(dependent)

    assert store.get_dependencies(dependent.id) == [root.id]
    assert store.get_unsatisfied_deps(dependent.id) == [root.id]
    asyncio.run(supervisor.unblock_ready_dependents(store, root.id, MagicMock(), AsyncMock()))
    assert store.get(dependent.id).status == JobStatus.FAILED

    if terminal_status == JobStatus.FAILED:
        store.reconcile_to_done(root.id)
    else:
        recovered = store.retry(root.id)
        assert recovered is not None
        recovered.status = JobStatus.DONE
        recovered.stage = Stage.DONE
        store.save(recovered)
    asyncio.run(supervisor.unblock_ready_dependents(store, root.id, MagicMock(), AsyncMock()))

    assert store.get_dependencies(dependent.id) == [root.id]
    assert store.get_unsatisfied_deps(dependent.id) == []
    assert store.get(dependent.id).status == JobStatus.PENDING


def test_split_repoint_blocks_until_every_child_is_done(store, dep_env):
    parent = dep_env(idea="split parent")
    child_a = dep_env(idea="split child a")
    child_b = dep_env(idea="split child b")
    dependent = dep_env(idea="dependent")
    store.add_job_dependency(dependent.id, parent.id)

    store.repoint_split_dependents(parent.id, [child_a.id, child_b.id])
    parent.status = JobStatus.CANCELLED
    store.save(parent)
    child_a.status = JobStatus.DONE
    child_a.stage = Stage.DONE
    store.save(child_a)

    assert store.get_dependencies(dependent.id) == sorted([child_a.id, child_b.id])
    assert store.get_unsatisfied_deps(dependent.id) == [child_b.id]
    assert store.claimable() is not None
    assert store.claimable().id != dependent.id

    child_b.status = JobStatus.DONE
    child_b.stage = Stage.DONE
    store.save(child_b)

    assert store.get_unsatisfied_deps(dependent.id) == []
    assert store.claimable().id == dependent.id


def _verification_runner(store, tmp_path) -> PipelineRunner:
    config = SimpleNamespace(model="test-model", data_dir=str(tmp_path / "data"))
    return PipelineRunner(store, config, notify=AsyncMock())


def _verification_backend(verdict: str, evidence: str) -> MagicMock:
    backend = MagicMock()
    backend.run = AsyncMock(
        return_value=SimpleNamespace(
            text=(
                "<<<RESULT_JSON>>>"
                f'{{"verdict":"{verdict}","evidence":"{evidence}"}}'
                "<<<END_RESULT>>>"
            ),
            usage=Usage(),
        )
    )
    return backend


def _run_verification(rn, job, backend, tmp_path):
    with (
        patch("hyqs.pipeline.runner.gitops.remove_worktree", new_callable=AsyncMock),
        patch(
            "hyqs.pipeline.runner.gitops.fresh_base",
            new_callable=AsyncMock,
            return_value="main",
        ),
        patch(
            "hyqs.pipeline.runner.gitops.create_detached_worktree",
            new_callable=AsyncMock,
            return_value=MagicMock(ok=True),
        ),
    ):
        asyncio.run(
            rn._verify_already_satisfied(
                job,
                "2026-07-27T00:00:00Z",
                managed=tmp_path / "managed",
                worktree=tmp_path / f"job-{job.id}",
                base="main",
                backend=backend,
            )
        )


def test_failed_already_satisfied_verification_keeps_dependent_blocked(store, dep_env, tmp_path):
    root = dep_env("Add GET /required")
    dependent = dep_env("Use GET /required")
    store.add_job_dependency(dependent.id, root.id)
    root.status = JobStatus.FAILED
    store.save(root)
    _mark_dead_lettered(store, root.id)
    asyncio.run(supervisor._reconcile_blocked_dependents(store, AsyncMock()))
    root.status = JobStatus.RUNNING
    root.stage = Stage.BUILD
    store.save(root)

    rn = _verification_runner(store, tmp_path)
    _run_verification(
        rn,
        root,
        _verification_backend("fail", "GET /required is not registered"),
        tmp_path,
    )

    assert store.get(root.id).status == JobStatus.FAILED
    assert store.get(dependent.id).status == JobStatus.FAILED
    assert store.get_unsatisfied_deps(dependent.id) == [root.id]
    events = store.list_events(root.id)
    assert events[-1]["detail"]["evidence"] == "GET /required is not registered"


def test_verified_already_satisfied_unblocks_dependent(store, dep_env, tmp_path):
    root = dep_env("Expose existing health route")
    dependent = dep_env("Consume health route")
    store.add_job_dependency(dependent.id, root.id)
    root.status = JobStatus.RUNNING
    root.stage = Stage.BUILD
    store.save(root)

    rn = _verification_runner(store, tmp_path)
    _run_verification(
        rn,
        root,
        _verification_backend("pass", "GET /health route and handler are present"),
        tmp_path,
    )

    refreshed_root = store.get(root.id)
    assert refreshed_root.status == JobStatus.DONE
    assert refreshed_root.resolution == "already-satisfied"
    refreshed_dependent = store.get(dependent.id)
    assert refreshed_dependent.status == JobStatus.PENDING
    assert refreshed_dependent.stage == Stage.QUEUED
    events = store.list_events(root.id)
    assert any(
        event["detail"].get("evidence") == "GET /health route and handler are present"
        for event in events
    )
