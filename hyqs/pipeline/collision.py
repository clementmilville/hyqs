"""Deterministic symbol-collision gate.

M1 PRINCIPLE — substantiate every finding against POST-BUILD reality, never a
stale snapshot.  The gate examines the post-build worktree *only*.  A symbol
that appears in exactly one module post-build cannot be a collision regardless
of what the baseline index says — it may be a MOVE (deleted from A, added to
B).  Only a name duplicated across two or more *different* non-conventional
modules in the post-build worktree can be a collision, and only when at least
one of those modules is *new* (absent from the baseline for that name), which
means the job introduced the duplication.

MOVE-AWARE semantics (fixes the false-positive from Job #76)
------------------------------------------------------------
Job #76 moved ``useRole`` from App.jsx into rbac.jsx.  Under the old baseline-
comparison logic the gate saw ``rbac`` was not in the baseline and flagged it.
Under M1: post-build has ``useRole`` in exactly one module (``rbac``) so it
passes immediately — regardless of what the baseline says.

Convention allowlist
--------------------
Even a *newly added* module legitimately reuses certain names: a bare
top-level ``main`` in *any* module, ``run`` in a ``__main__`` entrypoint,
helper/fixture names in test modules, and ``run`` in any leaf module under
``*.pipeline.stages.*`` (the stage-handler dispatch convention — every
pipeline stage exposes exactly this signature so the runner can dispatch
it). Alembic revision modules under an exact ``alembic.versions`` package
also conventionally expose ``upgrade`` and ``downgrade``. Those are exempt
regardless of the baseline so a first-of-its-kind entrypoint, test file,
pipeline stage, or Alembic revision never trips the gate.

``main`` is exempted everywhere, not just in ``__main__`` modules, because it
is a universal CLI-entrypoint convention: ``hyqs/__main__.py``,
``hyqs/pipeline/__main__.py``, and ``hyqs/pipeline/migrate_sqlite.py`` (a
console-script entry point in pyproject.toml) each define their own bare
top-level ``def main() -> None:``, and every one of them is only ever invoked
via ``if __name__ == "__main__":`` or a console-script shim — never
cross-imported. Two independent ``main()`` functions can therefore never be
the copy-paste coherence bug this gate exists to catch.
"""

from __future__ import annotations

import ast
import heapq
import re
from dataclasses import asdict, dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterable

from .symbols import extract_symbols

SCOPE_CHECK_UNEXPECTED_LIMIT = 50
PLANNING_CANDIDATE_LIMIT = 40
SCOPE_COMPLETENESS_EVIDENCE_LIMIT = 20
SCOPE_AMENDMENT_PER_CYCLE_LIMIT = 8
SCOPE_AMENDMENT_CUMULATIVE_LIMIT = 24

AUTHORIZED_AMENDMENT_GATES = frozenset({"test", "lint", "security", "design_review"})
"""The complete set of gates permitted to authorize a scope amendment."""

AUTHORIZED_GATE_EVIDENCE_CATEGORIES = frozenset(
    {
        "coverage",
        "coverage_paths",
        "import",
        "imports",
        "import_paths",
        "symbol",
        "symbols",
        "symbol_paths",
    }
)
"""Deterministic evidence kinds accepted by scope-amendment policy."""
_GLOB_CHARS = frozenset("*?[]{}")
_SENSITIVE_PARTS = frozenset(
    {
        "alembic",
        "migrations",
        "migration",
        "auth",
        "authentication",
        "authorization",
        "deploy",
        "deployment",
        "security",
        "credentials",
        "credential",
        "secrets",
        "secret",
        "config",
        "configuration",
    }
)
_SENSITIVE_NAMES = frozenset(
    {
        ".env",
        ".env.example",
        "alembic.ini",
        "docker-compose.yml",
        "docker-compose.yaml",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "poetry.lock",
        "uv.lock",
        "cargo.lock",
        "gemfile.lock",
        "composer.lock",
        "go.sum",
        "pyproject.toml",
        "package.json",
        "requirements.txt",
        "requirements-dev.txt",
    }
)


@dataclass(frozen=True)
class ScopeAmendmentAccepted:
    """One newly authorized concrete path and its deterministic provenance."""

    path: str
    provenance: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"path": self.path, "provenance": list(self.provenance)}


@dataclass(frozen=True)
class ScopeAmendmentRejected:
    """One bounded scope-amendment rejection with a stable machine reason."""

    candidate: str
    reason_code: str

    def to_dict(self) -> dict:
        return {"candidate": self.candidate, "reason_code": self.reason_code}


@dataclass(frozen=True)
class ScopeAmendmentDecision:
    """Immutable result of evaluating one proposed scope-amendment cycle."""

    accepted: tuple[ScopeAmendmentAccepted, ...]
    rejected: tuple[ScopeAmendmentRejected, ...]

    @property
    def accepted_paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.accepted)

    def to_dict(self) -> dict:
        return {
            "accepted": [item.to_dict() for item in self.accepted],
            "accepted_paths": list(self.accepted_paths),
            "rejected": [item.to_dict() for item in self.rejected],
        }


_CANDIDATE_PRIORITY = {
    "explicit_path": 0,
    "parent_manifest": 1,
    "covered_story": 2,
    "rejected_diff": 3,
    "failure_path": 4,
    "traceback_path": 5,
    "reviewer_path": 6,
    "explicit_symbol": 7,
    "nearest_test": 8,
    "model_migration": 9,
    "package_export": 10,
    "indexed_caller": 11,
    "execution_layer": 12,
    "persistence_layer": 13,
    "requeue_layer": 14,
    "explicit_new": 15,
    "explicit_reference": 16,
}

_REUSE_ONLY_REASONS = {"explicit_symbol", "indexed_caller", "explicit_reference"}


@dataclass(frozen=True)
class PlanningCandidate:
    """One validated, ranked path offered to PLAN as focused evidence."""

    path: str
    status: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"path": self.path, "status": self.status, "reasons": list(self.reasons)}


_COMPLETENESS_REASON_BY_PROVENANCE = {
    "persistence_layer": "persistence_omission",
    "requeue_layer": "lifecycle_requeue_omission",
    "model_migration": "migration_model_omission",
    "package_export": "model_export_omission",
    "indexed_caller": "caller_integration_omission",
    "nearest_test": "regression_test_omission",
    "execution_layer": "recovery_omission",
}


@dataclass(frozen=True)
class ScopeCompletenessResult:
    """Repository-aware validation of a PLAN's complete, normalized file manifest."""

    manifest: tuple[str, ...]
    reason_codes: tuple[str, ...]
    missing_paths: tuple[str, ...]
    candidate_paths: tuple[str, ...]
    provenance: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.reason_codes

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "manifest": list(self.manifest),
            "reason_codes": list(self.reason_codes),
            "missing_paths": list(self.missing_paths),
            "candidate_paths": list(self.candidate_paths),
            "provenance": list(self.provenance),
        }


