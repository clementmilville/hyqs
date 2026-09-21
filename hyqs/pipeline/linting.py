"""Deterministic lint + format detection and execution — NO AI.

Per the project's control-plane rule, this stage is pure detection + subprocess.
Config lives in each project's repo, never global.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Awaitable, Callable

from . import gitops, resources

log = logging.getLogger("hyqs.linting")

LINT_TIMEOUT = 300  # seconds per command

# A lint command that exits non-zero for an ENVIRONMENT reason (the linter can't
# run: missing package, bad config, not installed, timeout) is NOT a code-quality
# failure — it must not fail the job or be fed to the fix agent, which cannot fix
# a missing npm module by editing source. eslint/ruff signal genuine violations
# with exit code 1; a fatal tool/config error is exit >=2 or one of these markers.
_TOOL_FAILURE_MARKERS = (
    "err_module_not_found",
    "cannot find package",
    "cannot find module",
    "command not found",
    "failed to find executable",
    "oops! something went wrong",
    "no configuration found",
    "could not run",  # emitted by _run_cmd on OSError
    "timed out after",  # emitted by _run_cmd on timeout
)


def _is_tool_failure(exit_code: int, output: str) -> bool:
    """True when a non-zero lint exit means the linter itself failed to run
    (environment/config problem), rather than reporting real violations."""
    if exit_code >= 2:  # eslint fatal / ruff internal error (violations are exit 1)
        return True
    low = output.lower()
    return any(m in low for m in _TOOL_FAILURE_MARKERS)


def _detect_entries(worktree: Path, changed_files: list[str] | None = None) -> list[dict]:
    """Internal: return list of {cwd, format, fix, lint} dicts for each detected toolset.

    When ``changed_files`` (repo-relative paths) is given, the gate is scoped to
    just those files — a job is responsible for the files it changed, NOT the
    codebase's accumulated debt. Scoping is what stops a whole-repo ``ruff check .``
    from failing every job over pre-existing errors it never introduced. Pass
    ``None`` to lint the whole tree (back-compat / non-pipeline callers).

    ``fix`` runs between ``format`` and ``lint`` for Python (ruff) entries only:
    ``ruff check --fix`` applies safe fixes (never ``--unsafe-fixes``), so the
    final ``lint`` command reports only diagnostics ruff couldn't fix itself.
    JS/TS entries get an empty ``fix`` list — no auto-fix behavior there.
    """
    entries: list[dict] = []

    def _scoped(exts: set[str], base_dir: Path | None = None) -> list[str]:
        """Absolute paths among changed_files matching exts (and under base_dir)."""
        out: list[str] = []
        for f in changed_files or []:
            p = worktree / f
            if p.suffix not in exts or not p.exists():
                continue
            if base_dir is not None and base_dir not in p.parents:
                continue
            out.append(str(p))
        return out

    # Python: ruff when pyproject.toml declares [tool.ruff]
    pyproject = worktree / "pyproject.toml"
    if pyproject.exists() and "[tool.ruff]" in pyproject.read_text(errors="ignore"):
        ruff = ["uv", "run", "ruff"] if shutil.which("uv") else ["ruff"]
        if changed_files is None:
            fmt = [[*ruff, "format", "."]]
            fix = [[*ruff, "check", "--fix", "."]]
            lint = [[*ruff, "check", "."]]
        else:
            py = _scoped({".py"})
            fmt = [[*ruff, "format", *py]] if py else []
            fix = [[*ruff, "check", "--fix", *py]] if py else []
            lint = [[*ruff, "check", *py]] if py else []
        if fmt or fix or lint:
            entries.append({"cwd": str(worktree), "format": fmt, "fix": fix, "lint": lint})

    # JS/TS: prettier + eslint, one entry per package.json found
    if shutil.which("npx"):
        for pkg_json in worktree.rglob("package.json"):
            d = pkg_json.parent
            if "node_modules" in d.parts:
                continue
            has_prettier = any(d.glob(".prettierrc*")) or any(d.glob("prettier.config.*"))
            has_eslint = any(d.glob("eslint.config.*")) or any(d.glob(".eslintrc*"))
            fmt_cmds: list[list[str]] = []
            lint_cmds: list[list[str]] = []
            if changed_files is None:
                if has_prettier:
                    fmt_cmds.append(["npx", "prettier", "--write", "src/"])
                if has_eslint:
                    lint_cmds.append(["npx", "eslint", "src/"])
            else:
                js = _scoped({".js", ".jsx", ".ts", ".tsx"}, base_dir=d)
                if has_prettier and js:
                    fmt_cmds.append(["npx", "prettier", "--write", *js])
                if has_eslint and js:
                    lint_cmds.append(["npx", "eslint", *js])
            if fmt_cmds or lint_cmds:
                entries.append({"cwd": str(d), "format": fmt_cmds, "fix": [], "lint": lint_cmds})

    return entries


async def _run_cmd(
    cmd: list[str],
    *,
    cwd: str,
    log_sink: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[int, str, "resources.ResourceRecord | None"]:
    """Run one command; return (exit_code, combined_stdout+stderr, resource_record)."""
    try:
        return await resources.measure_subprocess(
            cmd,
            cwd=cwd,
            timeout=LINT_TIMEOUT,
            log_sink=log_sink,
            env=resources.minimal_subprocess_env(),
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        return 1, f"could not run {' '.join(cmd)}: {e}", None


async def run_lint(
    worktree: str | Path,
    *,
    changed_files: list[str] | None = None,
    log_sink: Callable[[str], Awaitable[None]] | None = None,
) -> dict:
    """Detect + run format, safe-fix, then lint, scoped to ``changed_files`` when provided.

    Returns {passed, auto_fixed, summary, output, format_commands, fix_commands,
    lint_commands}. See ``_detect_entries`` for the scoping contract: passing the
    job's changed files keeps pre-existing whole-repo debt from failing the job.
    """
    root = Path(worktree)
    entries = _detect_entries(root, changed_files)

    format_commands: list[list[str]] = []
    fix_commands: list[list[str]] = []
    lint_commands: list[list[str]] = []
    for e in entries:
        format_commands.extend(e["format"])
        fix_commands.extend(e["fix"])
        lint_commands.extend(e["lint"])

    if not format_commands and not fix_commands and not lint_commands:
        return {
            "passed": True,
            "auto_fixed": False,
            "summary": "(no lint config detected or no changed files to lint)",
            "output": "",
            "format_commands": [],
            "fix_commands": [],
            "lint_commands": [],
            "resource": None,
        }

    buf: list[str] = []
    auto_fixed = False
    aggregate_record: "resources.ResourceRecord | None" = None

    # Run format commands; ignore exit code; capture output
    for entry in entries:
        for cmd in entry["format"]:
            _, out, rec = await _run_cmd(cmd, cwd=entry["cwd"])
            buf.extend(out.splitlines())
            if "reformatted" in out:  # ruff: "1 file reformatted"
                auto_fixed = True
            if rec is not None:
                aggregate_record = (aggregate_record + rec) if aggregate_record is not None else rec

    # Run safe-fix commands (ruff check --fix, never --unsafe-fixes); ignore exit
    # code — the plain `lint` check below is authoritative for pass/fail. ruff
    # doesn't print "reformatted" here even when it mutates files (e.g. an
    # import-only fix), so mutation detection for this step relies entirely on
    # the git-status fallback below.
    for entry in entries:
        for cmd in entry["fix"]:
            _, out, rec = await _run_cmd(cmd, cwd=entry["cwd"])
            buf.extend(out.splitlines())
            if rec is not None:
                aggregate_record = (aggregate_record + rec) if aggregate_record is not None else rec

    # Formatters/fixers that don't print a mutation marker (e.g. prettier, or
    # ruff --fix on an import-only fix) still touch tracked files — fall back
    # to a git-status check to catch those.
    if not auto_fixed:
        try:
            auto_fixed = await gitops.has_changes(root)
        except Exception:  # noqa: BLE001 - not a git repo in tests, etc.
            pass

    # Run lint commands; OR exit codes. A tool/environment failure (linter can't
    # run) is skipped with a warning rather than failing the job — feeding it to
    # the fix agent is futile (it can't `npm install` by editing code).
    all_passed = True
    tool_failures: list[str] = []
    for entry in entries:
        for cmd in entry["lint"]:
            exit_code, out, rec = await _run_cmd(cmd, cwd=entry["cwd"], log_sink=log_sink)
            buf.extend(out.splitlines())
            if rec is not None:
                aggregate_record = (aggregate_record + rec) if aggregate_record is not None else rec
            if exit_code == 0:
                continue
            if _is_tool_failure(exit_code, out):
                tool_failures.append(" ".join(cmd))
                log.warning(
                    "lint: %s could not run (tool/env error, exit %s); skipping",
                    " ".join(cmd),
                    exit_code,
                )
                continue
            all_passed = False

    tail = "\n".join(buf[-25:])
    full = "\n".join(buf[-400:])

    return {
        "passed": all_passed,
        "auto_fixed": auto_fixed,
        "summary": tail or "(no output)",
        "output": full or "(no output)",
        "format_commands": format_commands,
        "fix_commands": fix_commands,
        "lint_commands": lint_commands,
        "tool_failures": tool_failures,
        "resource": aggregate_record,
    }
