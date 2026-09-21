#!/usr/bin/env bash
#
# Smoke-check for the self-hosted Zot registry (Epic 81): confirms the
# registry is reachable and that push/pull credentials are correctly scoped —
# the push account can write, the pull account can read by digest but was
# never used to push.
#
# Required env vars:
#   REGISTRY_PUSH_USER / REGISTRY_PUSH_PASS   - the build-host (push) account
#   REGISTRY_PULL_USER / REGISTRY_PULL_PASS   - the deployer (pull-only) account
# Optional:
#   REGISTRY_DOMAIN  (required, e.g. registry.example.com)
#   TEST_IMAGE       (default: alpine:latest — a small public image to relay)
#
# Usage:
#   REGISTRY_PUSH_USER=push REGISTRY_PUSH_PASS=... \
#   REGISTRY_PULL_USER=pull REGISTRY_PULL_PASS=... \
#   bash deploy/registry-smoke-check.sh

set -euo pipefail

REGISTRY_DOMAIN="${REGISTRY_DOMAIN:?set REGISTRY_DOMAIN, e.g. registry.example.com}"
TEST_IMAGE="${TEST_IMAGE:-alpine:latest}"
REPO_PATH="smoke-test"
LOCAL_TAG="${REGISTRY_DOMAIN}/${REPO_PATH}:smoke"

: "${REGISTRY_PUSH_USER:?set REGISTRY_PUSH_USER (the push account)}"
: "${REGISTRY_PUSH_PASS:?set REGISTRY_PUSH_PASS (the push account's password)}"
: "${REGISTRY_PULL_USER:?set REGISTRY_PULL_USER (the pull-only account)}"
: "${REGISTRY_PULL_PASS:?set REGISTRY_PULL_PASS (the pull-only account's password)}"

# shellcheck source=deploy/registry-digest.sh
source "$(dirname "${BASH_SOURCE[0]}")/registry-digest.sh"

PULLED_BY_DIGEST_TAG=""

cleanup() {
  echo "Cleaning up local tags..."
  docker rmi -f "${LOCAL_TAG}" >/dev/null 2>&1 || true
  docker rmi -f "${TEST_IMAGE}" >/dev/null 2>&1 || true
  if [[ -n "${PULLED_BY_DIGEST_TAG}" ]]; then
    docker rmi -f "${PULLED_BY_DIGEST_TAG}" >/dev/null 2>&1 || true
  fi
  docker logout "${REGISTRY_DOMAIN}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "[1/5] Checking reachability + auth of /v2/ with pull credentials..."
HTTP_STATUS="$(curl -s -o /dev/null -w '%{http_code}' \
  -u "${REGISTRY_PULL_USER}:${REGISTRY_PULL_PASS}" \
  "https://${REGISTRY_DOMAIN}/v2/")"
if [[ "${HTTP_STATUS}" != "200" ]]; then
  echo "ERROR: GET https://${REGISTRY_DOMAIN}/v2/ returned ${HTTP_STATUS} (expected 200)" >&2
  exit 1
fi
echo "    OK (200)"

echo "[2/5] Logging in as push account and pushing a test image..."
echo "${REGISTRY_PUSH_PASS}" | docker login "${REGISTRY_DOMAIN}" -u "${REGISTRY_PUSH_USER}" --password-stdin
docker pull "${TEST_IMAGE}"
docker tag "${TEST_IMAGE}" "${LOCAL_TAG}"
PUSH_OUTPUT="$(docker push "${LOCAL_TAG}")"
echo "${PUSH_OUTPUT}"

echo "[3/5] Resolving pushed digest..."
PUSHED_DIGEST="$(printf '%s\n' "${PUSH_OUTPUT}" | parse_push_digest)"
if [[ -z "${PUSHED_DIGEST}" ]]; then
  PUSHED_DIGEST="$(resolve_manifest_digest_via_http "${REGISTRY_DOMAIN}" "${REPO_PATH}" smoke "${REGISTRY_PUSH_USER}" "${REGISTRY_PUSH_PASS}")"
fi
if [[ -z "${PUSHED_DIGEST}" ]]; then
  echo "ERROR: could not resolve a repo digest for ${LOCAL_TAG} after push" >&2
  exit 1
fi
echo "    pushed digest: ${PUSHED_DIGEST}"
docker logout "${REGISTRY_DOMAIN}" >/dev/null 2>&1 || true

echo "[4/5] Logging in as pull-only account and pulling back by digest..."
echo "${REGISTRY_PULL_PASS}" | docker login "${REGISTRY_DOMAIN}" -u "${REGISTRY_PULL_USER}" --password-stdin
PULLED_BY_DIGEST_TAG="${REGISTRY_DOMAIN}/${REPO_PATH}@${PUSHED_DIGEST}"
docker pull "${PULLED_BY_DIGEST_TAG}"

echo "[5/5] Verifying pulled digest matches pushed digest..."
PULLED_DIGEST="$(resolve_manifest_digest_via_http "${REGISTRY_DOMAIN}" "${REPO_PATH}" smoke "${REGISTRY_PULL_USER}" "${REGISTRY_PULL_PASS}")"
if [[ "${PULLED_DIGEST}" != "${PUSHED_DIGEST}" ]]; then
  echo "ERROR: pulled digest (${PULLED_DIGEST}) does not match pushed digest (${PUSHED_DIGEST})" >&2
  exit 1
fi

echo
echo "Smoke check passed: ${REGISTRY_DOMAIN} is reachable, push account can write,"
echo "and the pull-only account can read ${REPO_PATH}@${PUSHED_DIGEST} by digest."