def validate_plan_scope_completeness(
    plan: dict,
    tracked_files: list[str],
    candidates: list[PlanningCandidate],
    *,
    evidence_limit: int = SCOPE_COMPLETENESS_EVIDENCE_LIMIT,
) -> ScopeCompletenessResult:
    """Validate and canonicalize PLAN scope without granting prose filesystem authority.

    Candidate relationships are required only when deterministic repository evidence
    produced them. Existing-file checks are skipped when discovery returned no file
    map, preserving operation for legacy/offline workers that cannot obtain one.
    """
    if evidence_limit < 0:
        raise ValueError("evidence_limit must be non-negative")

    def plan_path(value: object) -> str | None:
        text = str(value).strip().strip("`'\"").replace("\\", "/")
        path = Path(text)
        if (
            not text
            or path.is_absolute()
            or ".." in path.parts
            or text.endswith("/")
            or not path.suffix
            or any(part in ("", ".") for part in path.parts)
        ):
            return None
        return path.as_posix()

    def paths(values: list[object]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            safe = plan_path(value)
            if safe and safe not in normalized:
                normalized.append(safe)
        return normalized

    plan_paths = paths(list(plan.get("target_files") or []))
    stories = plan.get("stories") or []
    story_paths = paths([path for story in stories for path in (story.get("target_files") or [])])
    impact = plan.get("file_impact") or []
    impact_paths = paths([item.get("path", "") for item in impact])
    expected = paths([*story_paths, *impact_paths])
    # Older persisted/mock plans may lack the newer itemized fields. They remain
    # readable, while newly parsed plans (which always contain them) are exact.
    union_is_checkable = "file_impact" in plan
    manifest = tuple(sorted(expected if union_is_checkable else plan_paths))
    reason_paths: dict[str, set[str]] = {}

    def reject(code: str, path: str) -> None:
        reason_paths.setdefault(code, set()).add(path)

    if union_is_checkable and set(plan_paths) != set(expected):
        for path in sorted(set(plan_paths) ^ set(expected)):
            reject("plan_union_mismatch", path)
    if union_is_checkable and set(story_paths) != set(impact_paths):
        for path in sorted(set(story_paths) ^ set(impact_paths)):
            reject("story_file_impact_mismatch", path)

    tracked = {path for value in tracked_files if (path := plan_path(value))}
    new_paths = {
        path
        for item in impact
        if item.get("status") == "new" and (path := plan_path(item.get("path", "")))
    }
    if tracked and union_is_checkable:
        for path in manifest:
            if path not in tracked and path not in new_paths:
                reject("existing_path_not_tracked", path)

    declared = set(manifest)
    ui = plan.get("ui_impact") or {}
    internal_only = ui.get("touches_backend_surface") is False and bool(
        str(ui.get("no_ui_change_reason") or "").strip()
    )
    candidate_provenance: set[str] = set()
    for candidate in candidates:
        candidate_provenance.update(candidate.reasons)
        if not union_is_checkable:
            continue
        if candidate.path in declared:
            continue
        frontend = _is_frontend_path(candidate.path)
        if frontend and internal_only:
            continue
        codes = {
            _COMPLETENESS_REASON_BY_PROVENANCE[reason]
            for reason in candidate.reasons
            if reason in _COMPLETENESS_REASON_BY_PROVENANCE
        }
        if "package_export" in candidate.reasons and not any(
            Path(path).parent == Path(candidate.path).parent for path in new_paths
        ):
            codes.discard("model_export_omission")
        if "explicit_path" in candidate.reasons or "covered_story" in candidate.reasons:
            codes.add("candidate_omission")
        for code in codes:
            reject(code, candidate.path)

    if ui.get("touches_backend_surface") is True:
        has_frontend = any(_is_frontend_path(path) for path in manifest)
        if not has_frontend:
            frontend_candidates = [item.path for item in candidates if _is_frontend_path(item.path)]
            for path in frontend_candidates or ["ui_impact.frontend_changes"]:
                reject("frontend_coverage_omission", path)

    reason_codes = tuple(sorted(reason_paths))
    missing = sorted({path for values in reason_paths.values() for path in values})
    candidate_paths = sorted(
        {candidate.path for candidate in candidates if candidate.path in missing}
    )
    return ScopeCompletenessResult(
        manifest=manifest,
        reason_codes=reason_codes,
        missing_paths=tuple(missing[:evidence_limit]),
        candidate_paths=tuple(candidate_paths[:evidence_limit]),
        provenance=tuple(sorted(candidate_provenance)),
    )


def normalize_scope_amendment_path(value: str) -> str | None:
    """Return a canonical concrete repository file path, or ``None`` if unsafe."""
    value = value.strip().strip("`'\"").replace("\\", "/")
    path = Path(value)
    if (
        not value
        or any(char in value for char in _GLOB_CHARS)
        or any(char.isspace() or ord(char) < 32 for char in value)
        or re.match(r"^[A-Za-z]:/", value)
        or path.is_absolute()
        or ".." in path.parts
        or value.endswith("/")
        or len(path.parts) < 2
        or not path.suffix
        or any(part in ("", ".") for part in path.parts)
    ):
        return None
    return path.as_posix()


def _module_path(module: str, tracked: set[str]) -> str | None:
    direct = normalize_scope_amendment_path(module)
    if direct in tracked:
        return direct
    base = module.replace(".", "/")
    for candidate in (f"{base}.py", f"{base}/__init__.py"):
        if candidate in tracked:
            return candidate
    return None


def _named_path_tokens(text: str) -> list[str]:
    return [token for token in _PATH_TOKEN_RE.findall(text or "") if "/" in token]


def _reference_only_paths(text: str) -> set[str]:
    """Return paths presented only as read-only planning context."""
    referenced: set[str] = set()
    implementation: set[str] = set()
    in_reference_section = False
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip().lower()
            in_reference_section = any(
                marker in heading for marker in ("context", "study", "read first", "reference")
            )
        line_paths = {
            safe
            for token in _named_path_tokens(line)
            if (safe := normalize_scope_amendment_path(token))
        }
        lowered = stripped.lower()
        line_is_reference = in_reference_section or any(
            marker in lowered
            for marker in ("study these", "read first", "inspect ", "mirror ", "pattern in")
        )
        (referenced if line_is_reference else implementation).update(line_paths)
    return referenced - implementation


def _scope_fence_paths(text: str) -> set[str]:
    """Return concrete paths inside an explicit ONLY-files scope section."""
    fenced: set[str] = set()
    in_fence = False
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip().lower()
            in_fence = ("scope" in heading and "only files" in heading) or any(
                marker in heading
                for marker in (
                    "allowed target files",
                    "allowed files",
                    "files this job may change",
                )
            )
            continue
        if not in_fence:
            continue
        for token in _named_path_tokens(line):
            if safe := normalize_scope_amendment_path(token):
                fenced.add(safe)
    return fenced


_NO_CHANGE_MARKERS = (
    "no functional change",
    "no functional changes",
    "byte-for-byte unchanged",
    "byte for byte unchanged",
)


def _no_change_declared_paths(text: str) -> set[str]:
    """Return concrete paths covered by an unambiguous no-change declaration.

    A path is declared unchanged either by sitting under a heading whose text
    contains a no-change marker phrase (a fence lasting until the next
    heading, mirroring ``_scope_fence_paths``), or by being named on an
    individual line that itself contains a marker phrase.
    """
    declared: set[str] = set()
    in_fence = False
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip().lower()
            in_fence = any(marker in heading for marker in _NO_CHANGE_MARKERS)
            continue
        lowered = stripped.lower()
        if not (in_fence or any(marker in lowered for marker in _NO_CHANGE_MARKERS)):
            continue
        for token in _named_path_tokens(line):
            if safe := normalize_scope_amendment_path(token):
                declared.add(safe)
    return declared


_FILES_HEADING_RE = re.compile(r"^(?:target\s+files|files)\s*:?$", re.IGNORECASE)


def _files_label_fenced_paths(text: str) -> set[str]:
    """Return concrete paths declared via an authorized 'Files:' label.

    A bare path mention in free-form prose is advisory by default; it becomes
    mandatory only inside an explicit declaration of intent to modify. That
    declaration is either a heading whose text is exactly 'Target files' or
    'Files' (a fence lasting until the next heading, mirroring
    ``_scope_fence_paths`` and ``_no_change_declared_paths``), or an inline
    ``Files:``/``Target files:`` label (``_FILES_LABEL_RE``, the same label
    convention ``extract_scope_from_idea`` reads) naming paths on that one line.
    """
    labeled: set[str] = set()
    in_fence = False
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            in_fence = bool(_FILES_HEADING_RE.match(heading))
            continue
        if in_fence:
            for token in _named_path_tokens(line):
                if safe := normalize_scope_amendment_path(token):
                    labeled.add(safe)
            continue
        if _FILES_LABEL_RE.search(stripped):
            for token in _named_path_tokens(stripped):
                if safe := normalize_scope_amendment_path(token):
                    labeled.add(safe)
    return labeled


def _ui_surface_evidence(text: str, paths: list[str]) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in ("frontend", " ui ", "user interface", "react", "browser", "api route")
    ) or any(_is_frontend_path(path) for path in paths)


