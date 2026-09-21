#!/usr/bin/env bash
#
# enable_ipv6_nginx.sh — Add IPv6 (`listen [::]:...`) directives to the nginx
# server block(s) that serve a given domain, so IPv6-capable clients stop
# getting ERR_CONNECTION_RESET when DNS publishes an AAAA record but nginx
# only listens on IPv4 (0.0.0.0).
#
# Safe by construction:
#   * only touches server blocks whose server_name matches the domain
#   * idempotent — re-running makes no further changes
#   * backs up every file it edits (<file>.bak.<timestamp>)
#   * `nginx -t` before reload; auto-rolls back and aborts on any failure
#   * --dry-run prints the diff without writing anything
#
# Usage:
#   sudo ./enable_ipv6_nginx.sh [--dry-run] [DOMAIN]
#
# DOMAIN is required, e.g. hyqs.example.com.

set -euo pipefail

DOMAIN="${DOMAIN:?set DOMAIN, e.g. hyqs.example.com}"
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    -*) echo "Unknown option: $arg" >&2; exit 2 ;;
    *)  DOMAIN="$arg" ;;
  esac
done

log() { printf '%s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ ${EUID:-$(id -u)} -eq 0 ]] || die "must run as root (use: sudo $0 $*)"
command -v nginx >/dev/null 2>&1 || die "nginx not found in PATH"

TS="$(date +%Y%m%d-%H%M%S)"

# ---------------------------------------------------------------------------
# 1. Discover the config file(s) that define a server block for $DOMAIN.
#    `nginx -T` dumps the fully-resolved config, annotated with the source
#    file of each section (# configuration file: /path). We map every
#    server_name line back to its file.
# ---------------------------------------------------------------------------
mapfile -t FILES < <(
  nginx -T 2>/dev/null | awk -v domain="$DOMAIN" '
    # Marker format varies by nginx version:
    #   "# configuration file: /path"   or   "# configuration file /path:"
    /configuration file/ {
      f = $0
      sub(/.*configuration file[: \t]*/, "", f)   # strip up to the path
      sub(/[: \t]*$/, "", f)                       # strip trailing colon/space
      if (f != "") file = f
    }
    $1 == "server_name" {
      for (i = 2; i <= NF; i++) {
        tok = $i; sub(/;$/, "", tok)
        if (tok == domain && file != "") { print file; break }
      }
    }
  ' | awk 'NF' | sort -u
)

if [[ ${#FILES[@]} -eq 0 ]]; then
  # Fallback: grep the on-disk config tree.
  mapfile -t FILES < <(grep -rlE "server_name[^;]*\\b${DOMAIN//./\\.}\\b" \
    /etc/nginx 2>/dev/null | sort -u)
fi

[[ ${#FILES[@]} -gt 0 ]] || die "no nginx server block found for $DOMAIN"
log "Config file(s) serving ${DOMAIN}:"
printf '  %s\n' "${FILES[@]}" >&2

# ---------------------------------------------------------------------------
# 2. awk transform: within each top-level `server { ... }` block whose
#    server_name matches $DOMAIN, mirror every IPv4 `listen ...:80|443`
#    directive to an equivalent `listen [::]:...` line (unless one already
#    exists in that block). Preserves indentation and listen flags
#    (ssl, http2, default_server, ...).
# ---------------------------------------------------------------------------
read -r -d '' AWK_PROG <<'AWK' || true
function reset_block() { blk_n = 0; depth = 0; has_domain = 0; has6_80 = 0; has6_443 = 0 }

# Emit the IPv6 counterpart of an IPv4 listen line, or "" if not applicable.
function ipv6_line(line,   indent, body, n, toks, i, addr, port, flags) {
  match(line, /^[ \t]*/); indent = substr(line, 1, RLENGTH)
  body = line; sub(/^[ \t]*/, "", body); sub(/[ \t]*;[ \t]*$/, "", body)
  n = split(body, toks, /[ \t]+/)
  if (toks[1] != "listen") return ""
  addr = toks[2]
  if (addr ~ /^\[/) return ""                       # already IPv6
  if (addr ~ /:/)      { port = addr; sub(/^.*:/, "", port) }
  else if (addr ~ /^[0-9]+$/) { port = addr }
  else return ""                                    # unix:/hostname — skip
  if (port != "80" && port != "443") return ""
  flags = ""
  for (i = 3; i <= n; i++) flags = flags " " toks[i]
  return indent "listen [::]:" port flags ";"
}

function flush_block(   i, line, v, port) {
  if (!has_domain) { for (i = 1; i <= blk_n; i++) print blk[i]; return }
  # Pre-scan for existing IPv6 listeners so we stay idempotent.
  for (i = 1; i <= blk_n; i++) {
    if (blk[i] ~ /listen[ \t]+\[::\]:80([ \t;])/)  has6_80 = 1
    if (blk[i] ~ /listen[ \t]+\[::\]:443([ \t;])/) has6_443 = 1
  }
  for (i = 1; i <= blk_n; i++) {
    line = blk[i]; print line
    v = ipv6_line(line)
    if (v == "") continue
    port = (v ~ /\[::\]:443/) ? "443" : (v ~ /\[::\]:80/ ? "80" : "")
    if (port == "80"  && has6_80)  continue
    if (port == "443" && has6_443) continue
    print v
    if (port == "80")  has6_80  = 1
    if (port == "443") has6_443 = 1
  }
}

BEGIN { inserver = 0; reset_block() }
{
  if (inserver == 0) {
    if ($0 ~ /(^|[ \t])server[ \t]*\{/) {
      inserver = 1; reset_block()
      blk[++blk_n] = $0
      tmp = $0; depth += gsub(/\{/, "{", tmp); depth -= gsub(/\}/, "}", tmp)
      if ($0 ~ /server_name/ && index($0, DOMAIN)) has_domain = 1
      if (depth <= 0) { flush_block(); inserver = 0 }
    } else print
    next
  }
  blk[++blk_n] = $0
  tmp = $0; depth += gsub(/\{/, "{", tmp); depth -= gsub(/\}/, "}", tmp)
  if ($0 ~ /server_name/ && index($0, DOMAIN)) has_domain = 1
  if (depth <= 0) { flush_block(); inserver = 0 }
}
END { if (inserver) flush_block() }   # unbalanced braces — flush what we have
AWK

# ---------------------------------------------------------------------------
# 3. Apply per file, with backup. Collect backups for rollback.
# ---------------------------------------------------------------------------
declare -a BACKUPS=() CHANGED=()
changed_any=0

for f in "${FILES[@]}"; do
  [[ -f "$f" && -w "$f" ]] || die "cannot write $f"
  tmp="$(mktemp)"
  awk -v DOMAIN="$DOMAIN" "$AWK_PROG" "$f" > "$tmp"

  if cmp -s "$f" "$tmp"; then
    log "= ${f}: already dual-stack, no change"
    rm -f "$tmp"
    continue
  fi

  changed_any=1
  log "~ ${f}: adding IPv6 listeners"
  diff -u "$f" "$tmp" | sed 's/^/    /' >&2 || true

  if [[ $DRY_RUN -eq 1 ]]; then
    rm -f "$tmp"
    continue
  fi

  bak="${f}.bak.${TS}"
  cp -p "$f" "$bak"
  BACKUPS+=("$f=$bak")
  cat "$tmp" > "$f"        # preserve original inode/permissions
  rm -f "$tmp"
  CHANGED+=("$f")
done

if [[ $changed_any -eq 0 ]]; then
  log "Nothing to do — $DOMAIN already listens on IPv6."
  exit 0
fi

if [[ $DRY_RUN -eq 1 ]]; then
  log "(dry-run) no files written."
  exit 0
fi

# ---------------------------------------------------------------------------
# 4. Validate; roll back on any error.
# ---------------------------------------------------------------------------
rollback() {
  log "!! rolling back changes"
  for pair in "${BACKUPS[@]}"; do
    cp -p "${pair#*=}" "${pair%%=*}"
  done
}

if ! nginx -t; then
  rollback
  die "nginx -t failed — changes reverted, nginx untouched"
fi

if ! systemctl reload nginx 2>/dev/null && ! nginx -s reload; then
  rollback
  nginx -t >/dev/null 2>&1 && systemctl reload nginx 2>/dev/null || true
  die "reload failed — changes reverted"
fi

log "nginx reloaded. Backups: ${BACKUPS[*]//=/ -> }"

# ---------------------------------------------------------------------------
# 5. Verify over IPv6 (best effort — needs an IPv6 route on this host).
# ---------------------------------------------------------------------------
if curl -6 -sS -m 8 -o /dev/null -w '%{http_code}' "https://${DOMAIN}/api/health" 2>/dev/null | grep -qE '^[23]'; then
  log "✓ IPv6 request to https://${DOMAIN}/api/health succeeded"
else
  log "… IPv6 self-check inconclusive (host may lack an IPv6 route to itself)."
  log "  Verify externally, e.g.:  curl -6 -I https://${DOMAIN}/"
fi

log "Done."
