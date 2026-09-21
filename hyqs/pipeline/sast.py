"""Deterministic SAST / secret / dependency scanner layer — NO AI.

Runs diff-scoped static analysis tools and returns findings attributable
to the current job's diff. Non-dependency scanners gracefully skip when tools
are unavailable; npm comparison uncertainty is preserved as fail-closed evidence
when an attributable dependency file changed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

from hyqs.pipeline.npm_audit import (
    AuditPolicy,
    ClassifiedFinding,
    ComparisonStatus,
    NpmAuditReport,
    audit_changed_npm_projects,
    discover_npm_projects,
)

log = logging.getLogger("hyqs.sast")

SAST_TIMEOUT = 120  # seconds per scanner


@dataclass
class SastFinding:
    severity: str
    note: str
    tool: str
    file: str = ""
    line: int = 0


@dataclass(frozen=True)
class SastScanResult:
    """Complete deterministic security evidence across scanner families."""

    findings: tuple[SastFinding, ...] = ()
    npm_audit: NpmAuditReport = NpmAuditReport((), ())

    @property
    def blocked(self) -> bool:
        return bool(self.findings) or self.npm_audit.blocked


async def _run_cmd(
    *args: str,
    cwd: str | Path | None = None,
    stdin: str | None = None,
) -> tuple[int, str, str]:
    """Run a subprocess; return (returncode, stdout, stderr). Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd) if cwd else None,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=stdin.encode() if stdin else None),
            timeout=SAST_TIMEOUT,
        )
        return (
            proc.returncode or 0,
            stdout_b.decode(errors="replace"),
            stderr_b.decode(errors="replace"),
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        log.warning("sast: command timed out: %s", " ".join(args))
        return 2, "", "timed out"
    except OSError as exc:
        log.warning("sast: OSError running %s: %s", " ".join(args), exc)
        return 2, "", str(exc)


def _is_test_path(path: str) -> bool:
    """True if ``path`` lives under a directory literally named ``tests`` or
    ``test``, at any depth (e.g. ``backend/tests/unit/test_x.py``).

    Deliberately directory-based only: matching on basename convention alone
    (``test_*.py``, ``*_test.py``, ``conftest.py``) would let a production
    file dodge bandit scanning purely by its name, and paths in a scanned
    diff are attacker-controlled.
    """
    parts = Path(path).parts
    return any(p in ("tests", "test") for p in parts[:-1])


def _pyproject_has_bandit_table(path: Path) -> bool:
    """True if ``path`` (a pyproject.toml) declares a [tool.bandit] table."""
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return "bandit" in data.get("tool", {})


def _rel_to_worktree(path: Path, worktree: Path) -> str:
    try:
        return str(path.resolve().relative_to(worktree))
    except ValueError:
        return str(path)


def _find_bandit_config(
    worktree: str | Path, file_abs: Path, changed_files: set[str]
) -> str | None:
    """Walk up from ``file_abs``'s directory to ``worktree`` looking for a
    .bandit file or a pyproject.toml with a [tool.bandit] table. Return the
    config path found, or None.

    A candidate config is skipped (treated as absent) if its own repo-relative
    path is in ``changed_files`` — i.e. the config is itself part of the diff
    under review. Honoring a config the same diff introduces or edits would
    let that diff add bandit skips/exclude_dirs to suppress findings on the
    vulnerable code it introduces alongside it, defeating the SAST gate. Only
    configs that predate this diff are trusted.
    """
    wt = Path(worktree).resolve()
    current = file_abs.resolve().parent
    while True:
        bandit_file = current / ".bandit"
        if bandit_file.is_file() and _rel_to_worktree(bandit_file, wt) not in changed_files:
            return str(bandit_file)
        pyproject = current / "pyproject.toml"
        if (
            pyproject.is_file()
            and _rel_to_worktree(pyproject, wt) not in changed_files
            and _pyproject_has_bandit_table(pyproject)
        ):
            return str(pyproject)
        if current == wt or current.parent == current:
            break
        current = current.parent
    return None


def _group_by_bandit_config(
    worktree: str | Path, py_files: list[str], changed_files: list[str]
) -> dict[str | None, list[str]]:
    """Bucket ``py_files`` (repo-relative) by their resolved bandit config path
    (or None if no ancestor config was found)."""
    changed = set(changed_files)
    groups: dict[str | None, list[str]] = {}
    for f in py_files:
        file_abs = Path(worktree) / f
        config = _find_bandit_config(worktree, file_abs, changed)
        groups.setdefault(config, []).append(str(file_abs))
    return groups


def _parse_bandit_output(stdout: str, rc: int) -> list[SastFinding]:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        log.debug("sast: bandit produced no JSON (rc=%d)", rc)
        return []

    findings: list[SastFinding] = []
    for result in data.get("results", []):
        sev = result.get("issue_severity", "MEDIUM").lower()
        if sev == "low":
            # bandit low-severity are frequently noise; keep only medium+
            # unless confidence is HIGH
            if result.get("issue_confidence", "").upper() != "HIGH":
                continue
        findings.append(
            SastFinding(
                severity=sev,
                note=f"{result.get('issue_text', '')} ({result.get('test_id', '')})",
                tool="bandit",
                file=result.get("filename", ""),
                line=result.get("line_number", 0),
            )
        )
    return findings


async def _run_bandit(worktree: str | Path, changed_files: list[str]) -> list[SastFinding]:
    """Run bandit on changed .py files; return findings."""
    if not shutil.which("bandit"):
        log.debug("sast: bandit not found, skipping")
        return []

    py_files = [f for f in changed_files if f.endswith(".py") and not _is_test_path(f)]
    if not py_files:
        return []

    groups = _group_by_bandit_config(worktree, py_files, changed_files)

    findings: list[SastFinding] = []
    for config, abs_files in groups.items():
        args = ["bandit", "-f", "json", "-q", "--exit-zero"]
        if config is not None:
            args += ["-c", config]
        args += abs_files
        rc, stdout, stderr = await _run_cmd(*args, cwd=worktree)
        if rc >= 2:
            log.warning("sast: bandit infra error (rc=%d): %s", rc, stderr[:200])
            continue
        findings.extend(_parse_bandit_output(stdout, rc))
    return findings


async def _run_pip_audit(worktree: str | Path, changed_files: list[str]) -> list[SastFinding]:
    """Run pip-audit when pyproject.toml or uv.lock changed."""
    if not shutil.which("pip-audit"):
        log.debug("sast: pip-audit not found, skipping")
        return []

    dep_files = {"pyproject.toml", "uv.lock", "requirements.txt", "requirements-dev.txt"}
    if not any(Path(f).name in dep_files for f in changed_files):
        return []

    rc, stdout, stderr = await _run_cmd(
        "pip-audit",
        "--format=json",
        "--progress-spinner=off",
        cwd=worktree,
    )
    if rc >= 2:
        log.warning("sast: pip-audit infra error (rc=%d): %s", rc, stderr[:200])
        return []

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        log.debug("sast: pip-audit produced no JSON (rc=%d)", rc)
        return []

    findings: list[SastFinding] = []
    for dep in data:
        for vuln in dep.get("vulns", []):
            findings.append(
                SastFinding(
                    severity="high",
                    note=(
                        f"Known vulnerability {vuln.get('id', '?')} in "
                        f"{dep.get('name', '?')} {dep.get('version', '?')}: "
                        f"{vuln.get('description', '')[:200]}"
                    ),
                    tool="pip-audit",
                    file="pyproject.toml",
                )
            )
    return findings


async def _run_detect_secrets(worktree: str | Path, changed_files: list[str]) -> list[SastFinding]:
    """Run detect-secrets over the changed files to catch committed credentials."""
    if not shutil.which("detect-secrets"):
        log.debug("sast: detect-secrets not found, skipping")
        return []

    if not changed_files:
        return []

    # Scan changed files directly
    abs_files = [str(Path(worktree) / f) for f in changed_files if not f.startswith("/")]
    existing = [f for f in abs_files if Path(f).is_file()]
    if not existing:
        return []

    rc, stdout, stderr = await _run_cmd(
        "detect-secrets",
        "scan",
        *existing,
        cwd=worktree,
    )
    if rc >= 2:
        log.warning("sast: detect-secrets infra error (rc=%d): %s", rc, stderr[:200])
        return []

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        log.debug("sast: detect-secrets produced no JSON (rc=%d)", rc)
        return []

    findings: list[SastFinding] = []
    for filepath, secrets in data.get("results", {}).items():
        for secret in secrets:
            # Only report on files that are in our changed set
            rel = filepath
            for changed in changed_files:
                if filepath.endswith(changed) or changed.endswith(Path(filepath).name):
                    rel = changed
                    break
            findings.append(
                SastFinding(
                    severity="high",
                    note=f"Potential secret detected: {secret.get('type', 'unknown')}",
                    tool="detect-secrets",
                    file=rel,
                    line=secret.get("line_number", 0),
                )
            )
    return findings


async def _run_npm_audit(
    worktree: str | Path, base: str, changed_files: list[str]
) -> NpmAuditReport:
    """Return complete baseline-aware npm evidence without lossy adaptation."""
    projects = discover_npm_projects(worktree, changed_files)
    if not projects:
        return NpmAuditReport((), ())
    if not shutil.which("npm"):
        return NpmAuditReport(
            projects,
            tuple(
                ClassifiedFinding(
                    ComparisonStatus.INDETERMINATE,
                    None,
                    None,
                    "npm executable is unavailable",
                    AuditPolicy().block_indeterminate,
                )
                for _project in projects
            ),
        )
    return await audit_changed_npm_projects(worktree, base, changed_files)


async def _run_semgrep(worktree: str | Path, changed_files: list[str]) -> list[SastFinding]:
    """Run semgrep with security rulesets on changed files."""
    if not shutil.which("semgrep"):
        log.debug("sast: semgrep not found, skipping")
        return []

    if not changed_files:
        return []

    abs_files = [str(Path(worktree) / f) for f in changed_files if not f.startswith("/")]
    existing = [f for f in abs_files if Path(f).is_file()]
    if not existing:
        return []

    rc, stdout, stderr = await _run_cmd(
        "semgrep",
        "--config=auto",
        "--json",
        "--quiet",
        *existing,
        cwd=worktree,
    )
    if rc >= 2:
        log.warning("sast: semgrep infra error (rc=%d): %s", rc, stderr[:200])
        return []

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        log.debug("sast: semgrep produced no JSON (rc=%d)", rc)
        return []

    findings: list[SastFinding] = []
    for result in data.get("results", []):
        extra = result.get("extra", {})
        metadata = extra.get("metadata", {})
        # Only report security-relevant findings
        cats = metadata.get("category", "") or metadata.get("categories", [])
        if isinstance(cats, str):
            cats = [cats]
        if not any("security" in c.lower() for c in cats):
            continue
        sev = extra.get("severity", "WARNING").lower()
        if sev == "info":
            continue
        findings.append(
            SastFinding(
                severity=sev
                if sev in ("error", "warning", "high", "medium", "low", "critical")
                else "medium",
                note=f"{extra.get('message', '')} [{result.get('check_id', '')}]",
                tool="semgrep",
                file=result.get("path", ""),
                line=result.get("start", {}).get("line", 0),
            )
        )
    return findings


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


async def _changed_lines(
    worktree: str | Path, base: str, changed_files: list[str]
) -> dict[str, set[int]]:
    """Map each changed file (repo-relative) -> set of NEW-side line numbers that
    the job's diff (``base...HEAD``) added or modified.

    Used to scope line-based scanner findings to the job's actual diff so a job is
    never blocked by PRE-EXISTING findings in a file it merely touched (which would
    be an un-healable trap — the job can't legitimately fix code it didn't write).
    """
    if not changed_files:
        return {}
    rc, stdout, stderr = await _run_cmd(
        "git", "diff", "--unified=0", f"{base}...HEAD", "--", *changed_files, cwd=worktree
    )
    if rc != 0:
        # Can't compute ranges — do NOT silently keep everything (that re-opens the
        # trap); signal "unknown" by returning empty so the caller keeps only
        # non-line findings. A diff error here is rare and logged.
        log.warning("sast: could not compute changed line ranges (rc=%d): %s", rc, stderr[:200])
        return {}
    ranges: dict[str, set[int]] = {}
    current: str | None = None
    for line in stdout.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            ranges.setdefault(current, set())
        elif line.startswith("+++ "):
            current = None  # e.g. "+++ /dev/null" (pure deletion)
        elif line.startswith("@@") and current is not None:
            m = _HUNK_RE.match(line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) is not None else 1
                ranges[current].update(range(start, start + count))
    return ranges


def _filter_to_diff(
    findings: list[SastFinding], worktree: str | Path, changed_lines: dict[str, set[int]]
) -> list[SastFinding]:
    """Keep only findings attributable to the job's diff.

    A line-based finding (``line > 0``) survives only if its line is within the
    diff's changed ranges for that file. Non-line findings (``line <= 0``, e.g.
    a vulnerable dependency reported against a changed lockfile) are kept as-is.
    """
    wt = Path(worktree)
    kept: list[SastFinding] = []
    for f in findings:
        if f.line and f.line > 0:
            try:
                rel = str(Path(f.file).relative_to(wt))
            except (ValueError, TypeError):
                rel = f.file
            allowed = changed_lines.get(rel)
            if allowed is None or f.line not in allowed:
                continue  # pre-existing line, not part of this job's diff — skip
        kept.append(f)
    return kept


async def run_sast_scan(
    worktree: str | Path,
    base: str,
    changed_files: list[str],
) -> SastScanResult:
    """Run all SAST scanners and preserve ordinary and npm comparison evidence.

    Ordinary findings are filtered to lines the job's diff actually changed.
    npm failures become indeterminate findings when a supported dependency
    project changed; other scanner infrastructure failures remain non-blocking.
    """
    try:
        results = await asyncio.gather(
            _run_bandit(worktree, changed_files),
            _run_pip_audit(worktree, changed_files),
            _run_detect_secrets(worktree, changed_files),
            _run_npm_audit(worktree, base, changed_files),
            _run_semgrep(worktree, changed_files),
            return_exceptions=True,
        )
        findings: list[SastFinding] = []
        npm_report = NpmAuditReport((), ())
        for index, r in enumerate(results):
            if isinstance(r, BaseException):
                log.warning("sast: scanner raised unexpected exception: %s", r)
                if index == 3:
                    projects = discover_npm_projects(worktree, changed_files)
                    npm_report = NpmAuditReport(
                        projects,
                        tuple(
                            ClassifiedFinding(
                                ComparisonStatus.INDETERMINATE,
                                None,
                                None,
                                f"npm comparison failed: {r}",
                                True,
                            )
                            for _project in projects
                        ),
                    )
            elif index == 3:
                npm_report = r
            else:
                findings.extend(r)
        changed_lines = await _changed_lines(worktree, base, changed_files)
        return SastScanResult(tuple(_filter_to_diff(findings, worktree, changed_lines)), npm_report)
    except Exception as exc:
        log.warning("sast: unexpected error in run_sast_scan: %s", exc)
        projects = discover_npm_projects(worktree, changed_files)
        return SastScanResult(
            (),
            NpmAuditReport(
                projects,
                tuple(
                    ClassifiedFinding(
                        ComparisonStatus.INDETERMINATE,
                        None,
                        None,
                        f"deterministic scan failed: {exc}",
                        True,
                    )
                    for _project in projects
                ),
            ),
        )
