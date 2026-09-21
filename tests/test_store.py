"""Tests for JobStore.list_pending_with_unsatisfied_deps (real Postgres, no mocks).

Jobs are created through store.create (which auto-creates a project for the
fake repo path), so every test cleans up its rows in a fixture teardown — an
earlier version of this file leaked a fake project + jobs into the database
the suite pointed at (the '/fake/repo' incident).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import bcrypt
import psycopg
import pytest
from cryptography.fernet import Fernet

import hyqs.pipeline.store as store_module
from hyqs.pipeline.collision import (
    ScopeAmendmentAccepted,
    ScopeAmendmentDecision,
    ScopeAmendmentRejected,
)
from hyqs.pipeline.models import (
    CRITICAL_PATH_BOOST_PER_DEPENDENT,
    CRITICAL_PATH_BOOST_PER_DEPTH,
    MAX_CRITICAL_PATH_BOOST,
    MAX_PRIORITY_AGING_BOOST,
    PRIORITY_AGING_PER_HOUR,
    REMEDIATION_PRIORITY_BOOST,
    DependencyProvenance,
    Job,
    JobSource,
    JobsPage,
    JobStatus,
    PageView,
    PageViewSummary,
    PromotionKind,
    PromotionState,
    SchedulerWaitReason,
    SiteStats,
    Stage,
    Usage,
    lock_owner_id,
)
from hyqs.pipeline.resources import ResourceRecord
from hyqs.pipeline.store import JobStore, set_db_actor


@pytest.fixture
def dep_env(store):
    """A per-test fake repo path + cleanup of every row created under it."""
    repo_path = f"/tmp/test-store-{uuid.uuid4()}"
    created: list[int] = []

    def make_job(idea: str = "idea", **kwargs):
        job = store.create(idea=idea, repo_path=repo_path, chat_id=1, **kwargs)
        created.append(job.id)
        return job

    yield make_job
    with store._pool.connection() as conn:
        if created:
            conn.execute("DELETE FROM supervisor_events WHERE job_id = ANY(%s)", (created,))
            conn.execute("DELETE FROM job_events WHERE job_id = ANY(%s)", (created,))
        conn.execute(
            "DELETE FROM job_dependencies WHERE job_id IN "
            "(SELECT id FROM jobs WHERE repo_path=%s) OR depends_on_job_id IN "
            "(SELECT id FROM jobs WHERE repo_path=%s)",
            (repo_path, repo_path),
        )
        conn.execute("DELETE FROM jobs WHERE repo_path=%s", (repo_path,))
        row = conn.execute("SELECT id FROM projects WHERE repo_path = %s", (repo_path,)).fetchone()
        if row:
            conn.execute("DELETE FROM project_agents WHERE project_id = %s", (row["id"],))
            conn.execute("DELETE FROM projects WHERE id = %s", (row["id"],))


def test_create_preserves_declared_dependency_with_fixes_job_metadata(store, dep_env):
    parent = dep_env(idea="declared parent")
    fixes = dep_env(idea="job being fixed")

    dependent = dep_env(
        idea="fix another job after the parent lands",
        depends_on=[parent.id],
        source_meta={"fixes_job_id": fixes.id},
    )

    assert dependent.source_meta["fixes_job_id"] == fixes.id
    assert store.get_dependencies(dependent.id) == [parent.id]
    assert store.get_unsatisfied_deps(dependent.id) == [parent.id]


def _baseline_finding(advisory_id: str) -> dict:
    return {
        "tool": "npm-audit",
        "comparison_status": "unchanged_baseline",
        "advisory_id": advisory_id,
        "package": "example",
        "severity": "high",
        "manifest_path": "package.json",
        "lockfile_path": "package-lock.json",
        "verification_commands": ["npm audit --json"],
    }


def test_baseline_security_remediation_deduplicates_canonical_set_concurrently(store, dep_env):
    trigger = dep_env(idea="feature")

    def file_once(order):
        return store.file_baseline_security_remediation(
            trigger.id,
            [_baseline_finding(advisory_id) for advisory_id in order],
            ["npm audit --json"],
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                file_once,
                [
                    ["GHSA-B", "GHSA-A", "GHSA-A"],
                    ["GHSA-A", "GHSA-B"],
                    ["GHSA-B", "GHSA-A"],
                    ["GHSA-A", "GHSA-B"],
                ],
            )
        )

    assert len({result.job.id for result in results}) == 1
    assert sum(result.created for result in results) == 1
    remediation = results[0].job
    assert remediation.priority == trigger.priority
    assert remediation.source == JobSource.SUPERVISOR
    meta = remediation.source_meta["baseline_security_remediation"]
    assert meta["advisory_ids"] == ["GHSA-A", "GHSA-B"]
    assert meta["verification_commands"] == ["npm audit --json"]
    assert {finding["advisory_id"] for finding in meta["findings"]} == {
        "GHSA-A",
        "GHSA-B",
    }
    assert store.get_dependencies(trigger.id) == []


def test_baseline_security_remediation_uses_exact_project_identity_and_replaces_terminal(
    store, dep_env
):
    trigger = dep_env(idea="feature")
    first = store.file_baseline_security_remediation(
        trigger.id, [_baseline_finding("GHSA-A")], ["npm audit"]
    )
    distinct = store.file_baseline_security_remediation(
        trigger.id,
        [_baseline_finding("GHSA-A"), _baseline_finding("GHSA-B")],
        ["npm audit"],
    )
    assert distinct.job.id != first.job.id

    with store._pool.connection() as conn:
        conn.execute(
            "UPDATE jobs SET status=%s, stage=%s WHERE id=%s",
            (JobStatus.DONE.value, Stage.DONE.value, first.job.id),
        )
    replacement = store.file_baseline_security_remediation(
        trigger.id, [_baseline_finding("GHSA-A")], ["npm audit"]
    )
    assert replacement.created is True
    assert replacement.job.id != first.job.id

    other_repo = f"/tmp/test-store-other-{uuid.uuid4()}"
    other = store.create("other feature", other_repo, 1)
    try:
        other_result = store.file_baseline_security_remediation(
            other.id, [_baseline_finding("GHSA-A")], ["npm audit"]
        )
        assert other_result.job.project_id != replacement.job.project_id
    finally:
        with store._pool.connection() as conn:
            conn.execute(
                "DELETE FROM job_dependencies WHERE job_id IN "
                "(SELECT id FROM jobs WHERE repo_path=%s) OR depends_on_job_id IN "
                "(SELECT id FROM jobs WHERE repo_path=%s)",
                (other_repo, other_repo),
            )
            conn.execute("DELETE FROM jobs WHERE repo_path=%s", (other_repo,))
            project = conn.execute(
                "SELECT id FROM projects WHERE repo_path=%s", (other_repo,)
            ).fetchone()
            if project:
                conn.execute("DELETE FROM project_agents WHERE project_id=%s", (project["id"],))
                conn.execute("DELETE FROM projects WHERE id=%s", (project["id"],))


def test_baseline_security_remediation_legacy_metadata_and_blocking_are_safe(store, dep_env):
    trigger = dep_env(idea="feature")
    dep_env(
        idea="malformed legacy",
        source=JobSource.SUPERVISOR,
        source_meta={"security_remediation": {"advisory_ids": "GHSA-A"}},
    )
    legacy = dep_env(
        idea="legacy exact remediation",
        source=JobSource.SUPERVISOR,
        source_meta={"security_remediation": {"advisory_ids": ["GHSA-B", "GHSA-A"]}},
    )
    result = store.file_baseline_security_remediation(
        trigger.id,
        [_baseline_finding("GHSA-A"), _baseline_finding("GHSA-B")],
        ["npm audit"],
        blocking=True,
    )
    assert result.job.id == legacy.id
    assert result.created is False
    assert result.blocking_dependency_attached is True
    assert store.get_dependencies(trigger.id) == [legacy.id]
    assert store.get(trigger.id).status == JobStatus.PENDING
    assert store.get(trigger.id).stage == Stage.REVIEW

    repeated = store.file_baseline_security_remediation(
        trigger.id,
        [_baseline_finding("GHSA-B"), _baseline_finding("GHSA-A")],
        ["npm audit"],
        blocking=True,
    )
    assert repeated.job.id == legacy.id
    assert store.get_dependencies(trigger.id) == [legacy.id]


def test_baseline_security_remediation_never_reuses_trigger_as_its_own_dependency(store, dep_env):
    trigger = dep_env(idea="feature")
    first = store.file_baseline_security_remediation(
        trigger.id, [_baseline_finding("GHSA-A")], ["npm audit"]
    )

    retry = store.file_baseline_security_remediation(
        first.job.id,
        [_baseline_finding("GHSA-A")],
        ["npm audit"],
        blocking=True,
    )

    assert retry.created is True
    assert retry.job.id != first.job.id
    assert store.get_dependencies(first.job.id) == [retry.job.id]


def test_baseline_security_remediation_never_reuses_transitive_dependent(store, dep_env):
    trigger = dep_env(idea="first remediation attempt")
    intermediate = dep_env(idea="dependency bridge", depends_on=[trigger.id])
    existing = dep_env(
        idea="matching active remediation",
        source=JobSource.SUPERVISOR,
        source_meta={
            "baseline_security_remediation": {
                "advisory_ids": ["GHSA-A"],
            }
        },
        depends_on=[intermediate.id],
    )

    result = store.file_baseline_security_remediation(
        trigger.id,
        [_baseline_finding("GHSA-A")],
        ["npm audit"],
        blocking=True,
    )

    assert result.created is True
    assert result.job.id != existing.id
    assert store.get_dependencies(trigger.id) == [result.job.id]
    assert store.get_dependencies(existing.id) == [intermediate.id]


def test_baseline_security_remediation_inherits_trigger_priority(store, dep_env):
    trigger = dep_env(idea="feature", priority=9)

    result = store.file_baseline_security_remediation(
        trigger.id, [_baseline_finding("GHSA-Z")], ["npm audit --json"]
    )

    assert result.job.priority == 9


def test_mark_early_scope_correction_attempt_is_atomic_and_preserves_metadata(store, dep_env):
    scope = {
        "allowed_paths": ["hyqs/pipeline/store.py"],
        "interfaces": {"nested": "preserved"},
    }
    job = dep_env(
        idea="correct early scope",
        source_meta={"scope": scope, "filing_channel": "supervisor"},
    )

    assert store.mark_early_scope_correction_attempt(job.id) is True
    assert store.mark_early_scope_correction_attempt(job.id) is False

    reloaded = store.get(job.id)
    assert reloaded.source_meta["scope"] == scope
    assert reloaded.source_meta["filing_channel"] == "supervisor"
    assert reloaded.source_meta["early_scope_correction"]["attempted_at"]


@pytest.mark.parametrize(
    ("status", "archived"),
    [
        (JobStatus.PENDING, False),
        (JobStatus.RUNNING, False),
        (JobStatus.FAILED, False),
        (JobStatus.CANCELLED, False),
        (JobStatus.FAILED, True),
    ],
)
def test_non_done_parent_transition_keeps_dependency_unsatisfied(store, dep_env, status, archived):
    parent = dep_env(idea="parent")
    dependent = dep_env(idea="dependent", depends_on=[parent.id])
    parent.status = status
    parent.stage = Stage.DONE
    store.save(parent)
    if archived:
        store.set_archived(parent.id, True)

    assert store.get_dependencies(dependent.id) == [parent.id]
    assert store.get_unsatisfied_deps(dependent.id) == [parent.id]
    assert dependent.id in {job.id for job in store.list_pending_with_unsatisfied_deps()}


def test_done_parent_satisfies_dependency_without_deleting_edge(store, dep_env):
    parent = dep_env(idea="parent")
    dependent = dep_env(idea="dependent", depends_on=[parent.id])
    parent.status = JobStatus.DONE
    parent.stage = Stage.DONE
    store.save(parent)

    assert store.get_dependencies(dependent.id) == [parent.id]
    assert store.get_unsatisfied_deps(dependent.id) == []
    assert dependent.id not in {job.id for job in store.list_pending_with_unsatisfied_deps()}


def test_dependency_block_still_applies_true_when_no_dependency_id_recorded(store, dep_env):
    job = dep_env(idea="blocked without a recorded dependency_id")
    job.failure_detail = None
    assert store.dependency_block_still_applies(job) is True

    job.failure_detail = {}
    assert store.dependency_block_still_applies(job) is True


def test_dependency_block_still_applies_true_while_dependency_unsatisfied(store, dep_env):
    parent = dep_env(idea="parent")
    dependent = dep_env(idea="dependent", depends_on=[parent.id])
    dependent.failure_detail = {"dependency_id": parent.id}

    assert store.dependency_block_still_applies(dependent) is True


def test_dependency_block_still_applies_false_once_dependency_lands_done(store, dep_env):
    parent = dep_env(idea="parent")
    dependent = dep_env(idea="dependent", depends_on=[parent.id])
    dependent.failure_detail = {"dependency_id": parent.id}

    parent.status = JobStatus.DONE
    parent.stage = Stage.DONE
    store.save(parent)

    assert store.dependency_block_still_applies(dependent) is False


def test_dependency_block_still_applies_false_when_edge_rewired_onto_other_dependency(
    store, dep_env
):
    original_parent = dep_env(idea="original parent")
    other_parent = dep_env(idea="other parent")
    dependent = dep_env(idea="dependent", depends_on=[original_parent.id])
    dependent.failure_detail = {"dependency_id": original_parent.id}

    store.remove_job_dependency(dependent.id, original_parent.id)
    store.add_job_dependency(dependent.id, other_parent.id)

    assert store.get_unsatisfied_deps(dependent.id) == [other_parent.id]
    assert store.dependency_block_still_applies(dependent) is False


def test_remove_job_dependency_is_explicit_and_restores_claimability(store, dep_env):
    parent = dep_env(idea="parent")
    parent.stage = Stage.DONE
    store.save(parent)
    dependent = dep_env(idea="dependent", depends_on=[parent.id])
    dependent.stage = Stage.LINT
    store.save(dependent)

    assert store.get_unsatisfied_deps(dependent.id) == [parent.id]
    assert asyncio.run(store.claim_fastpath(dependent.id, "worker", time.time(), 90)) is None

    store.remove_job_dependency(dependent.id, parent.id)

    assert store.get_dependencies(dependent.id) == []
    claimed = asyncio.run(store.claim_fastpath(dependent.id, "worker", time.time(), 90))
    assert claimed is not None and claimed.id == dependent.id


@pytest.mark.parametrize("claim_path", ["claimable", "claim", "claim_fastpath"])
@pytest.mark.parametrize(
    ("parent_status", "archived"),
    [
        (JobStatus.PENDING, False),
        (JobStatus.RUNNING, False),
        (JobStatus.FAILED, False),
        (JobStatus.CANCELLED, False),
        (JobStatus.FAILED, True),
        (JobStatus.DONE, False),
    ],
)
def test_scheduler_claim_paths_require_done_parent(
    store, dep_env, claim_path, parent_status, archived
):
    parent = dep_env(idea="parent")
    parent.status = parent_status
    parent.stage = Stage.DONE
    store.save(parent)
    if archived:
        store.set_archived(parent.id, True)
    dependent = dep_env(idea="dependent", depends_on=[parent.id])
    dependent.stage = Stage.LINT
    store.save(dependent)

    if claim_path == "claimable":
        claimed = store.claimable()
    elif claim_path == "claim":
        claimed = asyncio.run(
            store.claim("worker", time.time(), 90, project_id=dependent.project_id)
        )
    else:
        claimed = asyncio.run(store.claim_fastpath(dependent.id, "worker", time.time(), 90))

    if parent_status == JobStatus.DONE:
        assert claimed is not None and claimed.id == dependent.id
    else:
        assert claimed is None
    assert store.get_dependencies(dependent.id) == [parent.id]


def test_pending_dependency_is_included_as_candidate(store, dep_env):
    # Still a raw candidate: it's up to the caller (the supervisor's
    # _reconcile_blocked_dependents) to decide whether a non-done dependency
    # is actually terminal or merely still in flight.
    dep = dep_env(idea="dependency")
    job = dep_env(idea="dependent")
    store.add_job_dependency(job.id, dep.id)

    result = store.list_pending_with_unsatisfied_deps()

    assert job.id in {j.id for j in result}


def test_failed_dependency_blocks_list(store, dep_env):
    dep = dep_env(idea="dependency")
    job = dep_env(idea="dependent")
    store.add_job_dependency(job.id, dep.id)
    dep.status = JobStatus.FAILED
    store.save(dep)

    result = store.list_pending_with_unsatisfied_deps()

    assert job.id in {j.id for j in result}


def test_dependency_marked_done_removes_job_from_list(store, dep_env):
    dep = dep_env(idea="dependency")
    job = dep_env(idea="dependent")
    store.add_job_dependency(job.id, dep.id)
    dep.status = JobStatus.FAILED
    store.save(dep)
    assert job.id in {j.id for j in store.list_pending_with_unsatisfied_deps()}

    store.reconcile_to_done(dep.id)

    result = store.list_pending_with_unsatisfied_deps()
    assert job.id not in {j.id for j in result}


def test_archived_but_not_done_dependency_still_returns_job(store, dep_env):
    dep = dep_env(idea="dependency")
    job = dep_env(idea="dependent")
    store.add_job_dependency(job.id, dep.id)
    dep.status = JobStatus.FAILED
    store.save(dep)
    store.set_archived(dep.id, True)

    result = store.list_pending_with_unsatisfied_deps()

    assert job.id in {j.id for j in result}


def test_get_dependent_jobs_returns_reverse_lookup(store, dep_env):
    dep = dep_env(idea="dependency")
    dependent_a = dep_env(idea="dependent a")
    dependent_b = dep_env(idea="dependent b")
    store.add_job_dependency(dependent_a.id, dep.id)
    store.add_job_dependency(dependent_b.id, dep.id)

    result = store.get_dependent_jobs(dep.id)

    assert {j.id for j in result} == {dependent_a.id, dependent_b.id}


def test_get_dependent_jobs_empty_when_none(store, dep_env):
    job = dep_env(idea="lonely")

    assert store.get_dependent_jobs(job.id) == []


def test_get_dependency_jobs_returns_forward_lookup(store, dep_env):
    dep_a = dep_env(idea="dependency a")
    dep_b = dep_env(idea="dependency b")
    job = dep_env(idea="dependent")
    store.add_job_dependency(job.id, dep_a.id)
    store.add_job_dependency(job.id, dep_b.id)

    result = store.get_dependency_jobs(job.id)

    assert [j.id for j in result] == sorted([dep_a.id, dep_b.id])
    assert {j.idea for j in result} == {"dependency a", "dependency b"}


def test_get_dependency_jobs_empty_when_none(store, dep_env):
    job = dep_env(idea="lonely")

    assert store.get_dependency_jobs(job.id) == []


def test_add_job_dependency_rejects_self_edge(store, dep_env):
    job = dep_env(idea="self")

    with pytest.raises(ValueError, match=str(job.id)):
        store.add_job_dependency(job.id, job.id)

    assert store.get_dependencies(job.id) == []


def test_add_job_dependency_rejects_direct_two_node_cycle(store, dep_env):
    a = dep_env(idea="a")
    b = dep_env(idea="b")
    store.add_job_dependency(a.id, b.id)

    with pytest.raises(ValueError) as excinfo:
        store.add_job_dependency(b.id, a.id)
    assert str(a.id) in str(excinfo.value)
    assert str(b.id) in str(excinfo.value)

    assert store.get_dependencies(a.id) == [b.id]
    assert store.get_dependencies(b.id) == []


def test_add_job_dependency_rejects_indirect_cycle(store, dep_env):
    a = dep_env(idea="a")
    b = dep_env(idea="b")
    c = dep_env(idea="c")
    store.add_job_dependency(a.id, b.id)
    store.add_job_dependency(b.id, c.id)

    with pytest.raises(ValueError) as excinfo:
        store.add_job_dependency(c.id, a.id)
    assert str(a.id) in str(excinfo.value)
    assert str(c.id) in str(excinfo.value)

    assert store.get_dependencies(a.id) == [b.id]
    assert store.get_dependencies(b.id) == [c.id]
    assert store.get_dependencies(c.id) == []


def test_add_job_dependency_allows_legitimate_chain(store, dep_env):
    a = dep_env(idea="a")
    b = dep_env(idea="b")
    c = dep_env(idea="c")

    store.add_job_dependency(b.id, a.id)
    store.add_job_dependency(c.id, b.id)

    assert store.get_dependencies(b.id) == [a.id]
    assert store.get_dependencies(c.id) == [b.id]


def test_add_job_dependency_allows_diamond(store, dep_env):
    root = dep_env(idea="root")
    left = dep_env(idea="left")
    right = dep_env(idea="right")
    tip = dep_env(idea="tip")

    store.add_job_dependency(left.id, root.id)
    store.add_job_dependency(right.id, root.id)
    store.add_job_dependency(tip.id, left.id)
    store.add_job_dependency(tip.id, right.id)

    assert store.get_dependencies(left.id) == [root.id]
    assert store.get_dependencies(right.id) == [root.id]
    assert store.get_dependencies(tip.id) == sorted([left.id, right.id])


def test_add_job_dependency_serializes_disjoint_endpoint_races(store, dep_env):
    """Two concurrent adds with disjoint endpoints must not jointly create a cycle.

    Pre-existing edges b->c and d->a are each acyclic on their own. Adding
    a->b and c->d concurrently would jointly close the cycle a->b->c->d->a
    even though neither call's own two endpoints overlap with the other's.
    A lock scoped to just the two endpoint rows can't catch this; only a
    fleet-wide serialization point can.
    """
    a = dep_env(idea="a")
    b = dep_env(idea="b")
    c = dep_env(idea="c")
    d = dep_env(idea="d")
    store.add_job_dependency(b.id, c.id)
    store.add_job_dependency(d.id, a.id)

    def try_add(job_id, dep_id):
        try:
            store.add_job_dependency(job_id, dep_id)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda args: try_add(*args), [(a.id, b.id), (c.id, d.id)]))

    assert sum(results) == 1

    all_edges = {job.id: set(store.get_dependencies(job.id)) for job in (a, b, c, d)}
    assert all_edges[b.id] == {c.id}
    assert all_edges[d.id] == {a.id}
    if results[0]:
        assert all_edges[a.id] == {b.id}
        assert all_edges[c.id] == set()
    else:
        assert all_edges[a.id] == set()
        assert all_edges[c.id] == {d.id}


def test_repoint_split_dependents_moves_dependent_onto_all_children(store, dep_env):
    parent = dep_env(idea="parent")
    c1 = dep_env(idea="child1")
    c2 = dep_env(idea="child2")
    dependent = dep_env(idea="dependent")
    store.add_job_dependency(dependent.id, parent.id)

    repointed = store.repoint_split_dependents(parent.id, [c1.id, c2.id])

    assert repointed == [dependent.id]
    assert store.get_dependencies(dependent.id) == sorted([c1.id, c2.id])


def test_repoint_split_dependents_moves_dependent_onto_terminal_child_only(store, dep_env):
    """Simulates the plan-splitting DAG builder's chained-story output (job #3181):
    child2's job_dependencies row already depends on child1, so child1 is a
    non-terminal predecessor within the split and must not receive the parent's
    former dependent's edge — only child2, the terminal/sink child, should."""
    parent = dep_env(idea="parent")
    child1 = dep_env(idea="child1 (non-terminal predecessor)")
    child2 = dep_env(idea="child2 (terminal)")
    dependent = dep_env(idea="dependent")
    store.add_job_dependency(dependent.id, parent.id)
    store.add_job_dependency(child2.id, child1.id)

    repointed = store.repoint_split_dependents(parent.id, [child1.id, child2.id])

    assert repointed == [dependent.id]
    assert store.get_dependencies(dependent.id) == [child2.id]


def test_repoint_split_dependents_preserves_other_deps_and_dedupes(store, dep_env):
    parent = dep_env(idea="parent")
    other = dep_env(idea="other dep")
    c1 = dep_env(idea="child1")
    c2 = dep_env(idea="child2")
    dependent = dep_env(idea="dependent")
    store.add_job_dependency(dependent.id, parent.id)
    store.add_job_dependency(dependent.id, other.id)
    store.add_job_dependency(dependent.id, c1.id)

    repointed = store.repoint_split_dependents(parent.id, [c1.id, c2.id])

    assert repointed == [dependent.id]
    assert store.get_dependencies(dependent.id) == sorted([other.id, c1.id, c2.id])


def test_repoint_split_dependents_preserves_strongest_provenance(store, dep_env):
    parent = dep_env(idea="parent")
    child = dep_env(idea="child")
    dependent = dep_env(idea="dependent")
    store.add_job_dependency(dependent.id, parent.id, DependencyProvenance.SEMANTIC)
    store.add_job_dependency(dependent.id, child.id, DependencyProvenance.AUTO)

    store.repoint_split_dependents(parent.id, [child.id])

    with store._pool.connection() as conn:
        row = conn.execute(
            "SELECT provenance FROM job_dependencies WHERE job_id=%s AND depends_on_job_id=%s",
            (dependent.id, child.id),
        ).fetchone()
    assert row["provenance"] == DependencyProvenance.SEMANTIC.value


def test_repoint_split_dependents_empty_when_parent_has_no_dependents(store, dep_env):
    parent = dep_env(idea="lonely parent")

    assert store.repoint_split_dependents(parent.id, [999]) == []


def test_repoint_split_dependents_reconciles_disjoint_auto_edge_immediately(store, dep_env):
    parent = dep_env(idea="parent")
    child_disjoint = dep_env(
        idea="child disjoint", source_meta={"scope": {"allowed_paths": ["other/x.py"]}}
    )
    child_overlap = dep_env(
        idea="child overlap", source_meta={"scope": {"allowed_paths": ["dep/only.py"]}}
    )
    dependent_auto = dep_env(
        idea="auto dependent", source_meta={"scope": {"allowed_paths": ["dep/only.py"]}}
    )
    dependent_semantic = dep_env(idea="semantic dependent")
    store.add_job_dependency(dependent_auto.id, parent.id, DependencyProvenance.AUTO)
    store.add_job_dependency(dependent_semantic.id, parent.id, DependencyProvenance.SEMANTIC)

    repointed = store.repoint_split_dependents(parent.id, [child_disjoint.id, child_overlap.id])

    assert sorted(repointed) == sorted([dependent_auto.id, dependent_semantic.id])
    # the auto edge to the now-known-disjoint child was reconciled away immediately
    assert store.get_dependencies(dependent_auto.id) == [child_overlap.id]
    # the semantic dependent's fan-out edges are never touched by reconciliation
    assert store.get_dependencies(dependent_semantic.id) == sorted(
        [child_disjoint.id, child_overlap.id]
    )
    events = store.list_events(dependent_auto.id)
    removed = [
        e for e in events if e["summary"] == f"auto dependency removed: #{child_disjoint.id}"
    ]
    assert len(removed) == 1
    assert removed[0]["detail"]["reason"] == "known_disjoint_scopes"
    assert not any(e["summary"] == f"auto dependency removed: #{child_overlap.id}" for e in events)


def test_repoint_split_dependents_rejects_cycle_and_leaves_graph_unchanged(store, dep_env):
    parent = dep_env(idea="parent")
    dependent = dep_env(idea="dependent")
    child = dep_env(idea="child that is also a dependent")
    store.add_job_dependency(dependent.id, parent.id)
    # child already depends on the same dependent, so repointing dependent
    # onto child would close dependent -> child -> dependent.
    store.add_job_dependency(child.id, dependent.id)

    with pytest.raises(ValueError, match="cycle"):
        store.repoint_split_dependents(parent.id, [child.id])

    assert store.get_dependencies(dependent.id) == [parent.id]
    assert store.get_dependencies(child.id) == [dependent.id]


def test_list_split_parents_with_pending_dependents_finds_orphan(store, dep_env):
    parent = dep_env(idea="parent")
    child = dep_env(idea="child")
    dependent = dep_env(idea="dependent")
    store.add_job_dependency(dependent.id, parent.id)
    store.set_job_resolution(parent.id, "superseded-by-split")
    store.add_event(
        parent.id,
        "plan",
        "cancelled",
        summary="plan split into 1 job(s)",
        detail={"superseded_by": [child.id]},
    )
    try:
        result = store.list_split_parents_with_pending_dependents()

        matches = [e for e in result if e["parent_id"] == parent.id]
        assert len(matches) == 1
        assert matches[0]["child_ids"] == [child.id]
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_events WHERE job_id = %s", (parent.id,))


def test_list_split_parents_with_pending_dependents_empty_once_repointed(store, dep_env):
    parent = dep_env(idea="parent")
    child = dep_env(idea="child")
    dependent = dep_env(idea="dependent")
    store.add_job_dependency(dependent.id, parent.id)
    store.set_job_resolution(parent.id, "superseded-by-split")
    store.add_event(
        parent.id,
        "plan",
        "cancelled",
        summary="plan split into 1 job(s)",
        detail={"superseded_by": [child.id]},
    )
    try:
        store.repoint_split_dependents(parent.id, [child.id])

        result = store.list_split_parents_with_pending_dependents()

        assert all(e["parent_id"] != parent.id for e in result)
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_events WHERE job_id = %s", (parent.id,))


@pytest.fixture
def fix_forward_env(store):
    """A per-test fake repo path + cleanup for fix-forward/repoint tests.

    Unlike dep_env, exposes the ``created`` list so tests can register the
    extra job id that fix_forward_and_repoint() itself creates.
    """
    repo_path = f"/tmp/test-store-{uuid.uuid4()}"
    created: list[int] = []

    def make_job(idea: str = "idea"):
        job = store.create(idea=idea, repo_path=repo_path, chat_id=1)
        created.append(job.id)
        return job

    yield make_job, created
    with store._pool.connection() as conn:
        if created:
            conn.execute(
                "DELETE FROM job_dependencies WHERE job_id = ANY(%s) OR depends_on_job_id = ANY(%s)",
                (created, created),
            )
            conn.execute("DELETE FROM supervisor_events WHERE job_id = ANY(%s)", (created,))
            conn.execute("DELETE FROM job_events WHERE job_id = ANY(%s)", (created,))
            conn.execute("DELETE FROM jobs WHERE id = ANY(%s)", (created,))
        row = conn.execute("SELECT id FROM projects WHERE repo_path = %s", (repo_path,)).fetchone()
        if row:
            conn.execute("DELETE FROM project_agents WHERE project_id = %s", (row["id"],))
            conn.execute("DELETE FROM projects WHERE id = %s", (row["id"],))


def test_fix_forward_and_repoint_creates_job_and_moves_selected_dependents(store, fix_forward_env):
    make_job, created = fix_forward_env
    failed = make_job(idea="failed job")
    failed.status = JobStatus.FAILED
    store.save(failed)
    dep_a = make_job(idea="dependent a")
    dep_b = make_job(idea="dependent b")
    other_dep = make_job(idea="dep_a's other dependency")
    store.add_job_dependency(dep_a.id, failed.id)
    store.add_job_dependency(dep_a.id, other_dep.id)
    store.add_job_dependency(dep_b.id, failed.id)

    new_job = store.fix_forward_and_repoint(
        failed.id, "fix idea", "fix title", "tester@example.com", [dep_a.id, dep_b.id]
    )
    created.append(new_job.id)

    assert new_job.idea == "fix idea"
    assert new_job.title == "fix title"
    assert new_job.source_meta == {"fix_for": failed.id}
    assert set(store.get_dependencies(dep_a.id)) == {new_job.id, other_dep.id}
    assert store.get_dependencies(dep_b.id) == [new_job.id]
    assert store.get(failed.id).archived is True


def test_fix_forward_and_repoint_leaves_unselected_dependent_pointing_at_archived_job(
    store, fix_forward_env
):
    make_job, created = fix_forward_env
    failed = make_job(idea="failed job")
    failed.status = JobStatus.FAILED
    store.save(failed)
    dep_selected = make_job(idea="selected dep")
    dep_unselected = make_job(idea="unselected dep")
    store.add_job_dependency(dep_selected.id, failed.id)
    store.add_job_dependency(dep_unselected.id, failed.id)

    new_job = store.fix_forward_and_repoint(
        failed.id, "fix idea", "", "tester@example.com", [dep_selected.id]
    )
    created.append(new_job.id)

    assert store.get_dependencies(dep_selected.id) == [new_job.id]
    assert store.get_dependencies(dep_unselected.id) == [failed.id]
    assert store.get(failed.id).archived is True


def test_fix_forward_and_repoint_empty_repoint_list_only_archives(store, fix_forward_env):
    make_job, created = fix_forward_env
    failed = make_job(idea="failed job")
    failed.status = JobStatus.FAILED
    store.save(failed)

    new_job = store.fix_forward_and_repoint(failed.id, "fix idea", "", "tester@example.com", [])
    created.append(new_job.id)

    assert store.get(failed.id).archived is True
    assert new_job.source_meta == {"fix_for": failed.id}


def test_fix_forward_and_repoint_strengthens_generated_relationship(store, fix_forward_env):
    make_job, created = fix_forward_env
    failed = make_job(idea="failed")
    dependent = make_job(idea="dependent")
    store.add_job_dependency(dependent.id, failed.id, DependencyProvenance.AUTO)

    replacement = store.fix_forward_and_repoint(
        failed.id, "fix", "", "tester@example.com", [dependent.id]
    )
    created.append(replacement.id)

    with store._pool.connection() as conn:
        row = conn.execute(
            "SELECT provenance FROM job_dependencies WHERE job_id=%s AND depends_on_job_id=%s",
            (dependent.id, replacement.id),
        ).fetchone()
    assert row["provenance"] == DependencyProvenance.SEMANTIC.value


def test_repointed_pending_dependent_claim_eligible_after_new_job_done(store, fix_forward_env):
    make_job, created = fix_forward_env
    failed = make_job(idea="failed job")
    failed.status = JobStatus.FAILED
    store.save(failed)
    dep = make_job(idea="dependent")
    dep.stage = Stage.LINT
    store.save(dep)
    store.add_job_dependency(dep.id, failed.id)

    new_job = store.fix_forward_and_repoint(
        failed.id, "fix idea", "", "tester@example.com", [dep.id]
    )
    created.append(new_job.id)
    new_job.stage = Stage.LINT
    store.save(new_job)

    claimed_new = asyncio.run(store.claim("worker-a", time.time(), 90, project_id=dep.project_id))
    assert claimed_new is not None and claimed_new.id == new_job.id

    assert asyncio.run(store.claim("worker-b", time.time(), 90, project_id=dep.project_id)) is None

    claimed_new.status = JobStatus.DONE
    claimed_new.stage = Stage.DONE
    store.save(claimed_new)

    claimed_dep = asyncio.run(store.claim("worker-c", time.time(), 90, project_id=dep.project_id))
    assert claimed_dep is not None and claimed_dep.id == dep.id


def test_claim_is_exclusive_across_concurrent_workers(store, dep_env):
    """claim()'s pg_advisory_xact_lock must let exactly one worker win a job.

    LINT is a deterministic stage (no agent task, see stage_task in models.py),
    so this exercises the claim advisory lock itself rather than roster routing.
    """
    import time

    job = dep_env(idea="race me")
    job.stage = Stage.LINT
    store.save(job)

    async def _race():
        now = time.time()
        return await asyncio.gather(
            store.claim("worker-a", now, 90, project_id=job.project_id),
            store.claim("worker-b", now, 90, project_id=job.project_id),
        )

    results = asyncio.run(_race())
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert winners[0].id == job.id


def test_claim_skips_deploy_job_pinned_to_a_different_host(store, dep_env):
    job = dep_env(idea="deploy me")
    job.stage = Stage.DEPLOY
    store.save(job)
    env = store.get_or_create_default_environment(job.project_id)
    host = store.create_host(f"host-{uuid.uuid4()}")
    store.set_environment_host(env.id, host.id)

    claimed = asyncio.run(
        store.claim("worker-a", time.time(), 90, project_id=job.project_id, worker_host_id=None)
    )

    assert claimed is None


def test_claim_returns_deploy_job_pinned_to_matching_host(store, dep_env):
    job = dep_env(idea="deploy me")
    job.stage = Stage.DEPLOY
    store.save(job)
    env = store.get_or_create_default_environment(job.project_id)
    host = store.create_host(f"host-{uuid.uuid4()}")
    store.set_environment_host(env.id, host.id)

    claimed = asyncio.run(
        store.claim("worker-a", time.time(), 90, project_id=job.project_id, worker_host_id=host.id)
    )

    assert claimed is not None and claimed.id == job.id


def test_claim_returns_null_host_deploy_job_regardless_of_worker(store, dep_env):
    """Regression guard: no host pinned anywhere (today's universal state) must
    stay claimable by any worker, matching current single-host behavior."""
    job = dep_env(idea="deploy me")
    job.stage = Stage.DEPLOY
    store.save(job)
    store.get_or_create_default_environment(job.project_id)  # host_id stays NULL

    claimed = asyncio.run(
        store.claim("worker-a", time.time(), 90, project_id=job.project_id, worker_host_id=999999)
    )

    assert claimed is not None and claimed.id == job.id


def test_claim_ignores_host_pinning_for_non_deploy_stage(store, dep_env):
    job = dep_env(idea="lint me")
    job.stage = Stage.LINT
    store.save(job)
    env = store.get_or_create_default_environment(job.project_id)
    host = store.create_host(f"host-{uuid.uuid4()}")
    store.set_environment_host(env.id, host.id)

    claimed = asyncio.run(
        store.claim("worker-a", time.time(), 90, project_id=job.project_id, worker_host_id=None)
    )

    assert claimed is not None and claimed.id == job.id


def test_claim_fastpath_skips_deploy_job_pinned_to_a_different_host(store, dep_env):
    job = dep_env(idea="deploy me")
    job.stage = Stage.DEPLOY
    store.save(job)
    env = store.get_or_create_default_environment(job.project_id)
    host = store.create_host(f"host-{uuid.uuid4()}")
    store.set_environment_host(env.id, host.id)

    claimed = asyncio.run(
        store.claim_fastpath(job.id, "worker-a", time.time(), 90, worker_host_id=None)
    )

    assert claimed is None


