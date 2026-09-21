"""Pure unit tests for the required-env manifest parser + matcher. No I/O, no docker."""

from __future__ import annotations

from hyqs.pipeline.required_env import (
    RequiredEnvVar,
    find_missing_required_env,
    format_missing_env_message,
    parse_required_env,
)


def test_parse_required_env_ignores_comments_and_blank_lines():
    text = "\n# a comment\nFOO\n\n  # another\nbackend:BAR\n"

    parsed = parse_required_env(text)

    assert parsed == [
        RequiredEnvVar(name="FOO"),
        RequiredEnvVar(name="BAR", service="backend"),
    ]


def test_parse_required_env_handles_both_syntaxes():
    parsed = parse_required_env("FERNET_KEY\nweb:LETZ_ALLOWED_AUTH_HOSTS\n")

    assert parsed == [
        RequiredEnvVar(name="FERNET_KEY"),
        RequiredEnvVar(name="LETZ_ALLOWED_AUTH_HOSTS", service="web"),
    ]


def test_parse_required_env_strips_whitespace_around_entries():
    parsed = parse_required_env("  FOO  \n  backend : BAR  \n")

    assert parsed == [
        RequiredEnvVar(name="FOO"),
        RequiredEnvVar(name="BAR", service="backend"),
    ]


def test_find_missing_required_env_empty_manifest_returns_empty():
    assert find_missing_required_env([], {"app": {}}) == []


def test_find_missing_required_env_unqualified_present_in_any_service_passes():
    vars_ = [RequiredEnvVar(name="FOO")]
    service_envs = {"backend": {}, "frontend": {"FOO": "bar"}}

    assert find_missing_required_env(vars_, service_envs) == []


def test_find_missing_required_env_unqualified_missing_from_all_services():
    vars_ = [RequiredEnvVar(name="FOO")]
    service_envs = {"backend": {}, "frontend": {"OTHER": "x"}}

    assert find_missing_required_env(vars_, service_envs) == vars_


def test_find_missing_required_env_empty_string_value_counts_as_missing():
    vars_ = [RequiredEnvVar(name="FOO")]
    service_envs = {"app": {"FOO": ""}}

    assert find_missing_required_env(vars_, service_envs) == vars_


def test_find_missing_required_env_service_qualified_checks_only_that_service():
    vars_ = [RequiredEnvVar(name="FOO", service="backend")]
    service_envs = {"backend": {}, "frontend": {"FOO": "bar"}}

    assert find_missing_required_env(vars_, service_envs) == vars_


def test_find_missing_required_env_service_qualified_present_passes():
    vars_ = [RequiredEnvVar(name="FOO", service="backend")]
    service_envs = {"backend": {"FOO": "bar"}, "frontend": {}}

    assert find_missing_required_env(vars_, service_envs) == []


def test_find_missing_required_env_service_qualified_missing_service_treated_missing():
    vars_ = [RequiredEnvVar(name="FOO", service="backend")]
    service_envs = {"frontend": {"FOO": "bar"}}

    assert find_missing_required_env(vars_, service_envs) == vars_


def test_format_missing_env_message_empty_returns_empty_string():
    assert format_missing_env_message([], ["app"]) == ""


def test_format_missing_env_message_names_every_missing_var_and_service():
    missing = [
        RequiredEnvVar(name="FERNET_KEY"),
        RequiredEnvVar(name="LETZ_ALLOWED_AUTH_HOSTS", service="backend"),
    ]

    message = format_missing_env_message(missing, ["backend", "frontend"])

    assert "FERNET_KEY" in message
    assert "backend, frontend" in message
    assert "LETZ_ALLOWED_AUTH_HOSTS" in message
    assert "service: backend" in message


def test_format_missing_env_message_hints_both_causes():
    message = format_missing_env_message([RequiredEnvVar(name="FOO")], ["app"])

    assert "unset in .env" in message
    assert "not mapped into" in message
