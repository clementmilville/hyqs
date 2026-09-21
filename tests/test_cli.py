"""Tests for the hyqs-jobs CLI: OAuth client wiring, response classification,
and argparse-to-MCP-tool dispatch (job #2892)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hyqs.cli import client as cli_client
from hyqs.cli import main as cli_main
from hyqs.cli.client import (
    EXIT_ERROR,
    EXIT_FORBIDDEN,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_TRANSPORT_ERROR,
    build_client,
    call_tool,
    classify_payload,
)
from hyqs.cli.main import _build_call, _parse_args, main
from hyqs.config import Config


def _config(tmp_path) -> Config:
    # web_base_url is what the MCP resource URL is derived from when
    # HYQS_MCP_RESOURCE_URL is not set explicitly.
    return Config(
        model="sonnet",
        permission_mode="acceptEdits",
        data_dir=tmp_path,
        web_base_url="https://hyqs.example.test",
    )


# ---------------------------------------------------------------------------
# classify_payload
# ---------------------------------------------------------------------------


def test_classify_payload_success_dict_is_ok():
    assert classify_payload({"job": {"id": 1}}) == EXIT_OK


def test_classify_payload_success_list_is_ok():
    assert classify_payload([{"id": 1}]) == EXIT_OK


def test_classify_payload_generic_error_string():
    assert classify_payload("error: idea is required") == EXIT_ERROR


def test_classify_payload_forbidden_string():
    assert (
        classify_payload("error: forbidden — you are not a member of this project")
        == EXIT_FORBIDDEN
    )


def test_classify_payload_not_found_string():
    assert classify_payload("error: job not found") == EXIT_NOT_FOUND


def test_classify_payload_not_found_string_bare():
    assert classify_payload("error: not found") == EXIT_NOT_FOUND


# ---------------------------------------------------------------------------
# build_client — persistent FileTreeStore token cache
# ---------------------------------------------------------------------------


def test_build_client_uses_filetree_store_under_data_dir(tmp_path, monkeypatch):
    from key_value.aio.stores.filetree import FileTreeStore

    monkeypatch.delenv(cli_client.API_TOKEN_ENV_VAR, raising=False)
    client = build_client(_config(tmp_path))
    auth = client.transport.auth
    assert isinstance(auth._token_storage, FileTreeStore)
    assert str(auth._token_storage._data_directory) == str((tmp_path / "cli-oauth").resolve())


def test_build_client_derives_resource_url_from_the_web_base_url(tmp_path, monkeypatch):
    monkeypatch.delenv(cli_client.API_TOKEN_ENV_VAR, raising=False)
    client = build_client(_config(tmp_path))
    assert client.transport.url == "https://hyqs.example.test/mcp"


def test_build_client_refuses_to_guess_a_server(tmp_path, monkeypatch):
    """With no URL configured there is no safe host to fall back to."""
    monkeypatch.delenv(cli_client.API_TOKEN_ENV_VAR, raising=False)
    config = _config(tmp_path)
    config.web_base_url = ""

    with pytest.raises(ValueError, match="HYQS_MCP_RESOURCE_URL"):
        build_client(config)


def test_build_client_uses_configured_resource_url(tmp_path, monkeypatch):
    monkeypatch.delenv(cli_client.API_TOKEN_ENV_VAR, raising=False)
    config = _config(tmp_path)
    config.mcp_resource_url = "https://example.test/mcp"
    client = build_client(config)
    assert client.transport.url == "https://example.test/mcp"


# ---------------------------------------------------------------------------
# build_client — non-interactive API-token bearer auth
# ---------------------------------------------------------------------------


def test_build_client_uses_bearer_auth_when_token_env_var_set(tmp_path, monkeypatch):
    from fastmcp.client.auth import BearerAuth

    monkeypatch.setenv(cli_client.API_TOKEN_ENV_VAR, "proj-scoped-token")
    client = build_client(_config(tmp_path))
    auth = client.transport.auth
    assert isinstance(auth, BearerAuth)
    assert auth.token.get_secret_value() == "proj-scoped-token"


def test_build_client_with_token_never_touches_oauth_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(cli_client.API_TOKEN_ENV_VAR, "proj-scoped-token")
    build_client(_config(tmp_path))
    assert not (tmp_path / "cli-oauth").exists()


def test_build_client_ignores_blank_token_env_var(tmp_path, monkeypatch):
    from fastmcp.client.auth import OAuth

    monkeypatch.setenv(cli_client.API_TOKEN_ENV_VAR, "   ")
    client = build_client(_config(tmp_path))
    assert isinstance(client.transport.auth, OAuth)


# ---------------------------------------------------------------------------
# call_tool — transport-failure handling
# ---------------------------------------------------------------------------


def test_call_tool_transport_exception_returns_exit_2():
    client = MagicMock()
    client.__aenter__ = AsyncMock(side_effect=ConnectionError("boom"))
    client.__aexit__ = AsyncMock(return_value=False)

    payload, code = asyncio.run(call_tool(client, "list_projects", {}))
    assert code == EXIT_TRANSPORT_ERROR
    assert payload is None


def test_call_tool_protocol_is_error_returns_exit_2():
    result = MagicMock()
    result.is_error = True
    result.data = "boom"
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.call_tool = AsyncMock(return_value=result)

    payload, code = asyncio.run(call_tool(client, "list_projects", {}))
    assert code == EXIT_TRANSPORT_ERROR
    assert payload == "boom"


def test_call_tool_rejected_token_returns_forbidden_with_one_line_message():
    import httpx

    response = MagicMock()
    response.status_code = 403
    error = httpx.HTTPStatusError("forbidden", request=MagicMock(), response=response)

    client = MagicMock()
    client.__aenter__ = AsyncMock(side_effect=error)
    client.__aexit__ = AsyncMock(return_value=False)

    payload, code = asyncio.run(call_tool(client, "list_projects", {}))
    assert code == EXIT_FORBIDDEN
    assert isinstance(payload, str)
    assert "\n" not in payload
    assert "forbidden" in payload.lower()


def test_call_tool_rejected_token_401_also_returns_forbidden():
    import httpx

    response = MagicMock()
    response.status_code = 401
    error = httpx.HTTPStatusError("unauthorized", request=MagicMock(), response=response)

    client = MagicMock()
    client.__aenter__ = AsyncMock(side_effect=error)
    client.__aexit__ = AsyncMock(return_value=False)

    payload, code = asyncio.run(call_tool(client, "list_projects", {}))
    assert code == EXIT_FORBIDDEN


def test_call_tool_non_auth_http_error_returns_exit_2():
    import httpx

    response = MagicMock()
    response.status_code = 500
    error = httpx.HTTPStatusError("boom", request=MagicMock(), response=response)

    client = MagicMock()
    client.__aenter__ = AsyncMock(side_effect=error)
    client.__aexit__ = AsyncMock(return_value=False)

    payload, code = asyncio.run(call_tool(client, "list_projects", {}))
    assert code == EXIT_TRANSPORT_ERROR
    assert payload is None


def test_call_tool_success_classifies_payload():
    result = MagicMock()
    result.is_error = False
    result.data = [{"id": 1}]
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.call_tool = AsyncMock(return_value=result)

    payload, code = asyncio.run(call_tool(client, "list_projects", {}))
    assert code == EXIT_OK
    assert payload == [{"id": 1}]


# ---------------------------------------------------------------------------
# _parse_args / _build_call — argparse dispatch to tool name + arguments
# ---------------------------------------------------------------------------


def test_parse_args_requires_resource():
    with pytest.raises(SystemExit):
        _parse_args([])


def test_parse_args_requires_action():
    with pytest.raises(SystemExit):
        _parse_args(["jobs"])


def test_build_call_projects_list():
    args = _parse_args(["projects", "list"])
    assert _build_call(args) == ("list_projects", {})


def test_build_call_epics_list():
    args = _parse_args(["epics", "list", "--project", "7"])
    assert _build_call(args) == ("list_epics", {"project_id": 7})


def test_build_call_jobs_list_defaults():
    args = _parse_args(["jobs", "list", "--project", "7"])
    assert _build_call(args) == (
        "list_jobs",
        {"project_id": 7, "status": "active", "summary": False},
    )


def test_build_call_jobs_list_with_status_and_summary():
    args = _parse_args(["jobs", "list", "--project", "7", "--status", "failed", "--summary"])
    assert _build_call(args) == (
        "list_jobs",
        {"project_id": 7, "status": "failed", "summary": True},
    )


def test_build_call_jobs_get():
    args = _parse_args(["jobs", "get", "42"])
    assert _build_call(args) == ("get_job", {"job_id": 42, "summary": False})


def test_build_call_jobs_get_summary():
    args = _parse_args(["jobs", "get", "42", "--summary"])
    assert _build_call(args) == ("get_job", {"job_id": 42, "summary": True})


def test_build_call_jobs_watch():
    args = _parse_args(["jobs", "watch", "42", "--timeout", "30"])
    assert _build_call(args) == ("watch_job", {"job_id": 42, "timeout_seconds": 30})


def test_build_call_jobs_watch_default_timeout():
    args = _parse_args(["jobs", "watch", "42"])
    assert _build_call(args) == ("watch_job", {"job_id": 42, "timeout_seconds": 120})


def test_build_call_jobs_create_reads_idea_file(tmp_path):
    idea_file = tmp_path / "idea.txt"
    idea_file.write_text("Add a widget.")
    args = _parse_args(
        [
            "jobs",
            "create",
            "--project",
            "7",
            "--epic",
            "3",
            "--title",
            "Add widget",
            "--idea-file",
            str(idea_file),
            "--depends-on",
            "1, 2,3",
        ]
    )
    assert _build_call(args) == (
        "create_job",
        {
            "idea": "Add a widget.",
            "project_id": 7,
            "title": "Add widget",
            "epic_id": 3,
            "depends_on": [1, 2, 3],
        },
    )


def test_build_call_jobs_create_without_epic_or_depends_on(tmp_path):
    idea_file = tmp_path / "idea.txt"
    idea_file.write_text("Add a widget.")
    args = _parse_args(
        [
            "jobs",
            "create",
            "--project",
            "7",
            "--title",
            "Add widget",
            "--idea-file",
            str(idea_file),
        ]
    )
    assert _build_call(args) == (
        "create_job",
        {
            "idea": "Add a widget.",
            "project_id": 7,
            "title": "Add widget",
            "epic_id": None,
            "depends_on": [],
        },
    )


# ---------------------------------------------------------------------------
# main() — end-to-end dispatch with the call_tool boundary mocked
# ---------------------------------------------------------------------------


def test_main_prints_json_and_exits_with_classified_code(capsys):
    with (
        patch.object(cli_main, "build_client", return_value=MagicMock()),
        patch.object(cli_main, "call_tool", new=AsyncMock(return_value=([{"id": 1}], EXIT_OK))),
    ):
        with pytest.raises(SystemExit) as exc_info:
            main(["projects", "list"])
    assert exc_info.value.code == EXIT_OK
    out = capsys.readouterr().out
    assert out.strip() == '[{"id": 1}]'


def test_main_exits_with_forbidden_code(capsys):
    with (
        patch.object(cli_main, "build_client", return_value=MagicMock()),
        patch.object(
            cli_main,
            "call_tool",
            new=AsyncMock(return_value=("error: forbidden — nope", EXIT_FORBIDDEN)),
        ),
    ):
        with pytest.raises(SystemExit) as exc_info:
            main(["jobs", "get", "1"])
    assert exc_info.value.code == EXIT_FORBIDDEN


def test_main_uses_log_handler_only_for_watch():
    captured = {}

    def _fake_build_client(config=None, log_handler=None):
        captured["log_handler"] = log_handler
        return MagicMock()

    with (
        patch.object(cli_main, "build_client", side_effect=_fake_build_client),
        patch.object(cli_main, "call_tool", new=AsyncMock(return_value=({}, EXIT_OK))),
    ):
        with pytest.raises(SystemExit):
            main(["projects", "list"])
        assert captured["log_handler"] is None

        with pytest.raises(SystemExit):
            main(["jobs", "watch", "1"])
        assert captured["log_handler"] is cli_main._watch_log_handler
