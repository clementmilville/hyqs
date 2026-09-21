#!/usr/bin/env bash
#
# One-command install of Hyqs on a fresh Debian/Ubuntu VPS.
#
#   git clone <repo-url> ~/hyqs-ai
#   cd ~/hyqs-ai
#   bash deploy/install.sh                          # localhost only
#   bash deploy/install.sh --domain hyqs.example.com --email you@example.com
#
# What it does, in order: system packages -> uv -> Claude CLI -> Python deps ->
# .env (generated secrets) -> Postgres -> frontend build -> systemd user units
# -> optional nginx + TLS. Every step is idempotent, so re-running it after a
# failure resumes rather than duplicating work.
#
# It NEVER overwrites an existing .env — your secrets survive a re-run.
#
# Run as the unprivileged user that will own the install (NOT root). sudo is
# used only for the steps that genuinely need it: apt, nginx, and enabling
# systemd lingering.

set -euo pipefail

# --- arguments --------------------------------------------------------------
DOMAIN=""
ACME_EMAIL=""
SKIP_APT=0
NO_START=0
SKIP_DNS_CHECK=0
GITHUB_ORG=""
GITHUB_ORG_SUPPLIED=0

usage() {
    cat <<'USAGE'
Usage: bash deploy/install.sh [options]

  --domain <fqdn>    Serve the console at this hostname behind nginx + Let's
                     Encrypt TLS. Its DNS A/AAAA records must already point at
                     this host. Omit to bind localhost only (see --help notes).
  --email <address>  Contact address for Let's Encrypt. Required with --domain.
  --github-org <name>
                     Create provisioned project repositories in this GitHub
                     organization. An empty value uses your personal account.
  --skip-apt         Don't touch apt; assume the system packages are present.
  --skip-dns-check   Don't verify --domain resolves here before requesting a
                     certificate. Only for a split-horizon or proxied setup.
  --no-start         Install everything but don't start the services.
  -h, --help         Show this message.

Without --domain the console listens on 127.0.0.1:8787 only. That is the safe
default: Hyqs runs shell commands and edits files on this host, so it should
never be exposed to the internet without TLS and an account to sign in with.
Reach it over an SSH tunnel:  ssh -L 8787:127.0.0.1:8787 <user>@<host>
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --domain) DOMAIN="${2:-}"; shift 2 ;;
        --email)  ACME_EMAIL="${2:-}"; shift 2 ;;
        --github-org) GITHUB_ORG="${2:-}"; GITHUB_ORG_SUPPLIED=1; shift 2 ;;
        --skip-apt) SKIP_APT=1; shift ;;
        --skip-dns-check) SKIP_DNS_CHECK=1; shift ;;
        --no-start) NO_START=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "ERROR: unknown option '$1' (try --help)" >&2; exit 1 ;;
    esac
done

if [[ -n "${DOMAIN}" && -z "${ACME_EMAIL}" ]]; then
    echo "ERROR: --domain requires --email (Let's Encrypt needs a contact address)" >&2
    exit 1
fi

# --- preflight --------------------------------------------------------------
step() { echo; echo "=== $* ==="; }

if [[ "${EUID}" -eq 0 ]]; then
    cat >&2 <<'EOF'
ERROR: do not run this as root.

Hyqs runs as a normal user under `systemd --user`, and the agent executes shell
commands — running the whole stack as root would give every generated command
root on this box. Create a user first:

    adduser --disabled-password --gecos "" hyqs
    usermod -aG sudo hyqs
    su - hyqs
EOF
    exit 1
fi

if ! command -v sudo >/dev/null 2>&1; then
    echo "ERROR: sudo is required (apt, nginx and lingering need it)" >&2
    exit 1
fi

