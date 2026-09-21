#!/usr/bin/env bash
#
# Client host: onboard this host as a standalone deployer (Epic 81,
# CONNECTED tier — this host reaches our coordination DB + registry
# directly). Minimal and auditable by design: no telemetry, and no network
# calls beyond the ones described below. Reuses the already-landed
# primitives verbatim — `hyqs-pipeline enroll` (#1781), the deployer image
# (#1782), the file-based secret store (#1748) — this script never
# reimplements them.
#
# Required flags:
#   --name <host>                  this host's enrolled identity
#   --image-ref <ref>               e.g. registry.example.com/hyqs-deployer/app
#   --image-digest <sha256:...>     printed by deploy/publish-deployer.sh
# Optional flags:
#   --data-dir <path>       default: ~/.hyqs/deployer-data
#   --secrets-dir <path>    default: ~/.hyqs/deployer-secrets
#
# Required env:
#   HYQS_DB_URL              the shared control-plane Postgres DSN
#   HYQS_REGISTRY_PULL_USER  the pull-only registry account (v1: shared
#                            across all client hosts — per-host-scoped pull
#                            creds are a Harbor-era FUTURE follow-up)
#   HYQS_REGISTRY_PULL_PASS  that account's password
#
# Usage:
#   HYQS_DB_URL=postgresql://hyqs:...@db.example.com:5432/hyqs \
#   HYQS_REGISTRY_PULL_USER=pull HYQS_REGISTRY_PULL_PASS=... \
#   bash deploy/install-deployer.sh --name my-edge-host \
#     --image-ref registry.example.com/hyqs-deployer/app \
#     --image-digest sha256:...
#
# Idempotent: safe to re-run to upgrade the pinned image + systemd unit. It
# never regenerates the enrollment keypair or touches existing files under
# --secrets-dir.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --- EDIT ME: pin the cosign public key printed by publish-deployer.sh -----
# This is the one hard trust anchor in this whole flow: paste the exact
# contents of the .pub file printed by deploy/publish-deployer.sh below. It
# is pinned inline, never fetched over the network, and verified against
# the image BY DIGEST before anything else runs.
HYQS_COSIGN_PUBLIC_KEY_PEM="$(cat <<'COSIGN_PUBKEY_EOF'
-----BEGIN PUBLIC KEY-----
REPLACE_WITH_THE_COSIGN_PUBLIC_KEY_PRINTED_BY_publish-deployer.sh
-----END PUBLIC KEY-----
COSIGN_PUBKEY_EOF
)"
# ---------------------------------------------------------------------------

usage() {
  cat <<USAGE >&2
Usage: $(basename "$0") --name <host> --image-ref <ref> --image-digest <sha256:...> \\
         [--data-dir <path>] [--secrets-dir <path>]

Required env:
  HYQS_DB_URL              the shared control-plane Postgres DSN
  HYQS_REGISTRY_PULL_USER  the pull-only registry account
  HYQS_REGISTRY_PULL_PASS  that account's password

Optional flags:
  --data-dir     default: ~/.hyqs/deployer-data
  --secrets-dir  default: ~/.hyqs/deployer-secrets
USAGE
}

NAME=""
IMAGE_REF=""
IMAGE_DIGEST=""
DATA_DIR="${HOME}/.hyqs/deployer-data"
SECRETS_DIR="${HOME}/.hyqs/deployer-secrets"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)
      NAME="${2:-}"
      shift 2
      ;;
    --image-ref)
      IMAGE_REF="${2:-}"
      shift 2
      ;;
    --image-digest)
      IMAGE_DIGEST="${2:-}"
      shift 2
      ;;
    --data-dir)
      DATA_DIR="${2:-}"
      shift 2
      ;;
    --secrets-dir)
      SECRETS_DIR="${2:-}"
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "${NAME}" || -z "${IMAGE_REF}" || -z "${IMAGE_DIGEST}" || -z "${HYQS_DB_URL:-}" ]]; then
  echo "ERROR: --name, --image-ref, --image-digest, and HYQS_DB_URL are all required" >&2
  usage
  exit 1
fi

if [[ "${IMAGE_DIGEST}" != sha256:* ]]; then
  echo "ERROR: --image-digest must look like 'sha256:<hex>', got: ${IMAGE_DIGEST}" >&2
  exit 1
