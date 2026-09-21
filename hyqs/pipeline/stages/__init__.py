"""Per-stage orchestration handlers + the stage->handler dispatch table.

Each module exposes `async def run(rn, job)`. HANDLERS maps a job's current
Stage (the last checkpoint reached) to the handler that does the *next* unit of
work — e.g. Stage.BUILD's handler runs the tests. Mirrors runner._EXEC_STEP.

This package also exposes the provider-neutral payload built from authorized
gate failures. Structured fields keep provider prose from becoming filesystem
authority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from ..collision import (
    AUTHORIZED_AMENDMENT_GATES,
    AUTHORIZED_GATE_EVIDENCE_CATEGORIES,
    SCOPE_AMENDMENT_CUMULATIVE_LIMIT,
    SCOPE_AMENDMENT_PER_CYCLE_LIMIT,
    normalize_scope_amendment_path,
)
from ..models import Stage
from . import (
    build,
    deploy,
    design_review,
    fix,
    lint,
    merge,
    merge_verify,
    plan,
    review,
    security,
    test,
)
from .design_review import DESIGN_REVIEW_CHECK_ID, compose_design_review_failure_detail
from .security import SECURITY_CHECK_ID, compose_security_failure_detail

AUTHORIZED_GATE_EVIDENCE_PATH_LIMIT = 32

_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$")


@dataclass(frozen=True)
class AuthorizedGateFailureEvidence:
    """Stable, bounded evidence emitted by an authorized failing gate."""

    gate: str
    check_id: str
    event_id: str
    failing_paths: tuple[str, ...]
    categories: tuple[str, ...]

    def to_dict(self) -> dict[str, str | list[str]]:
        """Return a JSON-safe payload suitable for persistence and retries."""
        return {
            "gate": self.gate,
            "check_id": self.check_id,
            "event_id": self.event_id,
            "failing_paths": list(self.failing_paths),
            "categories": list(self.categories),
        }


def _canonical_gate(gate: Stage | str) -> str:
    value = gate.value if isinstance(gate, Stage) else str(gate)
    return value.strip().lower().replace("-", "_")


def _stable_identity(value: object, name: str) -> str:
    if not isinstance(value, str) or not _IDENTITY_RE.fullmatch(value):
        raise ValueError(f"{name} must be a non-empty stable identity")
    return value


def authorized_gate_failure(
    gate: Stage | str,
    check_id: str,
    event_id: str,
    failing_paths: Iterable[str],
    categories: Iterable[str],
) -> AuthorizedGateFailureEvidence:
    """Construct deterministic authorized-gate evidence or fail closed."""
    canonical_gate = _canonical_gate(gate)
    if canonical_gate not in AUTHORIZED_AMENDMENT_GATES:
        raise ValueError(f"gate cannot authorize scope amendments: {canonical_gate!r}")
    stable_check_id = _stable_identity(check_id, "check_id")
    stable_event_id = _stable_identity(event_id, "event_id")

    try:
        raw_paths = tuple(failing_paths)
        canonical_paths = tuple(normalize_scope_amendment_path(value) for value in raw_paths)
    except (AttributeError, TypeError) as exc:
        raise ValueError("failing_paths must be an iterable of concrete paths") from exc
    if not canonical_paths or any(path is None for path in canonical_paths):
        raise ValueError("failing_paths must contain only concrete repository paths")
    normalized_paths = tuple(sorted(set(canonical_paths)))
    if len(normalized_paths) > AUTHORIZED_GATE_EVIDENCE_PATH_LIMIT:
        raise ValueError(
            f"failing path evidence exceeds limit of {AUTHORIZED_GATE_EVIDENCE_PATH_LIMIT}"
        )

    try:
        raw_categories = tuple(categories)
    except TypeError as exc:
        raise ValueError("categories must be an iterable") from exc
    if any(not isinstance(category, str) or not category.strip() for category in raw_categories):
        raise ValueError("evidence categories must be non-empty strings")
    normalized_categories = tuple(
        sorted({category.strip().lower().replace("-", "_") for category in raw_categories})
    )
    if not normalized_categories:
        raise ValueError("at least one deterministic evidence category is required")
    unknown = set(normalized_categories) - AUTHORIZED_GATE_EVIDENCE_CATEGORIES
    if unknown:
        raise ValueError(f"unknown evidence categories: {', '.join(sorted(unknown))}")

    return AuthorizedGateFailureEvidence(
        canonical_gate,
        stable_check_id,
        stable_event_id,
        normalized_paths,
        normalized_categories,
    )


build_authorized_gate_failure_evidence = authorized_gate_failure


def compose_authorized_gate_failure_detail(
    detail: Mapping[str, object],
    gate: Stage | str,
    check_id: str,
    event_id: str,
    failing_paths: Iterable[str],
    categories: Iterable[str],
) -> dict[str, object]:
    """Copy *detail* and attach canonical authorized-gate failure evidence."""
    composed = dict(detail)
    composed["authorized_gate_failure"] = authorized_gate_failure(
        gate,
        check_id,
        event_id,
        failing_paths,
        categories,
    ).to_dict()
    return composed


def compose_authorized_gate_failure_detail_if_valid(
    detail: Mapping[str, object],
    gate: Stage | str,
    check_id: str,
    event_id: str,
    failing_paths: Iterable[str],
    categories: Iterable[str],
) -> dict[str, object]:
    """Copy *detail*, attaching evidence only when every input is authorizing.

    Gate handlers use this fail-closed boundary because unavailable or ambiguous
    deterministic path inputs must not turn diagnostic prose into scope authority.
    The strict composer remains available to callers that need validation errors.
    """
    composed = dict(detail)
    try:
        stable_check_id = _stable_identity(check_id, "check_id")
        stable_event_id = _stable_identity(event_id, "event_id")
    except ValueError:
        return composed
    composed.setdefault("check_id", stable_check_id)
    composed.setdefault("event_id", stable_event_id)
    try:
        evidence = authorized_gate_failure(
            gate,
            check_id,
            event_id,
            failing_paths,
            categories,
        )
    except ValueError:
        return composed
    composed["authorized_gate_failure"] = evidence.to_dict()
    return composed


HANDLERS = {
    Stage.QUEUED: plan.run,
    Stage.PLAN: build.run,
    Stage.LINT: lint.run,
    Stage.BUILD: test.run,
    Stage.TEST: review.run,
    Stage.REVIEW: security.run,
    Stage.SECURITY: design_review.run,
    Stage.DESIGN_REVIEW: merge.run,
    Stage.MERGE_VERIFY: merge_verify.run,
    Stage.DEPLOY: deploy.run,
    Stage.FIX: fix.run,
}

__all__ = [
    "AUTHORIZED_AMENDMENT_GATES",
    "AUTHORIZED_GATE_EVIDENCE_CATEGORIES",
    "AUTHORIZED_GATE_EVIDENCE_PATH_LIMIT",
    "SCOPE_AMENDMENT_CUMULATIVE_LIMIT",
    "SCOPE_AMENDMENT_PER_CYCLE_LIMIT",
    "AuthorizedGateFailureEvidence",
    "DESIGN_REVIEW_CHECK_ID",
    "HANDLERS",
    "SECURITY_CHECK_ID",
    "authorized_gate_failure",
    "build_authorized_gate_failure_evidence",
    "compose_authorized_gate_failure_detail",
    "compose_authorized_gate_failure_detail_if_valid",
    "compose_design_review_failure_detail",
    "compose_security_failure_detail",
    "normalize_scope_amendment_path",
]
