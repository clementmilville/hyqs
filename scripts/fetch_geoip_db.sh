#!/usr/bin/env bash
#
# Download the current month's DB-IP City Lite GeoIP database (MaxMind MMDB
# format) and install it at the path Hyqs reads via HYQS_GEOIP_DB.
#
# DB-IP City Lite is distributed under CC BY 4.0 — any product using this
# database must visibly credit DB-IP (https://db-ip.com) per that license.
#
# Usage:
#   bash scripts/fetch_geoip_db.sh [destination-path]
#
# Destination is resolved in order: the positional argument, $HYQS_GEOIP_DB,
# or ~/.hyqs/data/geoip/dbip-city-lite.mmdb.

set -euo pipefail

log() { printf '%s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

command -v curl >/dev/null 2>&1 || die "curl is not installed"
command -v gunzip >/dev/null 2>&1 || die "gunzip is not installed"

DEST="${1:-${HYQS_GEOIP_DB:-${HOME}/.hyqs/data/geoip/dbip-city-lite.mmdb}}"
MONTH="$(date +%Y-%m)"
URL="https://download.db-ip.com/free/dbip-city-lite-${MONTH}.mmdb.gz"

mkdir -p "$(dirname "${DEST}")"

tmp_gz="$(mktemp)"
trap 'rm -f "${tmp_gz}"' EXIT

log "Downloading ${URL}"
curl -fSL --retry 3 -o "${tmp_gz}" "${URL}" || die "download failed: ${URL}"

gunzip -c "${tmp_gz}" > "${DEST}.tmp" || die "gunzip failed for ${tmp_gz}"
mv -f "${DEST}.tmp" "${DEST}"

log "Installed GeoIP database at ${DEST}"
log "Attribution required: IP Geolocation by DB-IP (https://db-ip.com), CC BY 4.0"