def _is_frontend_path(path: str) -> bool:
    return (
        path.startswith("frontend/")
        or "/frontend/" in path
        or path.endswith((".js", ".jsx", ".ts", ".tsx", ".vue", ".css", ".html"))
    )


def _path_surface(path: str) -> tuple[str, str] | None:
    """Return the language and product area represented by a concrete path."""
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        language = "python"
    elif suffix in {".js", ".jsx", ".ts", ".tsx", ".css"}:
        language = "frontend"
    else:
        return None

    parts = Path(path).parts
    if len(parts) >= 2 and parts[0] == "hyqs":
        area = "/".join(parts[:2])
    elif parts:
        area = parts[0]
    else:
        return None
    return language, area


def _embedded_candidate_evidence(text: str) -> tuple[dict[str, list[str]], list[str]]:
    marker = "### Focused candidate files (evidence, not authorization)"
    if marker not in text:
        return {}, []
    section = text.split(marker, 1)[1].split("\n\n", 1)[0]
    evidence: dict[str, list[str]] = {}
    new_files: list[str] = []
    pattern = re.compile(
        r"^-\s+(\S+)\s+\[(existing|new)\]\s+provenance=([a-z_,]+)\s*$",
        re.MULTILINE,
    )
    for path, status, reason_text in pattern.findall(section):
        for reason in reason_text.split(","):
            if reason in _CANDIDATE_PRIORITY:
                evidence.setdefault(reason, []).append(path)
        if status == "new":
            new_files.append(path)
    return evidence, new_files


def build_planning_candidates(
    tracked_files: list[str],
    symbol_index: dict[str, list[dict]] | None,
    idea_text: str,
    *,
    evidence_paths: dict[str, list[str]] | None = None,
    explicit_new_files: list[str] | None = None,
    limit: int = PLANNING_CANDIDATE_LIMIT,
) -> list[PlanningCandidate]:
    """Build a bounded stable map without turning prose into filesystem authority.

    Existing results must be tracked. Nonexistent results are accepted only via
    ``explicit_new_files``. Evidence values are structured paths supplied by
    deterministic callers; arbitrary analyst prose is deliberately not parsed.
    """
    if limit < 0:
        raise ValueError("limit must be non-negative")
    tracked = {normalize_scope_amendment_path(path) for path in tracked_files}
    tracked.discard(None)
    reasons: dict[str, set[str]] = {}

    def add(path: str, reason: str, *, allow_new: bool = False) -> None:
        safe = normalize_scope_amendment_path(path)
        if safe is None or (safe not in tracked and not allow_new):
            return
        if "/frontend/" in safe and not ui_evidence:
            return
        reasons.setdefault(safe, set()).add(reason)

    embedded_marker = "### Focused candidate files (evidence, not authorization)"
    authorization_text = idea_text.split(embedded_marker, 1)[0]
    named_paths = _named_path_tokens(authorization_text)
    reference_paths = _reference_only_paths(authorization_text)
    reference_paths |= _no_change_declared_paths(authorization_text)
    mandatory_paths = _scope_fence_paths(authorization_text) | _files_label_fenced_paths(
        authorization_text
    )
    embedded, embedded_new = _embedded_candidate_evidence(idea_text)
    structured = {reason: list(paths) for reason, paths in (evidence_paths or {}).items()}
    for reason, paths in embedded.items():
        structured.setdefault(reason, []).extend(paths)
    all_evidence_paths = (
        named_paths
        + [p for paths in structured.values() for p in paths]
        + list(explicit_new_files or [])
        + embedded_new
    )
    ui_evidence = _ui_surface_evidence(authorization_text, all_evidence_paths)
    declared_surfaces = {
        surface
        for path in all_evidence_paths
        if (safe := normalize_scope_amendment_path(path))
        if (surface := _path_surface(safe))
    }

    def symbol_path_is_local(path: str) -> bool:
        """Keep index relationships within explicitly declared code surfaces."""
        surface = _path_surface(path)
        return not declared_surfaces or surface in declared_surfaces

    for path in named_paths:
        safe = normalize_scope_amendment_path(path)
        reason = (
            "explicit_path"
            if safe in mandatory_paths and safe not in reference_paths
            else "explicit_reference"
        )
        add(path, reason)
    for reason, paths in sorted(structured.items()):
        if reason not in _CANDIDATE_PRIORITY:
            continue
        for path in paths:
            add(path, reason)
    for path in [*(explicit_new_files or []), *embedded_new]:
        add(path, "explicit_new", allow_new=True)

    # Symbols must be identifier-shaped and explicitly named in the idea.
    words = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", authorization_text))
    for symbol in sorted(words & set(symbol_index or {})):
        for entry in sorted((symbol_index or {})[symbol], key=lambda item: item.get("module", "")):
            path = _module_path(str(entry.get("module", "")), tracked)
            if path and symbol_path_is_local(path):
                add(path, "explicit_reference" if path in reference_paths else "explicit_symbol")
            if path in reference_paths:
                continue
            for related in entry.get("callers", []) or []:
                related_path = _module_path(str(related), tracked)
                if related_path and symbol_path_is_local(related_path):
                    add(related_path, "indexed_caller")

    # Conservative, convention-based co-located tests and package exports.
    source_paths = list(reasons)
    for path in source_paths:
        if reasons[path] <= _REUSE_ONLY_REASONS:
            continue
        p = Path(path)
        if path.endswith(".py") and not p.name.startswith("test_"):
            test_names = {
                f"tests/test_{p.stem}.py",
                str(p.with_name(f"test_{p.name}")),
            }
            for test_path in sorted(test_names & tracked):
                add(test_path, "nearest_test")
            init_path = str(p.parent / "__init__.py")
            if init_path in tracked and (
                p.parent.name == "models" or p.stem in {"model", "models"}
            ):
                add(init_path, "package_export")
        elif path.endswith((".js", ".jsx", ".ts", ".tsx")) and ".test." not in path:
            for suffix in (".test.js", ".test.jsx", ".test.ts", ".test.tsx"):
                test_path = str(p.with_suffix(suffix))
                if test_path in tracked:
                    add(test_path, "nearest_test")
        if p.name in {"model.py", "models.py"}:
            prefix = str(p.parent)
            for migration in sorted(
                candidate
                for candidate in tracked
                if candidate.startswith(f"{prefix}/")
                and "/versions/" in candidate
                and candidate.endswith(".py")
            ):
                add(migration, "model_migration")
        if "/versions/" in path and path.endswith(".py"):
            service_root = path.split("/alembic/", 1)[0]
            for model_path in (f"{service_root}/models.py", f"{service_root}/model.py"):
                if model_path in tracked:
                    add(model_path, "model_migration")

    def sort_key(item: tuple[str, set[str]]) -> tuple[int, str]:
        return min(_CANDIDATE_PRIORITY[r] for r in item[1]), item[0]

    result = []
    for path, path_reasons in sorted(reasons.items(), key=sort_key)[:limit]:
        ordered = tuple(sorted(path_reasons, key=lambda r: (_CANDIDATE_PRIORITY[r], r)))
        result.append(PlanningCandidate(path, "existing" if path in tracked else "new", ordered))
    return result


@dataclass(frozen=True)
class ScopeCheckResult:
    """Bounded, serialisable result of enforcing a job's scope manifest."""

    enforced: bool
    allowed_paths: tuple[str, ...]
    unexpected_paths: tuple[str, ...]
    unexpected_count: int
    truncated: bool

    @property
    def passed(self) -> bool:
        return self.unexpected_count == 0

    def to_dict(self) -> dict:
        data = asdict(self)
        data["allowed_paths"] = list(self.allowed_paths)
        data["unexpected_paths"] = list(self.unexpected_paths)
        data["passed"] = self.passed
        return data


