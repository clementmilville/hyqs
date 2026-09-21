"""Deterministic test detection + execution — NO AI.

Per the project's primary rule (prefer deterministic automation over AI), the
TEST stage is plain detection + subprocess. We inspect the repo for a known
test setup, run it, and report pass/fail from the exit code.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import psycopg
from dotenv import dotenv_values

from . import gitops, resources

log = logging.getLogger("hyqs.testing")

TEST_TIMEOUT = 900  # seconds

REGENERABLE_LOCKFILES = ["uv.lock", "package-lock.json", "yarn.lock", "poetry.lock"]

_IMPORT_SMOKE_ENV_FILES = (".env", ".env.secrets", ".env.example")

_PLACEHOLDER_SECRET_DEFAULTS = {
    "SECRET_KEY": "hyqs-import-smoke-placeholder",
    "SESSION_SECRET": "hyqs-import-smoke-placeholder",
    "JWT_SECRET": "hyqs-import-smoke-placeholder",
    "DATABASE_URL": "sqlite:///hyqs-import-smoke.db",
    "REDIS_URL": "redis://localhost:6379/0",
}


def _benign_import_env(root: Path) -> dict[str, str]:
    """Env for the import-smoke subprocess: real config wins, placeholders fill gaps.

    Import-time fail-fast checks (e.g. ``raise`` when SECRET_KEY is unset) are
    exactly what the security gate demands, so a bare env would deadlock the
    pipeline against its own review requirement. Overlay the project's own
    .env files first, then plug any still-missing common secret vars with
    obviously-fake placeholders — enough to satisfy a presence check, not a
    real service.

    The base env is the shared minimal allowlist, not the worker's full
    ``os.environ`` — the imported module is job-controlled code and must not
    see the pipeline's own secrets (Anthropic credential, HYQS_DB_URL, etc.).
    """
    env = resources.minimal_subprocess_env()
    documented_empty: list[str] = []
    for name in _IMPORT_SMOKE_ENV_FILES:
        path = root / name
        if not path.exists():
            continue
        for key, value in dotenv_values(path).items():
            # EMPTY values mean "unset", not "set to ''": an .env.example ships
            # `SECRET_KEY=` to document the variable, and letting that empty
            # string shadow the placeholder re-created the exact fail-fast
            # deadlock this env exists to solve (job #543, round 2).
            if not value:
                documented_empty.append(key)
            elif key not in env:
                env[key] = value
    for key, value in _PLACEHOLDER_SECRET_DEFAULTS.items():
        env.setdefault(key, value)
    # Names the env files document but leave empty get a placeholder too, even
    # when they're not in the common-defaults list (e.g. CREDENTIAL_ENCRYPTION_KEY).
    for key in documented_empty:
        env.setdefault(key, "hyqs-import-smoke-placeholder")
    return env


def _uv_project_has_extras(pyproject_path: Path) -> bool:
    """True if ``pyproject_path`` declares a [project.optional-dependencies] table.

    ``uv run`` only auto-installs PEP 735 dependency-groups, not optional-
    dependencies extras, so a project that ships its test deps as an extra
    (e.g. a ``dev`` extra) needs ``--all-extras`` to have pytest available at
    collection time.
    """
    try:
        with pyproject_path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return bool(data.get("project", {}).get("optional-dependencies"))


@dataclass(frozen=True)
class TestExecution:
    """A detected test command and the directory whose environment owns it."""

    command: list[str]
    cwd: Path


def _nested_python_project(root: Path, changed_files: list[str] | None) -> Path | None:
    """Find the single nested Python project that owns the requested change set."""
    candidates = sorted(
        path.parent
        for path in root.glob("*/pyproject.toml")
        if not {".git", ".venv", "node_modules"}.intersection(path.parts)
    )
    if not candidates:
        return None
    if changed_files:
        owners = {
            candidate
            for changed in changed_files
            for candidate in candidates
            if Path(changed).parts
            and Path(changed).parts[0] == candidate.relative_to(root).parts[0]
        }
        if len(owners) == 1:
            return owners.pop()
        if len(owners) > 1:
            return None
    return candidates[0] if len(candidates) == 1 else None


def detect_test_execution(
    worktree: str | Path, changed_files: list[str] | None = None
) -> TestExecution | None:
    """Return the test command and owning project directory, if detected."""
    root = Path(worktree)

    def has(*names: str) -> bool:
        return any((root / n).exists() for n in names)

    # Makefile `test:` target wins (explicit project intent).
    makefile = root / "Makefile"
    if makefile.exists() and "test:" in makefile.read_text(errors="ignore"):
        return TestExecution(["make", "test"], root)

    # Python — prefer `uv run` so the project's deps/venv resolve.
    if has("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg") or (root / "tests").is_dir():
        pyproject = root / "pyproject.toml"
        if shutil.which("uv") and pyproject.exists():
            cmd = ["uv", "run"]
            if _uv_project_has_extras(pyproject):
                cmd.append("--all-extras")
            cmd.extend(["pytest", "-q"])
            return TestExecution(cmd, root)
        nested = _nested_python_project(root, changed_files)
        if shutil.which("uv") and nested is not None:
            cmd = ["uv", "run"]
            if _uv_project_has_extras(nested / "pyproject.toml"):
                cmd.append("--all-extras")
            cmd.extend(["pytest", "-q"])
            return TestExecution(cmd, nested)
        return TestExecution(["python", "-m", "pytest", "-q"], root)

    if (root / "package.json").exists() and shutil.which("npm"):
        return TestExecution(["npm", "test", "--silent"], root)
    if (root / "Cargo.toml").exists() and shutil.which("cargo"):
        return TestExecution(["cargo", "test"], root)
    if (root / "go.mod").exists() and shutil.which("go"):
        return TestExecution(["go", "test", "./..."], root)
    return None


def detect_test_command(worktree: str | Path) -> list[str] | None:
    """Compatibility wrapper returning only the detected repository command."""
    execution = detect_test_execution(worktree)
    return execution.command if execution is not None else None


_FORCE_FULL_SUITE_NAMES = frozenset(
    {
        "conftest.py",
        "docker-compose.yml",
        "makefile",
        "pyproject.toml",
        "pytest.ini",
        "setup.cfg",
        "tox.ini",
        "uv.lock",
    }
)
_FORCE_FULL_SUITE_PREFIXES = (".github/", ".env")
_MIGRATION_TEST_MARKERS = ("alembic", "baseline", "migration", "rls")
_DOCUMENTATION_SUFFIXES = frozenset({".md", ".rst", ".txt"})
_ROOT_DOCUMENTATION_FILES = frozenset({"agents.md", "changelog.md", "conventions.md", "readme.md"})


def _forces_full_suite(path: str) -> bool:
    """True for test infrastructure and shared config with unbounded impact."""
    if Path(path).name.lower() in _FORCE_FULL_SUITE_NAMES:
        return True
    return path.startswith(_FORCE_FULL_SUITE_PREFIXES)


def _is_documentation_path(path: str) -> bool:
    """True for prose-only files that cannot affect the executable test graph."""
    candidate = Path(path)
    if candidate.suffix.lower() not in _DOCUMENTATION_SUFFIXES:
        return False
    return candidate.name.lower() in _ROOT_DOCUMENTATION_FILES or (
        bool(candidate.parts) and candidate.parts[0].lower() in {"doc", "docs"}
    )


def _all_changed_files_are_documentation(changed_files: list[str]) -> bool:
    return bool(changed_files) and all(_is_documentation_path(path) for path in changed_files)


def _pytest_test_files(root: Path) -> list[Path]:
    """Return repository test modules, excluding dependency/build directories."""
    excluded = {".git", ".tox", ".venv", "dist", "node_modules", "site-packages"}
    return sorted(
        path
        for path in root.rglob("*.py")
        if not excluded.intersection(path.parts)
        and (path.name.startswith("test_") or path.name.endswith("_test.py"))
    )


def _scoped_pytest_files(root: Path, changed_files: list[str]) -> list[str] | None:
    """Map a Python diff to relevant pytest modules.

    Explicitly changed tests are always selected. Source modules select tests
    whose filename names that module (``client.py`` -> ``test_client_*.py``).
    Alembic revisions additionally select the repository's migration contract
    tests. If the mapping is unsafe or finds nothing, return ``None`` so the
    caller retains the full-suite gate.
    """
    if not changed_files or any(_forces_full_suite(path) for path in changed_files):
        return None

    python_changes = [Path(path) for path in changed_files if Path(path).suffix == ".py"]
    explicit_tests = {
        root / path
        for path in changed_files
        if Path(path).suffix == ".py"
        and (Path(path).name.startswith("test_") or Path(path).name.endswith("_test.py"))
        and (root / path).is_file()
    }
    non_python_backend = [
        path
        for path in changed_files
        if Path(path).suffix not in {".py", ".md", ".rst", ".txt"}
        and not any(part in {"frontend", "web"} for part in Path(path).parts)
    ]
    # Deployment artifacts commonly ship with an explicit contract test in
    # the same diff. Run that focused test during iterative TEST/FIX instead
    # of repeatedly attributing unrelated repository-wide baseline failures
    # to the job. Non-deployment infrastructure still forces the full suite.
    deploy_only_non_python = non_python_backend and all(
        Path(path).parts and Path(path).parts[0] == "deploy" for path in non_python_backend
    )
    if not python_changes or (
        non_python_backend and not (deploy_only_non_python and explicit_tests)
    ):
        return None

    tests = _pytest_test_files(root)
    selected: set[Path] = set(explicit_tests)
    stems: set[str] = set()
    migration_change = False
    for changed in python_changes:
        candidate = root / changed
        if changed.name.startswith("test_") or changed.name.endswith("_test.py"):
            if candidate.is_file():
                selected.add(candidate)
            continue
        if changed.name == "__init__.py":
            return None
        stem = changed.stem.lower()
        if "alembic" in changed.parts or "migrations" in changed.parts:
            stem = re.sub(r"^\d+_", "", stem)
        stems.add(stem)
        migration_change = (
            migration_change or "alembic" in changed.parts or "migrations" in changed.parts
        )

    for test_path in tests:
        name = test_path.stem.lower()
        if any(stem in name for stem in stems):
            selected.add(test_path)
        if migration_change and any(marker in name for marker in _MIGRATION_TEST_MARKERS):
            selected.add(test_path)

    if not selected:
        return None
    return [str(path.relative_to(root)) for path in sorted(selected)]


def _scope_pytest_command(
    command: list[str], root: Path, changed_files: list[str] | None
) -> list[str]:
    """Append deterministic test paths to pytest commands when safely mappable."""
    if not changed_files or "pytest" not in command:
        return command
    selected = _scoped_pytest_files(root, changed_files)
    return [*command, *selected] if selected else command


def _all_changed_files_under_frontend_roots(worktree: Path, changed_files: list[str]) -> bool:
    frontend_roots = _find_frontend_roots(worktree, changed_files)
    if not frontend_roots:
        return False
    return all(
        any((worktree / rel).is_relative_to(root) for root in frontend_roots)
        for rel in changed_files
    )


def _find_frontend_roots(worktree: Path, changed_files: list[str]) -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()
    for rel in changed_files:
        candidate = (worktree / rel).parent
        while True:
            try:
                candidate.relative_to(worktree)
            except ValueError:
                break
            pkg = candidate / "package.json"
            if pkg.exists():
                try:
                    data = json.loads(pkg.read_text())
                    if data.get("scripts", {}).get("build"):
                        if candidate not in seen:
                            seen.add(candidate)
                            roots.append(candidate)
                except (json.JSONDecodeError, OSError):
                    pass
                break
            if candidate == worktree:
                break
            candidate = candidate.parent
    return roots


async def _npm_scripts_allowed(worktree: Path, frontend_root: Path, base: str) -> bool:
    """Trust the ``.hyqs/allow-npm-scripts`` opt-in only if it already existed on
    the fetched base branch — never read it from the job's own unreviewed
    worktree. Otherwise a malicious diff could add both a postinstall script
    and this marker in the same PR, defeating ``--ignore-scripts`` before
    REVIEW/SECURITY ever inspect the diff.
    """
    if not base:
        return False
    marker = (frontend_root.relative_to(worktree) / ".hyqs" / "allow-npm-scripts").as_posix()
    res = await gitops.git(worktree, "cat-file", "-e", f"{base}:{marker}")
    return res.ok


async def run_frontend_build(
    worktree: str | Path,
    changed_files: list[str] | None,
) -> dict:
    """Scoped production-build gate: run npm build for frontend roots touched by the diff."""
    if not changed_files:
        return {"passed": True, "skipped": True}
    if not shutil.which("npm"):
        return {"passed": True, "skipped": True}

    root = Path(worktree)
    frontend_roots = _find_frontend_roots(root, changed_files)
    if not frontend_roots:
        return {"passed": True, "skipped": True}

    # --prefer-offline serves packages from the shared npm cache (set via
    # npm_config_cache at runner startup) instead of re-downloading per worktree.
    build_cmd = ["npm", "run", "build", "--if-present"]
    all_output: list[str] = []
    last_resource = None
    build_env = resources.minimal_subprocess_env()

    base = ""
    try:
        base = await gitops.fresh_base(root, await gitops.default_branch(root))
    except Exception:
        base = ""

    for fr in frontend_roots:
        # Lifecycle scripts (postinstall, etc.) run job-controlled code on the
        # worker before REVIEW/SECURITY ever see the diff, so they're disabled
        # by default. A project that genuinely needs them opts in by dropping
        # a marker file, mirroring this repo's existing `.hyqs/`-directory
        # config convention (see invariants.INVARIANTS_DIR) — trusted from the
        # base branch only, see _npm_scripts_allowed.
        install_cmd = ["npm", "ci", "--prefer-offline"]
        if not await _npm_scripts_allowed(root, fr, base):
            install_cmd.append("--ignore-scripts")
        log.info("installing dependencies in %s: %s", fr, " ".join(install_cmd))
        try:
            returncode, output, _record = await resources.measure_subprocess(
                install_cmd, cwd=str(fr), timeout=TEST_TIMEOUT, env=build_env
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return {
                "passed": False,
                "command": " ".join(install_cmd),
                "summary": str(e),
                "resource": None,
            }
        if returncode != 0:
            lines = output.splitlines()
            tail = "\n".join(lines[-25:])
            return {
                "passed": False,
                "command": " ".join(install_cmd),
                "summary": tail or f"npm ci exited {returncode}",
                "output": output,
                "resource": _record,
            }
        all_output.append(output)

        log.info("running frontend build in %s: %s", fr, " ".join(build_cmd))
        try:
            returncode, output, record = await resources.measure_subprocess(
                build_cmd, cwd=str(fr), timeout=TEST_TIMEOUT, env=build_env
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return {
                "passed": False,
                "command": " ".join(build_cmd),
                "summary": str(e),
                "resource": None,
            }
        all_output.append(output)
        last_resource = record
        if returncode != 0:
            combined = "\n".join(all_output).strip()
            lines = combined.splitlines()
            tail = "\n".join(lines[-25:])
            return {
                "passed": False,
                "command": " ".join(build_cmd),
                "summary": tail or f"build exited {returncode}",
                "output": combined,
                "resource": last_resource,
            }

    combined = "\n".join(all_output).strip()
    return {
        "passed": True,
        "command": " ".join(build_cmd),
        "summary": "frontend build passed",
        "output": combined,
        "resource": last_resource,
    }


async def check_lockfile_sync(worktree: str | Path) -> dict:
    """Return {passed, skipped?, command?, summary?} for the lockfile sync check.

    Runs `uv lock --check` when pyproject.toml + uv.lock are present and uv is available.
    Returns skipped=True when the repo has no lockable manifest or the tool is absent.
    """
    root = Path(worktree)
    if (root / "pyproject.toml").exists() and (root / "uv.lock").exists() and shutil.which("uv"):
        cmd = ["uv", "lock", "--check"]
        log.debug("checking lockfile sync in %s", worktree)
        try:
            returncode, output, _ = await resources.measure_subprocess(
                cmd, cwd=str(root), timeout=120, env=resources.minimal_subprocess_env()
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return {"passed": False, "command": "uv lock --check", "summary": str(e)}
        if returncode != 0:
            lines = output.strip().splitlines()
            tail = "\n".join(lines[-25:])
            return {
                "passed": False,
                "command": "uv lock --check",
                "summary": tail or f"uv lock --check exited {returncode}",
            }
        return {"passed": True, "command": "uv lock --check"}
    return {"passed": True, "skipped": True}


async def run_import_smoke(worktree: str | Path) -> dict:
    """Import the app entry module so import-time breakage fails the TEST stage.

    Test suites often never import the entrypoint's startup path, so a missing
    import merges cleanly and only explodes at deploy (ledger-app shipped a
    NameError this way). Cheap deterministic gate: `python -c "import main"`
    for repos with a top-level main.py, run with a benign env (see
    _benign_import_env) so import-time fail-fast secret checks don't turn this
    gate into a deadlock against the security review that demands them.
    Returns {passed, skipped?, command?, summary?}; skipped when there's no
    main.py or no runner available.
    """
    root = Path(worktree)
    if not (root / "main.py").exists():
        return {"passed": True, "skipped": True}
    if (root / "pyproject.toml").exists() and shutil.which("uv"):
        cmd = ["uv", "run", "python", "-c", "import main"]
    else:
        cmd = ["python3", "-c", "import main"]
    env = _benign_import_env(root)
    try:
        returncode, output, _ = await resources.measure_subprocess(
            cmd, cwd=str(root), timeout=180, env=env
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return {"passed": False, "command": " ".join(cmd), "summary": str(e)}
    if returncode != 0:
        lines = output.strip().splitlines()
        tail = "\n".join(lines[-25:])
        return {
            "passed": False,
            "command": " ".join(cmd),
            "summary": tail or f"import smoke exited {returncode}",
        }
    return {"passed": True, "command": " ".join(cmd)}


def _provision_disposable_test_database(base_dsn: str) -> tuple[str, str]:
    """Create a uniquely-named disposable Postgres database; return (isolated_dsn, db_name).

    Runs in the trusted pipeline-worker process against the worker's own DSN
    (``base_dsn``, never handed to the job-controlled pytest subprocess).
    Mirrors ``tests/conftest.py``'s ``_provision_session_database`` pattern,
    implemented independently here since ``tests/`` is not importable from
    ``hyqs/pipeline``. Raises ``psycopg.Error`` on failure — the caller
    decides whether to skip provisioning.
    """
    info = {k: v for k, v in psycopg.conninfo.conninfo_to_dict(base_dsn).items() if v is not None}
    root_dbname = info.get("dbname") or "hyqs"
    db_name = f"{root_dbname}_test_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(base_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')
    isolated_info = dict(info)
    isolated_info["dbname"] = db_name
    return psycopg.conninfo.make_conninfo(**isolated_info), db_name


def _drop_disposable_test_database(base_dsn: str, db_name: str) -> None:
    """Best-effort drop of a database created by ``_provision_disposable_test_database``."""
    try:
        with psycopg.connect(base_dsn, autocommit=True) as conn:
            try:
                conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
            except psycopg.Error:
                conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
    except psycopg.Error as e:
        log.warning("could not drop disposable test database %r: %s", db_name, e)


async def run_tests(
    worktree: str | Path,
    *,
    changed_files: list[str] | None = None,
    log_sink: Callable[[str], Awaitable[None]] | None = None,
) -> dict:
    """Detect + run tests. Returns {passed, command, summary}.

    Documentation- and frontend-only diffs skip the backend suite. Python diffs
    select relevant pytest modules deterministically; unmappable/shared changes
    retain the full suite. Calling without ``changed_files`` preserves the
    complete-suite gate for callers that cannot establish a safe diff boundary.
    """
    if changed_files and not any(_forces_full_suite(f) for f in changed_files):
        if _all_changed_files_are_documentation(changed_files):
            return {
                "passed": True,
                "skipped": True,
                "command": "(skipped: documentation-only diff)",
                "resource": None,
            }
        if _all_changed_files_under_frontend_roots(Path(worktree), changed_files):
            return {
                "passed": True,
                "skipped": True,
                "command": "(skipped: frontend-only diff)",
                "resource": None,
            }

    worktree_path = Path(worktree)
    if not worktree_path.exists() or not any(worktree_path.iterdir()):
        return {
            "passed": False,
            "command": "(none)",
            "summary": (
                "[worktree-missing] worktree is missing or empty — cannot run tests "
                "(infrastructure fault, not an untested-code gap)."
            ),
            "resource": None,
        }

    execution = detect_test_execution(worktree, changed_files)
    if execution is None:
        return {
            "passed": False,
            "command": "(none)",
            "summary": "No test suite detected — refusing to merge untested code.",
            "resource": None,
        }
    cmd = _scope_pytest_command(execution.command, Path(worktree), changed_files)
    test_cwd = execution.cwd
    if test_cwd != Path(worktree):
        prefix = f"{test_cwd.relative_to(Path(worktree)).as_posix()}/"
        cmd = [arg[len(prefix) :] if arg.startswith(prefix) else arg for arg in cmd]
    log.info("running tests in %s: %s", test_cwd, " ".join(cmd))

    # Only the pre-scoped, disposable HYQS_TEST_DB_URL is ever forwarded to
    # the job-controlled pytest subprocess. HYQS_DB_URL/DATABASE_URL (the
    # worker's own DSN, with CREATE DATABASE privileges over the shared
    # multi-tenant control plane) must never reach it — see job #4308. When
    # the caller hasn't already pinned HYQS_TEST_DB_URL, this trusted parent
    # process provisions a uniquely-named disposable database itself (using
    # its own HYQS_DB_URL/DATABASE_URL, read directly from os.environ here —
    # never handed to the subprocess) and hands the subprocess only that
    # database's isolated DSN.
    extra_env: dict[str, str] = {}
    explicit_test_db_url = os.environ.get("HYQS_TEST_DB_URL", "").strip()
    provisioned_admin_dsn: str | None = None
    provisioned_db_name: str | None = None
    if explicit_test_db_url:
        extra_env["HYQS_TEST_DB_URL"] = explicit_test_db_url
    else:
        worker_dsn = (
            os.environ.get("HYQS_DB_URL", "").strip() or os.environ.get("DATABASE_URL", "").strip()
        )
        if worker_dsn:
            try:
                isolated_dsn, db_name = _provision_disposable_test_database(worker_dsn)
            except psycopg.Error as e:
                log.warning("could not provision disposable test database: %s", e)
            else:
                extra_env["HYQS_TEST_DB_URL"] = isolated_dsn
                provisioned_admin_dsn = worker_dsn
                provisioned_db_name = db_name

    try:
        try:
            returncode, output, record = await resources.measure_subprocess(
                cmd,
                cwd=str(test_cwd),
                timeout=TEST_TIMEOUT,
                log_sink=log_sink,
                env=resources.minimal_subprocess_env(extra=extra_env),
            )
            passed = returncode == 0
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            return {
                "passed": False,
                "command": " ".join(cmd),
                "summary": f"could not run tests: {e}",
                "resource": None,
            }
    finally:
        if provisioned_db_name is not None and provisioned_admin_dsn is not None:
            _drop_disposable_test_database(provisioned_admin_dsn, provisioned_db_name)

    lines = output.strip().splitlines()
    tail = "\n".join(lines[-25:])
    full = "\n".join(lines[-400:])
    timed_out = returncode != 0 and output.rstrip().endswith(f"[timed out after {TEST_TIMEOUT}s]")
    return {
        "passed": passed,
        "timed_out": timed_out,
        "command": " ".join(cmd),
        "summary": tail or "(no output)",
        "output": full or "(no output)",
        "resource": record,
    }
