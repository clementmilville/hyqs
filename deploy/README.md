# Deploy — run Hyqs 24/7

A `systemd --user` service that keeps Hyqs running and auto-restarts it on
crash or reboot. Runs as **your** user (no root), so it uses your logged-in
`claude` CLI subscription and your `.env`.

## Prerequisite: the shared Postgres

The build pipeline coordinates through a **shared Postgres** — it's the plane
every worker (on every host) claims/leases jobs from. The chat daemon, the web
console, and each `hyqs-pipeline` worker all need `HYQS_DB_URL` pointing at it.

Bring one up locally with the bundled compose file (data persists in a named
volume, so recreating the container keeps your projects/jobs/usage):

```bash
docker compose -f deploy/docker-compose.yml up -d
echo 'HYQS_DB_URL=postgresql://hyqs:hyqs@localhost:5432/hyqs' >> .env
```

Migrating from the old local `pipeline.db`? Carry the history across once:

```bash
uv run hyqs-migrate-sqlite        # reads <data_dir>/pipeline.db + HYQS_DB_URL
```

## Install

```bash
# 1. From the repo root, make sure it runs by hand first:
uv sync && uv run hyqs        # Ctrl-C once you see "Hyqs is live"

# 2. Edit deploy/hyqs.service — set WorkingDirectory to your checkout's
#    absolute path (default assumes ~/hyqs-ai). Confirm `which uv`.

# 3. Install the unit and start it:
mkdir -p ~/.config/systemd/user
cp deploy/hyqs.service ~/.config/systemd/user/hyqs.service
systemctl --user daemon-reload
systemctl --user enable --now hyqs
```

## Survive logout / reboot

User services normally stop when you log out. Enable **lingering** so Hyqs
keeps running 24/7 without an active session:

```bash
sudo loginctl enable-linger "$USER"
```

## Operate

```bash
systemctl --user status hyqs      # is it up?
journalctl --user -u hyqs -f      # live logs
systemctl --user restart hyqs     # after pulling new code
systemctl --user stop hyqs        # stop it
systemctl --user disable hyqs     # don't start on boot
```

After deploying new code, `git pull && systemctl --user restart hyqs`.

## Optional: run the pipeline as its own service

By default the `hyqs` daemon runs everything (chat + pipeline + web) in one
process. To run the autonomous `/build` pipeline as a **separate** process — so
it survives independently of the chat layer — split it out:

```bash
# 1. Tell the chat daemon NOT to run its own runner (avoid two on one db):
echo 'HYQS_PIPELINE_DISABLED=1' >> .env
systemctl --user restart hyqs

# 2. Install + start the pipeline worker:
cp deploy/hyqs-pipeline.service ~/.config/systemd/user/hyqs-pipeline.service
systemctl --user daemon-reload
systemctl --user enable --now hyqs-pipeline
journalctl --user -u hyqs-pipeline -f
```

Both services connect to the same `HYQS_DB_URL`, so `/build` jobs queued from
chat are picked up by the worker.

## Zero-downtime pipeline deploys

`deploy/release.sh` replaces the pipeline worker with an overlapping-fleet
(blue-green) swap instead of a same-unit restart: it starts a fresh
`hyqs-pipeline@<generation>` instance on the new code, then waits (up to 90s)
for that instance to write a fresh heartbeat row into the shared `workers`
table (via `hyqs-pipeline --healthcheck --pid <pids>`) — proving it has
actually come up and claimed/heartbeat-ed as a live worker, not just that the
OS process exists. `<pids>` is a comma-separated set covering the unit's
whole cgroup (its `MainPID` — the `uv run` wrapper — plus any children),
because the heartbeat is written by the `hyqs-pipeline` python child process,
not the wrapper itself; if the cgroup can't be read, it falls back to
`MainPID` alone. Only once the heartbeat is confirmed does it ask each old
instance to stop. If the new instance never proves itself, release.sh stops
it and leaves the old fleet serving, exiting non-zero so the deploy stage
reports failure. `hyqs-web` keeps a simple restart — it's effectively
stateless, so blue-green only pays for the pipeline, where an in-flight stage
can run tens of minutes.

