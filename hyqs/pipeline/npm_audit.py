"""Baseline-aware, deterministic npm audit evidence and policy evaluation."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

from hyqs.pipeline.gitops import git

AUDIT_FILES = {"package.json", "package-lock.json", "npm-shrinkwrap.json"}
LOCKFILES = ("npm-shrinkwrap.json", "package-lock.json")
DEPENDENCY_MANIFEST_SECTIONS = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
    "overrides",
)
SEVERITY = {"info": 0, "low": 1, "moderate": 2, "high": 3, "critical": 4}


class AuditScope(str, Enum):
    PRODUCTION = "production"
    DEVELOPMENT = "development"
    UNKNOWN = "unknown"


class ComparisonStatus(str, Enum):
    INTRODUCED = "introduced"
    WORSENED = "worsened"
    UNCHANGED_BASELINE = "unchanged_baseline"
    REMOVED = "removed"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class NpmProject:
    manifest_path: str
    lockfile_path: str


@dataclass(frozen=True)
class AdvisoryEvidence:
    advisory_id: str
    package: str
    severity: str
    dependency_path: str
    scope: AuditScope
    installed_version: str | None
    fix_available: bool
    lowest_compatible_patched_version: str | None
    manifest_path: str
    lockfile_path: str
    actionable_evidence: str
    recommended_action: str
    verification_commands: tuple[str, ...]
    exploitable: bool = True

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.advisory_id, self.package, self.dependency_path


@dataclass(frozen=True)
class ProjectAuditResult:
    project: NpmProject
    records: tuple[AdvisoryEvidence, ...] = ()
    attributable: bool = True
    error: str | None = None


@dataclass(frozen=True)
class ClassifiedFinding:
    status: ComparisonStatus
    candidate: AdvisoryEvidence | None
    baseline: AdvisoryEvidence | None
    reason: str
    blocks: bool = False


@dataclass(frozen=True)
class AuditPolicy:
    threshold: str = "high"
    block_indeterminate: bool = True
    block_critical_production_baseline: bool = False

    def __post_init__(self) -> None:
        if self.threshold not in SEVERITY:
            raise ValueError(f"unknown npm audit severity threshold: {self.threshold}")


@dataclass(frozen=True)
class NpmAuditReport:
    projects: tuple[NpmProject, ...]
    findings: tuple[ClassifiedFinding, ...]

    @property
    def blocked(self) -> bool:
        return any(finding.blocks for finding in self.findings)


def discover_npm_projects(worktree: str | Path, changed_files: list[str]) -> tuple[NpmProject, ...]:
    """Return changed npm projects with a candidate manifest and supported lockfile."""
    root = Path(worktree)
    directories = {
        str(PurePosixPath(path).parent)
        for path in changed_files
        if PurePosixPath(path).name in AUDIT_FILES
        and "node_modules" not in PurePosixPath(path).parts
    }
    projects: list[NpmProject] = []
    for directory in sorted(directories):
        prefix = "" if directory == "." else f"{directory}/"
        manifest = f"{prefix}package.json"
        if not (root / manifest).is_file():
            continue
        lockfile = next(
            (f"{prefix}{name}" for name in LOCKFILES if (root / prefix / name).is_file()),
            None,
        )
        if lockfile:
            projects.append(NpmProject(manifest, lockfile))
    return tuple(projects)


def _scope_for_node(lock_packages: dict, node: str) -> AuditScope:
    metadata = lock_packages.get(node)
    if not isinstance(metadata, dict):
        return AuditScope.UNKNOWN
    return AuditScope.DEVELOPMENT if metadata.get("dev") is True else AuditScope.PRODUCTION


def _fix_details(fix: object, installed: str | None) -> tuple[bool, str | None]:
    if fix is True:
        return True, None
    if not isinstance(fix, dict):
        return False, None
    version = fix.get("version")
    if not isinstance(version, str) or fix.get("isSemVerMajor") is True:
        return True, None
    if installed and installed.split(".", 1)[0] != version.split(".", 1)[0]:
        return True, None
    return True, version


def parse_npm_audit_v2(
    payload: str | dict,
    project: NpmProject,
    lockfile: str | dict | None = None,
) -> ProjectAuditResult:
    """Parse npm audit v2 JSON, conservatively rejecting incomplete attribution."""
    try:
        data = json.loads(payload) if isinstance(payload, str) else payload
    except json.JSONDecodeError:
        return ProjectAuditResult(project, attributable=False, error="malformed npm JSON")
    if not isinstance(data, dict) or data.get("auditReportVersion") != 2:
        return ProjectAuditResult(project, attributable=False, error="unsupported npm audit output")
    vulnerabilities = data.get("vulnerabilities")
    try:
        lock_data = json.loads(lockfile) if isinstance(lockfile, str) else lockfile
    except json.JSONDecodeError:
        lock_data = None
    # npm audit v2 does not include package installation metadata. npm lockfiles
    # v2/v3 do, under ``packages``; accepting the old synthetic field keeps this
    # parser compatible with already-persisted test/evidence payloads.
    packages = lock_data.get("packages") if isinstance(lock_data, dict) else data.get("packages")
    if not isinstance(vulnerabilities, dict) or not isinstance(packages, dict):
        return ProjectAuditResult(
            project, attributable=False, error="incomplete npm audit v2 output"
        )

    records: list[AdvisoryEvidence] = []
    ambiguous = False
    for package, vulnerability in vulnerabilities.items():
        if not isinstance(vulnerability, dict):
            ambiguous = True
            continue
        nodes = vulnerability.get("nodes")
        vias = vulnerability.get("via")
        if not isinstance(nodes, list) or not nodes or not isinstance(vias, list):
            ambiguous = True
            continue
        advisories = [via for via in vias if isinstance(via, dict)]
        for via in vias:
            if isinstance(via, str):
                referenced = vulnerabilities.get(via)
                if isinstance(referenced, dict):
                    advisories.extend(
                        item for item in referenced.get("via", []) if isinstance(item, dict)
                    )
        if not advisories:
            ambiguous = True
            continue
        for node in sorted(set(nodes)):
            if not isinstance(node, str):
                ambiguous = True
                continue
            installed = (
                packages.get(node, {}).get("version")
                if isinstance(packages.get(node), dict)
                else None
            )
            scope = _scope_for_node(packages, node)
            if scope is AuditScope.UNKNOWN or not isinstance(installed, str):
                ambiguous = True
            for advisory in advisories:
                source = advisory.get("source")
                severity = str(advisory.get("severity", vulnerability.get("severity", ""))).lower()
                if source in (None, "") or severity not in SEVERITY:
                    ambiguous = True
                    continue
                advisory_id = str(source)
                title = str(advisory.get("title") or "npm advisory")
                url = str(advisory.get("url") or "")
                fix_available, patched = _fix_details(vulnerability.get("fixAvailable"), installed)
                evidence = f"{title}; affected range {advisory.get('range', vulnerability.get('range', 'unknown'))}"
                if url:
                    evidence += f"; {url}"
                if patched:
                    action = f"Update {package} to at least {patched} with npm install {package}@{patched}"
                elif fix_available:
                    action = (
                        f"Review npm audit fix for {package}; the available fix may be breaking"
                    )
                else:
                    action = f"No automatic fix is available for {package}; replace, remove, or mitigate it"
                records.append(
                    AdvisoryEvidence(
                        advisory_id=advisory_id,
                        package=str(package),
                        severity=severity,
                        dependency_path=node,
                        scope=scope,
                        installed_version=installed if isinstance(installed, str) else None,
                        fix_available=fix_available,
                        lowest_compatible_patched_version=patched,
                        manifest_path=project.manifest_path,
                        lockfile_path=project.lockfile_path,
                        actionable_evidence=evidence,
                        recommended_action=action,
                        verification_commands=(
                            f"npm audit --json --package-lock-only --prefix {PurePosixPath(project.manifest_path).parent}",
                            f"npm ls {package} --all",
                        ),
                    )
                )
    return ProjectAuditResult(
        project,
        tuple(sorted(records, key=lambda record: record.identity)),
        attributable=not ambiguous,
        error="candidate attribution is incomplete" if ambiguous else None,
    )


def classify_project(
    baseline: ProjectAuditResult,
    candidate: ProjectAuditResult,
    policy: AuditPolicy = AuditPolicy(),
) -> tuple[ClassifiedFinding, ...]:
    """Compare one project's baseline and candidate evidence and apply policy."""
    if not baseline.attributable or not candidate.attributable:
        return (
            ClassifiedFinding(
                ComparisonStatus.INDETERMINATE,
                candidate.records[0] if candidate.records else None,
                baseline.records[0] if baseline.records else None,
                candidate.error or baseline.error or "audit attribution unavailable",
                policy.block_indeterminate,
            ),
        )
    base = {record.identity: record for record in baseline.records}
    cand = {record.identity: record for record in candidate.records}
    findings: list[ClassifiedFinding] = []
    for identity in sorted(base.keys() | cand.keys()):
        old, new = base.get(identity), cand.get(identity)
        if old is None:
            status, reason = ComparisonStatus.INTRODUCED, "advisory/path introduced by candidate"
        elif new is None:
            status, reason = ComparisonStatus.REMOVED, "advisory/path removed by candidate"
        elif SEVERITY[new.severity] > SEVERITY[old.severity]:
            status, reason = ComparisonStatus.WORSENED, "severity increased"
        elif old.scope is AuditScope.DEVELOPMENT and new.scope is AuditScope.PRODUCTION:
            status, reason = (
                ComparisonStatus.WORSENED,
                "dependency scope changed from development to production",
            )
        else:
            status, reason = (
                ComparisonStatus.UNCHANGED_BASELINE,
                "present at the same or lower risk in baseline",
            )
        record = new or old
        blocks = bool(
            new
            and status in {ComparisonStatus.INTRODUCED, ComparisonStatus.WORSENED}
            and SEVERITY[new.severity] >= SEVERITY[policy.threshold]
        )
        if (
            status is ComparisonStatus.UNCHANGED_BASELINE
            and policy.block_critical_production_baseline
            and record
            and record.severity == "critical"
            and record.scope is AuditScope.PRODUCTION
            and record.exploitable
        ):
            blocks = True
        findings.append(ClassifiedFinding(status, new, old, reason, blocks))
    return tuple(findings)


