"""Bash-syntax + arg-validation tests for the client-host onboarding scripts.

Epic 81's "4-install" job wraps the already-landed deployer primitives
(enroll CLI #1781, deployer image #1782, registry push/sign #1761/#1806)
into two scripts: deploy/publish-deployer.sh (build host) and
deploy/install-deployer.sh (client host). Modeled on
tests/test_registry_smoke_check.py: no docker, network, or Postgres
required — every assertion here exercises only the scripts' fast-fail
validation paths, which must short-circuit before any external command runs.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from hyqs.pipeline.blueprints import KNOWN_STACKS, get_blueprint

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PUBLISH_SCRIPT = _REPO_ROOT / "deploy" / "publish-deployer.sh"
_INSTALL_SCRIPT = _REPO_ROOT / "deploy" / "install-deployer.sh"

_TIMEOUT = 30


def test_publish_deployer_is_valid_bash():
    result = subprocess.run(["bash", "-n", str(_PUBLISH_SCRIPT)], capture_output=True)
    assert result.returncode == 0, result.stderr.decode()


def test_install_deployer_is_valid_bash():
    result = subprocess.run(["bash", "-n", str(_INSTALL_SCRIPT)], capture_output=True)
    assert result.returncode == 0, result.stderr.decode()


def test_install_deployer_with_no_args_fails_fast_without_external_commands():
    env = {k: v for k, v in os.environ.items() if not k.startswith("HYQS_")}
    result = subprocess.run(
        ["bash", str(_INSTALL_SCRIPT)],
        capture_output=True,
        timeout=_TIMEOUT,
        env=env,
    )
    assert result.returncode != 0
    stderr = result.stderr.decode()
    assert "required" in stderr.lower()
    assert "Usage" in stderr


def test_install_deployer_missing_one_required_flag_fails_fast():
    env = {k: v for k, v in os.environ.items() if not k.startswith("HYQS_")}
    env["HYQS_DB_URL"] = "postgresql://hyqs:hyqs@localhost:5432/hyqs"  # pragma: allowlist secret
    result = subprocess.run(
        [
            "bash",
            str(_INSTALL_SCRIPT),
            "--name",
            "my-edge-host",
            "--image-ref",
            "registry.example.com/hyqs-deployer/app",
            # --image-digest deliberately omitted
        ],
        capture_output=True,
        timeout=_TIMEOUT,
        env=env,
    )
    assert result.returncode != 0
    assert "required" in result.stderr.decode().lower()


def test_install_deployer_rejects_a_non_digest_image_digest():
    env = {k: v for k, v in os.environ.items() if not k.startswith("HYQS_")}
    env["HYQS_DB_URL"] = "postgresql://hyqs:hyqs@localhost:5432/hyqs"  # pragma: allowlist secret
    result = subprocess.run(
        [
            "bash",
            str(_INSTALL_SCRIPT),
            "--name",
            "my-edge-host",
            "--image-ref",
            "registry.example.com/hyqs-deployer/app",
            "--image-digest",
            "latest",
        ],
        capture_output=True,
        timeout=_TIMEOUT,
        env=env,
    )
    assert result.returncode != 0
    assert "sha256" in result.stderr.decode().lower()


def test_install_deployer_cosign_pubkey_is_a_literal_placeholder_not_fetched():
    text = _INSTALL_SCRIPT.read_text()
    assert "EDIT ME" in text
    assert "-----BEGIN PUBLIC KEY-----" in text
    # Never fetched from a URL — this is the one hard trust anchor, pinned
    # inline via a heredoc, not curl'd/downloaded.
    assert "curl" not in text.split("EDIT ME")[1].split("---------------")[0]


def test_publish_deployer_with_cleared_registry_env_fails_fast():
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("HYQS_REGISTRY_PUSH") and k != "HYQS_COSIGN_KEY_PATH"
    }
    result = subprocess.run(
        ["bash", str(_PUBLISH_SCRIPT)],
        capture_output=True,
        timeout=_TIMEOUT,
        env=env,
    )
    assert result.returncode != 0
    combined = (result.stdout + result.stderr).decode().lower()
    assert "not configured" in combined


def test_known_stacks_includes_fastapi():
    assert "fastapi" in KNOWN_STACKS


def test_known_stacks_all_resolve_via_get_blueprint():
    for stack in KNOWN_STACKS:
        assert get_blueprint(stack) is not None