fi

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: required command '$1' not found on PATH" >&2
    exit 1
  fi
}

parse_host_port_from_url() {
  # Extracts host:port from a postgresql DSN (defaults to port 5432 when the
  # DSN has none). Shape: scheme://<credentials>@<host>[:<port>]/<db>?...
  local url="$1" rest
  rest="${url#*://}"
  rest="${rest#*@}"
  rest="${rest%%/*}"
  rest="${rest%%\?*}"
  if [[ "${rest}" != *:* ]]; then
    rest="${rest}:5432"
  fi
  printf '%s' "${rest}"
}

parse_domain_from_image_ref() {
  local ref="$1"
  printf '%s' "${ref%%/*}"
}

echo "[preflight] checking required tools..."
require_cmd docker
require_cmd cosign
require_cmd systemctl
require_cmd curl

echo "[preflight] checking docker daemon..."
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: docker daemon is not reachable — is it running, and are you in the docker group?" >&2
  exit 1
fi

echo "[preflight] checking systemd --user session..."
if ! systemctl --user status >/dev/null 2>&1; then
  echo "ERROR: 'systemctl --user status' failed — no systemd --user session available" >&2
  exit 1
fi

echo "[preflight] checking control-plane DB reachability..."
DB_HOST_PORT="$(parse_host_port_from_url "${HYQS_DB_URL}")"
DB_HOST="${DB_HOST_PORT%%:*}"
DB_PORT="${DB_HOST_PORT##*:}"
if ! timeout 5 bash -c "exec 3<>\"/dev/tcp/${DB_HOST}/${DB_PORT}\"" 2>/dev/null; then
  echo "ERROR: cannot reach ${DB_HOST}:${DB_PORT} (from HYQS_DB_URL) — is this host network-connected" >&2
  echo "  to the control-plane DB? (air-gapped installs are a documented FUTURE mode, not yet built)" >&2
  exit 1
fi

echo "[preflight] checking registry reachability..."
REGISTRY_DOMAIN="$(parse_domain_from_image_ref "${IMAGE_REF}")"
HTTP_STATUS="$(curl -sS -o /dev/null -w '%{http_code}' "https://${REGISTRY_DOMAIN}/v2/" || true)"
case "${HTTP_STATUS}" in
  2?? | 401 | 403) ;;
  *)
    echo "ERROR: registry ${REGISTRY_DOMAIN} returned HTTP ${HTTP_STATUS:-<none>} (expected 2xx/401/403) —" >&2
    echo "  is this host network-connected to the registry? (air-gapped installs are a documented FUTURE mode, not yet built)" >&2
    exit 1
    ;;
esac
echo "    OK"

echo "[bootstrap] pulling ${IMAGE_REF}@${IMAGE_DIGEST} by digest..."
docker pull "${IMAGE_REF}@${IMAGE_DIGEST}"

echo "[bootstrap] verifying cosign signature against the pinned public key..."
if ! cosign verify --key <(printf '%s' "${HYQS_COSIGN_PUBLIC_KEY_PEM}") "${IMAGE_REF}@${IMAGE_DIGEST}" >/dev/null; then
  echo "ERROR: cosign verify failed for ${IMAGE_REF}@${IMAGE_DIGEST} — aborting, nothing else will run" >&2
  exit 1
fi
echo "    verified."

: "${HYQS_REGISTRY_PULL_USER:?set HYQS_REGISTRY_PULL_USER (the pull-only registry account)}"
: "${HYQS_REGISTRY_PULL_PASS:?set HYQS_REGISTRY_PULL_PASS (the pull account password)}"
echo "[bootstrap] logging in to the registry with the pull-only credential..."
echo "${HYQS_REGISTRY_PULL_PASS}" | docker login "${REGISTRY_DOMAIN}" -u "${HYQS_REGISTRY_PULL_USER}" --password-stdin

HOST_CONFIG_PATH="${DATA_DIR}/deployer/host_config.json"
if [[ -f "${HOST_CONFIG_PATH}" ]]; then
  echo "[enroll] ${HOST_CONFIG_PATH} already exists — already enrolled, skipping"
else
  echo "[enroll] enrolling this host as '${NAME}'..."
  mkdir -p "${DATA_DIR}"
  docker run --rm \
    -v "${DATA_DIR}:/data" \
    -e HYQS_DB_URL="${HYQS_DB_URL}" \
    -e HYQS_DATA_DIR=/data \
    --entrypoint /app/.venv/bin/hyqs-pipeline \
    "${IMAGE_REF}@${IMAGE_DIGEST}" enroll --name "${NAME}"
