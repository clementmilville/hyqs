#!/usr/bin/env bash
#
# One-time host setup so the Hyqs pipeline (running as an unprivileged user) can
# register/remove per-project nginx sites (one subdomain per project) without root.
#
# It does three things, idempotently:
#   1. Creates an app-owned include dir  /etc/nginx/hyqs.d/  (pipeline writes here)
#   2. Wires `include /etc/nginx/hyqs.d/*.conf;` into nginx.conf's http{} block
#   3. Installs a narrow sudoers rule letting the app user validate + reload nginx
#
# Run as root:   sudo bash deploy/setup-nginx-hyqs.sh
# Override user: sudo HYQS_USER=someuser bash deploy/setup-nginx-hyqs.sh

set -euo pipefail

HYQS_USER="${HYQS_USER:-hyqs}"
INCLUDE_DIR="/etc/nginx/hyqs.d"
NGINX_CONF="/etc/nginx/nginx.conf"
SUDOERS_FILE="/etc/sudoers.d/hyqs-nginx"

# --- preconditions --------------------------------------------------------
if [[ "${EUID}" -ne 0 ]]; then
  echo "ERROR: run as root (sudo bash $0)" >&2
  exit 1
fi
if ! id -u "${HYQS_USER}" >/dev/null 2>&1; then
  echo "ERROR: user '${HYQS_USER}' does not exist (set HYQS_USER=...)" >&2
  exit 1
fi

# Resolve absolute binary paths — sudoers requires them and they vary by distro.
NGINX_BIN="$(command -v nginx || true)"
SYSTEMCTL_BIN="$(command -v systemctl || true)"
if [[ -z "${NGINX_BIN}" || -z "${SYSTEMCTL_BIN}" ]]; then
  echo "ERROR: could not locate nginx and/or systemctl on PATH" >&2
  exit 1
fi
echo "Using: nginx=${NGINX_BIN} systemctl=${SYSTEMCTL_BIN} user=${HYQS_USER}"

# --- 1. app-owned include dir --------------------------------------------
if [[ -d "${INCLUDE_DIR}" ]]; then
  echo "[1/3] ${INCLUDE_DIR} already exists"
else
  install -d -o "${HYQS_USER}" -g "${HYQS_USER}" -m 0755 "${INCLUDE_DIR}"
  echo "[1/3] created ${INCLUDE_DIR} (owned by ${HYQS_USER})"
fi
# Ensure ownership even if it pre-existed root-owned.
chown "${HYQS_USER}:${HYQS_USER}" "${INCLUDE_DIR}"

# --- 2. wire the include into http{} -------------------------------------
if grep -qE "include[[:space:]]+${INCLUDE_DIR}/\*\.conf" "${NGINX_CONF}"; then
  echo "[2/3] include already present in ${NGINX_CONF}"
else
  if ! grep -qE '^[[:space:]]*http[[:space:]]*\{' "${NGINX_CONF}"; then
    echo "ERROR: no 'http {' block found in ${NGINX_CONF}; add the include by hand:" >&2
    echo "       include ${INCLUDE_DIR}/*.conf;" >&2
    exit 1
  fi
  cp -a "${NGINX_CONF}" "${NGINX_CONF}.bak.$(date +%Y%m%d%H%M%S)"
  # Insert the include right after the first line that opens the http{} block.
  # awk (not sed) so the dir's slashes need no delimiter escaping.
  TMP_CONF="$(mktemp)"
  awk -v dir="${INCLUDE_DIR}" '
    !done && $0 ~ /^[[:space:]]*http[[:space:]]*\{/ {
      print; print "    include " dir "/*.conf;"; done=1; next
    }
    { print }
  ' "${NGINX_CONF}" >"${TMP_CONF}"
  cat "${TMP_CONF}" >"${NGINX_CONF}"   # preserve original perms/owner
  rm -f "${TMP_CONF}"
  echo "[2/3] added 'include ${INCLUDE_DIR}/*.conf;' to ${NGINX_CONF} (backup saved)"
fi

# --- 3. narrow sudoers rule ----------------------------------------------
TMP_SUDOERS="$(mktemp)"
trap 'rm -f "${TMP_SUDOERS}"' EXIT
cat >"${TMP_SUDOERS}" <<EOF
# Managed by deploy/setup-nginx-hyqs.sh — lets the Hyqs pipeline validate and
# reload nginx after writing per-project sites into ${INCLUDE_DIR}.
${HYQS_USER} ALL=(root) NOPASSWD: ${NGINX_BIN} -t, ${SYSTEMCTL_BIN} reload nginx
EOF
# Validate BEFORE installing so a typo can never lock out sudo.
if ! visudo -cf "${TMP_SUDOERS}"; then
  echo "ERROR: generated sudoers rule failed validation; not installing" >&2
  exit 1
fi
install -o root -g root -m 0440 "${TMP_SUDOERS}" "${SUDOERS_FILE}"
echo "[3/3] installed ${SUDOERS_FILE}"

# --- verify ---------------------------------------------------------------
echo "Validating full nginx config..."
"${NGINX_BIN}" -t
"${SYSTEMCTL_BIN}" reload nginx
echo "nginx reloaded."

echo
echo "Done. Quick check that the app user can reload without a password:"
echo "  sudo -u ${HYQS_USER} sudo -n ${SYSTEMCTL_BIN} reload nginx && echo OK"
