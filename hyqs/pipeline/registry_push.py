"""Registry push/pull/sign primitives — NO AI, pure subprocess.

Pushes a locally-built Docker image to the project's self-hosted OCI registry
(Zot) and cosign-signs its digest. The env vars this module reads for pushing
(HYQS_REGISTRY_PUSH_*, HYQS_COSIGN_KEY_*) carry a push credential and a signing
key, so they must only ever be set on the build host — never on a deploy or
client host.

The pull/verify side (HYQS_REGISTRY_PULL_*, HYQS_COSIGN_PUBLIC_KEY_PATH) is the
mirror image: a pull-only credential and a public verify key, meant only for
deploy/client hosts that apply pre-built releases by digest — never a build host.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Awaitable, Callable

log = logging.getLogger("hyqs.registry_push")

_TRUTHY = {"1", "true", "yes", "on"}

_REQUIRED_VARS = (
    "HYQS_REGISTRY_DOMAIN",
    "HYQS_REGISTRY_PUSH_USER",
    "HYQS_REGISTRY_PUSH_PASS",
    "HYQS_COSIGN_KEY_PATH",
)

_PULL_REQUIRED_VARS = (
    "HYQS_REGISTRY_DOMAIN",
    "HYQS_REGISTRY_PULL_USER",
    "HYQS_REGISTRY_PULL_PASS",
    "HYQS_COSIGN_PUBLIC_KEY_PATH",
)


def registry_configured() -> bool:
    """True when the enable flag is truthy and every required push/sign var is set."""
    enabled = os.environ.get("HYQS_REGISTRY_PUSH_ENABLED", "").strip().lower()
    if enabled not in _TRUTHY:
        return False
    return all(os.environ.get(var) for var in _REQUIRED_VARS)


def pull_configured() -> bool:
    """True when the enable flag is truthy and every required pull/verify var is set."""
    enabled = os.environ.get("HYQS_REGISTRY_PULL_ENABLED", "").strip().lower()
    if enabled not in _TRUTHY:
        return False
    return all(os.environ.get(var) for var in _PULL_REQUIRED_VARS)


def build_push_ref(project_slug: str, component: str = "app") -> str:
    """The namespaced registry ref for a project's component image."""
    domain = os.environ.get("HYQS_REGISTRY_DOMAIN", "")
    return f"{domain}/{project_slug}/{component}"


