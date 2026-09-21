"""Sanity tests for the registry smoke-check's digest-resolution helpers.

Epic 81: `docker inspect --format='{{index .RepoDigests 0}}'` returns the
SOURCE image's digest — for a multi-arch source image that is the manifest-
list digest, not the single-platform manifest digest the registry actually
stored, so a pull-by-digest against it 404s. deploy/registry-digest.sh
resolves the digest the registry itself reports instead; these tests only
exercise that resolution logic via subprocess/bash, with no docker/network/AI
calls.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SMOKE_CHECK = _REPO_ROOT / "deploy" / "registry-smoke-check.sh"
_DIGEST_LIB = _REPO_ROOT / "deploy" / "registry-digest.sh"


def test_registry_smoke_check_is_valid_bash():
    result = subprocess.run(["bash", "-n", str(_SMOKE_CHECK)], capture_output=True)
    assert result.returncode == 0, result.stderr.decode()


def test_registry_digest_lib_is_valid_bash():
    result = subprocess.run(["bash", "-n", str(_DIGEST_LIB)], capture_output=True)
    assert result.returncode == 0, result.stderr.decode()


def test_parse_push_digest_extracts_digest_from_push_output():
    script = f"""
set -euo pipefail
source "{_DIGEST_LIB}"
printf 'latest: digest: sha256:abc123def456 size: 1022\\n' | parse_push_digest
"""
    result = subprocess.run(["bash", "-c", script], capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.decode() == "sha256:abc123def456"


def test_parse_push_digest_returns_empty_when_no_digest_line():
    script = f"""
set -euo pipefail
source "{_DIGEST_LIB}"
printf 'no digest here\\n' | parse_push_digest
"""
    result = subprocess.run(["bash", "-c", script], capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.decode() == ""
