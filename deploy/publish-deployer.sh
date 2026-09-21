#!/usr/bin/env bash
#
# Build host: build the deployer image from deploy/deployer.Dockerfile, push
# it to the project's self-hosted registry, and cosign-sign it. Reuses
# hyqs.pipeline.registry_push (push_and_sign/build_push_ref) verbatim for the
# docker tag/login/push and cosign sign subprocess calls — this script never
# re-implements that logic itself.
#
# Prints IMAGE_REF / IMAGE_DIGEST / the cosign public key path so an operator
# can paste them into deploy/install-deployer.sh's flags and EDIT-ME block.
#
# Required env (see hyqs.pipeline.registry_push.registry_configured):
#   HYQS_REGISTRY_PUSH_ENABLED=1
#   HYQS_REGISTRY_DOMAIN
#   HYQS_REGISTRY_PUSH_USER
#   HYQS_REGISTRY_PUSH_PASS
#   HYQS_COSIGN_KEY_PATH
# Optional:
#   HYQS_COSIGN_PUB_PATH   (default: HYQS_COSIGN_KEY_PATH with .key -> .pub)
#
# Idempotent — safe to re-run whenever the deployer image changes.
#
# Usage:
#   HYQS_REGISTRY_PUSH_ENABLED=1 HYQS_REGISTRY_DOMAIN=registry.example.com \
#   HYQS_REGISTRY_PUSH_USER=push HYQS_REGISTRY_PUSH_PASS=... \
#   HYQS_COSIGN_KEY_PATH=~/.hyqs/keys/cosign.key \
#   bash deploy/publish-deployer.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_IMAGE="hyqs-deployer:latest"
PROJECT_SLUG="hyqs-deployer"

# Runs a python script (read from stdin) inside the repo's own environment,
# forwarding any extra args as sys.argv. Prefers `uv run python`, matching
# every other invocation in this repo; falls back to a bare `python3` if uv
# isn't on PATH (e.g. a minimal CI box).
run_py() {
  if command -v uv >/dev/null 2>&1; then
    (cd "${REPO_ROOT}" && uv run python - "$@")
  else
    (cd "${REPO_ROOT}" && python3 - "$@")
  fi
}

echo "[1/4] Checking registry push configuration..."
if ! run_py <<'PYEOF'
import os
import sys

from hyqs.pipeline import registry_push as rp

if not rp.registry_configured():
    checks = {
        "HYQS_REGISTRY_PUSH_ENABLED": os.environ.get("HYQS_REGISTRY_PUSH_ENABLED", "")
        .strip()
        .lower()
        in {"1", "true", "yes", "on"},
        "HYQS_REGISTRY_DOMAIN": bool(os.environ.get("HYQS_REGISTRY_DOMAIN")),
        "HYQS_REGISTRY_PUSH_USER": bool(os.environ.get("HYQS_REGISTRY_PUSH_USER")),
        "HYQS_REGISTRY_PUSH_PASS": bool(os.environ.get("HYQS_REGISTRY_PUSH_PASS")),
        "HYQS_COSIGN_KEY_PATH": bool(os.environ.get("HYQS_COSIGN_KEY_PATH")),
    }
    missing = [name for name, ok in checks.items() if not ok]
    print(
        "registry push is not configured; missing/unset: " + ", ".join(missing),
        file=sys.stderr,
    )
    sys.exit(1)
PYEOF
then
  exit 1
fi
echo "    OK"

echo "[2/4] Building ${LOCAL_IMAGE} from deploy/deployer.Dockerfile..."
docker build -f "${REPO_ROOT}/deploy/deployer.Dockerfile" -t "${LOCAL_IMAGE}" "${REPO_ROOT}"

echo "[3/4] Pushing and cosign-signing..."
PUSH_OUTPUT="$(run_py "${LOCAL_IMAGE}" "${PROJECT_SLUG}" <<'PYEOF'
import asyncio
import sys

from hyqs.pipeline import registry_push as rp


async def main() -> None:
    local_image, project_slug = sys.argv[1], sys.argv[2]
    result = await rp.push_and_sign(local_image, project_slug, "app")
    if result is None:
        print("registry push is not configured", file=sys.stderr)
        sys.exit(1)
    print(f"IMAGE_REF={result['image_ref']}")
    print(f"IMAGE_DIGEST={result['image_digest'] or ''}")
    print(f"SIGNED={'true' if result['signed'] else 'false'}")


asyncio.run(main())
PYEOF
)"
echo "${PUSH_OUTPUT}"

IMAGE_REF="$(printf '%s\n' "${PUSH_OUTPUT}" | sed -n 's/^IMAGE_REF=//p')"
IMAGE_DIGEST="$(printf '%s\n' "${PUSH_OUTPUT}" | sed -n 's/^IMAGE_DIGEST=//p')"
SIGNED="$(printf '%s\n' "${PUSH_OUTPUT}" | sed -n 's/^SIGNED=//p')"

if [[ "${SIGNED}" != "true" || -z "${IMAGE_DIGEST}" ]]; then
  echo "ERROR: push/sign failed (SIGNED=${SIGNED:-<empty>}, IMAGE_DIGEST=${IMAGE_DIGEST:-<empty>})" >&2
  exit 1
fi
echo "[4/4] Done."

HYQS_COSIGN_PUB_PATH="${HYQS_COSIGN_PUB_PATH:-${HYQS_COSIGN_KEY_PATH%.key}.pub}"

cat <<SUMMARY

Publish succeeded. Hand these to the client operator running
deploy/install-deployer.sh:

  --image-ref     ${IMAGE_REF}
  --image-digest  ${IMAGE_DIGEST}

Cosign public key (paste its contents into install-deployer.sh's EDIT-ME
block):

  ${HYQS_COSIGN_PUB_PATH}
SUMMARY