async def _run(
    cmd: list[str],
    *,
    input_data: bytes | None = None,
    env: dict[str, str] | None = None,
    log_sink: Callable[[str], Awaitable[None]] | None = None,
    timeout: float = 120,
) -> tuple[bool, str]:
    """Run cmd to completion, returning (success, combined output). Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if input_data is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        out, _ = await asyncio.wait_for(proc.communicate(input=input_data), timeout=timeout)
        text = out.decode(errors="replace")
        if log_sink is not None:
            for line in text.splitlines():
                await log_sink(line)
        return proc.returncode == 0, text
    except Exception as exc:  # noqa: BLE001
        log.warning("command failed: %s: %s", " ".join(cmd), exc)
        return False, str(exc)


_PUSH_DIGEST_RE = re.compile(r"digest:\s*(sha256:[0-9a-fA-F]+)")


def _parse_push_digest(output: str) -> str | None:
    """The last 'digest: sha256:...' value in `docker push` output, or None."""
    matches = _PUSH_DIGEST_RE.findall(output)
    return matches[-1] if matches else None


async def push_image(
    local_image: str,
    push_ref: str,
    *,
    log_sink: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[bool, str]:
    """docker-tag local_image as '<push_ref>:latest', login, and push it.

    Returns (success, combined output of all subprocess calls).
    """
    tagged = f"{push_ref}:latest"
    combined = ""

    ok, out = await _run(["docker", "tag", local_image, tagged], log_sink=log_sink)
    combined += out
    if not ok:
        return False, combined

    domain = os.environ.get("HYQS_REGISTRY_DOMAIN", "")
    user = os.environ.get("HYQS_REGISTRY_PUSH_USER", "")
    password = os.environ.get("HYQS_REGISTRY_PUSH_PASS", "")
    ok, out = await _run(
        ["docker", "login", domain, "-u", user, "--password-stdin"],
        input_data=password.encode(),
        log_sink=log_sink,
    )
    combined += out
    if not ok:
        return False, combined

    ok, out = await _run(["docker", "push", tagged], log_sink=log_sink)
    combined += out
    return ok, combined


async def pull_image_by_digest(
    image_ref: str,
    image_digest: str,
    *,
    log_sink: Callable[[str], Awaitable[None]] | None = None,
) -> bool:
    """Login with the pull-only credential, then `docker pull <image_ref>@<image_digest>`."""
    domain = os.environ.get("HYQS_REGISTRY_DOMAIN", "")
    user = os.environ.get("HYQS_REGISTRY_PULL_USER", "")
    password = os.environ.get("HYQS_REGISTRY_PULL_PASS", "")
    ok, _out = await _run(
        ["docker", "login", domain, "-u", user, "--password-stdin"],
        input_data=password.encode(),
        log_sink=log_sink,
    )
    if not ok:
        return False

    ok, _out = await _run(
        ["docker", "pull", f"{image_ref}@{image_digest}"],
        log_sink=log_sink,
    )
    return ok


async def verify_signature(
    ref_with_digest: str,
    *,
    log_sink: Callable[[str], Awaitable[None]] | None = None,
) -> bool:
    """cosign-verify ref_with_digest against HYQS_COSIGN_PUBLIC_KEY_PATH.

    Fail-closed: returns False without spawning any subprocess if the public
    key path is unset.
    """
    key_path = os.environ.get("HYQS_COSIGN_PUBLIC_KEY_PATH", "")
    if not key_path:
        return False

    ok, _out = await _run(
        ["cosign", "verify", "--key", key_path, ref_with_digest],
        log_sink=log_sink,
    )
    return ok


async def capture_digest(ref: str) -> str | None:
    """Return the 'sha256:...' digest for ref via docker inspect, or None on failure."""
    ok, out = await _run(
        ["docker", "inspect", "--format", "{{index .RepoDigests 0}}", ref],
    )
    if not ok:
        return None
    value = out.strip()
    _repo, sep, digest = value.partition("@")
    return digest if sep and digest else None


async def sign_digest(
    ref_with_digest: str,
    *,
    log_sink: Callable[[str], Awaitable[None]] | None = None,
) -> bool:
    """cosign-sign ref_with_digest using HYQS_COSIGN_KEY_PATH."""
    key_path = os.environ.get("HYQS_COSIGN_KEY_PATH", "")
    env = os.environ.copy()
    key_password = os.environ.get("HYQS_COSIGN_KEY_PASSWORD")
    if key_password:
        env["COSIGN_PASSWORD"] = key_password

    ok, _out = await _run(
        ["cosign", "sign", "--key", key_path, "--yes", ref_with_digest],
        env=env,
        log_sink=log_sink,
    )
    return ok


async def push_and_sign(
    local_image: str,
    project_slug: str,
    component: str = "app",
    *,
    log_sink: Callable[[str], Awaitable[None]] | None = None,
) -> dict | None:
    """Push local_image to the registry and cosign-sign its digest.

    Returns None immediately (no subprocess calls) when registry_configured() is
    False. Best-effort: never raises — any step failure short-circuits with
    signed=False and image_digest=None.
    """
    if not registry_configured():
        return None

    push_ref = build_push_ref(project_slug, component)

    ok, push_output = await push_image(local_image, push_ref, log_sink=log_sink)
    if not ok:
        return {
            "image_ref": push_ref,
            "image_digest": None,
            "signed": False,
            "output": "docker push failed",
        }

    digest = _parse_push_digest(push_output) or await capture_digest(f"{push_ref}:latest")
    if not digest:
        return {
            "image_ref": push_ref,
            "image_digest": None,
            "signed": False,
            "output": "failed to capture image digest",
        }

    ref_with_digest = f"{push_ref}@{digest}"
    if not await sign_digest(ref_with_digest, log_sink=log_sink):
        return {
            "image_ref": push_ref,
            "image_digest": None,
            "signed": False,
            "output": "cosign sign failed",
        }

    return {
        "image_ref": push_ref,
        "image_digest": digest,
        "signed": True,
        "output": "pushed and signed",
    }
