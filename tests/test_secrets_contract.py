from __future__ import annotations

import json

import pytest

from hyqs.pipeline.secret_provider import FileSecretProvider
from hyqs.pipeline.secrets_contract import (
    MissingSecret,
    SecretsContractError,
    find_missing_secrets,
    parse_secrets_contract,
)


def test_parse_valid_contract_with_multiple_owners():
    text = json.dumps(
        {
            "secrets": [
                {"name": "DB_PASSWORD", "owner": "client", "description": "db pw"},
                {"name": "SENTRY_DSN", "owner": "platform", "required": False},
            ]
        }
    )

    contract = parse_secrets_contract(text)

    assert [s.name for s in contract.secrets] == ["DB_PASSWORD", "SENTRY_DSN"]
    assert contract.secrets[0].owner == "client"
    assert contract.secrets[1].owner == "platform"
    assert contract.secrets[1].required is False


def test_parse_raises_for_malformed_json():
    with pytest.raises(SecretsContractError):
        parse_secrets_contract("{not json")


def test_parse_raises_for_missing_secrets_key():
    with pytest.raises(SecretsContractError):
        parse_secrets_contract(json.dumps({}))


def test_parse_raises_for_duplicate_name():
    text = json.dumps(
        {"secrets": [{"name": "DB_PASSWORD"}, {"name": "DB_PASSWORD"}]}
    )
    with pytest.raises(SecretsContractError):
        parse_secrets_contract(text)


def test_parse_raises_for_invalid_owner():
    text = json.dumps({"secrets": [{"name": "DB_PASSWORD", "owner": "hacker"}]})
    with pytest.raises(SecretsContractError):
        parse_secrets_contract(text)


def test_find_missing_secrets_flags_required_secret_absent_from_provider(tmp_path):
    contract = parse_secrets_contract(
        json.dumps({"secrets": [{"name": "DB_PASSWORD", "owner": "client"}]})
    )
    provider = FileSecretProvider(tmp_path)

    missing = find_missing_secrets(contract, provider)

    assert missing == [MissingSecret(name="DB_PASSWORD", owner="client", reason="missing")]


def test_find_missing_secrets_flags_invalid_format(tmp_path):
    (tmp_path / "API_KEY").write_text("not-hex")
    contract = parse_secrets_contract(
        json.dumps(
            {
                "secrets": [
                    {"name": "API_KEY", "owner": "platform", "format": "[0-9a-f]{6,}"}
                ]
            }
        )
    )
    provider = FileSecretProvider(tmp_path)

    missing = find_missing_secrets(contract, provider)

    assert missing == [MissingSecret(name="API_KEY", owner="platform", reason="invalid_format")]


def test_find_missing_secrets_does_not_flag_absent_optional_secret(tmp_path):
    contract = parse_secrets_contract(
        json.dumps(
            {"secrets": [{"name": "OPTIONAL_TOKEN", "owner": "client", "required": False}]}
        )
    )
    provider = FileSecretProvider(tmp_path)

    assert find_missing_secrets(contract, provider) == []


def test_find_missing_secrets_preserves_owner_for_client_and_platform_gaps(tmp_path):
    contract = parse_secrets_contract(
        json.dumps(
            {
                "secrets": [
                    {"name": "CLIENT_SECRET", "owner": "client"},
                    {"name": "PLATFORM_SECRET", "owner": "platform"},
                ]
            }
        )
    )
    provider = FileSecretProvider(tmp_path)

    missing = find_missing_secrets(contract, provider)

    owners = {m.name: m.owner for m in missing}
    assert owners == {"CLIENT_SECRET": "client", "PLATFORM_SECRET": "platform"}
