import asyncio
import inspect
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from hyqs.config import Config
from hyqs.pipeline.__main__ import _build_notifier
from hyqs.pipeline.gitops import GitResult
from hyqs.pipeline.models import Job, JobStatus, Stage, Webhook
from hyqs.pipeline.supervisor import (
    FailureClass,
    _check_dependency_cycles,
    _check_job_staleness,
    _janitor_scan,
    remediate_gate_no_changes,
    remediate_stale_branch,
)

_CONFIG_NO_LINK = Config(model="sonnet", permission_mode="acceptEdits", web_base_url="")
_CONFIG_WITH_LINK = Config(
    model="sonnet", permission_mode="acceptEdits", web_base_url="https://hyqs.example.com"
)


class _FakeStore:
    def __init__(
        self,
        *,
        token: str | None,
        webhooks: list[Webhook],
        job: Job | None = None,
    ) -> None:
        self._token = token
        self._webhooks = webhooks
        self._threads: dict[tuple[int, int], str] = {}
        self._job = job

    def get_project_slack_token(self, project_id: int) -> str | None:
        return self._token

    def list_webhooks(self, project_id: int) -> list[Webhook]:
        return self._webhooks

    def get_slack_thread_ts(self, job_id: int, webhook_id: int) -> str | None:
        return self._threads.get((job_id, webhook_id))

    def set_slack_thread_ts(self, job_id: int, webhook_id: int, thread_ts: str) -> None:
        self._threads.setdefault((job_id, webhook_id), thread_ts)

    def get(self, job_id: int) -> Job | None:
        return self._job


class _StaleJobStore:
    def __init__(self, jobs: list[Job]) -> None:
        self.jobs = jobs
        self.meta: dict[str, str] = {}

    def list_active(self, *, limit: int, status: str) -> list[Job]:
        assert limit > 0
        assert status == "active"
        return [
            job
            for job in self.jobs
            if job.status in {JobStatus.PENDING, JobStatus.RUNNING, JobStatus.DEPLOYING}
            and not job.archived
        ]

    def get_meta(self, key: str, default: str = "") -> str:
        return self.meta.get(key, default)

    def set_meta(self, key: str, value: str) -> None:
        self.meta[key] = value


class _DependencyGraphStore(_StaleJobStore):
    def __init__(self, jobs: list[Job], dependencies: dict[int, list[int]]) -> None:
        super().__init__(jobs)
        self.dependencies = {job_id: list(deps) for job_id, deps in dependencies.items()}
        self.supervisor_events: list[dict] = []

    def get_dependencies(self, job_id: int) -> list[int]:
        return list(self.dependencies.get(job_id, []))

    def remove_job_dependency(self, job_id: int, dep_id: int) -> None:
        self.dependencies[job_id] = [d for d in self.dependencies.get(job_id, []) if d != dep_id]

    def record_supervisor_event(
        self, job_id: int, action: str, failure_class: str, detail: str = ""
    ) -> None:
        self.supervisor_events.append(
            {"job_id": job_id, "action": action, "failure_class": failure_class, "detail": detail}
        )


def _job_with_age(hours: float, **kwargs) -> Job:
    created_at = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return Job(
        id=kwargs.pop("id", 42),
        idea="idea",
        repo_path="/tmp/repo",
        chat_id=1,
        project_id=7,
        epic_id=9,
        title="Long-running build",
        created_at=created_at,
        **kwargs,
    )


def test_config_job_stale_hours_defaults_to_24_and_reads_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HYQS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("HYQS_PIPELINE_JOB_STALE_HOURS", raising=False)
    assert Config.from_env().pipeline_job_stale_hours == 24

    monkeypatch.setenv("HYQS_PIPELINE_JOB_STALE_HOURS", "36.5")
    assert Config.from_env().pipeline_job_stale_hours == 36.5


def test_job_staleness_younger_than_threshold_does_not_notify():
    invalid = _job_with_age(25, id=43)
    invalid.created_at = "not-a-timestamp"
    store = _StaleJobStore([_job_with_age(23), invalid])
    notify = AsyncMock()

    asyncio.run(_check_job_staleness(store, notify, 24))

    notify.assert_not_awaited()


def test_job_staleness_older_than_threshold_notifies_once_with_context():
    store = _StaleJobStore([_job_with_age(25)])
    notify = AsyncMock()

    asyncio.run(_check_job_staleness(store, notify, 24))
    asyncio.run(_check_job_staleness(store, notify, 24))

    notify.assert_awaited_once()
    args, kwargs = notify.await_args
    assert args[0] == 1
    assert "Job #42: Long-running build" in args[1]
    assert "epic #9" in args[1]
    assert kwargs == {
        "project_id": 7,
        "job_id": 42,
        "event_type": "needs_attention",
        "reason": "job_stale",
    }


def test_job_staleness_terminal_job_before_scan_does_not_notify():
    job = _job_with_age(25)
    store = _StaleJobStore([job])
    notify = AsyncMock()
    job.status = JobStatus.DONE

    asyncio.run(_check_job_staleness(store, notify, 24))

    notify.assert_not_awaited()