# The shipped systemd units hardcode %h/hyqs-ai, and deploy/release.sh re-copies
# them from the repo on every release — so a checkout anywhere else would be
# silently reverted to a broken path at the next deploy. Require the canonical
# location rather than papering over it.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXPECTED_DIR="${HOME}/hyqs-ai"
if [[ "${REPO_DIR}" != "${EXPECTED_DIR}" ]]; then
    cat >&2 <<EOF
ERROR: the checkout must live at ${EXPECTED_DIR}, but this one is at:
         ${REPO_DIR}

The systemd units in deploy/ reference %h/hyqs-ai, and deploy/release.sh
refreshes them from the repo on every release — so a custom path gets reverted
on your next deploy. Move the checkout:

    mv "${REPO_DIR}" "${EXPECTED_DIR}" && cd "${EXPECTED_DIR}"
EOF
    exit 1
fi
cd "${REPO_DIR}"

if ! systemctl --user show-environment >/dev/null 2>&1; then
    echo "ERROR: no systemd --user session. Log in over SSH as this user (not via 'su')." >&2
    exit 1
fi

# Project provisioning shells out to GitHub CLI, but the rest of Hyqs does not
# depend on it. Keep this warning-only so an operator can install the service
# now and authenticate gh before creating the first project.
step "preflight: GitHub CLI"
if ! command -v gh >/dev/null 2>&1; then
    echo "WARNING: GitHub CLI (gh) is not installed; project repository provisioning will not work until it is installed and authenticated." >&2
elif ! gh auth status >/dev/null 2>&1; then
    echo "WARNING: GitHub CLI is not authenticated; run 'gh auth login' before provisioning project repositories." >&2
else
    echo "GitHub CLI is installed and authenticated."
fi

# --- preflight: does --domain actually point here? ---------------------------
# Certbot's HTTP-01 challenge resolves the name from the public internet and
# fetches a token over port 80. If the record is missing or points elsewhere
# this cannot succeed — and finding that out at step 9, after apt, a frontend
# build and a service start, wastes the operator's time on something knowable
# up front. Fail here instead, while nothing has been changed.
if [[ -n "${DOMAIN}" && "${SKIP_DNS_CHECK}" -eq 0 ]]; then
    step "preflight: DNS for ${DOMAIN}"
    resolved_ips="$(getent ahostsv4 "${DOMAIN}" 2>/dev/null | awk '{print $1}' | sort -u)"
    if [[ -z "${resolved_ips}" ]]; then
        cat >&2 <<EOF
