"""Runtime configuration, loaded from environment / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _as_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    """Read an int from the environment, treating set-but-empty as unset.

    `.env.example` documents optional settings as bare `NAME=` placeholders, and
    `load_dotenv` puts those in the environment as "". A plain
    `int(os.environ.get(NAME, "8"))` never sees its own default in that case and
    dies with `invalid literal for int() with base 10: ''` — naming no variable,
    before logging is even configured. That crash-looped hyqs-web on a fresh
    install whose .env was copied from .env.example.

    A non-empty but unparseable value is a real misconfiguration and still
    raises, but says which variable and what it contained.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None


def _env_float(name: str, default: float) -> float:
    """Float counterpart of :func:`_env_int`; same empty-means-unset rule."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None


@dataclass(slots=True)
class Config:
    model: str
    permission_mode: str
    data_dir: Path = field(default_factory=lambda: Path.home() / ".hyqs" / "data")
    # Shared Postgres coordinating the pipeline across hosts. The pipeline JobStore
    # lives here (chat MemoryStore stays a local sqlite file under data_dir).
    db_url: str = ""
    # --- pipeline ---
    pipeline_model: str = ""  # falls back to `model` if empty
    default_repo: str = ""  # default target repo for /build
    git_push: bool = False  # push to origin after merge (off by default)
    pipeline_max_attempts: int = 5  # fix→retest→re-review rounds before giving up
    pipeline_stage_timeout: int = 1800  # seconds; hard cap on a single AI stage
    pipeline_max_timeouts: int = (
        2  # stage-timeout retries before failing the job (caps the reclaim loop)
    )
    pipeline_limit_backoff: int = 600  # seconds to pause when rate-limited w/o a reset time
    pipeline_enabled: bool = (
        True  # run the pipeline runner in-process (off => use the standalone worker)
    )
    # Manual/operator drain (SIGUSR1): stop claiming new jobs, give in-flight
    # stages this long to finish before force-cancelling.
    pipeline_drain_timeout: int = 300
    # Deploy-driven drain (first SIGTERM, e.g. from `systemctl stop`/restart
    # during a self-deploy): a long ceiling so a mid-flight build/fix stage
    # survives an overlapping-fleet release instead of being killed.
    pipeline_deploy_drain_timeout: int = 2400
    # --- multi-worker ---
    pipeline_concurrency: int = 1  # jobs this process runs in parallel (run more processes too)
    pipeline_lease_ttl: int = 90  # seconds; a stale lease means the worker died
    pipeline_merge_lock_ttl: int = 300  # seconds; safety net for a crashed merge holder
    pipeline_schema_lock_ttl: int = 300  # seconds; safety net for a crashed schema-lock holder
    pipeline_deploy_cmd: str = (
        ""  # shell command to run after merge; overridden by deploy/release.sh
    )
    pipeline_auto_deploy: bool = False  # enable the supervisor auto-deploy poller
    pipeline_auto_deploy_interval: int = 120  # seconds between auto-deploy scans
    pipeline_auto_deploy_max_attempts: int = (
        3  # cap on auto-deploy jobs filed per (project, unchanged origin tip)
    )
    pipeline_job_stale_hours: float = 24  # wall-clock age before an active job alerts
    # This worker's enrolled host identity (hosts.name), used to pin deploy-stage
    # jobs to a matching-host worker. "" => the local/default host — unchanged,
    # unpinned claim behavior (today's single-host setup).
    pipeline_host_name: str = ""
    # AI incident analyst diagnosis model — defaults to the same strong model the
    # pipeline builders use (see `model`'s own "sonnet" default), since diagnosis
    # is a harder reasoning task than implementation and shouldn't run on the
    # cheapest model. Costs more per invocation than a small model; the
    # confidence threshold, per-job diagnosis cap, remediation depth ceiling,
    # requeue ceiling, and provider-pause skip in supervisor.py bound the total
    # spend, not the model choice.
    pipeline_incident_analyst_model: str = "sonnet"
    # ``auto`` prefers Claude but falls back to any healthy enrolled provider.
    # This keeps supervisor recovery available while one provider's circuit
    # breaker is open. Set an explicit provider to pin it operationally.
    pipeline_incident_analyst_provider: str = "auto"
    # Frequency ceilings for the AI incident analyst: safety margins, not the sole
    # cost bound. The per-scan value times the janitor's scan cadence (see
    # _janitor_loop in supervisor.py) sets the worst-case rate of analyst
    # invocations an operator should reason about when tuning cost.
    pipeline_incident_analyst_scan_budget: int = (
        8  # max analyst diagnoses run within one janitor scan pass
    )
    pipeline_incident_analyst_job_cap: int = 6  # max times any single job may ever be AI-diagnosed
    # Consult the AI incident analyst once before a deterministic remediation path
    # (stale-branch, transient, env-deploy, dependency-blocked) dead-letters a job,
    # so operators can disable just this new path independently of the existing
    # judgment-class analyst consult.
    pipeline_incident_analyst_predeadletter: bool = True
    # --- codex provider ---
    codex_bin: str = "codex"  # path to the Codex CLI
    codex_model: str = ""  # "" => the CLI's default model
    codex_extra_args: str = ""  # "" => "exec --full-auto"
    projects_dir: Path = field(
        default_factory=lambda: Path("~/projects").expanduser()
    )  # where new project repos are provisioned
    # GitHub org to create new project repos under (`gh repo create <org>/<slug>`).
    # "" => today's behavior: the authenticated gh account's personal namespace.
    github_org: str = ""
    # MaxMind-format (DB-IP City Lite) GeoIP database path. Populated by
    # scripts/fetch_geoip_db.sh; defaults under data_dir so a fresh install has
    # a sane, writable location before anyone runs the fetch script.
    geoip_db: Path = field(
        default_factory=lambda: Path.home() / ".hyqs" / "data" / "geoip" / "dbip-city-lite.mmdb"
    )
    # --- logging ---
    log_format: str = "json"  # "json" (structured, shippable) or "console" (human-readable)
    # --- web ui ---
    web_host: str = "0.0.0.0"
    web_port: int = 8787
    web_enabled: bool = True
    web_token: str = ""  # bearer token guarding /api/*
    # Number of trusted reverse-proxy hops in front of this process (e.g. the
    # single nginx hop in front of hyqs-web today).
    trusted_proxy_depth: int = 1
    # Global ceiling on any single request body, enforced by
    # hyqs.web.app.MaxRequestBodySize before a body is buffered into memory —
    # the host also runs Postgres and every deployed container, so an
    # unbounded body on an unauthenticated route (e.g. the page-view beacon)
    # is a full-host OOM risk. Must stay comfortably above the largest
    # legitimate payload (the chat image-upload path's own 5MB base64 cap) and
    # should track the `client_max_body_size` value deploy/install.sh writes
    # into the generated nginx vhost, so the edge and the app agree.
    max_request_body_bytes: int = 25 * 1024 * 1024
    mcp_resource_url: str = (
        ""  # public URL of the /mcp endpoint (e.g. https://hyqs.example.com/mcp)
    )
    # Public base URL of the web console (e.g. https://hyqs.example.com), used to
    # build outbound links like Slack job links. Distinct from mcp_resource_url,
    # which is /mcp-specific.
    web_base_url: str = ""
    # --- OAuth providers ---
    google_client_id: str = ""
    google_client_secret: str = ""
    apple_client_id: str = ""
    apple_team_id: str = ""
    apple_key_id: str = ""
    apple_private_key: str = ""  # PEM-encoded EC private key for Sign In With Apple

    def resolved_mcp_resource_url(self) -> str:
        """Public URL of the ``/mcp`` endpoint, or "" when it cannot be known.

        An explicit ``HYQS_MCP_RESOURCE_URL`` wins. Otherwise it is derived from
        the console's public base URL, since in every normal deployment the MCP
        endpoint is mounted on that same host — which means one setting, not two,
        has to be right. Returns "" when neither is configured; callers decide
        whether that is fatal, because there is no safe hostname to guess.
        """
        if self.mcp_resource_url:
            return self.mcp_resource_url
        if self.web_base_url:
            return self.web_base_url.rstrip("/") + "/mcp"
        return ""

    @classmethod
    def from_env(cls) -> "Config":
        data_dir = Path(
            os.environ.get("HYQS_DATA_DIR", str(Path.home() / ".hyqs" / "data"))
        ).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        # HYQS_DB_URL (preferred) or DATABASE_URL — the shared Postgres DSN.
        db_url = (
            os.environ.get("HYQS_DB_URL", "").strip() or os.environ.get("DATABASE_URL", "").strip()
        )
        return cls(
            model=os.environ.get("HYQS_MODEL", "sonnet").strip() or "sonnet",
            permission_mode=os.environ.get("HYQS_PERMISSION_MODE", "acceptEdits").strip(),
            data_dir=data_dir,
            db_url=db_url,
            pipeline_model=os.environ.get("HYQS_PIPELINE_MODEL", "").strip(),
            default_repo=os.environ.get("HYQS_DEFAULT_REPO", "").strip(),
            git_push=_as_bool(os.environ.get("HYQS_GIT_PUSH", "")),
            pipeline_max_attempts=_env_int("HYQS_PIPELINE_MAX_ATTEMPTS", 5),
            pipeline_stage_timeout=_env_int("HYQS_PIPELINE_STAGE_TIMEOUT", 1800),
            pipeline_max_timeouts=_env_int("HYQS_PIPELINE_MAX_TIMEOUTS", 2),
            pipeline_limit_backoff=_env_int("HYQS_PIPELINE_LIMIT_BACKOFF", 600),
            pipeline_enabled=not _as_bool(os.environ.get("HYQS_PIPELINE_DISABLED", "")),
            pipeline_drain_timeout=_env_int("HYQS_PIPELINE_DRAIN_TIMEOUT", 300),
            pipeline_deploy_drain_timeout=_env_int("HYQS_PIPELINE_DEPLOY_DRAIN_TIMEOUT", 2400),
            pipeline_concurrency=_env_int("HYQS_PIPELINE_CONCURRENCY", 1),
            pipeline_lease_ttl=_env_int("HYQS_PIPELINE_LEASE_TTL", 90),
            pipeline_merge_lock_ttl=_env_int("HYQS_PIPELINE_MERGE_LOCK_TTL", 300),
            pipeline_schema_lock_ttl=_env_int("HYQS_PIPELINE_SCHEMA_LOCK_TTL", 300),
            pipeline_deploy_cmd=os.environ.get("HYQS_DEPLOY_CMD", "").strip(),
            pipeline_auto_deploy=_as_bool(os.environ.get("HYQS_AUTO_DEPLOY", "")),
            pipeline_auto_deploy_interval=_env_int("HYQS_AUTO_DEPLOY_INTERVAL", 120),
            pipeline_auto_deploy_max_attempts=_env_int("HYQS_AUTO_DEPLOY_MAX_ATTEMPTS", 3),
            pipeline_job_stale_hours=_env_float("HYQS_PIPELINE_JOB_STALE_HOURS", 24),
            pipeline_host_name=os.environ.get("HYQS_HOST_NAME", "").strip(),
            pipeline_incident_analyst_model=(
                os.environ.get("HYQS_PIPELINE_INCIDENT_ANALYST_MODEL", "sonnet").strip() or "sonnet"
            ),
            pipeline_incident_analyst_provider=(
                os.environ.get("HYQS_PIPELINE_INCIDENT_ANALYST_PROVIDER", "auto").strip().lower()
                or "auto"
            ),
            pipeline_incident_analyst_scan_budget=_env_int(
                "HYQS_PIPELINE_INCIDENT_ANALYST_SCAN_BUDGET", 8
            ),
            pipeline_incident_analyst_job_cap=_env_int("HYQS_PIPELINE_INCIDENT_ANALYST_JOB_CAP", 6),
            pipeline_incident_analyst_predeadletter=not _as_bool(
                os.environ.get("HYQS_PIPELINE_INCIDENT_ANALYST_PREDEADLETTER_DISABLED", "")
            ),
            log_format=os.environ.get("HYQS_LOG_FORMAT", "json").strip() or "json",
            codex_bin=os.environ.get("HYQS_CODEX_BIN", "codex").strip() or "codex",
            codex_model=os.environ.get("HYQS_CODEX_MODEL", "").strip(),
            codex_extra_args=os.environ.get("HYQS_CODEX_ARGS", "").strip(),
            projects_dir=Path(os.environ.get("HYQS_PROJECTS_DIR", "~/projects")).expanduser(),
            github_org=os.environ.get("HYQS_GITHUB_ORG", "").strip(),
            geoip_db=Path(
                os.environ.get("HYQS_GEOIP_DB", "").strip()
                or str(data_dir / "geoip" / "dbip-city-lite.mmdb")
            ).expanduser(),
            web_host=os.environ.get("HYQS_WEB_HOST", "0.0.0.0").strip(),
            web_port=_env_int("HYQS_WEB_PORT", 8787),
            web_enabled=not _as_bool(os.environ.get("HYQS_WEB_DISABLED", "")),
            web_token=os.environ.get("HYQS_WEB_TOKEN", "").strip(),
            trusted_proxy_depth=_env_int("HYQS_TRUSTED_PROXY_DEPTH", 1),
            max_request_body_bytes=_env_int("HYQS_MAX_REQUEST_BODY_BYTES", 25 * 1024 * 1024),
            mcp_resource_url=os.environ.get("HYQS_MCP_RESOURCE_URL", "").strip(),
            web_base_url=os.environ.get("HYQS_WEB_BASE_URL", "").strip(),
            google_client_id=os.environ.get("HYQS_GOOGLE_CLIENT_ID", "").strip(),
            google_client_secret=os.environ.get("HYQS_GOOGLE_CLIENT_SECRET", "").strip(),
            apple_client_id=os.environ.get("HYQS_APPLE_CLIENT_ID", "").strip(),
            apple_team_id=os.environ.get("HYQS_APPLE_TEAM_ID", "").strip(),
            apple_key_id=os.environ.get("HYQS_APPLE_KEY_ID", "").strip(),
            apple_private_key=os.environ.get("HYQS_APPLE_PRIVATE_KEY", "").strip(),
        )
