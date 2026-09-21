"""Unit tests for hyqs.pipeline.activation — pure, no mocking required."""

from __future__ import annotations

from hyqs.pipeline.activation import (
    ActivationVerification,
    extract_activation_env_var,
    verify_env_activation,
)


def test_extract_activation_env_var_finds_token_in_location():
    var = extract_activation_env_var("repo-root .env: HYQS_GITHUB_ORG", "org is set")
    assert var == "HYQS_GITHUB_ORG"


def test_extract_activation_env_var_falls_back_to_expected_live_effect():
    var = extract_activation_env_var(
        "repo-root .env", "New repos are created under HYQS_GITHUB_ORG once set"
    )
    assert var == "HYQS_GITHUB_ORG"


def test_extract_activation_env_var_returns_none_when_neither_names_one():
    var = extract_activation_env_var("hyqs/config.py FOO flag", "widgets turn blue")
    assert var is None


def test_verify_env_activation_satisfied_when_value_non_empty():
    verdict, evidence = verify_env_activation("HYQS_GITHUB_ORG", {"HYQS_GITHUB_ORG": "acme"})
    assert verdict is ActivationVerification.satisfied
    assert "HYQS_GITHUB_ORG" in evidence


def test_verify_env_activation_unsatisfied_when_key_absent():
    verdict, evidence = verify_env_activation("HYQS_GITHUB_ORG", {})
    assert verdict is ActivationVerification.unsatisfied
    assert "HYQS_GITHUB_ORG" in evidence


def test_verify_env_activation_unsatisfied_when_value_empty_string():
    verdict, _ = verify_env_activation("HYQS_GITHUB_ORG", {"HYQS_GITHUB_ORG": ""})
    assert verdict is ActivationVerification.unsatisfied


def test_verify_env_activation_unsatisfied_when_value_whitespace_only():
    verdict, _ = verify_env_activation("HYQS_GITHUB_ORG", {"HYQS_GITHUB_ORG": "   "})
    assert verdict is ActivationVerification.unsatisfied


def test_verify_env_activation_unobservable_when_var_name_none():
    verdict, evidence = verify_env_activation(None, {"HYQS_GITHUB_ORG": "acme"})
    assert verdict is ActivationVerification.unobservable
    assert evidence
