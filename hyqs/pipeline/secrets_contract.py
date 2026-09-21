"""Declared secrets contract parsing + fail-closed validation.

A project DECLARES the secrets a release requires in a repo-root file (e.g.
`secrets.schema.json`) — this module never scans code to derive that list.
Validation only checks whether a `SecretProvider` (see
`hyqs.pipeline.secret_provider`) can resolve each declared secret; it never
reads, logs, or persists the resolved values themselves.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .secret_provider import SecretProvider

_VALID_OWNERS = {"client", "platform"}


class SecretsContractError(ValueError):
    """The contract text is malformed or violates the declared-contract schema."""


@dataclass(slots=True)
class SecretSpec:
    name: str
    description: str = ""
    owner: str = "client"
    required: bool = True
    format: str = ""


@dataclass(slots=True)
class SecretsContract:
    secrets: list[SecretSpec] = field(default_factory=list)


@dataclass(slots=True)
class MissingSecret:
    name: str
    owner: str
    reason: str  # "missing" | "invalid_format"


def parse_secrets_contract(text: str) -> SecretsContract:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise SecretsContractError(f"invalid JSON: {e}") from e

    if not isinstance(data, dict) or not isinstance(data.get("secrets"), list):
        raise SecretsContractError("contract must be a JSON object with a 'secrets' list")

    specs: list[SecretSpec] = []
    seen: set[str] = set()
    for entry in data["secrets"]:
        if not isinstance(entry, dict) or not entry.get("name"):
            raise SecretsContractError("every secret entry must have a non-empty 'name'")
        name = entry["name"]
        if name in seen:
            raise SecretsContractError(f"duplicate secret name: {name}")
        seen.add(name)
        owner = entry.get("owner", "client")
        if owner not in _VALID_OWNERS:
            raise SecretsContractError(
                f"secret {name!r} has invalid owner {owner!r}; must be one of {_VALID_OWNERS}"
            )
        specs.append(
            SecretSpec(
                name=name,
                description=entry.get("description", ""),
                owner=owner,
                required=entry.get("required", True),
                format=entry.get("format", ""),
            )
        )
    return SecretsContract(secrets=specs)


def find_missing_secrets(
    contract: SecretsContract, provider: SecretProvider
) -> list[MissingSecret]:
    missing: list[MissingSecret] = []
    for spec in contract.secrets:
        if not spec.required:
            continue
        value = provider.get(spec.name)
        if not value:
            missing.append(MissingSecret(name=spec.name, owner=spec.owner, reason="missing"))
            continue
        if spec.format and not re.fullmatch(spec.format, value):
            missing.append(
                MissingSecret(name=spec.name, owner=spec.owner, reason="invalid_format")
            )
    return missing
