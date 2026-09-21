"""Stage I/O contracts: sentinel-based parsing + JSON-schema validation."""

from __future__ import annotations

import json
import posixpath
import re
from importlib import resources as importlib_resources
from typing import Any

import jsonschema

RESULT_START_MARKER = "<<<RESULT_JSON>>>"
RESULT_END_MARKER = "<<<END_RESULT>>>"

_ALREADY_SATISFIED_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "evidence": {"type": "string", "minLength": 1, "maxLength": 2000},
    },
    "required": ["verdict", "evidence"],
    "additionalProperties": False,
}


class ContractError(ValueError):
    """Base for contract violations."""


class ResultBlockError(ContractError):
    """No parseable result block found in agent output."""


class SchemaValidationError(ContractError):
    """Parsed result does not match the stage schema."""


def _extract_from(text: str, start: int) -> str | None:
    """Extract a balanced {...} object from text[start:], string/escape-aware."""
    depth = 0
    in_string = False
    i = start
    while i < len(text):
        c = text[i]
        if in_string:
            if c == "\\":
                i += 2  # skip the escaped character
                continue
            elif c == '"':
                in_string = False
        else:
            if c == '"':
                in_string = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        i += 1
    return None


def _brace_extract(text: str) -> str | None:
    """Return a balanced {...} object starting at the last '{' in text."""
    start = text.rfind("{")
    if start == -1:
        return None
    return _extract_from(text, start)