def test_claim_fastpath_returns_deploy_job_pinned_to_matching_host(store, dep_env):
    job = dep_env(idea="deploy me")
    job.stage = Stage.DEPLOY
    store.save(job)
    env = store.get_or_create_default_environment(job.project_id)
    host = store.create_host(f"host-{uuid.uuid4()}")
    store.set_environment_host(env.id, host.id)

    claimed = asyncio.run(
        store.claim_fastpath(job.id, "worker-a", time.time(), 90, worker_host_id=host.id)
    )

    assert claimed is not None and claimed.id == job.id


def _make_staging_pinned_deploy_job(store, host_id: int | None):
    """A project with a 'staging' environment (pinned to host_id) plus a DEPLOY
    job whose source_meta targets that environment. Returns (project_id, job)."""
    project = store.create_project(f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}")
    staging = store.create_environment(project.id, "staging", "staging")
    if host_id is not None:
        store.set_environment_host(staging.id, host_id)
    job = store.create(
        idea="deploy me to staging",
        repo_path=project.repo_path,
        chat_id=1,
        source_meta={"environment_id": staging.id},
    )
    job.stage = Stage.DEPLOY
    store.save(job)
    return project.id, job


def test_claim_skips_deploy_job_pinned_to_a_different_staging_host(store):
    host = store.create_host(f"host-{uuid.uuid4()}")
    project_id, job = _make_staging_pinned_deploy_job(store, host.id)
    try:
        claimed = asyncio.run(
            store.claim("worker-a", time.time(), 90, project_id=project_id, worker_host_id=None)
        )
        assert claimed is None
    finally:
        store.delete_project(project_id)


def test_claim_returns_deploy_job_pinned_to_matching_staging_host(store):
    host = store.create_host(f"host-{uuid.uuid4()}")
    project_id, job = _make_staging_pinned_deploy_job(store, host.id)
    try:
        claimed = asyncio.run(
            store.claim("worker-a", time.time(), 90, project_id=project_id, worker_host_id=host.id)
        )
        assert claimed is not None and claimed.id == job.id
    finally:
        store.delete_project(project_id)


def test_create_project_exclusive_creates_project_with_default_agent(store):
    repo_path = f"/tmp/test-store-{uuid.uuid4()}"
    project = store.create_project_exclusive("Exclusive Create Project", repo_path)
    try:
        assert project is not None
        assert project.name == "Exclusive Create Project"
        assert project.repo_path == repo_path
        agents = store.list_agents(project.id)
        assert len(agents) == 1
    finally:
        store.delete_project(project.id)


def test_create_project_exclusive_returns_none_on_conflict(store):
    repo_path = f"/tmp/test-store-{uuid.uuid4()}"
    original = store.create_project_exclusive("Exclusive Conflict Project", repo_path)
    try:
        result = store.create_project_exclusive("Attacker Project", repo_path)
        assert result is None

        unchanged = store.get_project_by_repo(repo_path)
        assert unchanged.id == original.id
        assert unchanged.name == original.name
        assert unchanged.created_at == original.created_at

        agents = store.list_agents(original.id)
        assert len(agents) == 1
    finally:
        store.delete_project(original.id)


def test_claim_without_environment_id_ignores_staging_pin_and_uses_prod(store, dep_env):
    """Regression guard: a job with no environment_id in source_meta must resolve via
    the prod lookup only, even if a staging environment is pinned to a different host."""
    job = dep_env(idea="deploy me")
    job.stage = Stage.DEPLOY
    store.save(job)
    staging = store.create_environment(job.project_id, "staging", "staging")
    staging_host = store.create_host(f"host-{uuid.uuid4()}")
    store.set_environment_host(staging.id, staging_host.id)
    store.get_or_create_default_environment(job.project_id)  # prod host_id stays NULL

    claimed = asyncio.run(
        store.claim("worker-a", time.time(), 90, project_id=job.project_id, worker_host_id=999999)
    )

    assert claimed is not None and claimed.id == job.id


def test_claim_fastpath_skips_deploy_job_pinned_to_a_different_staging_host(store):
    host = store.create_host(f"host-{uuid.uuid4()}")
    project_id, job = _make_staging_pinned_deploy_job(store, host.id)
    try:
        claimed = asyncio.run(
            store.claim_fastpath(job.id, "worker-a", time.time(), 90, worker_host_id=None)
        )
        assert claimed is None
    finally:
        store.delete_project(project_id)


def test_claim_fastpath_returns_deploy_job_pinned_to_matching_staging_host(store):
    host = store.create_host(f"host-{uuid.uuid4()}")
    project_id, job = _make_staging_pinned_deploy_job(store, host.id)
    try:
        claimed = asyncio.run(
            store.claim_fastpath(job.id, "worker-a", time.time(), 90, worker_host_id=host.id)
        )
        assert claimed is not None and claimed.id == job.id
    finally:
        store.delete_project(project_id)


def test_claim_stage_allowlist_skips_non_matching_pending_job(store, dep_env):
    job = dep_env(idea="plan me")
    job.stage = Stage.PLAN
    store.save(job)

    claimed = asyncio.run(
        store.claim(
            "worker-a",
            time.time(),
            90,
            project_id=job.project_id,
            stage_allowlist={Stage.DEPLOY},
        )
    )

    assert claimed is None


def test_claim_stage_allowlist_returns_matching_pending_job(store, dep_env):
    plan_job = dep_env(idea="plan me")
    plan_job.stage = Stage.PLAN
    store.save(plan_job)
    deploy_job = dep_env(idea="deploy me")
    deploy_job.stage = Stage.DEPLOY
    store.save(deploy_job)

    claimed = asyncio.run(
        store.claim(
            "worker-a",
            time.time(),
            90,
            project_id=plan_job.project_id,
            stage_allowlist={Stage.DEPLOY},
        )
    )

    assert claimed is not None and claimed.id == deploy_job.id


def test_claim_without_stage_allowlist_is_unrestricted(store, dep_env):
    job = dep_env(idea="lint me")
    job.stage = Stage.LINT
    store.save(job)

    claimed = asyncio.run(store.claim("worker-a", time.time(), 90, project_id=job.project_id))

    assert claimed is not None and claimed.id == job.id


def test_claim_fastpath_stage_allowlist_skips_non_matching_job(store, dep_env):
    job = dep_env(idea="lint me")
    job.stage = Stage.LINT
    store.save(job)

    claimed = asyncio.run(
        store.claim_fastpath(job.id, "worker-a", time.time(), 90, stage_allowlist={Stage.DEPLOY})
    )

    assert claimed is None


# ---------------------------------------------------------------------------
# record_provider_failover: atomic provider-failover transition + claim
# integration (job #2870)
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_provider_pauses(store):
    """Reset claude/codex pauses before AND after: these tests share one
    physical Postgres 'meta' table (no per-test isolation for it), so a pause
    one test sets — even one deliberately hours in the future — would
    otherwise leak into every later test in this run."""
    store.set_provider_pause("claude", 0.0)
    store.set_provider_pause("codex", 0.0)
    yield
    store.set_provider_pause("claude", 0.0)
    store.set_provider_pause("codex", 0.0)


def _running_job(store, job, *, provider: str, agent_id: int, stage: Stage = Stage.PLAN) -> Job:
    """Check ``job`` out RUNNING under 'worker-a' with a specific pinned agent."""
    job.stage = stage
    job.status = JobStatus.RUNNING
    job.owner = "worker-a"
    job.provider = provider
    job.agent_id = agent_id
    job.lease_until = time.time() + 90
    job.attempts = 2
    job.rebase_attempts = 1
    job.timeout_attempts = 1
    job.plan_reask_attempts = 1
    store.save(job)
    return store.get(job.id)


def _default_agent_id(store, project_id: int) -> int:
    return store.list_agents(project_id)[0].id  # the seeded 'Claude (default)' agent


def test_record_provider_failover_pauses_source_and_requeues_same_stage(
    store, dep_env, clean_provider_pauses
):
    job = dep_env(idea="provider failover me")
    claude_agent_id = _default_agent_id(store, job.project_id)
    job = _running_job(store, job, provider="claude", agent_id=claude_agent_id)
    now = time.time()
    resets_at = now + 300

    transition = asyncio.run(
        store.record_provider_failover(
            job.id,
            "worker-a",
            Stage.PLAN,
            "claude",
            resets_at,
            now,
            project_id=job.project_id,
            failed_step="build",
        )
    )

    assert transition.applied is True
    assert transition.event_id is not None
    assert transition.alternate_available is False  # only the default claude agent exists
    assert store.paused_providers(now) == {"claude"}

    reloaded = store.get(job.id)
    assert reloaded.status == JobStatus.PENDING
    assert reloaded.stage == Stage.PLAN
    assert reloaded.owner == ""
    assert reloaded.lease_until == 0.0
    assert reloaded.agent_id is None
    assert reloaded.provider == ""
    # the failure snapshot describes this pause, not a stale earlier failure
    assert reloaded.failed_step == "build"
    assert reloaded.failure_code == "provider_unavailable"
    assert reloaded.failure_origin == "provider"
    assert reloaded.retry_disposition == "same_step"
    assert reloaded.failure_detail == {"provider": "claude", "resets_at": resets_at}
    # functional retry budgets are untouched by a provider-limit requeue
    assert reloaded.attempts == 2
    assert reloaded.rebase_attempts == 1
    assert reloaded.timeout_attempts == 1
    assert reloaded.plan_reask_attempts == 1

    events = [e for e in store.list_events(job.id) if e["status"] == "provider_failover"]
    assert len(events) == 1
    detail = events[0]["detail"]
    assert detail["source_provider"] == "claude"
    assert detail["destination_provider"] is None
    assert detail["stage"] == "plan"
    assert detail["alternate_available"] is False


def test_record_provider_failover_alternate_available_when_codex_can_take_over(
    store, dep_env, clean_provider_pauses
):
    job = dep_env(idea="provider failover me")
    claude_agent_id = _default_agent_id(store, job.project_id)
    store.create_agent(job.project_id, "Codex", "codex", "gpt-5")
    job = _running_job(store, job, provider="claude", agent_id=claude_agent_id)

    now = time.time()
    transition = asyncio.run(
        store.record_provider_failover(
            job.id, "worker-a", Stage.PLAN, "claude", now + 300, now, project_id=job.project_id
        )
    )

    assert transition.applied is True
    assert transition.alternate_available is True


def test_record_provider_failover_no_alternate_when_only_agent_lacks_required_task(
    store, dep_env, clean_provider_pauses
):
    job = dep_env(idea="provider failover me")
    claude_agent_id = _default_agent_id(store, job.project_id)
    store.create_agent(job.project_id, "Codex", "codex", "gpt-5", allowed_tasks=["review"])
    job = _running_job(store, job, provider="claude", agent_id=claude_agent_id)

    now = time.time()
    # PLAN's next task is BUILD; Codex is only allowed 'review'.
    transition = asyncio.run(
        store.record_provider_failover(
            job.id, "worker-a", Stage.PLAN, "claude", now + 300, now, project_id=job.project_id
        )
    )

    assert transition.alternate_available is False


def test_record_provider_failover_no_alternate_when_every_provider_paused(
    store, dep_env, clean_provider_pauses
):
    job = dep_env(idea="provider failover me")
    claude_agent_id = _default_agent_id(store, job.project_id)
    store.create_agent(job.project_id, "Codex", "codex", "gpt-5")
    job = _running_job(store, job, provider="claude", agent_id=claude_agent_id)
    now = time.time()
    store.set_provider_pause("codex", now + 3600)

    transition = asyncio.run(
        store.record_provider_failover(
            job.id, "worker-a", Stage.PLAN, "claude", now + 300, now, project_id=job.project_id
        )
    )

    assert transition.alternate_available is False


def test_record_provider_failover_rejects_wrong_owner(store, dep_env, clean_provider_pauses):
    job = dep_env(idea="provider failover me")
    claude_agent_id = _default_agent_id(store, job.project_id)
    job = _running_job(store, job, provider="claude", agent_id=claude_agent_id)
    now = time.time()

    transition = asyncio.run(
        store.record_provider_failover(
            job.id, "worker-b", Stage.PLAN, "claude", now + 300, now, project_id=job.project_id
        )
    )

    assert transition.applied is False
    assert store.paused_providers(now) == set()
    reloaded = store.get(job.id)
    assert reloaded.status == JobStatus.RUNNING
    assert reloaded.owner == "worker-a"
    assert [e for e in store.list_events(job.id) if e["status"] == "provider_failover"] == []


def test_record_provider_failover_rejects_wrong_stage(store, dep_env, clean_provider_pauses):
    job = dep_env(idea="provider failover me")
    claude_agent_id = _default_agent_id(store, job.project_id)
    job = _running_job(store, job, provider="claude", agent_id=claude_agent_id)
    now = time.time()

    transition = asyncio.run(
        store.record_provider_failover(
            job.id, "worker-a", Stage.BUILD, "claude", now + 300, now, project_id=job.project_id
        )
    )

    assert transition.applied is False
    reloaded = store.get(job.id)
    assert reloaded.status == JobStatus.RUNNING


def test_record_provider_failover_rejects_wrong_provider(store, dep_env, clean_provider_pauses):
    job = dep_env(idea="provider failover me")
    claude_agent_id = _default_agent_id(store, job.project_id)
    job = _running_job(store, job, provider="claude", agent_id=claude_agent_id)
    now = time.time()

    transition = asyncio.run(
        store.record_provider_failover(
            job.id, "worker-a", Stage.PLAN, "codex", now + 300, now, project_id=job.project_id
        )
    )

    assert transition.applied is False
    reloaded = store.get(job.id)
    assert reloaded.status == JobStatus.RUNNING
    assert store.paused_providers(now) == set()


def test_record_provider_failover_rejects_when_not_running(store, dep_env, clean_provider_pauses):
    job = dep_env(idea="provider failover me")
    job.stage = Stage.PLAN
    store.save(job)  # left PENDING
    now = time.time()

    transition = asyncio.run(
        store.record_provider_failover(
            job.id, "worker-a", Stage.PLAN, "claude", now + 300, now, project_id=job.project_id
        )
    )

    assert transition.applied is False


def test_claim_attaches_codex_destination_after_claude_failover(
    store, dep_env, clean_provider_pauses
):
    job = dep_env(idea="provider failover me")
    claude_agent_id = _default_agent_id(store, job.project_id)
    codex_agent = store.create_agent(job.project_id, "Codex", "codex", "gpt-5")
    job = _running_job(store, job, provider="claude", agent_id=claude_agent_id)
    now = time.time()

    transition = asyncio.run(
        store.record_provider_failover(
            job.id, "worker-a", Stage.PLAN, "claude", now + 300, now, project_id=job.project_id
        )
    )
    assert transition.alternate_available is True

    claimed = asyncio.run(store.claim("worker-b", time.time(), 90, project_id=job.project_id))

    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.provider == "codex"
    assert claimed.agent_id == codex_agent.id
    events = [e for e in store.list_events(job.id) if e["status"] == "provider_failover"]
    assert len(events) == 1
    assert events[0]["detail"]["destination_provider"] == "codex"
    assert events[0]["detail"]["source_provider"] == "claude"


def test_claim_attaches_claude_destination_after_codex_failover(
    store, dep_env, clean_provider_pauses
):
    job = dep_env(idea="provider failover me")
    claude_agent_id = _default_agent_id(store, job.project_id)
    codex_agent = store.create_agent(job.project_id, "Codex", "codex", "gpt-5")
    job = _running_job(store, job, provider="codex", agent_id=codex_agent.id)
    now = time.time()

    transition = asyncio.run(
        store.record_provider_failover(
            job.id, "worker-a", Stage.PLAN, "codex", now + 300, now, project_id=job.project_id
        )
    )
    assert transition.alternate_available is True

    claimed = asyncio.run(store.claim("worker-b", time.time(), 90, project_id=job.project_id))

    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.provider == "claude"
    assert claimed.agent_id == claude_agent_id
    events = [e for e in store.list_events(job.id) if e["status"] == "provider_failover"]
    assert events[0]["detail"]["destination_provider"] == "claude"
    assert events[0]["detail"]["source_provider"] == "codex"


def test_claim_stays_pending_and_event_unresolved_with_no_compatible_alternate(
    store, dep_env, clean_provider_pauses
):
    job = dep_env(idea="provider failover me")
    claude_agent_id = _default_agent_id(store, job.project_id)
    job = _running_job(store, job, provider="claude", agent_id=claude_agent_id)
    now = time.time()

    transition = asyncio.run(
        store.record_provider_failover(
            job.id, "worker-a", Stage.PLAN, "claude", now + 300, now, project_id=job.project_id
        )
    )
    assert transition.alternate_available is False

    claimed = asyncio.run(store.claim("worker-b", time.time(), 90, project_id=job.project_id))

    assert claimed is None
    reloaded = store.get(job.id)
    assert reloaded.status == JobStatus.PENDING
    events = [e for e in store.list_events(job.id) if e["status"] == "provider_failover"]
    assert events[0]["detail"]["destination_provider"] is None


def test_claim_does_not_rewrite_a_resolved_failover_event_on_reclaim(
    store, dep_env, clean_provider_pauses
):
    job = dep_env(idea="provider failover me")
    claude_agent_id = _default_agent_id(store, job.project_id)
    codex_agent = store.create_agent(job.project_id, "Codex", "codex", "gpt-5")
    job = _running_job(store, job, provider="claude", agent_id=claude_agent_id)
    now = time.time()
    asyncio.run(
        store.record_provider_failover(
            job.id, "worker-a", Stage.PLAN, "claude", now + 300, now, project_id=job.project_id
        )
    )
    first_claim = asyncio.run(store.claim("worker-b", time.time(), 90, project_id=job.project_id))
    assert first_claim.provider == "codex"
    assert first_claim.agent_id == codex_agent.id

    # Simulate the lease expiring (claude stays paused) and a second worker
    # re-claiming the same still-pending stage.
    first_claim.status = JobStatus.PENDING
    first_claim.owner = ""
    first_claim.agent_id = None
    first_claim.provider = ""
    store.save(first_claim)
    second_claim = asyncio.run(store.claim("worker-c", time.time(), 90, project_id=job.project_id))

    assert second_claim is not None and second_claim.provider == "codex"
    events = [e for e in store.list_events(job.id) if e["status"] == "provider_failover"]
    assert len(events) == 1  # no duplicate event created
    assert events[0]["detail"]["destination_provider"] == "codex"


def test_ensure_schema_fresh_db_stores_hash(store):
    assert store.get_meta("schema_hash") == store_module._SCHEMA_HASH


def test_ensure_schema_matching_hash_skips_ddl(store):
    with patch.object(JobStore, "_run_schema_ddl") as run_ddl:
        second = JobStore(store._dsn)
        second.close()

    run_ddl.assert_not_called()


def test_ensure_schema_hash_mismatch_triggers_replay(store, monkeypatch):
    # Unique per invocation: the test DB is now provisioned per pytest
    # session (conftest._provision_session_database), so concurrent pipeline
    # jobs no longer share relations — but a fixed fake hash is kept unique
    # here as defense-in-depth against any other JobStore construction within
    # the same session (e.g. parallel test workers sharing one session's DB),
    # where a fixed value could still collide with another copy of this test.
    fake_hash = f"deadbeef-{uuid.uuid4().hex}"
    real_hash = store_module._SCHEMA_HASH
    original_run_ddl = JobStore._run_schema_ddl
    monkeypatch.setattr(store_module, "_SCHEMA_HASH", fake_hash)

    with patch.object(JobStore, "_run_schema_ddl", autospec=True) as run_ddl:
        run_ddl.side_effect = original_run_ddl
        second = JobStore(store._dsn)
        second.close()

    run_ddl.assert_called()

    # Each pipeline job's pytest session now gets its own isolated database
    # (conftest._provision_session_database), but within THIS session any
    # concurrent JobStore construction still re-checks schema_hash and
    # self-heals a mismatch. Hold the same advisory lock _ensure_schema takes
    # before it replays DDL, so another process can't race in and reset the
    # hash between our assertion and the restore below.
    with psycopg.connect(store._dsn, autocommit=True) as lock_conn:
        lock_conn.execute("SELECT pg_advisory_lock(%s)", (store_module._LOCK_SCHEMA,))
        try:
            assert store.get_meta("schema_hash") == fake_hash
        finally:
            store.set_meta("schema_hash", real_hash)
            lock_conn.execute("SELECT pg_advisory_unlock(%s)", (store_module._LOCK_SCHEMA,))


def test_delete_user_removes_user_row(store):
    user = store.create_user(f"delete-user-{uuid.uuid4()}@example.com", "pw")

    assert store.delete_user(user.id) is True
    assert store.get_user_by_id(user.id) is None


def test_delete_user_removes_project_memberships(store):
    user = store.create_user(f"delete-user-membership-{uuid.uuid4()}@example.com", "pw")
    project = store.create_project("Delete User Project", f"/tmp/test-store-{uuid.uuid4()}")
    store.add_project_member(project.id, str(user.id), "viewer")

    store.delete_user(user.id)

    assert store.get_user_memberships(user.id) == []
    with store._pool.connection() as conn:
        conn.execute("DELETE FROM project_agents WHERE project_id = %s", (project.id,))
        conn.execute("DELETE FROM projects WHERE id = %s", (project.id,))


def test_authenticate_user_checks_bcrypt_for_unknown_email(store):
    with patch.object(store_module.bcrypt, "checkpw", wraps=bcrypt.checkpw) as spy:
        result = store.authenticate_user(f"unknown-{uuid.uuid4()}@example.com", "whatever")

    assert result is None
    spy.assert_called_once()


def test_authenticate_user_checks_bcrypt_for_inactive_user(store):
    user = store.create_user(f"inactive-auth-{uuid.uuid4()}@example.com", "correct-pw")
    store.update_user(user.id, is_active=False)
    try:
        with patch.object(store_module.bcrypt, "checkpw", wraps=bcrypt.checkpw) as spy:
            result = store.authenticate_user(user.email, "correct-pw")

        assert result is None
        spy.assert_called_once()
    finally:
        store.delete_user(user.id)


def test_authenticate_user_checks_bcrypt_for_oauth_only_user(store):
    user = store.provision_oauth_user(
        f"oauth-auth-{uuid.uuid4()}@example.com", "OAuth User", "google"
    )
    try:
        with patch.object(store_module.bcrypt, "checkpw", wraps=bcrypt.checkpw) as spy:
            result = store.authenticate_user(user.email, "whatever")

        assert result is None
        spy.assert_called_once()
    finally:
        store.delete_user(user.id)


def test_authenticate_user_checks_bcrypt_for_wrong_password(store):
    user = store.create_user(f"wrongpw-auth-{uuid.uuid4()}@example.com", "correct-pw")
    try:
        with patch.object(store_module.bcrypt, "checkpw", wraps=bcrypt.checkpw) as spy:
            result = store.authenticate_user(user.email, "wrong-pw")

        assert result is None
        spy.assert_called_once()
    finally:
        store.delete_user(user.id)


def test_authenticate_user_returns_user_for_correct_password(store):
    user = store.create_user(f"correctpw-auth-{uuid.uuid4()}@example.com", "correct-pw")
    try:
        with patch.object(store_module.bcrypt, "checkpw", wraps=bcrypt.checkpw) as spy:
            result = store.authenticate_user(user.email, "correct-pw")

        assert result is not None
        assert result.id == user.id
        spy.assert_called_once()
    finally:
        store.delete_user(user.id)


def test_dummy_password_hash_is_a_valid_bcrypt_hash():
    assert store_module._DUMMY_PASSWORD_HASH
    assert bcrypt.checkpw(b"anything", store_module._DUMMY_PASSWORD_HASH.encode("utf-8")) is False


@pytest.fixture
def batch_env(store):
    """A per-test fake repo path + cleanup of every job/project row it creates."""
    repo_path = f"/tmp/test-store-{uuid.uuid4()}"
    job_ids: list[int] = []

    def make_batch(jobs_spec: list[dict]) -> list:
        jobs = store.create_batch(repo_path, jobs_spec, chat_id=1)
        job_ids.extend(j.id for j in jobs)
        return jobs

    yield make_batch
    with store._pool.connection() as conn:
        if job_ids:
            conn.execute(
                "DELETE FROM job_dependencies WHERE job_id = ANY(%s) OR depends_on_job_id = ANY(%s)",
                (job_ids, job_ids),
            )
            conn.execute("DELETE FROM job_events WHERE job_id = ANY(%s)", (job_ids,))
            conn.execute("DELETE FROM jobs WHERE id = ANY(%s)", (job_ids,))
        row = conn.execute("SELECT id FROM projects WHERE repo_path = %s", (repo_path,)).fetchone()
        if row:
            conn.execute("DELETE FROM project_agents WHERE project_id = %s", (row["id"],))
            conn.execute("DELETE FROM projects WHERE id = %s", (row["id"],))


_JOB_1135_IDEA = (
    "FRONTEND ONLY. Files: new frontend/src/components/page-header.tsx + "
    "a/page.tsx, b/page.tsx, c/page.tsx, d/page.tsx, e/page.tsx, f/page.tsx, "
    "g/page.tsx"
)


