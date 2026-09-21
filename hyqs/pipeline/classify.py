"""Deterministic diff classifier: changed files -> domains, no AI involved.

Used to select which specialist checklists (see :mod:`hyqs.pipeline.personas`) get
appended to the REVIEW / SECURITY / DESIGN_REVIEW prompts. Path globs/regex only —
this must stay cheap and side-effect-free so it can run on every gate invocation.
"""

from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path

UI_EXTS = {".jsx", ".tsx", ".vue", ".css", ".html"}

_DB_PATH_RE = re.compile(r"(^|/)(migrations|alembic)(/|$)")
_DB_MODEL_RE = re.compile(
    r"^\+.*class\s+\w+\s*\(\s*(?:\w+\.)*(?:SQLModel|Base|DeclarativeBase|Model)\b", re.MULTILINE
)
_AUTHZ_PATH_RE = re.compile(r"(^|/)(auth|session|crypto|oauth)\w*(/|\.|$)")
_AUTHZ_KEYWORD_RE = re.compile(r"password|token|secret|permission", re.IGNORECASE)
_INFRA_PATH_RE = re.compile(r"(^|/)(docker-compose|Dockerfile|nginx)\w*|(^|/)deploy/")
_MIGRATION_MARKER_RE = re.compile(r"^\+.*(_run_.*migration|on_startup)", re.MULTILINE)


def _is_db_path(path: str) -> bool:
    return bool(_DB_PATH_RE.search(path)) or path.endswith(".sql")


def _is_frontend_path(path: str) -> bool:
    return Path(path).suffix in UI_EXTS


def _is_authz_path(path: str) -> bool:
    return (
        bool(_AUTHZ_PATH_RE.search(path))
        or "middleware" in path.lower()
        or bool(_AUTHZ_KEYWORD_RE.search(path))
    )


def _is_infra_path(path: str) -> bool:
    return bool(_INFRA_PATH_RE.search(path))


def classify_diff(changed_files: list[str], diff_text: str = "") -> set[str]:
    """Return the subset of {db, frontend, authz, infra} touched by this diff.

    Fail-open: never raises. Malformed/empty input yields an empty set.
    """
    domains: set[str] = set()
    for path in changed_files or []:
        if not isinstance(path, str):
            continue
        if _is_db_path(path):
            domains.add("db")
        if _is_frontend_path(path):
            domains.add("frontend")
        if _is_authz_path(path):
            domains.add("authz")
        if _is_infra_path(path):
            domains.add("infra")
    if diff_text and _DB_MODEL_RE.search(diff_text):
        domains.add("db")
    return domains


def is_schema_touching(
    changed_files: list[str], diff_text: str = "", extra_patterns: list[str] | None = None
) -> bool:
    """True if this diff touches schema/migration surface — used to serialize
    schema jobs per project (see the ``schema_locks`` gate in stages/merge.py).

    Matches: a file literally named ``models.py``; anything under a
    ``migrations/`` or ``alembic/`` directory; ``*.sql`` files; ``main.py``
    when ``diff_text`` has an added line matching ``_run_.*migration`` or
    ``on_startup``; plus any caller-supplied glob override in
    ``extra_patterns`` (a project's ``schema_paths`` config).

    Fail-open: never raises. Malformed/empty input yields False.
    """
    for path in changed_files or []:
        if not isinstance(path, str):
            continue
        name = Path(path).name
        if name == "models.py" or path.endswith(".sql") or _DB_PATH_RE.search(path):
            return True
        if name == "main.py" and diff_text and _MIGRATION_MARKER_RE.search(diff_text):
            return True
        if extra_patterns and any(fnmatch(path, pattern) for pattern in extra_patterns):
            return True
    return False
