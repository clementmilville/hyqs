#!/usr/bin/env bash
#
# Install the nginx vhost for the self-hosted Zot registry (Epic 81), fronting
# it over TLS at DOMAIN and proxying to the loopback-only Zot container (see
# deploy/docker-compose.registry.yml). Idempotent, analogous to
# deploy/setup-nginx-hyqs.sh: writes a dual-stack IPv6 server block into the
# app-owned include dir that script provisions (/etc/nginx/hyqs.d/), the same
# way hyqs/pipeline/nginx_sites.py's render_site() does for per-project sites.
#
# Run as root:  sudo bash deploy/setup-nginx-registry.sh
# Override:     sudo DOMAIN=registry.example.com PORT=5100 bash deploy/setup-nginx-registry.sh
#            or sudo bash deploy/setup-nginx-registry.sh registry.example.com 5100

set -euo pipefail

DOMAIN="${1:-${DOMAIN:?set DOMAIN or pass it as $1, e.g. registry.example.com}}"
# SSL snippet named after the domain's first label, matching the convention
# in hyqs/pipeline/nginx_sites.py (example.com -> example-ssl.conf).
SSL_SNIPPET_NAME="${SSL_SNIPPET_NAME:-${DOMAIN#*.}}"
SSL_SNIPPET_NAME="${SSL_SNIPPET_NAME%%.*}-ssl.conf"
PORT="${2:-${PORT:-5100}}"
INCLUDE_DIR="/etc/nginx/hyqs.d"
CONF_PATH="${INCLUDE_DIR}/registry.conf"

# --- preconditions --------------------------------------------------------
if [[ "${EUID}" -ne 0 ]]; then
  echo "ERROR: run as root (sudo bash $0)" >&2
  exit 1
fi

NGINX_BIN="$(command -v nginx || true)"
SYSTEMCTL_BIN="$(command -v systemctl || true)"
if [[ -z "${NGINX_BIN}" || -z "${SYSTEMCTL_BIN}" ]]; then
  echo "ERROR: could not locate nginx and/or systemctl on PATH" >&2
  exit 1
fi

if [[ ! -d "${INCLUDE_DIR}" ]]; then
  echo "ERROR: ${INCLUDE_DIR} does not exist — run deploy/setup-nginx-hyqs.sh first" >&2
  exit 1
fi

echo "Installing registry vhost: domain=${DOMAIN} port=${PORT}"

# --- render + install (validate-then-install, rollback on failure) -------
TMP_CONF="$(mktemp)"
trap 'rm -f "${TMP_CONF}"' EXIT

cat >"${TMP_CONF}" <<EOF
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name ${DOMAIN};
    include snippets/${SSL_SNIPPET_NAME};

    location / {
        proxy_pass http://127.0.0.1:${PORT};
        include snippets/proxy-common.conf;
        # Registry pushes/pulls can ship large layers; don't buffer/truncate them.
        client_max_body_size 0;
    }
}
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};
    return 301 https://\$host\$request_uri;
}
EOF

PREVIOUS_CONF=""
if [[ -f "${CONF_PATH}" ]]; then
  PREVIOUS_CONF="$(mktemp)"
  cp -a "${CONF_PATH}" "${PREVIOUS_CONF}"
fi

install -o root -g root -m 0644 "${TMP_CONF}" "${CONF_PATH}"

echo "Validating nginx config..."
if ! "${NGINX_BIN}" -t; then
  echo "ERROR: nginx -t failed after installing ${CONF_PATH}; rolling back" >&2
  if [[ -n "${PREVIOUS_CONF}" ]]; then
    cp -a "${PREVIOUS_CONF}" "${CONF_PATH}"
  else
    rm -f "${CONF_PATH}"
  fi
  rm -f "${PREVIOUS_CONF}"
  exit 1
fi

"${SYSTEMCTL_BIN}" reload nginx

echo "Re-validating after reload..."
if ! "${NGINX_BIN}" -t; then
  echo "ERROR: nginx -t failed post-reload for ${CONF_PATH}; rolling back" >&2
  if [[ -n "${PREVIOUS_CONF}" ]]; then
    cp -a "${PREVIOUS_CONF}" "${CONF_PATH}"
  else
    rm -f "${CONF_PATH}"
  fi
  "${SYSTEMCTL_BIN}" reload nginx
  rm -f "${PREVIOUS_CONF}"
  exit 1
fi

rm -f "${PREVIOUS_CONF}"
echo "Done. ${DOMAIN} -> 127.0.0.1:${PORT} (installed at ${CONF_PATH})"
