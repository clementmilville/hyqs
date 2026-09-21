"""Web layer: JSON + SSE API and the React SPA."""

from hyqs.pipeline.stages.security import SECURITY_CHECK_ID, compose_security_failure_detail

from .app import build_app, job_to_dict, serve

__all__ = [
    "SECURITY_CHECK_ID",
    "build_app",
    "compose_security_failure_detail",
    "job_to_dict",
    "serve",
]