# Names that every module-execution entrypoint (`python -m pkg`) legitimately
# defines. Duplicated across `__main__` modules by Python convention, not by
# accidental copy-paste.
_ENTRYPOINT_NAMES = {"main", "run"}

# Project-manifest filenames that mark a directory as an independently
# deployable workspace member (its own uv/npm/cargo/go project, never
# cross-imported by a sibling member).
_MANIFEST_NAMES = ("pyproject.toml", "package.json", "Cargo.toml", "go.mod")


def _module_dir(module: str) -> Path:
    """The directory a ``module`` string denotes, relative to the repo root.

    Python modules are dotted paths naming a *file* (``pkg.sub.mod`` ->
    ``pkg/sub/mod.py``), so the module's directory is its dotted parent.
    JS/TS modules (from ``extract_symbols``/the symbol index) are already
    relative file paths, so it's simply ``Path(module).parent``.
    """
    if "/" in module or module.endswith((".js", ".ts", ".jsx", ".tsx")):
        return Path(module).parent
    return Path(*module.split(".")[:-1])


def _workspace_root(repo_root: Path, module: str) -> str:
    """The nearest ancestor directory of ``module``, strictly below
    ``repo_root``, that contains a project manifest — or ``""`` (the repo
    root itself) when no such ancestor exists.

    Independently-deployed workspace members (each with its own
    pyproject.toml/package.json/Cargo.toml/go.mod) are the collision gate's
    unit of coherence: a symbol duplicated *across* members — e.g. two
    FastAPI services each defining their own ``create_app``/``lifespan``, or
    an alembic ``env.py`` per service — is framework convention, not
    copy-paste. Only duplication *within* the same member is still a
    coherence bug.
    """
    current = _module_dir(module)
    while str(current) not in (".", ""):
        if any((repo_root / current / name).is_file() for name in _MANIFEST_NAMES):
            return str(current)
        current = current.parent
    return ""


def _is_test_module(module: str) -> bool:
    """True for pytest modules, where helper/fixture names routinely repeat."""
    parts = module.split(".")
    return parts[0] == "tests" or parts[-1].startswith("test_") or parts[-1] == "conftest"


def _is_stage_handler(module: str, name: str) -> bool:
    """True for the per-stage dispatch entrypoint convention: async def run(...)
    defined in any leaf module under *.pipeline.stages.*.
    Every pipeline stage module defines exactly this signature so the runner can
    dispatch it; it is not accidental copy-paste."""
    if name != "run":
        return False
    parent, _, leaf = module.rpartition(".")
    return parent.endswith("pipeline.stages") and leaf not in ("", "__init__")


def _is_alembic_revision_entrypoint(module: str, name: str) -> bool:
    """True for Alembic's required entrypoints in a revision module."""
    if name not in {"upgrade", "downgrade"} or "/" in module:
        return False
    parts = module.split(".")
    return parts[-1] not in ("", "__init__") and any(
        parts[index : index + 2] == ["alembic", "versions"] for index in range(len(parts) - 2)
    )


def _is_conventional(module: str, name: str) -> bool:
    """True when reusing ``name`` in ``module`` is a language/framework convention
    rather than a coherence problem — exempt even when newly introduced."""
    if name == "main":
        return True
    if module.rpartition(".")[2] == "__main__" and name in _ENTRYPOINT_NAMES:
        return True
    if _is_test_module(module):
        return True
    if _is_stage_handler(module, name):
        return True
    if _is_alembic_revision_entrypoint(module, name):
        return True
    return False


def _module_in_changed_files(module: str, changed_files: set[str]) -> bool:
    """True if ``module``'s underlying source file is present in ``changed_files``.

    JS/TS modules (and any already path-shaped module — see ``_module_dir``)
    are matched directly by path. Dotted Python modules are matched against
    both their plain-file form (``pkg/sub/mod.py``) and their package
    ``__init__.py`` form, since a module string never spells out ``__init__``
    itself.
    """
    if "/" in module or module.endswith((".js", ".ts", ".jsx", ".tsx")):
        return module in changed_files
    base = module.replace(".", "/")
    return f"{base}.py" in changed_files or f"{base}/__init__.py" in changed_files


def check_symbol_collisions(
    worktree: str | Path,
    project_id: int,
    store,
    changed_files: list[str] | set[str] | None = None,
) -> list[str]:
    """Return one collision message per symbol the job *introduced* that now
    appears in two or more different non-conventional modules in the post-build
    worktree, where at least one of those modules is new relative to the
    baseline.

    M1: every finding is substantiated against the post-build worktree.  A
    symbol in exactly one post-build module produces no message — it may be a
    MOVE.  Returns [] when there are no collisions.

    Workspace-scoped (job #1424): a symbol duplicated across two different
    workspace members (see ``_workspace_root``) is never a collision — only
    duplication within the same member is. Raises nothing.

    Diff-aware (job #2140): the project-wide baseline index is only refreshed
    periodically (during some job's PLAN stage), so it can lag a just-merged
    file. When ``changed_files`` is supplied, a module absent from the
    baseline is only ever attributed to *this* job if that module's file is
    actually part of the job's own diff — a module the job merely inherited
    via git, without touching it, can never be reported as a collision the
    job introduced. ``changed_files=None`` (the default) preserves prior
    behavior exactly.
    """
    repo_root = Path(worktree)
    post_build = extract_symbols(str(worktree))
    index = store.get_symbol_index_names(project_id)
    changed_set = set(changed_files) if changed_files is not None else None

    # Group non-conventional post-build symbols by name → list of sym dicts.
    post_by_name: dict[str, list[dict]] = {}
    for sym in post_build:
        if _is_conventional(sym["module"], sym["symbol"]):
            continue
        name = sym["symbol"]
        if name not in post_by_name:
            post_by_name[name] = []
        post_by_name[name].append(sym)

    messages: list[str] = []
    for name, syms in post_by_name.items():
        baseline_entries = index.get(name, [])

        # Sub-group both sides by workspace root, so the duplication check
        # below only ever compares modules that share the same member.
        by_root: dict[str, dict[str, list[dict]]] = {}
        for sym in syms:
            root = _workspace_root(repo_root, sym["module"])
            by_root.setdefault(root, {"post": [], "baseline": []})["post"].append(sym)
        for entry in baseline_entries:
            root = _workspace_root(repo_root, entry["module"])
            by_root.setdefault(root, {"post": [], "baseline": []})["baseline"].append(entry)

        for groups in by_root.values():
            root_syms = groups["post"]
            root_baseline = groups["baseline"]
            post_modules = {s["module"] for s in root_syms}

            # M1: fewer than 2 distinct post-build modules in this workspace
            # member → no duplication possible. A moved symbol appears in
            # exactly one module here → no collision.
            if len(post_modules) < 2:
                continue

            baseline_modules = {e["module"] for e in root_baseline}

            # If every post-build module was already in the baseline for this
            # name, the duplication is pre-existing — not introduced by this job.
            new_modules = post_modules - baseline_modules
            if changed_set is not None:
                new_modules = {m for m in new_modules if _module_in_changed_files(m, changed_set)}
            if not new_modules:
                continue

            # At least one module is new → job introduced a cross-module duplicate.
            # Emit one message: the "new" side vs the "other" side (prefer baseline).
            new_mod = min(new_modules)  # stable ordering
            new_sym = next(s for s in root_syms if s["module"] == new_mod)

            other_candidates = post_modules - {new_mod}
            baseline_other = other_candidates & baseline_modules
            other_mod = min(baseline_other) if baseline_other else min(other_candidates)
            entry = next((e for e in root_baseline if e["module"] == other_mod), None)
            sig = entry["signature"] if entry else new_sym.get("signature", "")

            messages.append(
                f"Symbol '{name}' ({new_sym['kind']}) in module '{new_mod}' "
                f"duplicates existing symbol in '{other_mod}' — "
                f"signature: {sig}"
            )

    return messages


