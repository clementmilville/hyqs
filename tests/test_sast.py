"""Unit tests for the deterministic SAST scanner layer's bandit path.

These tests mock ``hyqs.pipeline.sast._run_cmd`` (and ``shutil.which``) so no
real bandit subprocess is required. No Postgres or network needed.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from hyqs.pipeline import sast
from hyqs.pipeline.npm_audit import (
    AdvisoryEvidence,
    AuditScope,
    ClassifiedFinding,
    ComparisonStatus,
    NpmAuditReport,
    NpmProject,
)


def test_is_test_path_true_for_nested_test_dirs():
    assert sast._is_test_path("backend/tests/unit/test_x.py")
    assert sast._is_test_path("tests/test_y.py")


def test_is_test_path_false_for_regular_source():
    assert not sast._is_test_path("src/app.py")


def test_is_test_path_false_for_naming_convention_outside_test_dir():
    # Basename alone (test_*.py / *_test.py / conftest.py) is not trusted as a
    # test-file signal outside of an actual tests/test directory — diff paths
    # are attacker-controlled, so a production file could otherwise dodge
    # bandit scanning purely by its name.
    assert not sast._is_test_path("src/conftest.py")
    assert not sast._is_test_path("src/test_helpers.py")
    assert not sast._is_test_path("src/helpers_test.py")


def _bandit_payload(findings: list[dict]) -> str:
    return json.dumps({"results": findings})


def _finding(filename: str, line: int = 1) -> dict:
    return {
        "issue_severity": "MEDIUM",
        "issue_confidence": "HIGH",
        "issue_text": "assert used",
        "test_id": "B101",
        "filename": filename,
        "line_number": line,
    }


def test_run_bandit_excludes_nested_test_dirs_only(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n")
    (tmp_path / "src" / "conftest.py").write_text("x = 1\n")

    changed_files = [
        "backend/tests/unit/test_x.py",
        "tests/test_y.py",
        "src/conftest.py",
        "src/app.py",
    ]

    recorded_calls: list[list[str]] = []

    async def fake_run_cmd(*args, cwd=None, stdin=None):
        recorded_calls.append(list(args))
        return 0, _bandit_payload([_finding(str(tmp_path / "src" / "app.py"))]), ""

    with (
        patch("hyqs.pipeline.sast.shutil.which", return_value="/usr/bin/bandit"),
        patch("hyqs.pipeline.sast._run_cmd", side_effect=fake_run_cmd),
    ):
        findings = asyncio.run(sast._run_bandit(tmp_path, changed_files))

    assert len(recorded_calls) == 1
    scanned = recorded_calls[0]
    # Regular source and a conftest.py OUTSIDE any tests/test dir are both scanned —
    # basename alone is never trusted as a test-only signal.
    assert str(tmp_path / "src" / "app.py") in scanned
    assert str(tmp_path / "src" / "conftest.py") in scanned
    for excluded in (
        "backend/tests/unit/test_x.py",
        "tests/test_y.py",
    ):
        assert str(tmp_path / excluded) not in scanned
    assert len(findings) == 1


def test_run_bandit_passes_config_only_for_files_under_it(tmp_path):
    backend = tmp_path / "backend"
    backend_src = backend / "src"
    backend_src.mkdir(parents=True)
    (backend / "pyproject.toml").write_text("[tool.bandit]\nskips = ['B101']\n")
    (backend_src / "app.py").write_text("x = 1\n")

    top_level = tmp_path / "app2.py"
    top_level.write_text("y = 2\n")

    changed_files = ["backend/src/app.py", "app2.py"]

    calls: list[list[str]] = []

    async def fake_run_cmd(*args, cwd=None, stdin=None):
        calls.append(list(args))
        return 0, _bandit_payload([]), ""

    with (
        patch("hyqs.pipeline.sast.shutil.which", return_value="/usr/bin/bandit"),
        patch("hyqs.pipeline.sast._run_cmd", side_effect=fake_run_cmd),
    ):
        asyncio.run(sast._run_bandit(tmp_path, changed_files))

    assert len(calls) == 2
    backend_call = next(c for c in calls if str(backend_src / "app.py") in c)
    app2_call = next(c for c in calls if str(top_level) in c)

    assert "-c" in backend_call
    assert backend_call[backend_call.index("-c") + 1] == str(backend / "pyproject.toml")
    assert "-c" not in app2_call


def test_run_bandit_ignores_config_that_is_itself_part_of_the_diff(tmp_path):
    # A diff that adds/edits its own bandit config alongside vulnerable code
    # must not have that config honored — otherwise the diff could smuggle in
    # skips/exclude_dirs to suppress findings on the code it introduces.
    backend = tmp_path / "backend"
    backend_src = backend / "src"
    backend_src.mkdir(parents=True)
    (backend / "pyproject.toml").write_text("[tool.bandit]\nskips = ['B101']\n")
    (backend_src / "app.py").write_text("x = 1\n")

    changed_files = ["backend/src/app.py", "backend/pyproject.toml"]

    calls: list[list[str]] = []

    async def fake_run_cmd(*args, cwd=None, stdin=None):
        calls.append(list(args))
        return 0, _bandit_payload([]), ""

    with (
        patch("hyqs.pipeline.sast.shutil.which", return_value="/usr/bin/bandit"),
        patch("hyqs.pipeline.sast._run_cmd", side_effect=fake_run_cmd),
    ):
        asyncio.run(sast._run_bandit(tmp_path, changed_files))

    assert len(calls) == 1
    assert "-c" not in calls[0]
    assert str(backend_src / "app.py") in calls[0]


def test_run_bandit_single_invocation_no_config_for_flat_repo(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("def test_x(): assert True\n")

    changed_files = ["app.py", "tests/test_app.py"]

    calls: list[list[str]] = []

    async def fake_run_cmd(*args, cwd=None, stdin=None):
        calls.append(list(args))
        return 0, _bandit_payload([]), ""

    with (
        patch("hyqs.pipeline.sast.shutil.which", return_value="/usr/bin/bandit"),
        patch("hyqs.pipeline.sast._run_cmd", side_effect=fake_run_cmd),
    ):
        asyncio.run(sast._run_bandit(tmp_path, changed_files))

    assert len(calls) == 1
    assert "-c" not in calls[0]
    assert str(tmp_path / "app.py") in calls[0]


def _npm_evidence(project: NpmProject) -> AdvisoryEvidence:
    return AdvisoryEvidence(
        "GHSA-1234",
        "example",
        "high",
        "node_modules/example",
        AuditScope.DEVELOPMENT,
        "1.0.0",
        True,
        "1.0.1",
        project.manifest_path,
        project.lockfile_path,
        "example advisory",
        "upgrade example",
        ("npm audit --json", "npm ls example --all"),
    )


def test_run_sast_scan_preserves_complete_npm_comparison_and_other_scanners(tmp_path):
    project = NpmProject("web/package.json", "web/package-lock.json")
    evidence = _npm_evidence(project)
    report = NpmAuditReport(
        (project,),
        (
            ClassifiedFinding(ComparisonStatus.INTRODUCED, evidence, None, "introduced", True),
            ClassifiedFinding(
                ComparisonStatus.UNCHANGED_BASELINE,
                evidence,
                evidence,
                "baseline",
                False,
            ),
            ClassifiedFinding(ComparisonStatus.REMOVED, None, evidence, "removed", False),
        ),
    )
    bandit = [sast.SastFinding("high", "unsafe call", "bandit", "src/app.py")]

    with (
        patch.object(sast, "_run_bandit", return_value=bandit),
        patch.object(sast, "_run_pip_audit", return_value=[]),
        patch.object(sast, "_run_detect_secrets", return_value=[]),
        patch.object(sast, "_run_npm_audit", return_value=report),
        patch.object(sast, "_run_semgrep", return_value=[]),
        patch.object(sast, "_changed_lines", return_value={}),
    ):
        result = asyncio.run(
            sast.run_sast_scan(
                tmp_path,
                "main",
                ["src/app.py", "web/package.json", "web/package-lock.json"],
            )
        )

    assert result.findings == tuple(bandit)
    assert result.npm_audit == report
    assert [finding.status for finding in result.npm_audit.findings] == [
        ComparisonStatus.INTRODUCED,
        ComparisonStatus.UNCHANGED_BASELINE,
        ComparisonStatus.REMOVED,
    ]
    assert result.blocked


def test_run_npm_audit_missing_executable_is_blocking_indeterminate(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "package.json").write_text("{}")
    (web / "package-lock.json").write_text("{}")

    with patch.object(sast.shutil, "which", return_value=None):
        report = asyncio.run(
            sast._run_npm_audit(
                tmp_path,
                "main",
                ["web/package.json", "web/package-lock.json"],
            )
        )

    assert report.blocked
    assert report.findings[0].status is ComparisonStatus.INDETERMINATE
    assert report.findings[0].reason == "npm executable is unavailable"