def test_create_derives_scope_from_full_idea_when_caller_supplies_none(store):
    project = store.create_project("Scope Fallback Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        job = store.create(idea=_JOB_1135_IDEA, repo_path=project.repo_path, chat_id=1)

        assert job.source_meta["scope"]["allowed_paths"] == [
            "frontend/src/components/page-header.tsx",
            "a/page.tsx",
            "b/page.tsx",
            "c/page.tsx",
            "d/page.tsx",
            "e/page.tsx",
            "f/page.tsx",
            "g/page.tsx",
        ]
    finally:
        store.delete_project(project.id)


def test_create_leaves_explicit_scope_untouched(store):
    project = store.create_project("Explicit Scope Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        job = store.create(
            idea=_JOB_1135_IDEA,
            repo_path=project.repo_path,
            chat_id=1,
            source_meta={"scope": {"allowed_paths": ["only/this.py"], "interfaces": ""}},
        )

        assert job.source_meta["scope"] == {"allowed_paths": ["only/this.py"], "interfaces": ""}
    finally:
        store.delete_project(project.id)


def test_create_batch_persists_scope_and_priority_on_scoped_job(store, batch_env):
    jobs = batch_env(
        [
            {
                "idea": "Add helper",
                "title": "Add helper",
                "depends_on": [],
                "target_files": [],
                "priority": 5,
                "scope": {"allowed_paths": ["a.py"], "interfaces": "exposes foo()"},
            }
        ]
    )

    reloaded = store.get(jobs[0].id)

    assert reloaded.priority == 5
    assert reloaded.source_meta["scope"] == {
        "allowed_paths": ["a.py"],
        "interfaces": "exposes foo()",
    }


def test_create_batch_leaves_source_meta_unchanged_for_manifest_less_job(store, batch_env):
    jobs = batch_env(
        [
            {
                "idea": "Plain job",
                "title": "Plain job",
                "depends_on": [],
                "target_files": [],
                "priority": 0,
                "scope": None,
            }
        ]
    )

    reloaded = store.get(jobs[0].id)

    assert reloaded.priority == 0
    assert reloaded.source_meta == {}


def test_create_batch_derives_scope_from_idea_when_spec_has_no_scope(store, batch_env):
    scoped, fallback = batch_env(
        [
            {
                "idea": "Explicit scope job",
                "title": "Explicit scope job",
                "depends_on": [],
                "target_files": [],
                "priority": 0,
                "scope": {"allowed_paths": ["only/this.py"], "interfaces": ""},
            },
            {
                "idea": _JOB_1135_IDEA,
                "title": "Fallback scope job",
                "depends_on": [],
                "target_files": [],
                "priority": 0,
            },
        ]
    )

    reloaded_scoped = store.get(scoped.id)
    reloaded_fallback = store.get(fallback.id)

    assert reloaded_scoped.source_meta["scope"] == {
        "allowed_paths": ["only/this.py"],
        "interfaces": "",
    }
    assert reloaded_fallback.source_meta["scope"]["allowed_paths"] == [
        "frontend/src/components/page-header.tsx",
        "a/page.tsx",
        "b/page.tsx",
        "c/page.tsx",
        "d/page.tsx",
        "e/page.tsx",
        "f/page.tsx",
        "g/page.tsx",
    ]


def test_create_batch_rejects_out_of_range_depends_on_index(store, batch_env):
    marker = str(uuid.uuid4())

    with pytest.raises(ValueError, match="depends_on index 5 out of range for batch of size 2"):
        batch_env(
            [
                {
                    "idea": f"job A {marker}",
                    "title": "A",
                    "depends_on": [],
                    "target_files": [],
                    "priority": 0,
                },
                {
                    "idea": f"job B {marker}",
                    "title": "B",
                    "depends_on": [5],
                    "target_files": [],
                    "priority": 0,
                },
            ]
        )

    with store._pool.connection() as conn:
        rows = conn.execute("SELECT id FROM jobs WHERE idea LIKE %s", (f"%{marker}%",)).fetchall()
    assert rows == []


def test_create_with_same_idempotency_key_returns_existing_job(store):
    project = store.create_project("Idempotency Key Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        key = str(uuid.uuid4())
        first = store.create(
            idea="First idea", repo_path=project.repo_path, chat_id=1, idempotency_key=key
        )
        second = store.create(
            idea="Second idea, different text",
            repo_path=project.repo_path,
            chat_id=1,
            idempotency_key=key,
        )

        assert second.id == first.id
        with store._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE project_id = %s AND idempotency_key = %s",
                (project.id, key),
            ).fetchall()
        assert [r["id"] for r in rows] == [first.id]
    finally:
        store.delete_project(project.id)


def test_create_with_different_idempotency_keys_creates_distinct_jobs(store):
    project = store.create_project("Idempotency Key Project 2", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        first = store.create(
            idea="Idea A",
            repo_path=project.repo_path,
            chat_id=1,
            idempotency_key=str(uuid.uuid4()),
        )
        second = store.create(
            idea="Idea B",
            repo_path=project.repo_path,
            chat_id=1,
            idempotency_key=str(uuid.uuid4()),
        )

        assert first.id != second.id
    finally:
        store.delete_project(project.id)


def test_create_with_omitted_idempotency_key_creates_distinct_jobs(store):
    project = store.create_project(
        "Idempotency Key Omitted Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        first = store.create(idea="Idea C", repo_path=project.repo_path, chat_id=1)
        second = store.create(idea="Idea D", repo_path=project.repo_path, chat_id=1)

        assert first.id != second.id
        with store._pool.connection() as conn:
            rows = conn.execute(
                "SELECT idempotency_key FROM jobs WHERE id = ANY(%s)",
                ([first.id, second.id],),
            ).fetchall()
        assert all(r["idempotency_key"] is None for r in rows)
    finally:
        store.delete_project(project.id)


def test_get_by_idempotency_key_scoped_per_project(store):
    project_a = store.create_project("Idempotency Lookup A", f"/tmp/test-store-{uuid.uuid4()}")
    project_b = store.create_project("Idempotency Lookup B", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        key = str(uuid.uuid4())
        job_a = store.create(
            idea="Project A job", repo_path=project_a.repo_path, chat_id=1, idempotency_key=key
        )

        assert store.get_by_idempotency_key(project_a.id, "unknown-key") is None
        assert store.get_by_idempotency_key(project_a.id, key) == job_a
        # Same key reused in a different project must not collide.
        assert store.get_by_idempotency_key(project_b.id, key) is None
    finally:
        store.delete_project(project_a.id)
        store.delete_project(project_b.id)


def test_create_batch_resolves_idempotency_key_against_existing_job(store):
    project = store.create_project("Idempotency Batch Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        key = str(uuid.uuid4())
        existing = store.create(
            idea="Pre-existing job",
            repo_path=project.repo_path,
            chat_id=1,
            idempotency_key=key,
        )

        jobs = store.create_batch(
            project.repo_path,
            [
                {
                    "idea": "Sibling job",
                    "title": "Sibling job",
                    "depends_on": [1],
                    "target_files": [],
                    "priority": 0,
                },
                {
                    "idea": "Should dedup to existing job",
                    "title": "Should dedup to existing job",
                    "depends_on": [],
                    "target_files": [],
                    "priority": 0,
                    "idempotency_key": key,
                },
            ],
            chat_id=1,
        )

        sibling, deduped = jobs
        assert deduped.id == existing.id
        assert sibling.id != existing.id
        assert store.get_dependencies(sibling.id) == [existing.id]
        with store._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE project_id = %s AND idempotency_key = %s",
                (project.id, key),
            ).fetchall()
        assert [r["id"] for r in rows] == [existing.id]
    finally:
        store.delete_project(project.id)


def test_create_batch_serializes_glob_overlap_and_not_unknown_scope(store, batch_env):
    glob_job, literal_job, unknown_job = batch_env(
        [
            {"idea": "glob", "scope": {"allowed_paths": ["src/*.py"]}},
            {"idea": "literal", "scope": {"allowed_paths": ["src/store.py"]}},
            {"idea": "unknown", "scope": None},
        ]
    )

    assert store.get_dependencies(glob_job.id) == []
    assert store.get_dependencies(literal_job.id) == [glob_job.id]
    assert store.get_dependencies(unknown_job.id) == []
    with store._pool.connection() as conn:
        row = conn.execute(
            "SELECT provenance FROM job_dependencies WHERE job_id=%s AND depends_on_job_id=%s",
            (literal_job.id, glob_job.id),
        ).fetchone()
    assert row["provenance"] == DependencyProvenance.AUTO.value


def test_reconcile_auto_dependencies_removes_disjoint_edge_and_is_idempotent(store, batch_env):
    first, second = batch_env(
        [
            {"idea": "first", "scope": {"allowed_paths": ["src/a.py"]}},
            {"idea": "second", "scope": {"allowed_paths": ["src/b.py"]}},
        ]
    )
    store.add_job_dependency(second.id, first.id, DependencyProvenance.AUTO)

    changes = store.reconcile_auto_dependencies(first.project_id)

    assert changes == [
        {
            "action": "removed",
            "job_id": second.id,
            "depends_on_job_id": first.id,
            "provenance": "auto",
            "reason": "known_disjoint_scopes",
        }
    ]
    assert store.get_dependencies(second.id) == []
    assert store.reconcile_auto_dependencies(first.project_id) == []


def test_reconcile_auto_dependencies_preserves_protected_and_unknown_edges(store, batch_env):
    known, disjoint, unknown = batch_env(
        [
            {"idea": "known", "scope": {"allowed_paths": ["src/a.py"]}},
            {"idea": "disjoint", "scope": {"allowed_paths": ["src/b.py"]}},
            {"idea": "unknown", "scope": None},
        ]
    )
    store.add_job_dependency(disjoint.id, known.id)
    store.add_job_dependency(unknown.id, known.id, DependencyProvenance.AUTO)

    assert store.reconcile_auto_dependencies(known.project_id) == []
    assert store.get_dependencies(disjoint.id) == [known.id]
    assert store.get_dependencies(unknown.id) == [known.id]


def test_claim_skips_job_whose_manifest_conflicts_with_a_running_job(store, batch_env):
    import time

    scoped_a, scoped_b, plain = batch_env(
        [
            {
                "idea": "Touch store.py",
                "title": "Touch store.py",
                "depends_on": [],
                "target_files": [],
                "priority": 0,
                "scope": {"allowed_paths": ["hyqs/pipeline/store.py"], "interfaces": ""},
            },
            {
                "idea": "Also touch store.py",
                "title": "Also touch store.py",
                "depends_on": [],
                "target_files": [],
                "priority": 0,
                "scope": {"allowed_paths": ["hyqs/pipeline/store.py"], "interfaces": ""},
            },
            {
                "idea": "Manifest-less job",
                "title": "Manifest-less job",
                "depends_on": [],
                "target_files": [],
                "priority": 0,
                "scope": None,
            },
        ]
    )
    for job in (scoped_a, scoped_b, plain):
        job.stage = Stage.LINT
        store.save(job)
    scoped_a.status = JobStatus.RUNNING
    store.save(scoped_a)

    now = time.time()
    claimed_plain = asyncio.run(store.claim("worker-plain", now, 90, project_id=plain.project_id))
    claimed_conflict = asyncio.run(
        store.claim("worker-conflict", now, 90, project_id=plain.project_id)
    )

    assert claimed_plain is not None and claimed_plain.id == plain.id
    assert claimed_conflict is None


def test_get_effective_priority_bypasses_boost_computation_for_non_pending_job(store, dep_env):
    parent = dep_env(idea="remediation parent")
    job = dep_env(
        idea="remediation job that is already running",
        priority=7,
        source_meta={"ai_fix_for": parent.id},
    )
    job.status = JobStatus.RUNNING
    store.save(job)
    reloaded = store.get(job.id)

    assert store.get_effective_priority(reloaded) == (7, [])


def test_get_effective_priority_returns_base_priority_when_no_boost_applies(store, dep_env):
    job = dep_env(idea="plain pending job", priority=3)

    assert store.get_effective_priority(job) == (3, [])


def test_get_effective_priority_applies_remediation_boost(store, dep_env):
    parent = dep_env(idea="remediation parent")
    job = dep_env(
        idea="remediation job",
        priority=10,
        source_meta={"ai_fix_for": parent.id},
    )

    effective, reasons = store.get_effective_priority(job)

    assert effective == 10 + REMEDIATION_PRIORITY_BOOST
    assert any(reason["reason"] == "remediation" for reason in reasons)


def test_get_effective_priority_applies_and_clears_critical_path_boost(store, dep_env):
    job = dep_env(idea="depended-upon job", priority=5)
    dependent = dep_env(idea="unresolved dependent", depends_on=[job.id])

    effective, reasons = store.get_effective_priority(job)
    assert effective > 5
    assert any(reason["reason"] == "critical_path" for reason in reasons)

    dependent.status = JobStatus.DONE
    store.save(dependent)

    effective_after, reasons_after = store.get_effective_priority(store.get(job.id))
    assert effective_after == 5
    assert not any(reason["reason"] == "critical_path" for reason in reasons_after)


def test_claim_prefers_boosted_candidate_at_equal_base_priority(store, dep_env):
    plain = dep_env(idea="plain candidate", priority=5)
    remediation_parent = dep_env(idea="remediation parent")
    boosted = dep_env(
        idea="boosted candidate",
        priority=5,
        source_meta={"ai_fix_for": remediation_parent.id},
    )
    for job in (plain, boosted):
        job.stage = Stage.LINT  # deterministic stage: claimable with no project agent roster
        store.save(job)

    claimed = asyncio.run(store.claim("worker-boost", time.time(), 90, project_id=plain.project_id))

    assert claimed is not None and claimed.id == boosted.id


def test_claim_still_skips_boosted_job_whose_manifest_conflicts_with_running(store, dep_env):
    remediation_parent = dep_env(idea="remediation parent")
    remediation_parent.status = JobStatus.DONE
    remediation_parent.stage = Stage.DONE
    store.save(remediation_parent)  # resolved already; excluded from claim() candidates
    running = dep_env(
        idea="running job",
        source_meta={"scope": {"allowed_paths": ["hyqs/pipeline/store.py"]}},
    )
    conflicting = dep_env(
        idea="boosted conflicting job",
        priority=0,
        source_meta={
            "scope": {"allowed_paths": ["hyqs/pipeline/store.py"]},
            "ai_fix_for": remediation_parent.id,
        },
    )
    eligible = dep_env(idea="eligible job", priority=0)
    for job in (running, conflicting, eligible):
        job.stage = Stage.LINT  # deterministic stage: claimable with no project agent roster
        store.save(job)
    running.status = JobStatus.RUNNING
    store.save(running)

    claimed = asyncio.run(
        store.claim("worker-boost-conflict", time.time(), 90, project_id=running.project_id)
    )

    assert claimed is not None and claimed.id == eligible.id


# ---------------------------------------------------------------------------
# get_effective_priority / claim / claimable: critical-path breadth + depth,
# aging boost, and boost-vs-gate ranking regressions (job #3209)
# ---------------------------------------------------------------------------


def test_get_effective_priority_caps_critical_path_boost_for_wide_fanout(store, dep_env):
    """Many direct unresolved dependents alone should saturate the cap, with
    the depth term (pegged at 1) contributing only a small remainder."""
    job = dep_env(idea="wide fanout root", priority=5)
    dependents = [dep_env(idea=f"fanout dependent {i}", depends_on=[job.id]) for i in range(12)]
    assert len(dependents) * CRITICAL_PATH_BOOST_PER_DEPENDENT > MAX_CRITICAL_PATH_BOOST

    effective, reasons = store.get_effective_priority(job)

    critical_path = [r for r in reasons if r["reason"] == "critical_path"]
    assert len(critical_path) == 1
    assert critical_path[0]["amount"] == MAX_CRITICAL_PATH_BOOST
    assert effective == 5 + MAX_CRITICAL_PATH_BOOST


def test_get_effective_priority_caps_critical_path_boost_for_deep_chain(store, dep_env):
    """A single-strand chain has far fewer total dependents than the wide
    fanout case, but the depth term alone pushes it up to the same cap —
    distinguishing depth-driven capping from dependent-count-driven capping."""
    chain_length = 6
    job = dep_env(idea="deep chain root", priority=5)
    prev = job
    for i in range(chain_length):
        prev = dep_env(idea=f"chain link {i}", depends_on=[prev.id])
    uncapped_amount = (
        chain_length * CRITICAL_PATH_BOOST_PER_DEPENDENT
        + chain_length * CRITICAL_PATH_BOOST_PER_DEPTH
    )
    assert uncapped_amount >= MAX_CRITICAL_PATH_BOOST
    assert chain_length < 12  # fewer total dependents than the wide-fanout case

    effective, reasons = store.get_effective_priority(job)

    critical_path = [r for r in reasons if r["reason"] == "critical_path"]
    assert len(critical_path) == 1
    assert critical_path[0]["amount"] == MAX_CRITICAL_PATH_BOOST
    assert effective == 5 + MAX_CRITICAL_PATH_BOOST


def test_get_effective_priority_critical_path_boost_shrinks_as_dependents_resolve(store, dep_env):
    job = dep_env(idea="shrinking critical path root", priority=5)
    d1 = dep_env(idea="shrink dependent 1", depends_on=[job.id])
    d2 = dep_env(idea="shrink dependent 2", depends_on=[job.id])
    d3 = dep_env(idea="shrink dependent 3", depends_on=[job.id])

    effective_initial, _ = store.get_effective_priority(job)

    d1.status = JobStatus.DONE
    store.save(d1)
    effective_after_one, _ = store.get_effective_priority(store.get(job.id))

    d2.status = JobStatus.DONE
    store.save(d2)
    effective_after_two, _ = store.get_effective_priority(store.get(job.id))

    d3.status = JobStatus.DONE
    store.save(d3)
    effective_after_all, _ = store.get_effective_priority(store.get(job.id))

    assert effective_initial > effective_after_one > effective_after_two > effective_after_all
    assert effective_after_all == 5


def test_get_effective_priority_clears_critical_path_boost_when_dependent_cancelled(store, dep_env):
    job = dep_env(idea="depended-upon job cancelled clear", priority=5)
    dependent = dep_env(idea="unresolved dependent to cancel", depends_on=[job.id])

    effective, reasons = store.get_effective_priority(job)
    assert effective > 5
    assert any(reason["reason"] == "critical_path" for reason in reasons)

    dependent.status = JobStatus.CANCELLED
    store.save(dependent)

    effective_after, reasons_after = store.get_effective_priority(store.get(job.id))
    assert effective_after == 5
    assert not any(reason["reason"] == "critical_path" for reason in reasons_after)


def test_get_effective_priority_applies_uncapped_aging_boost(store, dep_env):
    job = dep_env(idea="moderately aged job", priority=2)
    backdated = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
    with store._pool.connection() as conn:
        conn.execute("UPDATE jobs SET created_at = %s WHERE id = %s", (backdated, job.id))

    effective, reasons = store.get_effective_priority(store.get(job.id))

    aging = [r for r in reasons if r["reason"] == "aging"]
    assert len(aging) == 1
    assert aging[0]["amount"] == 4 * PRIORITY_AGING_PER_HOUR
    assert effective == 2 + 4 * PRIORITY_AGING_PER_HOUR


def test_get_effective_priority_caps_aging_boost_for_very_old_job(store, dep_env):
    job = dep_env(idea="ancient job", priority=1)
    backdated = (datetime.now(timezone.utc) - timedelta(hours=1000)).isoformat()
    with store._pool.connection() as conn:
        conn.execute("UPDATE jobs SET created_at = %s WHERE id = %s", (backdated, job.id))

    effective, reasons = store.get_effective_priority(store.get(job.id))

    aging = [r for r in reasons if r["reason"] == "aging"]
    assert len(aging) == 1
    assert aging[0]["amount"] == MAX_PRIORITY_AGING_BOOST
    assert effective == 1 + MAX_PRIORITY_AGING_BOOST


def test_get_effective_priority_returns_base_priority_for_done_job(store, dep_env):
    job = dep_env(idea="already done job", priority=9)
    job.status = JobStatus.DONE
    store.save(job)

    assert store.get_effective_priority(store.get(job.id)) == (9, [])


def test_claim_prefers_lower_base_priority_boosted_job_over_higher_base_priority(store, dep_env):
    plain = dep_env(idea="plain higher base priority", priority=10)
    remediation_parent = dep_env(idea="remediation parent for base-priority ranking")
    boosted = dep_env(
        idea="boosted lower base priority",
        priority=0,
        source_meta={"ai_fix_for": remediation_parent.id},
    )
    for job in (plain, boosted):
        job.stage = Stage.LINT
        store.save(job)
    assert 0 + REMEDIATION_PRIORITY_BOOST > 10

    claimed = asyncio.run(
        store.claim("worker-base-priority-rank", time.time(), 90, project_id=plain.project_id)
    )

    assert claimed is not None and claimed.id == boosted.id


def test_claim_prefers_job_with_more_unresolved_dependents_at_equal_base_priority(store, dep_env):
    few_dependents = dep_env(idea="job with one dependent", priority=5)
    dep_env(idea="the one dependent", depends_on=[few_dependents.id])

    many_dependents = dep_env(idea="job with several dependents", priority=5)
    for i in range(3):
        dep_env(idea=f"dependent-count dependent {i}", depends_on=[many_dependents.id])

    for job in (few_dependents, many_dependents):
        job.stage = Stage.LINT
        store.save(job)

    claimed = asyncio.run(
        store.claim(
            "worker-dependent-count-rank", time.time(), 90, project_id=few_dependents.project_id
        )
    )

    assert claimed is not None and claimed.id == many_dependents.id


def test_claimable_prefers_lower_base_priority_boosted_job_over_higher_base_priority(
    store, dep_env
):
    plain = dep_env(idea="claimable plain higher base priority", priority=10)
    remediation_parent = dep_env(idea="claimable remediation parent for ranking")
    boosted = dep_env(
        idea="claimable boosted lower base priority",
        priority=0,
        source_meta={"ai_fix_for": remediation_parent.id},
    )
    for job in (plain, boosted):
        job.stage = Stage.LINT
        store.save(job)

    claimed = store.claimable()

    assert claimed is not None and claimed.id == boosted.id


def test_claim_and_claimable_break_effective_priority_ties_by_lowest_job_id(store, dep_env):
    job_a = dep_env(idea="tie candidate a (lower id)", priority=5)
    job_b = dep_env(idea="tie candidate b (higher id)", priority=5)
    for job in (job_a, job_b):
        job.stage = Stage.LINT
        store.save(job)
    assert job_a.id < job_b.id

    for _ in range(3):
        assert store.claimable().id == job_a.id

    claimed = asyncio.run(
        store.claim("worker-tie-break", time.time(), 90, project_id=job_a.project_id)
    )
    assert claimed is not None and claimed.id == job_a.id


def test_claim_skips_boosted_job_with_unsatisfied_dependency_for_eligible_job(store, dep_env):
    remediation_parent = dep_env(idea="remediation parent for dependency gate")
    remediation_parent.status = JobStatus.DONE
    remediation_parent.stage = Stage.DONE
    store.save(remediation_parent)  # resolved; only used to mark `gated` as a remediation job

    parent = dep_env(idea="unsatisfied dependency parent")
    parent.status = JobStatus.RUNNING  # not DONE: dependency stays unsatisfied, and parent
    store.save(parent)  # itself is no longer a PENDING claim candidate

    gated = dep_env(
        idea="boosted but blocked by dependency",
        priority=0,
        depends_on=[parent.id],
        source_meta={"ai_fix_for": remediation_parent.id},
    )
    gated.stage = Stage.LINT
    store.save(gated)

    eligible = dep_env(idea="eligible unblocked job", priority=-5)
    eligible.stage = Stage.LINT
    store.save(eligible)

    claimed = asyncio.run(
        store.claim("worker-dependency-gate", time.time(), 90, project_id=gated.project_id)
    )

    assert claimed is not None and claimed.id == eligible.id


def test_claim_skips_boosted_deploy_job_pinned_to_a_different_host(store, dep_env):
    remediation_parent = dep_env(idea="remediation parent for host-pin gate")
    remediation_parent.status = JobStatus.DONE
    remediation_parent.stage = Stage.DONE
    store.save(remediation_parent)

    job = dep_env(
        idea="boosted deploy job pinned to different host",
        priority=0,
        source_meta={"ai_fix_for": remediation_parent.id},
    )
    job.stage = Stage.DEPLOY
    store.save(job)
    env = store.get_or_create_default_environment(job.project_id)
    host = store.create_host(f"host-{uuid.uuid4()}")
    store.set_environment_host(env.id, host.id)

    claimed = asyncio.run(
        store.claim(
            "worker-host-pin-gate", time.time(), 90, project_id=job.project_id, worker_host_id=None
        )
    )

    assert claimed is None


def test_claim_skips_boosted_job_outside_stage_allowlist_for_matching_job(store, dep_env):
    remediation_parent = dep_env(idea="remediation parent for stage-allowlist gate")
    remediation_parent.status = JobStatus.DONE
    remediation_parent.stage = Stage.DONE
    store.save(remediation_parent)

    gated = dep_env(
        idea="boosted job outside allowlist",
        priority=0,
        source_meta={"ai_fix_for": remediation_parent.id},
    )
    gated.stage = Stage.PLAN
    store.save(gated)

    matching = dep_env(idea="unboosted job matching allowlist", priority=-5)
    matching.stage = Stage.DEPLOY
    store.save(matching)

    claimed = asyncio.run(
        store.claim(
            "worker-stage-allowlist-gate",
            time.time(),
            90,
            project_id=gated.project_id,
            stage_allowlist={Stage.DEPLOY},
        )
    )

    assert claimed is not None and claimed.id == matching.id


def test_claim_skips_boosted_job_when_only_capable_provider_is_paused(
    store, dep_env, clean_provider_pauses
):
    remediation_parent = dep_env(idea="remediation parent for provider-pause gate")
    remediation_parent.status = JobStatus.DONE
    remediation_parent.stage = Stage.DONE
    store.save(remediation_parent)

    gated = dep_env(
        idea="boosted job needing paused provider",
        priority=0,
        source_meta={"ai_fix_for": remediation_parent.id},
    )  # default QUEUED stage needs AgentTask.PLAN, served only by the seeded claude agent

    eligible = dep_env(idea="eligible job on deterministic stage", priority=-5)
    eligible.stage = Stage.LINT
    store.save(eligible)

    store.set_provider_pause("claude", time.time() + 3600)

    claimed = asyncio.run(
        store.claim("worker-provider-pause-gate", time.time(), 90, project_id=gated.project_id)
    )

    assert claimed is not None and claimed.id == eligible.id


def test_find_manifest_conflict_returns_none_when_project_id_is_none(store):
    with store._pool.connection() as conn:
        assert store._find_manifest_conflict(conn, None, 1, ["a.py"]) is None


def test_find_manifest_conflict_returns_none_when_no_running_job_conflicts(store, batch_env):
    (scoped,) = batch_env(
        [
            {
                "idea": "Touch store.py",
                "title": "Touch store.py",
                "depends_on": [],
                "target_files": [],
                "priority": 0,
                "scope": {"allowed_paths": ["hyqs/pipeline/store.py"], "interfaces": ""},
            },
        ]
    )

    with store._pool.connection() as conn:
        result = store._find_manifest_conflict(
            conn, scoped.project_id, scoped.id, ["hyqs/pipeline/store.py"]
        )

    assert result is None


def test_find_manifest_conflict_returns_first_conflicting_running_job_and_overlap(store, batch_env):
    scoped_a, scoped_b = batch_env(
        [
            {
                "idea": "Touch store.py",
                "title": "Touch store.py",
                "depends_on": [],
                "target_files": [],
                "priority": 0,
                "scope": {"allowed_paths": ["hyqs/pipeline/store.py"], "interfaces": ""},
            },
            {
                "idea": "Touch store.py and models.py",
                "title": "Touch store.py and models.py",
                "depends_on": [],
                "target_files": [],
                "priority": 0,
                "scope": {
                    "allowed_paths": ["hyqs/pipeline/store.py", "hyqs/pipeline/models.py"],
                    "interfaces": "",
                },
            },
        ]
    )
    scoped_a.status = JobStatus.RUNNING
    store.save(scoped_a)
    allowed_paths = scoped_b.source_meta["scope"]["allowed_paths"]

    with store._pool.connection() as conn:
        result = store._find_manifest_conflict(
            conn, scoped_b.project_id, scoped_b.id, allowed_paths
        )
        still_conflicts = store._manifest_conflicts_with_running(
            conn, scoped_b.project_id, scoped_b.id, allowed_paths
        )

    assert result == (scoped_a.id, ["hyqs/pipeline/store.py"])
    assert still_conflicts is True


def test_expand_job_scope_appends_paths_and_records_justification(store, batch_env):
    jobs = batch_env(
        [
            {
                "idea": "Add helper",
                "title": "Add helper",
                "depends_on": [],
                "target_files": [],
                "priority": 0,
                "scope": {"allowed_paths": ["a.py"], "interfaces": "exposes foo()"},
            }
        ]
    )

    store.expand_job_scope(jobs[0].id, ["b.py"], "needed a shared helper in b.py")

    reloaded = store.get(jobs[0].id)
    scope = reloaded.source_meta["scope"]
    assert scope["allowed_paths"] == ["a.py", "b.py"]
    assert scope["expansions"][0]["paths"] == ["b.py"]
    assert scope["expansions"][0]["justification"] == "needed a shared helper in b.py"


def test_expand_job_scope_unions_plan_targets_when_manifest_was_unset(store, dep_env):
    job = dep_env(idea="Add a remediation helper")
    job.plan = {
        "summary": "s",
        "stories": [{"id": "S1", "target_files": ["hyqs/pipeline/remediation.py"]}],
        "reuses": [],
        "adds": [],
    }
    store.save(job)

    store.expand_job_scope(job.id, ["tests/test_remediation.py"], "bandit false-positive")

    reloaded = store.get(job.id)
    allowed_paths = reloaded.source_meta["scope"]["allowed_paths"]
    assert "hyqs/pipeline/remediation.py" in allowed_paths
    assert "tests/test_remediation.py" in allowed_paths


def test_expand_job_scope_unions_plan_targets_when_manifest_already_exists(store, batch_env):
    jobs = batch_env(
        [
            {
                "idea": "Add helper",
                "title": "Add helper",
                "depends_on": [],
                "target_files": [],
                "priority": 0,
                "scope": {"allowed_paths": ["a.py"], "interfaces": ""},
            }
        ]
    )
    job = jobs[0]
    job.plan = {
        "summary": "s",
        "stories": [{"id": "S1", "target_files": ["a.py"]}],
        "reuses": [],
        "adds": [],
    }
    store.save(job)

    store.expand_job_scope(job.id, ["b.py"], "needed a shared helper")

    reloaded = store.get(job.id)
    allowed_paths = reloaded.source_meta["scope"]["allowed_paths"]
    assert allowed_paths == ["a.py", "b.py"]


def test_expand_job_scope_is_idempotent_when_applied_twice(store, batch_env):
    jobs = batch_env(
        [
            {
                "idea": "Add helper",
                "title": "Add helper",
                "depends_on": [],
                "target_files": [],
                "priority": 0,
                "scope": {"allowed_paths": ["a.py"], "interfaces": ""},
            }
        ]
    )
    job = jobs[0]
    job.plan = {
        "summary": "s",
        "stories": [{"id": "S1", "target_files": ["a.py"]}],
        "reuses": [],
        "adds": [],
    }
    store.save(job)

    store.expand_job_scope(job.id, ["b.py"], "needed a shared helper")
    store.expand_job_scope(job.id, ["b.py"], "needed a shared helper")

    reloaded = store.get(job.id)
    allowed_paths = reloaded.source_meta["scope"]["allowed_paths"]
    assert allowed_paths == ["a.py", "b.py"]
    assert allowed_paths.count("b.py") == 1


def test_reconcile_plan_scope_seeds_manifest_from_plan_and_idea(store, dep_env):
    job = dep_env(idea="Add a widget for the dashboard")
    plan = {
        "summary": "s",
        "stories": [{"id": "S1", "target_files": ["src/widget.py", "tests/test_widget.py"]}],
        "reuses": [],
        "adds": [],
    }
    assert job.source_meta.get("scope") is None

    added = store.reconcile_plan_scope(job.id, job.idea, plan)

    assert sorted(added) == ["src/widget.py", "tests/test_widget.py"]
    reloaded = store.get(job.id)
    allowed_paths = reloaded.source_meta["scope"]["allowed_paths"]
    assert "src/widget.py" in allowed_paths
    assert "tests/test_widget.py" in allowed_paths


def test_reconcile_plan_scope_is_noop_when_already_covered(store, batch_env):
    jobs = batch_env(
        [
            {
                "idea": "Add helper",
                "title": "Add helper",
                "depends_on": [],
                "target_files": [],
                "priority": 0,
                "scope": {"allowed_paths": ["a.py", "tests/test_a.py"], "interfaces": ""},
            }
        ]
    )
    job = jobs[0]
    plan = {
        "summary": "s",
        "stories": [{"id": "S1", "target_files": ["tests/test_a.py"]}],
        "reuses": [],
        "adds": [],
    }

    added = store.reconcile_plan_scope(job.id, job.idea, plan)

    assert added == []
    reloaded = store.get(job.id)
    assert reloaded.source_meta["scope"]["allowed_paths"] == ["a.py", "tests/test_a.py"]


def test_reconcile_plan_scope_composes_with_expand_job_scope(store, dep_env):
    job = dep_env(idea="Add a widget for the dashboard")
    plan = {
        "summary": "s",
        "stories": [{"id": "S1", "target_files": ["src/widget.py", "tests/test_widget.py"]}],
        "reuses": [],
        "adds": [],
    }

    store.reconcile_plan_scope(job.id, job.idea, plan)
    store.expand_job_scope(job.id, ["src/helper.py"], "needed a shared helper")

    reloaded = store.get(job.id)
    allowed_paths = reloaded.source_meta["scope"]["allowed_paths"]
    assert "src/widget.py" in allowed_paths
    assert "tests/test_widget.py" in allowed_paths
    assert "src/helper.py" in allowed_paths


def test_freeze_validated_plan_scope_is_exact_and_preserves_source_meta(store, dep_env):
    job = dep_env(
        idea="Target files: stale.py",
        source_meta={
            "filing_channel": "supervisor",
            "scope": {"allowed_paths": ["stale.py"], "interfaces": "stable"},
        },
    )

    store.freeze_validated_plan_scope(
        job.id,
        ["src/a.py", "tests/test_a.py"],
        {
            "candidate_count": 3,
            "final_manifest_count": 2,
            "correction_count": 1,
            "reason_codes": [],
            "provenance": ["nearest_test"],
            "input_tokens": 42,
        },
    )

    reloaded = store.get(job.id)
    assert reloaded.source_meta["filing_channel"] == "supervisor"
    assert reloaded.source_meta["scope"] == {
        "allowed_paths": ["src/a.py", "tests/test_a.py"],
        "interfaces": "stable",
        "frozen": True,
    }
    assert reloaded.source_meta["planning"]["candidate_count"] == 3
    assert reloaded.source_meta["planning"]["input_tokens"] == 42
    assert "prompt" not in reloaded.source_meta["planning"]


def _scope_amendment(
    *accepted: tuple[str, tuple[str, ...]],
    rejected: tuple[tuple[str, str], ...] = (),
) -> ScopeAmendmentDecision:
    return ScopeAmendmentDecision(
        accepted=tuple(ScopeAmendmentAccepted(path, provenance) for path, provenance in accepted),
        rejected=tuple(ScopeAmendmentRejected(path, reason) for path, reason in rejected),
    )


def test_append_scope_amendment_preserves_manifest_and_records_successive_evidence(store, dep_env):
    original_scope = {
        "allowed_paths": ["src/original.py"],
        "interfaces": {"api": "frozen"},
        "frozen": True,
    }
    job = dep_env(
        idea="legacy amendment target",
        source_meta={"scope": original_scope, "filing_channel": "supervisor"},
    )

    first = store.append_scope_amendment(
        job.id,
        _scope_amendment(("tests/test_original.py", ("coverage", "nearest_test"))),
        authorizing_gate="test",
        failure_event_id="test:failure:1",
        expected_prior_sequence=0,
        cumulative_limit=3,
    )
    second = store.append_scope_amendment(
        job.id,
        _scope_amendment(
            ("src/export.py", ("import",)),
            rejected=(("deploy/release.sh", "sensitive_path"),),
        ),
        authorizing_gate="lint",
        failure_event_id="lint:failure:2",
        expected_prior_sequence=1,
        cumulative_limit=3,
    )

    assert first.status == "applied"
    assert second.status == "applied"
    assert second.sequence == 2
    reloaded = store.get(job.id)
    assert reloaded.source_meta["filing_channel"] == "supervisor"
    assert reloaded.source_meta["scope"] == {
        **original_scope,
        "allowed_paths": ["src/original.py", "tests/test_original.py", "src/export.py"],
    }
    amendments = reloaded.source_meta["scope_amendments"]
    assert amendments["sequence"] == 2
    assert amendments["cumulative_count"] == 2
    assert amendments["ledger"][0]["accepted"] == [
        {
            "path": "tests/test_original.py",
            "provenance": ["coverage", "nearest_test"],
        }
    ]
    assert amendments["ledger"][0]["authorizing_gate"] == "test"
    assert amendments["ledger"][0]["failure_event_id"] == "test:failure:1"
    assert amendments["ledger"][1]["rejected"] == [
        {"candidate": "deploy/release.sh", "reason_code": "sensitive_path"}
    ]
    assert amendments["ledger"][1]["cumulative_allowed_count"] == 3


def test_append_scope_amendment_duplicate_event_and_path_are_noops(store, dep_env):
    job = dep_env(source_meta={"scope": {"allowed_paths": ["src/original.py"], "frozen": True}})
    decision = _scope_amendment(("src/helper.py", ("import",)))
    applied = store.append_scope_amendment(
        job.id,
        decision,
        authorizing_gate="test",
        failure_event_id="event-1",
        expected_prior_sequence=0,
        cumulative_limit=2,
    )

    duplicate_event = store.append_scope_amendment(
        job.id,
        decision,
        authorizing_gate="test",
        failure_event_id="event-1",
        expected_prior_sequence=0,
        cumulative_limit=2,
    )
    duplicate_path = store.append_scope_amendment(
        job.id,
        decision,
        authorizing_gate="test",
        failure_event_id="event-2",
        expected_prior_sequence=1,
        cumulative_limit=2,
    )

    assert applied.status == "applied"
    assert duplicate_event.status == "idempotent"
    assert duplicate_event.reason == "duplicate_event"
    assert duplicate_path.status == "idempotent"
    assert duplicate_path.reason == "already_allowed"
    amendments = store.get(job.id).source_meta["scope_amendments"]
    assert amendments["sequence"] == 1
    assert len(amendments["ledger"]) == 1


def test_append_scope_amendment_rejects_stale_sequence_and_limit_without_widening(store, dep_env):
    job = dep_env(source_meta={"scope": {"allowed_paths": ["src/original.py"]}})
    applied = store.append_scope_amendment(
        job.id,
        _scope_amendment(("src/one.py", ("coverage",))),
        authorizing_gate="test",
        failure_event_id="event-1",
        expected_prior_sequence=0,
        cumulative_limit=1,
    )
    stale = store.append_scope_amendment(
        job.id,
        _scope_amendment(("src/two.py", ("coverage",))),
        authorizing_gate="test",
        failure_event_id="event-2",
        expected_prior_sequence=0,
        cumulative_limit=2,
    )
    over_limit = store.append_scope_amendment(
        job.id,
        _scope_amendment(("src/two.py", ("coverage",))),
        authorizing_gate="test",
        failure_event_id="event-2",
        expected_prior_sequence=1,
        cumulative_limit=1,
    )

    assert applied.status == "applied"
    assert stale.status == "rejected" and stale.reason == "stale_sequence"
    assert over_limit.status == "rejected" and over_limit.reason == "cumulative_limit"
    reloaded = store.get(job.id)
    assert reloaded.source_meta["scope"]["allowed_paths"] == [
        "src/original.py",
        "src/one.py",
    ]
    assert len(reloaded.source_meta["scope_amendments"]["ledger"]) == 1


def test_append_scope_amendment_serializes_concurrent_grants(store, dep_env):
    job = dep_env(source_meta={"scope": {"allowed_paths": ["src/original.py"]}})

    def append(path: str, event_id: str):
        return store.append_scope_amendment(
            job.id,
            _scope_amendment((path, ("coverage",))),
            authorizing_gate="test",
            failure_event_id=event_id,
            expected_prior_sequence=0,
            cumulative_limit=1,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda args: append(*args),
                [("src/one.py", "event-1"), ("src/two.py", "event-2")],
            )
        )

    assert sorted(result.status for result in results) == ["applied", "rejected"]
    assert next(result for result in results if result.status == "rejected").reason == (
        "stale_sequence"
    )
    reloaded = store.get(job.id)
    amendments = reloaded.source_meta["scope_amendments"]
    assert amendments["sequence"] == 1
    assert amendments["cumulative_count"] == 1
    assert len(amendments["ledger"]) == 1
    assert len(reloaded.source_meta["scope"]["allowed_paths"]) == 2


def test_append_scope_amendment_supports_legacy_job_without_scope_metadata(store, dep_env):
    job = dep_env(source_meta={"role": "mcp"})

    result = store.append_scope_amendment(
        job.id,
        _scope_amendment(("src/new.py", ("symbol",))),
        authorizing_gate="design_review",
        failure_event_id="review:1",
        expected_prior_sequence=0,
        cumulative_limit=1,
    )

    assert result.status == "applied"
    reloaded = store.get(job.id)
    assert reloaded.source_meta["role"] == "mcp"
    assert reloaded.source_meta["scope"]["allowed_paths"] == ["src/new.py"]
    assert reloaded.source_meta["scope_amendments"]["sequence"] == 1


def test_merge_planning_metadata_is_additive_for_legacy_job(store, dep_env):
    job = dep_env(idea="legacy", source_meta={"role": "mcp"})

    store.merge_planning_metadata(job.id, {"candidate_count": 2})
    store.merge_planning_metadata(job.id, {"correction_count": 1})

    reloaded = store.get(job.id)
    assert reloaded.source_meta["role"] == "mcp"
    assert reloaded.source_meta["planning"] == {
        "candidate_count": 2,
        "correction_count": 1,
    }


def test_spend_plan_correction_is_durable_and_single_use(store, dep_env):
    job = dep_env(idea="needs correction")

    assert store.spend_plan_correction(job.id) is True
    assert store.spend_plan_correction(job.id) is False
    assert store.get(job.id).plan_reask_attempts == 1


def test_delete_user_returns_false_for_unknown_id(store):
    assert store.delete_user(999999999) is False


def test_get_active_project_job_returns_job_in_any_stage(store, dep_env):
    job = dep_env(idea="in-flight merge")
    job.stage = Stage.MERGE
    job.status = JobStatus.RUNNING
    store.save(job)

    result = store.get_active_project_job(job.project_id)

    assert result is not None
    assert result.id == job.id


def test_get_active_project_job_returns_none_when_no_active_job(store, dep_env):
    job = dep_env(idea="finished job")
    job.stage = Stage.DEPLOY
    job.status = JobStatus.DONE
    store.save(job)

    assert store.get_active_project_job(job.project_id) is None


def test_get_active_project_job_matches_deploying_status(store, dep_env):
    job = dep_env(idea="deploying job")
    job.stage = Stage.DEPLOY
    job.status = JobStatus.DEPLOYING
    store.save(job)

    result = store.get_active_project_job(job.project_id)

    assert result is not None


def test_performance_slowest_jobs_includes_title(store, dep_env):
    job = dep_env(idea="slow job idea")
    job.title = "Slow job title"
    store.save(job)
    store.add_event(
        job.id,
        "build",
        "done",
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:05:00+00:00",
    )
    try:
        result = store.performance_slowest_jobs(job.project_id)
        assert len(result) == 1
        assert result[0]["job_id"] == job.id
        assert result[0]["title"] == "Slow job title"
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_events WHERE job_id = %s", (job.id,))


def test_add_event_with_dataclass_in_detail_stores_json_string(store, dep_env):
    job = dep_env(idea="post-merge validation with resource")
    store.save(job)
    record = ResourceRecord(
        cpu_seconds=1.0,
        peak_rss_bytes=100,
        io_read_bytes=0,
        io_write_bytes=0,
        wall_seconds=2.0,
        sampled_at="2026-09-07T08:56:00+00:00",
    )
    try:
        store.add_event(
            job.id,
            "merge",
            "failed",
            detail={"test_result": {"passed": False}, "resource": record},
        )
        events = store.list_events(job.id)
        assert events[-1]["detail"]["resource"] == asdict(record)
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_events WHERE job_id = %s", (job.id,))


def test_performance_stage_stats_returns_p50_and_p95(store, dep_env):
    job = dep_env(idea="latency job idea")
    store.save(job)
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    durations = [100, 200, 300, 400, 500]
    for duration in durations:
        ended = started + timedelta(seconds=duration)
        store.add_event(
            job.id,
            "build",
            "done",
            started_at=started.isoformat(),
            ended_at=ended.isoformat(),
        )
    try:
        result = store.performance_stage_stats(job.project_id)
        assert len(result) == 1
        stats = result[0]
        assert stats["stage"] == "build"
        assert stats["run_count"] == 5
        assert stats["avg_duration_s"] == 300.0
        assert stats["p50_duration_s"] == 300.0
        assert stats["p95_duration_s"] == 480.0
        assert "p90_duration_s" not in stats
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_events WHERE job_id = %s", (job.id,))


def test_performance_stage_stats_excludes_operational_jobs_by_default(store, dep_env):
    ordinary = dep_env(idea="ordinary job")
    operational = dep_env(idea="auto-deploy job", source_actor="auto-deploy")
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.add_event(
        ordinary.id,
        "build",
        "done",
        started_at=started.isoformat(),
        ended_at=(started + timedelta(seconds=100)).isoformat(),
    )
    store.add_event(
        operational.id,
        "build",
        "done",
        started_at=started.isoformat(),
        ended_at=(started + timedelta(seconds=900)).isoformat(),
    )

    excluded = store.performance_stage_stats(ordinary.project_id)
    assert len(excluded) == 1
    assert excluded[0]["run_count"] == 1
    assert excluded[0]["avg_duration_s"] == 100.0

    included = store.performance_stage_stats(ordinary.project_id, include_operational=True)
    assert len(included) == 1
    assert included[0]["run_count"] == 2
    assert included[0]["avg_duration_s"] == 500.0


def test_performance_stage_stats_excludes_legacy_operational_jobs_by_default(store, dep_env):
    ordinary = dep_env(idea="ordinary job")
    legacy_operational = dep_env(
        idea="legacy auto-deploy job", source=JobSource.SUPERVISOR, title="Auto-deploy: prod"
    )
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.add_event(
        ordinary.id,
        "build",
        "done",
        started_at=started.isoformat(),
        ended_at=(started + timedelta(seconds=100)).isoformat(),
    )
    store.add_event(
        legacy_operational.id,
        "build",
        "done",
        started_at=started.isoformat(),
        ended_at=(started + timedelta(seconds=900)).isoformat(),
    )

    excluded = store.performance_stage_stats(ordinary.project_id)
    assert len(excluded) == 1
    assert excluded[0]["run_count"] == 1
    assert excluded[0]["avg_duration_s"] == 100.0

    included = store.performance_stage_stats(ordinary.project_id, include_operational=True)
    assert len(included) == 1
    assert included[0]["run_count"] == 2
    assert included[0]["avg_duration_s"] == 500.0


def test_performance_slowest_jobs_excludes_operational_jobs_by_default(store, dep_env):
    ordinary = dep_env(idea="ordinary job")
    operational = dep_env(idea="auto-deploy job", source_actor="auto-deploy")
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.add_event(
        ordinary.id,
        "build",
        "done",
        started_at=started.isoformat(),
        ended_at=(started + timedelta(seconds=100)).isoformat(),
    )
    store.add_event(
        operational.id,
        "build",
        "done",
        started_at=started.isoformat(),
        ended_at=(started + timedelta(seconds=900)).isoformat(),
    )

    excluded = store.performance_slowest_jobs(ordinary.project_id)
    assert {j["job_id"] for j in excluded} == {ordinary.id}

    included = store.performance_slowest_jobs(ordinary.project_id, include_operational=True)
    assert {j["job_id"] for j in included} == {ordinary.id, operational.id}


def test_performance_slowest_jobs_excludes_legacy_operational_jobs_by_default(store, dep_env):
    ordinary = dep_env(idea="ordinary job")
    legacy_operational = dep_env(
        idea="legacy auto-deploy job", source=JobSource.SUPERVISOR, title="Auto-deploy: prod"
    )
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.add_event(
        ordinary.id,
        "build",
        "done",
        started_at=started.isoformat(),
        ended_at=(started + timedelta(seconds=100)).isoformat(),
    )
    store.add_event(
        legacy_operational.id,
        "build",
        "done",
        started_at=started.isoformat(),
        ended_at=(started + timedelta(seconds=900)).isoformat(),
    )

    excluded = store.performance_slowest_jobs(ordinary.project_id)
    assert {j["job_id"] for j in excluded} == {ordinary.id}

    included = store.performance_slowest_jobs(ordinary.project_id, include_operational=True)
    assert {j["job_id"] for j in included} == {ordinary.id, legacy_operational.id}


def test_performance_headline_stats_excludes_operational_jobs_by_default(store, dep_env):
    ordinary = dep_env(idea="ordinary job")
    operational = dep_env(idea="auto-deploy job", source_actor="auto-deploy")
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.add_event(
        ordinary.id,
        "build",
        "done",
        tokens=100,
        cost_usd=1.0,
        started_at=started.isoformat(),
        ended_at=(started + timedelta(seconds=100)).isoformat(),
    )
    store.add_event(
        operational.id,
        "build",
        "done",
        tokens=50,
        cost_usd=0.5,
        started_at=started.isoformat(),
        ended_at=(started + timedelta(seconds=900)).isoformat(),
    )

    excluded = store.performance_headline_stats(ordinary.project_id)
    assert excluded["total_jobs"] == 1
    assert excluded["total_tokens"] == 100
    assert excluded["total_cost_usd"] == 1.0
    assert excluded["excludes_operational"] is True
    assert excluded["operational_jobs_excluded"] == 1

    included = store.performance_headline_stats(ordinary.project_id, include_operational=True)
    assert included["total_jobs"] == 2
    assert included["total_tokens"] == 150
    assert included["total_cost_usd"] == 1.5
    assert included["excludes_operational"] is False
    assert included["operational_jobs_excluded"] == 0


def test_performance_headline_stats_excludes_legacy_operational_jobs_by_default(store, dep_env):
    ordinary = dep_env(idea="ordinary job")
    legacy_operational = dep_env(
        idea="legacy auto-deploy job", source=JobSource.SUPERVISOR, title="Auto-deploy: prod"
    )
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.add_event(
        ordinary.id,
        "build",
        "done",
        tokens=100,
        cost_usd=1.0,
        started_at=started.isoformat(),
        ended_at=(started + timedelta(seconds=100)).isoformat(),
    )
    store.add_event(
        legacy_operational.id,
        "build",
        "done",
        tokens=50,
        cost_usd=0.5,
        started_at=started.isoformat(),
        ended_at=(started + timedelta(seconds=900)).isoformat(),
    )

    excluded = store.performance_headline_stats(ordinary.project_id)
    assert excluded["total_jobs"] == 1
    assert excluded["total_tokens"] == 100
    assert excluded["total_cost_usd"] == 1.0
    assert excluded["excludes_operational"] is True
    assert excluded["operational_jobs_excluded"] == 1

    included = store.performance_headline_stats(ordinary.project_id, include_operational=True)
    assert included["total_jobs"] == 2
    assert included["total_tokens"] == 150
    assert included["total_cost_usd"] == 1.5
    assert included["excludes_operational"] is False
    assert included["operational_jobs_excluded"] == 0


def test_performance_headline_stats_zero_job_default_carries_operational_keys(store, dep_env):
    project = dep_env(idea="empty project").project_id

    result = store.performance_headline_stats(project)

    assert result["total_jobs"] == 0
    assert result["excludes_operational"] is True
    assert result["operational_jobs_excluded"] == 0


def test_performance_headline_stats_operational_exclusion_respects_date_filters(store, dep_env):
    ordinary = dep_env(idea="ordinary job")
    operational_day1 = dep_env(idea="op day 1", source_actor="auto-deploy")
    operational_day2 = dep_env(idea="op day 2", source_actor="auto-deploy")
    day1 = datetime(2026, 2, 1, tzinfo=timezone.utc)
    day2 = datetime(2026, 2, 5, tzinfo=timezone.utc)
    store.add_event(
        ordinary.id,
        "build",
        "done",
        tokens=100,
        cost_usd=1.0,
        started_at=day1.isoformat(),
        ended_at=(day1 + timedelta(seconds=60)).isoformat(),
    )
    store.add_event(
        operational_day1.id,
        "build",
        "done",
        tokens=50,
        cost_usd=0.5,
        started_at=day1.isoformat(),
        ended_at=(day1 + timedelta(seconds=60)).isoformat(),
    )
    store.add_event(
        operational_day2.id,
        "build",
        "done",
        tokens=70,
        cost_usd=0.7,
        started_at=day2.isoformat(),
        ended_at=(day2 + timedelta(seconds=60)).isoformat(),
    )

    windowed = store.performance_headline_stats(
        ordinary.project_id,
        from_date=day1.isoformat(),
        to_date=(day1 + timedelta(hours=1)).isoformat(),
    )
    assert windowed["total_jobs"] == 1
    assert windowed["total_tokens"] == 100
    assert windowed["total_cost_usd"] == 1.0
    assert windowed["operational_jobs_excluded"] == 1

    windowed_included = store.performance_headline_stats(
        ordinary.project_id,
        from_date=day1.isoformat(),
        to_date=(day1 + timedelta(hours=1)).isoformat(),
        include_operational=True,
    )
    assert windowed_included["total_jobs"] == 2
    assert windowed_included["total_tokens"] == 150
    assert windowed_included["operational_jobs_excluded"] == 0


def test_list_events_after_returns_only_newer_rows_in_id_order(store, dep_env):
    job = dep_env(idea="watched job")
    store.add_event(job.id, "plan", "done", detail={"n": 1})
    store.add_event(job.id, "build", "running", detail={"n": 2})
    store.add_event(job.id, "build", "done", detail={"n": 3})
    try:
        all_events = store.list_events(job.id)
        assert len(all_events) == 3
        cutoff = all_events[0]["id"]

        result = store.list_events_after(job.id, cutoff)

        assert [e["id"] for e in result] == [e["id"] for e in all_events[1:]]
        assert [e["detail"] for e in result] == [{"n": 2}, {"n": 3}]
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_events WHERE job_id = %s", (job.id,))


def test_list_events_enriches_mixed_roster_history_and_preserves_legacy_rows(store, dep_env):
    job = dep_env(idea="mixed roster history")
    claude = store.create_agent(job.project_id, "Planner", "claude", "claude-sonnet")
    codex = store.create_agent(job.project_id, "Builder", "codex", "gpt-5")
    deleted = store.create_agent(job.project_id, "Retired", "claude", "claude-opus")
    store.add_event(job.id, "plan", "done", detail={"agent": "planner"}, agent_id=claude.id)
    store.add_event(job.id, "lint", "done", detail={"deterministic": True})
    store.add_event(job.id, "build", "done", detail={"agent": "builder"}, agent_id=codex.id)
    store.add_event(job.id, "review", "done", detail={"deleted": True}, agent_id=deleted.id)
    store.delete_agent(deleted.id)

    try:
        events = store.list_events(job.id)
        incremental = store.list_events_after(job.id, events[0]["id"])

        assert [event["id"] for event in events] == sorted({event["id"] for event in events})
        assert [event["detail"] for event in events] == [
            {"agent": "planner"},
            {"deterministic": True},
            {"agent": "builder"},
            {"deleted": True},
        ]
        assert [
            (
                event["agent_id"],
                event["agent_name"],
                event["agent_provider"],
                event["agent_model"],
            )
            for event in events
        ] == [
            (claude.id, "Planner", "claude", "claude-sonnet"),
            (None, None, None, None),
            (codex.id, "Builder", "codex", "gpt-5"),
            (deleted.id, None, None, None),
        ]
        assert [event["id"] for event in incremental] == [event["id"] for event in events[1:]]
        assert incremental == events[1:]
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_events WHERE job_id = %s", (job.id,))


def test_list_events_after_applies_cursor_and_exact_200_row_cap(store, dep_env):
    job = dep_env(idea="bounded event history")
    store.add_event(job.id, "plan", "done", detail={"cursor": True})
    cursor = store.list_events(job.id)[0]["id"]
    for index in range(205):
        store.add_event(job.id, "build", "running", detail={"index": index})

    try:
        events = store.list_events_after(job.id, cursor)

        assert len(events) == 200
        assert [event["id"] for event in events] == sorted({event["id"] for event in events})
        assert all(event["id"] > cursor for event in events)
        assert events[0]["detail"] == {"index": 0}
        assert events[-1]["detail"] == {"index": 199}
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_events WHERE job_id = %s", (job.id,))


def test_pipeline_package_exports_agent_spec_and_job_store():
    from hyqs.pipeline import AgentSpec as ExportedAgentSpec
    from hyqs.pipeline import JobStore as ExportedJobStore
    from hyqs.pipeline.models import AgentSpec

    assert ExportedAgentSpec is AgentSpec
    assert ExportedJobStore is JobStore


def test_list_events_round_trips_authorized_gate_failure_identity_and_nested_evidence(
    store, dep_env
):
    job = dep_env(idea="durable gate evidence")
    detail = {
        "authorized_gate_failure": {
            "gate": "design_review",
            "check_id": "design-review.findings",
            "event_id": "design-review.findings",
            "failing_paths": ["src/LogPanel.jsx", "src/LogPanel.test.jsx"],
            "categories": ["symbol_paths"],
        },
        "evidence": {"symbol_paths": ["src/LogPanel.test.jsx"]},
        "relationships": [
            {
                "source": "src/LogPanel.jsx",
                "target": "src/LogPanel.test.jsx",
                "kind": "companion_test",
            }
        ],
    }
    store.add_event(job.id, "test", "done", detail={"marker": "before"})
    store.add_event(job.id, "design_review", "failed", detail=detail, attempt=2)

    events = store.list_events(job.id)

    assert [event["id"] for event in events] == sorted(event["id"] for event in events)
    assert events[0]["id"] != events[1]["id"]
    recovered = events[1]
    assert recovered["stage"] == "design_review"
    assert recovered["status"] == "failed"
    assert recovered["attempt"] == 2
    assert recovered["detail"] == detail


def test_list_events_after_zero_returns_all_events(store, dep_env):
    job = dep_env(idea="watched job 2")
    store.add_event(job.id, "plan", "done")
    try:
        result = store.list_events_after(job.id, 0)
        assert len(result) == 1
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM job_events WHERE job_id = %s", (job.id,))


def test_usage_by_project_labels_pid_none_bucket_and_has_no_epics(store):
    from hyqs.pipeline.models import _now

    with store._pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO usage(job_id, source, input_tokens, output_tokens, "
            "cache_creation_tokens, cache_read_tokens, cost_usd, created_at) "
            "VALUES (NULL, 'dev_session', 100, 50, 0, 0, 0.01, %s) RETURNING id",
            (_now(),),
        ).fetchone()
        usage_id = row["id"]
    try:
        with store._pool.connection() as conn:
            projects = store._usage_by_project(conn)
        pid_none = next((p for p in projects if p["project_id"] is None), None)
        assert pid_none is not None
        assert pid_none["name"] == "Interactive dev sessions (Claude Code)"
        assert pid_none["epics"] == []
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM usage WHERE id = %s", (usage_id,))


def test_usage_summary_project_id_scopes_to_that_project(store):
    from hyqs.pipeline.models import _now

    project_a = store.create_project("Usage Project A", f"/tmp/test-store-{uuid.uuid4()}")
    project_b = store.create_project("Usage Project B", f"/tmp/test-store-{uuid.uuid4()}")
    dev_session_id = None
    try:
        job_a = store.create(idea="job a", repo_path=project_a.repo_path, chat_id=1)
        job_b = store.create(idea="job b", repo_path=project_b.repo_path, chat_id=1)
        store.record_usage(
            "build", Usage(input_tokens=100, output_tokens=50, cost_usd=1.0), job_id=job_a.id
        )
        store.record_usage(
            "build", Usage(input_tokens=10, output_tokens=5, cost_usd=0.1), job_id=job_b.id
        )
        with store._pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO usage(job_id, source, input_tokens, output_tokens, "
                "cache_creation_tokens, cache_read_tokens, cost_usd, created_at) "
                "VALUES (NULL, 'dev_session', 999, 999, 0, 0, 9.0, %s) RETURNING id",
                (_now(),),
            ).fetchone()
            dev_session_id = row["id"]

        result = store.usage_summary(project_id=project_a.id)

        assert result["total_tokens"] == 150
        assert result["total_cost_usd"] == 1.0
        assert [s["source"] for s in result["by_source"]] == ["build"]
        assert result["by_source"][0]["tokens"] == 150
        project_ids = {p["project_id"] for p in result["projects"]}
        assert project_ids == {project_a.id}
    finally:
        if dev_session_id is not None:
            with store._pool.connection() as conn:
                conn.execute("DELETE FROM usage WHERE id = %s", (dev_session_id,))
        store.delete_project(project_a.id)
        store.delete_project(project_b.id)


def test_usage_summary_unscoped_still_aggregates_globally(store):
    from hyqs.pipeline.models import _now

    with store._pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO usage(job_id, source, input_tokens, output_tokens, "
            "cache_creation_tokens, cache_read_tokens, cost_usd, created_at) "
            "VALUES (NULL, 'dev_session', 20, 10, 0, 0, 0.5, %s) RETURNING id",
            (_now(),),
        ).fetchone()
        usage_id = row["id"]
    try:
        before = store.usage_summary()
        assert usage_id is not None
        assert before["total_tokens"] >= 30
        pid_none = next((p for p in before["projects"] if p["project_id"] is None), None)
        assert pid_none is not None
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM usage WHERE id = %s", (usage_id,))


def test_usage_by_project_with_project_id_returns_only_matching_project(store):
    project_a = store.create_project("Usage Project C", f"/tmp/test-store-{uuid.uuid4()}")
    project_b = store.create_project("Usage Project D", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        job_a = store.create(idea="job a", repo_path=project_a.repo_path, chat_id=1)
        job_b = store.create(idea="job b", repo_path=project_b.repo_path, chat_id=1)
        store.record_usage(
            "build", Usage(input_tokens=100, output_tokens=0, cost_usd=1.0), job_id=job_a.id
        )
        store.record_usage(
            "build", Usage(input_tokens=10, output_tokens=0, cost_usd=0.1), job_id=job_b.id
        )

        with store._pool.connection() as conn:
            projects = store._usage_by_project(conn, project_id=project_a.id)

        assert len(projects) == 1
        assert projects[0]["project_id"] == project_a.id
        assert projects[0]["epics"][0]["jobs"][0]["job_id"] == job_a.id
    finally:
        store.delete_project(project_a.id)
        store.delete_project(project_b.id)


def test_operational_exclusion_clause_returns_documented_fragment(store):
    assert store._operational_exclusion_clause("j") == (
        "(j.source_actor = 'auto-deploy' OR "
        "(j.source = 'supervisor' AND j.title LIKE 'Auto-deploy:%'))"
    )


@pytest.fixture
def isolated_store():
    """A JobStore backed by a brand-new, empty Postgres database.

    ``site_stats()`` aggregates globally with no project/scope filter, so
    testing it against the shared session database (like every other
    ``store`` test) would mix in whatever other tests have already inserted,
    making exact totals unverifiable. This provisions a disposable database
    (the same technique conftest.py's ``_provision_session_database`` uses to
    isolate whole test sessions) so a hand-seeded fixture produces exact,
    reproducible aggregates.
    """
    import conftest

    base = conftest._base_dsn()
    info = {k: v for k, v in psycopg.conninfo.conninfo_to_dict(base).items() if v is not None}
    db_name = f"{(info.get('dbname') or 'hyqs')}_sitestats_{uuid.uuid4().hex[:8]}"
    try:
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{db_name}"')
    except psycopg.errors.InsufficientPrivilege:
        pytest.skip("test role cannot CREATE DATABASE in this environment")
        return

    isolated_info = dict(info)
    isolated_info["dbname"] = db_name
    set_db_actor("test:pytest")
    s = JobStore(psycopg.conninfo.make_conninfo(**isolated_info))
    try:
        yield s
    finally:
        s.close()
        with psycopg.connect(base, autocommit=True) as conn:
            try:
                conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
            except psycopg.Error:
                conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')


def test_site_stats_empty_database_returns_zero_and_none_defaults(isolated_store):
    stats = isolated_store.site_stats()

    assert isinstance(stats, SiteStats)
    assert stats.generated_at
    datetime.fromisoformat(stats.generated_at)
    assert stats.shipped_total == 0
    assert stats.shipped_last_7d == 0
    assert stats.shipped_last_24h == 0
    assert stats.minutes_since_last_ship is None
    assert stats.avg_minutes_between_ships_last_7d is None
    assert stats.first_job_at is None
    assert stats.active_days is None
    assert stats.humans == 0
    assert stats.projects == 0
    assert stats.gate_stops_total == 0
    assert stats.gate_stops_by_gate == {
        "lint": 0,
        "test": 0,
        "review": 0,
        "security": 0,
        "design": 0,
    }
    assert stats.self_healed_shipped == 0
    assert stats.fix_rounds == 0
    assert stats.conflicts_resolved == 0
    assert stats.median_lead_minutes is None
    assert stats.p25_lead_minutes is None
    assert stats.off_hours_share is None
    assert stats.cost_per_shipped_usd is None
    assert stats.median_cost_shipped_usd is None
    assert stats.running_now == 0
    assert stats.workers_online == 0


def _off_hours(dt: datetime) -> bool:
    dt_utc = dt.astimezone(timezone.utc)
    return dt_utc.isoweekday() in (6, 7) or not (8 <= dt_utc.hour <= 19)


def test_site_stats_hand_seeded_dataset_computes_expected_aggregates_and_excludes_noise(
    isolated_store,
):
    s = isolated_store
    repo_path = f"/tmp/site-stats-{uuid.uuid4()}"
    s.create_project("Site Stats Project", repo_path)
    noise_project = s.create_project("Site Stats Noise Project", f"{repo_path}-noise")
    with s._pool.connection() as conn:
        conn.execute("UPDATE projects SET status = 'archived' WHERE id = %s", (noise_project.id,))

    s.create_user("sitestats-a@example.com", "pw-a")
    s.create_user("sitestats-b@example.com", "pw-b")

    call_before = datetime.now(timezone.utc)
    in_window_updated = call_before - timedelta(hours=1)
    in_window_created = call_before - timedelta(hours=2)
    out_of_window_created = call_before - timedelta(days=10)

    job_in_window = s.create(idea="in window shipped job", repo_path=repo_path, chat_id=1)
    job_out_of_window = s.create(idea="out of window shipped job", repo_path=repo_path, chat_id=1)
    job_running = s.create(idea="currently running job", repo_path=repo_path, chat_id=1)
    job_running_operational = s.create(
        idea="running auto-deploy job",
        repo_path=repo_path,
        chat_id=1,
        source_actor="auto-deploy",
    )
    job_archived_done = s.create(idea="archived shipped job", repo_path=repo_path, chat_id=1)
    job_auto_deploy_done = s.create(
        idea="auto-deploy shipped job",
        repo_path=repo_path,
        chat_id=1,
        source_actor="auto-deploy",
    )
    job_legacy_operational_done = s.create(
        idea="legacy auto-deploy shipped job",
        repo_path=repo_path,
        chat_id=1,
        source=JobSource.SUPERVISOR,
        title="Auto-deploy: prod",
    )

    with s._pool.connection() as conn:
        conn.execute(
            "UPDATE jobs SET status = %s, attempts = 0, created_at = %s, updated_at = %s "
            "WHERE id = %s",
            (
                JobStatus.DONE.value,
                in_window_created.isoformat(),
                in_window_updated.isoformat(),
                job_in_window.id,
            ),
        )
        conn.execute(
            "UPDATE jobs SET status = %s, attempts = 4, created_at = %s, updated_at = %s "
            "WHERE id = %s",
            (
                JobStatus.DONE.value,
                out_of_window_created.isoformat(),
                out_of_window_created.isoformat(),
                job_out_of_window.id,
            ),
        )
        conn.execute(
            "UPDATE jobs SET status = %s WHERE id = %s",
            (JobStatus.RUNNING.value, job_running.id),
        )
        conn.execute(
            "UPDATE jobs SET status = %s WHERE id = %s",
            (JobStatus.RUNNING.value, job_running_operational.id),
        )
        for excluded_job in (job_archived_done, job_auto_deploy_done, job_legacy_operational_done):
            conn.execute(
                "UPDATE jobs SET status = %s, attempts = 9, created_at = %s, updated_at = %s "
                "WHERE id = %s",
                (
                    JobStatus.DONE.value,
                    (call_before - timedelta(days=20)).isoformat(),
                    (call_before - timedelta(seconds=30)).isoformat(),
                    excluded_job.id,
                ),
            )
    s.set_archived(job_archived_done.id, True)

    s.record_usage("build", Usage(cost_usd=3.0), job_id=job_in_window.id)
    s.record_usage("build", Usage(cost_usd=5.0), job_id=job_out_of_window.id)
    for excluded_job in (job_archived_done, job_auto_deploy_done, job_legacy_operational_done):
        s.record_usage("build", Usage(cost_usd=999.0), job_id=excluded_job.id)

    # Gate-stop buckets, all on the one eligible job that should count.
    s.add_event(job_in_window.id, "lint", "failed")
    s.add_event(job_in_window.id, "lockfile-drift", "failed")
    s.add_event(job_in_window.id, "test", "failed")
    s.add_event(job_in_window.id, "import-smoke", "failed")
    s.add_event(job_in_window.id, "frontend-build", "failed")
    s.add_event(job_in_window.id, "invariants", "failed")
    s.add_event(job_in_window.id, "symbol-collision", "failed")
    s.add_event(job_in_window.id, "alembic-heads", "failed")
    s.add_event(job_in_window.id, "review", "failed")
    s.add_event(job_in_window.id, "security", "failed")
    s.add_event(job_in_window.id, "design_review", "failed")
    s.add_event(job_in_window.id, "lint", "done")  # non-failed: must not count
    s.add_event(job_in_window.id, "deploy", "failed")  # unmapped stage: must not count
    s.add_event(job_in_window.id, "fix", "done")
    s.add_event(job_in_window.id, "fix", "failed")  # wrong status: must not count
    s.add_event(job_in_window.id, "merge", "conflict")
    s.add_event(job_in_window.id, "merge", "done")  # wrong status: must not count

    # Same event shapes on excluded jobs must not leak into any bucket/count.
    for excluded_job in (job_archived_done, job_auto_deploy_done):
        s.add_event(excluded_job.id, "lint", "failed")
        s.add_event(excluded_job.id, "fix", "done")
        s.add_event(excluded_job.id, "merge", "conflict")

    fresh_worker = f"w-{uuid.uuid4()}"
    stale_worker = f"w-{uuid.uuid4()}"
    asyncio.run(
        s.worker_heartbeat(
            fresh_worker, "host-a", 111, status="idle", role="executor", now=time.time()
        )
    )
    asyncio.run(
        s.worker_heartbeat(
            stale_worker, "host-b", 222, status="idle", role="executor", now=time.time() - 400
        )
    )

    stats = s.site_stats()

    assert stats.generated_at
    generated_at = datetime.fromisoformat(stats.generated_at)

    assert stats.shipped_total == 2
    assert stats.shipped_last_7d == 1
    assert stats.shipped_last_24h == 1
    assert stats.self_healed_shipped == 1

    expected_minutes_since_last_ship = round(
        (generated_at - in_window_updated).total_seconds() / 60, 2
    )
    assert stats.minutes_since_last_ship == pytest.approx(expected_minutes_since_last_ship, abs=0.1)

    assert stats.avg_minutes_between_ships_last_7d == round(10080.0 / 1, 2)

    assert stats.first_job_at == out_of_window_created.date().isoformat()
    assert stats.active_days == (generated_at.date() - out_of_window_created.date()).days

    assert stats.humans == 2
    assert stats.projects == 1

    assert stats.gate_stops_by_gate == {
        "lint": 2,
        "test": 6,
        "review": 1,
        "security": 1,
        "design": 1,
    }
    assert stats.gate_stops_total == 11

    assert stats.fix_rounds == 1
    assert stats.conflicts_resolved == 1

    assert stats.median_lead_minutes == 30.0
    assert stats.p25_lead_minutes == 15.0

    expected_off_hours = sum(_off_hours(dt) for dt in (in_window_updated, out_of_window_created))
    assert stats.off_hours_share == round(expected_off_hours / 2, 4)

    assert stats.cost_per_shipped_usd == 4.0
    assert stats.median_cost_shipped_usd == 4.0

    assert stats.running_now == 1
    assert stats.workers_online == 1


def test_usage_summary_excludes_operational_jobs_by_default(store):
    project = store.create_project("Usage Op Exclude Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        ordinary = store.create(
            idea="ordinary job", repo_path=project.repo_path, chat_id=1, source=JobSource.CLI
        )
        auto_deploy = store.create(
            idea="auto-deploy job",
            repo_path=project.repo_path,
            chat_id=1,
            source_actor="auto-deploy",
        )
        legacy_auto_deploy = store.create(
            idea="legacy auto-deploy job",
            repo_path=project.repo_path,
            chat_id=1,
            source=JobSource.SUPERVISOR,
            title="Auto-deploy: prod",
        )
        store.record_usage(
            "build", Usage(input_tokens=100, output_tokens=50, cost_usd=1.0), job_id=ordinary.id
        )
        store.record_usage(
            "build",
            Usage(input_tokens=10, output_tokens=5, cost_usd=0.2),
            job_id=auto_deploy.id,
        )
        store.record_usage(
            "build",
            Usage(input_tokens=20, output_tokens=5, cost_usd=0.3),
            job_id=legacy_auto_deploy.id,
        )

        excluded = store.usage_summary(project_id=project.id)
        assert excluded["total_tokens"] == 150
        assert excluded["total_cost_usd"] == 1.0
        assert [s["source"] for s in excluded["by_source"]] == ["build"]
        assert excluded["by_source"][0]["tokens"] == 150
        assert excluded["excludes_operational"] is True
        assert excluded["operational_jobs_excluded"] == 2
        proj_rollup = next(p for p in excluded["projects"] if p["project_id"] == project.id)
        assert proj_rollup["tokens"] == 150
        job_ids_seen = {j["job_id"] for epic in proj_rollup["epics"] for j in epic["jobs"]}
        assert job_ids_seen == {ordinary.id}

        included = store.usage_summary(project_id=project.id, include_operational=True)
        assert included["total_tokens"] == 150 + 15 + 25
        assert included["total_cost_usd"] == 1.5
        assert included["excludes_operational"] is False
        assert included["operational_jobs_excluded"] == 0
        proj_rollup_all = next(p for p in included["projects"] if p["project_id"] == project.id)
        assert proj_rollup_all["tokens"] == 150 + 15 + 25
    finally:
        store.delete_project(project.id)


def test_usage_summary_never_excludes_dev_session_usage(store):
    from hyqs.pipeline.models import _now

    project = store.create_project(
        "Usage Op Dev Session Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    dev_session_id = None
    try:
        auto_deploy = store.create(
            idea="auto-deploy job",
            repo_path=project.repo_path,
            chat_id=1,
            source_actor="auto-deploy",
        )
        store.record_usage(
            "build",
            Usage(input_tokens=10, output_tokens=5, cost_usd=0.2),
            job_id=auto_deploy.id,
        )
        with store._pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO usage(job_id, source, input_tokens, output_tokens, "
                "cache_creation_tokens, cache_read_tokens, cost_usd, created_at) "
                "VALUES (NULL, 'dev_session', 20, 10, 0, 0, 0.5, %s) RETURNING id",
                (_now(),),
            ).fetchone()
            dev_session_id = row["id"]

        default_result = store.usage_summary()
        assert default_result["total_tokens"] >= 30
        included_result = store.usage_summary(include_operational=True)
        assert included_result["total_tokens"] >= 30 + 15
    finally:
        if dev_session_id is not None:
            with store._pool.connection() as conn:
                conn.execute("DELETE FROM usage WHERE id = %s", (dev_session_id,))
        store.delete_project(project.id)


def test_usage_by_project_include_operational_toggles_rollup(store):
    project_a = store.create_project("Usage Op By Project A", f"/tmp/test-store-{uuid.uuid4()}")
    project_b = store.create_project("Usage Op By Project B", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        job_a = store.create(idea="job a", repo_path=project_a.repo_path, chat_id=1)
        auto_deploy_a = store.create(
            idea="auto-deploy a",
            repo_path=project_a.repo_path,
            chat_id=1,
            source_actor="auto-deploy",
        )
        job_b = store.create(idea="job b", repo_path=project_b.repo_path, chat_id=1)
        store.record_usage(
            "build", Usage(input_tokens=100, output_tokens=0, cost_usd=1.0), job_id=job_a.id
        )
        store.record_usage(
            "build",
            Usage(input_tokens=50, output_tokens=0, cost_usd=0.5),
            job_id=auto_deploy_a.id,
        )
        store.record_usage(
            "build", Usage(input_tokens=10, output_tokens=0, cost_usd=0.1), job_id=job_b.id
        )

        with store._pool.connection() as conn:
            excluded_projects = store._usage_by_project(conn, project_id=project_a.id)
            included_projects = store._usage_by_project(
                conn, project_id=project_a.id, include_operational=True
            )

        assert len(excluded_projects) == 1
        job_ids_excluded = {
            j["job_id"] for epic in excluded_projects[0]["epics"] for j in epic["jobs"]
        }
        assert job_ids_excluded == {job_a.id}
        assert excluded_projects[0]["tokens"] == 100

        assert len(included_projects) == 1
        job_ids_included = {
            j["job_id"] for epic in included_projects[0]["epics"] for j in epic["jobs"]
        }
        assert job_ids_included == {job_a.id, auto_deploy_a.id}
        assert included_projects[0]["tokens"] == 150
    finally:
        store.delete_project(project_a.id)
        store.delete_project(project_b.id)


def test_agent_stats_excludes_operational_events_by_default(store):
    from hyqs.pipeline.models import _now

    project = store.create_project(
        "Agent Stats Op Exclude Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        agent = store.create_agent(project.id, "Agent Op Exclude")
        ordinary = store.create(
            idea="ordinary job", repo_path=project.repo_path, chat_id=1, source=JobSource.CLI
        )
        auto_deploy = store.create(
            idea="auto-deploy job",
            repo_path=project.repo_path,
            chat_id=1,
            source_actor="auto-deploy",
        )
        started = _now()
        ended = _now()
        store.add_event(
            ordinary.id,
            "build",
            "done",
            cost_usd=1.0,
            started_at=started,
            ended_at=ended,
            agent_id=agent.id,
        )
        store.add_event(
            auto_deploy.id,
            "build",
            "done",
            cost_usd=5.0,
            started_at=started,
            ended_at=ended,
            agent_id=agent.id,
        )

        excluded = {r["agent_id"]: r for r in store.agent_stats(project.id)}
        assert excluded[agent.id]["total_cost_usd"] == 1.0
        assert excluded[agent.id]["avg_duration_s"] is not None
    finally:
        store.delete_project(project.id)


def test_agent_stats_agent_with_only_operational_events_still_appears_with_zero_defaults(store):
    from hyqs.pipeline.models import _now

    project = store.create_project("Agent Stats Op Only Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        agent = store.create_agent(project.id, "Agent Op Only")
        auto_deploy = store.create(
            idea="auto-deploy job",
            repo_path=project.repo_path,
            chat_id=1,
            source_actor="auto-deploy",
        )
        store.add_event(
            auto_deploy.id,
            "build",
            "done",
            cost_usd=5.0,
            started_at=_now(),
            ended_at=_now(),
            agent_id=agent.id,
        )

        stats = {r["agent_id"]: r for r in store.agent_stats(project.id)}
        assert agent.id in stats
        assert stats[agent.id]["total_cost_usd"] == 0
        assert stats[agent.id]["avg_duration_s"] is None
        assert stats[agent.id]["fix_rate"] == 0.0
    finally:
        store.delete_project(project.id)


def test_agent_stats_include_operational_true_matches_unfiltered(store):
    from hyqs.pipeline.models import _now

    project = store.create_project(
        "Agent Stats Op Include Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        agent = store.create_agent(project.id, "Agent Op Include")
        ordinary = store.create(
            idea="ordinary job", repo_path=project.repo_path, chat_id=1, source=JobSource.CLI
        )
        auto_deploy = store.create(
            idea="auto-deploy job",
            repo_path=project.repo_path,
            chat_id=1,
            source_actor="auto-deploy",
        )
        started = _now()
        ended = _now()
        store.add_event(
            ordinary.id,
            "build",
            "done",
            cost_usd=1.0,
            started_at=started,
            ended_at=ended,
            agent_id=agent.id,
        )
        store.add_event(
            auto_deploy.id,
            "build",
            "done",
            cost_usd=5.0,
            started_at=started,
            ended_at=ended,
            agent_id=agent.id,
        )

        included = {
            r["agent_id"]: r for r in store.agent_stats(project.id, include_operational=True)
        }
        assert included[agent.id]["total_cost_usd"] == 6.0
    finally:
        store.delete_project(project.id)


def test_agent_stats_excludes_legacy_operational_events_by_default(store):
    from hyqs.pipeline.models import _now

    project = store.create_project(
        "Agent Stats Legacy Op Exclude Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        agent = store.create_agent(project.id, "Agent Legacy Op Exclude")
        ordinary = store.create(
            idea="ordinary job", repo_path=project.repo_path, chat_id=1, source=JobSource.CLI
        )
        legacy_operational = store.create(
            idea="legacy auto-deploy job",
            repo_path=project.repo_path,
            chat_id=1,
            source=JobSource.SUPERVISOR,
            title="Auto-deploy: prod",
        )
        started = _now()
        ended = _now()
        store.add_event(
            ordinary.id,
            "build",
            "done",
            cost_usd=1.0,
            started_at=started,
            ended_at=ended,
            agent_id=agent.id,
        )
        store.add_event(
            legacy_operational.id,
            "build",
            "done",
            cost_usd=5.0,
            started_at=started,
            ended_at=ended,
            agent_id=agent.id,
        )

        excluded = {r["agent_id"]: r for r in store.agent_stats(project.id)}
        assert excluded[agent.id]["total_cost_usd"] == 1.0

        included = {
            r["agent_id"]: r for r in store.agent_stats(project.id, include_operational=True)
        }
        assert included[agent.id]["total_cost_usd"] == 6.0
    finally:
        store.delete_project(project.id)


def test_agent_stats_fix_rate_excludes_operational_fix_events_by_default(store):
    from hyqs.pipeline.models import _now

    project = store.create_project(
        "Agent Stats Fix Rate Op Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        agent = store.create_agent(project.id, "Agent Fix Rate")
        ordinary = store.create(
            idea="ordinary build job",
            repo_path=project.repo_path,
            chat_id=1,
            source=JobSource.CLI,
        )
        operational = store.create(
            idea="auto-deploy fix job",
            repo_path=project.repo_path,
            chat_id=1,
            source_actor="auto-deploy",
        )
        started = _now()
        ended = _now()
        store.add_event(
            ordinary.id, "build", "done", started_at=started, ended_at=ended, agent_id=agent.id
        )
        store.add_event(
            operational.id, "fix", "done", started_at=started, ended_at=ended, agent_id=agent.id
        )

        excluded = {r["agent_id"]: r for r in store.agent_stats(project.id)}
        assert excluded[agent.id]["fix_rate"] == 0.0

        included = {
            r["agent_id"]: r for r in store.agent_stats(project.id, include_operational=True)
        }
        assert included[agent.id]["fix_rate"] == 0.5
    finally:
        store.delete_project(project.id)


def test_operational_exclusion_preserves_ordinary_nullable_jobs_and_filters(store):
    # jobs.source_actor and jobs.title are declared
    # "TEXT NOT NULL DEFAULT ''" (hyqs/pipeline/store.py:504,528), so the
    # schema forbids storing SQL NULL for either column — an empty string is
    # the only possible null-equivalent sentinel a job can carry. Confirm
    # that live against the real Postgres schema (not assumed), since every
    # operational-exclusion test in this suite relies on '' meaning "unset".
    with store._pool.connection() as conn:
        rows = conn.execute(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'jobs' AND column_name IN ('source_actor', 'title')"
        ).fetchall()
    nullability = {r["column_name"]: r["is_nullable"] for r in rows}
    assert nullability == {"source_actor": "NO", "title": "NO"}

    project = store.create_project(
        "Op Exclusion Nullable Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        agent = store.create_agent(project.id, "Op Exclusion Nullable Agent")
        # No source_actor/source/title override: source_actor falls back to
        # the '' schema default above (title is auto-derived from idea when
        # omitted, but source_actor and source are what the exclusion clause
        # actually keys on) — this is what an ordinary job's fields look like.
        ordinary = store.create(
            idea="ordinary nullable-field job", repo_path=project.repo_path, chat_id=1
        )
        assert ordinary.source_actor == ""
        assert ordinary.source == JobSource.UNKNOWN
        auto_deploy = store.create(
            idea="auto-deploy job",
            repo_path=project.repo_path,
            chat_id=1,
            source_actor="auto-deploy",
        )
        started = "2026-01-01T00:00:00+00:00"
        ended = "2026-01-01T00:01:00+00:00"
        store.record_usage(
            "build", Usage(input_tokens=10, output_tokens=5, cost_usd=0.4), job_id=ordinary.id
        )
        store.record_usage(
            "build", Usage(input_tokens=20, output_tokens=5, cost_usd=0.6), job_id=auto_deploy.id
        )
        store.add_event(
            ordinary.id,
            "build",
            "done",
            cost_usd=0.4,
            started_at=started,
            ended_at=ended,
            agent_id=agent.id,
        )
        store.add_event(
            auto_deploy.id,
            "build",
            "done",
            cost_usd=0.6,
            started_at=started,
            ended_at=ended,
            agent_id=agent.id,
        )

        excluded_usage = store.usage_summary(project_id=project.id)
        excluded_job_ids = {
            j["job_id"] for p in excluded_usage["projects"] for e in p["epics"] for j in e["jobs"]
        }
        assert excluded_job_ids == {ordinary.id}

        included_usage = store.usage_summary(project_id=project.id, include_operational=True)
        included_job_ids = {
            j["job_id"] for p in included_usage["projects"] for e in p["epics"] for j in e["jobs"]
        }
        assert included_job_ids == {ordinary.id, auto_deploy.id}

        excluded_agent_stats = {r["agent_id"]: r for r in store.agent_stats(project.id)}
        assert excluded_agent_stats[agent.id]["total_cost_usd"] == 0.4

        included_agent_stats = {
            r["agent_id"]: r for r in store.agent_stats(project.id, include_operational=True)
        }
        assert included_agent_stats[agent.id]["total_cost_usd"] == 1.0

        excluded_headline = store.performance_headline_stats(project.id)
        assert excluded_headline["total_jobs"] == 1

        included_headline = store.performance_headline_stats(project.id, include_operational=True)
        assert included_headline["total_jobs"] == 2
    finally:
        store.delete_project(project.id)


def test_operational_exclusion_preserves_ordinary_supervisor_jobs_with_blank_or_nonmatching_title(
    store,
):
    from hyqs.pipeline.models import _now

    project = store.create_project(
        "Op Exclusion Supervisor Title Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        agent = store.create_agent(project.id, "Op Exclusion Supervisor Title Agent")
        blank_title = store.create(
            idea="supervisor job that will get a blank title",
            repo_path=project.repo_path,
            chat_id=1,
            source=JobSource.SUPERVISOR,
        )
        with store._pool.connection() as conn:
            conn.execute("UPDATE jobs SET title = '' WHERE id = %s", (blank_title.id,))
        nonmatching_title = store.create(
            idea="supervisor job with an unrelated title",
            repo_path=project.repo_path,
            chat_id=1,
            source=JobSource.SUPERVISOR,
            title="Regular supervisor job",
        )
        operational = store.create(
            idea="legacy auto-deploy job",
            repo_path=project.repo_path,
            chat_id=1,
            source=JobSource.SUPERVISOR,
            title="Auto-deploy: prod",
        )
        started = _now()
        ended = _now()
        for job, cost in ((blank_title, 1.0), (nonmatching_title, 2.0), (operational, 99.0)):
            store.record_usage(
                "build", Usage(input_tokens=10, output_tokens=0, cost_usd=cost), job_id=job.id
            )
            store.add_event(
                job.id,
                "build",
                "done",
                tokens=10,
                cost_usd=cost,
                started_at=started,
                ended_at=ended,
                agent_id=agent.id,
            )

        excluded_usage = store.usage_summary(project_id=project.id)
        excluded_job_ids = {
            j["job_id"] for p in excluded_usage["projects"] for e in p["epics"] for j in e["jobs"]
        }
        assert excluded_job_ids == {blank_title.id, nonmatching_title.id}

        included_usage = store.usage_summary(project_id=project.id, include_operational=True)
        included_job_ids = {
            j["job_id"] for p in included_usage["projects"] for e in p["epics"] for j in e["jobs"]
        }
        assert included_job_ids == {blank_title.id, nonmatching_title.id, operational.id}

        excluded_agent_stats = {r["agent_id"]: r for r in store.agent_stats(project.id)}
        assert excluded_agent_stats[agent.id]["total_cost_usd"] == 3.0

        included_agent_stats = {
            r["agent_id"]: r for r in store.agent_stats(project.id, include_operational=True)
        }
        assert included_agent_stats[agent.id]["total_cost_usd"] == 102.0

        excluded_headline = store.performance_headline_stats(project.id)
        assert excluded_headline["total_jobs"] == 2

        included_headline = store.performance_headline_stats(project.id, include_operational=True)
        assert included_headline["total_jobs"] == 3

        reliability = store.deployment_reliability_breakdown(project_id=project.id)
        assert len(reliability) == 1
        assert reliability[0]["total"] == 1
    finally:
        store.delete_project(project.id)


def test_performance_headline_stats_and_usage_summary_project_with_only_operational_job(store):
    project = store.create_project(
        "Only Operational Job Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        operational = store.create(
            idea="only operational job",
            repo_path=project.repo_path,
            chat_id=1,
            source_actor="auto-deploy",
        )
        started = "2026-01-01T00:00:00+00:00"
        ended = "2026-01-01T00:01:00+00:00"
        store.add_event(
            operational.id,
            "build",
            "done",
            tokens=100,
            cost_usd=1.0,
            started_at=started,
            ended_at=ended,
        )
        store.record_usage(
            "build", Usage(input_tokens=100, output_tokens=0, cost_usd=1.0), job_id=operational.id
        )

        default_headline = store.performance_headline_stats(project.id)
        assert default_headline["total_jobs"] == 0
        assert default_headline["success_rate"] is None
        assert default_headline["avg_cycle_time_s"] is None
        assert default_headline["total_tokens"] == 0
        assert default_headline["total_cost_usd"] == 0.0
        assert default_headline["operational_jobs_excluded"] == 1

        included_headline = store.performance_headline_stats(project.id, include_operational=True)
        assert included_headline["total_jobs"] == 1
        assert included_headline["total_tokens"] == 100
        assert included_headline["total_cost_usd"] == 1.0
        assert included_headline["operational_jobs_excluded"] == 0

        default_usage = store.usage_summary(project_id=project.id)
        assert default_usage["total_tokens"] == 0
        assert default_usage["total_cost_usd"] == 0.0
        assert default_usage["by_source"] == []

        included_usage = store.usage_summary(project_id=project.id, include_operational=True)
        assert included_usage["total_tokens"] == 100
        assert included_usage["total_cost_usd"] == 1.0
        assert [s["source"] for s in included_usage["by_source"]] == ["build"]
    finally:
        store.delete_project(project.id)


def test_jobs_filed_by_breakdown_groups_by_source_and_actor(store):
    project = store.create_project("Filed By Breakdown Project", f"/tmp/test-store-{uuid.uuid4()}")
    ui_actor = f"member-{uuid.uuid4()}@example.com"
    mcp_actor = f"token-{uuid.uuid4()}"
    try:
        store.create(
            idea="job 1",
            repo_path=project.repo_path,
            chat_id=1,
            source=JobSource.UI,
            source_actor=ui_actor,
        )
        store.create(
            idea="job 2",
            repo_path=project.repo_path,
            chat_id=1,
            source=JobSource.UI,
            source_actor=ui_actor,
        )
        store.create(
            idea="job 3",
            repo_path=project.repo_path,
            chat_id=1,
            source=JobSource.MCP,
            source_actor=mcp_actor,
        )

        result = store.jobs_filed_by_breakdown()

        by_pair = {(r["source"], r["source_actor"]): r["count"] for r in result}
        assert by_pair[("ui", ui_actor)] == 2
        assert by_pair[("mcp", mcp_actor)] == 1
    finally:
        store.delete_project(project.id)


def test_jobs_filed_by_breakdown_since_restricts_to_recent_jobs(store):
    from hyqs.pipeline.models import _now

    project = store.create_project(
        "Filed By Breakdown Since Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    actor = f"alice-{uuid.uuid4()}"
    try:
        old_job = store.create(
            idea="old job",
            repo_path=project.repo_path,
            chat_id=1,
            source=JobSource.CLI,
            source_actor=actor,
        )
        new_job = store.create(
            idea="new job",
            repo_path=project.repo_path,
            chat_id=1,
            source=JobSource.CLI,
            source_actor=actor,
        )
        cutoff = _now()
        with store._pool.connection() as conn:
            conn.execute(
                "UPDATE jobs SET created_at = %s WHERE id = %s",
                ("2020-01-01T00:00:00", old_job.id),
            )
            conn.execute(
                "UPDATE jobs SET created_at = %s WHERE id = %s",
                (cutoff, new_job.id),
            )

        result = store.jobs_filed_by_breakdown(since=cutoff)

        by_pair = {(r["source"], r["source_actor"]): r["count"] for r in result}
        assert by_pair[("cli", actor)] == 1
    finally:
        store.delete_project(project.id)


def test_try_acquire_schema_lock_grants_to_one_owner(store):
    project = store.create_project("Schema Lock Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        now = time.time()
        assert store.try_acquire_schema_lock(project.id, "job-1", now, 300) is True
    finally:
        store.release_schema_lock(project.id, "job-1")
        store.delete_project(project.id)


def test_try_acquire_schema_lock_refuses_second_concurrent_owner(store):
    project = store.create_project("Schema Lock Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        now = time.time()
        assert store.try_acquire_schema_lock(project.id, "job-1", now, 300) is True
        assert store.try_acquire_schema_lock(project.id, "job-2", now, 300) is False
    finally:
        store.release_schema_lock(project.id, "job-1")
        store.delete_project(project.id)


def test_try_acquire_schema_lock_reclaims_after_ttl_elapses(store):
    project = store.create_project("Schema Lock Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        stale_time = time.time() - 1000
        assert store.try_acquire_schema_lock(project.id, "job-1", stale_time, 300) is True
        now = time.time()
        assert store.try_acquire_schema_lock(project.id, "job-2", now, 300) is True
    finally:
        store.release_schema_lock(project.id, "job-2")
        store.delete_project(project.id)


def test_release_schema_lock_frees_it_for_new_acquirer(store):
    project = store.create_project("Schema Lock Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        now = time.time()
        assert store.try_acquire_schema_lock(project.id, "job-1", now, 300) is True
        store.release_schema_lock(project.id, "job-1")
        assert store.try_acquire_schema_lock(project.id, "job-2", now, 300) is True
    finally:
        store.release_schema_lock(project.id, "job-2")
        store.delete_project(project.id)


# ---------------------------------------------------------------------------
# get_scheduler_wait: live scheduler diagnostics (job #3164)
# ---------------------------------------------------------------------------


def test_get_scheduler_wait_returns_none_for_running_job(store, dep_env):
    job = dep_env(idea="already running")
    other = dep_env(idea="holds the schema lock")
    now = time.time()
    store.try_acquire_schema_lock(job.project_id, lock_owner_id(other.id), now, 300)
    try:
        job.stage = Stage.MERGE
        job.status = JobStatus.RUNNING
        store.save(job)

        assert store.get_scheduler_wait(job, []) is None
    finally:
        store.release_schema_lock(job.project_id, lock_owner_id(other.id))


def test_get_scheduler_wait_returns_none_when_waiting_on_nonempty(store, dep_env):
    job = dep_env(idea="blocked by dependency")
    other = dep_env(idea="holds the schema lock")
    now = time.time()
    store.try_acquire_schema_lock(job.project_id, lock_owner_id(other.id), now, 300)
    try:
        job.stage = Stage.MERGE
        store.save(job)

        assert store.get_scheduler_wait(job, [other.id]) is None
    finally:
        store.release_schema_lock(job.project_id, lock_owner_id(other.id))


def test_get_scheduler_wait_reports_schema_lock_held_by_other_job(store, dep_env):
    job = dep_env(idea="merge me")
    other = dep_env(idea="holds the schema lock")
    now = time.time()
    store.try_acquire_schema_lock(job.project_id, lock_owner_id(other.id), now, 300)
    try:
        job.stage = Stage.MERGE
        store.save(job)

        wait = store.get_scheduler_wait(job, [])

        assert wait is not None
        assert wait.reason == SchedulerWaitReason.SCHEMA_LOCK
        assert wait.blocking_job_ids == [other.id]
        assert wait.conflicting_paths == []
        assert f"#{other.id}" in wait.summary
    finally:
        store.release_schema_lock(job.project_id, lock_owner_id(other.id))


def test_get_scheduler_wait_schema_lock_clears_after_release(store, dep_env):
    job = dep_env(idea="merge me")
    other = dep_env(idea="holds the schema lock")
    now = time.time()
    store.try_acquire_schema_lock(job.project_id, lock_owner_id(other.id), now, 300)
    job.stage = Stage.MERGE
    store.save(job)
    assert store.get_scheduler_wait(job, []) is not None

    store.release_schema_lock(job.project_id, lock_owner_id(other.id))

    assert store.get_scheduler_wait(job, []) is None


def test_get_scheduler_wait_ignores_schema_lock_held_by_self(store, dep_env):
    job = dep_env(idea="merge me")
    now = time.time()
    store.try_acquire_schema_lock(job.project_id, lock_owner_id(job.id), now, 300)
    try:
        job.stage = Stage.MERGE
        store.save(job)

        assert store.get_scheduler_wait(job, []) is None
    finally:
        store.release_schema_lock(job.project_id, lock_owner_id(job.id))


def test_get_scheduler_wait_reports_merge_lock_when_no_schema_lock(store, dep_env):
    job = dep_env(idea="merge me")
    other = dep_env(idea="holds the merge lock")
    now = time.time()
    store.try_acquire_merge_lock(job.repo_path, lock_owner_id(other.id), now, 300)
    try:
        job.stage = Stage.MERGE
        store.save(job)

        wait = store.get_scheduler_wait(job, [])

        assert wait is not None
        assert wait.reason == SchedulerWaitReason.MERGE_LOCK
        assert wait.blocking_job_ids == [other.id]
        assert f"#{other.id}" in wait.summary
    finally:
        store.release_merge_lock(job.repo_path, lock_owner_id(other.id))


def test_get_scheduler_wait_schema_lock_takes_precedence_over_merge_lock(store, dep_env):
    job = dep_env(idea="merge me")
    schema_owner = dep_env(idea="holds schema lock")
    merge_owner = dep_env(idea="holds merge lock")
    now = time.time()
    store.try_acquire_schema_lock(job.project_id, lock_owner_id(schema_owner.id), now, 300)
    store.try_acquire_merge_lock(job.repo_path, lock_owner_id(merge_owner.id), now, 300)
    try:
        job.stage = Stage.MERGE
        store.save(job)

        wait = store.get_scheduler_wait(job, [])

        assert wait is not None
        assert wait.reason == SchedulerWaitReason.SCHEMA_LOCK
        assert wait.blocking_job_ids == [schema_owner.id]
    finally:
        store.release_schema_lock(job.project_id, lock_owner_id(schema_owner.id))
        store.release_merge_lock(job.repo_path, lock_owner_id(merge_owner.id))


def test_get_scheduler_wait_reports_file_overlap_with_specific_paths_only(store, batch_env):
    scoped, running_job = batch_env(
        [
            {
                "idea": "Touch store.py and other.py",
                "title": "Touch store.py and other.py",
                "depends_on": [],
                "target_files": [],
                "priority": 0,
                "scope": {
                    "allowed_paths": ["hyqs/pipeline/store.py", "hyqs/pipeline/other.py"],
                    "interfaces": "",
                },
            },
            {
                "idea": "Also touch store.py",
                "title": "Also touch store.py",
                "depends_on": [],
                "target_files": [],
                "priority": 0,
                "scope": {"allowed_paths": ["hyqs/pipeline/store.py"], "interfaces": ""},
            },
        ]
    )
    running_job.status = JobStatus.RUNNING
    store.save(running_job)

    wait = store.get_scheduler_wait(scoped, [])

    assert wait is not None
    assert wait.reason == SchedulerWaitReason.FILE_OVERLAP
    assert wait.blocking_job_ids == [running_job.id]
    assert wait.conflicting_paths == ["hyqs/pipeline/store.py"]


def test_get_scheduler_wait_returns_none_when_capable_agent_has_capacity(
    store, dep_env, clean_provider_pauses
):
    job = dep_env(idea="plan me")

    assert store.get_scheduler_wait(job, []) is None


def test_get_scheduler_wait_reports_provider_capacity_without_leaking_other_project_job(
    store, dep_env, clean_provider_pauses
):
    job = dep_env(idea="plan me")
    other_project = store.create_project(
        "Other Provider Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        other_agent = store.list_agents(other_project.id)[0]
        other_job = store.create(idea="other running", repo_path=other_project.repo_path, chat_id=1)
        other_job.status = JobStatus.RUNNING
        other_job.agent_id = other_agent.id
        other_job.provider = "claude"
        store.save(other_job)

        now = time.time()
        store.set_provider_pause("claude", now + 3600)

        wait = store.get_scheduler_wait(job, [])

        assert wait is not None
        assert wait.reason == SchedulerWaitReason.PROVIDER_CAPACITY
        assert wait.blocking_job_ids == []
        assert "claude" in wait.summary
    finally:
        store.delete_project(other_project.id)


def test_get_scheduler_wait_reports_project_slot_occupied_at_max_concurrency(
    store, dep_env, clean_provider_pauses
):
    job = dep_env(idea="plan me")
    occupant = dep_env(idea="occupies the only slot")
    agent_id = _default_agent_id(store, job.project_id)
    occupant.status = JobStatus.RUNNING
    occupant.agent_id = agent_id
    occupant.provider = "claude"
    store.save(occupant)

    wait = store.get_scheduler_wait(job, [])

    assert wait is not None
    assert wait.reason == SchedulerWaitReason.PROJECT_SLOT_OCCUPIED
    assert wait.blocking_job_ids == [occupant.id]

    occupant.status = JobStatus.DONE
    store.save(occupant)

    assert store.get_scheduler_wait(job, []) is None


def test_get_scheduler_wait_file_overlap_takes_precedence_over_provider_capacity(
    store, batch_env, clean_provider_pauses
):
    scoped, running_job = batch_env(
        [
            {
                "idea": "Touch store.py",
                "title": "Touch store.py",
                "depends_on": [],
                "target_files": [],
                "priority": 0,
                "scope": {"allowed_paths": ["hyqs/pipeline/store.py"], "interfaces": ""},
            },
            {
                "idea": "Also touch store.py",
                "title": "Also touch store.py",
                "depends_on": [],
                "target_files": [],
                "priority": 0,
                "scope": {"allowed_paths": ["hyqs/pipeline/store.py"], "interfaces": ""},
            },
        ]
    )
    running_job.status = JobStatus.RUNNING
    store.save(running_job)
    store.set_provider_pause("claude", time.time() + 3600)

    wait = store.get_scheduler_wait(scoped, [])

    assert wait is not None
    assert wait.reason == SchedulerWaitReason.FILE_OVERLAP


def test_create_webhook_returns_webhook_with_id(store):
    project = store.create_project("Webhook Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        webhook = store.create_webhook(
            project.id, "https://example.com/hook", "job_complete", "user@example.com"
        )
        assert webhook.id > 0
        assert webhook.project_id == project.id
        assert webhook.url == "https://example.com/hook"
        assert webhook.event_type == "job_complete"
        assert webhook.created_by == "user@example.com"
        assert webhook.active is True
    finally:
        store.delete_project(project.id)


def test_create_webhook_unknown_project_raises_value_error(store):
    with pytest.raises(ValueError):
        store.create_webhook(999999999, "https://example.com/hook", "deploy")


def test_list_webhooks_returns_project_webhooks_newest_first(store):
    project = store.create_project("Webhook Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        first = store.create_webhook(project.id, "https://example.com/a", "job_complete")
        second = store.create_webhook(project.id, "https://example.com/b", "deploy")
        webhooks = store.list_webhooks(project.id)
        assert [w.id for w in webhooks] == [second.id, first.id]
    finally:
        store.delete_project(project.id)


def test_list_webhooks_empty_for_new_project(store):
    project = store.create_project("Webhook Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        assert store.list_webhooks(project.id) == []
    finally:
        store.delete_project(project.id)


def test_create_webhook_defaults_kind_to_http(store):
    project = store.create_project("Webhook Kind Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        webhook = store.create_webhook(project.id, "https://example.com/hook", "job_complete")
        assert webhook.kind == "http"
    finally:
        store.delete_project(project.id)


def test_get_webhook_returns_none_for_unknown_id(store):
    assert store.get_webhook(999999999) is None


def test_get_webhook_returns_matching_webhook(store):
    project = store.create_project("Webhook Get Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        webhook = store.create_webhook(project.id, "https://example.com/hook", "job_complete")
        assert store.get_webhook(webhook.id) == webhook
    finally:
        store.delete_project(project.id)


def test_set_webhook_active_toggles_and_returns_updated_webhook(store):
    project = store.create_project("Webhook Toggle Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        webhook = store.create_webhook(project.id, "https://example.com/hook", "job_complete")
        updated = store.set_webhook_active(webhook.id, False)
        assert updated.active is False
        assert store.get_webhook(webhook.id).active is False
    finally:
        store.delete_project(project.id)


def test_delete_webhook_removes_it_from_list(store):
    project = store.create_project("Webhook Delete Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        webhook = store.create_webhook(project.id, "https://example.com/hook", "job_complete")
        store.delete_webhook(webhook.id)
        assert store.get_webhook(webhook.id) is None
        assert webhook.id not in {w.id for w in store.list_webhooks(project.id)}
    finally:
        store.delete_project(project.id)


@pytest.fixture
def slack_env(monkeypatch):
    monkeypatch.setenv("HYQS_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())


def test_set_project_slack_token_then_get_round_trips_plaintext(store, slack_env):
    project = store.create_project("Slack Token Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        store.set_project_slack_token(
            project.id, "xoxb-secret-token", created_by="user@example.com"
        )
        assert store.get_project_slack_token(project.id) == "xoxb-secret-token"
    finally:
        store.delete_project(project.id)


def test_project_slack_credentials_stores_ciphertext_not_plaintext(store, slack_env):
    project = store.create_project("Slack Cipher Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        store.set_project_slack_token(project.id, "xoxb-secret-token")
        with store._pool.connection() as conn:
            row = conn.execute(
                "SELECT bot_token_encrypted FROM project_slack_credentials WHERE project_id = %s",
                (project.id,),
            ).fetchone()
        assert row["bot_token_encrypted"] != "xoxb-secret-token"
    finally:
        store.delete_project(project.id)


def test_set_project_slack_token_unknown_project_raises_value_error(store, slack_env):
    with pytest.raises(ValueError):
        store.set_project_slack_token(999999999, "xoxb-secret-token")


def test_get_project_slack_token_returns_none_when_missing(store, slack_env):
    project = store.create_project("Slack Missing Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        assert store.get_project_slack_token(project.id) is None
    finally:
        store.delete_project(project.id)


def test_set_project_slack_token_twice_updates_to_newest_value(store, slack_env):
    project = store.create_project("Slack Update Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        store.set_project_slack_token(project.id, "xoxb-first")
        store.set_project_slack_token(project.id, "xoxb-second")
        assert store.get_project_slack_token(project.id) == "xoxb-second"
    finally:
        store.delete_project(project.id)


def test_get_slack_thread_ts_returns_none_when_absent(store):
    project = store.create_project("Slack Thread Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        webhook = store.create_webhook(project.id, "https://example.com/hook", "notify")
        assert store.get_slack_thread_ts(123, webhook.id) is None
    finally:
        store.delete_project(project.id)


def test_set_slack_thread_ts_then_get_round_trips_value(store):
    project = store.create_project("Slack Thread Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        webhook = store.create_webhook(project.id, "https://example.com/hook", "notify")
        store.set_slack_thread_ts(123, webhook.id, "1111.2222")
        assert store.get_slack_thread_ts(123, webhook.id) == "1111.2222"
    finally:
        store.delete_project(project.id)


def test_set_slack_thread_ts_twice_does_not_clobber_first_value(store):
    project = store.create_project("Slack Thread Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        webhook = store.create_webhook(project.id, "https://example.com/hook", "notify")
        store.set_slack_thread_ts(123, webhook.id, "1111.2222")
        store.set_slack_thread_ts(123, webhook.id, "9999.0000")
        assert store.get_slack_thread_ts(123, webhook.id) == "1111.2222"
    finally:
        store.delete_project(project.id)


@pytest.fixture
def token_project(store):
    project = store.create_project("Api Token Project", f"/tmp/test-store-{uuid.uuid4()}")
    yield project
    store.delete_project(project.id)


def test_create_api_token_returns_plaintext_secret_once(store, token_project):
    token, secret = store.create_api_token(
        token_project.id, "CI token", "contributor", created_by="user:a@example.com"
    )

    assert secret.startswith("hpat_")
    assert token.project_id == token_project.id
    assert token.name == "CI token"
    assert token.role == "contributor"
    assert token.last4 == secret[-4:]


def test_create_api_token_never_persists_plaintext_secret(store, token_project):
    _token, secret = store.create_api_token(token_project.id, "CI token", "viewer")

    with store._pool.connection() as conn:
        row = conn.execute(
            "SELECT token_hash FROM api_tokens WHERE project_id = %s", (token_project.id,)
        ).fetchone()
    assert row["token_hash"] != secret
    assert secret not in row["token_hash"]


def test_list_api_tokens_returns_tokens_for_project(store, token_project):
    store.create_api_token(token_project.id, "Token A", "viewer")
    store.create_api_token(token_project.id, "Token B", "contributor")

    tokens = store.list_api_tokens(token_project.id)

    assert {t.name for t in tokens} == {"Token A", "Token B"}


def test_get_api_token_by_secret_returns_matching_token(store, token_project):
    token, secret = store.create_api_token(token_project.id, "CI token", "contributor")

    found = store.get_api_token_by_secret(secret)

    assert found is not None
    assert found.id == token.id


def test_get_api_token_by_secret_returns_none_for_unknown_secret(store, token_project):
    assert store.get_api_token_by_secret("hpat_does-not-exist") is None


def test_get_api_token_by_secret_returns_none_for_revoked_token(store, token_project):
    token, secret = store.create_api_token(token_project.id, "CI token", "contributor")
    store.revoke_api_token(token_project.id, token.id)

    assert store.get_api_token_by_secret(secret) is None


def test_touch_api_token_sets_last_used_at(store, token_project):
    token, _secret = store.create_api_token(token_project.id, "CI token", "viewer")
    assert token.last_used_at is None

    store.touch_api_token(token.id)

    [refreshed] = store.list_api_tokens(token_project.id)
    assert refreshed.last_used_at is not None


def test_revoke_api_token_scopes_to_own_project(store, token_project):
    other_project = store.create_project("Other Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        token, _secret = store.create_api_token(token_project.id, "CI token", "viewer")

        assert store.revoke_api_token(other_project.id, token.id) is False
        assert store.revoke_api_token(token_project.id, token.id) is True
    finally:
        store.delete_project(other_project.id)


def test_list_epics_includes_per_epic_status_counts(store):
    project = store.create_project("Status Counts Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        epic = store.create_epic(project.id, "Epic A")
        done_job = store.create(
            idea="done", repo_path=project.repo_path, chat_id=1, epic_id=epic.id
        )
        done_job.status = JobStatus.DONE
        store.save(done_job)
        running_job = store.create(
            idea="running", repo_path=project.repo_path, chat_id=1, epic_id=epic.id
        )
        running_job.status = JobStatus.RUNNING
        store.save(running_job)
        failed_job = store.create(
            idea="failed", repo_path=project.repo_path, chat_id=1, epic_id=epic.id
        )
        failed_job.status = JobStatus.FAILED
        store.save(failed_job)

        epics = store.list_epics(project_id=project.id)
        [epic_dict] = [e for e in epics if e["id"] == epic.id]

        assert epic_dict["status_counts"] == {"done": 1, "running": 1, "failed": 1}
    finally:
        store.delete_project(project.id)


def test_list_audit_before_id_pages_strictly_older_non_overlapping_rows(store):
    actor = f"test-audit-{uuid.uuid4()}"
    with store._pool.connection() as conn:
        for i in range(5):
            conn.execute(
                "INSERT INTO audit_log(actor, table_name, row_pk, action) "
                "VALUES (%s, 'jobs', %s, 'update')",
                (actor, str(i)),
            )
    try:
        first_page = store.list_audit(actor=actor, limit=3)
        assert len(first_page) == 3
        assert [r["id"] for r in first_page] == sorted((r["id"] for r in first_page), reverse=True)
        assert all({"backend_pid", "client_addr", "backend_start"} <= r.keys() for r in first_page)

        second_page = store.list_audit(actor=actor, limit=3, before_id=first_page[-1]["id"])
        assert len(second_page) == 2
        assert all({"backend_pid", "client_addr", "backend_start"} <= r.keys() for r in second_page)

        first_ids = {r["id"] for r in first_page}
        second_ids = {r["id"] for r in second_page}
        assert first_ids.isdisjoint(second_ids)
        assert all(rid < first_page[-1]["id"] for rid in second_ids)
        assert [r["id"] for r in second_page] == sorted(second_ids, reverse=True)
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM audit_log WHERE actor = %s", (actor,))


def test_list_supervisor_events_for_job_empty_when_none(store):
    project = store.create_project("Supervisor Events Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        job = store.create(idea="idea", repo_path=project.repo_path, chat_id=1)
        assert store.list_supervisor_events_for_job(job.id) == []
    finally:
        store.delete_project(project.id)


def test_list_supervisor_events_for_job_scoped_ordered_and_parses_detail(store):
    project = store.create_project("Supervisor Events Project 2", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        job_a = store.create(idea="a", repo_path=project.repo_path, chat_id=1)
        job_b = store.create(idea="b", repo_path=project.repo_path, chat_id=1)
        store.record_supervisor_event(
            job_a.id, "ai_diagnosed", "unknown", json.dumps({"diagnosis": "x"})
        )
        store.record_supervisor_event(job_a.id, "escalated", "unknown", "not json")
        store.record_supervisor_event(
            job_b.id, "ai_diagnosed", "unknown", json.dumps({"diagnosis": "y"})
        )

        events = store.list_supervisor_events_for_job(job_a.id)

        assert [e["action"] for e in events] == ["ai_diagnosed", "escalated"]
        assert events[0]["detail"] == {"diagnosis": "x"}
        assert events[1]["detail"] == {}
    finally:
        store.delete_project(project.id)


def test_set_and_get_active_remediation_roundtrips(store):
    project = store.create_project("Active Remediation Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        incident = store.create(idea="incident", repo_path=project.repo_path, chat_id=1)
        remediation = store.create(idea="remediation", repo_path=project.repo_path, chat_id=1)

        store.set_active_remediation(incident.id, remediation.id)
        assert store.get_active_remediation(incident.id) == remediation.id

        store.set_active_remediation(incident.id, None)
        assert store.get_active_remediation(incident.id) is None
    finally:
        store.delete_project(project.id)


def test_get_active_remediation_none_when_never_set(store):
    project = store.create_project(
        "Active Remediation Project 2", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        incident = store.create(idea="incident", repo_path=project.repo_path, chat_id=1)
        assert store.get_active_remediation(incident.id) is None
    finally:
        store.delete_project(project.id)


def test_remediation_lineage_inherits_root_depth_and_terminal_cleanup(store, dep_env):
    root = dep_env("root incident")
    first = store.create_supervisor_remediation(
        root.id, [{"idea": "first fix"}], failure_class="genuine_code"
    ).jobs[0]
    assert first.source_meta["remediation_root_job_id"] == root.id
    assert first.source_meta["remediation_depth"] == 1

    first.status = JobStatus.FAILED
    store.save(first)
    assert store.get_active_remediation(root.id) is None

    second = store.create_supervisor_remediation(
        first.id, [{"idea": "second fix"}], failure_class="genuine_code"
    ).jobs[0]
    assert second.source_meta["remediation_root_job_id"] == root.id
    assert second.source_meta["remediation_depth"] == 2
    assert store.get_active_remediation(first.id) == second.id


def test_done_fix_forward_automatically_resolves_incident(store, dep_env):
    incident = dep_env("failed incident")
    incident.status = JobStatus.FAILED
    store.save(incident)
    remediation = dep_env("successful fix", source_meta={"fix_for": incident.id})

    remediation.status = JobStatus.DONE
    store.save(remediation)

    resolved = store.get(incident.id)
    assert resolved.resolution == "resolved"
    assert resolved.archived is False
    events = store.list_supervisor_events_for_job(incident.id)
    assert events[-1]["action"] == "auto_resolved_by_remediation"
    assert events[-1]["detail"] == {"remediation_job_id": remediation.id}


def test_supervisor_batch_waits_for_every_remediation_before_resolving(store, dep_env):
    incident = dep_env("failed incident")
    incident.status = JobStatus.FAILED
    store.save(incident)
    first = dep_env("first fix", source_meta={"ai_fix_for": incident.id})
    second = dep_env("second fix", source_meta={"ai_fix_for": incident.id})

    first.status = JobStatus.DONE
    store.save(first)
    assert store.get(incident.id).resolution == ""

    second.status = JobStatus.DONE
    store.save(second)
    assert store.get(incident.id).resolution == "resolved"


def test_fix_of_failed_batch_child_propagates_closure_to_parent(store, dep_env):
    incident = dep_env("failed parent")
    incident.status = JobStatus.FAILED
    store.save(incident)
    successful_child = dep_env("successful child", source_meta={"ai_fix_for": incident.id})
    failed_child = dep_env("failed child", source_meta={"ai_fix_for": incident.id})
    successful_child.status = JobStatus.DONE
    store.save(successful_child)
    failed_child.status = JobStatus.FAILED
    store.save(failed_child)
    replacement = dep_env("replacement", source_meta={"fix_for": failed_child.id})

    replacement.status = JobStatus.DONE
    store.save(replacement)

    assert store.get(failed_child.id).resolution == "resolved"
    assert store.get(incident.id).resolution == "resolved"


def test_remediation_closure_is_idempotent_and_requires_persisted_done(store, dep_env):
    incident = dep_env("failed incident")
    incident.status = JobStatus.FAILED
    store.save(incident)
    remediation = dep_env("fix", source_meta={"fix_for": incident.id})

    assert store.propagate_successful_remediation(remediation.id) == []
    assert store.get(incident.id).resolution == ""

    remediation.status = JobStatus.DONE
    store.save(remediation)
    assert store.propagate_successful_remediation(remediation.id) == []
    events = store.list_supervisor_events_for_job(incident.id)
    assert [event["action"] for event in events] == ["auto_resolved_by_remediation"]


def test_create_supervisor_remediation_inherits_root_priority(store, dep_env):
    root = dep_env("root incident", priority=7)

    remediation = store.create_supervisor_remediation(
        root.id, [{"idea": "fix"}], failure_class="genuine_code"
    ).jobs[0]

    assert remediation.priority == 7


def test_supervisor_remediation_persists_source_and_releases_artifact_after_capture(store, dep_env):
    incident = dep_env("failed candidate")
    incident.status = JobStatus.FAILED
    incident.branch = f"hyqs/job-{incident.id}"
    store.save(incident)

    assert incident.id in store.get_git_artifact_protected_job_ids()
    remediation = store.create_supervisor_remediation(
        incident.id,
        [{"idea": "fix exact candidate"}],
        failure_class="genuine_code",
        source_branch=incident.branch,
        source_sha="a" * 40,
    ).jobs[0]

    reloaded = store.get(remediation.id)
    assert reloaded.source_meta["remediation_source"] == {
        "incident_job_id": incident.id,
        "branch": incident.branch,
        "sha": "a" * 40,
        "captured": False,
    }
    assert incident.id in store.get_git_artifact_protected_job_ids()

    store.mark_remediation_source_captured(remediation.id)

    assert incident.id not in store.get_git_artifact_protected_job_ids()


def test_git_artifact_protection_ends_after_resolution_or_archival(store, dep_env):
    resolved = dep_env("resolved failed candidate")
    resolved.status = JobStatus.FAILED
    store.save(resolved)
    assert resolved.id in store.get_git_artifact_protected_job_ids()
    store.set_job_resolution(resolved.id, "resolved")
    assert resolved.id not in store.get_git_artifact_protected_job_ids()

    archived = dep_env("archived failed candidate")
    archived.status = JobStatus.FAILED
    store.save(archived)
    assert archived.id in store.get_git_artifact_protected_job_ids()
    store.set_archived(archived.id, True)
    assert archived.id not in store.get_git_artifact_protected_job_ids()


def test_remediation_creation_is_atomic_per_root_but_independent_between_roots(store, dep_env):
    root_a = dep_env("root a")
    root_b = dep_env("root b")

    def create(incident_id):
        return store.create_supervisor_remediation(
            incident_id, [{"idea": f"fix {incident_id}"}], failure_class="unknown"
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(create, [root_a.id, root_a.id, root_b.id]))

    root_a_results = results[:2]
    assert sum(bool(result.jobs) for result in root_a_results) == 1
    assert sum(result.active_remediation_job_id is not None for result in root_a_results) == 1
    assert results[2].jobs


def test_create_supervisor_remediation_persists_external_queue_dependencies(store, dep_env):
    root = dep_env("root incident")
    other_active = dep_env("unrelated active job")

    result = store.create_supervisor_remediation(
        root.id,
        [
            {"idea": "first fix", "depends_on_job_ids": [other_active.id]},
            {"idea": "second fix", "depends_on_indexes": [0]},
        ],
        failure_class="genuine_code",
    )

    first, second = result.jobs
    assert store.get_dependencies(first.id) == [other_active.id]
    assert store.get_dependencies(second.id) == [first.id]
    assert store.get_dependencies(root.id) == [second.id]


def test_supervisor_remediation_chain_seeds_source_only_at_dag_roots(store, dep_env):
    incident = dep_env("failed candidate for chained repair")
    incident.status = JobStatus.FAILED
    incident.branch = f"hyqs/job-{incident.id}"
    store.save(incident)

    first, second = store.create_supervisor_remediation(
        incident.id,
        [
            {"idea": "seed and repair candidate"},
            {"idea": "continue repair", "depends_on_indexes": [0]},
        ],
        failure_class="genuine_code",
        source_branch=incident.branch,
        source_sha="a" * 40,
    ).jobs

    assert first.source_meta["remediation_source"]["sha"] == "a" * 40
    assert "remediation_source" not in second.source_meta
    assert store.get_dependencies(second.id) == [first.id]


def test_git_artifacts_remain_protected_until_every_dag_root_captures(store, dep_env):
    incident = dep_env("failed candidate for parallel repair")
    incident.status = JobStatus.FAILED
    incident.branch = f"hyqs/job-{incident.id}"
    store.save(incident)
    first, second = store.create_supervisor_remediation(
        incident.id,
        [{"idea": "first root repair"}, {"idea": "second root repair"}],
        failure_class="genuine_code",
        source_branch=incident.branch,
        source_sha="b" * 40,
    ).jobs

    store.mark_remediation_source_captured(first.id)
    assert incident.id in store.get_git_artifact_protected_job_ids()

    store.mark_remediation_source_captured(second.id)
    assert incident.id not in store.get_git_artifact_protected_job_ids()


def test_create_supervisor_remediation_rejects_cycle_atomically(store, dep_env):
    root = dep_env("root incident")
    external = dep_env("external blocker")
    store.add_job_dependency(external.id, root.id)

    with pytest.raises(ValueError, match="dependency cycle"):
        store.create_supervisor_remediation(
            root.id,
            [{"idea": "fix", "depends_on_job_ids": [external.id]}],
            failure_class="genuine_code",
        )

    assert store.get_dependencies(root.id) == []
    assert store.get_active_remediation(root.id) is None
    with store._pool.connection() as conn:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE source_meta->>'ai_fix_for'=%s", (str(root.id),)
        ).fetchall()
    assert rows == []


def test_concurrent_supervisor_remediations_cannot_commit_cycle(store, dep_env):
    root_a = dep_env("incident a")
    root_b = dep_env("incident b")

    def create(incident_id, external_id):
        try:
            return store.create_supervisor_remediation(
                incident_id,
                [{"idea": "fix", "depends_on_job_ids": [external_id]}],
                failure_class="genuine_code",
            )
        except ValueError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(create, root_a.id, root_b.id),
            pool.submit(create, root_b.id, root_a.id),
        ]
        results = [future.result() for future in futures]

    assert sum(isinstance(result, ValueError) for result in results) == 1
    with store._pool.connection() as conn:
        rows = conn.execute("SELECT job_id, depends_on_job_id FROM job_dependencies").fetchall()
    edges: dict[int, set[int]] = {}
    for row in rows:
        edges.setdefault(int(row["job_id"]), set()).add(int(row["depends_on_job_id"]))
    nodes = set(edges) | {
        dependency for dependencies in edges.values() for dependency in dependencies
    }
    assert store._dependency_topo_order(nodes, edges) is not None


def test_supervisor_auto_dependency_is_reconciled_with_structured_events(store, dep_env):
    root = dep_env("root incident", source_meta={"scope": {"allowed_paths": ["src/a.py"]}})
    external = dep_env("external blocker", source_meta={"scope": {"allowed_paths": ["src/b.py"]}})

    remediation = store.create_supervisor_remediation(
        root.id,
        [
            {
                "idea": "fix",
                "source_meta": {"scope": {"allowed_paths": ["src/a.py"]}},
                "depends_on_job_ids": [external.id],
            }
        ],
        failure_class="genuine_code",
    ).jobs[0]

    assert store.get_dependencies(remediation.id) == []
    events = [event for event in store.list_events(remediation.id) if event["stage"] == "scheduler"]
    assert [event["summary"] for event in events] == [
        f"auto dependency added: #{external.id}",
        f"auto dependency removed: #{external.id}",
    ]
    assert [event["detail"] for event in events] == [
        {
            "action": "added",
            "depends_on_job_id": external.id,
            "provenance": "auto",
            "reason": "external_queue_collision",
        },
        {
            "action": "removed",
            "depends_on_job_id": external.id,
            "provenance": "auto",
            "reason": "known_disjoint_scopes",
        },
    ]
    assert store.reconcile_auto_dependencies(root.project_id) == []


def test_legacy_remediation_ancestry_and_cycle_are_bounded(store, dep_env):
    root = dep_env("legacy root")
    child = store.create(
        "legacy child",
        root.repo_path,
        root.chat_id,
        source_meta={"ai_fix_for": root.id},
    )
    lineage = store.resolve_remediation_lineage(child.id)
    assert (lineage.root_job_id, lineage.depth) == (root.id, 1)

    with store._connection() as conn:
        conn.execute(
            "UPDATE jobs SET source_meta=%s WHERE id=%s",
            (json.dumps({"ai_fix_for": child.id}), root.id),
        )
    cyclic = store.resolve_remediation_lineage(child.id)
    assert cyclic.root_job_id == min(root.id, child.id)
    assert cyclic.depth <= store_module._REMEDIATION_ANCESTRY_LIMIT


def test_human_active_remediation_conflicts_across_lineage(store, dep_env):
    root = dep_env("root")
    automated = store.create_supervisor_remediation(
        root.id, [{"idea": "automated"}], failure_class="unknown"
    ).jobs[0]
    automated.status = JobStatus.FAILED
    store.save(automated)
    human = store.create(
        "human fix",
        root.repo_path,
        root.chat_id,
        source=JobSource.MCP,
        source_meta={"fixes_job_id": automated.id},
    )
    store.set_active_remediation(automated.id, human.id)
    assert store.get_active_remediation(root.id) == human.id


def test_set_job_resolution_updates_terminal_job(store, dep_env):
    job = dep_env()
    job.status = JobStatus.FAILED
    store.save(job)

    assert store.set_job_resolution(job.id, "resolved") is True
    assert store.get(job.id).resolution == "resolved"


def test_set_job_resolution_returns_false_for_unknown_job(store):
    assert store.set_job_resolution(999_999_999, "resolved") is False


def test_update_job_idea_persists_new_idea(store, dep_env):
    job = dep_env()

    assert store.update_job_idea(job.id, "a better idea") is True
    assert store.get(job.id).idea == "a better idea"


def test_update_job_idea_returns_false_for_unknown_job(store):
    assert store.update_job_idea(999_999_999, "a better idea") is False


def test_update_job_fields_updates_only_supplied_fields(store, dep_env):
    job = dep_env(idea="original idea")
    original_title = job.title

    updated = store.update_job_fields(job.id, idea="a new idea")

    assert updated.idea == "a new idea"
    assert updated.title == original_title
    assert updated.priority == job.priority


def test_update_job_fields_applies_multiple_fields_atomically(store, dep_env):
    job = dep_env()
    epic = store.create_epic(job.project_id, "Multi-field epic")
    try:
        updated = store.update_job_fields(
            job.id, title="new title", idea="new idea", priority=5, epic_id=epic.id
        )

        assert updated.title == "new title"
        assert updated.idea == "new idea"
        assert updated.priority == 5
        assert updated.epic_id == epic.id
    finally:
        with store._pool.connection() as conn:
            conn.execute("UPDATE jobs SET epic_id = NULL WHERE id = %s", (job.id,))
            conn.execute("DELETE FROM epics WHERE id = %s", (epic.id,))


def test_update_job_fields_bumps_updated_at(store, dep_env):
    job = dep_env()
    time.sleep(0.01)

    updated = store.update_job_fields(job.id, title="bumped title")

    assert updated.updated_at > job.updated_at


def test_update_job_fields_returns_none_for_unknown_job(store):
    assert store.update_job_fields(999_999_999, title="doesn't matter") is None


def test_update_job_fields_no_fields_returns_unchanged_job(store, dep_env):
    job = dep_env()

    updated = store.update_job_fields(job.id)

    assert updated == job


def test_retry_without_force_leaves_source_meta_untouched(store):
    project = store.create_project("Retry Force Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        job = store.create(
            idea="idea", repo_path=project.repo_path, chat_id=1, source_meta={"role": "mcp"}
        )
        store.cancel(job.id)

        updated = store.retry(job.id)

        assert updated.source_meta == {"role": "mcp"}
    finally:
        store.delete_project(project.id)


def test_retry_with_force_sets_bypass_and_preserves_existing_meta(store):
    project = store.create_project("Retry Force Project 2", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        job = store.create(
            idea="idea", repo_path=project.repo_path, chat_id=1, source_meta={"role": "mcp"}
        )
        store.cancel(job.id)

        updated = store.retry(job.id, force=True)

        assert updated.source_meta == {"role": "mcp", "scope_gate_bypass": True}
        assert updated.status == JobStatus.PENDING
        assert updated.stage == Stage.QUEUED
    finally:
        store.delete_project(project.id)


def test_job_from_row_defaults_plan_reask_attempts_when_column_absent():
    from hyqs.pipeline.models import Job

    row = {
        "id": 1,
        "idea": "idea",
        "repo_path": "/tmp/x",
        "chat_id": 1,
        "branch": "",
        "stage": "queued",
        "status": "pending",
        "plan": None,
        "review": None,
        "error": "",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }

    job = Job.from_row(row)

    assert job.plan_reask_attempts == 0


def test_job_from_row_reads_plan_reask_attempts_when_present():
    from hyqs.pipeline.models import Job

    row = {
        "id": 1,
        "idea": "idea",
        "repo_path": "/tmp/x",
        "chat_id": 1,
        "branch": "",
        "stage": "queued",
        "status": "pending",
        "plan": None,
        "review": None,
        "error": "",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "plan_reask_attempts": 1,
    }

    job = Job.from_row(row)

    assert job.plan_reask_attempts == 1


def test_save_persists_plan_reask_attempts(store):
    project = store.create_project("Plan Reask Save Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        job = store.create(idea="idea", repo_path=project.repo_path, chat_id=1)
        assert job.plan_reask_attempts == 0

        job.plan_reask_attempts = 1
        store.save(job)

        assert store.get(job.id).plan_reask_attempts == 1
    finally:
        store.delete_project(project.id)


def test_structured_failure_state_round_trips_and_terminal_clears_execution(store, dep_env):
    job = dep_env()
    job.status = JobStatus.RUNNING
    job.stage = Stage.PLAN
    job.executing_step = "build"
    job.failed_step = "build"
    job.failure_code = "no_diff_verification_failed"
    job.failure_origin = "ai_gate"
    job.retry_disposition = "human_review"
    job.failure_detail = {"verification": "rejected", "evidence": "missing route"}
    store.save(job)

    saved = store.get(job.id)
    assert saved.executing_step == "build"
    assert saved.failed_step == "build"
    assert saved.failure_code == "no_diff_verification_failed"
    assert saved.failure_detail == {"verification": "rejected", "evidence": "missing route"}

    saved.status = JobStatus.DONE
    saved.stage = Stage.DONE
    store.save(saved)
    completed = store.get(job.id)
    assert completed.executing_step is None
    assert completed.failure_code is None


def test_retry_clears_structured_failure_state(store, dep_env):
    job = dep_env()
    job.status = JobStatus.FAILED
    job.executing_step = "build"
    job.failed_step = "build"
    job.failure_code = "unexpected_stage_error"
    job.failure_origin = "infrastructure"
    job.retry_disposition = "same_step"
    job.failure_detail = {"exception_type": "RuntimeError"}
    store.save(job)

    retried = store.retry(job.id)

    assert retried is not None
    assert retried.executing_step is None
    assert retried.failed_step is None
    assert retried.failure_code is None
    assert retried.failure_detail is None


def test_requeue_no_diff_build_preserves_bounded_retry_context_once(store, dep_env):
    job = dep_env()
    job.status = JobStatus.FAILED
    job.stage = Stage.PLAN
    job.failed_step = "build"
    job.failure_code = "no_diff_verification_failed"
    job.failure_origin = "ai_gate"
    job.retry_disposition = "retry_build"
    job.failure_detail = {
        "verification": "rejected",
        "evidence": "GET /required is absent",
        "retry_attempt": 0,
        "outcome": "retry_build",
    }
    store.save(job)

    assert store.requeue_no_diff_build(job.id) is True
    retried = store.get(job.id)
    assert retried.status == JobStatus.PENDING
    assert retried.stage == Stage.PLAN
    assert retried.failed_step == "build"
    assert retried.failure_detail["evidence"] == "GET /required is absent"
    assert retried.failure_detail["retry_attempt"] == 1
    assert store.requeue_no_diff_build(job.id) is False


def test_retry_resets_plan_reask_attempts(store):
    project = store.create_project("Plan Reask Retry Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        job = store.create(idea="idea", repo_path=project.repo_path, chat_id=1)
        job.plan_reask_attempts = 1
        store.save(job)
        store.cancel(job.id)

        updated = store.retry(job.id)

        assert updated.plan_reask_attempts == 0
    finally:
        store.delete_project(project.id)


def test_requeue_job_zero_attempts_resets_plan_reask_attempts(store):
    project = store.create_project("Plan Reask Requeue Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        job = store.create(idea="idea", repo_path=project.repo_path, chat_id=1)
        with store._pool.connection() as conn:
            conn.execute(
                "UPDATE jobs SET status='failed', plan_reask_attempts=1 WHERE id=%s",
                (job.id,),
            )

        store.requeue_job(job.id, zero_attempts=True)

        assert store.get(job.id).plan_reask_attempts == 0
    finally:
        store.delete_project(project.id)


def test_mark_needs_split_sets_needs_split_and_archived(store):
    project = store.create_project("Needs Split Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        job = store.create(idea="idea", repo_path=project.repo_path, chat_id=1)

        updated = store.mark_needs_split(job.id)

        assert updated.needs_split is True
        assert updated.archived is True
        assert store.get(job.id).needs_split is True
        assert store.get(job.id).archived is True
    finally:
        store.delete_project(project.id)


def test_mark_needs_split_returns_none_for_unknown_job(store):
    assert store.mark_needs_split(999_999_999) is None


def test_retry_returns_none_for_needs_split_job(store):
    project = store.create_project("Needs Split Retry Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        job = store.create(idea="idea", repo_path=project.repo_path, chat_id=1)
        store.cancel(job.id)
        store.mark_needs_split(job.id)

        assert store.retry(job.id) is None
        # bare retry stays refused even after a force retry attempt (unrelated path)
        assert store.get(job.id).needs_split is True
    finally:
        store.delete_project(project.id)


def test_retry_force_true_unparks_needs_split_job(store):
    project = store.create_project(
        "Needs Split Force Retry Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        job = store.create(idea="idea", repo_path=project.repo_path, chat_id=1)
        store.cancel(job.id)
        store.mark_needs_split(job.id)

        updated = store.retry(job.id, force=True)

        assert updated is not None
        assert updated.needs_split is False
        assert updated.archived is False
        assert updated.status == JobStatus.PENDING
        assert updated.stage == Stage.QUEUED
        assert updated.source_meta.get("scope_gate_bypass") is True
    finally:
        store.delete_project(project.id)


def test_create_backlog_item_rejects_invalid_type(store):
    project = store.create_project("Backlog Type Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        with pytest.raises(ValueError):
            store.create_backlog_item(project.id, "Do the thing", "", "task", "someone")
        assert store.list_backlog_items(project.id) == []
    finally:
        store.delete_project(project.id)


def test_patch_backlog_item_rejects_invalid_status(store):
    project = store.create_project("Backlog Status Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        item = store.create_backlog_item(project.id, "Do the thing", "", "idea", "someone")
        with pytest.raises(ValueError):
            store.patch_backlog_item(item.id, status="bogus")
        assert store.get_backlog_item(item.id).status.value == "new"
    finally:
        store.delete_project(project.id)


def test_list_backlog_items_skips_corrupted_row(store):
    project = store.create_project("Backlog Corrupt Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        good = store.create_backlog_item(project.id, "Good item", "", "idea", "someone")
        from hyqs.pipeline.models import _now

        now = _now()
        with store._connection() as conn:
            row = conn.execute(
                "INSERT INTO backlog_items(project_id, title, body, type, proposed_by, "
                "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (project.id, "Corrupted item", "", "task", "someone", now, now),
            ).fetchone()
            bad_id = row["id"]

        items = store.list_backlog_items(project.id)

        assert [i.id for i in items] == [good.id]
        assert bad_id not in [i.id for i in items]
    finally:
        store.delete_project(project.id)


# --- DONE transitions clear stale `failure`/`error` text ------------------
#
# A job that fails a gate, gets fixed, and reaches DONE must not keep the
# original rejection message around forever — see job #1086.


def test_gate_reject_then_fix_then_done_clears_failure(store, dep_env):
    job = dep_env(idea="gate reject then fix")
    rejection = "security rejected: exploitable user-enumeration vulnerability (CWE-208)"
    job.status = JobStatus.FAILED
    job.failure = rejection
    store.save(job)
    assert store.get(job.id).failure == rejection

    # The fix loop carries the rejection forward into FIX...
    store.requeue_job_at_stage(job.id, Stage.FIX, failure=rejection)
    assert store.get(job.id).failure == rejection

    # ...but once the job reaches DONE the stale rejection must be cleared.
    fixed = store.get(job.id)
    fixed.status = JobStatus.DONE
    store.save(fixed)

    done = store.get(job.id)
    assert done.status == JobStatus.DONE
    assert done.failure == ""
    assert done.error == ""


def test_failed_job_retains_failure_text(store, dep_env):
    job = dep_env(idea="stays failed")
    message = "review rejected: missing test coverage"
    job.status = JobStatus.FAILED
    job.failure = message
    store.save(job)

    assert store.get(job.id).failure == message


def test_finalize_deploying_job_clears_failure(store, dep_env):
    job = dep_env(idea="deploying with stale failure")
    job.status = JobStatus.DEPLOYING
    job.failure = "an earlier gate rejection"
    store.save(job)
    assert store.get(job.id).failure == "an earlier gate rejection"

    store.finalize_deploying_job(job.id, deployed_commit="abc1234")

    done = store.get(job.id)
    assert done.status == JobStatus.DONE
    assert done.failure == ""
    assert done.error == ""


# --- "failed" status filter excludes cancelled jobs (Needs Attention) -----
#
# A user-cancelled job is a deliberate action, not a failure needing a human
# look, so it must not surface in attention lists — see job #1112.


def test_list_active_failed_filter_excludes_cancelled(store, dep_env):
    failed = dep_env(idea="genuinely failed")
    failed.status = JobStatus.FAILED
    store.save(failed)

    cancelled = dep_env(idea="user cancelled")
    store.cancel(cancelled.id)

    results = store.list_active(project_id=failed.project_id, status="failed")

    ids = {j.id for j in results}
    assert failed.id in ids
    assert cancelled.id not in ids


def test_list_active_filters_deploying_as_active_only_and_preserves_order_limit(store, dep_env):
    pending = dep_env(idea="pending active job")
    running = dep_env(idea="running active job")
    running.status = JobStatus.RUNNING
    store.save(running)
    deploying = dep_env(idea="deploying active job")
    deploying.status = JobStatus.DEPLOYING
    deploying.stage = Stage.DEPLOY
    store.save(deploying)
    done = dep_env(idea="done terminal job")
    done.status = JobStatus.DONE
    store.save(done)
    failed = dep_env(idea="failed terminal job")
    failed.status = JobStatus.FAILED
    store.save(failed)

    active = store.list_active(limit=2, project_id=pending.project_id, status="active")
    done_jobs = store.list_active(project_id=pending.project_id, status="done")
    failed_jobs = store.list_active(project_id=pending.project_id, status="failed")

    assert [job.id for job in active] == [deploying.id, running.id]
    assert deploying.id not in {job.id for job in done_jobs}
    assert deploying.id not in {job.id for job in failed_jobs}
    assert done.id in {job.id for job in done_jobs}
    assert failed.id in {job.id for job in failed_jobs}


def test_jobs_page_is_typed_and_defaults_to_active_unarchived(store, dep_env):
    pending = dep_env(idea="pending")
    running = dep_env(idea="running")
    running.status = JobStatus.RUNNING
    store.save(running)
    done = dep_env(idea="done")
    done.status = JobStatus.DONE
    store.save(done)
    archived = dep_env(idea="archived")
    store.set_archived(archived.id, True)

    page = store.list_jobs_page(pending.project_id)

    assert isinstance(page, JobsPage)
    assert [job.id for job in page.jobs] == [running.id, pending.id]
    assert page.next_cursor is None


@pytest.mark.parametrize(
    ("status", "expected_ideas"),
    [
        ("pending", {"pending"}),
        ("running", {"running"}),
        ("deploying", {"deploying"}),
        ("done", {"done"}),
        ("failed", {"failed"}),
        ("cancelled", {"cancelled"}),
        ("terminal", {"done", "failed", "cancelled"}),
        ("archived", {"archived"}),
        (
            "all",
            {"pending", "running", "deploying", "done", "failed", "cancelled", "archived"},
        ),
    ],
)
def test_jobs_page_status_filters_preserve_archive_semantics(
    store, dep_env, status, expected_ideas
):
    pending = dep_env(idea="pending")
    for idea, job_status in (
        ("running", JobStatus.RUNNING),
        ("deploying", JobStatus.DEPLOYING),
        ("done", JobStatus.DONE),
        ("failed", JobStatus.FAILED),
        ("cancelled", JobStatus.CANCELLED),
    ):
        job = dep_env(idea=idea)
        job.status = job_status
        store.save(job)
    archived = dep_env(idea="archived")
    store.set_archived(archived.id, True)

    page = store.list_jobs_page(pending.project_id, status=status)

    assert {job.idea for job in page.jobs} == expected_ideas


def test_jobs_page_keyset_cursor_traverses_without_gaps(store, dep_env):
    jobs = [dep_env(idea=f"page job {index}") for index in range(55)]

    first = store.list_jobs_page(jobs[0].project_id)
    second = store.list_jobs_page(jobs[0].project_id, cursor=first.next_cursor)

    assert len(first.jobs) == 50
    assert first.next_cursor == first.jobs[-1].id
    assert len(second.jobs) == 5
    assert second.next_cursor is None
    ids = [job.id for job in first.jobs + second.jobs]
    assert ids == sorted((job.id for job in jobs), reverse=True)
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("cursor", [0, -1, True, "10"])
def test_jobs_page_rejects_invalid_cursor(store, dep_env, cursor):
    job = dep_env(idea="cursor validation")

    with pytest.raises(ValueError, match="cursor must be a positive integer"):
        store.list_jobs_page(job.project_id, cursor=cursor)


def test_jobs_page_rejects_unknown_status(store, dep_env):
    job = dep_env(idea="status validation")

    with pytest.raises(ValueError, match="unknown job status filter"):
        store.list_jobs_page(job.project_id, status="mystery")


def test_jobs_page_explicit_none_status_matches_default_active(store, dep_env):
    pending = dep_env(idea="pending")
    running = dep_env(idea="running")
    running.status = JobStatus.RUNNING
    store.save(running)

    default_page = store.list_jobs_page(pending.project_id)
    explicit_none_page = store.list_jobs_page(pending.project_id, status=None)

    assert [job.id for job in explicit_none_page.jobs] == [job.id for job in default_page.jobs]
    assert explicit_none_page.next_cursor == default_page.next_cursor


def test_jobs_page_covers_every_status_bucket_including_active_and_all(store, dep_env):
    pending = dep_env(idea="pending")
    extra_active_default = dep_env(idea="extra active default")
    running = dep_env(idea="running")
    running.status = JobStatus.RUNNING
    store.save(running)
    deploying = dep_env(idea="deploying")
    deploying.status = JobStatus.DEPLOYING
    store.save(deploying)
    done = dep_env(idea="done")
    done.status = JobStatus.DONE
    store.save(done)
    failed = dep_env(idea="failed")
    failed.status = JobStatus.FAILED
    store.save(failed)
    cancelled = dep_env(idea="cancelled")
    cancelled.status = JobStatus.CANCELLED
    store.save(cancelled)
    archived = dep_env(idea="archived")
    store.set_archived(archived.id, True)

    project_id = pending.project_id
    expected_by_status = {
        "pending": {pending.id, extra_active_default.id},
        "running": {running.id},
        "deploying": {deploying.id},
        "done": {done.id},
        "failed": {failed.id},
        "cancelled": {cancelled.id},
        "archived": {archived.id},
        "active": {pending.id, extra_active_default.id, running.id, deploying.id},
        "all": {
            pending.id,
            extra_active_default.id,
            running.id,
            deploying.id,
            done.id,
            failed.id,
            cancelled.id,
            archived.id,
        },
    }

    for status, expected_ids in expected_by_status.items():
        page = store.list_jobs_page(project_id, status=status)
        assert {job.id for job in page.jobs} == expected_ids, status


def test_jobs_page_full_traversal_covers_every_seeded_job_without_gaps(store, dep_env):
    jobs = [dep_env(idea=f"traversal job {index}") for index in range(120)]
    seeded_ids = {job.id for job in jobs}
    project_id = jobs[0].project_id

    collected: list[int] = []
    cursor = None
    pages_seen = 0
    while True:
        page = store.list_jobs_page(project_id, cursor=cursor)
        assert len(page.jobs) <= 50
        collected.extend(job.id for job in page.jobs)
        pages_seen += 1
        cursor = page.next_cursor
        if cursor is None:
            break
        assert pages_seen < 10  # guard against a pagination bug looping forever

    assert pages_seen == 3
    assert collected == sorted(collected, reverse=True)
    assert len(collected) == len(set(collected))
    assert set(collected) == seeded_ids


def test_jobs_page_next_cursor_none_for_partial_page(store, dep_env):
    jobs = [dep_env(idea=f"partial page job {index}") for index in range(5)]

    page = store.list_jobs_page(jobs[0].project_id)

    assert len(page.jobs) == 5
    assert page.next_cursor is None


def test_archive_terminal_still_archives_cancelled_jobs(store, dep_env):
    failed = dep_env(idea="genuinely failed for archive")
    failed.status = JobStatus.FAILED
    store.save(failed)

    cancelled = dep_env(idea="user cancelled for archive")
    store.cancel(cancelled.id)

    archived = store.archive_terminal(project_id=failed.project_id)

    archived_ids = {j.id for j in archived}
    assert failed.id in archived_ids
    assert cancelled.id in archived_ids


def test_reconcile_to_done_clears_failure(store, dep_env):
    dep = dep_env(idea="dependency")
    dep.status = JobStatus.FAILED
    dep.failure = "stale rejection text"
    store.save(dep)
    assert store.get(dep.id).failure == "stale rejection text"

    store.reconcile_to_done(dep.id)

    reconciled = store.get(dep.id)
    assert reconciled.status == JobStatus.DONE
    assert reconciled.failure == ""


def test_backfill_clears_failure_on_done_rows_only(store, dep_env):
    from hyqs.pipeline.models import _now
    from hyqs.pipeline.store import _SCHEMA

    backfill_sql = next(s for s in _SCHEMA if "UPDATE jobs SET failure=''" in s)

    done_job = dep_env(idea="stale done job")
    failed_job = dep_env(idea="stale failed job")
    now = _now()
    with store._connection() as conn:
        conn.execute(
            "UPDATE jobs SET status=%s, failure=%s, updated_at=%s WHERE id=%s",
            (JobStatus.DONE.value, "stale rejection", now, done_job.id),
        )
        conn.execute(
            "UPDATE jobs SET status=%s, failure=%s, updated_at=%s WHERE id=%s",
            (JobStatus.FAILED.value, "live rejection", now, failed_job.id),
        )

        conn.execute(backfill_sql)

        conn.execute(backfill_sql)  # idempotent: a second run is a no-op

    assert store.get(done_job.id).failure == ""
    assert store.get(failed_job.id).failure == "live rejection"


def _insert_deploy_lock(store, project_id: int, owner: str, acquired_at: str) -> None:
    env = store.get_or_create_default_environment(project_id)
    with store._pool.connection() as conn:
        conn.execute(
            "INSERT INTO deploy_locks(project_id, owner, acquired_at, environment_id) "
            "VALUES (%s, %s, %s, %s)",
            (project_id, owner, acquired_at, env.id),
        )


def _get_deploy_lock(store, project_id: int):
    with store._pool.connection() as conn:
        return conn.execute(
            "SELECT owner, acquired_at, environment_id FROM deploy_locks WHERE project_id = %s",
            (project_id,),
        ).fetchone()


def test_acquire_deploy_lock_takes_over_when_owner_job_done(store):
    from hyqs.pipeline.models import _now

    project = store.create_project("Deploy Lock Done Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        owner_job = store.create(idea="deploy", repo_path=project.repo_path, chat_id=1)
        owner_job.status = JobStatus.DONE
        store.save(owner_job)
        _insert_deploy_lock(store, project.id, f"job-{owner_job.id}", _now())

        acquired = asyncio.run(store.acquire_deploy_lock(project.id, "job-999", timeout_s=5.0))

        assert acquired is True
        row = _get_deploy_lock(store, project.id)
        assert row["owner"] == "job-999"
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM deploy_locks WHERE project_id = %s", (project.id,))
        store.delete_project(project.id)


def test_acquire_deploy_lock_waits_out_live_owner(store):
    from hyqs.pipeline.models import _now

    project = store.create_project("Deploy Lock Live Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        owner_job = store.create(idea="deploy", repo_path=project.repo_path, chat_id=1)
        owner_job.status = JobStatus.RUNNING
        store.save(owner_job)
        _insert_deploy_lock(store, project.id, f"job-{owner_job.id}", _now())

        acquired = asyncio.run(store.acquire_deploy_lock(project.id, "job-999", timeout_s=0.0))

        assert acquired is False
        row = _get_deploy_lock(store, project.id)
        assert row["owner"] == f"job-{owner_job.id}"
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM deploy_locks WHERE project_id = %s", (project.id,))
        store.delete_project(project.id)


def test_acquire_deploy_lock_takes_over_when_ttl_expired(store):
    project = store.create_project("Deploy Lock TTL Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        owner_job = store.create(idea="deploy", repo_path=project.repo_path, chat_id=1)
        owner_job.status = JobStatus.RUNNING
        store.save(owner_job)
        old_acquired_at = (datetime.now(timezone.utc) - timedelta(seconds=7200)).isoformat()
        _insert_deploy_lock(store, project.id, f"job-{owner_job.id}", old_acquired_at)

        acquired = asyncio.run(
            store.acquire_deploy_lock(project.id, "job-999", timeout_s=5.0, stale_ttl_s=3600.0)
        )

        assert acquired is True
        row = _get_deploy_lock(store, project.id)
        assert row["owner"] == "job-999"
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM deploy_locks WHERE project_id = %s", (project.id,))
        store.delete_project(project.id)


def test_takeover_stale_lock_race_exactly_one_winner(store):
    from hyqs.pipeline.models import _now

    project = store.create_project("Deploy Lock Race Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        owner_job = store.create(idea="deploy", repo_path=project.repo_path, chat_id=1)
        owner_job.status = JobStatus.DONE
        store.save(owner_job)
        _insert_deploy_lock(store, project.id, f"job-{owner_job.id}", _now())

        real_is_stale = store._deploy_lock_is_stale

        def _race_is_stale(owner, acquired_at, ttl):
            # A concurrent reaper wins the race right after our SELECT.
            store.release_deploy_lock(project.id, owner)
            return real_is_stale(owner, acquired_at, ttl)

        with patch.object(store, "_deploy_lock_is_stale", side_effect=_race_is_stale):
            won = store._takeover_stale_deploy_lock(project.id, "job-999", 3600.0)

        assert won is False
        assert _get_deploy_lock(store, project.id) is None
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM deploy_locks WHERE project_id = %s", (project.id,))
        store.delete_project(project.id)


def test_sweep_stale_deploy_locks_removes_only_terminal_owners(store):
    from hyqs.pipeline.models import _now

    project_done = store.create_project("Sweep Done Project", f"/tmp/test-store-{uuid.uuid4()}")
    project_running = store.create_project(
        "Sweep Running Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    project_archived = store.create_project(
        "Sweep Archived Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        done_job = store.create(idea="done", repo_path=project_done.repo_path, chat_id=1)
        done_job.status = JobStatus.DONE
        store.save(done_job)
        running_job = store.create(idea="running", repo_path=project_running.repo_path, chat_id=1)
        running_job.status = JobStatus.RUNNING
        store.save(running_job)
        archived_job = store.create(
            idea="archived", repo_path=project_archived.repo_path, chat_id=1
        )
        store.set_archived(archived_job.id, True)

        _insert_deploy_lock(store, project_done.id, f"job-{done_job.id}", _now())
        _insert_deploy_lock(store, project_running.id, f"job-{running_job.id}", _now())
        _insert_deploy_lock(store, project_archived.id, f"job-{archived_job.id}", _now())

        reaped = store.sweep_stale_deploy_locks()

        own_projects = {project_done.id, project_running.id, project_archived.id}
        own_reaped = {r["project_id"] for r in reaped if r["project_id"] in own_projects}
        assert own_reaped == {project_done.id, project_archived.id}
        assert _get_deploy_lock(store, project_running.id) is not None
        assert _get_deploy_lock(store, project_done.id) is None
        assert _get_deploy_lock(store, project_archived.id) is None
    finally:
        with store._pool.connection() as conn:
            conn.execute(
                "DELETE FROM deploy_locks WHERE project_id = ANY(%s)",
                ([project_done.id, project_running.id, project_archived.id],),
            )
        store.delete_project(project_done.id)
        store.delete_project(project_running.id)
        store.delete_project(project_archived.id)


def test_has_fresh_replacement_worker_true_for_fresh_other_pid_executor(store):
    host = f"host-{uuid.uuid4()}"
    asyncio.run(
        store.worker_heartbeat(
            f"w-{uuid.uuid4()}", host, 111, status="busy", role="executor", now=1_000.0
        )
    )
    assert store.has_fresh_replacement_worker(host, 222, max_age_seconds=60.0, now=1_030.0)


def test_has_fresh_replacement_worker_true_for_fresh_other_pid_supervisor(store):
    host = f"host-{uuid.uuid4()}"
    asyncio.run(
        store.worker_heartbeat(
            f"w-{uuid.uuid4()}", host, 111, status="leader", role="supervisor", now=1_000.0
        )
    )
    assert store.has_fresh_replacement_worker(host, 222, max_age_seconds=60.0, now=1_030.0)


def test_has_fresh_replacement_worker_false_for_own_pid_only(store):
    host = f"host-{uuid.uuid4()}"
    asyncio.run(
        store.worker_heartbeat(
            f"w-{uuid.uuid4()}", host, 111, status="busy", role="executor", now=1_000.0
        )
    )
    assert not store.has_fresh_replacement_worker(host, 111, max_age_seconds=60.0, now=1_030.0)


def test_has_fresh_replacement_worker_false_for_stale_heartbeat(store):
    host = f"host-{uuid.uuid4()}"
    asyncio.run(
        store.worker_heartbeat(
            f"w-{uuid.uuid4()}", host, 111, status="busy", role="executor", now=1_000.0
        )
    )
    assert not store.has_fresh_replacement_worker(host, 222, max_age_seconds=60.0, now=1_100.0)


def test_has_fresh_replacement_worker_false_for_different_host(store):
    host = f"host-{uuid.uuid4()}"
    other_host = f"host-{uuid.uuid4()}"
    asyncio.run(
        store.worker_heartbeat(
            f"w-{uuid.uuid4()}", host, 111, status="busy", role="executor", now=1_000.0
        )
    )
    assert not store.has_fresh_replacement_worker(
        other_host, 222, max_age_seconds=60.0, now=1_030.0
    )


def test_get_job_usage_sums_per_source_model_provider(store):
    project = store.create_project("Job Usage Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        job = store.create(idea="idea", repo_path=project.repo_path, chat_id=1)
        other_job = store.create(idea="other idea", repo_path=project.repo_path, chat_id=1)

        store.record_usage(
            "build",
            Usage(
                input_tokens=100,
                output_tokens=50,
                cache_creation_tokens=10,
                cache_read_tokens=5,
                cost_usd=0.01234,
                model="claude-sonnet-4-6",
                provider="claude",
            ),
            job_id=job.id,
        )
        store.record_usage(
            "build",
            Usage(
                input_tokens=200,
                output_tokens=25,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                cost_usd=0.02,
                model="claude-sonnet-4-6",
                provider="claude",
            ),
            job_id=job.id,
        )
        store.record_usage(
            "review",
            Usage(
                input_tokens=40,
                output_tokens=20,
                cost_usd=0.005,
                model="gpt-5",
                provider="codex",
            ),
            job_id=job.id,
        )
        store.record_usage(
            "build",
            Usage(input_tokens=999, output_tokens=999, cost_usd=9.99),
            job_id=other_job.id,
        )

        rows = store.get_job_usage(job.id)

        by_source = {r["source"]: r for r in rows}
        assert set(by_source) == {"build", "review"}

        build_row = by_source["build"]
        assert build_row["model"] == "claude-sonnet-4-6"
        assert build_row["provider"] == "claude"
        assert build_row["input_tokens"] == 300
        assert build_row["output_tokens"] == 75
        assert build_row["cache_creation_tokens"] == 10
        assert build_row["cache_read_tokens"] == 5
        assert build_row["cost_usd"] == round(0.01234 + 0.02, 4)
        assert build_row["runs"] == 2

        review_row = by_source["review"]
        assert review_row["model"] == "gpt-5"
        assert review_row["provider"] == "codex"
        assert review_row["input_tokens"] == 40
        assert review_row["output_tokens"] == 20
        assert review_row["cost_usd"] == 0.005
        assert review_row["runs"] == 1
    finally:
        store.delete_project(project.id)


def test_usage_defaults_and_aggregation_preserve_available_attribution():
    unattributed = Usage(input_tokens=2)
    attributed = Usage(output_tokens=3, model="gpt-5.6-sol", provider="codex")

    combined = unattributed + attributed

    assert unattributed.model == ""
    assert combined.model == "gpt-5.6-sol"
    assert combined.provider == "codex"


def test_record_usage_persists_model_and_provider(store):
    project = store.create_project("Usage Attribution", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        job = store.create(idea="idea", repo_path=project.repo_path, chat_id=1)
        store.record_usage(
            "build",
            Usage(input_tokens=10, model="gpt-5.6-sol", provider="codex"),
            job_id=job.id,
        )

        assert store.get_job_usage(job.id) == [
            {
                "source": "build",
                "model": "gpt-5.6-sol",
                "provider": "codex",
                "input_tokens": 10,
                "output_tokens": 0,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 0,
                "cost_usd": 0.0,
                "runs": 1,
            }
        ]
    finally:
        store.delete_project(project.id)


def test_get_job_usage_empty_for_job_with_no_usage(store):
    project = store.create_project("Job Usage Empty Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        job = store.create(idea="idea", repo_path=project.repo_path, chat_id=1)
        assert store.get_job_usage(job.id) == []
    finally:
        store.delete_project(project.id)


def test_create_environment_returns_environment_with_id(store):
    project = store.create_project("Env Create Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        env = store.create_environment(project.id, "staging", "staging")
        assert env.id > 0
        assert env.project_id == project.id
        assert env.name == "staging"
        assert env.kind == "staging"
        assert env.status == "active"
    finally:
        store.delete_project(project.id)


def test_create_environment_raises_value_error_for_unknown_project(store):
    with pytest.raises(ValueError):
        store.create_environment(999999999, "prod", "prod")


def test_get_environment_returns_none_for_unknown_id(store):
    assert store.get_environment(999999999) is None


def test_get_environment_returns_matching_environment(store):
    project = store.create_project("Env Get Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        created = store.create_environment(project.id, "dev", "dev")
        fetched = store.get_environment(created.id)
        assert fetched == created
    finally:
        store.delete_project(project.id)


def test_list_environments_returns_only_that_projects_environments(store):
    project_a = store.create_project("Env List Project A", f"/tmp/test-store-{uuid.uuid4()}")
    project_b = store.create_project("Env List Project B", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        store.create_environment(project_a.id, "dev", "dev")
        store.create_environment(project_a.id, "staging", "staging")
        store.create_environment(project_b.id, "dev", "dev")

        envs_a = store.list_environments(project_a.id)
        assert {e.name for e in envs_a} == {"dev", "staging"}
        assert all(e.project_id == project_a.id for e in envs_a)
    finally:
        store.delete_project(project_a.id)
        store.delete_project(project_b.id)


def test_get_or_create_default_environment_creates_prod_on_first_call(store):
    project = store.create_project("Env Default Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        env = store.get_or_create_default_environment(project.id)
        assert env.project_id == project.id
        assert env.name == "prod"
        assert env.kind == "prod"
    finally:
        store.delete_project(project.id)


def test_get_or_create_default_environment_is_idempotent(store):
    project = store.create_project("Env Idempotent Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        first = store.get_or_create_default_environment(project.id)
        second = store.get_or_create_default_environment(project.id)
        assert first.id == second.id
        assert len(store.list_environments(project.id)) == 1
    finally:
        store.delete_project(project.id)


def test_create_environment_defaults_auto_deploy_false(store):
    project = store.create_project(
        "Env Auto Deploy Default Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        env = store.create_environment(project.id, "staging", "staging")
        assert env.auto_deploy is False
    finally:
        store.delete_project(project.id)


def test_get_or_create_default_environment_sets_auto_deploy_true(store):
    project = store.create_project(
        "Env Auto Deploy Default True Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        env = store.get_or_create_default_environment(project.id)
        assert env.auto_deploy is True
    finally:
        store.delete_project(project.id)


def test_seed_migration_backfills_auto_deploy_true_for_existing_prod_env(store):
    project = store.create_project(
        "Env Auto Deploy Backfill Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        # The schema's backfill migration already ran during store setup, so
        # any 'prod'-named environment created via the default-env path
        # reflects it — mirroring how
        # test_seed_migration_backfills_exactly_one_prod_environment_per_project
        # exercises the sibling seed-insert migration.
        env = store.get_or_create_default_environment(project.id)
        assert env.name == "prod"
        assert env.auto_deploy is True
    finally:
        store.delete_project(project.id)


def test_seed_migration_backfills_exactly_one_prod_environment_per_project(store):
    project = store.create_project("Env Seed Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        # The store fixture's schema init already ran the seed INSERT once for
        # this newly-created project (created after schema setup, so it has no
        # backfilled row yet); re-running the same idempotent statement must
        # not create a duplicate 'prod' row.
        with store._pool.connection() as conn:
            conn.execute(
                "INSERT INTO environments (project_id, name, kind, status, created_at, updated_at) "
                "SELECT id, 'prod', 'prod', 'active', created_at, created_at FROM projects "
                "WHERE id = %s ON CONFLICT (project_id, name) DO NOTHING",
                (project.id,),
            )
            conn.execute(
                "INSERT INTO environments (project_id, name, kind, status, created_at, updated_at) "
                "SELECT id, 'prod', 'prod', 'active', created_at, created_at FROM projects "
                "WHERE id = %s ON CONFLICT (project_id, name) DO NOTHING",
                (project.id,),
            )
        assert len(store.list_environments(project.id)) == 1
    finally:
        store.delete_project(project.id)


def test_deploys_environment_id_column_accepts_null(store):
    project = store.create_project(
        "Deploys Env Nullable Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        from hyqs.pipeline.models import _now

        with store._pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO deploys(project_id, deployed_commit, deployed_at, trigger) "
                "VALUES (%s, %s, %s, %s) RETURNING environment_id",
                (project.id, "sha-null-env", _now(), "unknown"),
            ).fetchone()
        assert row["environment_id"] is None
    finally:
        store.delete_project(project.id)


def test_deploy_locks_environment_id_column_accepts_null(store):
    from hyqs.pipeline.models import _now

    project_id = 999_888_777
    try:
        with store._pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO deploy_locks(project_id, owner, acquired_at) "
                "VALUES (%s, %s, %s) RETURNING environment_id",
                (project_id, "job-1", _now()),
            ).fetchone()
        assert row["environment_id"] is None
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM deploy_locks WHERE project_id = %s", (project_id,))


def test_record_deploy_persists_image_ref_digest_and_signed(store):
    project = store.create_project(
        "Deploys Artifact Identity Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        store.record_deploy(
            project.id,
            "sha-artifact",
            None,
            "pipeline_job",
            image_ref="registry.example/proj/app:latest",
            image_digest="sha256:deadbeef",
            signed=True,
        )
        with store._pool.connection() as conn:
            row = conn.execute(
                "SELECT image_ref, image_digest, signed FROM deploys "
                "WHERE project_id=%s AND deployed_commit='sha-artifact'",
                (project.id,),
            ).fetchone()
        assert row["image_ref"] == "registry.example/proj/app:latest"
        assert row["image_digest"] == "sha256:deadbeef"
        assert row["signed"] is True
    finally:
        store.delete_project(project.id)


def test_record_deploy_defaults_image_fields_to_null_and_unsigned(store):
    project = store.create_project(
        "Deploys Artifact Identity Default Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        store.record_deploy(project.id, "sha-no-artifact", None, "pipeline_job")
        with store._pool.connection() as conn:
            row = conn.execute(
                "SELECT image_ref, image_digest, signed FROM deploys "
                "WHERE project_id=%s AND deployed_commit='sha-no-artifact'",
                (project.id,),
            ).fetchone()
        assert row["image_ref"] is None
        assert row["image_digest"] is None
        assert row["signed"] is False
    finally:
        store.delete_project(project.id)


def test_deploys_backfill_maps_existing_rows_to_prod_environment(store):
    from hyqs.pipeline.models import _now
    from hyqs.pipeline.store import _SCHEMA

    backfill_sql = next(s for s in _SCHEMA if "UPDATE deploys d SET environment_id" in s)

    project = store.create_project("Deploys Backfill Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        prod_env = store.get_or_create_default_environment(project.id)
        with store._pool.connection() as conn:
            conn.execute(
                "INSERT INTO deploys(project_id, deployed_commit, deployed_at, trigger) "
                "VALUES (%s, %s, %s, %s)",
                (project.id, "sha-backfill", _now(), "unknown"),
            )

            conn.execute(backfill_sql)
            conn.execute(backfill_sql)  # idempotent: a second run is a no-op

            row = conn.execute(
                "SELECT environment_id FROM deploys WHERE project_id=%s AND deployed_commit='sha-backfill'",
                (project.id,),
            ).fetchone()
        assert row["environment_id"] == prod_env.id
    finally:
        store.delete_project(project.id)


def test_deploy_locks_backfill_maps_existing_rows_to_prod_environment(store):
    from hyqs.pipeline.models import _now
    from hyqs.pipeline.store import _SCHEMA

    backfill_sql = next(s for s in _SCHEMA if "UPDATE deploy_locks l SET environment_id" in s)

    project = store.create_project(
        "Deploy Locks Backfill Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        prod_env = store.get_or_create_default_environment(project.id)
        with store._pool.connection() as conn:
            conn.execute(
                "INSERT INTO deploy_locks(project_id, owner, acquired_at) VALUES (%s, %s, %s)",
                (project.id, "job-backfill", _now()),
            )

            conn.execute(backfill_sql)
            conn.execute(backfill_sql)  # idempotent: a second run is a no-op

            row = conn.execute(
                "SELECT environment_id FROM deploy_locks WHERE project_id=%s",
                (project.id,),
            ).fetchone()
        assert row["environment_id"] == prod_env.id
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM deploy_locks WHERE project_id = %s", (project.id,))
        store.delete_project(project.id)


def test_acquire_deploy_lock_stamps_environment_id_on_new_lock(store):
    project = store.create_project(
        "Deploy Lock Env Stamp Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        prod_env = store.get_or_create_default_environment(project.id)

        acquired = asyncio.run(store.acquire_deploy_lock(project.id, "job-1", timeout_s=5.0))

        assert acquired is True
        row = _get_deploy_lock(store, project.id)
        assert row["environment_id"] == prod_env.id
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM deploy_locks WHERE project_id = %s", (project.id,))
        store.delete_project(project.id)


def test_two_environments_under_one_project_hold_independent_deploy_locks(store):
    project = store.create_project(
        "Deploy Lock Multi-Env Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        env_a = store.create_environment(project.id, "staging", "staging")
        env_b = store.create_environment(project.id, "dev", "dev")

        acquired_a = asyncio.run(
            store.acquire_deploy_lock(project.id, "job-a", timeout_s=5.0, environment_id=env_a.id)
        )
        acquired_b = asyncio.run(
            store.acquire_deploy_lock(project.id, "job-b", timeout_s=5.0, environment_id=env_b.id)
        )

        assert acquired_a is True
        assert acquired_b is True

        # A second acquire against the already-held env_a lock times out —
        # env_b's lock did not block it, but env_a's own lock still does.
        acquired_a_again = asyncio.run(
            store.acquire_deploy_lock(project.id, "job-a2", timeout_s=0.0, environment_id=env_a.id)
        )
        assert acquired_a_again is False

        store.release_deploy_lock(project.id, "job-a", environment_id=env_a.id)

        with store._pool.connection() as conn:
            row_a = conn.execute(
                "SELECT owner FROM deploy_locks WHERE environment_id = %s", (env_a.id,)
            ).fetchone()
            row_b = conn.execute(
                "SELECT owner FROM deploy_locks WHERE environment_id = %s", (env_b.id,)
            ).fetchone()
        assert row_a is None
        assert row_b is not None and row_b["owner"] == "job-b"
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM deploy_locks WHERE project_id = %s", (project.id,))
        store.delete_project(project.id)


def test_get_environment_config_falls_back_to_project_deploy_config(store):
    project = store.create_project("Env Config Fallback Project", f"/tmp/test-store-{uuid.uuid4()}")
    store.update_project(project.id, deploy_config=json.dumps({"port": 8080}))
    env = store.get_or_create_default_environment(project.id)

    assert store.get_environment_config(env.id) == {"port": 8080}


def test_set_environment_config_persists_and_overrides_fallback(store):
    project = store.create_project("Env Config Override Project", f"/tmp/test-store-{uuid.uuid4()}")
    store.update_project(project.id, deploy_config=json.dumps({"port": 8080}))
    env = store.get_or_create_default_environment(project.id)

    updated = store.set_environment_config(env.id, {"port": 9090, "mem": "512m"})

    assert updated.config == json.dumps({"port": 9090, "mem": "512m"})
    assert store.get_environment_config(env.id) == {"port": 9090, "mem": "512m"}


def test_get_environment_config_raises_for_unknown_environment(store):
    with pytest.raises(ValueError):
        store.get_environment_config(999999999)


def test_set_environment_config_raises_for_unknown_environment(store):
    with pytest.raises(ValueError):
        store.set_environment_config(999999999, {"port": 1})


def test_create_host_returns_host_with_id(store):
    host = store.create_host(f"host-{uuid.uuid4()}", public_key="ssh-ed25519 AAAA")
    assert host.id > 0
    assert host.status == "active"
    assert host.public_key == "ssh-ed25519 AAAA"


def test_create_host_allows_null_public_key(store):
    host = store.create_host(f"host-{uuid.uuid4()}")
    assert host.public_key == ""


def test_get_host_returns_none_for_unknown_id(store):
    assert store.get_host(999999999) is None


def test_get_host_by_name_returns_matching_host(store):
    name = f"host-{uuid.uuid4()}"
    created = store.create_host(name)
    fetched = store.get_host_by_name(name)
    assert fetched == created


def test_get_host_by_public_key_returns_matching_host(store):
    public_key = f"ssh-ed25519 {uuid.uuid4()}"
    created = store.create_host(f"host-{uuid.uuid4()}", public_key=public_key)
    fetched = store.get_host_by_public_key(public_key)
    assert fetched == created


def test_list_hosts_returns_all_hosts(store):
    name_a = f"host-{uuid.uuid4()}"
    name_b = f"host-{uuid.uuid4()}"
    store.create_host(name_a)
    store.create_host(name_b)

    names = {h.name for h in store.list_hosts()}
    assert name_a in names
    assert name_b in names


def test_set_environment_host_updates_environment(store):
    project = store.create_project("Env Host Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        env = store.get_or_create_default_environment(project.id)
        host = store.create_host(f"host-{uuid.uuid4()}")

        updated = store.set_environment_host(env.id, host.id)

        assert updated.host_id == host.id
    finally:
        store.delete_project(project.id)


def test_set_environment_host_can_reset_to_null(store):
    project = store.create_project("Env Host Reset Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        env = store.get_or_create_default_environment(project.id)
        host = store.create_host(f"host-{uuid.uuid4()}")
        store.set_environment_host(env.id, host.id)

        updated = store.set_environment_host(env.id, None)

        assert updated.host_id is None
    finally:
        store.delete_project(project.id)


def test_set_environment_auto_deploy_updates_environment(store):
    project = store.create_project(
        "Env Auto Deploy Setter Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        env = store.create_environment(project.id, "staging", "staging")

        turned_on = store.set_environment_auto_deploy(env.id, True)
        assert turned_on.auto_deploy is True
        assert store.get_environment(env.id).auto_deploy is True

        turned_off = store.set_environment_auto_deploy(env.id, False)
        assert turned_off.auto_deploy is False
        assert store.get_environment(env.id).auto_deploy is False
    finally:
        store.delete_project(project.id)


def test_set_environment_auto_deploy_raises_for_unknown_environment(store):
    with pytest.raises(ValueError):
        store.set_environment_auto_deploy(999999999, True)


def test_list_auto_deploy_environments_returns_only_true_envs(store):
    project = store.create_project(
        "Env Auto Deploy Lister Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        auto_env = store.get_or_create_default_environment(project.id)
        store.create_environment(project.id, "staging", "staging")

        auto_deploy_envs = store.list_auto_deploy_environments()

        assert auto_env.id in {e.id for e in auto_deploy_envs}
        assert all(e.auto_deploy for e in auto_deploy_envs)
    finally:
        store.delete_project(project.id)


def test_create_release_returns_release_with_id(store):
    project = store.create_project("Release Create Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        release = store.create_release(
            project.id,
            "abc123",
            "registry.example.com/proj/app",
            "sha256:deadbeef",
            signature_ref="registry.example.com/proj/app@sha256:deadbeef",
            built_by="job-1",
        )
        assert release.id > 0
        assert release.project_id == project.id
        assert release.source_commit == "abc123"
        assert release.image_ref == "registry.example.com/proj/app"
        assert release.image_digest == "sha256:deadbeef"
        assert release.signature_ref == "registry.example.com/proj/app@sha256:deadbeef"
        assert release.built_by == "job-1"
    finally:
        store.delete_project(project.id)


def test_create_release_raises_value_error_for_unknown_project(store):
    with pytest.raises(ValueError):
        store.create_release(999999999, "abc123", "registry.example.com/proj/app", "sha256:x")


def test_get_release_returns_none_for_unknown(store):
    assert store.get_release(999999999) is None


def test_get_release_by_digest_returns_none_when_absent(store):
    project = store.create_project(
        "Release Digest Absent Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        assert store.get_release_by_digest(project.id, "sha256:nope") is None
    finally:
        store.delete_project(project.id)


def test_get_release_by_digest_finds_matching_release(store):
    project = store.create_project(
        "Release Digest Found Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        created = store.create_release(
            project.id, "abc123", "registry.example.com/proj/app", "sha256:deadbeef"
        )
        fetched = store.get_release_by_digest(project.id, "sha256:deadbeef")
        assert fetched == created
    finally:
        store.delete_project(project.id)


def test_list_releases_orders_by_built_at(store):
    project = store.create_project("Release List Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        first = store.create_release(
            project.id, "commit-1", "registry.example.com/proj/app", "sha256:one"
        )
        second = store.create_release(
            project.id, "commit-2", "registry.example.com/proj/app", "sha256:two"
        )
        releases = store.list_releases(project.id)
        assert [r.id for r in releases] == [second.id, first.id]
    finally:
        store.delete_project(project.id)


def test_set_environment_current_release_updates_environment(store):
    project = store.create_project("Env Release Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        env = store.get_or_create_default_environment(project.id)
        release = store.create_release(
            project.id, "abc123", "registry.example.com/proj/app", "sha256:deadbeef"
        )

        updated = store.set_environment_current_release(env.id, release.id)

        assert updated.current_release_id == release.id
    finally:
        store.delete_project(project.id)


def test_set_environment_current_release_can_reset_to_null(store):
    project = store.create_project("Env Release Reset Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        env = store.get_or_create_default_environment(project.id)
        release = store.create_release(
            project.id, "abc123", "registry.example.com/proj/app", "sha256:deadbeef"
        )
        store.set_environment_current_release(env.id, release.id)

        updated = store.set_environment_current_release(env.id, None)

        assert updated.current_release_id is None
    finally:
        store.delete_project(project.id)


def test_set_environment_current_release_raises_for_unknown_release(store):
    project = store.create_project("Env Release Unknown Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        env = store.get_or_create_default_environment(project.id)
        with pytest.raises(ValueError):
            store.set_environment_current_release(env.id, 999999999)
    finally:
        store.delete_project(project.id)


@pytest.fixture
def promo_setup(store):
    """A project + two environments + a release, cleaned up via project cascade."""
    project = store.create_project("Promotion Project", f"/tmp/test-store-{uuid.uuid4()}")
    source_env = store.get_or_create_default_environment(project.id)
    target_env = store.create_environment(project.id, "staging", "staging")
    release = store.create_release(
        project.id, "abc123", "registry.example.com/proj/app", "sha256:deadbeef"
    )
    yield project, source_env, target_env, release
    store.delete_project(project.id)


def test_create_promotion_returns_promotion_with_id_and_state(store, promo_setup):
    project, source_env, target_env, release = promo_setup
    promotion = store.create_promotion(
        project.id,
        release.id,
        target_env.id,
        PromotionKind.PROMOTE,
        source_env_id=source_env.id,
        requested_by="job-1",
    )
    assert promotion.id > 0
    assert promotion.state == PromotionState.DISPATCHED
    assert promotion.kind == PromotionKind.PROMOTE
    assert promotion.project_id == project.id
    assert promotion.release_id == release.id
    assert promotion.target_env_id == target_env.id
    assert promotion.source_env_id == source_env.id
    assert promotion.requested_by == "job-1"


def test_create_promotion_offer_starts_available(store, promo_setup):
    project, _source_env, target_env, release = promo_setup
    promotion = store.create_promotion(project.id, release.id, target_env.id, PromotionKind.OFFER)
    assert promotion.state == PromotionState.AVAILABLE


def test_create_promotion_raises_value_error_for_unknown_project(store, promo_setup):
    _project, _source_env, target_env, release = promo_setup
    with pytest.raises(ValueError):
        store.create_promotion(999999999, release.id, target_env.id, PromotionKind.PROMOTE)


def test_create_promotion_raises_value_error_for_unknown_release(store, promo_setup):
    project, _source_env, target_env, _release = promo_setup
    with pytest.raises(ValueError):
        store.create_promotion(project.id, 999999999, target_env.id, PromotionKind.PROMOTE)


def test_create_promotion_raises_value_error_for_unknown_target_env(store, promo_setup):
    project, _source_env, _target_env, release = promo_setup
    with pytest.raises(ValueError):
        store.create_promotion(project.id, release.id, 999999999, PromotionKind.PROMOTE)


def test_create_promotion_is_noop_when_release_already_current(store, promo_setup):
    project, _source_env, target_env, release = promo_setup
    store.set_environment_current_release(target_env.id, release.id)

    result = store.create_promotion(project.id, release.id, target_env.id, PromotionKind.PROMOTE)

    assert result is None
    assert store.list_promotions(target_env.id) == []


def test_create_promotion_not_noop_when_release_differs_from_current(store, promo_setup):
    project, _source_env, target_env, release = promo_setup
    other_release = store.create_release(
        project.id, "def456", "registry.example.com/proj/app", "sha256:otherdigest"
    )
    store.set_environment_current_release(target_env.id, other_release.id)

    result = store.create_promotion(project.id, release.id, target_env.id, PromotionKind.PROMOTE)

    assert result is not None
    assert result.release_id == release.id


def test_get_promotion_returns_none_for_unknown(store):
    assert store.get_promotion(999999999) is None


def test_get_promotion_returns_created_row(store, promo_setup):
    project, _source_env, target_env, release = promo_setup
    created = store.create_promotion(project.id, release.id, target_env.id, PromotionKind.PROMOTE)
    assert store.get_promotion(created.id) == created


def test_list_promotions_scopes_to_target_env(store, promo_setup):
    project, _source_env, target_env, release = promo_setup
    other_env = store.create_environment(project.id, "qa", "dev")

    in_scope = store.create_promotion(project.id, release.id, target_env.id, PromotionKind.PROMOTE)
    store.create_promotion(project.id, release.id, other_env.id, PromotionKind.PROMOTE)

    results = store.list_promotions(target_env.id)

    assert [p.id for p in results] == [in_scope.id]


def test_list_promotions_orders_newest_first(store, promo_setup):
    project, _source_env, target_env, release = promo_setup
    other_release = store.create_release(
        project.id, "def456", "registry.example.com/proj/app", "sha256:otherdigest2"
    )
    first = store.create_promotion(project.id, release.id, target_env.id, PromotionKind.PROMOTE)
    second = store.create_promotion(
        project.id, other_release.id, target_env.id, PromotionKind.PROMOTE
    )

    results = store.list_promotions(target_env.id)

    assert [p.id for p in results] == [second.id, first.id]


def test_list_promotions_for_project_scopes_to_project(store, promo_setup):
    project, _source_env, target_env, release = promo_setup
    other_project = store.create_project(
        "Other Promotion Project", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        other_env = store.get_or_create_default_environment(other_project.id)
        other_release = store.create_release(
            other_project.id, "xyz", "registry.example.com/other/app", "sha256:otherproj"
        )
        in_scope = store.create_promotion(
            project.id, release.id, target_env.id, PromotionKind.PROMOTE
        )
        store.create_promotion(
            other_project.id, other_release.id, other_env.id, PromotionKind.PROMOTE
        )

        results = store.list_promotions_for_project(project.id)

        assert [p.id for p in results] == [in_scope.id]
    finally:
        store.delete_project(other_project.id)


def test_transition_promotion_moves_through_legal_chain(store, promo_setup):
    project, _source_env, target_env, release = promo_setup
    promotion = store.create_promotion(project.id, release.id, target_env.id, PromotionKind.PROMOTE)

    deploying = store.transition_promotion(promotion.id, PromotionState.DEPLOYING)
    assert deploying.state == PromotionState.DEPLOYING

    deployed = store.transition_promotion(promotion.id, PromotionState.DEPLOYED, applied_by="job-1")
    assert deployed.state == PromotionState.DEPLOYED
    assert deployed.applied_by == "job-1"
    assert deployed.applied_at is not None


def test_transition_promotion_rejects_illegal_transition_and_leaves_state(store, promo_setup):
    project, _source_env, target_env, release = promo_setup
    promotion = store.create_promotion(project.id, release.id, target_env.id, PromotionKind.PROMOTE)
    store.transition_promotion(promotion.id, PromotionState.DEPLOYING)
    store.transition_promotion(promotion.id, PromotionState.DEPLOYED)

    with pytest.raises(ValueError):
        store.transition_promotion(promotion.id, PromotionState.DEPLOYING)

    unchanged = store.get_promotion(promotion.id)
    assert unchanged.state == PromotionState.DEPLOYED


def test_transition_promotion_rejects_declined_to_deploying(store, promo_setup):
    project, _source_env, target_env, release = promo_setup
    promotion = store.create_promotion(project.id, release.id, target_env.id, PromotionKind.OFFER)
    store.transition_promotion(promotion.id, PromotionState.DECLINED)

    with pytest.raises(ValueError):
        store.transition_promotion(promotion.id, PromotionState.DEPLOYING)

    unchanged = store.get_promotion(promotion.id)
    assert unchanged.state == PromotionState.DECLINED


def test_transition_promotion_raises_for_unknown_promotion(store):
    with pytest.raises(ValueError):
        store.transition_promotion(999999999, PromotionState.DEPLOYING)


def test_supersede_prior_promotions_marks_non_terminal_and_skips_terminal(store, promo_setup):
    project, _source_env, target_env, release = promo_setup
    other_release = store.create_release(
        project.id, "def456", "registry.example.com/proj/app", "sha256:supersede1"
    )
    terminal_release = store.create_release(
        project.id, "ghi789", "registry.example.com/proj/app", "sha256:supersede2"
    )

    non_terminal = store.create_promotion(
        project.id, release.id, target_env.id, PromotionKind.PROMOTE
    )
    terminal = store.create_promotion(
        project.id, terminal_release.id, target_env.id, PromotionKind.PROMOTE
    )
    store.transition_promotion(terminal.id, PromotionState.DEPLOYING)
    store.transition_promotion(terminal.id, PromotionState.DEPLOYED)
    newest = store.create_promotion(
        project.id, other_release.id, target_env.id, PromotionKind.PROMOTE
    )

    superseded = store.supersede_prior_promotions(target_env.id, exclude_promotion_id=newest.id)

    assert [p.id for p in superseded] == [non_terminal.id]
    assert store.get_promotion(non_terminal.id).state == PromotionState.SUPERSEDED
    assert store.get_promotion(terminal.id).state == PromotionState.DEPLOYED
    assert store.get_promotion(newest.id).state == PromotionState.DISPATCHED


def test_promote_release_enqueues_deploy_job_linked_to_promotion(store, promo_setup):
    project, _source_env, target_env, release = promo_setup

    promotion = store.promote_release(project.id, release.id, target_env.id, requested_by="job-1")

    assert promotion is not None
    assert promotion.deploy_job_id is not None
    job = store.get(promotion.deploy_job_id)
    assert job.stage == Stage.DEPLOY
    assert job.source_meta["environment_id"] == target_env.id
    assert job.source_meta["release_id"] == release.id
    assert job.source_meta["promotion_id"] == promotion.id


def test_promote_release_supersedes_prior_and_dispatches_new(store, promo_setup):
    project, _source_env, target_env, release = promo_setup
    other_release = store.create_release(
        project.id, "def456", "registry.example.com/proj/app", "sha256:promoteother"
    )

    first = store.promote_release(project.id, release.id, target_env.id)
    second = store.promote_release(project.id, other_release.id, target_env.id)

    assert second is not None
    assert second.id != first.id
    assert store.get_promotion(first.id).state == PromotionState.SUPERSEDED
    assert second.state == PromotionState.DISPATCHED
    assert second.deploy_job_id is not None
    assert second.deploy_job_id != first.deploy_job_id


def test_promote_release_is_noop_when_release_already_current(store, promo_setup):
    project, _source_env, target_env, release = promo_setup
    store.set_environment_current_release(target_env.id, release.id)

    result = store.promote_release(project.id, release.id, target_env.id)

    assert result is None
    assert store.list_promotions_for_project(project.id) == []


def test_promotion_for_deploy_job_returns_none_for_unlinked(store):
    assert store.promotion_for_deploy_job(999999999) is None


def test_promotion_for_deploy_job_returns_linked_promotion(store, promo_setup):
    project, _source_env, target_env, release = promo_setup
    promotion = store.promote_release(project.id, release.id, target_env.id)

    assert store.promotion_for_deploy_job(promotion.deploy_job_id) == promotion


def test_advance_promotion_deploying_noops_for_unlinked_job(store):
    store.advance_promotion_deploying(999999999)  # must not raise


def test_advance_promotion_deploying_transitions_linked_promotion(store, promo_setup):
    project, _source_env, target_env, release = promo_setup
    promotion = store.promote_release(project.id, release.id, target_env.id)

    store.advance_promotion_deploying(promotion.deploy_job_id)

    assert store.get_promotion(promotion.id).state == PromotionState.DEPLOYING


def test_advance_promotion_deployed_noops_for_unlinked_job(store):
    store.advance_promotion_deployed(999999999, 1, 1)  # must not raise


def test_advance_promotion_deployed_noops_for_terminal_promotion(store, promo_setup):
    project, _source_env, target_env, release = promo_setup
    promotion = store.promote_release(project.id, release.id, target_env.id)
    store.transition_promotion(promotion.id, PromotionState.DEPLOYING)
    store.transition_promotion(promotion.id, PromotionState.FAILED)

    store.advance_promotion_deployed(promotion.deploy_job_id, target_env.id, release.id)

    assert store.get_promotion(promotion.id).state == PromotionState.FAILED
    assert store.get_environment(target_env.id).current_release_id is None


def test_advance_promotion_deployed_transitions_and_sets_current_release(store, promo_setup):
    project, _source_env, target_env, release = promo_setup
    promotion = store.promote_release(project.id, release.id, target_env.id)
    store.transition_promotion(promotion.id, PromotionState.DEPLOYING)

    store.advance_promotion_deployed(promotion.deploy_job_id, target_env.id, release.id)

    assert store.get_promotion(promotion.id).state == PromotionState.DEPLOYED
    assert store.get_environment(target_env.id).current_release_id == release.id


def test_fail_promotion_noops_for_unlinked_job(store):
    store.fail_promotion(999999999)  # must not raise


def test_fail_promotion_noops_for_terminal_promotion(store, promo_setup):
    project, _source_env, target_env, release = promo_setup
    promotion = store.promote_release(project.id, release.id, target_env.id)
    store.transition_promotion(promotion.id, PromotionState.DEPLOYING)
    store.transition_promotion(promotion.id, PromotionState.DEPLOYED)

    store.fail_promotion(promotion.deploy_job_id)

    assert store.get_promotion(promotion.id).state == PromotionState.DEPLOYED


def test_fail_promotion_transitions_linked_promotion(store, promo_setup):
    project, _source_env, target_env, release = promo_setup
    promotion = store.promote_release(project.id, release.id, target_env.id)
    store.transition_promotion(promotion.id, PromotionState.DEPLOYING)

    store.fail_promotion(promotion.deploy_job_id)

    assert store.get_promotion(promotion.id).state == PromotionState.FAILED


# --- offer_release / get_available_offer / list_environments_by_host --------


def test_offer_release_creates_available_promotion_with_no_deploy_job(store, promo_setup):
    project, _source_env, target_env, release = promo_setup

    promotion = store.offer_release(project.id, release.id, target_env.id, requested_by="client-op")

    assert promotion is not None
    assert promotion.kind == PromotionKind.OFFER
    assert promotion.state == PromotionState.AVAILABLE
    assert promotion.deploy_job_id is None
    assert promotion.requested_by == "client-op"


def test_offer_release_is_noop_when_release_already_current(store, promo_setup):
    project, _source_env, target_env, release = promo_setup
    store.set_environment_current_release(target_env.id, release.id)

    result = store.offer_release(project.id, release.id, target_env.id)

    assert result is None
    assert store.list_promotions_for_project(project.id) == []


def test_offer_release_supersedes_prior_non_terminal_promotion(store, promo_setup):
    project, _source_env, target_env, release = promo_setup
    other_release = store.create_release(
        project.id, "def456", "registry.example.com/proj/app", "sha256:offerother"
    )
    first = store.offer_release(project.id, release.id, target_env.id)

    second = store.offer_release(project.id, other_release.id, target_env.id)

    assert second is not None
    assert second.id != first.id
    assert store.get_promotion(first.id).state == PromotionState.SUPERSEDED
    assert second.state == PromotionState.AVAILABLE


def test_offer_release_raises_value_error_for_unknown_target_env(store, promo_setup):
    project, _source_env, _target_env, release = promo_setup
    with pytest.raises(ValueError):
        store.offer_release(project.id, release.id, 999999999)


def test_get_available_offer_returns_none_when_none_exists(store, promo_setup):
    _project, _source_env, target_env, _release = promo_setup
    assert store.get_available_offer(target_env.id) is None


def test_get_available_offer_returns_the_available_offer(store, promo_setup):
    project, _source_env, target_env, release = promo_setup
    offer = store.offer_release(project.id, release.id, target_env.id)

    assert store.get_available_offer(target_env.id) == offer


def test_get_available_offer_returns_none_after_declined(store, promo_setup):
    project, _source_env, target_env, release = promo_setup
    offer = store.offer_release(project.id, release.id, target_env.id)
    store.transition_promotion(offer.id, PromotionState.DECLINED)

    assert store.get_available_offer(target_env.id) is None


def test_get_available_offer_returns_none_after_deployed(store, promo_setup):
    project, _source_env, target_env, release = promo_setup
    offer = store.offer_release(project.id, release.id, target_env.id)
    store.transition_promotion(offer.id, PromotionState.DEPLOYING)
    store.transition_promotion(offer.id, PromotionState.DEPLOYED)

    assert store.get_available_offer(target_env.id) is None


def test_get_available_offer_returns_none_after_superseded(store, promo_setup):
    project, _source_env, target_env, release = promo_setup
    offer = store.offer_release(project.id, release.id, target_env.id)
    other_release = store.create_release(
        project.id, "def456", "registry.example.com/proj/app", "sha256:offersuperseded"
    )
    newest = store.offer_release(project.id, other_release.id, target_env.id)

    assert store.get_promotion(offer.id).state == PromotionState.SUPERSEDED
    # supersession replaces the available offer with the newer one, rather
    # than leaving the environment with no available offer at all
    assert store.get_available_offer(target_env.id) == newest


def test_list_environments_by_host_returns_only_pinned_environments(store, promo_setup):
    project, _source_env, target_env, _release = promo_setup
    host = store.create_host(f"host-{uuid.uuid4()}")
    store.set_environment_host(target_env.id, host.id)

    results = store.list_environments_by_host(host.id)

    assert [e.id for e in results] == [target_env.id]


def test_list_environments_by_host_empty_for_unused_host(store):
    host = store.create_host(f"host-{uuid.uuid4()}")
    assert store.list_environments_by_host(host.id) == []


# --- environment members (environment-scoped RBAC grants) --------------------


def test_add_environment_member_round_trips_via_get_and_list(store):
    project = store.create_project("Env Member Project", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        env = store.create_environment(project.id, "client", "dev")
        user = store.create_user(f"env-member-{uuid.uuid4()}@example.com", "pw")

        added = store.add_environment_member(env.id, str(user.id), "client_approver")

        fetched = store.get_environment_member(env.id, str(user.id))
        assert fetched == added
        assert [m.id for m in store.list_environment_members(env.id)] == [added.id]
    finally:
        store.delete_project(project.id)


def test_get_environment_member_returns_none_for_unrelated_pair(store):
    project = store.create_project("Env Member Project B", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        env = store.create_environment(project.id, "client", "dev")
        user = store.create_user(f"env-member-unrelated-{uuid.uuid4()}@example.com", "pw")

        assert store.get_environment_member(env.id, str(user.id)) is None
    finally:
        store.delete_project(project.id)


def test_get_role_permissions_includes_new_environment_scoped_roles(store):
    perms = store.get_role_permissions()

    assert perms["release_manager"] == ["deploy.offer", "deploy.promote", "deploy.view"]
    assert perms["prod_promoter"] == [
        "deploy.offer",
        "deploy.promote",
        "deploy.promote_prod",
        "deploy.view",
    ]
    assert perms["client_approver"] == ["deploy.apply", "deploy.view"]
    assert "deploy.promote_prod" in perms["platform_admin"]
    assert "deploy.apply" in perms["platform_admin"]
    assert "deploy.view" in perms["platform_admin"]
    assert "deploy.view" in perms["viewer"]


def test_get_role_permissions_automation_client_is_contributor_minus_archive(store):
    perms = store.get_role_permissions()

    assert perms["automation_client"] == [
        "cancel_job",
        "edit_job_deps",
        "queue_job",
        "resolve_job",
        "retry_job",
    ]
    excluded = {
        "archive_job",
        "edit_project",
        "manage_webhooks",
        "manage_api_tokens",
        "deploy.promote",
        "deploy.offer",
        "manage_members",
        "delete_project",
    }
    assert excluded.isdisjoint(perms["automation_client"])


def test_add_project_member_accepts_automation_client_role(store):
    user = store.create_user(f"automation-client-{uuid.uuid4()}@example.com", "pw")
    project = store.create_project("Automation Client Project", f"/tmp/test-store-{uuid.uuid4()}")

    member = store.add_project_member(project.id, str(user.id), "automation_client")

    assert member.role == "automation_client"


def test_has_alembic_merge_fix_job_false_when_none_exists(store):
    project = store.create_project(f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}")
    failed = store.create(idea="deploy me", repo_path=project.repo_path, chat_id=1)

    assert store.has_alembic_merge_fix_job(failed.id) is False


def test_has_alembic_merge_fix_job_true_after_fix_job_created(store):
    project = store.create_project(f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}")
    failed = store.create(idea="deploy me", repo_path=project.repo_path, chat_id=1)
    store.create(
        idea="[alembic-merge-fix] write the merge revision",
        repo_path=project.repo_path,
        chat_id=1,
        source=JobSource.SUPERVISOR,
        source_meta={"alembic_merge_fix_for": failed.id},
    )

    assert store.has_alembic_merge_fix_job(failed.id) is True


def test_count_alembic_merge_fix_jobs_counts_only_this_project(store):
    project_a = store.create_project(
        f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}"
    )
    project_b = store.create_project(
        f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}"
    )
    failed_a = store.create(idea="deploy me", repo_path=project_a.repo_path, chat_id=1)
    store.create(
        idea="[alembic-merge-fix] write the merge revision",
        repo_path=project_a.repo_path,
        chat_id=1,
        source=JobSource.SUPERVISOR,
        source_meta={"alembic_merge_fix_for": failed_a.id},
    )
    store.create(
        idea="unrelated job",
        repo_path=project_b.repo_path,
        chat_id=1,
        source=JobSource.SUPERVISOR,
        source_meta={"deploy_fix_for": 999},
    )

    assert store.count_alembic_merge_fix_jobs(project_a.id) == 1
    assert store.count_alembic_merge_fix_jobs(project_b.id) == 0


def _insert_usage_on_day(
    store, job_id: int, day: datetime, *, cost_usd: float, tokens: int
) -> None:
    with store._pool.connection() as conn:
        conn.execute(
            "INSERT INTO usage(job_id, source, input_tokens, output_tokens, "
            "cache_creation_tokens, cache_read_tokens, cost_usd, created_at) "
            "VALUES (%s, 'build', %s, 0, 0, 0, %s, %s)",
            (job_id, tokens, cost_usd, day.isoformat()),
        )


def _set_job_status_on_day(store, job_id: int, status: JobStatus, day: datetime) -> None:
    with store._pool.connection() as conn:
        conn.execute(
            "UPDATE jobs SET status = %s, updated_at = %s WHERE id = %s",
            (status.value, day.isoformat(), job_id),
        )


def test_performance_trend_buckets_multiple_days_separately(store):
    project = store.create_project(f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        job = store.create(idea="trend job", repo_path=project.repo_path, chat_id=1)
        day1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        day2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
        _insert_usage_on_day(store, job.id, day1, cost_usd=1.0, tokens=100)
        _insert_usage_on_day(store, job.id, day2, cost_usd=2.0, tokens=200)

        result = store.performance_trend(project.id)

        assert [r["date"] for r in result] == ["2026-01-01", "2026-01-02"]
        assert result[0]["cost_usd"] == 1.0
        assert result[0]["tokens"] == 100
        assert result[1]["cost_usd"] == 2.0
        assert result[1]["tokens"] == 200
    finally:
        store.delete_project(project.id)


def test_performance_trend_scopes_cost_and_tokens_to_project(store):
    project_a = store.create_project(
        f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}"
    )
    project_b = store.create_project(
        f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        job_a = store.create(idea="job a", repo_path=project_a.repo_path, chat_id=1)
        job_b = store.create(idea="job b", repo_path=project_b.repo_path, chat_id=1)
        day = datetime(2026, 1, 5, tzinfo=timezone.utc)
        _insert_usage_on_day(store, job_a.id, day, cost_usd=1.0, tokens=100)
        _insert_usage_on_day(store, job_b.id, day, cost_usd=99.0, tokens=9999)

        result = store.performance_trend(project_a.id)

        assert len(result) == 1
        assert result[0]["cost_usd"] == 1.0
        assert result[0]["tokens"] == 100
    finally:
        store.delete_project(project_a.id)
        store.delete_project(project_b.id)


def test_performance_trend_computes_job_counts_and_success_rate(store):
    project = store.create_project(f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        day = datetime(2026, 1, 10, tzinfo=timezone.utc)
        done1 = store.create(idea="done 1", repo_path=project.repo_path, chat_id=1)
        done2 = store.create(idea="done 2", repo_path=project.repo_path, chat_id=1)
        failed1 = store.create(idea="failed 1", repo_path=project.repo_path, chat_id=1)
        running = store.create(idea="still running", repo_path=project.repo_path, chat_id=1)
        _set_job_status_on_day(store, done1.id, JobStatus.DONE, day)
        _set_job_status_on_day(store, done2.id, JobStatus.DONE, day)
        _set_job_status_on_day(store, failed1.id, JobStatus.FAILED, day)
        _set_job_status_on_day(store, running.id, JobStatus.RUNNING, day)

        result = store.performance_trend(project.id)

        assert len(result) == 1
        bucket = result[0]
        assert bucket["date"] == "2026-01-10"
        assert bucket["jobs_completed"] == 2
        assert bucket["jobs_failed"] == 1
        assert bucket["success_rate"] == 2 / 3
    finally:
        store.delete_project(project.id)


def test_performance_trend_empty_range_returns_empty_list(store):
    project = store.create_project(f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        job = store.create(idea="out of range job", repo_path=project.repo_path, chat_id=1)
        day = datetime(2026, 1, 1, tzinfo=timezone.utc)
        _insert_usage_on_day(store, job.id, day, cost_usd=1.0, tokens=100)

        result = store.performance_trend(
            project.id, from_date="2027-01-01T00:00:00+00:00", to_date="2027-02-01T00:00:00+00:00"
        )

        assert result == []
    finally:
        store.delete_project(project.id)


def test_performance_trend_excludes_operational_jobs_by_default(store):
    project = store.create_project(f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        day = datetime(2026, 1, 15, tzinfo=timezone.utc)
        ordinary = store.create(idea="ordinary job", repo_path=project.repo_path, chat_id=1)
        operational = store.create(
            idea="auto-deploy job",
            repo_path=project.repo_path,
            chat_id=1,
            source_actor="auto-deploy",
        )
        _insert_usage_on_day(store, ordinary.id, day, cost_usd=1.0, tokens=100)
        _insert_usage_on_day(store, operational.id, day, cost_usd=99.0, tokens=9999)
        _set_job_status_on_day(store, ordinary.id, JobStatus.DONE, day)
        _set_job_status_on_day(store, operational.id, JobStatus.FAILED, day)

        excluded = store.performance_trend(project.id)
        assert len(excluded) == 1
        bucket = excluded[0]
        assert bucket["cost_usd"] == 1.0
        assert bucket["tokens"] == 100
        assert bucket["jobs_completed"] == 1
        assert bucket["jobs_failed"] == 0

        included = store.performance_trend(project.id, include_operational=True)
        assert len(included) == 1
        bucket = included[0]
        assert bucket["cost_usd"] == 100.0
        assert bucket["tokens"] == 10099
        assert bucket["jobs_completed"] == 1
        assert bucket["jobs_failed"] == 1
    finally:
        store.delete_project(project.id)


def test_performance_trend_include_operational_empty_range_returns_empty_list(store):
    project = store.create_project(f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        operational = store.create(
            idea="out of range operational job",
            repo_path=project.repo_path,
            chat_id=1,
            source_actor="auto-deploy",
        )
        day = datetime(2026, 1, 1, tzinfo=timezone.utc)
        _insert_usage_on_day(store, operational.id, day, cost_usd=1.0, tokens=100)

        result = store.performance_trend(
            project.id,
            from_date="2027-01-01T00:00:00+00:00",
            to_date="2027-02-01T00:00:00+00:00",
            include_operational=True,
        )

        assert result == []
    finally:
        store.delete_project(project.id)


def test_performance_trend_excludes_legacy_operational_jobs_by_default(store):
    project = store.create_project(f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        day = datetime(2026, 1, 16, tzinfo=timezone.utc)
        ordinary = store.create(idea="ordinary job", repo_path=project.repo_path, chat_id=1)
        legacy_operational = store.create(
            idea="legacy auto-deploy job",
            repo_path=project.repo_path,
            chat_id=1,
            source=JobSource.SUPERVISOR,
            title="Auto-deploy: prod",
        )
        _insert_usage_on_day(store, ordinary.id, day, cost_usd=1.0, tokens=100)
        _insert_usage_on_day(store, legacy_operational.id, day, cost_usd=99.0, tokens=9999)
        _set_job_status_on_day(store, ordinary.id, JobStatus.DONE, day)
        _set_job_status_on_day(store, legacy_operational.id, JobStatus.FAILED, day)

        excluded = store.performance_trend(project.id)
        assert len(excluded) == 1
        bucket = excluded[0]
        assert bucket["cost_usd"] == 1.0
        assert bucket["tokens"] == 100
        assert bucket["jobs_completed"] == 1
        assert bucket["jobs_failed"] == 0

        included = store.performance_trend(project.id, include_operational=True)
        assert len(included) == 1
        bucket = included[0]
        assert bucket["cost_usd"] == 100.0
        assert bucket["tokens"] == 10099
        assert bucket["jobs_completed"] == 1
        assert bucket["jobs_failed"] == 1
    finally:
        store.delete_project(project.id)


def test_deployment_reliability_breakdown_only_counts_operational_jobs(store):
    project = store.create_project(f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        day = datetime(2026, 1, 20, tzinfo=timezone.utc)
        ordinary = store.create(idea="ordinary job", repo_path=project.repo_path, chat_id=1)
        operational = store.create(
            idea="auto-deploy job",
            repo_path=project.repo_path,
            chat_id=1,
            source_actor="auto-deploy",
        )
        _set_job_status_on_day(store, ordinary.id, JobStatus.FAILED, day)
        _set_job_status_on_day(store, operational.id, JobStatus.DONE, day)

        result = store.deployment_reliability_breakdown()

        assert len(result) == 1
        row = result[0]
        assert row["project_id"] == project.id
        assert row["total"] == 1
        assert row["done"] == 1
        assert row["failed"] == 0
        assert row["cancelled"] == 0
        assert row["failure_rate"] == 0.0
    finally:
        store.delete_project(project.id)


def test_deployment_reliability_breakdown_computes_counts_and_failure_rate(store):
    project = store.create_project(f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        day = datetime(2026, 1, 21, tzinfo=timezone.utc)
        done1 = store.create(
            idea="op done 1", repo_path=project.repo_path, chat_id=1, source_actor="auto-deploy"
        )
        done2 = store.create(
            idea="op done 2", repo_path=project.repo_path, chat_id=1, source_actor="auto-deploy"
        )
        failed = store.create(
            idea="op failed", repo_path=project.repo_path, chat_id=1, source_actor="auto-deploy"
        )
        cancelled = store.create(
            idea="op cancelled",
            repo_path=project.repo_path,
            chat_id=1,
            source_actor="auto-deploy",
        )
        _set_job_status_on_day(store, done1.id, JobStatus.DONE, day)
        _set_job_status_on_day(store, done2.id, JobStatus.DONE, day)
        _set_job_status_on_day(store, failed.id, JobStatus.FAILED, day)
        _set_job_status_on_day(store, cancelled.id, JobStatus.CANCELLED, day)

        result = store.deployment_reliability_breakdown()

        assert len(result) == 1
        row = result[0]
        assert row["total"] == 4
        assert row["done"] == 2
        assert row["failed"] == 1
        assert row["cancelled"] == 1
        assert row["failure_rate"] == 1 / 4
    finally:
        store.delete_project(project.id)


def test_deployment_reliability_breakdown_counts_legacy_operational_jobs(store):
    project = store.create_project(f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        day = datetime(2026, 1, 22, tzinfo=timezone.utc)
        ordinary = store.create(idea="ordinary job", repo_path=project.repo_path, chat_id=1)
        legacy_done = store.create(
            idea="legacy op done",
            repo_path=project.repo_path,
            chat_id=1,
            source=JobSource.SUPERVISOR,
            title="Auto-deploy: prod",
        )
        legacy_failed = store.create(
            idea="legacy op failed",
            repo_path=project.repo_path,
            chat_id=1,
            source=JobSource.SUPERVISOR,
            title="Auto-deploy: staging",
        )
        _set_job_status_on_day(store, ordinary.id, JobStatus.FAILED, day)
        _set_job_status_on_day(store, legacy_done.id, JobStatus.DONE, day)
        _set_job_status_on_day(store, legacy_failed.id, JobStatus.FAILED, day)

        result = store.deployment_reliability_breakdown(project_id=project.id)

        assert len(result) == 1
        row = result[0]
        assert row["total"] == 2
        assert row["done"] == 1
        assert row["failed"] == 1
        assert row["failure_rate"] == 0.5
    finally:
        store.delete_project(project.id)


def test_deployment_reliability_breakdown_since_restricts_to_recent_jobs(store):
    project = store.create_project(f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        old_job = store.create(
            idea="old operational job",
            repo_path=project.repo_path,
            chat_id=1,
            source_actor="auto-deploy",
        )
        new_job = store.create(
            idea="new operational job",
            repo_path=project.repo_path,
            chat_id=1,
            source_actor="auto-deploy",
        )
        cutoff = "2026-01-01T00:00:00+00:00"
        with store._pool.connection() as conn:
            conn.execute(
                "UPDATE jobs SET created_at = %s WHERE id = %s",
                ("2020-01-01T00:00:00+00:00", old_job.id),
            )
            conn.execute(
                "UPDATE jobs SET created_at = %s WHERE id = %s",
                ("2026-06-01T00:00:00+00:00", new_job.id),
            )

        result = store.deployment_reliability_breakdown(since=cutoff)

        assert len(result) == 1
        assert result[0]["total"] == 1
    finally:
        store.delete_project(project.id)


def test_deployment_reliability_breakdown_scopes_to_project_id(store):
    project_a = store.create_project(
        f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}"
    )
    project_b = store.create_project(
        f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        store.create(
            idea="op job a",
            repo_path=project_a.repo_path,
            chat_id=1,
            source_actor="auto-deploy",
        )
        store.create(
            idea="op job b",
            repo_path=project_b.repo_path,
            chat_id=1,
            source_actor="auto-deploy",
        )

        result = store.deployment_reliability_breakdown(project_id=project_a.id)

        assert len(result) == 1
        assert result[0]["project_id"] == project_a.id
    finally:
        store.delete_project(project_a.id)
        store.delete_project(project_b.id)


def test_deployment_reliability_breakdown_omits_project_with_no_operational_jobs(store):
    project = store.create_project(f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        store.create(idea="ordinary only", repo_path=project.repo_path, chat_id=1)

        result = store.deployment_reliability_breakdown(project_id=project.id)

        assert result == []
    finally:
        store.delete_project(project.id)


def test_deployment_reliability_breakdown_pending_job_has_zero_failure_rate_and_total(store):
    # failure_rate=None (hyqs/pipeline/store.py:6498) is structurally
    # unreachable for any row this query actually returns: the GROUP BY only
    # emits rows where COUNT(*) >= 1, so `total > 0` always holds for a
    # returned row. A project with zero operational jobs never reaches that
    # branch either — it returns [] entirely (see
    # test_deployment_reliability_breakdown_omits_project_with_no_operational_jobs
    # above). This test instead covers the total=1/failed=0 numerator path:
    # a lone operational job left in its default, non-terminal 'pending'
    # status should count toward total without counting as done/failed/cancelled.
    project = store.create_project(f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        pending = store.create(
            idea="pending auto-deploy job",
            repo_path=project.repo_path,
            chat_id=1,
            source_actor="auto-deploy",
        )
        assert pending.status == JobStatus.PENDING

        result = store.deployment_reliability_breakdown(project_id=project.id)

        assert len(result) == 1
        row = result[0]
        assert row["total"] == 1
        assert row["done"] == 0
        assert row["failed"] == 0
        assert row["cancelled"] == 0
        assert row["failure_rate"] == 0.0
    finally:
        store.delete_project(project.id)


# --- survey_active_job_queue -----------------------------------------------


def test_survey_active_job_queue_detects_plan_and_idea_declared_overlaps(store):
    project = store.create_project(f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        plan_job = store.create(idea="Add a helper", repo_path=project.repo_path, chat_id=1)
        plan_job.plan = {
            "summary": "s",
            "stories": [{"id": "S1", "target_files": ["hyqs/pipeline/foo.py"]}],
            "reuses": [],
            "adds": [],
        }
        store.save(plan_job)

        idea_job = store.create(
            idea="Fix a bug. Target files: hyqs/pipeline/bar.py",
            repo_path=project.repo_path,
            chat_id=1,
        )

        candidates = [
            {"key": "c1", "title": "Touch foo.py", "target_files": ["hyqs/pipeline/foo.py"]},
            {"key": "c2", "title": "Touch bar.py", "target_files": ["hyqs/pipeline/bar.py"]},
        ]

        result = store.survey_active_job_queue(project.id, candidates)

        assert result.overlaps["c1"] == [plan_job.id]
        assert result.overlaps["c2"] == [idea_job.id]
    finally:
        store.delete_project(project.id)


def test_survey_active_job_queue_detects_granted_scope_overlap(store):
    project = store.create_project(f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        job = store.create(
            idea="A job with no declared files up front",
            repo_path=project.repo_path,
            chat_id=1,
            source_meta={"scope": {"allowed_paths": ["hyqs/pipeline/baz.py"], "interfaces": ""}},
        )

        candidates = [
            {"key": "c1", "title": "Touch baz.py", "target_files": ["hyqs/pipeline/baz.py"]}
        ]

        result = store.survey_active_job_queue(project.id, candidates)

        assert result.overlaps["c1"] == [job.id]
    finally:
        store.delete_project(project.id)


def test_survey_active_job_queue_flags_job_with_no_scope_as_unknown(store):
    project = store.create_project(f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        job = store.create(idea="A vague job idea", repo_path=project.repo_path, chat_id=1)

        candidates = [
            {"key": "c1", "title": "Touch anything", "target_files": ["hyqs/pipeline/qux.py"]}
        ]

        result = store.survey_active_job_queue(project.id, candidates)

        assert result.unknown_target_file_jobs == [job.id]
        assert result.overlaps["c1"] == []
    finally:
        store.delete_project(project.id)


def test_survey_active_job_queue_excludes_other_projects_and_inactive_jobs(store):
    project = store.create_project(f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}")
    other_project = store.create_project(
        f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}"
    )
    try:
        store.create(
            idea="Target files: hyqs/pipeline/other.py",
            repo_path=other_project.repo_path,
            chat_id=1,
        )
        done_job = store.create(
            idea="Target files: hyqs/pipeline/done.py",
            repo_path=project.repo_path,
            chat_id=1,
        )
        done_job.status = JobStatus.DONE
        store.save(done_job)

        candidates = [
            {"key": "c1", "title": "Touch other.py", "target_files": ["hyqs/pipeline/other.py"]},
            {"key": "c2", "title": "Touch done.py", "target_files": ["hyqs/pipeline/done.py"]},
        ]

        result = store.survey_active_job_queue(project.id, candidates)

        assert result.overlaps == {"c1": [], "c2": []}
        assert result.unknown_target_file_jobs == []
    finally:
        store.delete_project(project.id)
        store.delete_project(other_project.id)


def test_get_active_remediation_clears_fix_that_depends_on_incident(store):
    project = store.create_project(f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        incident = store.create(idea="failed incident", repo_path=project.repo_path, chat_id=1)
        remediation = store.create(
            idea="invalid remediation",
            repo_path=project.repo_path,
            chat_id=1,
            depends_on=[incident.id],
        )
        store.set_active_remediation(incident.id, remediation.id)

        assert store.get_active_remediation(incident.id) is None
    finally:
        store.delete_project(project.id)


def test_get_active_remediation_clears_fix_behind_failed_dependency(store):
    project = store.create_project(f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        incident = store.create(idea="failed incident", repo_path=project.repo_path, chat_id=1)
        blocker = store.create(idea="failed blocker", repo_path=project.repo_path, chat_id=1)
        blocker.status = JobStatus.FAILED
        store.save(blocker)
        remediation = store.create(
            idea="blocked remediation",
            repo_path=project.repo_path,
            chat_id=1,
            depends_on=[blocker.id],
        )
        store.set_active_remediation(incident.id, remediation.id)

        assert store.get_active_remediation(incident.id) is None
    finally:
        store.delete_project(project.id)


def test_supersede_failed_with_split_transitions_terminal_parent(store):
    project = store.create_project(f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}")
    try:
        parent = store.create(idea="failed parent", repo_path=project.repo_path, chat_id=1)
        parent.status = JobStatus.FAILED
        store.save(parent)

        assert store.supersede_failed_with_split(parent.id, [7001, 7002]) is True

        updated = store.get(parent.id)
        assert updated is not None
        assert updated.status is JobStatus.CANCELLED
        assert updated.resolution == "superseded-by-saved-plan-split"
    finally:
        store.delete_project(project.id)


def test_reclaim_orphaned_worker_slots_idles_worker_on_non_running_job(store):
    """job #3988: a slot whose job_id points at a job that already went
    PENDING/FAILED elsewhere is orphaned — reclaim must free it."""
    project = store.create_project(f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}")
    worker_id = f"w-{uuid.uuid4()}"
    try:
        job = store.create(idea="stranded job", repo_path=project.repo_path, chat_id=1)
        job.status = JobStatus.FAILED
        store.save(job)

        now = time.time()
        asyncio.run(
            store.worker_heartbeat(
                worker_id,
                "host-a",
                111,
                status="busy",
                role="executor",
                now=now,
                job_id=job.id,
                stage="build",
                provider="claude",
            )
        )

        reclaimed = store.reclaim_orphaned_worker_slots()

        assert worker_id in reclaimed
        workers = store.list_workers(now, 300.0)
        row = next(w for w in workers if w["id"] == worker_id)
        assert row["status"] == "idle"
        assert row["job_id"] is None
    finally:
        store.worker_offline(worker_id)
        store.delete_project(project.id)


def test_reclaim_orphaned_worker_slots_never_touches_genuinely_running_job(store):
    """Critical negative case: a slot on a genuinely RUNNING job with a fresh
    heartbeat must never be reclaimed, no matter how stale last_seen looks —
    an over-eager reclaimer would corrupt in-flight work."""
    project = store.create_project(f"test-store-{uuid.uuid4()}", f"/tmp/test-store-{uuid.uuid4()}")
    worker_id = f"w-{uuid.uuid4()}"
    try:
        job = store.create(idea="genuinely running job", repo_path=project.repo_path, chat_id=1)
        job.status = JobStatus.RUNNING
        store.save(job)

        now = time.time()
        asyncio.run(
            store.worker_heartbeat(
                worker_id,
                "host-a",
                111,
                status="busy",
                role="executor",
                now=now,
                job_id=job.id,
                stage="build",
                provider="claude",
            )
        )

        reclaimed = store.reclaim_orphaned_worker_slots()

        assert worker_id not in reclaimed
        workers = store.list_workers(now, 300.0)
        row = next(w for w in workers if w["id"] == worker_id)
        assert row["status"] == "busy"
        assert row["job_id"] == job.id
    finally:
        store.worker_offline(worker_id)
        store.delete_project(project.id)


def test_reclaim_orphaned_worker_slots_idles_worker_on_missing_job(store):
    """A worker row referencing a job id that no longer exists at all is
    also orphaned and must be reclaimed."""
    worker_id = f"w-{uuid.uuid4()}"
    missing_job_id = 999_999_999
    try:
        now = time.time()
        asyncio.run(
            store.worker_heartbeat(
                worker_id,
                "host-a",
                111,
                status="busy",
                role="executor",
                now=now,
                job_id=missing_job_id,
                stage="build",
                provider="claude",
            )
        )

        reclaimed = store.reclaim_orphaned_worker_slots()

        assert worker_id in reclaimed
        workers = store.list_workers(now, 300.0)
        row = next(w for w in workers if w["id"] == worker_id)
        assert row["status"] == "idle"
        assert row["job_id"] is None
    finally:
        store.worker_offline(worker_id)


def test_reclaim_orphaned_worker_slots_leaves_idle_workers_untouched(store):
    """A worker row with job_id already NULL (idle/draining/leader/standby)
    must be left completely unchanged."""
    worker_id = f"w-{uuid.uuid4()}"
    try:
        now = time.time()
        asyncio.run(
            store.worker_heartbeat(
                worker_id, "host-a", 111, status="idle", role="executor", now=now
            )
        )

        reclaimed = store.reclaim_orphaned_worker_slots()

        assert worker_id not in reclaimed
        workers = store.list_workers(now, 300.0)
        row = next(w for w in workers if w["id"] == worker_id)
        assert row["status"] == "idle"
        assert row["job_id"] is None
    finally:
        store.worker_offline(worker_id)


def test_page_view_schema_statements_are_idempotent():
    schema = "\n".join(store_module._SCHEMA)

    assert "CREATE TABLE IF NOT EXISTS page_views" in schema
    assert "CREATE INDEX IF NOT EXISTS ix_page_views_ts ON page_views(ts)" in schema
    assert "CREATE INDEX IF NOT EXISTS ix_page_views_path_ts ON page_views(path, ts)" in schema
    for column in (
        "os_version",
        "arch",
        "platform",
        "langs",
        "win",
        "viewport",
        "color_scheme",
        "device_memory",
        "touch",
        "hour_cycle",
    ):
        assert f"ALTER TABLE page_views ADD COLUMN IF NOT EXISTS {column} TEXT" in schema


def test_record_and_list_page_views_round_trip_and_filter(store):
    visitor_hash = uuid.uuid4().hex
    first_ts = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
    second_ts = first_ts + timedelta(hours=1)
    third_ts = second_ts + timedelta(hours=1)
    try:
        first = store.record_page_view(
            path="/docs",
            ref="campaign-a",
            visitor_hash=visitor_hash,
            country="GB",
            region="England",
            city="London",
            referrer_host="example.com",
            ua_family="Firefox",
            os_family="Linux",
            lang="en-GB",
            tz="Europe/London",
            screen="1920x1080",
            os_version="15.6",
            arch="arm64",
            platform="macOS",
            langs="en-GB,fr",
            win="1400x900",
            viewport="lg",
            color_scheme="dark",
            device_memory="8.0",
            touch="0",
            hour_cycle="h23",
        )
        second = store.record_page_view(
            path="/pricing",
            ref="",
            visitor_hash=visitor_hash,
            country=None,
            region=None,
            city=None,
            referrer_host=None,
            ua_family=None,
            os_family=None,
            lang=None,
            tz=None,
            screen=None,
        )
        third = store.record_page_view(
            path="/docs",
            ref="campaign-b",
            visitor_hash=visitor_hash,
            country="US",
            region="CA",
            city="Oakland",
            referrer_host="search.example",
            ua_family="Safari",
            os_family="macOS",
            lang="en-US",
            tz="America/Los_Angeles",
            screen="1440x900",
        )
        with store._connection() as conn:
            conn.execute(
                "UPDATE page_views SET ts = CASE id WHEN %s THEN %s WHEN %s THEN %s "
                "WHEN %s THEN %s END WHERE id = ANY(%s)",
                (
                    first.id,
                    first_ts,
                    second.id,
                    second_ts,
                    third.id,
                    third_ts,
                    [first.id, second.id, third.id],
                ),
            )

        recorded = store.list_page_views(path="/docs")
        recorded = [view for view in recorded if view.visitor_hash == visitor_hash]

        assert all(isinstance(view, PageView) for view in recorded)
        assert [view.id for view in recorded] == [first.id, third.id]
        assert recorded[0] == PageView(
            id=first.id,
            ts=first_ts.isoformat(),
            path="/docs",
            ref="campaign-a",
            visitor_hash=visitor_hash,
            country="GB",
            region="England",
            city="London",
            referrer_host="example.com",
            ua_family="Firefox",
            os_family="Linux",
            lang="en-GB",
            tz="Europe/London",
            screen="1920x1080",
            os_version="15.6",
            arch="arm64",
            platform="macOS",
            langs="en-GB,fr",
            win="1400x900",
            viewport="lg",
            color_scheme="dark",
            device_memory="8.0",
            touch="0",
            hour_cycle="h23",
        )
        assert all(
            getattr(second, field) == ""
            for field in (
                "os_version",
                "arch",
                "platform",
                "langs",
                "win",
                "viewport",
                "color_scheme",
                "device_memory",
                "touch",
                "hour_cycle",
            )
        )
        since_results = store.list_page_views(since=second_ts.isoformat())
        assert [view.id for view in since_results if view.visitor_hash == visitor_hash] == [
            second.id,
            third.id,
        ]
        until_results = store.list_page_views(until=second_ts.isoformat())
        assert [view.id for view in until_results if view.visitor_hash == visitor_hash] == [
            first.id,
            second.id,
        ]
        assert [
            view.id
            for view in store.list_page_views(
                since=second_ts.isoformat(),
                until=third_ts.isoformat(),
                path="/docs",
            )
            if view.visitor_hash == visitor_hash
        ] == [third.id]
        assert all(
            getattr(second, field) is None
            for field in (
                "country",
                "region",
                "city",
                "referrer_host",
                "ua_family",
                "os_family",
                "lang",
                "tz",
                "screen",
            )
        )
    finally:
        with store._connection() as conn:
            conn.execute("DELETE FROM page_views WHERE visitor_hash = %s", (visitor_hash,))


def test_page_view_admin_summary_aggregates_bounded_to_window_and_path(store):
    hash_a = uuid.uuid4().hex
    hash_b = uuid.uuid4().hex
    hash_c = uuid.uuid4().hex
    hash_d = uuid.uuid4().hex
    all_hashes = [hash_a, hash_b, hash_c, hash_d]
    day_one = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
    day_two = day_one + timedelta(days=1)
    outside_window = day_one - timedelta(days=10)
    try:
        # Day one, /docs: two distinct visitors.
        view1 = store.record_page_view(
            path="/docs",
            ref="hn",
            visitor_hash=hash_a,
            country="US",
            region="CA",
            city="Oakland",
            referrer_host="news.ycombinator.com",
            ua_family="Chrome",
            os_family="macOS",
            lang="en-US",
            tz="America/Los_Angeles",
            screen="1920x1080",
        )
        view2 = store.record_page_view(
            path="/docs",
            ref="",
            visitor_hash=hash_b,
            country="GB",
            region="England",
            city="London",
            referrer_host="example.com",
            ua_family="Firefox",
            os_family="Linux",
            lang="en-GB",
            tz="Europe/London",
            screen="1366x768",
        )
        # Day two, /docs: hash_a reused across days. A naive global
        # COUNT(DISTINCT visitor_hash) would collapse this back down to
        # hash_a/hash_b (2), while the per-day-summed total must count it
        # as a third, distinct daily visitor.
        view3 = store.record_page_view(
            path="/docs",
            ref="tw",
            visitor_hash=hash_a,
            country="DE",
            region="Berlin",
            city="Berlin",
            referrer_host="twitter.com",
            ua_family="Edge",
            os_family="Windows",
            lang="de-DE",
            tz="Europe/Berlin",
            screen="2560x1440",
        )
        # Day one, different path: in-window but must be excluded by the
        # path filter, and included once the summary is unscoped.
        view4_pricing = store.record_page_view(
            path="/pricing",
            ref="hn",
            visitor_hash=hash_c,
            country="US",
            region="CA",
            city="Oakland",
            referrer_host="news.ycombinator.com",
            ua_family="Chrome",
            os_family="macOS",
            lang="en-US",
            tz="America/Los_Angeles",
            screen="1920x1080",
        )
        # Outside the window entirely.
        view5_outside = store.record_page_view(
            path="/docs",
            ref="hn",
            visitor_hash=hash_d,
            country="US",
            region="CA",
            city="Oakland",
            referrer_host="news.ycombinator.com",
            ua_family="Chrome",
            os_family="macOS",
            lang="en-US",
            tz="America/Los_Angeles",
            screen="1920x1080",
        )
        view1_ts = day_one
        view2_ts = day_one + timedelta(hours=1)
        view3_ts = day_two
        view4_ts = day_one + timedelta(hours=2)
        view5_ts = outside_window
        all_ids = [view1.id, view2.id, view3.id, view4_pricing.id, view5_outside.id]
        with store._connection() as conn:
            conn.execute(
                "UPDATE page_views SET ts = CASE id "
                "WHEN %s THEN %s WHEN %s THEN %s WHEN %s THEN %s "
                "WHEN %s THEN %s WHEN %s THEN %s END WHERE id = ANY(%s)",
                (
                    view1.id,
                    view1_ts,
                    view2.id,
                    view2_ts,
                    view3.id,
                    view3_ts,
                    view4_pricing.id,
                    view4_ts,
                    view5_outside.id,
                    view5_ts,
                    all_ids,
                ),
            )

        summary = store.page_view_admin_summary(
            since=day_one.isoformat(),
            until=day_two.isoformat(),
            path="/docs",
        )

        assert isinstance(summary, PageViewSummary)
        assert summary.totals["views"] == 3

        day_one_midnight = day_one.replace(hour=0, minute=0, second=0, microsecond=0)
        day_two_midnight = day_two.replace(hour=0, minute=0, second=0, microsecond=0)
        assert summary.by_day == [
            {"day": day_one_midnight.isoformat(), "views": 2, "unique_visitors": 2},
            {"day": day_two_midnight.isoformat(), "views": 1, "unique_visitors": 1},
        ]
        expected_daily_sum = 2 + 1
        assert summary.totals["unique_visitors"] == expected_daily_sum
        assert summary.totals["unique_visitors"] == sum(
            row["unique_visitors"] for row in summary.by_day
        )
        # The naive global DISTINCT would report 2 (hash_a, hash_b), not 3 —
        # proving the sum-of-daily-distinct computation is actually exercised.
        assert summary.totals["unique_visitors"] != len({hash_a, hash_b})
        assert summary.totals["days_with_traffic"] == 2

        assert {
            (row["country"], row["views"], row["unique_visitors"]) for row in summary.by_country
        } == {("US", 1, 1), ("GB", 1, 1), ("DE", 1, 1)}

        assert {(row["country"], row["city"], row["views"]) for row in summary.by_city} == {
            ("US", "Oakland", 1),
            ("GB", "London", 1),
            ("DE", "Berlin", 1),
        }

        assert {(row["ref"], row["views"], row["unique_visitors"]) for row in summary.by_ref} == {
            ("hn", 1, 1),
            ("(no token)", 1, 1),
            ("tw", 1, 1),
        }

        assert {(row["referrer_host"], row["views"]) for row in summary.by_referrer} == {
            ("news.ycombinator.com", 1),
            ("example.com", 1),
            ("twitter.com", 1),
        }

        assert {
            (row["os_family"], row["ua_family"], row["views"], row["unique_visitors"])
            for row in summary.by_device
        } == {
            ("macOS", "Chrome", 1, 1),
            ("Linux", "Firefox", 1, 1),
            ("Windows", "Edge", 1, 1),
        }

        # recent is ordered ts DESC: view3 (day two), then view2, then view1.
        assert [row["ts"] for row in summary.recent] == [
            view3_ts.isoformat(),
            view2_ts.isoformat(),
            view1_ts.isoformat(),
        ]
        assert {row["path"] for row in summary.recent} == {"/docs"}
        for row in summary.recent:
            assert "visitor_hash" not in row
            assert all(value not in all_hashes for value in row.values())

        # since/until filters, exercised independently.
        since_only = store.page_view_admin_summary(
            since=(day_one + timedelta(minutes=30)).isoformat(),
            until=day_two.isoformat(),
            path="/docs",
        )
        assert since_only.totals["views"] == 2
        assert {row["ts"] for row in since_only.recent} == {
            view2_ts.isoformat(),
            view3_ts.isoformat(),
        }

        until_only = store.page_view_admin_summary(
            since=day_one.isoformat(),
            until=(day_one + timedelta(minutes=30)).isoformat(),
            path="/docs",
        )
        assert until_only.totals["views"] == 1
        assert [row["ts"] for row in until_only.recent] == [view1_ts.isoformat()]

        # path filter, exercised independently.
        pricing_only = store.page_view_admin_summary(
            since=day_one.isoformat(),
            until=day_two.isoformat(),
            path="/pricing",
        )
        assert pricing_only.totals["views"] == 1
        assert {row["path"] for row in pricing_only.recent} == {"/pricing"}

        unscoped = store.page_view_admin_summary(
            since=day_one.isoformat(), until=day_two.isoformat()
        )
        assert unscoped.totals["views"] == 4
        assert unscoped.totals["unique_visitors"] == 4
        assert unscoped.totals["unique_visitors"] != len({hash_a, hash_b, hash_c})
    finally:
        with store._connection() as conn:
            conn.execute("DELETE FROM page_views WHERE visitor_hash = ANY(%s)", (all_hashes,))


def test_page_view_admin_summary_recent_never_selects_visitor_hash(store):
    visitor_hash = uuid.uuid4().hex
    try:
        store.record_page_view(
            path="/docs",
            ref="hn",
            visitor_hash=visitor_hash,
            country="US",
            region="CA",
            city="Oakland",
            referrer_host="news.ycombinator.com",
            ua_family="Chrome",
            os_family="macOS",
            lang="en-US",
            tz="America/Los_Angeles",
            screen="1920x1080",
        )

        summary = store.page_view_admin_summary(
            since="2000-01-01T00:00:00+00:00",
            until="2100-01-01T00:00:00+00:00",
            path="/docs",
        )

        assert summary.recent
        for row in summary.recent:
            assert "visitor_hash" not in row
            assert all(value != visitor_hash for value in row.values())
    finally:
        with store._connection() as conn:
            conn.execute("DELETE FROM page_views WHERE visitor_hash = %s", (visitor_hash,))


def test_page_view_admin_summary_aggregates_reader_dimensions(store):
    hash_a = uuid.uuid4().hex
    hash_b = uuid.uuid4().hex
    paths = [f"/chapter/{uuid.uuid4().hex}/{index}" for index in range(3)]
    day_one = datetime(2026, 5, 1, 9, tzinfo=timezone.utc)
    day_two = day_one + timedelta(days=1, hours=5)
    rows = [
        (paths[0], hash_a, day_one, "desktop", "dark", "en-US", "15.5", "macOS"),
        (
            paths[1],
            hash_a,
            day_one + timedelta(hours=1),
            "desktop",
            "dark",
            "en-US",
            "15.5",
            "macOS",
        ),
        (paths[0], hash_b, day_one + timedelta(hours=2), "mobile", "", "fr-FR", "", "Android"),
        (paths[2], hash_a, day_two, "desktop", "light", "en-US", "15.5", "macOS"),
    ]
    try:
        for path, visitor_hash, ts, viewport, scheme, langs, os_version, os_family in rows:
            view = store.record_page_view(
                path=path,
                ref="",
                visitor_hash=visitor_hash,
                country=None,
                region=None,
                city=None,
                referrer_host=None,
                ua_family="Browser",
                os_family=os_family,
                lang=langs,
                tz="UTC",
                screen="",
                viewport=viewport,
                color_scheme=scheme,
                langs=langs,
                os_version=os_version,
            )
            with store._connection() as conn:
                conn.execute("UPDATE page_views SET ts = %s WHERE id = %s", (ts, view.id))

        summary = store.page_view_admin_summary(
            since=day_one.isoformat(), until=(day_two + timedelta(hours=1)).isoformat()
        )

        assert summary.by_path[0] == {"path": paths[0], "views": 2, "unique_visitors": 2}
        assert {row["viewport"] for row in summary.by_viewport} == {"desktop", "mobile"}
        assert {row["color_scheme"] for row in summary.by_color_scheme} == {
            "dark",
            "light",
            "unknown",
        }
        assert len(summary.by_hour) == 24
        assert summary.by_hour[0] == {"hour": 0, "views": 0}
        assert {row["lang"] for row in summary.by_lang} == {"en-US", "fr-FR"}
        assert any(row["os_version"] == "unknown" for row in summary.by_os_version)
        assert {row["pages"]: row["visitors"] for row in summary.pages_per_visitor}[2] == 1
        assert summary.returning == {"single_day": 1, "multi_day": 1}
        assert summary.totals["paths_seen"] == 3
        assert summary.totals["avg_pages_per_visitor"] == 1.33
        assert all("visitor_hash" not in row for row in summary.recent)

        filtered = store.page_view_admin_summary(
            since=day_one.isoformat(),
            until=(day_two + timedelta(hours=1)).isoformat(),
            path=paths[0],
        )
        assert filtered.totals["views"] == 2
        assert filtered.totals["paths_seen"] == 1
    finally:
        with store._connection() as conn:
            conn.execute("DELETE FROM page_views WHERE visitor_hash = ANY(%s)", ([hash_a, hash_b],))


def test_page_view_summary_new_dimensions_default_independently():
    summary = PageViewSummary({}, [], [], [], [], [], [], [])

    assert summary.by_path == []
    assert summary.by_hour == []
    assert summary.returning == {"single_day": 0, "multi_day": 0}


def test_get_or_create_page_view_salt_is_persisted(store):
    original = store.get_meta("page_view_salt", default="")
    second_store = None
    try:
        with store._connection() as conn:
            conn.execute("DELETE FROM meta WHERE key = 'page_view_salt'")

        salt = store.get_or_create_page_view_salt()

        assert len(salt) == 64
        assert bytes.fromhex(salt)
        assert store.get_or_create_page_view_salt() == salt
        assert store.get_meta("page_view_salt") == salt
        second_store = JobStore(store._dsn)
        assert second_store.get_or_create_page_view_salt() == salt
    finally:
        if second_store is not None:
            second_store.close()
        with store._connection() as conn:
            conn.execute("DELETE FROM meta WHERE key = 'page_view_salt'")
        if original:
            store.set_meta("page_view_salt", original)
