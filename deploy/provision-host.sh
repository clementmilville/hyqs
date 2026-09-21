#!/usr/bin/env bash
set -euo pipefail

# Hard prerequisites for the EXPLORER chat agent sandbox.
#
# The EXPLORER role in providers.py calls _sandbox_deps_available() before
# spawning any agent and raises RuntimeError (fail-closed) if bwrap or socat
# is absent.  This script installs those deps idempotently so the sandbox
# actually engages in production instead of failing closed.
#
# Run as root on the deploy host:
#   sudo bash deploy/provision-host.sh

apt-get install -y bubblewrap socat

# Verify both binaries landed on PATH.
command -v bwrap
command -v socat

echo "Hard prereqs OK: bwrap=$(command -v bwrap)  socat=$(command -v socat)"