As of job #627, the worker's SIGTERM handling changed to make this safe:
the first SIGTERM a worker receives (what `systemctl stop`/`restart` sends)
now triggers a graceful drain with a long ceiling
(`HYQS_PIPELINE_DEPLOY_DRAIN_TIMEOUT`, default 2400s/40min) instead of an
immediate kill — the worker stops claiming new jobs but lets whatever stage
it's mid-run finish. A second SIGTERM (or SIGINT) still forces an immediate
stop. `SIGUSR1` remains the shorter manual/operator drain
(`HYQS_PIPELINE_DRAIN_TIMEOUT`, default 300s).

This means during a release, **old and new pipeline code run concurrently**
for up to `HYQS_PIPELINE_DEPLOY_DRAIN_TIMEOUT` while the old fleet finishes
its in-flight stages. Two consequences for anyone changing schema or stage
contracts in the same release:

- **Migrations must stay additive-only** (`ADD COLUMN IF NOT EXISTS`, new
  tables — never `DROP`/`RENAME`/type-change a column the old code still
  reads or writes). The migration-at-boot guard (hash-skip + `lock_timeout`,
  job #625) makes it safe for the new instance to run its migration while the
  old fleet still holds live transactions, but only if the migration itself
  doesn't break what the old code expects.
- **Stage semantics in-flight jobs depend on must not change incompatibly**
  within one release — schema.json shapes, `Stage` enum values, and
  `contracts.parse_and_validate` expectations. A breaking change needs a
  two-release rollout: add the new shape/value first (old code ignores it),
  then switch stages to require it in a later release, then remove the old
  path once no in-flight job can still be on it.

See CONVENTIONS.md's "Adding a Store Method" section for the store-side
conventions (dataclasses only, no raw SQL outside `store.py`) that make
additive migrations straightforward to write.

## Run multiple workers (parallel builds)

Workers coordinate through the shared Postgres — an atomic claim (a transaction
advisory lock + lease/heartbeat) means many of them, in one process, *across
processes*, **and across hosts**, can advance different jobs at once without
colliding. Two ways to scale, which compose:

```bash
# A) Several jobs per process (in-process async; stages are all I/O-bound):
echo 'HYQS_PIPELINE_CONCURRENCY=3' >> .env

# B) Several worker processes, via the templated unit:
cp deploy/hyqs-pipeline@.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hyqs-pipeline@1 hyqs-pipeline@2 hyqs-pipeline@3
journalctl --user -u 'hyqs-pipeline@*' -f
```

You no longer need `HYQS_PIPELINE_DISABLED` for correctness (the chat daemon's
runner is just one more safe worker), though keeping the pipeline split out is
still cleaner operationally. **How many jobs actually run in parallel is capped
per project by its agent roster** (each agent's max-concurrency), edited in the
web UI — the worker pool just sets the ceiling.

Different providers run independently: if Claude hits its rate limit, only
Claude-routed work pauses while Codex-routed jobs and the deterministic
test/merge steps keep flowing.

## Run workers on multiple hosts

Because coordination lives in Postgres rather than a local file, scaling past one
machine needs no broker — just point each host's worker at the same database:

```bash
# On every worker host (same checkout, same .env with the shared HYQS_DB_URL):
git clone … && cd hyqs-ai && uv sync
echo 'HYQS_DB_URL=postgresql://hyqs:STRONGPW@db-host:5432/hyqs?sslmode=require' >> .env
cp deploy/hyqs-pipeline@.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hyqs-pipeline@1 hyqs-pipeline@2
```

Each worker self-registers in the `workers` table, so they all show up live in
the web **fleet / C2 view** with their host, current job, and stage. Nothing
elects a leader; the claim is symmetric.

> Provisioning/starting workers on remote hosts *from the web UI* is not built
> yet — that needs a small per-host agent. For now, bring workers up per host via
> systemd as above; they appear in the fleet automatically.

### Network & security

Exposing Postgres to other hosts widens the trust boundary — treat it like any
shared datastore:

- Give the `hyqs` role a **strong password** (the compose default `hyqs:hyqs` is
  local-dev only) and reach it over TLS (`sslmode=require`).
- Expose `5432` only to the worker hosts (firewall / private network / VPN), not
  the public internet.
- The `docker compose` Postgres persists in the `hyqs_pgdata` volume; back it up
  like the production datastore it now is.

## Standalone deploy hosts (host-pinned deployer)

Every worker above (`hyqs-pipeline`, `hyqs-pipeline@N`) can run any stage,
including DEPLOY, against whichever host it happens to live on. For a
multi-environment fleet you often want the *opposite*: an environment's
DEPLOY-stage jobs pinned to run on that environment's actual host, and
nowhere else. That's what `hosts`/`environments.host_id`
(`hyqs/pipeline/hosts.py`, `hyqs/pipeline/store.py`'s `set_environment_host`)
and `hyqs-pipeline --deployer` (`hyqs/pipeline/__main__.py`) are for:

- A **host** is enrolled once via an ed25519 keypair (`hosts.enroll_host`) —
  the private key never leaves the host it was generated on.
- An **environment** row (`environments.host_id`) can be pinned to one
  enrolled host. `JobStore.claim`/`claim_fastpath` then skip that
  environment's DEPLOY-stage jobs for any worker whose own enrolled host
  doesn't match (`JobStore._deploy_stage_host_id`) — unpinned environments
  (`host_id IS NULL`) keep today's any-worker behavior.
- `hyqs-pipeline --deployer` is a **constrained** worker: it only ever claims
  the DEPLOY stage (`stage_allowlist={Stage.DEPLOY}`), refuses to start
  without `HYQS_HOST_NAME` set, and never touches plan/build/fix/review — so
  it needs no AI provider credential (`ANTHROPIC_API_KEY`, `CODEX_*`, etc.).

### Required environment

The deployer only needs three things — no AI credentials, ever:

| Var | Purpose |
|---|---|
| `HYQS_DB_URL` | The shared control-plane Postgres (same DSN as every other worker above) — where jobs are claimed/leased from and where `hosts`/`environments` live. |
| `HYQS_HOST_NAME` | This host's enrolled name (must match the `--name` passed to `enroll`, below). Pins DEPLOY-stage claims to this host's environment(s) and makes `--deployer` refuse to start without it. |
| registry pull credential | Only needed if you *pull* the deployer image instead of building it locally — see "Container registry" below for where the `pull` account's credential lives (`docker login`, not baked into the image). |

### Enroll → configure → check status (CLI flow)

`hyqs-pipeline enroll`/`status` are pure local operations plus one
control-plane write (`hosts.enroll_host`) — they never touch AI providers:

```bash
# Generates an ed25519 keypair, writes the private key under
# <data_dir>/deployer/host_key.pem (chmod 600), and registers the public key
# in the hosts table. Re-running with the same name is idempotent — it never
# silently rotates an existing key (hosts.enroll_host).
uv run hyqs-pipeline enroll --name my-edge-host

# Prints this host's local identity/state (host name, key path, last
# claim/deploy) — pure file I/O, no DB round-trip.
uv run hyqs-pipeline status
```

Point an environment at this host by setting `HYQS_HOST_NAME=my-edge-host`
for the deployer process below — the same name given to `enroll`. (Wiring an
`environments.host_id` to the enrolled host row is a store-level primitive
today — `JobStore.set_environment_host` — with no CLI/web surface yet; that's
follow-up work, not part of standing up the deployer host itself.)

### Install as a container + systemd unit

`deploy/deployer.Dockerfile` builds a minimal image: the `docker` CLI
(against the *host's* docker.sock, no daemon runs inside the container) +
`git` + the installed `hyqs` package — no project source baked in, no AI
credentials declared or required.

```bash
# 1. Build the image (or pull one from your registry — see "Container
#    registry" below for the pull credential):
docker build -f deploy/deployer.Dockerfile -t hyqs-deployer:latest .

# 2. Enroll by hand once, using the image itself, so the enrollment key and
#    host_config.json land in the persistent data dir the systemd unit will
#    reuse:
mkdir -p ~/.hyqs/deployer-data
docker run --rm \
  -e HYQS_DB_URL=postgresql://hyqs:hyqs@localhost:5432/hyqs \
  -e HYQS_DATA_DIR=/data \
  -v ~/.hyqs/deployer-data:/data \
  --entrypoint /app/.venv/bin/hyqs-pipeline \
  hyqs-deployer:latest enroll --name my-edge-host

# 3. Edit deploy/hyqs-deployer.service's "EDIT ME" block — set
#    HYQS_DEPLOYER_IMAGE, HYQS_DB_URL, and HYQS_HOST_NAME (the same name used
#    to enroll above) — then install it:
mkdir -p ~/.config/systemd/user
cp deploy/hyqs-deployer.service ~/.config/systemd/user/hyqs-deployer.service
systemctl --user daemon-reload
systemctl --user enable --now hyqs-deployer
journalctl --user -u hyqs-deployer -f
```

The unit runs `docker run --rm` in the foreground (not `-d`), mounting
`~/.hyqs/deployer-data` (the enrollment key + deployer state) and
`/var/run/docker.sock` (so the DEPLOY stage can build/run containers on this
host) into the container — see `deploy/hyqs-deployer.service` for the exact
flags. It shares `hyqs-pipeline.service`'s graceful-drain signal handling
(§"Zero-downtime pipeline deploys" above): the first `systemctl stop`/restart
sends SIGTERM, which the containerized process treats as a long graceful
drain rather than an immediate kill.

### Client-host onboarding (publish → install)

The manual "Install as a container + systemd unit" steps above work for a
host you administer directly. For a **client's** host — someone else's
machine, onboarded from a distance — Epic 81 adds two deterministic (no AI)
scripts that wrap the same primitives into a repeatable flow: build once,
publish once, hand the client a script + a pinned digest, they run it.

**v1 scope: CONNECTED tier only** — the client host reaches our coordination
Postgres and registry directly over the network. Air-gapped/egress-only
installs and per-host-scoped pull credentials (vs. today's one shared `pull`
account) are documented here as **FUTURE work, not built**; both scripts
leave `# TODO(FUTURE, not built here):` markers at the point they'd need to
change.

**1. Build host — publish once, whenever the deployer image changes:**

```bash
HYQS_REGISTRY_PUSH_ENABLED=1 HYQS_REGISTRY_DOMAIN=registry.example.com \
HYQS_REGISTRY_PUSH_USER=push HYQS_REGISTRY_PUSH_PASS='<strong-push-password>' \
HYQS_COSIGN_KEY_PATH=~/.hyqs/keys/cosign.key \
bash deploy/publish-deployer.sh
```

Idempotent — safe to re-run any time the image changes. It fails fast (no
`docker build`, no network) if any `HYQS_REGISTRY_PUSH_*`/`HYQS_COSIGN_KEY_PATH`
var is unset, printing exactly which ones. Otherwise it builds
`deploy/deployer.Dockerfile`, then pushes + cosign-signs it via
`hyqs.pipeline.registry_push.push_and_sign` (the exact same primitive used
elsewhere — this script never re-implements the docker/cosign subprocess
calls itself), and prints a summary block with `--image-ref`,
`--image-digest`, and the cosign public key path
(`HYQS_COSIGN_PUB_PATH`, defaulting to `HYQS_COSIGN_KEY_PATH` with `.key` →
`.pub`) to hand to the client.

**2. Hand off:** paste the printed public key's contents into
`deploy/install-deployer.sh`'s `--- EDIT ME ---` block (the one hard trust
anchor — pinned inline in the script, never fetched over the network), then
send the client operator the edited script plus the printed `--image-ref`
and `--image-digest`.

**3. Client host — the operator runs (and reads) the script themselves:**

```bash
HYQS_DB_URL=postgresql://hyqs:STRONGPW@db.example.com:5432/hyqs \
HYQS_REGISTRY_PULL_USER=pull HYQS_REGISTRY_PULL_PASS='<strong-pull-password>' \
bash deploy/install-deployer.sh --name my-edge-host \
  --image-ref registry.example.com/hyqs-deployer/app \
  --image-digest sha256:...
```

Optional flags: `--data-dir` (default `~/.hyqs/deployer-data`), `--secrets-dir`
(default `~/.hyqs/deployer-secrets`). Missing/invalid required flags or env
(`--name`/`--image-ref`/`--image-digest`/`HYQS_DB_URL`) print usage and exit
non-zero before touching docker, cosign, curl, or systemd. The script is
deliberately minimal and auditable (no telemetry, no undocumented network
calls) and idempotent end to end:

- **Preflight** — confirms `docker`/`cosign`/`systemctl`/`curl` are present,
  the docker daemon and a `systemd --user` session are reachable, and that
  this host can reach both `HYQS_DB_URL`'s host:port (a short `/dev/tcp`
  probe) and the registry domain parsed from `--image-ref` (an HTTPS probe
  accepting any 2xx/401/403). Any failure here names what's unreachable and
  notes that air-gapped installs are a documented future mode, not this one.
- **Bootstrap trust** — pulls `<image-ref>@<image-digest>` **strictly by
  digest** (never a mutable tag), then `cosign verify`s it against the
  pinned public key **before running anything else**; a verify failure
  aborts with nothing further executed. Only then does it `docker login`
  with the pull-only credential (`HYQS_REGISTRY_PULL_USER`/`_PASS` — v1
  uses one shared `pull` account for every client host).
- **Enroll** — runs the image's own `enroll --name <name>` (reusing #1781's
  `hyqs-pipeline enroll` exactly; the private key never leaves the host) the
  first time only, gated on `<data-dir>/deployer/host_config.json` not
  already existing — re-running never regenerates the keypair.
  See "Enroll → configure → check status" above for what that subcommand
  does under the hood.
- **Local secret store** — creates `<secrets-dir>` `chmod 700` (idempotent,
  never overwrites existing files) for the file-based `SecretProvider`
  (#1748) to read from. This script **never sets secret values** — that's
  the client's own responsibility. Required secrets are enforced
  **fail-closed at apply time** via
  `hyqs.pipeline.secrets_contract.find_missing_secrets`: a deploy refuses to
  proceed if a required secret is missing.
- **Install + start** — renders `deploy/hyqs-deployer.service` (substituting
  the pinned image ref@digest, `HYQS_HOST_NAME`, `HYQS_DB_URL`, and the
  data-dir mount path) into `~/.config/systemd/user/hyqs-deployer.service`
  every run — this is the "upgrade the pinned image" step on a re-run —
  enables lingering, and does `daemon-reload && enable --now`. See "Install
  as a container + systemd unit" above for what the unit itself does.
- **Verify** — checks the deployer reports healthy status and that the
  systemd unit is active, exiting non-zero with a clear message if either
  check fails.

Re-running the whole script end to end (e.g. after a new `publish-deployer.sh`
digest) is always safe: it never deletes/regenerates `host_key.pem` and never
touches existing files under `--secrets-dir`.

## MCP endpoint

Hyqs serves an MCP endpoint at `<HYQS_WEB_BASE_URL>/mcp` — this is how an
agent client (Claude Code, or anything else that speaks MCP) drives the job
queue: list/create jobs, watch them run, read diffs, and more (see
`docs/mcp.md` for the tool list). `bash deploy/install.sh`'s closing summary
prints the exact endpoint, active auth mode, and registration commands for
your install; this section is the durable reference since that summary
scrolls away.

**Endpoint URL.** Resolved the same way
`hyqs.config.Config.resolved_mcp_resource_url` does: an explicit
`HYQS_MCP_RESOURCE_URL` wins, otherwise it's `<HYQS_WEB_BASE_URL>/mcp`. A
loopback-only install (no `--domain`, no public base URL) has no resolvable
public URL — MCP is reachable only through the same SSH tunnel used to reach
the console, at the localhost form: `http://127.0.0.1:8787/mcp`.

**Authentication.** `hyqs.web.mcp_oauth.build_mcp_auth_provider` composes two
credential types, and which ones actually work depends on configuration:

- **Google OIDC sign-in** — only functional when both `HYQS_GOOGLE_CLIENT_ID`
  and `HYQS_GOOGLE_CLIENT_SECRET` are set. Without a public MCP URL to serve
  the OAuth redirect from, or with either variable empty, Google sign-in
  cannot work and the server falls back silently to API-token-only auth.
  Register `<endpoint-base>/mcp/auth/callback` (e.g.
  `https://hyqs.example.com/mcp/auth/callback`) as the redirect URI in the
  Google Cloud Console, and the OAuth client must be of **Web application**
  type (not "Desktop" or "Installed").
- **Project-scoped API tokens** — always available, and the only option when
  Google credentials aren't configured. Mint one with:

  ```
  POST /api/projects/{project_id}/tokens
  {"name": "<label>", "role": "<member-role>"}
  ```

  (requires the `manage_api_tokens` permission on that project; the response
  body's `token` field is the bearer secret — it is shown once).

**Ordering trap.** API tokens are **project-scoped**, so none can exist
until at least one project exists. On a brand-new install with no Google
credentials configured, there is therefore no way to authenticate to MCP
until a project has been created through the web console first.

**Registering a client.** Verified against the current Claude Code docs —
use `claude mcp add`, not a hand-edited config file:

```bash
# Non-interactive / automation client, authenticating with an API token:
claude mcp add --transport http hyqs <endpoint-url> \
  --header "Authorization: Bearer <api-token>"

# Interactive client, signing in with Google (only if configured above):
claude mcp add --transport http hyqs <endpoint-url>
# then, inside Claude Code:
/mcp
```

## Notes

- The service reads `.env` from `WorkingDirectory` (the app loads it via
  python-dotenv on startup), so keep your `.env` in the repo root.
- For unattended operation, review `HYQS_PERMISSION_MODE` — `bypassPermissions`
  lets the agent run shell/file ops with no human in the loop.
- Alongside `secrets.schema.json` (declares which secrets a release needs, checked
  against the `SecretProvider` before a `pull_deploy` cutover), a **project repo**
  can opt into a `deploy/required-env` manifest: one entry per line, either `VAR`
  (checked on the primary app container) or `service:VAR` (checked on that compose
  service's container); `#` comments and blank lines are ignored. Absent file =
  complete no-op. Unlike the secrets contract, this is a deterministic
  *container-env inspection* gate (`docker inspect`/`docker compose ps` +
  `.Config.Env`), not app-code parsing — it runs after the probe/services are
  confirmed up and, on any declared var missing or empty, aborts the cutover
  (single-container) or fails the deploy job (compose), naming exactly which var
  is missing on which service. This catches both failure modes behind the
  recurring "silent missing env var" incidents (#1808/#1814): a var left unset in
  `.env`/host secrets, and a var set in `.env` but never mapped into
  `docker-compose.yml`'s service `environment:` block.

## GeoIP database

Some features resolve IP addresses to a rough geographic location using a
local MaxMind-format database — no external lookup service, no keys.

`HYQS_GEOIP_DB` points at that database file. It defaults to
`<HYQS_DATA_DIR>/geoip/dbip-city-lite.mmdb` when unset. Populate it with:

```bash
bash scripts/fetch_geoip_db.sh
```

This downloads the current month's [DB-IP](https://db-ip.com) City Lite
database (`dbip-city-lite-YYYY-MM.mmdb.gz`), decompresses it, and writes it to
the resolved destination (a positional argument, `$HYQS_GEOIP_DB`, or the
default path above). Run it monthly (e.g. via cron) to keep the database
current, since DB-IP publishes a new City Lite snapshot each month.

**Attribution requirement:** DB-IP City Lite is distributed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Any product using
this database must visibly credit DB-IP — e.g. "IP Geolocation by DB-IP"
linking to <https://db-ip.com>.

## Request body size limits

`HYQS_MAX_REQUEST_BODY_BYTES` (default 25 MiB) bounds any single request body
that reaches the app. `hyqs.web.app.MaxRequestBodySize` — the outermost user
middleware, ahead of auth — rejects an over-cap body with HTTP 413 before it
is buffered into memory: a declared `Content-Length` over the cap is rejected
immediately, and a chunked body with no `Content-Length` is cut off once its
running byte count exceeds the cap. This guards the host, which also runs
Postgres and every deployed container, against an OOM triggered by a huge
upload to a route that needs no auth (e.g. the public page-view beacon).

Fresh installs via `bash deploy/install.sh --domain ...` already write
`client_max_body_size 25m;` into the generated nginx vhost (matching the app
default), so oversized requests are rejected at the edge before they ever
reach the app.

If you're running a hand-configured or pre-existing nginx vhost that
`install.sh` didn't generate, this repo has no access to edit that live
`/etc/nginx` config for you — add the directive yourself:

```nginx
server {
    ...
    client_max_body_size 25m;  # match HYQS_MAX_REQUEST_BODY_BYTES
}
```

then reload nginx:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

## Container registry (Zot)

Foundation for multi-environment deployment (Epic 81): a self-hosted OCI
registry that holds build-once artifact images, promoted across environments
by pulling a known digest rather than rebuilding per environment. This is
pure infrastructure — no `hyqs/pipeline/` code depends on it yet.

### 1. Bring the registry up

```bash
docker compose --env-file .env -f deploy/docker-compose.registry.yml up -d
```

Required env vars in `.env` (no defaults — compose refuses to start without
them, same as `POSTGRES_PASSWORD` above):

```bash
# Absolute path OUTSIDE the repo — registry blobs are never committed.
HYQS_REGISTRY_DATA_DIR=/home/youruser/.hyqs/data/registry
# htpasswd file holding both the push and pull credential classes (below).
HYQS_REGISTRY_HTPASSWD=/home/youruser/.hyqs/registry.htpasswd
# Loopback-only port nginx will proxy to. Defaults to 5100 if unset.
HYQS_REGISTRY_PORT=5100
```

### 2. Generate the cosign keypair

Zot stores cosign signatures natively via the OCI Referrers API
(`extensions.search.enable` in `deploy/zot-config.json`) — no extra config
needed. Generate the keypair **on the build host**:

```bash
mkdir -p ~/.hyqs/keys
cd ~/.hyqs/keys
cosign generate-key-pair
```

- `cosign.key` is the **private** signing key. It must live only on the build
  host, outside the repo (e.g. `~/.hyqs/keys/`), and must never be committed —
  add the directory to that host's global gitignore/backup exclusions if it
  isn't already covered.
- `cosign.pub` is the public verification key — safe to distribute to any
  host/CI job that verifies signatures.

### 3. Create the push and pull credentials

One htpasswd file (bcrypt hashes, per `deploy/zot-config.json`'s
`accessControl.repositories."**".policies`), two accounts — a `push` user
(the build host, full read/write) and a `pull` user (deployers, read-only):

```bash
htpasswd -Bbn push '<strong-push-password>'  > "${HYQS_REGISTRY_HTPASSWD}"
htpasswd -Bbn pull '<strong-pull-password>' >> "${HYQS_REGISTRY_HTPASSWD}"
```

Re-run `docker compose --env-file .env -f deploy/docker-compose.registry.yml up -d`
after editing the htpasswd file so Zot picks up the change.

### 4. Install the nginx vhost

```bash
sudo bash deploy/setup-nginx-registry.sh registry.example.com 5100
```

Requires `deploy/setup-nginx-hyqs.sh` to have been run first (it provisions
the shared `/etc/nginx/hyqs.d/` include dir). Like the per-project vhosts
`hyqs/pipeline/nginx_sites.py` writes, this is dual-stack IPv6 and validates
with `nginx -t` before and after reloading, rolling back the file if
validation ever fails.

### 5. Smoke-check

Verifies the registry is reachable, that the push account can push, and that
the pull-only account can pull the result back **by digest** (proving it can
read but was never used to write):

```bash
REGISTRY_DOMAIN=registry.example.com \
REGISTRY_PUSH_USER=push REGISTRY_PUSH_PASS='<strong-push-password>' \
REGISTRY_PULL_USER=pull REGISTRY_PULL_PASS='<strong-pull-password>' \
bash deploy/registry-smoke-check.sh
```