def test_janitor_scan_invokes_job_staleness_check():
    jobs = SimpleNamespace()
    notify = AsyncMock()
    config = SimpleNamespace(pipeline_job_stale_hours=31)

    with (
        patch("hyqs.pipeline.supervisor._repoint_split_orphans", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._check_job_staleness", new=AsyncMock()) as check,
        patch("hyqs.pipeline.supervisor._check_dependency_cycles", new=AsyncMock()) as cycles,
        patch("hyqs.pipeline.supervisor._check_fleet_liveness", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._reconcile_blocked_dependents", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._sweep_deploying_jobs", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor.gitops.gc_orphaned_artifacts", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._docker_sweep", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._docker_builder_prune", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._docker_image_prune", new=AsyncMock()),
    ):
        jobs.list_projects = lambda: []
        jobs.get_failed_jobs = lambda: []
        jobs.get_active_job_ids = lambda: set()
        jobs.get_git_artifact_protected_job_ids = lambda: set()
        jobs.prune_audit_log = lambda **kwargs: 0
        jobs.reclaim_orphaned_worker_slots = lambda: []
        asyncio.run(_janitor_scan(jobs, config, notify))

    check.assert_awaited_once_with(jobs, notify, 31.0)
    cycles.assert_awaited_once_with(jobs, notify)


def _graph_job(
    job_id: int, *, status: JobStatus = JobStatus.PENDING, source_meta: dict | None = None
) -> Job:
    return Job(
        id=job_id,
        idea="idea",
        repo_path="/tmp/repo",
        chat_id=job_id,
        project_id=7,
        title=f"Graph job {job_id}",
        status=status,
        source_meta=source_meta,
    )


def test_dependency_cycle_two_jobs_notifies_with_every_member():
    store = _DependencyGraphStore([_graph_job(1), _graph_job(2)], {1: [2], 2: [1]})
    notify = AsyncMock()

    asyncio.run(_check_dependency_cycles(store, notify))

    notify.assert_awaited_once()
    args, kwargs = notify.await_args
    assert "Job #1: Graph job 1" in args[1]
    assert "Job #2: Graph job 2" in args[1]
    assert kwargs["event_type"] == "needs_attention"
    assert kwargs["reason"] == "dependency_cycle"
    assert kwargs["job_id"] == 1


def test_dependency_cycle_three_jobs_notifies_with_every_member():
    jobs = [_graph_job(job_id) for job_id in (3, 1, 2)]
    store = _DependencyGraphStore(jobs, {1: [2], 2: [3], 3: [1]})
    notify = AsyncMock()

    asyncio.run(_check_dependency_cycles(store, notify))

    text = notify.await_args.args[1]
    assert all(f"Job #{job_id}: Graph job {job_id}" in text for job_id in (1, 2, 3))


def test_dependency_cycle_chain_and_diamond_do_not_notify():
    jobs = [_graph_job(job_id) for job_id in range(1, 7)]
    store = _DependencyGraphStore(
        jobs,
        {1: [2], 2: [3], 3: [4], 5: [2, 3], 6: [2, 3]},
    )
    notify = AsyncMock()

    asyncio.run(_check_dependency_cycles(store, notify))

    notify.assert_not_awaited()


def test_dependency_cycle_through_terminal_job_is_ignored():
    jobs = [_graph_job(1), _graph_job(2), _graph_job(3, status=JobStatus.DONE)]
    store = _DependencyGraphStore(jobs, {1: [2], 2: [3], 3: [1]})
    notify = AsyncMock()

    asyncio.run(_check_dependency_cycles(store, notify))

    notify.assert_not_awaited()


def test_dependency_cycle_consecutive_scans_are_deduplicated():
    store = _DependencyGraphStore([_graph_job(1), _graph_job(2)], {1: [2], 2: [1]})
    notify = AsyncMock()

    asyncio.run(_check_dependency_cycles(store, notify))
    asyncio.run(_check_dependency_cycles(store, notify))

    notify.assert_awaited_once()


def test_dependency_cycle_remediation_edge_repaired_preserves_incident_edge():
    incident = _graph_job(1)
    fix = _graph_job(2, source_meta={"ai_fix_for": 1, "remediation_root_job_id": 1})
    store = _DependencyGraphStore([incident, fix], {1: [2], 2: [1]})
    notify = AsyncMock()

    asyncio.run(_check_dependency_cycles(store, notify))

    # The incident's dependency on the fix survives — the fix chain still works.
    assert store.dependencies == {1: [2], 2: []}
    notify.assert_awaited_once()
    args, kwargs = notify.await_args
    assert "auto-repaired" in args[1]
    assert "#2" in args[1] and "#1" in args[1]
    assert kwargs["event_type"] == "dependency_cycle_repaired"
    assert kwargs["reason"] == "remediation_own_incident"
    assert kwargs["job_id"] == 2

    assert len(store.supervisor_events) == 1
    event = store.supervisor_events[0]
    assert event["job_id"] == 2
    assert event["action"] == "dependency_cycle_repaired"
    assert event["failure_class"] == "remediation_own_incident"
    detail = json.loads(event["detail"])
    assert detail["cycle"] == [1, 2]
    assert detail["removed_edge"] == {"job_id": 2, "depends_on_job_id": 1}
    assert detail["rule"] == "remediation_own_incident"


def test_dependency_cycle_survey_edge_is_repaired():
    dependent = _graph_job(2, source_meta={"queue_survey": {"depends_on_job_ids": [1]}})
    store = _DependencyGraphStore([_graph_job(1), dependent], {1: [2], 2: [1]})
    notify = AsyncMock()

    asyncio.run(_check_dependency_cycles(store, notify))

    assert store.dependencies == {1: [2], 2: []}
    notify.assert_awaited_once()
    kwargs = notify.await_args.kwargs
    assert kwargs["reason"] == "queue_survey_edge"
    assert kwargs["job_id"] == 2
    assert store.supervisor_events[0]["failure_class"] == "queue_survey_edge"


def test_dependency_cycle_survey_metadata_must_name_the_exact_edge():
    # job 2's queue survey recorded a dependency on job 3, not job 1 — the
    # 2 -> 1 edge is unproven and must be left alone (fail closed).
    dependent = _graph_job(2, source_meta={"queue_survey": {"depends_on_job_ids": [3]}})
    store = _DependencyGraphStore([_graph_job(1), dependent], {1: [2], 2: [1]})
    notify = AsyncMock()

    asyncio.run(_check_dependency_cycles(store, notify))

    assert store.dependencies == {1: [2], 2: [1]}
    assert store.supervisor_events == []
    kwargs = notify.await_args.kwargs
    assert kwargs["reason"] == "dependency_cycle"


def test_dependency_cycle_caller_declared_only_is_untouched_and_alerted():
    store = _DependencyGraphStore([_graph_job(1), _graph_job(2)], {1: [2], 2: [1]})
    notify = AsyncMock()

    asyncio.run(_check_dependency_cycles(store, notify))

    assert store.dependencies == {1: [2], 2: [1]}
    assert store.supervisor_events == []
    notify.assert_awaited_once()
    kwargs = notify.await_args.kwargs
    assert kwargs["reason"] == "dependency_cycle"
    assert kwargs["event_type"] == "needs_attention"


def test_dependency_cycle_prefers_remediation_over_queue_survey():
    job1 = _graph_job(1)
    job2 = _graph_job(2, source_meta={"queue_survey": {"depends_on_job_ids": [3]}})
    job3 = _graph_job(3, source_meta={"ai_fix_for": 1})
    store = _DependencyGraphStore([job1, job2, job3], {1: [2], 2: [3], 3: [1]})
    notify = AsyncMock()

    asyncio.run(_check_dependency_cycles(store, notify))

    assert store.dependencies == {1: [2], 2: [3], 3: []}
    notify.assert_awaited_once()
    kwargs = notify.await_args.kwargs
    assert kwargs["reason"] == "remediation_own_incident"
    assert kwargs["job_id"] == 3


def test_dependency_cycle_tie_break_prefers_lowest_dependent_job_id():
    job1 = _graph_job(1)
    job2 = _graph_job(2, source_meta={"ai_fix_for": 3})
    job3 = _graph_job(3, source_meta={"ai_fix_for": 1})
    store = _DependencyGraphStore([job1, job2, job3], {1: [2], 2: [3], 3: [1]})
    notify = AsyncMock()

    asyncio.run(_check_dependency_cycles(store, notify))

    assert store.dependencies == {1: [2], 2: [], 3: [1]}
    notify.assert_awaited_once()
    kwargs = notify.await_args.kwargs
    assert kwargs["reason"] == "remediation_own_incident"
    assert kwargs["job_id"] == 2


def test_dependency_cycle_independent_cycles_repaired_in_successive_passes():
    job1 = _graph_job(1)
    job2 = _graph_job(2, source_meta={"ai_fix_for": 1})
    job3 = _graph_job(3)
    job4 = _graph_job(4, source_meta={"ai_fix_for": 3})
    store = _DependencyGraphStore([job1, job2, job3, job4], {1: [2], 2: [1], 3: [4], 4: [3]})
    notify = AsyncMock()

    asyncio.run(_check_dependency_cycles(store, notify))

    # Exactly one edge removed per cycle — both independent cycles heal.
    assert store.dependencies == {1: [2], 2: [], 3: [4], 4: []}
    assert notify.await_count == 2
    reasons = {call.kwargs["reason"] for call in notify.await_args_list}
    assert reasons == {"remediation_own_incident"}
    dependent_ids = {call.kwargs["job_id"] for call in notify.await_args_list}
    assert dependent_ids == {2, 4}


def test_dependency_cycle_repair_budget_limits_repairs_per_scan(monkeypatch):
    import hyqs.pipeline.supervisor as supervisor_module

    monkeypatch.setattr(supervisor_module, "_DEPENDENCY_CYCLE_REPAIR_BUDGET", 1)
    job1 = _graph_job(1)
    job2 = _graph_job(2, source_meta={"ai_fix_for": 1})
    job3 = _graph_job(3)
    job4 = _graph_job(4, source_meta={"ai_fix_for": 3})
    store = _DependencyGraphStore([job1, job2, job3, job4], {1: [2], 2: [1], 3: [4], 4: [3]})
    notify = AsyncMock()

    asyncio.run(_check_dependency_cycles(store, notify))

    # Only one repair spent its budget; the other cycle falls back to the alert.
    assert store.dependencies[2] == []
    assert store.dependencies[3] == [4]
    assert store.dependencies[4] == [3]
    reasons = [call.kwargs["reason"] for call in notify.await_args_list]
    assert reasons.count("remediation_own_incident") == 1
    assert reasons.count("dependency_cycle") == 1


def _slack_webhook(*, active: bool = True, kind: str = "slack") -> Webhook:
    return Webhook(id=1, project_id=7, url="C123", event_type="notify", kind=kind, active=active)


def _http_webhook(
    webhook_id: int, event_type: str = "job_complete", *, active: bool = True
) -> Webhook:
    return Webhook(
        id=webhook_id,
        project_id=7,
        url=f"https://hooks.example.com/{webhook_id}",
        event_type=event_type,
        kind="http",
        active=active,
    )


async def _notify_and_close(notify, close_notifier, *args, **kwargs):
    await notify(*args, **kwargs)
    await close_notifier()


def test_notify_delivers_only_matching_active_http_webhooks_with_bounded_payload():
    job = Job(
        id=42,
        idea="idea",
        repo_path="/tmp/repo",
        chat_id=1,
        project_id=7,
        status=JobStatus.DONE,
    )
    store = _FakeStore(
        token=None,
        webhooks=[
            _http_webhook(1),
            _http_webhook(2, "deploy"),
            _http_webhook(3, active=False),
        ],
        job=job,
    )
    notify, close_notifier = _build_notifier(store, _CONFIG_NO_LINK)
    with patch(
        "hyqs.pipeline.notify_slack.send_http_webhook", new=AsyncMock(return_value=True)
    ) as mock_send:
        asyncio.run(
            _notify_and_close(
                notify,
                close_notifier,
                1,
                "x" * 600,
                project_id=7,
                job_id=42,
            )
        )

    mock_send.assert_awaited_once()
    assert mock_send.await_args.args[:2] == (
        "https://hooks.example.com/1",
        {
            "project_id": 7,
            "job_id": 42,
            "event_type": "job_complete",
            "summary": "x" * 500,
        },
    )


def test_notify_http_delivery_failures_are_independent():
    job = Job(
        id=42,
        idea="idea",
        repo_path="/tmp/repo",
        chat_id=1,
        project_id=7,
        status=JobStatus.DONE,
        deployed_commit="abc123",
    )
    store = _FakeStore(
        token=None, webhooks=[_http_webhook(1, "deploy"), _http_webhook(2, "deploy")], job=job
    )
    notify, close_notifier = _build_notifier(store, _CONFIG_NO_LINK)
    with patch(
        "hyqs.pipeline.notify_slack.send_http_webhook",
        new=AsyncMock(side_effect=[False, True]),
    ) as mock_send:
        asyncio.run(
            _notify_and_close(notify, close_notifier, 1, "deployed", project_id=7, job_id=42)
        )

    assert mock_send.await_count == 2
    assert all(call.args[1]["event_type"] == "deploy" for call in mock_send.await_args_list)


def test_notify_delivers_needs_attention_webhook_with_reason_and_epic_id():
    job = Job(
        id=42,
        idea="idea",
        repo_path="/tmp/repo",
        chat_id=1,
        project_id=7,
        epic_id=9,
        status=JobStatus.RUNNING,
    )
    store = _FakeStore(token=None, webhooks=[_http_webhook(1, "needs_attention")], job=job)
    notify, close_notifier = _build_notifier(store, _CONFIG_NO_LINK)
    with patch(
        "hyqs.pipeline.notify_slack.send_http_webhook", new=AsyncMock(return_value=True)
    ) as mock_send:
        asyncio.run(
            _notify_and_close(
                notify,
                close_notifier,
                1,
                "giving up",
                project_id=7,
                job_id=42,
                event_type="needs_attention",
                reason="gate_no_changes",
            )
        )

    mock_send.assert_awaited_once()
    assert mock_send.await_args.args[:2] == (
        "https://hooks.example.com/1",
        {
            "project_id": 7,
            "job_id": 42,
            "event_type": "needs_attention",
            "summary": "giving up",
            "epic_id": 9,
            "reason": "gate_no_changes",
        },
    )


def test_notify_delivers_needs_attention_webhook_without_a_job():
    store = _FakeStore(token=None, webhooks=[_http_webhook(1, "needs_attention")])
    notify, close_notifier = _build_notifier(store, _CONFIG_NO_LINK)
    with patch(
        "hyqs.pipeline.notify_slack.send_http_webhook", new=AsyncMock(return_value=True)
    ) as mock_send:
        asyncio.run(
            _notify_and_close(
                notify,
                close_notifier,
                0,
                "deploys appear stuck",
                project_id=7,
                event_type="needs_attention",
                reason="deploy_stale",
            )
        )

    mock_send.assert_awaited_once()
    assert mock_send.await_args.args[:2] == (
        "https://hooks.example.com/1",
        {
            "project_id": 7,
            "job_id": None,
            "event_type": "needs_attention",
            "summary": "deploys appear stuck",
            "epic_id": None,
            "reason": "deploy_stale",
        },
    )


def test_notify_needs_attention_routes_only_to_matching_webhook_not_job_complete():
    job = Job(
        id=42,
        idea="idea",
        repo_path="/tmp/repo",
        chat_id=1,
        project_id=7,
        epic_id=9,
        status=JobStatus.RUNNING,
    )
    store = _FakeStore(
        token=None,
        webhooks=[_http_webhook(1, "needs_attention"), _http_webhook(2, "job_complete")],
        job=job,
    )
    notify, close_notifier = _build_notifier(store, _CONFIG_NO_LINK)
    with patch(
        "hyqs.pipeline.notify_slack.send_http_webhook", new=AsyncMock(return_value=True)
    ) as mock_send:
        asyncio.run(
            _notify_and_close(
                notify,
                close_notifier,
                1,
                "giving up",
                project_id=7,
                job_id=42,
                event_type="needs_attention",
                reason="gate_no_changes",
            )
        )

    mock_send.assert_awaited_once()
    assert mock_send.await_args.args[:2] == (
        "https://hooks.example.com/1",
        {
            "project_id": 7,
            "job_id": 42,
            "event_type": "needs_attention",
            "summary": "giving up",
            "epic_id": 9,
            "reason": "gate_no_changes",
        },
    )


def test_notify_job_complete_webhook_unaffected_by_missing_event_type_kwarg():
    job = Job(
        id=42, idea="idea", repo_path="/tmp/repo", chat_id=1, project_id=7, status=JobStatus.DONE
    )
    store = _FakeStore(
        token=None,
        webhooks=[_http_webhook(1, "job_complete"), _http_webhook(2, "needs_attention")],
        job=job,
    )
    notify, close_notifier = _build_notifier(store, _CONFIG_NO_LINK)
    with patch(
        "hyqs.pipeline.notify_slack.send_http_webhook", new=AsyncMock(return_value=True)
    ) as mock_send:
        asyncio.run(_notify_and_close(notify, close_notifier, 1, "done", project_id=7, job_id=42))

    mock_send.assert_awaited_once()
    assert mock_send.await_args.args[:2] == (
        "https://hooks.example.com/1",
        {"project_id": 7, "job_id": 42, "event_type": "job_complete", "summary": "done"},
    )


def test_notify_needs_attention_without_job_id_isolated_from_job_complete_webhook():
    store = _FakeStore(
        token=None,
        webhooks=[_http_webhook(1, "needs_attention"), _http_webhook(2, "job_complete")],
    )
    notify, close_notifier = _build_notifier(store, _CONFIG_NO_LINK)
    with patch(
        "hyqs.pipeline.notify_slack.send_http_webhook", new=AsyncMock(return_value=True)
    ) as mock_send:
        asyncio.run(
            _notify_and_close(
                notify,
                close_notifier,
                0,
                "deploys appear stuck",
                project_id=7,
                event_type="needs_attention",
                reason="deploy_stale",
            )
        )

    mock_send.assert_awaited_once()
    assert mock_send.await_args.args[:2] == (
        "https://hooks.example.com/1",
        {
            "project_id": 7,
            "job_id": None,
            "event_type": "needs_attention",
            "summary": "deploys appear stuck",
            "epic_id": None,
            "reason": "deploy_stale",
        },
    )


def test_remediate_stale_branch_dead_letter_cap_notifies_needs_attention():
    job = Job(
        id=5,
        idea="idea",
        repo_path="/tmp/repo",
        chat_id=1,
        project_id=7,
        branch="job-5",
        status=JobStatus.FAILED,
        failure="worktree missing",
    )
    jobs = MagicMock()
    jobs.supervisor_requeue_count.return_value = 5
    jobs.is_supervisor_notified.return_value = False
    notify = AsyncMock()

    asyncio.run(remediate_stale_branch(jobs, job, SimpleNamespace(), notify=notify))

    assert notify.await_args.kwargs["event_type"] == "needs_attention"
    assert notify.await_args.kwargs["reason"]


def test_janitor_scan_judgment_class_escalation_notifies_needs_attention():
    jobs = MagicMock()
    job = Job(
        id=6,
        idea="idea",
        repo_path="/tmp/repo",
        chat_id=1,
        project_id=7,
        stage=Stage.BUILD,
        status=JobStatus.FAILED,
        failure="unrecognized failure",
    )
    jobs.get_failed_jobs.return_value = [job]
    jobs.get_active_job_ids.return_value = set()
    jobs.prune_audit_log = lambda **kwargs: 0
    jobs.is_supervisor_notified.return_value = False
    jobs.count_supervisor_events.return_value = 2
    notify = AsyncMock()
    config = SimpleNamespace(pipeline_job_stale_hours=31)

    with (
        patch("hyqs.pipeline.supervisor._repoint_split_orphans", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._check_job_staleness", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._check_dependency_cycles", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._reconcile_blocked_dependents", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._sweep_deploying_jobs", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor.gitops.gc_orphaned_artifacts", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._docker_sweep", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._docker_builder_prune", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._docker_image_prune", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._ai_diagnose_and_act", new=AsyncMock(return_value=False)),
        patch(
            "hyqs.pipeline.supervisor.classify_failure",
            return_value=FailureClass.genuine_code,
        ),
    ):
        asyncio.run(_janitor_scan(jobs, config, notify))

    assert notify.await_args.kwargs["event_type"] == "needs_attention"
    assert notify.await_args.kwargs["reason"] == "genuine_code"


def test_remediate_gate_no_changes_suppressed_requeue_omits_event_type(tmp_path):
    job = Job(
        id=1,
        idea="Add a foo() helper function",
        repo_path="/fake/repo",
        chat_id=5,
        stage=Stage.BUILD,
        status=JobStatus.FAILED,
        failure="[symbol-collision] found 1 conflict",
        project_id=10,
        owner="host:123:0",
    )
    (tmp_path / "worktrees" / f"job-{job.id}").mkdir(parents=True)
    jobs = MagicMock()
    jobs.supervisor_requeue_count.return_value = 0
    jobs.is_supervisor_notified.return_value = False
    notify = AsyncMock()
    config = MagicMock()
    config.data_dir = tmp_path

    with (
        patch(
            "hyqs.pipeline.supervisor.gitops.default_branch", new_callable=AsyncMock
        ) as mock_default_branch,
        patch(
            "hyqs.pipeline.supervisor.gitops.fresh_base", new_callable=AsyncMock
        ) as mock_fresh_base,
        patch("hyqs.pipeline.supervisor.gitops.git", new_callable=AsyncMock) as mock_git,
        patch("hyqs.pipeline.supervisor.gitops.numstat", new_callable=AsyncMock) as mock_numstat,
        patch("hyqs.pipeline.supervisor.collision.check_symbol_collisions") as mock_check,
    ):
        mock_default_branch.return_value = "main"
        mock_fresh_base.return_value = "main"
        mock_git.return_value = GitResult(ok=True, stdout="abc123\n", stderr="", code=0)
        mock_numstat.return_value = [{"path": "hyqs/pipeline/bar.py"}]
        mock_check.return_value = []

        asyncio.run(remediate_gate_no_changes(jobs, job, config, notify))

    assert "event_type" not in notify.await_args.kwargs


def test_combined_entry_point_uses_and_closes_shared_notifier():
    from hyqs import __main__ as combined_main

    source = inspect.getsource(combined_main.run)
    assert "_build_notifier(jobs, config)" in source
    assert "PipelineRunner(jobs, config, notify=notify)" in source
    assert "await close_notifier()" in source


def test_notifier_cleanup_closes_hardened_shared_http_client():
    client = AsyncMock()
    store = _FakeStore(token=None, webhooks=[])
    with patch("hyqs.pipeline.__main__.httpx.AsyncClient", return_value=client) as client_class:
        _notify, close_notifier = _build_notifier(store, _CONFIG_NO_LINK)
        asyncio.run(close_notifier())

    client_class.assert_called_once_with(timeout=5.0, trust_env=False)
    client.aclose.assert_awaited_once_with()


def test_notify_with_token_and_active_slack_webhook_sends_message(caplog):
    store = _FakeStore(token="xoxb-secret", webhooks=[_slack_webhook()])
    notify, _ = _build_notifier(store, _CONFIG_NO_LINK)
    with patch(
        "hyqs.pipeline.notify_slack.send_slack", new=AsyncMock(return_value="1111.2222")
    ) as mock_send:
        with caplog.at_level(logging.INFO, logger="hyqs.pipeline"):
            asyncio.run(notify(1, "hello", project_id=7, job_id=42))
    mock_send.assert_awaited_once_with("xoxb-secret", "C123", "hello", thread_ts=None)
    assert any("job→chat" in r.message for r in caplog.records)


def test_notify_without_token_does_not_send(caplog):
    store = _FakeStore(token=None, webhooks=[_slack_webhook()])
    notify, _ = _build_notifier(store, _CONFIG_NO_LINK)
    with patch("hyqs.pipeline.notify_slack.send_slack", new=AsyncMock()) as mock_send:
        with caplog.at_level(logging.INFO, logger="hyqs.pipeline"):
            asyncio.run(notify(1, "hello", project_id=7, job_id=42))
    mock_send.assert_not_awaited()
    assert any("job→chat" in r.message for r in caplog.records)


def test_notify_without_active_slack_webhook_does_not_send(caplog):
    store = _FakeStore(
        token="xoxb-secret",
        webhooks=[_slack_webhook(kind="http"), _slack_webhook(active=False)],
    )
    notify, _ = _build_notifier(store, _CONFIG_NO_LINK)
    with patch("hyqs.pipeline.notify_slack.send_slack", new=AsyncMock()) as mock_send:
        with caplog.at_level(logging.INFO, logger="hyqs.pipeline"):
            asyncio.run(notify(1, "hello", project_id=7, job_id=42))
    mock_send.assert_not_awaited()
    assert any("job→chat" in r.message for r in caplog.records)


def test_notify_without_project_id_does_not_send(caplog):
    store = _FakeStore(token="xoxb-secret", webhooks=[_slack_webhook()])
    notify, _ = _build_notifier(store, _CONFIG_NO_LINK)
    with patch("hyqs.pipeline.notify_slack.send_slack", new=AsyncMock()) as mock_send:
        with caplog.at_level(logging.INFO, logger="hyqs.pipeline"):
            asyncio.run(notify(1, "hello", job_id=42))
    mock_send.assert_not_awaited()
    assert any("job→chat" in r.message for r in caplog.records)


def test_notify_without_job_id_does_not_send(caplog):
    store = _FakeStore(token="xoxb-secret", webhooks=[_slack_webhook()])
    notify, _ = _build_notifier(store, _CONFIG_NO_LINK)
    with patch("hyqs.pipeline.notify_slack.send_slack", new=AsyncMock()) as mock_send:
        with caplog.at_level(logging.INFO, logger="hyqs.pipeline"):
            asyncio.run(notify(1, "hello", project_id=7))
    mock_send.assert_not_awaited()
    assert any("job→chat" in r.message for r in caplog.records)


def test_notify_first_call_uses_no_thread_ts_then_stores_returned_ts(caplog):
    store = _FakeStore(token="xoxb-secret", webhooks=[_slack_webhook()])
    notify, _ = _build_notifier(store, _CONFIG_NO_LINK)
    with patch(
        "hyqs.pipeline.notify_slack.send_slack", new=AsyncMock(return_value="1111.2222")
    ) as mock_send:
        with caplog.at_level(logging.INFO, logger="hyqs.pipeline"):
            asyncio.run(notify(1, "hello", project_id=7, job_id=42))
    mock_send.assert_awaited_once_with("xoxb-secret", "C123", "hello", thread_ts=None)
    assert store.get_slack_thread_ts(42, 1) == "1111.2222"


def test_notify_second_call_replies_into_stored_thread(caplog):
    store = _FakeStore(token="xoxb-secret", webhooks=[_slack_webhook()])
    store.set_slack_thread_ts(42, 1, "1111.2222")
    notify, _ = _build_notifier(store, _CONFIG_NO_LINK)
    with patch(
        "hyqs.pipeline.notify_slack.send_slack", new=AsyncMock(return_value="9999.0000")
    ) as mock_send:
        with caplog.at_level(logging.INFO, logger="hyqs.pipeline"):
            asyncio.run(notify(1, "world", project_id=7, job_id=42))
    mock_send.assert_awaited_once_with("xoxb-secret", "C123", "world", thread_ts="1111.2222")
    assert store.get_slack_thread_ts(42, 1) == "1111.2222"


def test_notify_first_call_prepends_job_title_header(caplog):
    job = Job(id=42, idea="idea", repo_path="/tmp/repo", chat_id=1, title="Fix the bug")
    store = _FakeStore(token="xoxb-secret", webhooks=[_slack_webhook()], job=job)
    notify, _ = _build_notifier(store, _CONFIG_NO_LINK)
    with patch(
        "hyqs.pipeline.notify_slack.send_slack", new=AsyncMock(return_value="1111.2222")
    ) as mock_send:
        with caplog.at_level(logging.INFO, logger="hyqs.pipeline"):
            asyncio.run(notify(1, "hello", project_id=7, job_id=42))
    mock_send.assert_awaited_once_with(
        "xoxb-secret", "C123", "*Job #42: Fix the bug*\nhello", thread_ts=None
    )


def test_notify_first_call_with_blank_title_omits_colon(caplog):
    job = Job(id=42, idea="idea", repo_path="/tmp/repo", chat_id=1, title="")
    store = _FakeStore(token="xoxb-secret", webhooks=[_slack_webhook()], job=job)
    notify, _ = _build_notifier(store, _CONFIG_NO_LINK)
    with patch(
        "hyqs.pipeline.notify_slack.send_slack", new=AsyncMock(return_value="1111.2222")
    ) as mock_send:
        with caplog.at_level(logging.INFO, logger="hyqs.pipeline"):
            asyncio.run(notify(1, "hello", project_id=7, job_id=42))
    mock_send.assert_awaited_once_with("xoxb-secret", "C123", "*Job #42*\nhello", thread_ts=None)


def test_notify_second_call_leaves_text_unmodified(caplog):
    job = Job(id=42, idea="idea", repo_path="/tmp/repo", chat_id=1, title="Fix the bug")
    store = _FakeStore(token="xoxb-secret", webhooks=[_slack_webhook()], job=job)
    store.set_slack_thread_ts(42, 1, "1111.2222")
    notify, _ = _build_notifier(store, _CONFIG_NO_LINK)
    with patch(
        "hyqs.pipeline.notify_slack.send_slack", new=AsyncMock(return_value="9999.0000")
    ) as mock_send:
        with caplog.at_level(logging.INFO, logger="hyqs.pipeline"):
            asyncio.run(notify(1, "world", project_id=7, job_id=42))
    mock_send.assert_awaited_once_with("xoxb-secret", "C123", "world", thread_ts="1111.2222")


def test_notify_first_call_appends_view_job_link_when_base_url_configured(caplog):
    job = Job(id=42, idea="idea", repo_path="/tmp/repo", chat_id=1, title="Fix the bug")
    store = _FakeStore(token="xoxb-secret", webhooks=[_slack_webhook()], job=job)
    notify, _ = _build_notifier(store, _CONFIG_WITH_LINK)
    with patch(
        "hyqs.pipeline.notify_slack.send_slack", new=AsyncMock(return_value="1111.2222")
    ) as mock_send:
        with caplog.at_level(logging.INFO, logger="hyqs.pipeline"):
            asyncio.run(notify(1, "hello", project_id=7, job_id=42))
    mock_send.assert_awaited_once_with(
        "xoxb-secret",
        "C123",
        "*Job #42: Fix the bug*\nhello\n<https://hyqs.example.com/#project/7/job/42|View job>",
        thread_ts=None,
    )


def test_notify_first_call_omits_link_when_base_url_unset(caplog):
    job = Job(id=42, idea="idea", repo_path="/tmp/repo", chat_id=1, title="Fix the bug")
    store = _FakeStore(token="xoxb-secret", webhooks=[_slack_webhook()], job=job)
    notify, _ = _build_notifier(store, _CONFIG_NO_LINK)
    with patch(
        "hyqs.pipeline.notify_slack.send_slack", new=AsyncMock(return_value="1111.2222")
    ) as mock_send:
        with caplog.at_level(logging.INFO, logger="hyqs.pipeline"):
            asyncio.run(notify(1, "hello", project_id=7, job_id=42))
    mock_send.assert_awaited_once_with(
        "xoxb-secret", "C123", "*Job #42: Fix the bug*\nhello", thread_ts=None
    )


def test_notify_second_call_never_includes_link_even_when_base_url_configured(caplog):
    job = Job(id=42, idea="idea", repo_path="/tmp/repo", chat_id=1, title="Fix the bug")
    store = _FakeStore(token="xoxb-secret", webhooks=[_slack_webhook()], job=job)
    store.set_slack_thread_ts(42, 1, "1111.2222")
    notify, _ = _build_notifier(store, _CONFIG_WITH_LINK)
    with patch(
        "hyqs.pipeline.notify_slack.send_slack", new=AsyncMock(return_value="9999.0000")
    ) as mock_send:
        with caplog.at_level(logging.INFO, logger="hyqs.pipeline"):
            asyncio.run(notify(1, "world", project_id=7, job_id=42))
    mock_send.assert_awaited_once_with("xoxb-secret", "C123", "world", thread_ts="1111.2222")


def _assert_all_notify_calls_have_kwarg(repo_root: Path, relpath: str, kwarg: str) -> None:
    src = (repo_root / relpath).read_text()
    for m in re.finditer(r"(?:self\.)?notify\(", src):
        start = m.start()
        depth = 0
        i = m.end() - 1
        while True:
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        call = src[start : i + 1]
        line = src[:start].count("\n") + 1
        assert kwarg in call, f"{relpath}:{line} missing {kwarg} kwarg: {call!r}"


def test_every_notify_call_in_runner_and_supervisor_passes_project_id():
    repo_root = Path(__file__).resolve().parent.parent
    for relpath in ("hyqs/pipeline/runner.py", "hyqs/pipeline/supervisor.py"):
        _assert_all_notify_calls_have_kwarg(repo_root, relpath, kwarg="project_id=")


def _assert_all_notify_calls_pass_job_id(repo_root: Path, relpath: str) -> None:
    """Every notify()/self.notify() call must pass job_id=, except the one call

    with no ``Job`` object in scope (``_check_deploy_staleness``'s project-wide
    stale-deploy alert, identifiable by ``project_id=project_id`` — a bare int,
    not ``job.project_id``).
    """
    src = (repo_root / relpath).read_text()
    for m in re.finditer(r"(?:self\.)?notify\(", src):
        start = m.start()
        depth = 0
        i = m.end() - 1
        while True:
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        call = src[start : i + 1]
        line = src[:start].count("\n") + 1
        if "project_id=project_id" in call:
            continue  # no job in scope
        assert "job_id=" in call, f"{relpath}:{line} missing job_id= kwarg: {call!r}"


def test_every_notify_call_in_runner_and_supervisor_passes_job_id():
    repo_root = Path(__file__).resolve().parent.parent
    for relpath in ("hyqs/pipeline/runner.py", "hyqs/pipeline/supervisor.py"):
        _assert_all_notify_calls_pass_job_id(repo_root, relpath)


def test_every_notify_call_in_stage_files_passes_project_id():
    repo_root = Path(__file__).resolve().parent.parent
    for relpath in (
        "hyqs/pipeline/stages/build.py",
        "hyqs/pipeline/stages/deploy.py",
        "hyqs/pipeline/stages/design_review.py",
        "hyqs/pipeline/stages/fix.py",
        "hyqs/pipeline/stages/lint.py",
        "hyqs/pipeline/stages/merge.py",
        "hyqs/pipeline/stages/security.py",
    ):
        _assert_all_notify_calls_have_kwarg(repo_root, relpath, kwarg="project_id=")


def test_every_notify_call_in_stage_files_passes_job_id():
    repo_root = Path(__file__).resolve().parent.parent
    for relpath in (
        "hyqs/pipeline/stages/build.py",
        "hyqs/pipeline/stages/deploy.py",
        "hyqs/pipeline/stages/design_review.py",
        "hyqs/pipeline/stages/fix.py",
        "hyqs/pipeline/stages/lint.py",
        "hyqs/pipeline/stages/merge.py",
        "hyqs/pipeline/stages/security.py",
    ):
        _assert_all_notify_calls_have_kwarg(repo_root, relpath, kwarg="job_id=")


def test_security_public_exports_share_owner_identity():
    import hyqs
    import hyqs.pipeline as pipeline
    import hyqs.pipeline.stages as stages
    import hyqs.pipeline.stages.security as security
    import hyqs.web as web

    for surface in (stages, pipeline, hyqs, web):
        assert surface.SECURITY_CHECK_ID is security.SECURITY_CHECK_ID
        assert surface.compose_security_failure_detail is security.compose_security_failure_detail
        assert not hasattr(surface, "SECURITY_EVIDENCE_ID")