# Matches a "Files:" or "Target files:" label anywhere in a job idea. Applied
# to the FULL idea text — never a truncated commit-subject string — so a
# job's initial scope manifest is never born cut off mid-enumeration (see
# job #1135, where a manifest derived from a truncated subject omitted 7
# files the idea's own "Files:" list named).
_FILES_LABEL_RE = re.compile(r"\b(?:target\s+files|files)\s*:", re.IGNORECASE)
_PATH_TOKEN_RE = re.compile(r"[\w\-./()\[\]]+\.[A-Za-z0-9]+")


def extract_scope_from_idea(idea: str) -> list[str]:
    """Pure parser: pull a full "Files:"/"Target files:" enumeration out of
    ``idea``. Finds the label anywhere in the text, then extracts every
    file-path-shaped token that follows it, deduped and in order. Returns []
    when the idea carries no such label.
    """
    match = _FILES_LABEL_RE.search(idea)
    if not match:
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for token in _PATH_TOKEN_RE.findall(idea[match.end() :]):
        if token not in seen:
            seen.add(token)
            paths.append(token)
    return paths


def job_declared_scope_paths(idea: str, plan: dict | None) -> list[str]:
    """Every path a job has ever declared as its own target, deduped and in order.

    Unions two sources: (a) each story's ``target_files`` from ``plan.stories``
    (``plan`` may be ``None`` — e.g. before the PLAN stage has run), followed by
    (b) whatever ``extract_scope_from_idea(idea)`` yields. This is the job's
    declared scope floor: a mid-run scope-expansion grant must never produce a
    manifest that excludes it (see job #1422, where a grant that only appended
    the newly-authorized paths silently created a manifest excluding the job's
    own deliverable).
    """
    paths: list[str] = []
    seen: set[str] = set()
    for story in (plan or {}).get("stories") or []:
        for path in story.get("target_files") or []:
            if path not in seen:
                seen.add(path)
                paths.append(path)
    for path in extract_scope_from_idea(idea):
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def check_manifest_conflict(paths_a: list[str], paths_b: list[str]) -> bool:
    """True if any glob in ``paths_a`` matches or is matched by any glob in ``paths_b``.

    Pure and symmetric: fnmatch is applied both ways since either side's pattern
    may be the more specific one (a literal path vs. a glob). Empty lists never
    conflict — an unscoped manifest carries no claim to serialize against.

    A literal-equality check runs before fnmatch (job #2239): fnmatch parses its
    pattern argument for glob syntax, where ``[...]`` means "match any single
    character in this set" — so a literal path containing bracket characters
    (e.g. a Next.js dynamic route directory like ``[token]``) can never match
    itself under fnmatch alone.
    """
    if not paths_a or not paths_b:
        return False
    return any(a == b or fnmatch(a, b) or fnmatch(b, a) for a in paths_a for b in paths_b)


@dataclass(frozen=True, slots=True)
class DependencyScope:
    """Normalized scope used by the dependency scheduler."""

    known: bool
    paths: tuple[str, ...]


def classify_dependency_scope(value: object) -> DependencyScope:
    """Classify a scope manifest without inferring authority from prose.

    Only a non-empty ``allowed_paths`` collection is known. Invalid, absent,
    and empty manifests are unknown and therefore require conservative ordering.
    """
    scope = value
    if isinstance(value, dict) and "scope" in value:
        scope = value.get("scope")
    if not isinstance(scope, dict):
        return DependencyScope(False, ())
    raw_paths = scope.get("allowed_paths")
    if not isinstance(raw_paths, (list, tuple)):
        return DependencyScope(False, ())
    paths = tuple(dict.fromkeys(path for path in raw_paths if isinstance(path, str) and path))
    return DependencyScope(bool(paths), paths)


def minimize_auto_dependencies(
    scopes: dict[int, object],
    edges: Iterable[tuple[int, int, str]],
) -> list[tuple[int, int, str]]:
    """Return a stable, reachability-preserving minimal dependency graph.

    Protected edges are never removed. Generated edges are removed when both
    endpoints have known-disjoint manifests, or when another path already
    provides the same ordering. Cyclic input is rejected before reduction so a
    reduction can never conceal a pre-existing cycle.
    """
    normalized = sorted(set(edges), key=lambda edge: (edge[0], edge[1], edge[2]))
    strongest: dict[tuple[int, int], str] = {}
    rank = {"auto": 0, "semantic": 1, "user": 2}
    for job_id, dependency_id, provenance in normalized:
        key = (job_id, dependency_id)
        if key[0] == key[1]:
            raise ValueError("dependency cycle")
        if provenance not in rank:
            raise ValueError(f"unknown dependency provenance: {provenance}")
        if key not in strongest or rank[provenance] > rank[strongest[key]]:
            strongest[key] = provenance

    adjacency: dict[int, set[int]] = {}
    for (job_id, dependency_id), _provenance in strongest.items():
        adjacency.setdefault(job_id, set()).add(dependency_id)

    nodes = sorted(set(adjacency) | {n for values in adjacency.values() for n in values})
    dependents: dict[int, list[int]] = {node: [] for node in nodes}
    remaining = {node: len(adjacency.get(node, ())) for node in nodes}
    for job_id, dependencies in adjacency.items():
        for dependency_id in dependencies:
            dependents[dependency_id].append(job_id)
    ready = sorted(node for node in nodes if remaining[node] == 0)
    heapq.heapify(ready)
    topo: list[int] = []
    while ready:
        node = heapq.heappop(ready)
        topo.append(node)
        for dependent_id in sorted(dependents[node]):
            remaining[dependent_id] -= 1
            if remaining[dependent_id] == 0:
                heapq.heappush(ready, dependent_id)
    if len(topo) != len(nodes):
        raise ValueError("dependency cycle")

    kept = dict(strongest)
    for key, provenance in strongest.items():
        if provenance != "auto":
            continue
        left = classify_dependency_scope(scopes.get(key[0]))
        right = classify_dependency_scope(scopes.get(key[1]))
        if (
            left.known
            and right.known
            and not check_manifest_conflict(list(left.paths), list(right.paths))
        ):
            kept.pop(key, None)
    reduced_adjacency: dict[int, set[int]] = {}
    for job_id, dependency_id in kept:
        reduced_adjacency.setdefault(job_id, set()).add(dependency_id)
    bit = {node: 1 << index for index, node in enumerate(nodes)}
    reachable: dict[int, int] = {}
    for node in topo:
        mask = 0
        for dependency_id in reduced_adjacency.get(node, ()):
            mask |= bit[dependency_id] | reachable[dependency_id]
        reachable[node] = mask

    for key, provenance in sorted(kept.items()):
        if provenance != "auto":
            continue
        alternate = any(
            other != key[1] and bool((bit[other] | reachable[other]) & bit[key[1]])
            for other in reduced_adjacency.get(key[0], ())
        )
        if alternate:
            kept.pop(key, None)
    return [
        (job_id, dependency_id, provenance)
        for (job_id, dependency_id), provenance in sorted(kept.items())
    ]


def repoint_dependency_provenance(
    edges: Iterable[tuple[int, str]],
    child_ids: list[int],
) -> list[tuple[int, int, str]]:
    """Fan a cancelled split parent's dependents out onto its split children.

    ``edges`` is one (dependent_job_id, provenance) pair per dependent that
    depended on the parent. Each dependent keeps its original provenance
    string against every child in ``child_ids``. Pure and order-independent:
    the result is sorted by (dependent_id, child_id) so callers get the same
    rows regardless of input ordering, matching ``minimize_auto_dependencies``'
    plain-string provenance convention.
    """
    if not child_ids:
        return []
    return sorted(
        {
            (dependent_id, child_id, provenance)
            for dependent_id, provenance in edges
            for child_id in child_ids
        }
    )


