"""Structured logging setup: every log record — structlog-native or plain
stdlib ``logging.getLogger(...)`` — is rendered as one queryable JSON line
carrying whatever job context (job_id/stage/project_id) is bound via
``structlog.contextvars`` at the time it's emitted.

Call ``configure_logging(...)`` once, at process boot, before any other
logging occurs.
"""

from __future__ import annotations

import logging

import structlog

_SHARED_PRE_CHAIN = [
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso"),
]


def configure_logging(log_format: str = "json") -> None:
    """Install structlog + a matching root stdlib handler.

    ``log_format="json"`` (default) renders every record — including plain
    ``logging.getLogger("hyqs...").info(...)`` calls throughout the codebase —
    as a single JSON line. ``log_format="console"`` renders human-readable
    output for local dev.
    """
    renderer = (
        structlog.dev.ConsoleRenderer()
        if log_format == "console"
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[
            *_SHARED_PRE_CHAIN,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=_SHARED_PRE_CHAIN,
    )
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
