"""Authenticated MCP client for hyqs-jobs, plus response classification.

Two auth modes, chosen at ``build_client`` time:

- Non-interactive (preferred for scripts/cron/containers): if
  ``API_TOKEN_ENV_VAR`` is set, a project-scoped API token is sent as a
  bearer credential — the same convention the REST layer already accepts
  (see ``hyqs/web/auth.py``) — via ``fastmcp.client.auth.BearerAuth``. No
  OAuth provider or token cache is touched in this mode.
- Interactive (the default when the variable is unset): reuses the same
  Google-backed OAuth the MCP server terminates (see
  ``hyqs/web/mcp_oauth.py``) — ``fastmcp.client.auth.OAuth`` drives the
  browser-based authorization-code flow against the server's ``/mcp``
  resource, and a persistent on-disk token cache means a re-run of any
  ``hyqs-jobs`` command doesn't re-open a browser.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.auth import BearerAuth, OAuth
from fastmcp.client.logging import LogHandler
from key_value.aio.stores.filetree import FileTreeStore

from hyqs.config import Config


# Set this to a project-scoped API token to authenticate non-interactively,
# skipping the browser OAuth flow entirely (see build_client).
API_TOKEN_ENV_VAR = "HYQS_JOBS_API_TOKEN"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_TRANSPORT_ERROR = 2
EXIT_FORBIDDEN = 3
EXIT_NOT_FOUND = 4


def build_client(config: Config | None = None, log_handler: LogHandler | None = None) -> Client:
    """A fastmcp Client authenticated against this deployment's MCP resource.

    If ``API_TOKEN_ENV_VAR`` is set, authenticates with that token as a
    bearer credential and never constructs the OAuth provider or touches the
    token cache. Otherwise falls back to the interactive OAuth flow, whose
    token/client-registration state is cached on disk under
    ``Config.data_dir/cli-oauth`` (via ``FileTreeStore``) instead of fastmcp's
    default in-memory store, so the browser flow only runs once per host.
    """
    config = config or Config.from_env()
    resource_url = config.resolved_mcp_resource_url()
    if not resource_url:
        raise ValueError(
            "set HYQS_MCP_RESOURCE_URL (or HYQS_WEB_BASE_URL) to the Hyqs server "
            "this CLI should talk to"
        )
    api_token = os.environ.get(API_TOKEN_ENV_VAR, "").strip()
    if api_token:
        return Client(resource_url, auth=BearerAuth(api_token), log_handler=log_handler)
    token_storage = FileTreeStore(data_directory=Path(config.data_dir) / "cli-oauth")
    auth = OAuth(mcp_url=resource_url, token_storage=token_storage)
    return Client(resource_url, auth=auth, log_handler=log_handler)


def classify_payload(payload: Any) -> int:
    """Map a successfully-returned tool payload to a CLI exit code.

    The MCP tools in ``hyqs/web/mcp_server.py`` report failure by returning an
    ``"error: ..."`` string rather than raising (see e.g. ``get_job``,
    ``_create_job_impl``), so classification is a text match on that
    convention, not an exception check.
    """
    if isinstance(payload, str) and payload.startswith("error:"):
        lowered = payload.lower()
        if "forbidden" in lowered:
            return EXIT_FORBIDDEN
        if "not found" in lowered:
            return EXIT_NOT_FOUND
        return EXIT_ERROR
    return EXIT_OK


def _is_auth_rejection(exc: Exception) -> bool:
    """True if ``exc`` is an HTTP 401/403 response — the server rejected our credential."""
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    return status_code in (401, 403)


async def call_tool(client: Client, name: str, arguments: dict) -> tuple[Any, int]:
    """Call an MCP tool inside its own session and classify the outcome.

    Returns ``(payload, exit_code)``. A rejected credential (HTTP 401/403 while
    connecting or calling) is reported as a single-line forbidden message
    instead of an unhandled exception. Any other transport/protocol failure
    (connection error, or the tool itself raising instead of returning an
    error string) is not something a caller can recover from here, so it is
    collapsed to ``EXIT_TRANSPORT_ERROR`` with a ``None`` payload.
    """
    try:
        async with client:
            result = await client.call_tool(name, arguments, raise_on_error=False)
    except Exception as exc:
        if _is_auth_rejection(exc):
            message = "error: forbidden — the supplied token was rejected by the server"
            return message, EXIT_FORBIDDEN
        return None, EXIT_TRANSPORT_ERROR
    if result.is_error:
        return result.data, EXIT_TRANSPORT_ERROR
    return result.data, classify_payload(result.data)