@dataclass(frozen=True)
class QueueSurveyResult:
    """Deterministic file-path-collision survey of candidate jobs against a
    project's active job queue — the pre-creation counterpart to
    ``create_batch``'s same-batch hot-file auto-chaining, computed against
    the live queue instead of a single batch call. File-path overlap only;
    no title/brief ("capability overlap") similarity matching — that needs
    fuzzy judgment with no deterministic substrate and is explicitly out of
    scope (job #2907)."""

    overlaps: dict[str, list[int]]
    unknown_target_file_jobs: list[int]

    def to_dict(self) -> dict:
        return {
            "overlaps": {key: list(ids) for key, ids in self.overlaps.items()},
            "unknown_target_file_jobs": list(self.unknown_target_file_jobs),
        }


def survey_job_queue(candidates: list[dict], active_jobs: list[dict]) -> QueueSurveyResult:
    """Check each candidate's ``target_files`` for path overlap against every
    active job's known scope.

    ``active_jobs`` entries are plain dicts ``{id, idea, plan, source_meta}``
    (no DB/Job-model coupling, keeping this module pure). Each active job's
    known target files union ``job_declared_scope_paths(idea, plan)`` (plan
    stories' target_files, then the idea's own "Target files:" enumeration)
    with any granted ``source_meta.scope.allowed_paths`` — the same field
    ``store._manifest_conflicts_with_running`` already reads for RUNNING jobs.
    An active job with no paths from either source is reported in
    ``unknown_target_file_jobs`` instead of being silently treated as
    collision-free — an unknown-scope job must never be assumed non-colliding.

    ``candidates`` entries are ``{key, title, target_files}``; ``title`` is
    carried by the caller but never compared here. Each candidate's ``key``
    maps to the sorted job ids among ``active_jobs`` (excluding those with
    unknown scope) whose known paths overlap ``target_files``, per
    ``check_manifest_conflict``.
    """
    known_paths_by_job: dict[int, list[str]] = {}
    unknown: list[int] = []
    for job in active_jobs:
        job_id = job["id"]
        paths = list(job_declared_scope_paths(job.get("idea") or "", job.get("plan")))
        scope = (job.get("source_meta") or {}).get("scope") or {}
        for path in scope.get("allowed_paths") or []:
            if path not in paths:
                paths.append(path)
        if paths:
            known_paths_by_job[job_id] = paths
        else:
            unknown.append(job_id)

    overlaps: dict[str, list[int]] = {}
    for candidate in candidates:
        target_files = candidate.get("target_files") or []
        overlaps[candidate["key"]] = sorted(
            job_id
            for job_id, paths in known_paths_by_job.items()
            if check_manifest_conflict(target_files, paths)
        )

    return QueueSurveyResult(overlaps=overlaps, unknown_target_file_jobs=sorted(unknown))


