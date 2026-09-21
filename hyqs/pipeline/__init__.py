"""The autonomous SDLC pipeline: idea -> plan -> build -> test -> review -> merge."""

from .agents import SECURITY_SYS, resolve_current_executor
from .collision import (
    AUTHORIZED_AMENDMENT_GATES,
    AUTHORIZED_GATE_EVIDENCE_CATEGORIES,
    SCOPE_AMENDMENT_CUMULATIVE_LIMIT,
    SCOPE_AMENDMENT_PER_CYCLE_LIMIT,
    ScopeAmendmentAccepted,
    ScopeAmendmentDecision,
    ScopeAmendmentRejected,
    evaluate_scope_amendment,
    normalize_scope_amendment_path,
)
from .models import AgentSpec, CurrentExecutor, Job, JobStatus, Project, SiteStats, Stage
from .runner import FailureDetail, PipelineRunner
from .stages import (
    AUTHORIZED_GATE_EVIDENCE_PATH_LIMIT,
    DESIGN_REVIEW_CHECK_ID,
    SECURITY_CHECK_ID,
    AuthorizedGateFailureEvidence,
    authorized_gate_failure,
    build_authorized_gate_failure_evidence,
    compose_authorized_gate_failure_detail,
    compose_authorized_gate_failure_detail_if_valid,
    compose_design_review_failure_detail,
    compose_security_failure_detail,
)
from .store import JobStore, ScopeAmendmentPersistenceResult

__all__ = [
    "AUTHORIZED_AMENDMENT_GATES",
    "AUTHORIZED_GATE_EVIDENCE_CATEGORIES",
    "AUTHORIZED_GATE_EVIDENCE_PATH_LIMIT",
    "SCOPE_AMENDMENT_CUMULATIVE_LIMIT",
    "SCOPE_AMENDMENT_PER_CYCLE_LIMIT",
    "AuthorizedGateFailureEvidence",
    "AgentSpec",
    "DESIGN_REVIEW_CHECK_ID",
    "CurrentExecutor",
    "FailureDetail",
    "Job",
    "JobStatus",
    "JobStore",
    "PipelineRunner",
    "Project",
    "ScopeAmendmentAccepted",
    "ScopeAmendmentDecision",
    "ScopeAmendmentRejected",
    "ScopeAmendmentPersistenceResult",
    "SECURITY_SYS",
    "SECURITY_CHECK_ID",
    "SiteStats",
    "Stage",
    "authorized_gate_failure",
    "build_authorized_gate_failure_evidence",
    "compose_authorized_gate_failure_detail",
    "compose_authorized_gate_failure_detail_if_valid",
    "compose_design_review_failure_detail",
    "compose_security_failure_detail",
    "evaluate_scope_amendment",
    "normalize_scope_amendment_path",
    "resolve_current_executor",
]