fi

echo "[secrets] creating local secret store at ${SECRETS_DIR}..."
mkdir -p "${SECRETS_DIR}"
chmod 700 "${SECRETS_DIR}"
cat <<SECRET_NOTE
    Local secret store ready at ${SECRETS_DIR} (chmod 700). This script
    NEVER sets secret values — populate it yourself. Required secrets are
    enforced FAIL-CLOSED at apply time
    (hyqs.pipeline.secrets_contract.find_missing_secrets): a deploy refuses
    to proceed if a required secret is missing.
SECRET_NOTE

echo "[install] rendering hyqs-deployer.service..."
SERVICE_SRC="${REPO_ROOT}/deploy/hyqs-deployer.service"
SERVICE_DST_DIR="${HOME}/.config/systemd/user"
SERVICE_DST="${SERVICE_DST_DIR}/hyqs-deployer.service"
mkdir -p "${SERVICE_DST_DIR}"

# Escape backslash/&/# (sed's replacement-side special chars, '#' being our
# chosen delimiter) so an HYQS_DB_URL with a query string, or any path, can
# never be misinterpreted as sed syntax.
escape_for_sed_repl() {
  printf '%s' "$1" | sed -e 's/[\&#]/\\&/g'
}

IMAGE_REF_DIGEST_ESC="$(escape_for_sed_repl "${IMAGE_REF}@${IMAGE_DIGEST}")"
DB_URL_ESC="$(escape_for_sed_repl "${HYQS_DB_URL}")"
NAME_ESC="$(escape_for_sed_repl "${NAME}")"
DATA_DIR_ESC="$(escape_for_sed_repl "${DATA_DIR}")"

sed \
  -e "s#^Environment=HYQS_DEPLOYER_IMAGE=.*#Environment=HYQS_DEPLOYER_IMAGE=${IMAGE_REF_DIGEST_ESC}#" \
  -e "s#^Environment=HYQS_DB_URL=.*#Environment=HYQS_DB_URL=${DB_URL_ESC}#" \
  -e "s#^Environment=HYQS_HOST_NAME=.*#Environment=HYQS_HOST_NAME=${NAME_ESC}#" \
  -e "s#^ExecStartPre=/usr/bin/mkdir -p .*#ExecStartPre=/usr/bin/mkdir -p ${DATA_DIR_ESC}#" \
  -e "s#%h/\.hyqs/deployer-data#${DATA_DIR_ESC}#g" \
  "${SERVICE_SRC}" >"${SERVICE_DST}"

echo "[install] enabling lingering for ${USER} (survive logout)..."
sudo loginctl enable-linger "${USER}"

echo "[install] enabling + starting hyqs-deployer..."
systemctl --user daemon-reload
systemctl --user enable --now hyqs-deployer

echo "[install] recent logs:"
journalctl --user -u hyqs-deployer -n 20 --no-pager || true

echo "[verify] checking deployer status..."
if ! docker run --rm \
  -v "${DATA_DIR}:/data" \
  -e HYQS_DATA_DIR=/data \
  --entrypoint /app/.venv/bin/hyqs-pipeline \
  "${IMAGE_REF}@${IMAGE_DIGEST}" status; then
  echo "ERROR: deployer status check failed" >&2
  exit 1
fi

if ! systemctl --user is-active --quiet hyqs-deployer; then
  echo "ERROR: hyqs-deployer systemd unit is not active" >&2
  exit 1
fi

echo
echo "Install complete: host '${NAME}' is enrolled, hyqs-deployer is running,"
echo "and its secret store is ready at ${SECRETS_DIR}."

# TODO(FUTURE, not built here): air-gapped/egress-only enrollment-bundle
#   path — package the deployer image + digest + cosign signature + an
#   enroll payload into an offline bundle an operator can transfer without
#   direct network reachability to the control-plane DB/registry (see the
#   reachability checks above, which fail fast today instead of supporting
#   this mode).
# TODO(FUTURE, not built here): per-host-scoped pull credentials — v1 uses
#   one shared 'pull' registry account for every client host; a Harbor-era
#   follow-up should issue a distinct, revocable pull credential per host.