def _amendment_paths(value: object) -> list[str]:
    """Extract path values from the small structured shapes used by the policy."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        for key in ("allowed_paths", "target_files", "paths", "files", "candidates"):
            if key in value:
                return _amendment_paths(value[key])
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        result: list[str] = []
        for item in value:
            if isinstance(item, dict):
                path = item.get("path") or item.get("candidate")
                if path:
                    result.append(str(path))
                else:
                    result.extend(_amendment_paths(item))
            else:
                result.append(str(item))
        return result
    return []


def _plan_story_paths(plan_story: object) -> list[str]:
    if not isinstance(plan_story, dict):
        return _amendment_paths(plan_story)
    paths = _amendment_paths(plan_story)
    for story in plan_story.get("stories") or []:
        paths.extend(_amendment_paths(story))
    for item in plan_story.get("file_impact") or []:
        paths.extend(_amendment_paths(item))
    return paths


def _is_sensitive_amendment_path(path: str) -> bool:
    lowered = path.lower()
    parts = set(Path(lowered).parts)
    name = Path(lowered).name
    stem = Path(lowered).stem
    return (
        bool(parts & _SENSITIVE_PARTS)
        or stem in _SENSITIVE_PARTS
        or name in _SENSITIVE_NAMES
        or name.startswith(".env.")
        or "/alembic/versions/" in lowered
        or name.endswith((".lock", ".pem", ".key"))
    )


def evaluate_scope_amendment(
    frozen_scope: object,
    plan_story: object,
    authorized_event: dict,
    tracked_files: list[str],
    current_diff: list[str],
    evidence: dict[str, object] | None,
    relationships: object,
    amendment_history: object,
    active_manifests: object,
    *,
    per_cycle_limit: int = SCOPE_AMENDMENT_PER_CYCLE_LIMIT,
    cumulative_limit: int = SCOPE_AMENDMENT_CUMULATIVE_LIMIT,
) -> ScopeAmendmentDecision:
    """Evaluate a bounded scope amendment using only deterministic gate evidence.

    ``evidence`` is a mapping of provenance names (for example ``imports`` or
    ``coverage``) to concrete paths. ``relationships`` is either a mapping from
    root paths to companion paths or records containing ``source``, ``target``,
    and optional ``kind``. Free-form prose is intentionally never inspected.
    """
    if per_cycle_limit < 0 or cumulative_limit < 0:
        raise ValueError("scope amendment limits must be non-negative")

    stage = str(authorized_event.get("stage") or authorized_event.get("gate") or "")
    stage = stage.lower().replace("-", "_")
    authorized = (
        stage in AUTHORIZED_AMENDMENT_GATES
        and authorized_event.get("authorized", True) is True
        and authorized_event.get("gated", True) is True
        and authorized_event.get("failed", True) is True
        and str(authorized_event.get("outcome", "failed")).lower() not in {"passed", "success"}
    )
    operator_paths = {
        path
        for raw in _amendment_paths(authorized_event.get("operator_authorized_paths"))
        if (path := normalize_scope_amendment_path(raw))
    }
    operator_paths.update(
        path
        for raw in _amendment_paths(authorized_event.get("operator_metadata"))
        if (path := normalize_scope_amendment_path(raw))
    )
    direct_values = (
        authorized_event.get("failing_paths")
        or authorized_event.get("paths")
        or authorized_event.get("files")
        or []
    )
    direct_raw = _amendment_paths(direct_values)
    plan_paths = {
        path
        for raw in _plan_story_paths(plan_story)
        if (path := normalize_scope_amendment_path(raw))
    }
    frozen_paths = {
        path
        for raw in _amendment_paths(frozen_scope)
        if (path := normalize_scope_amendment_path(raw))
    }
    tracked = {path for raw in tracked_files if (path := normalize_scope_amendment_path(raw))}
    history = {
        path
        for raw in _amendment_paths(amendment_history)
        if (path := normalize_scope_amendment_path(raw))
    }
    diff_paths = {
        path
        for raw in _amendment_paths(current_diff)
        if (path := normalize_scope_amendment_path(raw))
    }

    relation_targets: dict[str, set[tuple[str, str]]] = {}
    if isinstance(relationships, dict):
        relation_items = [
            {"source": source, "target": target, "kind": "direct_relationship"}
            for source, targets in relationships.items()
            for target in _amendment_paths(targets)
        ]
    elif isinstance(relationships, (list, tuple, set, frozenset)):
        relation_items = list(relationships)
    else:
        relation_items = []
    for item in relation_items:
        if not isinstance(item, dict):
            continue
        source = normalize_scope_amendment_path(str(item.get("source") or item.get("root") or ""))
        target = normalize_scope_amendment_path(str(item.get("target") or item.get("path") or ""))
        kind = str(item.get("kind") or item.get("relationship") or "direct_relationship")
        if (
            source
            and target
            and item.get("ambiguous") is not True
            and item.get("speculative") is not True
        ):
            relation_targets.setdefault(target, set()).add((source, kind))

    raw_candidates: list[tuple[str, str]] = [(path, f"{stage}:failure_path") for path in direct_raw]
    for provenance, values in sorted((evidence or {}).items()):
        normalized_provenance = provenance.lower()
        if normalized_provenance in {"ai", "ai_only", "model", "speculative", "ambiguous"}:
            reason = (
                "ai_only_evidence"
                if normalized_provenance in {"ai", "ai_only", "model"}
                else f"{normalized_provenance}_evidence"
            )
        elif normalized_provenance not in AUTHORIZED_GATE_EVIDENCE_CATEGORIES:
            reason = "ambiguous_evidence"
        else:
            reason = str(provenance)
        raw_candidates.extend((path, reason) for path in _amendment_paths(values))
    provenance_by_raw: dict[str, set[str]] = {}
    for raw, provenance in raw_candidates:
        text = str(raw).strip().strip("`'\"").replace("\\", "/")
        canonical = normalize_scope_amendment_path(text)
        provenance_by_raw.setdefault(canonical or text, set()).add(provenance)
    for raw in provenance_by_raw:
        safe = normalize_scope_amendment_path(raw)
        if safe in diff_paths:
            provenance_by_raw[raw].add("current_diff")

    rejected: list[ScopeAmendmentRejected] = []
    eligible: list[ScopeAmendmentAccepted] = []
    direct = {path for raw in direct_raw if (path := normalize_scope_amendment_path(raw))}
    roots = direct | plan_paths | frozen_paths
    active = (
        list(active_manifests.values()) if isinstance(active_manifests, dict) else active_manifests
    )
    active_lists = [_amendment_paths(item) for item in (active or [])]

    for raw in sorted(provenance_by_raw):
        provenances = provenance_by_raw[raw]
        safe = normalize_scope_amendment_path(raw)
        if any(char in raw for char in _GLOB_CHARS):
            rejected.append(ScopeAmendmentRejected(raw, "glob_path"))
        elif safe is None:
            code = "directory_path" if raw.endswith("/") or not Path(raw).suffix else "unsafe_path"
            rejected.append(ScopeAmendmentRejected(raw, code))
        elif not authorized:
            rejected.append(ScopeAmendmentRejected(safe, "unauthorized_event"))
        elif provenances <= {"ai_only_evidence", "current_diff"}:
            rejected.append(ScopeAmendmentRejected(safe, "ai_only_evidence"))
        elif provenances <= {"ambiguous_evidence", "speculative_evidence", "current_diff"}:
            rejected.append(
                ScopeAmendmentRejected(
                    safe, sorted(provenances & {"ambiguous_evidence", "speculative_evidence"})[0]
                )
            )
        elif safe not in tracked:
            rejected.append(ScopeAmendmentRejected(safe, "untracked_path"))
        elif safe in frozen_paths or safe in history:
            rejected.append(ScopeAmendmentRejected(safe, "repeated_path"))
        elif (
            _is_sensitive_amendment_path(safe)
            and safe not in plan_paths
            and safe not in operator_paths
        ):
            rejected.append(ScopeAmendmentRejected(safe, "sensitive_path"))
        elif any(check_manifest_conflict([safe], manifest) for manifest in active_lists):
            rejected.append(ScopeAmendmentRejected(safe, "manifest_conflict"))
        else:
            relations = relation_targets.get(safe, set())
            rooted = sorted((source, kind) for source, kind in relations if source in roots)
            if safe not in direct and not rooted:
                rejected.append(ScopeAmendmentRejected(safe, "unrelated_product_area"))
                continue
            path_provenance = set(provenances)
            path_provenance.update(f"{kind}:{source}" for source, kind in rooted)
            eligible.append(ScopeAmendmentAccepted(safe, tuple(sorted(path_provenance))))

    remaining = max(0, cumulative_limit - len(history))
    accepted_limit = min(per_cycle_limit, remaining)
    accepted = tuple(sorted(eligible, key=lambda item: item.path)[:accepted_limit])
    for item in sorted(eligible, key=lambda value: value.path)[accepted_limit:]:
        code = "cumulative_limit" if remaining <= per_cycle_limit else "per_cycle_limit"
        rejected.append(ScopeAmendmentRejected(item.path, code))
    bounded_rejections = sorted(rejected, key=lambda item: (item.candidate, item.reason_code))[
        :SCOPE_CHECK_UNEXPECTED_LIMIT
    ]
    return ScopeAmendmentDecision(accepted, tuple(bounded_rejections))


# Dependency lockfiles regenerated as a side effect of running a project's
# package manager (uv run, npm install, ...) inside the job's worktree. No
# planner ever lists these explicitly, so they are exempt from the
# out-of-lane check when they sit in a directory the job's manifest already
# covers — see job #1423 (a downstream project's job #1413 died un-healably on
# "identity/uv.lock", whose *directory* ("identity") was already in-lane via
# "identity/alembic.ini"). The exemption is scoped to directories the
# manifest already authorizes, never repo-wide: an unauthorized directory
# must still trip the gate even for a lockfile basename (security review,
# job #1423 fix-2) — a job cannot use a lockfile to smuggle changes into a
# project it has no allowed_paths entry for.
EXEMPT_LOCKFILE_BASENAMES = frozenset(
    {
        "uv.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "poetry.lock",
        "Cargo.lock",
        "Gemfile.lock",
        "composer.lock",
        "go.sum",
    }
)


def _lockfile_dir_in_scope(changed_file: str, allowed_paths: list[str]) -> bool:
    """True if ``changed_file``'s directory matches the directory of some
    ``allowed_paths`` entry, so a lockfile there is a side effect of work the
    manifest already authorizes rather than a change to an unrelated project.

    A literal-equality check runs before fnmatch (job #2239) so a bracketed
    directory name (e.g. a Next.js dynamic route like ``[token]``) matches
    itself instead of being parsed as a glob character class.
    """
    file_dir = str(Path(changed_file).parent)
    return any(
        file_dir == str(Path(pattern).parent)
        or fnmatch(file_dir, str(Path(pattern).parent))
        or fnmatch(str(Path(pattern).parent), file_dir)
        for pattern in allowed_paths
    )


# A genuine alembic merge migration lives under an `alembic/versions/`
# directory anywhere in the tree (services may nest their own alembic dirs).
_ALEMBIC_VERSIONS_RE = re.compile(r"(^|/)alembic/versions/[^/]+\.py$")


def _parse_alembic_revision(path: Path) -> tuple[str | None, object]:
    """The ``(revision, down_revision)`` pair a single alembic migration file
    declares as top-level assignments, or ``(None, None)`` on any parse/eval
    failure — a malformed or non-alembic file simply never contributes a head.
    """
    try:
        tree = ast.parse(path.read_text())
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None, None

    revision: str | None = None
    down_revision: object = None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, SyntaxError):
            continue
        if "revision" in targets:
            revision = value
        if "down_revision" in targets:
            down_revision = value
    return revision, down_revision


def discover_alembic_heads(versions_dir: str | Path, exclude: str | None = None) -> set[str]:
    """The set of revision ids under ``versions_dir`` never referenced as any
    other migration's ``down_revision`` — i.e. the current head(s) of the
    alembic chain. ``exclude`` (a basename) skips one file, so a merge
    migration can compute the heads that existed *before* it. Returns an
    empty set when ``versions_dir`` doesn't exist.
    """
    versions_dir = Path(versions_dir)
    if not versions_dir.is_dir():
        return set()

    revisions: set[str] = set()
    referenced: set[str] = set()
    for path in versions_dir.glob("*.py"):
        if exclude is not None and path.name == exclude:
            continue
        revision, down_revision = _parse_alembic_revision(path)
        if revision is None:
            continue
        revisions.add(revision)
        if down_revision is None:
            continue
        if isinstance(down_revision, (tuple, list, set)):
            referenced.update(down_revision)
        else:
            referenced.add(down_revision)

    return revisions - referenced


def check_alembic_head_collisions(
    worktree: str | Path, changed_files: list[str] | set[str]
) -> list[str]:
    """Return one message per ``alembic/versions`` directory the diff touches
    that is left with more than one head (job #2260 — the deterministic,
    proactive counterpart to ``discover_alembic_heads``'s existing reactive
    use in supervisor.py's ``remediate_alembic_multi_head``).

    A job that lands a migration whose ``down_revision`` points at a parent
    another already-merged migration also claims creates two alembic heads —
    the exact defect that crash-looped a production service in job #2242's
    incident, only caught after deploy. This runs pre-merge instead.

    Pure and fail-soft: only directories actually touched by ``changed_files``
    (matched via ``_ALEMBIC_VERSIONS_RE``) are ever walked, so a diff that
    doesn't touch alembic/versions returns ``[]`` without any filesystem
    access. A malformed migration file never raises (it simply contributes no
    head, per ``_parse_alembic_revision``'s own fail-soft behavior).
    """
    versions_files = [f for f in changed_files if _ALEMBIC_VERSIONS_RE.search(f)]
    if not versions_files:
        return []

    repo_root = Path(worktree)
    dirs = sorted({str(Path(f).parent) for f in versions_files})

    messages: list[str] = []
    for rel_dir in dirs:
        versions_dir = repo_root / rel_dir
        heads = discover_alembic_heads(versions_dir)
        if len(heads) <= 1:
            continue
        head_files = sorted(
            path.name
            for path in versions_dir.glob("*.py")
            if _parse_alembic_revision(path)[0] in heads
        )
        messages.append(
            f"'{rel_dir}' has {len(heads)} alembic heads after this diff: "
            f"{sorted(heads)} (files: {head_files}) — rebase this job's own "
            "new migration's down_revision onto the single correct tip so "
            "the chain has exactly one head."
        )
    return messages


_DIFF_NEW_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$")
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def _parse_hunk_ranges(patch_text: str) -> dict[str, list[tuple[int, int]]]:
    """Map each file in a unified diff to its old-side (``-a,b``) line ranges."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    current_file: str | None = None
    for line in patch_text.splitlines():
        file_match = _DIFF_NEW_FILE_RE.match(line)
        if file_match:
            current_file = file_match.group(1)
            continue
        hunk_match = _HUNK_HEADER_RE.match(line)
        if hunk_match and current_file is not None:
            start = int(hunk_match.group(1))
            count = int(hunk_match.group(2)) if hunk_match.group(2) is not None else 1
            end = start + max(count, 1) - 1
            ranges.setdefault(current_file, []).append((start, end))
    return ranges


def check_overlapping_merge_hunks(job_patch: str, base_patch: str) -> list[str]:
    """Return one message per file where the job's diff and the base's new
    commits (since branch-cut) independently touched overlapping original
    line ranges — the duplicate-implementation shape that produced a
    stacked-not-chosen merge defect (job #4073's postmortem, job #4077).

    Pure: parses unified-diff hunk headers only, no I/O, no git subprocess
    calls. Both patches must be diffs computed against the same shared
    merge-base commit for a file's ``-a,b`` old-side ranges to be directly
    comparable between the two sides. Returns [] when neither patch has any
    hunks, or when every touched file/range pair is disjoint.
    """
    job_ranges = _parse_hunk_ranges(job_patch)
    base_ranges = _parse_hunk_ranges(base_patch)
    messages: list[str] = []
    for file in sorted(set(job_ranges) & set(base_ranges)):
        for j_start, j_end in job_ranges[file]:
            overlap = next(
                (
                    (b_start, b_end)
                    for b_start, b_end in base_ranges[file]
                    if j_start <= b_end and b_start <= j_end
                ),
                None,
            )
            if overlap is not None:
                overlap_line = max(j_start, overlap[0])
                messages.append(
                    f"job and base both modified '{file}' around original line "
                    f"{overlap_line} — the merge may have stacked both edits "
                    "instead of choosing one"
                )
                break
    return messages


def _is_alembic_merge_resolution(changed_file: str, worktree: str | Path) -> bool:
    """True only when ``changed_file`` is a genuine alembic merge migration
    that reconciles exactly the two-or-more heads that existed before it —
    the merge's own ``down_revision`` tuple must equal the head set computed
    with the merge file itself excluded. A file whose down_revision doesn't
    match the real heads, or that isn't a single-revision down_revision
    tuple, is never exempted.
    """
    if not _ALEMBIC_VERSIONS_RE.search(changed_file):
        return False

    full_path = Path(worktree) / changed_file
    if not full_path.is_file():
        return False

    _, down_revision = _parse_alembic_revision(full_path)
    if not isinstance(down_revision, tuple) or len(down_revision) < 2:
        return False

    heads = discover_alembic_heads(full_path.parent, exclude=full_path.name)
    return set(down_revision) == heads


def check_out_of_lane(
    scope: dict | None,
    changed_files: list[str],
    worktree: str | Path | None = None,
) -> list[str]:
    """Return the subset of ``changed_files`` that match none of ``scope``'s allowed_paths.

    Fail-open: a job with no manifest (``scope`` is None or has no allowed_paths)
    is never out of lane — returns []. Dependency lockfiles (see
    ``EXEMPT_LOCKFILE_BASENAMES``) are exempt only within a directory the
    manifest already covers (see ``_lockfile_dir_in_scope``) — the package
    manager regenerates them as a side effect of running in the worktree and
    no planner ever lists them, but a lockfile outside the job's authorized
    directories is still out of lane.

    When ``worktree`` is supplied, a genuine alembic merge migration (see
    ``_is_alembic_merge_resolution``) is exempt too — resolving two heads
    into a merge file is a framework-mandated action whose path a planner
    can't predict in advance. ``worktree`` defaults to ``None``, so existing
    2-arg callers are unaffected and never take this branch.

    A literal-equality check runs before fnmatch (job #2239): fnmatch parses
    its pattern argument for glob syntax, where ``[...]`` means "match any
    single character in this set" — so when a planner lists a path containing
    literal brackets (e.g. a Next.js dynamic route like
    ``app/invite/[token]/page.tsx``) verbatim in ``allowed_paths``, fnmatch
    alone can never match that path against itself, un-healably rejecting
    every future job that touches it as out of lane.
    """
    if not scope:
        return []
    allowed_paths = scope.get("allowed_paths") or []
    if not allowed_paths:
        return []
    out_of_lane = []
    for f in changed_files:
        if any(f == pattern or fnmatch(f, pattern) for pattern in allowed_paths):
            continue
        if Path(f).name in EXEMPT_LOCKFILE_BASENAMES and _lockfile_dir_in_scope(f, allowed_paths):
            continue
        if worktree is not None and _is_alembic_merge_resolution(f, worktree):
            continue
        out_of_lane.append(f)
    return out_of_lane


def evaluate_scope(
    scope: dict | None,
    changed_files: list[str],
    worktree: str | Path | None = None,
    *,
    unexpected_limit: int = SCOPE_CHECK_UNEXPECTED_LIMIT,
) -> ScopeCheckResult:
    """Evaluate the existing out-of-lane policy with bounded diagnostics.

    Matching and exemptions remain owned by :func:`check_out_of_lane`. Missing
    or empty manifests are explicitly non-enforced. Inputs are never mutated.
    """
    allowed_paths = tuple((scope or {}).get("allowed_paths") or ())
    if not allowed_paths:
        return ScopeCheckResult(False, (), (), 0, False)
    if unexpected_limit < 0:
        raise ValueError("unexpected_limit must be non-negative")
    unexpected = sorted(set(check_out_of_lane(scope, changed_files, worktree)))
    return ScopeCheckResult(
        True,
        allowed_paths,
        tuple(unexpected[:unexpected_limit]),
        len(unexpected),
        len(unexpected) > unexpected_limit,
    )
