"""Opt-in deploy-time required-env manifest: parsing + missing-var matching.

A project DECLARES the env vars its running container(s) must have via a
`deploy/required-env` file in the project repo — this module never parses
app code or guesses required vars. Absence of the file means the check is a
complete no-op. Mirrors the style of `hyqs.pipeline.secrets_contract`, but
validates against the ACTUAL running container's env (read by the caller via
`docker inspect`) rather than a `SecretProvider`, so it catches both an unset
var and a var set in `.env` but never mapped into a compose service's
`environment:` block.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class RequiredEnvVar:
    name: str
    service: str | None = None


def parse_required_env(text: str) -> list[RequiredEnvVar]:
    """Parse one entry per line: 'VAR' or 'service:VAR'. '#' comments and blank
    lines are ignored."""
    entries: list[RequiredEnvVar] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        service, sep, name = line.partition(":")
        if sep:
            entries.append(RequiredEnvVar(name=name.strip(), service=service.strip()))
        else:
            entries.append(RequiredEnvVar(name=line))
    return entries


def find_missing_required_env(
    vars_: list[RequiredEnvVar], service_envs: dict[str, dict[str, str]]
) -> list[RequiredEnvVar]:
    """Return the subset of ``vars_`` missing (or empty) in ``service_envs``.

    A service-qualified entry (``service:VAR``) is checked only against that
    service's env. An unqualified entry (``VAR``) is checked against every
    known service — it's only "missing" if it's missing/empty in ALL of them.
    """
    missing: list[RequiredEnvVar] = []
    for var in vars_:
        if var.service is not None:
            env = service_envs.get(var.service, {})
            if not env.get(var.name):
                missing.append(var)
        else:
            if not any(env.get(var.name) for env in service_envs.values()):
                missing.append(var)
    return missing


def format_missing_env_message(missing: list[RequiredEnvVar], known_services: list[str]) -> str:
    """Name every missing var and its service, and hint at the two known causes."""
    if not missing:
        return ""
    lines = []
    for var in missing:
        service_desc = var.service or ", ".join(known_services) or "the app service"
        lines.append(f"  - {var.name} (service: {service_desc})")
    return (
        "required env var(s) declared in deploy/required-env are missing from the "
        "running container:\n"
        + "\n".join(lines)
        + "\n\nLikely cause: the var is unset in .env/host secrets, or it's set in "
        ".env but not mapped into the service's `environment:` block in "
        "docker-compose.yml."
    )
