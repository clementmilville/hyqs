from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from hyqs.pipeline.npm_audit import (
    AdvisoryEvidence,
    AuditPolicy,
    AuditScope,
    ComparisonStatus,
    NpmProject,
    ProjectAuditResult,
    audit_changed_npm_projects,
    classify_project,
    discover_npm_projects,
    parse_npm_audit_v2,
)

PROJECT = NpmProject("web/package.json", "web/package-lock.json")


def _payload(*, severity="high", dev=False, fix=None, source=1001, node="node_modules/a"):
    return {
        "auditReportVersion": 2,
        "vulnerabilities": {
            "a": {
                "severity": severity,
                "nodes": [node],
                "via": [
                    {
                        "source": source,
                        "title": "prototype pollution",
                        "url": "https://example.test/advisory",
                        "severity": severity,
                        "range": "<1.2.3",
                    }
                ],
                "fixAvailable": fix,
            }
        },
        "packages": {node: {"version": "1.2.0", "dev": dev}},
    }


def _record(**updates):
    record = AdvisoryEvidence(
        "1001",
        "a",
        "high",
        "node_modules/a",
        AuditScope.DEVELOPMENT,
        "1.2.0",
        True,
        "1.2.3",
        "web/package.json",
        "web/package-lock.json",
        "evidence",
        "action",
        ("npm audit", "npm ls a"),
    )
    return replace(record, **updates)


def test_parse_retains_evidence_identity_scope_and_compatible_fix():
    result = parse_npm_audit_v2(_payload(dev=True, fix={"version": "1.2.3"}), PROJECT)
    record = result.records[0]
    assert result.attributable
    assert record.identity == ("1001", "a", "node_modules/a")
    assert (record.scope, record.installed_version) == (AuditScope.DEVELOPMENT, "1.2.0")
    assert record.lowest_compatible_patched_version == "1.2.3"
    assert record.manifest_path == "web/package.json"
    assert record.lockfile_path == "web/package-lock.json"
    assert "prototype pollution" in record.actionable_evidence
    assert "1.2.3" in record.recommended_action
    assert record.verification_commands == (
        "npm audit --json --package-lock-only --prefix web",
        "npm ls a --all",
    )


def test_parse_major_and_no_fix_do_not_invent_patched_version():
    major = parse_npm_audit_v2(
        _payload(fix={"version": "2.0.0", "isSemVerMajor": True}), PROJECT
    ).records[0]
    no_fix = parse_npm_audit_v2(_payload(fix=False), PROJECT).records[0]
    assert major.fix_available and major.lowest_compatible_patched_version is None
    assert "breaking" in major.recommended_action
    assert not no_fix.fix_available and no_fix.lowest_compatible_patched_version is None
    assert "No automatic fix" in no_fix.recommended_action


def test_parse_is_stably_ordered_per_nested_dependency_path():
    payload = _payload()
    payload["vulnerabilities"]["a"]["nodes"] = ["node_modules/z/node_modules/a", "node_modules/a"]
    payload["packages"]["node_modules/z/node_modules/a"] = {"version": "1.2.0", "dev": True}
    result = parse_npm_audit_v2(payload, PROJECT)
    assert [r.dependency_path for r in result.records] == [
        "node_modules/a",
        "node_modules/z/node_modules/a",
    ]
    assert [r.scope for r in result.records] == [AuditScope.PRODUCTION, AuditScope.DEVELOPMENT]


def test_parse_real_v2_shape_uses_lockfile_for_version_and_scope():
    payload = _payload(dev=True)
    packages = payload.pop("packages")
    result = parse_npm_audit_v2(
        payload,
        PROJECT,
        {"lockfileVersion": 3, "packages": packages},
    )
    assert result.attributable
    assert result.records[0].installed_version == "1.2.0"
    assert result.records[0].scope is AuditScope.DEVELOPMENT


def test_classify_all_comparison_states_and_policy():
    base = ProjectAuditResult(
        PROJECT,
        (
            _record(),
            _record(advisory_id="severity", severity="moderate"),
            _record(advisory_id="scope"),
            _record(advisory_id="removed"),
        ),
    )
    candidate = ProjectAuditResult(
        PROJECT,
        (
            _record(),
            _record(advisory_id="severity", severity="critical"),
            _record(advisory_id="scope", scope=AuditScope.PRODUCTION),
            _record(advisory_id="new", severity="high"),
        ),
    )
    findings = classify_project(base, candidate, AuditPolicy(threshold="high"))
    by_id = {(f.candidate or f.baseline).advisory_id: f for f in findings}
    assert by_id["1001"].status is ComparisonStatus.UNCHANGED_BASELINE
    assert by_id["severity"].status is ComparisonStatus.WORSENED
    assert by_id["scope"].status is ComparisonStatus.WORSENED
    assert by_id["new"].status is ComparisonStatus.INTRODUCED
    assert by_id["removed"].status is ComparisonStatus.REMOVED
    assert by_id["severity"].blocks and by_id["scope"].blocks and by_id["new"].blocks
    assert not by_id["1001"].blocks and not by_id["removed"].blocks


