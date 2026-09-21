"""Hyqs — a living 24/7 personal AI orchestration agent."""

from .pipeline.stages.security import SECURITY_CHECK_ID, compose_security_failure_detail

__version__ = "0.1.0"

__all__ = ["SECURITY_CHECK_ID", "compose_security_failure_detail"]