def parse_result_block(text: str) -> dict:
    """Parse a result dict from agent output.

    Priority: sentinel block > last fenced ```json block > last bare {...} object.
    """
    # 1. Sentinel block
    s = text.find(RESULT_START_MARKER)
    e = text.find(RESULT_END_MARKER)
    if s != -1 and e != -1 and e > s:
        block = text[s + len(RESULT_START_MARKER) : e].strip()
        try:
            return json.loads(block)
        except json.JSONDecodeError:
            pass  # fall through

    # 2. Fenced blocks (last-first)
    fenced = re.findall(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    candidates = [b.strip() for b in fenced if b.strip().startswith("{")]
    for cand in reversed(candidates):
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue

    # 3. Bare balanced object (last one)
    raw = _brace_extract(text)
    if raw is not None:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

    raise ResultBlockError("no parseable result block found in agent output")


def _load_schema(stage: str) -> dict[str, Any]:
    ref = importlib_resources.files("hyqs.pipeline.schemas").joinpath(
        f"{stage}-response.schema.json"
    )
    return json.loads(ref.read_text(encoding="utf-8"))


def validate_reuse_contract(reuses: list[str], index: dict[str, list]) -> list[str]:
    """Return reuse symbols not found in the project's symbol index.

    Builds the set of fully-qualified names from index as ``module.name`` pairs,
    then returns the subset of *reuses* that are absent.  An empty list means
    every cited symbol was found.
    """
    known = {f"{e['module']}.{name}" for name, entries in index.items() for e in entries}
    return [r for r in reuses if r not in known]


def validate_adds_contract(adds: list[str], index: dict[str, list]) -> list[str]:
    """Return add symbols that already exist in the project's symbol index.

    A non-empty result is a pre-emptive collision hint (not a hard failure):
    the plan claims to introduce a symbol that the index already knows about.
    """
    known = {f"{e['module']}.{name}" for name, entries in index.items() for e in entries}
    return [a for a in adds if a in known]


def parse_and_validate(stage: str, text: str) -> dict:
    """Parse agent output and validate against the stage's JSON schema.

    Raises ResultBlockError if no result block is found, SchemaValidationError
    if the parsed object does not match the schema.
    """
    data = parse_result_block(text)
    schema = _load_schema(stage)
    validator = jsonschema.Draft202012Validator(schema)
    errors = list(validator.iter_errors(data))
    if errors:
        msg = errors[0].message
        raise SchemaValidationError(f"{stage}-response schema violation: {msg}")
    if stage == "plan":
        _validate_plan_semantics(data)
    elif stage == "security":
        _validate_security_semantics(data)
    return data


def _canonical_repository_path(
    path: str,
    *,
    stage: str,
    location: str,
    reject_metadata: bool = False,
) -> str:
    """Return a canonical concrete repository-relative POSIX file path.

    ``reject_metadata`` is used for authoritative path metadata. PLAN retains
    its established accepted character set while sharing the canonical path
    checks with SECURITY.
    """
    if (
        not path
        or path.startswith("/")
        or re.match(r"^[A-Za-z]:", path)
        or "\\" in path
        or "\n" in path
        or "\r" in path
        or path.endswith("/")
        or any(part in ("", ".", "..") for part in path.split("/"))
    ):
        raise SchemaValidationError(
            f"{stage}-response semantic violation: {location} path {path!r} must be "
            "a canonical, safe repository-relative path"
        )
    canonical = posixpath.normpath(path)
    if canonical != path or canonical == ".." or canonical.startswith("../"):
        raise SchemaValidationError(
            f"{stage}-response semantic violation: {location} path {path!r} is "
            f"non-canonical or unsafe; use {canonical!r}"
        )
    if reject_metadata and (
        any(character.isspace() for character in path)
        or re.search(r"[*?\[\]{}(),:;|\"'<>]", path)
        or path.startswith("~")
    ):
        raise SchemaValidationError(
            f"{stage}-response semantic violation: {location} path {path!r} must be "
            "exactly one concrete repository-relative file path, not prose, a glob, "
            "a range, or a list"
        )
    return canonical


def _is_test_path(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    return (
        "tests" in parts
        or name.startswith("test_")
        or name.endswith(
            (
                ".test.js",
                ".test.jsx",
                ".test.ts",
                ".test.tsx",
                ".spec.js",
                ".spec.jsx",
                ".spec.ts",
                ".spec.tsx",
            )
        )
    )


def _validate_plan_semantics(data: dict) -> None:
    """Enforce PLAN invariants that JSON Schema cannot express."""
    plan_paths = [
        _canonical_repository_path(path, stage="plan", location="target_files")
        for path in data["target_files"]
    ]
    impact_paths = [
        _canonical_repository_path(item["path"], stage="plan", location="file_impact")
        for item in data["file_impact"]
    ]
    if len(impact_paths) != len(set(impact_paths)):
        raise SchemaValidationError(
            "plan-response semantic violation: file_impact paths must be unique"
        )

    story_paths: list[str] = []
    seen_ids: set[str] = set()
    implementation_ids: list[str] = []
    for index, story in enumerate(data["stories"]):
        story_id = story["id"]
        if story_id in seen_ids:
            raise SchemaValidationError(
                f"plan-response semantic violation: duplicate story id {story_id!r}"
            )
        dependencies = story.get("depends_on", [])
        invalid = [dependency for dependency in dependencies if dependency not in seen_ids]
        if invalid:
            raise SchemaValidationError(
                f"plan-response semantic violation: story {story_id!r} depends_on "
                f"unknown or non-earlier stories {invalid!r}; dependencies must reference "
                "unique earlier story IDs (which also guarantees an acyclic graph)"
            )
        paths = [
            _canonical_repository_path(
                path,
                stage="plan",
                location=f"stories[{index}].target_files",
            )
            for path in story["target_files"]
        ]
        story_paths.extend(paths)
        test_only = all(_is_test_path(path) for path in paths)
        if test_only:
            missing = [item for item in implementation_ids if item not in dependencies]
            # A test-only story must run after the implementation stories that
            # precede it when an oversized plan is split into separate jobs. The
            # planner occasionally omits one of these purely ordering-oriented
            # edges. Completing the conservative dependency set is deterministic,
            # additive, and does not grant filesystem scope, so canonicalize it
            # here rather than rejecting an otherwise valid plan.
            dependencies.extend(missing)
            story["depends_on"] = dependencies
        else:
            implementation_ids.append(story_id)
        seen_ids.add(story_id)

    expected = set(story_paths) | set(impact_paths)
    actual = set(plan_paths)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise SchemaValidationError(
            "plan-response semantic violation: target_files must exactly equal the "
            "union of stories[].target_files and file_impact[].path; "
            f"missing={missing!r}, extra={extra!r}"
        )

    ui_impact = data["ui_impact"]
    frontend_changes = ui_impact["frontend_changes"]
    reason = ui_impact["no_ui_change_reason"].strip()
    if ui_impact["touches_backend_surface"] and not frontend_changes:
        raise SchemaValidationError(
            "plan-response semantic violation: API/UI-visible plans must list "
            "the required frontend_changes"
        )
    if not ui_impact["touches_backend_surface"] and not reason:
        raise SchemaValidationError(
            "plan-response semantic violation: internal plans must provide a concise "
            "no_ui_change_reason explaining the omitted UI layer"
        )

    activation = data["activation"]
    if activation["config_change_required"]:
        if not activation["activation_location"].strip():
            raise SchemaValidationError(
                "plan-response semantic violation: plans requiring a config change must "
                "provide an activation_location"
            )
        if not activation["expected_live_effect"].strip():
            raise SchemaValidationError(
                "plan-response semantic violation: plans requiring a config change must "
                "provide an expected_live_effect"
            )


def _validate_security_semantics(data: dict) -> None:
    """Validate optional authoritative SECURITY finding paths."""
    for index, finding in enumerate(data.get("findings", [])):
        if "path" not in finding:
            continue
        _canonical_repository_path(
            finding["path"],
            stage="security",
            location=f"findings[{index}]",
            reject_metadata=True,
        )


def parse_already_satisfied_verification(text: str) -> dict:
    """Parse the independent no-diff verification result.

    This contract intentionally lives in code rather than the general stage
    schema registry: it is a small runner decision, not a pipeline stage.
    """
    data = parse_result_block(text)
    validator = jsonschema.Draft202012Validator(_ALREADY_SATISFIED_SCHEMA)
    errors = list(validator.iter_errors(data))
    if errors:
        raise SchemaValidationError(
            f"already-satisfied verification schema violation: {errors[0].message}"
        )
    if not data["evidence"].strip():
        raise SchemaValidationError(
            "already-satisfied verification schema violation: evidence must not be blank"
        )
    return data