def test_policy_applies_threshold_to_development_and_optional_baseline():
    introduced_dev = classify_project(
        ProjectAuditResult(PROJECT),
        ProjectAuditResult(PROJECT, (_record(severity="moderate"),)),
        AuditPolicy(threshold="moderate"),
    )[0]
    baseline_critical = _record(severity="critical", scope=AuditScope.PRODUCTION)
    unchanged = classify_project(
        ProjectAuditResult(PROJECT, (baseline_critical,)),
        ProjectAuditResult(PROJECT, (baseline_critical,)),
        AuditPolicy(block_critical_production_baseline=True),
    )[0]
    assert introduced_dev.blocks
    assert unchanged.blocks


def test_malformed_legacy_and_incomplete_are_indeterminate_and_block():
    for payload in (
        "not json",
        {"auditReportVersion": 1},
        {"auditReportVersion": 2, "vulnerabilities": {}, "packages": None},
    ):
        parsed = parse_npm_audit_v2(payload, PROJECT)
        finding = classify_project(parsed, parsed)[0]
        assert not parsed.attributable
        assert finding.status is ComparisonStatus.INDETERMINATE
        assert finding.blocks


def test_discovery_limits_supported_changed_files_and_excludes_node_modules(tmp_path):
    for directory in ("web", "other", "node_modules/pkg"):
        (tmp_path / directory).mkdir(parents=True)
        (tmp_path / directory / "package.json").write_text("{}")
        (tmp_path / directory / "package-lock.json").write_text("{}")
    projects = discover_npm_projects(
        tmp_path, ["web/package.json", "other/index.js", "node_modules/pkg/package-lock.json"]
    )
    assert projects == (PROJECT,)


def test_audit_skips_scripts_only_manifest_change(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "package.json").write_text(
        json.dumps({"scripts": {"test": "node --test new.test.js"}, "dependencies": {"a": "1"}})
    )
    (web / "package-lock.json").write_text('{"lockfileVersion":3}')

    async def fake_git(worktree, revision, path):
        assert path == "web/package.json"
        return json.dumps({"scripts": {"test": "node --test"}, "dependencies": {"a": "1"}}).encode()

    with (
        patch("hyqs.pipeline.npm_audit._git_file", side_effect=fake_git),
        patch("hyqs.pipeline.npm_audit._run_audit") as run_audit,
    ):
        report = asyncio.run(
            audit_changed_npm_projects(tmp_path, "origin/main", ["web/package.json"])
        )

    assert report.projects == ()
    assert report.findings == ()
    assert report.blocked is False
    run_audit.assert_not_called()


@pytest.mark.parametrize(
    "section",
    [
        "dependencies",
        "devDependencies",
        "peerDependencies",
        "optionalDependencies",
        "overrides",
    ],
)
def test_audit_runs_when_dependency_manifest_section_changes(tmp_path, section):
    web = tmp_path / "web"
    web.mkdir()
    (web / "package.json").write_text(json.dumps({section: {"a": "2"}}))
    (web / "package-lock.json").write_text('{"lockfileVersion":3}')

    async def fake_git(worktree, revision, path):
        return json.dumps({section: {"a": "1"}}).encode()

    async def fake_audit(snapshot, project):
        return ProjectAuditResult(project)

    with (
        patch("hyqs.pipeline.npm_audit._git_file", side_effect=fake_git),
        patch("hyqs.pipeline.npm_audit._run_audit", side_effect=fake_audit) as run_audit,
    ):
        report = asyncio.run(
            audit_changed_npm_projects(tmp_path, "origin/main", ["web/package.json"])
        )

    assert report.projects == (PROJECT,)
    assert run_audit.call_count == 2


def test_audit_uses_disposable_snapshots_and_preserves_worktree(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    manifest = web / "package.json"
    lock = web / "package-lock.json"
    manifest.write_text('{"name":"candidate"}')
    lock.write_text('{"lockfileVersion":3}')
    before = {path: path.read_bytes() for path in (manifest, lock)}
    npm_cwds: list[Path] = []

    async def fake_git(worktree, revision, path):
        return b'{"name":"base"}' if path.endswith("package.json") else b'{"lockfileVersion":3}'

    async def fake_audit(snapshot, project):
        npm_cwds.append(snapshot)
        assert snapshot.resolve() != tmp_path.resolve()
        return parse_npm_audit_v2(json.dumps(_payload()), project)

    with (
        patch("hyqs.pipeline.npm_audit._git_file", side_effect=fake_git),
        patch("hyqs.pipeline.npm_audit._run_audit", side_effect=fake_audit),
    ):
        report = asyncio.run(
            audit_changed_npm_projects(tmp_path, "origin/main", ["web/package-lock.json"])
        )
    assert len(npm_cwds) == 2
    assert all(not str(cwd).startswith(str(tmp_path)) for cwd in npm_cwds)
    assert {path: path.read_bytes() for path in (manifest, lock)} == before
    assert not report.blocked