ERROR: ${DOMAIN} does not resolve (no A record).

  Create an A record for it pointing at this host, wait for it to propagate,
  then re-run. This host's public address:
      $(curl -fsS -4 --max-time 5 https://api.ipify.org 2>/dev/null || echo "  (could not determine — try: curl -4 ifconfig.me)")

  Add an AAAA record too if this host has IPv6, or v6-capable clients will
  resolve AAAA, find nothing, and fail to reach it.

  Re-run with --skip-dns-check only for a split-horizon or proxied setup.
EOF
        exit 1
    fi
    host_ip="$(curl -fsS -4 --max-time 5 https://api.ipify.org 2>/dev/null || true)"
    if [[ -n "${host_ip}" ]] && ! grep -qxF "${host_ip}" <<<"${resolved_ips}"; then
        cat >&2 <<EOF
ERROR: ${DOMAIN} resolves to $(tr '\n' ' ' <<<"${resolved_ips}")but this host is ${host_ip}.

  Certbot's HTTP-01 challenge is fetched from whatever that name points at, so
  it would validate against the wrong machine. Repoint the record and re-run.

  Re-run with --skip-dns-check if the name is fronted by a proxy or CDN that
  forwards port 80 here.
EOF
        exit 1
    fi
    echo "${DOMAIN} -> $(tr '\n' ' ' <<<"${resolved_ips}")(matches this host)"
fi

# --- 1. system packages -----------------------------------------------------
if [[ "${SKIP_APT}" -eq 1 ]]; then
    step "[1/9] system packages (skipped)"
else
    step "[1/9] system packages"
    if ! command -v apt-get >/dev/null 2>&1; then
        echo "ERROR: this installer targets Debian/Ubuntu (apt-get not found)." >&2
        echo "       Install the equivalents by hand and re-run with --skip-apt." >&2
        exit 1
    fi
    sudo apt-get update -qq
    # bubblewrap + socat are hard prerequisites: the sandboxed EXPLORER agent
    # role fails closed (refuses to run) when either is missing.
    sudo apt-get install -y --no-install-recommends \
        git curl ca-certificates jq openssl \
        python3 python3-venv \
        nodejs npm \
        docker.io docker-compose-v2 \
        bubblewrap socat
    if [[ -n "${DOMAIN}" ]]; then
        sudo apt-get install -y --no-install-recommends nginx certbot python3-certbot-nginx
    fi
    sudo systemctl enable --now docker
fi

# The pipeline shells out to docker for project deploys, so the app user needs
# non-root docker access.
#
# Do NOT decide this from group membership. `id -nG "$USER"` reads /etc/group,
# which lists the group as soon as usermod runs — but the CURRENT login session
# keeps the groups it was created with until you log out and back in. A re-run
# of this script therefore sees "already in the docker group", drops the `sg`
# wrapper, and dies with a permission-denied on the socket. Probe what actually
# works instead, and remember how to invoke docker for the rest of this run.
DOCKER_VIA=""            # "" = direct, "sg" = via `sg docker`
if docker info >/dev/null 2>&1; then
    DOCKER_VIA=""
else
    if ! id -nG "${USER}" | tr ' ' '\n' | grep -qx docker; then
        echo "adding ${USER} to the docker group…"
        sudo usermod -aG docker "${USER}"
    fi
    # `sg docker` starts a shell that HAS the group, so this run works without
    # forcing a re-login.
    if sg docker -c 'docker info' >/dev/null 2>&1; then
        DOCKER_VIA="sg"
        DOCKER_RELOGIN_NEEDED=1
    else
        cat >&2 <<EOF
ERROR: cannot reach the Docker daemon as ${USER}.

  Tried directly and via \`sg docker\`. Check the daemon is running:
      systemctl status docker
  then log out and back in (group changes only apply to a new session) and
  re-run this script.
EOF
        exit 1
    fi
fi
docker_run() {
    if [[ "${DOCKER_VIA}" == "sg" ]]; then
        sg docker -c "$*"
    else
        eval "$@"
    fi
}

# --- 2. uv ------------------------------------------------------------------
step "[2/9] uv (Python toolchain)"
if command -v uv >/dev/null 2>&1 || [[ -x "${HOME}/.local/bin/uv" ]]; then
    echo "uv already installed: $("${HOME}/.local/bin/uv" --version 2>/dev/null || uv --version)"
else
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="${HOME}/.local/bin:${PATH}"
command -v uv >/dev/null 2>&1 || { echo "ERROR: uv not on PATH after install" >&2; exit 1; }

# --- 3. Claude CLI ----------------------------------------------------------
# The Agent SDK does not talk to the API directly — it spawns the `claude`
# binary, so it is a hard runtime dependency of every AI stage.
step "[3/9] Claude Code CLI"
if command -v claude >/dev/null 2>&1; then
    echo "claude already installed: $(claude --version 2>&1 | head -1)"
else
    sudo npm install -g @anthropic-ai/claude-code
fi

# --- 4. Python dependencies -------------------------------------------------
step "[4/9] Python dependencies"
uv sync

# --- 4b. git identity --------------------------------------------------------
# Provisioning a new project scaffolds a repo and runs `git commit`, which fails
# outright on a fresh host with "Author identity unknown". Nothing else in the
# install surfaces that, so the first project creation dies deep inside the
# provisioning call with a git error the operator has no reason to expect.
# Set an identity only when one is genuinely absent — never overwrite the
# operator's own.
step "[4b/9] git identity"
if [[ -z "$(git config --global user.email || true)" ]]; then
    git_identity_email="${ACME_EMAIL:-hyqs@$(hostname -f 2>/dev/null || hostname)}"
    git config --global user.email "${git_identity_email}"
    echo "set git user.email = ${git_identity_email}"
else
    echo "git user.email already set: $(git config --global user.email)"
fi
if [[ -z "$(git config --global user.name || true)" ]]; then
    git config --global user.name "Hyqs"
    echo "set git user.name = Hyqs"
else
    echo "git user.name already set: $(git config --global user.name)"
fi

# --- 5. .env ----------------------------------------------------------------
step "[5/9] configuration (.env)"
if [[ -f .env ]]; then
    echo ".env already exists — leaving it untouched."
    echo "(delete it and re-run if you want a freshly generated one)"
else
    if [[ "${GITHUB_ORG_SUPPLIED}" -eq 0 && -t 0 ]]; then
        echo "Project repositories can be created in a GitHub organization."
        read -r -p "GitHub organization (leave empty for my personal account): " GITHUB_ORG
    fi
    if [[ -n "${GITHUB_ORG}" ]]; then
        # Same rule hyqs/pipeline/provision.py enforces on HYQS_GITHUB_ORG at
        # runtime (_GITHUB_ORG_RE): letters, digits, single hyphens between
        # segments. Rejecting anything else here — in particular newlines,
        # which a scripted --github-org value could carry — keeps a bad value
        # from ever reaching set_var, which does a raw substitution into .env.
        if ! [[ "${GITHUB_ORG}" =~ ^[A-Za-z0-9]+(-[A-Za-z0-9]+)*$ ]]; then
            echo "ERROR: --github-org value '${GITHUB_ORG}' is not a valid GitHub organization login (letters, digits, and single hyphens between segments only)." >&2
            exit 1
        fi
        echo "GitHub organization: ${GITHUB_ORG}"
        echo "The authenticated GitHub account must be able to create repositories in ${GITHUB_ORG}."
    else
        echo "GitHub organization not set; project repositories will use the authenticated user's personal account."
    fi

    # Every generated value below is restricted to characters that survive
    # BOTH python-dotenv and plain shell sourcing (`set -a; . ./.env`), which
    # deploy/docker-compose.yml --env-file and release.sh rely on. Keep any new
    # secret alphanumeric / hex / base64url — a raw `openssl rand -base64` can
    # emit `/` and `+`, and a quoted value is not portable across the parsers.
    PG_PASSWORD="$(openssl rand -hex 24)"
    WEB_TOKEN="$(openssl rand -hex 32)"
    ADMIN_PASSWORD="$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)"
    FERNET_KEY="$(uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
    ADMIN_EMAIL="${ACME_EMAIL:-admin@localhost}"

    cp .env.example .env
    # Only the console binds publicly, and only when a domain fronts it with
    # TLS; otherwise loopback so a fresh box is never exposed by default.
    if [[ -n "${DOMAIN}" ]]; then
        WEB_HOST="127.0.0.1"   # nginx is the only thing that needs to reach it
        BASE_URL="https://${DOMAIN}"
    else
        WEB_HOST="127.0.0.1"
        BASE_URL=""
    fi

    set_var() {
        local key="$1" value="$2"
        # Replace the first assignment of this key, commented or not.
        python3 - "$key" "$value" <<'PY'
import io, re, sys
key, value = sys.argv[1], sys.argv[2]
p = ".env"
s = io.open(p, encoding="utf-8").read()
pattern = re.compile(rf"^#?\s*{re.escape(key)}=.*$", re.MULTILINE)
line = f"{key}={value}"
s, n = pattern.subn(line, s, count=1)
if n == 0:
    s = s.rstrip("\n") + f"\n{line}\n"
io.open(p, "w", encoding="utf-8").write(s)
PY
    }

    set_var POSTGRES_PASSWORD "${PG_PASSWORD}"
    set_var HYQS_DB_URL "postgresql://hyqs:${PG_PASSWORD}@localhost:5432/hyqs"
    set_var HYQS_WEB_TOKEN "${WEB_TOKEN}"
    set_var HYQS_WEB_HOST "${WEB_HOST}"
    set_var HYQS_WEB_BASE_URL "${BASE_URL}"
    # .env.example carries a documentation example, but an explicit MCP URL
    # overrides HYQS_WEB_BASE_URL at runtime. Do not leave that example active
    # in a freshly generated installation.
    set_var HYQS_MCP_RESOURCE_URL ""
    set_var HYQS_ADMIN_EMAIL "${ADMIN_EMAIL}"
    set_var HYQS_ADMIN_PASSWORD "${ADMIN_PASSWORD}"
    set_var HYQS_CREDENTIAL_ENCRYPTION_KEY "${FERNET_KEY}"
    set_var HYQS_DEFAULT_REPO "${REPO_DIR}"
    set_var HYQS_GITHUB_ORG "${GITHUB_ORG}"
    chmod 600 .env

    # Stash the generated credentials so the final summary can show them even
    # on a re-run that skips this block.
    printf 'admin_email=%s\nadmin_password=%s\nweb_token=%s\n' \
        "${ADMIN_EMAIL}" "${ADMIN_PASSWORD}" "${WEB_TOKEN}" > .install-credentials
    chmod 600 .install-credentials
    echo "generated .env with fresh secrets (mode 600)"
fi

# --- 6. Postgres ------------------------------------------------------------
step "[6/9] Postgres (coordination plane)"
if ! docker_run "docker compose --env-file .env -f deploy/docker-compose.yml up -d"; then
    echo "ERROR: could not start the Postgres container (see the output above)." >&2
    exit 1
fi
echo -n "waiting for Postgres to accept connections"
for _ in $(seq 1 60); do
    if docker_run "docker exec hyqs-postgres pg_isready -U hyqs -d hyqs" >/dev/null 2>&1; then
        echo " — ready."
        break
    fi
    echo -n "."
    sleep 1
done
if ! docker_run "docker exec hyqs-postgres pg_isready -U hyqs -d hyqs" >/dev/null 2>&1; then
    echo
    echo "ERROR: Postgres did not become ready. Its log:" >&2
    echo "---------------------------------------------------------------" >&2
    docker_run "docker logs --tail 40 hyqs-postgres" 2>&1 | sed 's/^/  /' >&2 || true
    echo "---------------------------------------------------------------" >&2
    exit 1
fi
# The schema is created on first connect (JobStore._ensure_schema) — no separate
# migration step to run.

# --- 7. frontend ------------------------------------------------------------
step "[7/9] web console frontend"
npm --prefix hyqs/web/frontend ci
npm --prefix hyqs/web/frontend run build

# --- 8. systemd user services ----------------------------------------------
step "[8/9] systemd user services"
UNIT_DIR="${HOME}/.config/systemd/user"
mkdir -p "${UNIT_DIR}"
for unit in hyqs-web@.service hyqs-pipeline@.service hyqs-web.service hyqs-pipeline.service; do
    if [[ -f "deploy/${unit}" ]]; then
        cp "deploy/${unit}" "${UNIT_DIR}/${unit}"
    fi
done
systemctl --user daemon-reload

# Without lingering, systemd tears down the user manager on logout and the
# 24/7 services die with your SSH session.
if [[ "$(loginctl show-user "${USER}" -p Linger --value 2>/dev/null)" != "yes" ]]; then
    sudo loginctl enable-linger "${USER}"
    echo "enabled systemd lingering for ${USER}"
fi

if [[ "${NO_START}" -eq 1 ]]; then
    echo "--no-start given; not starting services."
else
    # Stop any generations a previous run of this script left behind. Each run
    # starts hyqs-{web,pipeline}@<timestamp>, so without this an idempotent
    # re-run LEAKS a whole fleet every time: four re-runs meant four pipeline
    # workers competing for jobs and four times the memory on the host.
    #
    # deploy/release.sh does a health-gated blue-green swap because a running
    # system must not drop requests mid-deploy. This is an install, so a plain
    # stop-then-start is correct and far simpler — there is nothing to keep
    # serving.
    mapfile -t stale_units < <(
        systemctl --user list-units 'hyqs-web@*' 'hyqs-pipeline@*' \
            --all --no-legend --plain 2>/dev/null | awk '{print $1}'
    )
    if [[ ${#stale_units[@]} -gt 0 ]]; then
        echo "stopping ${#stale_units[@]} unit(s) from a previous run…"
        for unit in "${stale_units[@]}"; do
            systemctl --user stop "${unit}" >/dev/null 2>&1 || true
            # A templated instance stays loaded-but-inactive after stop; reset it
            # so it does not linger in `list-units --all` forever.
            systemctl --user reset-failed "${unit}" >/dev/null 2>&1 || true
        done
    fi

    GEN="$(date +%s)"
    systemctl --user start "hyqs-web@${GEN}"
    systemctl --user start "hyqs-pipeline@${GEN}"
    # Watch for BOTH outcomes. Polling only for a healthy response turns a
    # startup crash into a silent 60-second wait and then a generic timeout —
    # the operator is left to go digging for a traceback the installer already
    # had access to. Break out the moment the unit gives up, and print the
    # error here.
    echo -n "waiting for the console to answer"
    WEB_UP=0
    WEB_DEAD=0
    for _ in $(seq 1 60); do
        if curl -fsS --max-time 2 "http://127.0.0.1:${HYQS_WEB_PORT:-8787}/api/health" >/dev/null 2>&1; then
            WEB_UP=1; echo " — up."; break
        fi
        # `failed` = start limit hit after repeated crashes; `inactive` = it
        # exited and systemd is done retrying. Either way, waiting is pointless.
        web_state="$(systemctl --user show -p ActiveState --value "hyqs-web@${GEN}" 2>/dev/null || true)"
        if [[ "${web_state}" == "failed" || "${web_state}" == "inactive" ]]; then
            WEB_DEAD=1; echo; break
        fi
        echo -n "."; sleep 1
    done

    if [[ "${WEB_UP}" -ne 1 ]]; then
        echo
        if [[ "${WEB_DEAD}" -eq 1 ]]; then
            echo "ERROR: the console failed to start. Its last error:" >&2
        else
            echo "ERROR: the console never answered on 127.0.0.1:${HYQS_WEB_PORT:-8787}." >&2
            echo "       It is still running, so this is a bind or hang, not a crash." >&2
            echo "       Recent log:" >&2
        fi
        echo "---------------------------------------------------------------" >&2
        journalctl --user -u "hyqs-web@${GEN}" -n 40 --no-pager 2>/dev/null \
            | sed 's/^/  /' >&2 || true
        echo "---------------------------------------------------------------" >&2
        echo >&2
        echo "Fix the cause, then re-run this script — it resumes from here." >&2
        exit 1
    fi

    # The pipeline has no HTTP surface to probe, but a crash-loop is still worth
    # catching now rather than leaving a silently dead worker behind.
    pipeline_state="$(systemctl --user show -p ActiveState --value "hyqs-pipeline@${GEN}" 2>/dev/null || true)"
    if [[ "${pipeline_state}" == "failed" || "${pipeline_state}" == "inactive" ]]; then
        echo "ERROR: the pipeline worker failed to start. Its last error:" >&2
        echo "---------------------------------------------------------------" >&2
        journalctl --user -u "hyqs-pipeline@${GEN}" -n 40 --no-pager 2>/dev/null \
            | sed 's/^/  /' >&2 || true
        echo "---------------------------------------------------------------" >&2
        exit 1
    fi
fi

# --- 9. nginx + TLS (optional) ---------------------------------------------
if [[ -z "${DOMAIN}" ]]; then
    step "[9/9] nginx (skipped — no --domain)"
else
    step "[9/9] nginx + TLS for ${DOMAIN}"

    # Shared proxy directives, referenced by the vhost below.
    sudo install -d -m 0755 /etc/nginx/snippets
    sudo tee /etc/nginx/snippets/hyqs-proxy.conf >/dev/null <<'SNIPPET'
proxy_http_version 1.1;
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header Upgrade           $http_upgrade;
proxy_set_header Connection        "upgrade";
proxy_read_timeout 3600s;
SNIPPET

    # HTTP-only to begin with; certbot --nginx rewrites this block in place to
    # add the TLS listener and the port-80 redirect.
    sudo tee "/etc/nginx/sites-available/${DOMAIN}" >/dev/null <<VHOST
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};

    # Cap request bodies at the edge so an oversized upload is rejected before
    # it reaches the app.
    client_max_body_size 25m;

    # MCP endpoint — SSE / streamable-HTTP transport: buffering off and long
    # timeouts, or streamed responses stall.
    location /mcp {
        proxy_pass http://127.0.0.1:8787;
        include snippets/hyqs-proxy.conf;
        proxy_buffering off;
        proxy_cache off;
        proxy_send_timeout 3600s;
    }

    location / {
        proxy_pass http://127.0.0.1:8787;
        include snippets/hyqs-proxy.conf;
    }
}
VHOST
    sudo ln -sf "/etc/nginx/sites-available/${DOMAIN}" "/etc/nginx/sites-enabled/${DOMAIN}"
    sudo nginx -t
    sudo systemctl reload nginx

    echo "requesting a certificate for ${DOMAIN}…"
    sudo certbot --nginx -d "${DOMAIN}" \
        --non-interactive --agree-tos --email "${ACME_EMAIL}" --redirect
    sudo nginx -t
    sudo systemctl reload nginx

    # Lets the pipeline register per-project vhosts without full root.
    sudo HYQS_USER="${USER}" bash deploy/setup-nginx-hyqs.sh
fi

# --- summary ----------------------------------------------------------------
CONSOLE_URL="http://127.0.0.1:8787"
[[ -n "${DOMAIN}" ]] && CONSOLE_URL="https://${DOMAIN}"

echo
echo "============================================================"
echo " Hyqs is installed."
echo "============================================================"
echo
echo "Console:  ${CONSOLE_URL}"
if [[ -z "${DOMAIN}" ]]; then
    echo "          (loopback only — tunnel in with:"
    echo "           ssh -L 8787:127.0.0.1:8787 ${USER}@<this-host>)"
fi
if [[ -f .install-credentials ]]; then
    echo
    echo "First admin (created on first start, from .env):"
    sed 's/^/  /' .install-credentials
    echo
    echo "  These are also in .env. Sign in, change the password, then remove"
    echo "  HYQS_ADMIN_EMAIL / HYQS_ADMIN_PASSWORD from .env and delete"
    echo "  .install-credentials."
fi

# --- MCP (agent client) access -----------------------------------------------
# Read through Config so URL precedence and dotenv whitespace/quoting semantics
# stay identical to the running application. Clear inherited values for these
# keys because the systemd services use this installation's .env file.
mapfile -t MCP_CONFIG < <(
    env -u HYQS_MCP_RESOURCE_URL -u HYQS_WEB_BASE_URL \
        -u HYQS_WEB_PORT -u HYQS_GOOGLE_CLIENT_ID -u HYQS_GOOGLE_CLIENT_SECRET \
        uv run python - <<'PY'
from hyqs.config import Config

config = Config.from_env()
print(config.resolved_mcp_resource_url())
print(config.web_port)
print(int(bool(config.google_client_id and config.google_client_secret)))
PY
)
MCP_URL="${MCP_CONFIG[0]}"
MCP_PUBLIC=1
if [[ -z "${MCP_URL}" ]]; then
    MCP_PUBLIC=0
    MCP_URL="http://127.0.0.1:${MCP_CONFIG[1]}/mcp"
fi
GOOGLE_CONFIGURED="${MCP_CONFIG[2]}"

echo
echo "MCP (agent client) access — this is how an agent client drives the job queue:"
if [[ "${MCP_PUBLIC}" -eq 1 ]]; then
    echo "  Endpoint: ${MCP_URL}"
else
    echo "  Endpoint: reachable only through the SSH tunnel above (no public URL"
    echo "            without --domain) — use the localhost form:"
    echo "            ${MCP_URL}"
fi
if [[ "${GOOGLE_CONFIGURED}" -eq 1 ]]; then
    echo "  Auth: Google sign-in and project-scoped API tokens both work."
    if [[ "${MCP_PUBLIC}" -eq 1 ]]; then
        echo "  Google redirect URI to register (client type must be 'Web application'):"
        echo "    ${MCP_URL}/auth/callback"
    fi
else
    echo "  Auth: only project-scoped API tokens will authenticate. Google"
    echo "        sign-in is NOT configured (HYQS_GOOGLE_CLIENT_ID /"
    echo "        HYQS_GOOGLE_CLIENT_SECRET are empty in .env), so interactive"
    echo "        sign-in will not work until you set them."
fi
echo "  Mint a project-scoped API token: POST /api/projects/{project_id}/tokens"
echo "    body: {\"name\": \"<label>\", \"role\": \"<member-role>\"}"
echo "  Register with Claude Code:"
echo "    claude mcp add --transport http hyqs ${MCP_URL} \\"
echo "      --header \"Authorization: Bearer <api-token>\""
echo "    (omit --header, then run /mcp in Claude Code, for interactive Google"
echo "    sign-in instead — only if Google is configured, above)"
echo
echo "  Ordering trap: API tokens are PROJECT-scoped, so none can exist until"
echo "  at least one project exists. On a brand-new install with no Google"
echo "  credentials configured, there is therefore no way to authenticate to"
echo "  MCP until you create a project through the web console first."
echo "  See deploy/README.md's 'MCP endpoint' section for full details."

cat <<EOF

Still to do — Hyqs cannot build anything until you authenticate an agent:

  1. Authenticate Claude (interactive, once):
         claude
     Or use an API key instead: set ANTHROPIC_API_KEY in .env.

  2. Back up HYQS_CREDENTIAL_ENCRYPTION_KEY from .env. Lose it and every
     stored third-party credential becomes unrecoverable.

  3. Point a project at a git repo in the console, then queue a job.

  4. Optional — to give each project its own subdomain (acme.example.com),
     set HYQS_NGINX_DOMAINS in .env. That needs a WILDCARD certificate and a
     matching /etc/nginx/snippets/<label>-ssl.conf, which this installer does
     not provision (it issues a single-name certificate for the console only).
     See deploy/README.md. Left unset, per-project vhost registration fails
     closed rather than writing a config nginx would reject.

Operate:
  journalctl --user -u 'hyqs-web@*' -f        # console logs
  journalctl --user -u 'hyqs-pipeline@*' -f   # pipeline logs
  bash deploy/release.sh                      # rebuild + zero-downtime restart

EOF
if [[ "${DOCKER_RELOGIN_NEEDED:-0}" -eq 1 ]]; then
    echo "NOTE: this session cannot reach Docker directly (the group applies only"
    echo "      to a NEW login). The install used \`sg docker\` to work around it."
    echo "      Log out and back in before running docker commands by hand."
    echo
fi
