"""Tests for the self-healing hardening pass (ledger-app incident follow-up).

Covers: env_deploy classification + container-conflict extraction, the docker
sweep candidate logic, the leak-guard transient filter, the import smoke, and
the AI analyst's action whitelist.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from hyqs.pipeline.incident_analyst import _evidence, validate_action
from hyqs.pipeline.models import Job, JobStatus, Stage
from hyqs.pipeline.stages.build import _filter_transient_paths
from hyqs.pipeline.supervisor import (
    _BUILD_CACHE_PRUNE_META_KEY,
    _CONTAINER_CONFLICT_RE,
    FailureClass,
    _builder_prune_due,
    _classify_deploy_failure,
    _classify_merge_failure,
    _docker_builder_prune,
    _docker_image_prune,
    _github_evidence_text,
    _image_prune_candidates,
    _is_github_api_transient,
    _sweep_candidates,
    classify_failure,
)
from hyqs.pipeline.testing import run_import_smoke


def _job(**kw) -> Job:
    defaults = dict(
        id=1, idea="i", repo_path="/r", chat_id=0, stage=Stage.DEPLOY, status=JobStatus.FAILED
    )
    defaults.update(kw)
    return Job(**defaults)


# --- deploy-stage failures route through merged_but_stuck first --------------


def test_container_name_conflict_classifies_env_deploy():
    # Any DEPLOY-stage failure is routed to merged_but_stuck FIRST so the
    # janitor can verify the PR state via GitHub before ever looking at the
    # error text (see _classify_deploy_failure for the env/attributable/config
    # split, which only runs once merged_but_stuck confirms the PR is unmerged).
    job = _job(
        error="deploy failed: Container hyqs-x Error response from daemon: Conflict. "
        'The container name "/hyqs-x" is already in use by container "683f87b".'
    )
    assert classify_failure(job) is FailureClass.merged_but_stuck


def test_probe_ip_failure_classifies_env_deploy():
    job = _job(error="deploy failed (docker run …): could not determine probe container IP")
    assert classify_failure(job) is FailureClass.merged_but_stuck


def test_attributable_deploy_still_files_fix_forward():
    job = _job(error="deploy failed: rollup failed — cannot find module './x'")
    assert classify_failure(job) is FailureClass.merged_but_stuck


def test_deploy_stage_deadlock_classifies_transient_not_config_deploy():
    # Job #610: PR already merged and deployed, but the worker's txn was
    # killed by a schema-migration deadlock. Must route to merged_but_stuck
    # (not the raw deadlock/transient shortcut, and not config_deploy/human
    # escalation) so the janitor verifies the merged PR and reconciles to DONE.
    job = _job(
        stage=Stage.DEPLOY,
        error="deploy failed: deadlock detected (SQLSTATE 40P01)",
    )
    assert classify_failure(job) is FailureClass.merged_but_stuck


def test_non_deploy_stage_serialization_failure_classifies_transient():
    job = _job(
        stage=Stage.BUILD,
        error="could not commit: serialization failure (SQLSTATE 40001)",
    )
    assert classify_failure(job) is FailureClass.transient


# --- _classify_deploy_failure: env/attributable/config split -----------------
# Only reached by the janitor once merged_but_stuck has confirmed the PR is
# NOT merged (see _janitor_scan).


def test_classify_deploy_failure_env_deploy():
    job = _job(error="deploy failed (docker run …): could not determine probe container IP")
    assert _classify_deploy_failure(job) is FailureClass.env_deploy


def test_classify_deploy_failure_attributable_deploy():
    job = _job(error="deploy failed: rollup failed — cannot find module './x'")
    assert _classify_deploy_failure(job) is FailureClass.attributable_deploy


def test_classify_deploy_failure_config_deploy_default():
    job = _job(error="deploy failed: container exited with code 137")
    assert _classify_deploy_failure(job) is FailureClass.config_deploy


def test_classify_deploy_failure_prefers_job_error_over_stale_failure():
    # Job #610: job.failure still had a stale "security rejected" message from
    # an earlier fix attempt while job.error held the real deploy-stage error.
    # job.error must decide the classification, not job.failure.
    job = _job(
        failure="cannot find module './x'",
        error="deploy failed (docker run …): could not determine probe container IP",
    )
    assert _classify_deploy_failure(job) is FailureClass.env_deploy


def test_classify_deploy_failure_falls_back_to_failure_when_error_empty():
    # job.error is only preferred when non-empty; an empty error must still
    # fall back to job.failure rather than crashing or misclassifying.
    job = _job(failure="deploy failed: deadlock detected (SQLSTATE 40P01)", error="")
    assert _classify_deploy_failure(job) is FailureClass.config_deploy


# --- _classify_merge_failure / _is_github_api_transient: GitHub API transients ---
# Only reached by the janitor once merged_but_stuck (rule 5) has confirmed the
# PR is NOT merged, mirroring _classify_deploy_failure (see _janitor_scan).


def test_classify_merge_failure_transient_from_prose_fallback():
    job = _job(
        stage=Stage.MERGE,
        error=(
            "PR merge failed: non-200 OK status code: 503 Service Unavailable "
            'body: "{\\"message\\": \\"No server is currently available to service '
            'your request. Sorry about that. Please try resubmitting your request..."'
        ),
    )
    assert _classify_merge_failure(job) is FailureClass.transient


def test_classify_merge_failure_transient_prefers_structured_evidence():
    # Structured github_evidence (populated by merge.py) must be preferred over
    # a stale/empty job.error, mirroring _classify_deploy_failure's job.error-
    # over-job.failure preference.
    job = _job(
        stage=Stage.MERGE,
        error="",
        failure_detail={
            "github_evidence": [
                {
                    "action": "merge",
                    "ok": False,
                    "stderr": "API rate limit exceeded ... secondary rate limit",
                }
            ]
        },
    )
    assert _classify_merge_failure(job) is FailureClass.transient


def test_classify_merge_failure_unknown_for_genuine_reason():
    # A negative case: a real reason the job's own code is responsible for must
    # NOT be reclassified as transient — this is the guard against a blanket
    # retry hiding real breakage.
    job = _job(
        stage=Stage.MERGE,
        error="PR merge failed: 2 of 3 required status checks are failing",
    )
    assert _classify_merge_failure(job) is FailureClass.unknown


def test_classify_failure_merge_conflict_precedence_unaffected_by_github_transient_text():
    # Rule 1 (merge_conflict_exhausted) must win even when the error text also
    # contains a GitHub-transient signal — precedence is unaffected by the new
    # GitHub-transient logic, which only runs from _janitor_scan after rule 5.
    job = _job(
        stage=Stage.MERGE,
        failure="[merge-conflict] Conflict markers (<<<<<<<, =======, >>>>>>>) are present",
        error="also saw a 503 Service Unavailable while retrying",
    )
    assert classify_failure(job) is FailureClass.merge_conflict_exhausted


def test_github_evidence_text_falls_back_to_error_when_no_evidence():
    job = _job(stage=Stage.MERGE, error="boom", failure_detail=None)
    assert _github_evidence_text(job) == "boom"


def test_is_github_api_transient_false_for_unrelated_error():
    job = _job(stage=Stage.MERGE, error="a required status check is failing")
    assert _is_github_api_transient(job) is False


def test_conflict_regex_extracts_container_name():
    text = 'The container name "/hyqs-ledger-app" is already in use by container "683f".'
    m = _CONTAINER_CONFLICT_RE.search(text)
    assert m and m.group(1) == "hyqs-ledger-app"


# --- docker sweep candidates ----------------------------------------------------


def test_sweep_removes_inactive_job_containers_keeps_active():
    containers = [
        ("job403-test-accountant", "Restarting (3) 46 seconds ago"),
        ("job999-probe-thing", "Up 2 minutes"),
        ("hyqs-ledger-app", "Up 3 hours"),
    ]
    out = _sweep_candidates(containers, active_job_ids={999})
    assert "job403-test-accountant" in out
    assert "job999-probe-thing" not in out  # active job's container is left alone
    assert "hyqs-ledger-app" not in out  # canonical app container never touched


def test_sweep_removes_dead_probes_keeps_live_ones():
    containers = [
        ("hyqs-ledger-app-probe", "Exited (1) 2 hours ago"),
        ("hyqs-acme-probe", "Up 20 seconds"),  # in-flight deploy: leave
        ("hyqs-web-probe-123", "Restarting (5) 10 seconds ago"),
    ]
    out = _sweep_candidates(containers, active_job_ids=set())
    assert "hyqs-ledger-app-probe" in out
    assert "hyqs-acme-probe" not in out
    assert "hyqs-web-probe-123" in out


# --- image prune candidates + builder prune gating --------------------------------


def test__image_prune_candidates_removes_inactive_job_images_keeps_active():
    images = ["job-403-frontend", "job-999-backend", "hyqs-web", "postgres:16"]
    out = _image_prune_candidates(images, active_job_ids={999})
    assert "job-403-frontend" in out
    assert "job-999-backend" not in out  # active job's image is left alone
    assert "hyqs-web" not in out
    assert "postgres:16" not in out


def test__image_prune_candidates_ignores_non_job_images():
    images = ["hyqs-web", "postgres:16", "redis:7"]
    out = _image_prune_candidates(images, active_job_ids=set())
    assert out == []


def test__builder_prune_due_true_when_never_pruned():
    assert _builder_prune_due("", time.time(), interval_seconds=3600) is True
    assert _builder_prune_due("not-a-number", time.time(), interval_seconds=3600) is True


def test__builder_prune_due_false_within_interval():
    now = time.time()
    assert _builder_prune_due(str(now - 10), now, interval_seconds=3600) is False


def test__builder_prune_due_true_after_interval_elapsed():
    now = time.time()
    assert _builder_prune_due(str(now - 7200), now, interval_seconds=3600) is True


def _make_proc(stdout_data: bytes = b"", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout_data, b""))
    return proc


def _make_jobs_meta_store(*, active_job_ids: set[int] | None = None) -> MagicMock:
    jobs = MagicMock()
    jobs.get_active_job_ids.return_value = active_job_ids or set()
    meta: dict[str, str] = {}
    jobs.get_meta.side_effect = lambda key, default="": meta.get(key, default)
    jobs.set_meta.side_effect = lambda key, value: meta.__setitem__(key, value)
    return jobs


def test_docker_builder_prune_uses_age_filter_and_records_meta():
    jobs = _make_jobs_meta_store()
    recorded_calls: list[tuple] = []

    async def fake_exec(*args, **kwargs):
        recorded_calls.append(args)
        return _make_proc(b"Total reclaimed space: 12GB\n")

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        asyncio.run(_docker_builder_prune(jobs))

    assert recorded_calls, "docker builder prune not invoked"
    cmd = recorded_calls[0]
    assert "builder" in cmd and "prune" in cmd
    assert "--filter" in cmd and "until=72h" in cmd
    assert jobs.get_meta(_BUILD_CACHE_PRUNE_META_KEY) != ""


def test_docker_builder_prune_skipped_when_recently_pruned():
    jobs = _make_jobs_meta_store()
    jobs.set_meta(_BUILD_CACHE_PRUNE_META_KEY, str(time.time()))

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        asyncio.run(_docker_builder_prune(jobs))

    mock_exec.assert_not_called()


def test_docker_builder_prune_swallows_subprocess_exception():
    jobs = _make_jobs_meta_store()

    async def fake_exec(*args, **kwargs):
        raise OSError("docker not found")

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        asyncio.run(_docker_builder_prune(jobs))  # must not raise

    # The attempt is still recorded so a persistent failure doesn't retry every scan.
    assert jobs.get_meta(_BUILD_CACHE_PRUNE_META_KEY) != ""


def test_docker_image_prune_only_removes_inactive_job_images():
    jobs = _make_jobs_meta_store(active_job_ids={999})
    recorded_calls: list[tuple] = []

    async def fake_exec(*args, **kwargs):
        recorded_calls.append(args)
        if args[:2] == ("docker", "images"):
            return _make_proc(b"job-403-frontend\njob-999-backend\nhyqs-web\npostgres:16\n")
        return _make_proc(b"")

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        asyncio.run(_docker_image_prune(jobs))

    rm_targets = {
        c[-1] for c in recorded_calls if c[0] == "docker" and c[1] == "image" and c[2] == "rm"
    }
    assert rm_targets == {"job-403-frontend"}


def test_docker_image_prune_swallows_subprocess_exception():
    jobs = _make_jobs_meta_store()

    async def fake_exec(*args, **kwargs):
        raise OSError("docker not found")

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        asyncio.run(_docker_image_prune(jobs))  # must not raise


# --- leak-guard transient filter -------------------------------------------------


def test_transient_paths_filtered_from_leak_diff():
    leaked = {
        "test_invitations.db-journal",
        "cache.tmp",
        "app/__pycache__/x.pyc",
        "coinbase.py",  # a real leak must survive the filter
    }
    out = _filter_transient_paths(leaked)
    assert out == {"coinbase.py"}


# --- import smoke ------------------------------------------------------------------


def test_import_smoke_skips_without_main(tmp_path):
    r = asyncio.run(run_import_smoke(tmp_path))
    assert r["passed"] and r.get("skipped")


def test_import_smoke_catches_import_time_nameerror(tmp_path):
    Path(tmp_path, "main.py").write_text("import missing_module_xyz\n")
    r = asyncio.run(run_import_smoke(tmp_path))
    assert not r["passed"]
    assert "missing_module_xyz" in r["summary"]


def test_import_smoke_passes_on_clean_main(tmp_path):
    Path(tmp_path, "main.py").write_text("x = 1\n")
    r = asyncio.run(run_import_smoke(tmp_path))
    assert r["passed"] and not r.get("skipped")


def test_import_smoke_injects_placeholder_for_missing_secret_key(tmp_path, monkeypatch):
    for key in ("SECRET_KEY", "SESSION_SECRET", "JWT_SECRET", "DATABASE_URL", "REDIS_URL"):
        monkeypatch.delenv(key, raising=False)
    Path(tmp_path, "main.py").write_text(
        "import os\n"
        "if not os.environ.get('SECRET_KEY'):\n"
        "    raise RuntimeError('SECRET_KEY must be set')\n"
    )
    r = asyncio.run(run_import_smoke(tmp_path))
    assert r["passed"] is True


def test_import_smoke_prefers_project_env_file(tmp_path):
    Path(tmp_path, ".env").write_text("SECRET_KEY=from-dotenv-file\n")
    Path(tmp_path, "main.py").write_text(
        "import os\n"
        "if os.environ['SECRET_KEY'] != 'from-dotenv-file':\n"
        "    raise AssertionError('placeholder leaked over project .env value')\n"
    )
    r = asyncio.run(run_import_smoke(tmp_path))
    assert r["passed"] is True


# --- analyst action whitelist ---------------------------------------------------------


def test_validate_action_whitelist():
    ok, _ = validate_action({"type": "requeue_at_stage", "stage": "deploy"})
    assert ok
    ok, _ = validate_action({"type": "requeue_at_stage", "stage": "merge_verify"})
    assert not ok  # not in the requeueable set
    ok, _ = validate_action({"type": "file_fix_job", "idea": "x" * 60})
    assert ok
    ok, _ = validate_action({"type": "file_fix_job", "idea": "too thin"})
    assert not ok
    ok, _ = validate_action(
        {
            "type": "repair_in_place",
            "idea": "Correct the existing configuration contract and its consumers.",
            "allowed_paths": ["src/config.py", "tests/test_config.py"],
        }
    )
    assert ok
    ok, _ = validate_action({"type": "escalate"})
    assert ok
    ok, _ = validate_action({"type": "run_shell", "cmd": "rm -rf /"})
    assert not ok


def test_validate_action_repair_in_place_rejects_unsafe_paths():
    invalid_paths = [[], ["/absolute.py"], ["../escape.py"], ["src/*.py"], ["src/a.py"] * 17]
    for paths in invalid_paths:
        ok, why = validate_action(
            {"type": "repair_in_place", "idea": "x" * 60, "allowed_paths": paths}
        )
        assert not ok
        assert "allowed_paths" in why
    ok, _ = validate_action("escalate")
    assert not ok


def test_validate_action_file_fix_jobs_chain():
    ok, _ = validate_action(
        {
            "type": "file_fix_jobs",
            "jobs": [
                {"idea": "x" * 60, "depends_on": []},
                {"idea": "y" * 60, "depends_on": [0]},
                {"idea": "z" * 60, "depends_on": [0, 1]},
            ],
        }
    )
    assert ok


def test_validate_action_file_fix_jobs_rejects_self_reference():
    ok, why = validate_action(
        {"type": "file_fix_jobs", "jobs": [{"idea": "x" * 60, "depends_on": [0]}]}
    )
    assert not ok
    assert "depends_on" in why


def test_validate_action_file_fix_jobs_rejects_forward_reference():
    ok, why = validate_action(
        {
            "type": "file_fix_jobs",
            "jobs": [
                {"idea": "x" * 60, "depends_on": [1]},
                {"idea": "y" * 60, "depends_on": []},
            ],
        }
    )
    assert not ok
    assert "depends_on" in why


def test_validate_action_file_fix_jobs_rejects_empty_list():
    ok, _ = validate_action({"type": "file_fix_jobs", "jobs": []})
    assert not ok


def test_validate_action_file_fix_jobs_rejects_too_many_entries():
    ok, _ = validate_action(
        {"type": "file_fix_jobs", "jobs": [{"idea": "x" * 60, "depends_on": []}] * 5}
    )
    assert not ok


def test_validate_action_file_fix_jobs_rejects_malformed_entry():
    ok, _ = validate_action(
        {"type": "file_fix_jobs", "jobs": [{"idea": "too thin", "depends_on": []}]}
    )
    assert not ok


def test_validate_action_accepts_covers_stories():
    ok, _ = validate_action(
        {"type": "file_fix_job", "idea": "x" * 60, "covers_stories": ["S1", "S2"]}
    )
    assert ok
    ok, _ = validate_action(
        {
            "type": "file_fix_jobs",
            "jobs": [{"idea": "x" * 60, "depends_on": [], "covers_stories": ["S1"]}],
        }
    )
    assert ok


def test_validate_action_rejects_malformed_covers_stories():
    ok, why = validate_action({"type": "file_fix_job", "idea": "x" * 60, "covers_stories": "S1"})
    assert not ok
    assert "covers_stories" in why
    ok, why = validate_action(
        {
            "type": "file_fix_jobs",
            "jobs": [{"idea": "x" * 60, "depends_on": [], "covers_stories": [1, 2]}],
        }
    )
    assert not ok
    assert "covers_stories" in why


def test_evidence_lists_plan_story_ids_for_covers_stories_reference():
    job = Job(
        id=7,
        idea="i",
        repo_path="/r",
        chat_id=1,
        stage=Stage.BUILD,
        status=JobStatus.FAILED,
        plan={
            "stories": [
                {"id": "S1", "title": "Add widget", "task": "Build the widget"},
                {"id": "S2", "title": "Wire widget", "task": "Wire it up"},
            ]
        },
    )
    text = _evidence(job, "genuine_code", [])
    assert "S1" in text
    assert "S2" in text
    assert "Add widget" in text


def test_evidence_omits_plan_section_when_no_stories():
    job = Job(id=7, idea="i", repo_path="/r", chat_id=1, stage=Stage.BUILD, status=JobStatus.FAILED)
    text = _evidence(job, "genuine_code", [])
    assert "Plan stories" not in text


def test_import_smoke_placeholder_beats_empty_envfile_value(tmp_path):
    """ledger-app round 2: .env.example ships `SECRET_KEY=` (empty). The empty
    string must not shadow the placeholder, or fail-fast apps deadlock again."""
    Path(tmp_path, ".env.example").write_text("SECRET_KEY=\nCUSTOM_REQUIRED_KEY=\n")
    Path(tmp_path, "main.py").write_text(
        "import os\n"
        "for k in ('SECRET_KEY', 'CUSTOM_REQUIRED_KEY'):\n"
        "    if not os.environ.get(k, ''):\n"
        "        raise RuntimeError(f'{k} must be set to a non-empty value')\n"
    )
    r = asyncio.run(run_import_smoke(tmp_path))
    assert r["passed"], r.get("summary")


# --- pipeline worker unit files: graceful drain must not be defeated -------------
# Job #627 made SIGTERM a graceful drain, but the default KillMode=control-group
# sends SIGTERM to every process in the unit's cgroup, including in-flight claude
# CLI subprocesses spawned for plan/build/fix — those got TERM'd out from under
# the worker's drain loop (job #543's plan agent died with exit 143 one second
# after the drain log line, 2026-07-12). KillMode=mixed limits SIGTERM to the
# main process only.


def test_pipeline_service_units_set_killmode_mixed():
    deploy_dir = Path(__file__).parent.parent / "deploy"
    for name in ("hyqs-pipeline@.service", "hyqs-pipeline.service"):
        text = (deploy_dir / name).read_text()
        assert "KillMode=mixed" in text, f"{name} is missing KillMode=mixed"


def test_pipeline_service_units_set_memory_limits():
    deploy_dir = Path(__file__).parent.parent / "deploy"
    for name in ("hyqs-pipeline@.service", "hyqs-pipeline.service"):
        text = (deploy_dir / name).read_text()
        assert "MemoryAccounting=yes" in text, f"{name} is missing MemoryAccounting=yes"
        assert "MemoryHigh=10G" in text, f"{name} is missing MemoryHigh=10G"
        assert "MemoryMax=14G" in text, f"{name} is missing MemoryMax=14G"