async def _run_audit(snapshot: Path, project: NpmProject) -> ProjectAuditResult:
    cwd = snapshot / PurePosixPath(project.manifest_path).parent
    try:
        proc = await asyncio.create_subprocess_exec(
            "npm",
            "audit",
            "--json",
            "--package-lock-only",
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except OSError as exc:
        return ProjectAuditResult(project, attributable=False, error=str(exc))
    if proc.returncode not in (0, 1):
        return ProjectAuditResult(
            project, attributable=False, error=stderr.decode(errors="replace")[:500]
        )
    try:
        lockfile = (snapshot / project.lockfile_path).read_text()
    except OSError as exc:
        return ProjectAuditResult(project, attributable=False, error=str(exc))
    return parse_npm_audit_v2(stdout.decode(errors="replace"), project, lockfile)


async def _git_file(worktree: Path, revision: str, path: str) -> bytes | None:
    result = await git(worktree, "show", f"{revision}:{path}")
    return result.stdout.encode() if result.ok else None


async def _dependency_audit_required(
    root: Path,
    base_revision: str,
    project: NpmProject,
    changed_files: set[str],
) -> bool:
    """Whether this project changed dependency resolution inputs.

    Lockfiles always require comparison. A manifest-only change requires it
    only when a dependency-bearing section differs from the base. Missing or
    malformed evidence fails closed and retains the audit.
    """
    if project.lockfile_path in changed_files:
        return True
    if project.manifest_path not in changed_files:
        return False
    baseline = await _git_file(root, base_revision, project.manifest_path)
    try:
        candidate = (root / project.manifest_path).read_bytes()
        baseline_data = json.loads(baseline) if baseline is not None else None
        candidate_data = json.loads(candidate)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return True
    if not isinstance(baseline_data, dict) or not isinstance(candidate_data, dict):
        return True
    return any(
        baseline_data.get(section) != candidate_data.get(section)
        for section in DEPENDENCY_MANIFEST_SECTIONS
    )


async def audit_changed_npm_projects(
    worktree: str | Path,
    base_revision: str,
    changed_files: list[str],
    policy: AuditPolicy = AuditPolicy(),
) -> NpmAuditReport:
    """Audit disposable base/candidate snapshots; never run npm in the worktree."""
    root = Path(worktree).resolve()
    changed_set = set(changed_files)
    discovered = discover_npm_projects(root, changed_files)
    selected: list[NpmProject] = []
    for project in discovered:
        if await _dependency_audit_required(root, base_revision, project, changed_set):
            selected.append(project)
    projects = tuple(selected)
    findings: list[ClassifiedFinding] = []
    for project in projects:
        with tempfile.TemporaryDirectory(prefix="hyqs-npm-audit-") as temp:
            temp_root = Path(temp)
            base_root, candidate_root = temp_root / "base", temp_root / "candidate"
            base_root.mkdir()
            candidate_root.mkdir()
            complete = True
            for path in (project.manifest_path, project.lockfile_path):
                base_data = await _git_file(root, base_revision, path)
                if base_data is None:
                    complete = False
                else:
                    target = base_root / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(base_data)
                candidate = root / path
                if candidate.is_file():
                    target = candidate_root / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(candidate, target)
                else:
                    complete = False
            if complete:
                baseline, candidate = await asyncio.gather(
                    _run_audit(base_root, project), _run_audit(candidate_root, project)
                )
            else:
                baseline = ProjectAuditResult(
                    project, attributable=False, error="base or candidate npm files unavailable"
                )
                candidate = baseline
            findings.extend(classify_project(baseline, candidate, policy))
    return NpmAuditReport(projects, tuple(findings))
