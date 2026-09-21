"""Postgres-backed job store. Persists pipeline state so runs are resumable,
and — being a networked database rather than a local file — lets workers on many
hosts share one coordination plane (claim/lease/merge-lock/pause all live here).

Concurrency model: a psycopg connection pool, one short transaction per call.
Cross-process exclusivity for the claim decision and one-time schema creation is
provided by Postgres transaction-level advisory locks (replacing the old
single-process ``threading.Lock`` + SQLite ``BEGIN IMMEDIATE``). The claim/lease/
pause/merge-lock logic is unchanged, deterministic Python/SQL — no AI — honoring
the rule that the control plane can't depend on the very thing that's unavailable
when a provider is rate-limited.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import logging
import re
import secrets
import time
from collections import deque
from dataclasses import asdict, dataclass, is_dataclass
from typing import TYPE_CHECKING, Callable, Iterable, TypeVar

import bcrypt
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import promotions
from .collision import (
    QueueSurveyResult,
    ScopeAmendmentDecision,
    check_manifest_conflict,
    classify_dependency_scope,
    extract_scope_from_idea,
    job_declared_scope_paths,
    minimize_auto_dependencies,
    repoint_dependency_provenance,
    survey_job_queue,
)
from .models import (
    ALL_TASKS,
    REMEDIATION_LINK_KEYS,
    TERMINAL_JOB_STATUSES,
    AgentSpec,
    ApiToken,
    BacklogItem,
    BacklogItemStatus,
    BacklogItemType,
    ChatSession,
    DependencyProvenance,
    Environment,
    EnvironmentMember,
    Epic,
    Host,
    Invitation,
    Job,
    JobSource,
    JobsPage,
    JobStatus,
    PageView,
    PageViewSummary,
    PriorityBoost,
    Project,
    ProjectMember,
    Promotion,
    PromotionKind,
    PromotionState,
    ProviderFailoverTransition,
    Release,
    ResourceUsage,
    SchedulerWait,
    SchedulerWaitReason,
    SiteStats,
    Stage,
    Usage,
    User,
    Webhook,
    compute_effective_priority,
    parse_lock_owner_id,
    stage_task,
)

if TYPE_CHECKING:
    from hyqs.intake.models import IntakeSession

_log = logging.getLogger("hyqs.store")

_T = TypeVar("_T")

_UNSET = object()  # sentinel: kwarg omitted vs. explicitly passed None
_REMEDIATION_ANCESTRY_LIMIT = 32
_DUMMY_PASSWORD_HASH = bcrypt.hashpw(secrets.token_bytes(32), bcrypt.gensalt()).decode("utf-8")


@dataclass(frozen=True)
class RemediationLineage:
    root_job_id: int
    depth: int


@dataclass(frozen=True)
class SupervisorRemediationResult:
    jobs: list[Job]
    lineage: RemediationLineage
    active_remediation_job_id: int | None = None


@dataclass(frozen=True)
class BaselineSecurityRemediationResult:
    """Outcome of atomically filing project-level unchanged dependency debt."""

    job: Job
    created: bool
    blocking_dependency_attached: bool
    advisory_ids: tuple[str, ...]


@dataclass(frozen=True)
class ScopeAmendmentPersistenceResult:
    """Outcome of one atomic attempt to persist an evaluated scope amendment."""

    status: str
    sequence: int
    newly_accepted_paths: tuple[str, ...]
    cumulative_amendment_paths: tuple[str, ...]
    cumulative_allowed_paths: tuple[str, ...]
    reason: str | None = None


def _json_safe_default(value: object) -> object:
    """``json.dumps`` default= hook: degrade a stray dataclass or other
    non-primitive value instead of crashing the stage that produced it."""
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return str(value)


def _is_db_error(exc: BaseException) -> bool:
    return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))


# SQLSTATEs for errors that are transient by definition: Postgres killed one
# transaction of a valid, non-buggy pair (deadlock_detected) or aborted a
# transaction that raced another under a stricter isolation level
# (serialization_failure). Neither implies the job's code is at fault.
_TRANSIENT_PG_SQLSTATES = {"40P01", "40001"}


def is_transient_db_error(exc: BaseException) -> bool:
    return getattr(exc, "sqlstate", None) in _TRANSIENT_PG_SQLSTATES


async def _db_with_retry(fn: Callable[[], _T], *, attempts: int = 4, base_delay: float = 1.0) -> _T:
    delay = base_delay
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if not _is_db_error(exc):
                raise
            if attempt == attempts - 1:
                raise
            _log.warning("DB error (attempt %d/%d): %s", attempt + 1, attempts, exc)
            await asyncio.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


# Canonical schema. A fresh Postgres db has no legacy shapes to patch, so unlike
# the old sqlite store there's no additive _migrate(): just create-if-absent.
# IDENTITY is BY DEFAULT (not ALWAYS) so the one-shot sqlite→pg migrator can
# carry over existing primary keys; the app itself never supplies ids.
_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS page_views (
        id            BIGSERIAL PRIMARY KEY,
        ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
        path          TEXT NOT NULL,
        ref           TEXT NOT NULL DEFAULT '',
        visitor_hash  TEXT NOT NULL,
        country       TEXT,
        region        TEXT,
        city          TEXT,
        referrer_host TEXT,
        ua_family     TEXT,
        os_family     TEXT,
        lang          TEXT,
        tz            TEXT,
        screen        TEXT,
        os_version    TEXT,
        arch          TEXT,
        platform      TEXT,
        langs         TEXT,
        win           TEXT,
        viewport      TEXT,
        color_scheme  TEXT,
        device_memory TEXT,
        touch         TEXT,
        hour_cycle    TEXT
    )
    """,
    "ALTER TABLE page_views ADD COLUMN IF NOT EXISTS os_version TEXT",
    "ALTER TABLE page_views ADD COLUMN IF NOT EXISTS arch TEXT",
    "ALTER TABLE page_views ADD COLUMN IF NOT EXISTS platform TEXT",
    "ALTER TABLE page_views ADD COLUMN IF NOT EXISTS langs TEXT",
    "ALTER TABLE page_views ADD COLUMN IF NOT EXISTS win TEXT",
    "ALTER TABLE page_views ADD COLUMN IF NOT EXISTS viewport TEXT",
    "ALTER TABLE page_views ADD COLUMN IF NOT EXISTS color_scheme TEXT",
    "ALTER TABLE page_views ADD COLUMN IF NOT EXISTS device_memory TEXT",
    "ALTER TABLE page_views ADD COLUMN IF NOT EXISTS touch TEXT",
    "ALTER TABLE page_views ADD COLUMN IF NOT EXISTS hour_cycle TEXT",
    "CREATE INDEX IF NOT EXISTS ix_page_views_ts ON page_views(ts)",
    "CREATE INDEX IF NOT EXISTS ix_page_views_path_ts ON page_views(path, ts)",
    """
    CREATE TABLE IF NOT EXISTS projects (
        id          INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        name        TEXT NOT NULL,
        repo_path   TEXT NOT NULL UNIQUE,
        description TEXT DEFAULT '',
        status      TEXT NOT NULL DEFAULT 'active',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS epics (
        id          INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        project_id  INTEGER NOT NULL REFERENCES projects(id),
        name        TEXT NOT NULL,
        description TEXT DEFAULT '',
        status      TEXT NOT NULL DEFAULT 'active',
        archived    BOOLEAN NOT NULL DEFAULT FALSE,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id          INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        idea        TEXT NOT NULL,
        repo_path   TEXT NOT NULL,
        chat_id     BIGINT NOT NULL,
        branch      TEXT,
        stage       TEXT NOT NULL DEFAULT 'queued',
        status      TEXT NOT NULL DEFAULT 'pending',
        plan        TEXT,
        review      TEXT,
        error       TEXT,
        attempts    INTEGER NOT NULL DEFAULT 0,
        failure     TEXT,
        project_id  INTEGER REFERENCES projects(id),
        epic_id     INTEGER REFERENCES epics(id),
        agent_id    INTEGER,                          -- roster agent for the current stage
        provider    TEXT,                             -- that agent's provider (for the pause gate)
        owner       TEXT,                             -- worker id that claimed it
        lease_until DOUBLE PRECISION NOT NULL DEFAULT 0, -- unix ts; stale => the worker died
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    # Per-project agent roster: which providers run, what each may do, and how
    # many of each may run at once. Edited in the web UI; enforced at claim time.
    """
    CREATE TABLE IF NOT EXISTS project_agents (
        id              INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        project_id      INTEGER NOT NULL REFERENCES projects(id),
        name            TEXT NOT NULL,
        provider        TEXT NOT NULL DEFAULT 'claude',
        model           TEXT DEFAULT '',
        allowed_tasks   TEXT NOT NULL DEFAULT '["plan","build","fix","review","security"]',
        max_concurrency INTEGER NOT NULL DEFAULT 1,
        enabled         BOOLEAN NOT NULL DEFAULT TRUE,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """,
    # A per-repo advisory lock so only one job merges into a given repo at a time
    # (builds/tests/reviews stay parallel in worktrees; only merge serializes).
    """
    CREATE TABLE IF NOT EXISTS merge_locks (
        repo_path   TEXT PRIMARY KEY,
        owner       TEXT NOT NULL,
        acquired_at DOUBLE PRECISION NOT NULL
    )
    """,
    # A per-project advisory lock serializing schema/migration-touching jobs from
    # the merge boundary through deploy — a schema job holds this the whole
    # build->deploy window, not just the merge instant (see merge_locks above).
    """
    CREATE TABLE IF NOT EXISTS schema_locks (
        project_id  INTEGER PRIMARY KEY,
        owner       TEXT NOT NULL,
        acquired_at DOUBLE PRECISION NOT NULL
    )
    """,
    # Live worker heartbeats — powers the real-time fleet/C2 view. Each worker
    # upserts its row as it claims/finishes work; a stale last_seen means it died.
    """
    CREATE TABLE IF NOT EXISTS workers (
        id         TEXT PRIMARY KEY,            -- host:pid:n
        host       TEXT,
        pid        INTEGER,
        status     TEXT,                        -- 'idle' | 'busy' | 'leader' | 'standby'
        role       TEXT NOT NULL DEFAULT 'executor',  -- 'executor' | 'supervisor'
        job_id     INTEGER,                     -- the job it's advancing (NULL when idle)
        stage      TEXT,                        -- the stage it's executing
        provider   TEXT,                        -- the agent provider in use
        started_at DOUBLE PRECISION,            -- when it started the CURRENT job/stage
        last_seen  DOUBLE PRECISION             -- heartbeat; stale => dead
    )
    """,
    # Durable key/value for runner control state (e.g. the rate-limit pause gate),
    # so a reboot doesn't lose where we were.
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS usage (
        id                    INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        job_id                INTEGER,           -- NULL = non-pipeline (e.g. dev session)
        source                TEXT NOT NULL,     -- 'plan' | 'build' | ... | 'dev-session'
        model                 TEXT,
        provider              TEXT,              -- 'claude' | 'codex' | ...
        input_tokens          INTEGER NOT NULL DEFAULT 0,
        output_tokens         INTEGER NOT NULL DEFAULT 0,
        cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
        cost_usd              DOUBLE PRECISION NOT NULL DEFAULT 0,
        created_at            TEXT NOT NULL
    )
    """,
    # One row per completed pipeline step, powering the per-job activity timeline:
    # what each stage did, how long it took, what it cost, and stage-specific
    # detail (diff stat, test output, review findings) as JSON in `detail`.
    """
    CREATE TABLE IF NOT EXISTS job_events (
        id          INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        job_id      INTEGER NOT NULL,
        stage       TEXT NOT NULL,
        status      TEXT NOT NULL,
        attempt     INTEGER NOT NULL DEFAULT 0,
        summary     TEXT,
        detail      TEXT,
        tokens      INTEGER NOT NULL DEFAULT 0,
        cost_usd    DOUBLE PRECISION NOT NULL DEFAULT 0,
        started_at  TEXT,
        ended_at    TEXT
    )
    """,
    # Additive migration: track which roster agent ran each event (NULL for rows
    # written before this column existed, so fully backwards-compatible).
    "ALTER TABLE job_events ADD COLUMN IF NOT EXISTS agent_id INTEGER",
    # Additive migration: separate rebase-conflict retry budget (never touches
    # job.attempts, so a merge-contention bounce doesn't spend a FIX credit).
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS rebase_attempts INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS rebase_retry_after DOUBLE PRECISION",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS timeout_attempts INTEGER NOT NULL DEFAULT 0",
    # Actual in-flight operation and stable failure metadata. All are nullable so
    # old workers and rows remain valid during a rolling deployment.
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS executing_step TEXT",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS failed_step TEXT",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS failure_code TEXT",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS failure_origin TEXT",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS retry_disposition TEXT",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS failure_detail JSONB",
    # Additive migration: separate bounded one-shot PLAN-stage oversized-plan
    # re-ask budget (never touches job.attempts).
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS plan_reask_attempts INTEGER NOT NULL DEFAULT 0",
    # Additive migration: per-project self-heal fix budget override. NULL means
    # 'use the global default' (pipeline_max_attempts from config).
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS max_fix_attempts INTEGER",
    # Additive migration: soft-archive terminal jobs so the active view stays
    # manageable without losing history.
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT FALSE",
    # Additive migration: soft-archive epics so the project view stays manageable.
    "ALTER TABLE epics ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT FALSE",
    # Additive migration: supervisor worker role + status variants.
    "ALTER TABLE workers ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'executor'",
    # Live subprocess output, one row per line. Consumed by the SSE log-stream
    # endpoint; rows are cheap to insert and auto-deleted with the job.
    """
    CREATE TABLE IF NOT EXISTS job_logs (
        id      BIGSERIAL PRIMARY KEY,
        job_id  INT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        stage   TEXT NOT NULL,
        line    TEXT NOT NULL,
        ts      DOUBLE PRECISION NOT NULL
    )
    """,
    # Additive migration: tag every log line with its attempt number so the UI
    # can separate failed-attempt output from the passing run.
    "ALTER TABLE job_logs ADD COLUMN IF NOT EXISTS attempt INTEGER NOT NULL DEFAULT 0",
    # Additive migration: janitor remediation history for the supervisor timeline.
    """
    CREATE TABLE IF NOT EXISTS supervisor_events (
        id            INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        ts            DOUBLE PRECISION NOT NULL DEFAULT extract(epoch from now()),
        job_id        INTEGER REFERENCES jobs(id),
        action        TEXT NOT NULL,
        failure_class TEXT NOT NULL,
        detail        TEXT
    )
    """,
    # Per-project symbol index: deterministically-extracted public symbols from
    # each project's repo, refreshed before PLAN so BUILD agents can reuse code.
    """
    CREATE TABLE IF NOT EXISTS symbol_index (
        id         INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        module     TEXT NOT NULL,
        symbol     TEXT NOT NULL,
        kind       TEXT NOT NULL,
        signature  TEXT NOT NULL DEFAULT '',
        summary    TEXT NOT NULL DEFAULT '',
        updated_at DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM now()),
        UNIQUE (project_id, module, symbol)
    )
    """,
    "CREATE INDEX IF NOT EXISTS symbol_index_project_id ON symbol_index(project_id)",
    # Additive migration: job-to-job dependencies for scheduling eligibility.
    "CREATE TABLE IF NOT EXISTS job_dependencies (job_id INTEGER NOT NULL REFERENCES jobs(id), depends_on_job_id INTEGER NOT NULL REFERENCES jobs(id), PRIMARY KEY (job_id, depends_on_job_id))",
    "ALTER TABLE job_dependencies ADD COLUMN IF NOT EXISTS provenance TEXT NOT NULL DEFAULT 'user'",
    "CREATE INDEX IF NOT EXISTS ix_jobdep_dep ON job_dependencies(depends_on_job_id)",
    # Additive migration: manual priority for ordering eligible jobs (higher = sooner).
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 0",
    # Additive migration: project tech stack label and structured spec blob.
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS stack TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS spec JSONB",
    # Additive migration: per-project deploy config (JSON string) for Docker deploys.
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS deploy_config TEXT NOT NULL DEFAULT ''",
    # Additive migration: GitHub repo URL — empty string means no remote was created yet.
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS github_url TEXT NOT NULL DEFAULT ''",
    # Additive migration: git commit hash confirmed live after deploy (NULL until verified).
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS deployed_commit TEXT",
    # Role → permission mapping. Seeded with defaults on first boot (empty table);
    # platform_admin admin-area permissions are locked (enforced in set_role_permission).
    """
    CREATE TABLE IF NOT EXISTS role_permissions (
        role        TEXT NOT NULL,
        permission  TEXT NOT NULL,
        PRIMARY KEY (role, permission)
    )
    """,
    # Per-project membership: which users have access and at what role.
    """
    CREATE TABLE IF NOT EXISTS project_members (
        id         SERIAL PRIMARY KEY,
        user_id    TEXT NOT NULL,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        role       TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT '',
        UNIQUE(user_id, project_id)
    )
    """,
    # Additive migration: rename identifier → user_id if the table was created with the old column name.
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='project_members' AND column_name='identifier') THEN ALTER TABLE project_members RENAME COLUMN identifier TO user_id; END IF; END $$",
    # Additive migration: ensure user_id column exists (for existing tables that lack it).
    "ALTER TABLE project_members ADD COLUMN IF NOT EXISTS user_id TEXT NOT NULL DEFAULT ''",
    # Additive migration: grant manage_members to project_admin and platform_admin.
    "INSERT INTO role_permissions(role, permission) VALUES ('project_admin', 'manage_members') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('platform_admin', 'manage_members') ON CONFLICT DO NOTHING",
    # Backfill full default permission set (idempotent; ON CONFLICT DO NOTHING).
    "INSERT INTO role_permissions(role, permission) VALUES ('contributor', 'queue_job') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('contributor', 'cancel_job') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('contributor', 'retry_job') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('contributor', 'archive_job') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('contributor', 'edit_job_deps') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('project_admin', 'edit_job_deps') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('platform_admin', 'edit_job_deps') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('project_admin', 'queue_job') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('project_admin', 'cancel_job') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('project_admin', 'retry_job') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('project_admin', 'archive_job') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('project_admin', 'edit_project') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('project_admin', 'edit_agents') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('platform_admin', 'queue_job') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('platform_admin', 'cancel_job') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('platform_admin', 'retry_job') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('platform_admin', 'archive_job') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('platform_admin', 'edit_project') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('platform_admin', 'edit_agents') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('platform_admin', 'manage_users') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('platform_admin', 'manage_invitations') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('platform_admin', 'manage_roles') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('platform_admin', 'manage_providers') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('platform_admin', 'view_fleet') ON CONFLICT DO NOTHING",
    # Guided intake sessions: each session captures a Project Spec via an AI interview.
    # Nothing is provisioned until the user explicitly confirms a complete spec.
    """
    CREATE TABLE IF NOT EXISTS intake_sessions (
        id          INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        session_id  TEXT NOT NULL UNIQUE,
        created_by  TEXT NOT NULL,
        draft_spec  JSONB NOT NULL DEFAULT '{}',
        messages    JSONB NOT NULL DEFAULT '[]',
        status      TEXT NOT NULL DEFAULT 'in_progress',
        project_id  INTEGER REFERENCES projects(id),
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    # Additive migration: if users.created_at is TIMESTAMPTZ from an older schema, cast to TEXT.
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='created_at' AND udt_name='timestamptz') THEN ALTER TABLE users ALTER COLUMN created_at TYPE TEXT USING to_char(created_at, 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'); END IF; END $$",
    # Platform users: email+bcrypt credentials, admin flag, active toggle.
    """
    CREATE TABLE IF NOT EXISTS users (
        id                INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        email             TEXT UNIQUE NOT NULL,
        password_hash     TEXT NOT NULL,
        display_name      TEXT NOT NULL DEFAULT '',
        is_platform_admin BOOLEAN NOT NULL DEFAULT FALSE,
        is_active         BOOLEAN NOT NULL DEFAULT TRUE,
        created_at        TEXT NOT NULL DEFAULT ''
    )
    """,
    # Login sessions: a random bearer token tied to a user with an expiry.
    """
    CREATE TABLE IF NOT EXISTS sessions (
        token       TEXT PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        expires_at  DOUBLE PRECISION NOT NULL,
        created_at  TEXT NOT NULL DEFAULT ''
    )
    """,
    # Per-stage hardware resource accounting: one row per deterministic stage run.
    # cpu/memory/io from cgroup v2; wall_seconds always present; nullable fields
    # are null when systemd-run / cgroup v2 is unavailable (graceful degradation).
    """
    CREATE TABLE IF NOT EXISTS resource_usage (
        id              BIGSERIAL PRIMARY KEY,
        job_id          BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        stage           TEXT NOT NULL,
        attempt         INT NOT NULL DEFAULT 0,
        cpu_seconds     DOUBLE PRECISION,
        peak_rss_bytes  BIGINT,
        io_read_bytes   BIGINT,
        io_write_bytes  BIGINT,
        wall_seconds    DOUBLE PRECISION NOT NULL,
        net_bytes       BIGINT,
        disk_bytes      BIGINT,
        sampled_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (job_id, stage, attempt)
    )
    """,
    # Additive migration: job provenance — how/who/where a job was filed.
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'unknown'",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source_actor TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source_meta JSONB NOT NULL DEFAULT '{}'",
    # Backfill: jobs #101 and #102 finished DONE with deployed_commit=NULL because an earlier
    # save() was silently no-op'd by the terminal-status guard before the fix landed.
    # Commit a3e2999 was live in main at their deploy time.
    "UPDATE jobs SET deployed_commit = 'a3e2999' WHERE id IN (101, 102) AND status = 'done' AND deployed_commit IS NULL",
    # Additive migration: per-stage approximate network bytes flag.
    "ALTER TABLE resource_usage ADD COLUMN IF NOT EXISTS net_bytes_approx BOOLEAN NOT NULL DEFAULT false",
    # Invitation tokens: admins issue email-scoped tokens before first OAuth login.
    """
    CREATE TABLE IF NOT EXISTS invitations (
        id          SERIAL PRIMARY KEY,
        token       TEXT UNIQUE NOT NULL,
        email       TEXT NOT NULL,
        invited_by  TEXT NOT NULL DEFAULT '',
        expires_at  DOUBLE PRECISION NOT NULL,
        consumed_at DOUBLE PRECISION,
        created_at  TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_invitations_email ON invitations(email)",
    # Additive migration: OAuth provider that created this user account ('' = password).
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS oauth_provider TEXT NOT NULL DEFAULT ''",
    # Additive migration: short one-line title derived from idea (<=80 chars).
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS title TEXT NOT NULL DEFAULT ''",
    # Additive migration: security review verdict (parallel to review column).
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS security_review TEXT",
    # Backfill: derive title from the first non-empty line of idea for existing rows.
    "UPDATE jobs SET title = trim(left(regexp_replace(idea, E'\\n.*', '', 'ns'), 80)) WHERE title = ''",
    # Additive migration: grant delete_project to project_admin and platform_admin.
    "INSERT INTO role_permissions(role, permission) VALUES ('project_admin', 'delete_project') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('platform_admin', 'delete_project') ON CONFLICT DO NOTHING",
    # Additive migration: platform-scoped create_project permission (platform_admin only).
    "INSERT INTO role_permissions(role, permission) VALUES ('platform_admin', 'create_project') ON CONFLICT DO NOTHING",
    # Additive migration: backfill the project_agents column default to include all current
    # AgentTask values. Agents created via the old default ('["plan","build","fix","review"]')
    # would never be able to claim jobs at the SECURITY stage. reconcile_agent_tasks() also
    # fixes existing *rows*, but correcting the column default prevents the same drift on
    # future INSERT … DEFAULT inserts that bypass _insert_default_agent.
    'ALTER TABLE project_agents ALTER COLUMN allowed_tasks SET DEFAULT \'["plan","build","fix","review","security"]\'',
    # Additive migration: terminal resolution marker ('already-satisfied' or '').
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS resolution TEXT NOT NULL DEFAULT ''",
    # Additive migration: AI-generated prose summary of what shipped (generated once at merge).
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS implementation_summary TEXT",
    # Additive migration: per-user platform-scoped permission grants (independent of project membership).
    """
    CREATE TABLE IF NOT EXISTS user_platform_permissions (
        user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        permission TEXT NOT NULL,
        granted_at TEXT NOT NULL DEFAULT '',
        granted_by TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (user_id, permission)
    )
    """,
    # Additive migration: SHA of the pre-conflict HEAD used to scope MERGE_VERIFY diff.
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS merge_delta_sha TEXT",
    # Backlog: pre-refinement proposals from any project member. Separate from Jobs:
    # a backlog item spends no quota and has no epic until it is explicitly converted.
    """
    CREATE TABLE IF NOT EXISTS backlog_items (
        id             SERIAL PRIMARY KEY,
        project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        title          TEXT NOT NULL,
        body           TEXT NOT NULL DEFAULT '',
        type           TEXT NOT NULL DEFAULT 'idea',
        proposed_by    TEXT NOT NULL DEFAULT '',
        status         TEXT NOT NULL DEFAULT 'new',
        votes          INTEGER NOT NULL DEFAULT 0,
        epic_hint      TEXT,
        linked_job_ids JSONB NOT NULL DEFAULT '[]',
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    )
    """,
    # Per-user per-item vote tracking: prevents ballot stuffing (toggle model).
    """
    CREATE TABLE IF NOT EXISTS backlog_votes (
        item_id INTEGER NOT NULL REFERENCES backlog_items(id) ON DELETE CASCADE,
        user_id TEXT NOT NULL,
        PRIMARY KEY (item_id, user_id)
    )
    """,
    # Backlog permissions: propose_backlog for all member roles; triage_backlog for admins.
    "INSERT INTO role_permissions(role, permission) VALUES ('viewer', 'propose_backlog') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('contributor', 'propose_backlog') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('project_admin', 'propose_backlog') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('platform_admin', 'propose_backlog') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('project_admin', 'triage_backlog') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('platform_admin', 'triage_backlog') ON CONFLICT DO NOTHING",
    # Webhooks: external subscriptions to job-complete/deploy events, managed via MCP tools.
    """
    CREATE TABLE IF NOT EXISTS webhooks (
        id          SERIAL PRIMARY KEY,
        project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        url         TEXT NOT NULL,
        event_type  TEXT NOT NULL,
        created_by  TEXT NOT NULL DEFAULT '',
        active      BOOLEAN NOT NULL DEFAULT TRUE,
        created_at  TEXT NOT NULL
    )
    """,
    "INSERT INTO role_permissions(role, permission) VALUES ('project_admin', 'manage_webhooks') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('platform_admin', 'manage_webhooks') ON CONFLICT DO NOTHING",
    "ALTER TABLE webhooks ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'http'",
    # Per-project Slack bot token, encrypted at rest — lets a later Slack
    # delivery transport post to a project's workspace without a plaintext secret.
    """
    CREATE TABLE IF NOT EXISTS project_slack_credentials (
        project_id          INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
        bot_token_encrypted TEXT NOT NULL,
        created_by          TEXT NOT NULL DEFAULT '',
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL
    )
    """,
    # Job-chat sessions: persisted intake conversations for the JobChatPanel.
    # Each session captures the full message history and proposed jobs from a planning chat.
    """
    CREATE TABLE IF NOT EXISTS chat_sessions (
        id            INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        session_id    TEXT NOT NULL UNIQUE,
        project_id    INTEGER NOT NULL REFERENCES projects(id),
        created_by    TEXT NOT NULL,
        messages      JSONB NOT NULL DEFAULT '[]',
        proposed_jobs JSONB NOT NULL DEFAULT '[]',
        status        TEXT NOT NULL DEFAULT 'active',
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    )
    """,
    "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS source_backlog_item_ids JSONB NOT NULL DEFAULT '[]'",
    # Per-project deploy mutex: serialize concurrent deploys so a sha is never built twice.
    """
    CREATE TABLE IF NOT EXISTS deploy_locks (
        project_id  INTEGER PRIMARY KEY,
        owner       TEXT NOT NULL,
        acquired_at TEXT NOT NULL
    )
    """,
    # Append-only deploys ledger: single source of truth for what is live per project.
    # trigger CHECK values: pipeline_job (normal pipeline), manual (UI button),
    # auto_poller (#284 supervisor poller), unknown (backfilled / out-of-band).
    # job_id is nullable so manual/auto_poller deploys have no attribution.
    # ON DELETE CASCADE on project_id keeps deploys in sync with project deletes.
    # ON DELETE SET NULL on job_id preserves the deploy record when a job is archived.
    # UNIQUE(job_id) makes the one-time backfill idempotent (multiple NULLs are allowed).
    """
    CREATE TABLE IF NOT EXISTS deploys (
        id              BIGSERIAL PRIMARY KEY,
        project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        deployed_commit TEXT NOT NULL,
        previous_commit TEXT,
        deployed_at     TEXT NOT NULL,
        verified        BOOLEAN NOT NULL DEFAULT TRUE,
        trigger         TEXT NOT NULL CHECK(trigger IN ('pipeline_job','manual','auto_poller','unknown')),
        job_id          INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
        summary         TEXT,
        UNIQUE(job_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_deploys_project_deployed_at ON deploys(project_id, deployed_at)",
    # One-time best-effort backfill: seed deploys from done jobs' deployed_commit history.
    # LAG computes previous_commit per project (ordered by updated_at).
    # ON CONFLICT (job_id) DO NOTHING makes this idempotent on repeated schema runs.
    """
    INSERT INTO deploys (project_id, deployed_commit, previous_commit, deployed_at, verified, trigger, job_id)
    SELECT
        project_id,
        deployed_commit,
        LAG(deployed_commit) OVER (PARTITION BY project_id ORDER BY updated_at),
        updated_at,
        TRUE,
        'pipeline_job',
        id
    FROM jobs
    WHERE status = 'done' AND deployed_commit IS NOT NULL
    ON CONFLICT (job_id) DO NOTHING
    """,
    # --- Audit trail: who changed what, when -------------------------------
    # Every mutation on an audited table stamps created_by/updated_by and appends
    # an audit_log row. The actor comes from the per-transaction 'hyqs.actor'
    # setting (set by JobStore._connection from the set_db_actor contextvar);
    # writes that bypass the app (psql, scripts) fall back to 'db:<session_user>'
    # so out-of-band edits are still attributable.
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id          BIGSERIAL PRIMARY KEY,
        at          TIMESTAMPTZ NOT NULL DEFAULT now(),
        actor       TEXT NOT NULL,
        table_name  TEXT NOT NULL,
        row_pk      TEXT NOT NULL,
        action      TEXT NOT NULL,
        changed     JSONB NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_audit_log_at ON audit_log(at)",
    "CREATE INDEX IF NOT EXISTS ix_audit_log_table_at ON audit_log(table_name, at)",
    "CREATE INDEX IF NOT EXISTS ix_audit_log_actor_at ON audit_log(actor, at)",
    # Connection-level forensic data Postgres already tracks for every
    # connection, independent of the self-reported 'actor' string.
    "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS backend_pid INTEGER",
    "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS client_addr TEXT",
    "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS backend_start TIMESTAMPTZ",
    # Provenance columns on every audited table (trigger-maintained).
    *(
        f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {c} TEXT NOT NULL DEFAULT ''"
        for t in (
            "jobs",
            "projects",
            "epics",
            "project_agents",
            "project_members",
            "users",
            "invitations",
            "backlog_items",
            "chat_sessions",
            "deploys",
            "meta",
            "role_permissions",
            "webhooks",
        )
        for c in ("created_by", "updated_by")
    ),
    # Generic audit trigger. TG_ARGV[0] names the primary-key column. Diff values
    # are truncated to 300 chars (the audit answers who/what/when, not full
    # content diffs). Updates whose only changes are heartbeat churn
    # (lease_until / updated_at) produce no audit row.
    """
    CREATE OR REPLACE FUNCTION hyqs_audit() RETURNS trigger AS $fn$
    DECLARE
        actor TEXT;
        pk_col TEXT := TG_ARGV[0];
        diff JSONB;
        rec JSONB;
        conn_pid INTEGER := pg_backend_pid();
        conn_addr TEXT := inet_client_addr()::text;
        conn_start TIMESTAMPTZ := (
            SELECT backend_start FROM pg_stat_activity WHERE pid = pg_backend_pid()
        );
    BEGIN
        actor := COALESCE(NULLIF(current_setting('hyqs.actor', true), ''), 'db:' || session_user);
        IF TG_OP = 'INSERT' THEN
            NEW.created_by := COALESCE(NULLIF(NEW.created_by, ''), actor);
            NEW.updated_by := actor;
            rec := to_jsonb(NEW);
            INSERT INTO audit_log(actor, table_name, row_pk, action, backend_pid, client_addr, backend_start)
            VALUES (actor, TG_TABLE_NAME, COALESCE(rec ->> pk_col, ''), 'insert', conn_pid, conn_addr, conn_start);
            RETURN NEW;
        ELSIF TG_OP = 'UPDATE' THEN
            NEW.updated_by := actor;
            SELECT COALESCE(
                jsonb_object_agg(
                    n.key,
                    jsonb_build_array(left(o.value::text, 300), left(n.value::text, 300))
                ),
                '{}'::jsonb
            )
              INTO diff
              FROM jsonb_each(to_jsonb(OLD)) o
              JOIN jsonb_each(to_jsonb(NEW)) n ON n.key = o.key
             WHERE o.value IS DISTINCT FROM n.value
               AND n.key NOT IN ('updated_at', 'updated_by', 'lease_until');
            IF diff = '{}'::jsonb THEN
                RETURN NEW;
            END IF;
            rec := to_jsonb(NEW);
            INSERT INTO audit_log(actor, table_name, row_pk, action, changed, backend_pid, client_addr, backend_start)
            VALUES (actor, TG_TABLE_NAME, COALESCE(rec ->> pk_col, ''), 'update', diff, conn_pid, conn_addr, conn_start);
            RETURN NEW;
        ELSE
            rec := to_jsonb(OLD);
            INSERT INTO audit_log(actor, table_name, row_pk, action, backend_pid, client_addr, backend_start)
            VALUES (actor, TG_TABLE_NAME, COALESCE(rec ->> pk_col, ''), 'delete', conn_pid, conn_addr, conn_start);
            RETURN OLD;
        END IF;
    END;
    $fn$ LANGUAGE plpgsql
    """,
    *(
        stmt
        for t, pk in (
            ("jobs", "id"),
            ("projects", "id"),
            ("epics", "id"),
            ("project_agents", "id"),
            ("project_members", "id"),
            ("users", "id"),
            ("invitations", "id"),
            ("backlog_items", "id"),
            ("chat_sessions", "id"),
            ("deploys", "id"),
            ("meta", "key"),
            ("role_permissions", "role"),
            ("webhooks", "id"),
        )
        for stmt in (
            f"DROP TRIGGER IF EXISTS audit_{t} ON {t}",
            f"CREATE TRIGGER audit_{t} BEFORE INSERT OR UPDATE OR DELETE ON {t} "
            f"FOR EACH ROW EXECUTE FUNCTION hyqs_audit('{pk}')",
        )
    ),
    # Additive migration: design/UX review verdict (parallel to security_review column).
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS design_review TEXT",
    # Project-scoped API tokens: a least-privilege alternative to the global
    # web_token bearer. Bound to one project + one existing role; the secret
    # itself is never persisted, only its sha256 hash (for lookup) + last4
    # (for display).
    """
    CREATE TABLE IF NOT EXISTS api_tokens (
        id           INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        name         TEXT NOT NULL,
        role         TEXT NOT NULL,
        token_hash   TEXT NOT NULL UNIQUE,
        last4        TEXT NOT NULL,
        created_by   TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL,
        last_used_at TEXT,
        revoked_at   TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_api_tokens_project_id ON api_tokens(project_id)",
    # Additive migration: grant manage_api_tokens to project_admin and platform_admin
    # (mirroring the manage_members seed rows above).
    "INSERT INTO role_permissions(role, permission) VALUES ('project_admin', 'manage_api_tokens') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('platform_admin', 'manage_api_tokens') ON CONFLICT DO NOTHING",
    # Additive migration: guided job-resolution actions (requeue/fix-forward/resolve)
    # for FAILED jobs, gated separately from edit_job_deps.
    "INSERT INTO role_permissions(role, permission) VALUES ('contributor', 'resolve_job') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('project_admin', 'resolve_job') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('platform_admin', 'resolve_job') ON CONFLICT DO NOTHING",
    # Per-job, per-webhook Slack thread anchor: lets every notify() call for a
    # job reply into the same thread instead of posting a new top-level message.
    """
    CREATE TABLE IF NOT EXISTS job_slack_threads (
        job_id     INTEGER NOT NULL,
        webhook_id INTEGER NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
        thread_ts  TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (job_id, webhook_id)
    )
    """,
    # Backfill: DONE jobs carried forward stale `failure` text from an earlier
    # gate rejection that a later fix-and-retry attempt corrected before merge.
    # save()/finalize_deploy_done()/finalize_deploying_job()/reconcile_to_done()
    # now clear failure/error on every genuine DONE transition; this clears the
    # ~261 rows that reached DONE before that fix landed. Idempotent: only
    # matches status='done' rows with non-empty failure, so a second run is a no-op.
    # Additive migration: track which remediation job is currently in flight for
    # a given incident job, so the janitor doesn't file a second one concurrently.
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS active_remediation_for INTEGER REFERENCES jobs(id) ON DELETE SET NULL",
    """
    CREATE OR REPLACE FUNCTION hyqs_clear_terminal_remediation()
    RETURNS TRIGGER AS $fn$
    BEGIN
        IF NEW.status IN ('done', 'failed', 'cancelled')
           AND OLD.status IS DISTINCT FROM NEW.status THEN
            UPDATE jobs SET active_remediation_for = NULL
            WHERE active_remediation_for = NEW.id;
        END IF;
        RETURN NEW;
    END;
    $fn$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS clear_terminal_remediation ON jobs",
    """
    CREATE TRIGGER clear_terminal_remediation
    AFTER UPDATE OF status ON jobs
    FOR EACH ROW EXECUTE FUNCTION hyqs_clear_terminal_remediation()
    """,
    # Additive migration: a PLAN-stage job whose oversized plan survived the
    # bounded one-shot re-ask (see stages/plan.py) is parked here rather than
    # left FAILED-and-retryable — it needs a human to refile a smaller job.
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS needs_split BOOLEAN NOT NULL DEFAULT FALSE",
    # Foundation for multi-environment deployment (Epic 81): a first-class
    # Environment entity. Purely additive — nothing reads or branches on
    # environment_id yet; see deploy.py/docker_deploy.py/supervisor.py, unchanged.
    """
    CREATE TABLE IF NOT EXISTS environments (
        id         INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        name       TEXT NOT NULL,
        kind       TEXT NOT NULL CHECK(kind IN ('dev','staging','prod')),
        status     TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(project_id, name)
    )
    """,
    # One-time best-effort backfill: every existing project gets exactly one
    # implicit 'prod' environment. ON CONFLICT makes this idempotent.
    """
    INSERT INTO environments (project_id, name, kind, status, created_at, updated_at)
    SELECT id, 'prod', 'prod', 'active', created_at, created_at FROM projects
    ON CONFLICT (project_id, name) DO NOTHING
    """,
    "UPDATE jobs SET failure='' WHERE status='done' AND COALESCE(failure,'') <> ''",
    # Deploy-plane re-key step 2 (Epic 81, job #1746): additive environment_id
    # columns on deploys/deploy_locks, dual-state alongside project_id. Nothing
    # yet reads or branches on environment_id — see store.py's record_deploy/
    # acquire_deploy_lock, which only stamp it on new rows.
    "ALTER TABLE deploys ADD COLUMN IF NOT EXISTS environment_id INTEGER REFERENCES environments(id) ON DELETE CASCADE",
    "ALTER TABLE deploy_locks ADD COLUMN IF NOT EXISTS environment_id INTEGER REFERENCES environments(id) ON DELETE CASCADE",
    # Idempotent backfill: every pre-existing deploys/deploy_locks row gets its
    # project's implicit 'prod' environment. The `environment_id IS NULL` guard
    # makes a second pass a no-op.
    """
    UPDATE deploys d SET environment_id = e.id
    FROM environments e
    WHERE e.project_id = d.project_id AND e.name = 'prod' AND d.environment_id IS NULL
    """,
    """
    UPDATE deploy_locks l SET environment_id = e.id
    FROM environments e
    WHERE e.project_id = l.project_id AND e.name = 'prod' AND l.environment_id IS NULL
    """,
    # Deploy-plane re-key step 3 (Epic 81, job #1747): the deploy ledger and
    # deploy mutex are now keyed on environment_id (see store.py's
    # record_deploy/get_last_deploy/acquire_deploy_lock/release_deploy_lock),
    # with project_id kept only for grouping. deploy_locks drops its old
    # per-project PK for a per-environment unique index, so two environments
    # under one project can each hold their own lock.
    "ALTER TABLE deploy_locks DROP CONSTRAINT IF EXISTS deploy_locks_pkey",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_deploy_locks_environment_id ON deploy_locks(environment_id)",
    "CREATE INDEX IF NOT EXISTS ix_deploy_locks_project_id ON deploy_locks(project_id)",
    "CREATE INDEX IF NOT EXISTS ix_deploys_environment_deployed_at ON deploys(environment_id, deployed_at)",
    # Epic 81 foundation (job #1748): per-environment deploy config, layered
    # over the project-level `deploy_config` rather than replacing it. Empty
    # by default so existing single-env projects keep reading their project's
    # deploy_config via get_environment_config's fallback.
    "ALTER TABLE environments ADD COLUMN IF NOT EXISTS config TEXT NOT NULL DEFAULT ''",
    # Epic 81 step (job #1762): promotable artifact identity on the deploy
    # ledger — image_ref/image_digest/signed record what was pushed/signed by
    # registry_push.push_and_sign, so later promotion jobs can read it back.
    "ALTER TABLE deploys ADD COLUMN IF NOT EXISTS image_ref TEXT",
    "ALTER TABLE deploys ADD COLUMN IF NOT EXISTS image_digest TEXT",
    "ALTER TABLE deploys ADD COLUMN IF NOT EXISTS signed BOOLEAN NOT NULL DEFAULT FALSE",
    # Deployer foundation (Epic 81): a first-class Host entity. Purely
    # additive — nothing reads or branches on host_id yet; runner, claim, and
    # deploy paths are unchanged.
    """
    CREATE TABLE IF NOT EXISTS hosts (
        id         INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        name       TEXT NOT NULL UNIQUE,
        public_key TEXT,
        status     TEXT NOT NULL DEFAULT 'active',
        last_seen  TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_hosts_public_key ON hosts(public_key) WHERE public_key IS NOT NULL",
    # NULL host_id means "the local/default host" — this preserves today's
    # single-host behavior; nothing consults this column yet.
    "ALTER TABLE environments ADD COLUMN IF NOT EXISTS host_id INTEGER REFERENCES hosts(id) ON DELETE SET NULL",
    # Promotion foundation (Epic 81): a first-class Release entity for the
    # build-once artifact. Purely additive — nothing consumes it for
    # promotion yet outside recording a signed-push release and pointing
    # the deploying environment's current_release_id at it.
    """
    CREATE TABLE IF NOT EXISTS releases (
        id             BIGSERIAL PRIMARY KEY,
        project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        source_commit  TEXT,
        image_ref      TEXT,
        image_digest   TEXT,
        manifest       TEXT,
        signature_ref  TEXT,
        built_at       TEXT NOT NULL,
        built_by       TEXT,
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_releases_project_built_at ON releases(project_id, built_at)",
    "ALTER TABLE environments ADD COLUMN IF NOT EXISTS current_release_id INTEGER REFERENCES releases(id) ON DELETE SET NULL",
    # Promotion foundation, step 2 (Epic 81, job #1785): the promotions ledger
    # + state machine. Purely additive and deterministic — no promote/offer/
    # apply business actions or deploy-job enqueue happen here yet.
    """
    CREATE TABLE IF NOT EXISTS promotions (
        id             BIGSERIAL PRIMARY KEY,
        project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        release_id     INTEGER NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
        source_env_id  INTEGER REFERENCES environments(id) ON DELETE SET NULL,
        target_env_id  INTEGER NOT NULL REFERENCES environments(id) ON DELETE CASCADE,
        kind           TEXT NOT NULL,
        state          TEXT NOT NULL,
        requested_by   TEXT,
        requested_at   TEXT NOT NULL,
        approved_by    TEXT,
        applied_by     TEXT,
        applied_at     TEXT,
        deploy_job_id  INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_promotions_target_env_created_at ON promotions(target_env_id, created_at)",
    # Additive migration: grant deploy.promote to project_admin and platform_admin
    # (grants the promote_release MCP tool).
    "INSERT INTO role_permissions(role, permission) VALUES ('project_admin', 'deploy.promote') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('platform_admin', 'deploy.promote') ON CONFLICT DO NOTHING",
    # Additive migration: grant deploy.offer to project_admin and platform_admin
    # (grants the offer_release MCP tool). Guard-only for now — a later job
    # tightens this to CLIENT-kind target environments only.
    "INSERT INTO role_permissions(role, permission) VALUES ('project_admin', 'deploy.offer') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('platform_admin', 'deploy.offer') ON CONFLICT DO NOTHING",
    # Environment-scoped grants (Epic 81, job #1789): lets a user hold a
    # permission for exactly one environment (e.g. a client_approver) without
    # being a project_member with that permission project-wide.
    """
    CREATE TABLE IF NOT EXISTS environment_members (
        id             SERIAL PRIMARY KEY,
        environment_id INTEGER NOT NULL REFERENCES environments(id) ON DELETE CASCADE,
        user_id        TEXT NOT NULL,
        role           TEXT NOT NULL,
        created_at     TEXT NOT NULL DEFAULT '',
        UNIQUE(user_id, environment_id)
    )
    """,
    # Poller-narrowing foundation (Epic 81, job #1809): distinguishes an
    # auto-deploy-from-main environment (dev) from a promotion-only one
    # (staging/prod/client). Purely additive — nothing reads or branches on
    # auto_deploy yet; the poller and deploy paths are unchanged.
    "ALTER TABLE environments ADD COLUMN IF NOT EXISTS auto_deploy BOOLEAN NOT NULL DEFAULT FALSE",
    # Backfill: every existing implicit per-project 'prod' environment (seeded
    # by #1744) was the self-deploy-from-main target, so preserve that once
    # the poller (#7b) starts consulting this flag. Idempotent: only flips
    # rows still FALSE.
    "UPDATE environments SET auto_deploy = TRUE WHERE name = 'prod' AND auto_deploy = FALSE",
    # Additive migration (job #2969): a new 'automation_client' role for
    # unattended external orchestration clients. Grants the same job-level
    # actions as contributor, minus archive_job (a human-only action).
    "INSERT INTO role_permissions(role, permission) VALUES ('automation_client', 'queue_job') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('automation_client', 'cancel_job') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('automation_client', 'retry_job') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('automation_client', 'edit_job_deps') ON CONFLICT DO NOTHING",
    "INSERT INTO role_permissions(role, permission) VALUES ('automation_client', 'resolve_job') ON CONFLICT DO NOTHING",
    # Additive migration (job #2970): an optional caller-supplied idempotency
    # key so a job creation can be safely retried on a cadence beyond
    # find_recent_duplicate's 10-second window. NULL keys never collide, so
    # omitting it leaves existing create/create_batch behavior unchanged.
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS idempotency_key TEXT",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_jobs_project_idempotency_key "
    "ON jobs(project_id, idempotency_key) WHERE idempotency_key IS NOT NULL",
)

# Default role→permission grants, matching the hardcoded frontend PERMISSIONS constant.
# Used to seed an empty role_permissions table on first boot.
_SEED_PERMISSIONS: dict[str, list[str]] = {
    "viewer": ["propose_backlog", "deploy.view"],
    "release_manager": ["deploy.promote", "deploy.offer", "deploy.view"],
    "prod_promoter": ["deploy.promote", "deploy.offer", "deploy.view", "deploy.promote_prod"],
    "client_approver": ["deploy.apply", "deploy.view"],
    "contributor": [
        "queue_job",
        "cancel_job",
        "retry_job",
        "archive_job",
        "edit_job_deps",
        "resolve_job",
        "propose_backlog",
    ],
    "automation_client": [
        "queue_job",
        "cancel_job",
        "retry_job",
        "edit_job_deps",
        "resolve_job",
    ],
    "project_admin": [
        "queue_job",
        "cancel_job",
        "retry_job",
        "archive_job",
        "edit_project",
        "edit_agents",
        "edit_job_deps",
        "resolve_job",
        "manage_members",
        "delete_project",
        "propose_backlog",
        "triage_backlog",
        "manage_webhooks",
        "manage_api_tokens",
        "deploy.promote",
        "deploy.offer",
    ],
    "platform_admin": [
        "queue_job",
        "cancel_job",
        "retry_job",
        "archive_job",
        "edit_project",
        "edit_agents",
        "edit_job_deps",
        "resolve_job",
        "manage_members",
        "delete_project",
        "create_project",
        "manage_users",
        "manage_invitations",
        "manage_roles",
        "manage_providers",
        "view_fleet",
        "view_audit",
        "propose_backlog",
        "triage_backlog",
        "manage_webhooks",
        "manage_api_tokens",
        "deploy.promote",
        "deploy.offer",
        "deploy.promote_prod",
        "deploy.apply",
        "deploy.view",
    ],
}

_KNOWN_ROLES: frozenset[str] = frozenset(_SEED_PERMISSIONS)
_KNOWN_PERMISSIONS: frozenset[str] = frozenset(
    p for perms in _SEED_PERMISSIONS.values() for p in perms
)
_PLATFORM_ADMIN_LOCKED_PERMISSIONS: frozenset[str] = frozenset(
    {
        "manage_users",
        "manage_invitations",
        "manage_roles",
        "manage_providers",
        "view_fleet",
    }
)

# Transaction-level advisory lock keys (arbitrary, app-private constants).
_LOCK_SCHEMA = 0x68797173_0001  # serialize one-time CREATE TABLE IF NOT EXISTS
_LOCK_CLAIM = 0x68797173_0002  # serialize the claim decision across all workers
_LOCK_SUPERVISOR = 0x68797173_0003  # session-level; exactly one Supervisor leader fleet-wide
_LOCK_JOB_DEPENDENCIES = 0x68797173_0004  # serialize single-edge dependency inserts fleet-wide

# Dependency edges are durable scheduling facts. Parent lifecycle states and
# archival never consume an edge; successful completion or an explicit
# remediation resolution satisfies it.
_DEPENDENCY_SATISFIED_STATUS = JobStatus.DONE.value
_DEPENDENCY_SATISFIED_RESOLUTION = "resolved"

# Bound on how many SQL-eligible candidates claimable() re-ranks by effective
# priority; the base ORDER BY already puts the likeliest winners first.
_CLAIMABLE_CANDIDATE_LIMIT = 50

_SCHEMA_VERSION = "5"

# Digest of the DDL replayed by _ensure_schema. Stored in meta['schema_hash']
# once applied so a later JobStore() against an unchanged schema can skip the
# entire DDL pass (each "ADD COLUMN IF NOT EXISTS" still takes an
# AccessExclusiveLock even as a no-op, which can deadlock against a live
# worker transaction holding a conflicting lock order).
_SCHEMA_HASH = hashlib.sha256("\n".join(_SCHEMA).encode()).hexdigest()

# Attempts for the DDL replay retry loop: each attempt bounds its wait for
# conflicting locks with 'SET LOCAL lock_timeout' so a migration can never
# queue an AccessExclusiveLock indefinitely behind a long-running transaction.
_SCHEMA_DDL_MAX_ATTEMPTS = 5
_SCHEMA_DDL_RETRY_DELAY = 1.0

# Sentinel for update_project to distinguish "not provided" from "explicitly None".
_UNSET = object()

# The audit actor for the current task/request context. Read by
# JobStore._connection and injected as the per-transaction 'hyqs.actor' setting,
# which the hyqs_audit trigger stamps into created_by/updated_by and audit_log.
# ContextVar semantics give each asyncio task / web request its own value.
_db_actor: contextvars.ContextVar[str] = contextvars.ContextVar("hyqs_db_actor", default="")


def set_db_actor(actor: str) -> None:
    """Record who is behind subsequent DB writes in this context.

    Conventions: ``user:<email>`` (web), ``worker:<host:pid:n> job:<id>``
    (pipeline), ``supervisor:<id>`` (janitor), ``system:<service>`` (startup),
    ``test:pytest``. Unset contexts fall back to ``db:<session_user>`` in the
    trigger, so nothing is ever unattributed.
    """
    _db_actor.set(actor)


def get_db_actor() -> str:
    return _db_actor.get()


class JobStore:
    def __init__(self, dsn: str, *, max_size: int = 10) -> None:
        if not dsn:
            raise ValueError(
                "JobStore needs a Postgres DSN (set HYQS_DB_URL). "
                "Bring one up with `docker compose -f deploy/docker-compose.yml up -d`."
            )
        self._dsn = dsn
        self._pool = ConnectionPool(
            dsn,
            min_size=1,
            max_size=max_size,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=False,
        )
        self._pool.open()
        self._pool.wait(timeout=30)
        self._ensure_schema()

    @contextlib.contextmanager
    def _connection(self):
        """Pool connection with the audit actor bound for this transaction.

        The single choke point for all store SQL: sets 'hyqs.actor' (transaction-
        local) from the set_db_actor contextvar so the hyqs_audit trigger can
        attribute every write. SQL added anywhere in the store is covered
        automatically — never call self._pool.connection() directly.
        """
        with self._pool.connection() as conn:
            actor = _db_actor.get()
            if actor:
                conn.execute("SELECT set_config('hyqs.actor', %s, true)", (actor,))
            yield conn

    def _stored_schema_hash(self) -> str | None:
        """The schema_hash recorded in meta, or None if unset/table absent."""
        try:
            value = self.get_meta("schema_hash")
        except psycopg.errors.UndefinedTable:
            return None
        return value or None

    def _run_schema_ddl(self, conn) -> None:
        for stmt in _SCHEMA:
            conn.execute(stmt)

    def _ensure_schema(self) -> None:
        if self._stored_schema_hash() == _SCHEMA_HASH:
            # Already migrated to this exact schema — skip DDL entirely so a
            # fresh JobStore() boot never contends for AccessExclusiveLocks
            # against a live worker transaction.
            self._seed_role_permissions()
            return
        for attempt in range(1, _SCHEMA_DDL_MAX_ATTEMPTS + 1):
            try:
                with self._connection() as conn:
                    # One worker at a time runs DDL; the rest no-op safely.
                    conn.execute("SET LOCAL lock_timeout = '5s'")
                    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_SCHEMA,))
                    # Re-check under the lock: another process may have just
                    # finished migrating and committed before releasing it.
                    if self._stored_schema_hash() == _SCHEMA_HASH:
                        break
                    self._run_schema_ddl(conn)
                    conn.execute(
                        "INSERT INTO meta(key, value) VALUES ('schema_hash', %s) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (_SCHEMA_HASH,),
                    )
                    conn.execute(
                        "INSERT INTO meta(key, value) VALUES ('schema_version', %s) "
                        "ON CONFLICT(key) DO NOTHING",
                        (_SCHEMA_VERSION,),
                    )
                break
            except (psycopg.errors.LockNotAvailable, psycopg.errors.DeadlockDetected):
                # LockNotAvailable is our own 'lock_timeout' giving up; DeadlockDetected
                # is Postgres's deadlock detector picking this transaction as the
                # victim of a lock-order cycle against a concurrent worker transaction
                # (e.g. its audit_log insert vs. our AccessExclusiveLock DDL). Both are
                # transient contention — the advisory lock guarantees only one schema
                # replay runs at a time, so a retry finds the schema already migrated
                # or the conflicting transaction gone.
                if attempt == _SCHEMA_DDL_MAX_ATTEMPTS:
                    raise
                time.sleep(_SCHEMA_DDL_RETRY_DELAY)
        self._seed_role_permissions()

    def _seed_role_permissions(self) -> None:
        """Idempotent: ensure all default role→permission rows exist."""
        with self._connection() as conn:
            for role, permissions in _SEED_PERMISSIONS.items():
                for perm in permissions:
                    conn.execute(
                        "INSERT INTO role_permissions(role, permission) VALUES (%s, %s) "
                        "ON CONFLICT DO NOTHING",
                        (role, perm),
                    )

    _DEFAULT_AGENT_TASKS = json.dumps(ALL_TASKS)

    def _insert_default_agent(self, conn: psycopg.Connection, project_id: int, now: str) -> None:
        """Insert the implicit 'Claude (default)' agent — runs inside ``conn``'s txn."""
        conn.execute(
            "INSERT INTO project_agents(project_id, name, provider, model, allowed_tasks, "
            "max_concurrency, enabled, created_at, updated_at) "
            "VALUES (%s, 'Claude (default)', 'claude', '', %s, 1, TRUE, %s, %s)",
            (project_id, self._DEFAULT_AGENT_TASKS, now, now),
        )

    def _create_job_conn(
        self,
        conn: psycopg.Connection,
        *,
        idea: str,
        repo_path: str,
        chat_id: int,
        project_id: int,
        epic_id: int | None = None,
        priority: int = 0,
        source: JobSource = JobSource.UNKNOWN,
        source_actor: str = "",
        source_meta: dict | None = None,
        title: str = "",
        initial_stage: Stage = Stage.QUEUED,
        idempotency_key: str | None = None,
    ) -> int:
        """Create a job inside the caller's transaction and return its id."""
        from .models import _now

        if epic_id is not None:
            row = conn.execute("SELECT * FROM epics WHERE id = %s", (epic_id,)).fetchone()
            if row is None:
                raise ValueError(f"epic {epic_id} not found")
            if row["project_id"] != project_id:
                raise ValueError(
                    f"epic {epic_id} belongs to project {row['project_id']}, not {project_id}"
                )
        if not title:
            title = idea.split("\n")[0].strip()[:80]
        job_source_meta = dict(source_meta) if source_meta else {}
        if "scope" not in job_source_meta:
            paths = extract_scope_from_idea(idea)
            if paths:
                job_source_meta["scope"] = {"allowed_paths": paths, "interfaces": ""}
        now = _now()
        row = conn.execute(
            "INSERT INTO jobs(idea, title, repo_path, chat_id, project_id, epic_id, priority, "
            "source, source_actor, source_meta, stage, created_at, updated_at, idempotency_key) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                idea,
                title,
                repo_path,
                chat_id,
                project_id,
                epic_id,
                priority,
                source.value,
                source_actor,
                json.dumps(job_source_meta),
                initial_stage.value,
                now,
                now,
                idempotency_key,
            ),
        ).fetchone()
        return int(row["id"])

    @staticmethod
    def _add_job_dependency_conn(
        conn: psycopg.Connection,
        job_id: int,
        depends_on_job_id: int,
        provenance: DependencyProvenance = DependencyProvenance.USER,
    ) -> bool:
        """Add a dependency inside the caller's transaction."""
        row = conn.execute(
            "INSERT INTO job_dependencies(job_id, depends_on_job_id, provenance) "
            "VALUES (%s, %s, %s) ON CONFLICT (job_id, depends_on_job_id) DO UPDATE SET "
            "provenance = CASE "
            "WHEN job_dependencies.provenance = 'user' OR EXCLUDED.provenance = 'user' THEN 'user' "
            "WHEN job_dependencies.provenance = 'semantic' OR EXCLUDED.provenance = 'semantic' "
            "THEN 'semantic' ELSE 'auto' END "
            "RETURNING (xmax = 0) AS inserted",
            (job_id, depends_on_job_id, provenance.value),
        ).fetchone()
        return bool(row["inserted"])

    @staticmethod
    def _dependency_event_conn(
        conn: psycopg.Connection,
        job_id: int,
        action: str,
        dependency_id: int,
        provenance: str,
        reason: str,
    ) -> None:
        conn.execute(
            "INSERT INTO job_events(job_id, stage, status, summary, detail) "
            "VALUES (%s, 'scheduler', 'info', %s, %s)",
            (
                job_id,
                f"auto dependency {action}: #{dependency_id}",
                json.dumps(
                    {
                        "action": action,
                        "depends_on_job_id": dependency_id,
                        "provenance": provenance,
                        "reason": reason,
                    }
                ),
            ),
        )

    @staticmethod
    def _dependency_topo_order(
        nodes: Iterable[int], edges: dict[int, set[int]]
    ) -> list[int] | None:
        """Kahn's-algorithm topological order over ``nodes``, or None if ``edges`` cycle.

        ``edges[u]`` is the set of nodes ``u`` depends on (must be ordered after
        them). A node depending on itself is the one-node case of a cycle and is
        rejected the same way. Shared by ``create_batch`` (ordering + cycle
        detection for a whole new wave) and ``add_job_dependency`` (cycle
        detection for one proposed edge against the persisted graph) so both
        paths agree on what counts as a cycle.
        """
        node_list = list(nodes)
        node_set = set(node_list)
        in_degree = {n: 0 for n in node_set}
        adj: dict[int, list[int]] = {n: [] for n in node_set}
        for u in node_list:
            for v in edges.get(u, ()):
                if v not in node_set:
                    continue
                adj[v].append(u)
                in_degree[u] += 1
        queue = deque(n for n in node_list if in_degree[n] == 0)
        order: list[int] = []
        while queue:
            v = queue.popleft()
            order.append(v)
            for u in adj[v]:
                in_degree[u] -= 1
                if in_degree[u] == 0:
                    queue.append(u)
        return order if len(order) == len(node_set) else None

    def reconcile_agent_tasks(self) -> list[dict]:
        """Auto-grant any AgentTask values missing from enabled agents.

        Idempotent: running twice produces no second update. Called at startup
        so adding a new AgentTask to models.py never silently wedges the
        pipeline by leaving existing agents incapable of the new stage.
        Returns one dict per modified agent: {agent_id, name, added}.
        """
        from .models import _now

        results = []
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM project_agents WHERE enabled = TRUE").fetchall()
            for row in rows:
                agent = AgentSpec.from_row(row)
                missing = [t for t in ALL_TASKS if t not in agent.allowed_tasks]
                if not missing:
                    continue
                updated_tasks = self._clean_tasks(agent.allowed_tasks + missing)
                conn.execute(
                    "UPDATE project_agents SET allowed_tasks=%s, updated_at=%s WHERE id=%s",
                    (json.dumps(updated_tasks), _now(), agent.id),
                )
                results.append({"agent_id": agent.id, "name": agent.name, "added": missing})
        return results

    def create(
        self,
        idea: str,
        repo_path: str,
        chat_id: int,
        epic_id: int | None = None,
        depends_on: list[int] | None = None,
        priority: int = 0,
        source: JobSource = JobSource.UNKNOWN,
        source_actor: str = "",
        source_meta: dict | None = None,
        title: str = "",
        initial_stage: Stage = Stage.QUEUED,
        idempotency_key: str | None = None,
    ) -> Job:
        project = self.ensure_project_for_repo(repo_path)
        try:
            with self._connection() as conn:
                job_id = self._create_job_conn(
                    conn,
                    idea=idea,
                    repo_path=repo_path,
                    chat_id=chat_id,
                    project_id=project.id,
                    epic_id=epic_id,
                    priority=priority,
                    source=source,
                    source_actor=source_actor,
                    source_meta=source_meta,
                    title=title,
                    initial_stage=initial_stage,
                    idempotency_key=idempotency_key,
                )
                if depends_on:
                    for dep_id in depends_on:
                        if dep_id >= job_id:
                            raise ValueError(
                                f"depends_on must reference an earlier job (dep {dep_id} >= new job {job_id})"
                            )
                        self._add_job_dependency_conn(conn, job_id, dep_id)
        except psycopg.errors.UniqueViolation:
            if idempotency_key is not None:
                existing = self.get_by_idempotency_key(project.id, idempotency_key)
                if existing is not None:
                    return existing
            raise
        return self.get(job_id)  # type: ignore[return-value]

    def create_batch(
        self,
        repo_path: str,
        jobs_spec: list[dict],
        chat_id: int,
        source: JobSource = JobSource.UNKNOWN,
        source_actor: str = "",
        source_meta: dict | None = None,
    ) -> list[Job]:
        from .models import _now

        n = len(jobs_spec)
        if n == 0:
            return []

        for spec in jobs_spec:
            for idx in spec.get("depends_on") or []:
                if not isinstance(idx, int) or not (0 <= idx < n):
                    raise ValueError(f"depends_on index {idx} out of range for batch of size {n}")

        project = self.ensure_project_for_repo(repo_path)

        # Build mutable dependency sets (batch indexes)
        deps: list[set[int]] = [set(spec.get("depends_on") or []) for spec in jobs_spec]

        declared_deps = [set(values) for values in deps]
        scopes: dict[int, object] = {}
        for index, spec in enumerate(jobs_spec):
            scope = spec.get("scope")
            if not classify_dependency_scope(scope).known and spec.get("target_files"):
                scope = {"allowed_paths": spec["target_files"]}
            scopes[index] = scope
        for current in range(n):
            current_scope = classify_dependency_scope(scopes[current])
            if not current_scope.known:
                continue
            for prior in range(current):
                prior_scope = classify_dependency_scope(scopes[prior])
                if prior_scope.known and check_manifest_conflict(
                    list(current_scope.paths), list(prior_scope.paths)
                ):
                    deps[current].add(prior)

        minimized = minimize_auto_dependencies(
            scopes,
            (
                (
                    batch_idx,
                    dependency_idx,
                    (
                        DependencyProvenance.USER.value
                        if dependency_idx in declared_deps[batch_idx]
                        else DependencyProvenance.AUTO.value
                    ),
                )
                for batch_idx in range(n)
                for dependency_idx in deps[batch_idx]
            ),
        )
        deps = [set() for _index in range(n)]
        edge_provenance: dict[tuple[int, int], DependencyProvenance] = {}
        for batch_idx, dependency_idx, provenance in minimized:
            deps[batch_idx].add(dependency_idx)
            edge_provenance[(batch_idx, dependency_idx)] = DependencyProvenance(provenance)

        topo_order = self._dependency_topo_order(range(n), {i: deps[i] for i in range(n)})
        if topo_order is None:
            raise ValueError("dependency cycle")

        now = _now()
        with self._connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_JOB_DEPENDENCIES,))
            # Validate all epic_ids before touching jobs
            for spec in jobs_spec:
                eid = spec.get("epic_id")
                if eid is not None:
                    row = conn.execute("SELECT * FROM epics WHERE id = %s", (eid,)).fetchone()
                    if row is None:
                        raise ValueError(f"epic {eid} not found")
                    if row["project_id"] != project.id:
                        raise ValueError(
                            f"epic {eid} belongs to project {row['project_id']}, not {project.id}"
                        )

            # Insert jobs in topo order within this single transaction
            batch_to_job_id: dict[int, int] = {}
            deduped_batch_idxs: set[int] = set()

            for batch_idx in topo_order:
                spec = jobs_spec[batch_idx]
                idempotency_key = spec.get("idempotency_key") or None
                if idempotency_key is not None:
                    # Also catches a key reused twice within the same wave: earlier
                    # inserts in this transaction are visible to this SELECT.
                    existing = conn.execute(
                        "SELECT id FROM jobs WHERE project_id = %s AND idempotency_key = %s",
                        (project.id, idempotency_key),
                    ).fetchone()
                    if existing is not None:
                        batch_to_job_id[batch_idx] = int(existing["id"])
                        deduped_batch_idxs.add(batch_idx)
                        continue

                idea = spec["idea"]
                title = spec.get("title") or idea.split("\n")[0].strip()[:80]
                epic_id = spec.get("epic_id")
                priority = spec.get("priority", 0)
                job_source_meta = dict(source_meta or {})
                if spec.get("scope"):
                    job_source_meta["scope"] = spec["scope"]
                elif "scope" not in job_source_meta:
                    paths = extract_scope_from_idea(idea)
                    if paths:
                        job_source_meta["scope"] = {"allowed_paths": paths, "interfaces": ""}

                row = conn.execute(
                    "INSERT INTO jobs(idea, title, repo_path, chat_id, project_id, epic_id, "
                    "priority, source, source_actor, source_meta, created_at, updated_at, "
                    "idempotency_key) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (
                        idea,
                        title,
                        repo_path,
                        chat_id,
                        project.id,
                        epic_id,
                        priority,
                        source.value,
                        source_actor,
                        json.dumps(job_source_meta),
                        now,
                        now,
                        idempotency_key,
                    ),
                ).fetchone()
                job_id = int(row["id"])
                batch_to_job_id[batch_idx] = job_id

            # Insert dependency rows (topo order guarantees dep IDs are lower).
            # Skip edges *from* a deduped entry — it's a pre-existing job whose
            # own dependency graph this wave must not mutate. Edges *to* it from
            # other new jobs in the wave are inserted normally below.
            for batch_idx in topo_order:
                if batch_idx in deduped_batch_idxs:
                    continue
                job_id = batch_to_job_id[batch_idx]
                for dep_batch_idx in deps[batch_idx]:
                    dep_job_id = batch_to_job_id[dep_batch_idx]
                    provenance = edge_provenance[(batch_idx, dep_batch_idx)]
                    inserted = self._add_job_dependency_conn(conn, job_id, dep_job_id, provenance)
                    if inserted and provenance == DependencyProvenance.AUTO:
                        self._dependency_event_conn(
                            conn,
                            job_id,
                            "added",
                            dep_job_id,
                            provenance.value,
                            "overlapping_frozen_manifests",
                        )

        return [self.get(batch_to_job_id[i]) for i in range(n)]  # type: ignore[return-value]

    def survey_active_job_queue(self, project_id: int, candidates: list[dict]) -> QueueSurveyResult:
        """Check candidate jobs' target files for file-path collisions against
        this project's active queue, before any of the candidates are created.

        The pre-creation counterpart to ``create_batch``'s same-batch hot-file
        auto-chaining: that only sees jobs submitted together in one call, and
        ``_manifest_conflicts_with_running`` only compares against RUNNING jobs
        at claim time. This surveys the full active queue (pending/running/
        deploying — same status set ``list_active(status="active")`` already
        exposes) up front. Delegates the actual comparison to the pure
        ``collision.survey_job_queue``.
        """
        active_jobs = [
            {
                "id": job.id,
                "idea": job.idea,
                "plan": job.plan,
                "source_meta": job.source_meta,
            }
            for job in self.list_active(1000, project_id=project_id, status="active")
        ]
        return survey_job_queue(candidates, active_jobs)

    def reconcile_plan_scope(self, job_id: int, idea: str, plan: dict) -> list[str]:
        """Union the plan's own declared target_files into the job's scope manifest.

        Proactive counterpart to ``expand_job_scope``'s reactive escape hatch: called
        right after PLAN succeeds so ``source_meta.scope.allowed_paths`` already
        includes every path ``job_declared_scope_paths`` (idea's "Target files:" plus
        every story's ``target_files``) declares, before the out-of-lane gate ever runs
        in REVIEW/FIX. This is deterministic reconciliation, not a self-declared grant —
        it does not touch ``scope.expansions``. No-op (returns ``[]``, no write) if the
        manifest already covers every declared path.
        """
        from .models import _now

        with self._connection() as conn:
            row = conn.execute("SELECT source_meta FROM jobs WHERE id = %s", (job_id,)).fetchone()
            if row is None:
                return []
            meta = dict(row["source_meta"] or {})
            scope = dict(meta.get("scope") or {})
            allowed_paths = list(scope.get("allowed_paths") or [])
            added = [p for p in job_declared_scope_paths(idea, plan) if p not in allowed_paths]
            if not added:
                return []
            scope["allowed_paths"] = allowed_paths + added
            meta["scope"] = scope
            conn.execute(
                "UPDATE jobs SET source_meta = %s, updated_at = %s WHERE id = %s",
                (json.dumps(meta), _now(), job_id),
            )
        return added

    def freeze_validated_plan_scope(
        self,
        job_id: int,
        manifest: list[str],
        planning_metadata: dict | None = None,
    ) -> None:
        """Atomically freeze an exact validated manifest and merge safe PLAN metadata.

        The JSON shape is additive for mixed-worker compatibility. Callers are
        responsible for supplying only non-sensitive scalar/list observations.
        """
        from .models import _now

        normalized = list(dict.fromkeys(manifest))
        with self._connection() as conn:
            row = conn.execute(
                "SELECT source_meta FROM jobs WHERE id = %s FOR UPDATE", (job_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"job {job_id} not found")
            meta = dict(row["source_meta"] or {})
            old_scope = dict(meta.get("scope") or {})
            meta["scope"] = {
                **old_scope,
                "allowed_paths": normalized,
                "frozen": True,
            }
            if planning_metadata is not None:
                existing = dict(meta.get("planning") or {})
                existing.update(planning_metadata)
                meta["planning"] = existing
            conn.execute(
                "UPDATE jobs SET source_meta = %s, updated_at = %s WHERE id = %s",
                (json.dumps(meta), _now(), job_id),
            )

    def append_scope_amendment(
        self,
        job_id: int,
        decision: ScopeAmendmentDecision,
        *,
        authorizing_gate: str,
        failure_event_id: str,
        expected_prior_sequence: int,
        cumulative_limit: int,
    ) -> ScopeAmendmentPersistenceResult:
        """Atomically append one evaluated scope amendment to a job's audit ledger.

        The job row lock makes the expected-sequence and cumulative-budget checks
        part of the same transaction as the manifest widening. Legacy jobs have
        sequence zero and an empty ledger. Event identity is durable idempotency:
        retrying an already-recorded event never depends on the caller's now-stale
        expected sequence.
        """
        from .models import _now

        if expected_prior_sequence < 0:
            raise ValueError("expected_prior_sequence must be non-negative")
        if cumulative_limit < 0:
            raise ValueError("cumulative_limit must be non-negative")
        if not authorizing_gate:
            raise ValueError("authorizing_gate must not be empty")
        if not failure_event_id:
            raise ValueError("failure_event_id must not be empty")

        with self._connection() as conn:
            row = conn.execute(
                "SELECT source_meta FROM jobs WHERE id = %s FOR UPDATE", (job_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"job {job_id} not found")

            meta = dict(row["source_meta"] or {})
            scope = dict(meta.get("scope") or {})
            allowed_paths = list(dict.fromkeys(scope.get("allowed_paths") or []))
            amendment_meta = dict(meta.get("scope_amendments") or {})
            ledger = list(amendment_meta.get("ledger") or [])
            sequence = int(amendment_meta.get("sequence") or 0)
            cumulative_paths = list(dict.fromkeys(amendment_meta.get("cumulative_paths") or []))

            duplicate = next(
                (entry for entry in ledger if entry.get("failure_event_id") == failure_event_id),
                None,
            )
            if duplicate is not None:
                return ScopeAmendmentPersistenceResult(
                    status="idempotent",
                    sequence=sequence,
                    newly_accepted_paths=(),
                    cumulative_amendment_paths=tuple(cumulative_paths),
                    cumulative_allowed_paths=tuple(allowed_paths),
                    reason="duplicate_event",
                )

            if sequence != expected_prior_sequence:
                return ScopeAmendmentPersistenceResult(
                    status="rejected",
                    sequence=sequence,
                    newly_accepted_paths=(),
                    cumulative_amendment_paths=tuple(cumulative_paths),
                    cumulative_allowed_paths=tuple(allowed_paths),
                    reason="stale_sequence",
                )

            accepted_by_path = {
                item.path: item for item in decision.accepted if item.path not in allowed_paths
            }
            newly_accepted = list(accepted_by_path)
            rejected = [item.to_dict() for item in decision.rejected]
            if not newly_accepted and not rejected:
                return ScopeAmendmentPersistenceResult(
                    status="idempotent",
                    sequence=sequence,
                    newly_accepted_paths=(),
                    cumulative_amendment_paths=tuple(cumulative_paths),
                    cumulative_allowed_paths=tuple(allowed_paths),
                    reason="already_allowed",
                )

            next_cumulative_paths = cumulative_paths + [
                path for path in newly_accepted if path not in cumulative_paths
            ]
            if len(next_cumulative_paths) > cumulative_limit:
                return ScopeAmendmentPersistenceResult(
                    status="rejected",
                    sequence=sequence,
                    newly_accepted_paths=(),
                    cumulative_amendment_paths=tuple(cumulative_paths),
                    cumulative_allowed_paths=tuple(allowed_paths),
                    reason="cumulative_limit",
                )

            next_sequence = sequence + 1
            next_allowed_paths = allowed_paths + newly_accepted
            entry = {
                "sequence": next_sequence,
                "authorizing_gate": authorizing_gate,
                "failure_event_id": failure_event_id,
                "accepted": [accepted_by_path[path].to_dict() for path in newly_accepted],
                "rejected": rejected,
                "newly_accepted_paths": newly_accepted,
                "cumulative_amendment_paths": next_cumulative_paths,
                "cumulative_amendment_count": len(next_cumulative_paths),
                "cumulative_allowed_paths": next_allowed_paths,
                "cumulative_allowed_count": len(next_allowed_paths),
            }
            scope["allowed_paths"] = next_allowed_paths
            meta["scope"] = scope
            meta["scope_amendments"] = {
                **amendment_meta,
                "sequence": next_sequence,
                "cumulative_paths": next_cumulative_paths,
                "cumulative_count": len(next_cumulative_paths),
                "ledger": ledger + [entry],
            }
            conn.execute(
                "UPDATE jobs SET source_meta = %s, updated_at = %s WHERE id = %s",
                (json.dumps(meta), _now(), job_id),
            )

        return ScopeAmendmentPersistenceResult(
            status="applied",
            sequence=next_sequence,
            newly_accepted_paths=tuple(newly_accepted),
            cumulative_amendment_paths=tuple(next_cumulative_paths),
            cumulative_allowed_paths=tuple(next_allowed_paths),
        )

    def merge_planning_metadata(self, job_id: int, metadata: dict) -> None:
        """Additively merge non-sensitive planning observations into source_meta."""
        from .models import _now

        with self._connection() as conn:
            row = conn.execute(
                "SELECT source_meta FROM jobs WHERE id = %s FOR UPDATE", (job_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"job {job_id} not found")
            meta = dict(row["source_meta"] or {})
            planning = dict(meta.get("planning") or {})
            planning.update(metadata)
            meta["planning"] = planning
            conn.execute(
                "UPDATE jobs SET source_meta = %s, updated_at = %s WHERE id = %s",
                (json.dumps(meta), _now(), job_id),
            )

    def spend_plan_correction(self, job_id: int) -> bool:
        """Atomically spend the job's single durable PLAN correction budget."""
        from .models import _now

        with self._connection() as conn:
            row = conn.execute(
                "UPDATE jobs SET plan_reask_attempts=1, updated_at=%s "
                "WHERE id=%s AND plan_reask_attempts < 1 RETURNING id",
                (_now(), job_id),
            ).fetchone()
        return row is not None

    def mark_early_scope_correction_attempt(self, job_id: int) -> bool:
        """Atomically claim the one permitted early BUILD scope correction.

        The marker lives in ``source_meta`` because transient requeue clears the
        failure fields.  The conditional update makes concurrent/reclaimed workers
        agree on a single claimant while preserving every unrelated metadata key.
        """
        from .models import _now

        attempted_at = _now()
        marker = json.dumps({"attempted_at": attempted_at})
        with self._connection() as conn:
            row = conn.execute(
                "UPDATE jobs SET source_meta = source_meta || "
                "jsonb_build_object('early_scope_correction', %s::jsonb), updated_at = %s "
                "WHERE id = %s AND NOT (source_meta ? 'early_scope_correction') "
                "RETURNING id",
                (marker, attempted_at, job_id),
            ).fetchone()
        return row is not None

    def expand_job_scope(self, job_id: int, paths: list[str], justification: str) -> None:
        """Append *paths* to a job's scope manifest, recording *justification*.

        The FIX-stage escape hatch: when the architect under-scoped a job, the fixer
        may widen ``source_meta.scope.allowed_paths`` instead of being permanently
        stuck out-of-lane. ``save()`` never touches ``source_meta``, so this is a
        dedicated read-modify-write. No-op if the job doesn't exist.

        Before appending the newly granted ``paths``, the job's own declared scope
        (its plan's per-story ``target_files`` unioned with ``extract_scope_from_idea``)
        is unioned into ``allowed_paths`` first — idempotently, whether the manifest
        was previously unset or already populated. This guarantees a grant can never
        yield a manifest that excludes the job's own targets (job #1422: a grant
        applied to a job with no manifest created one containing only the granted
        paths, which activated out-of-lane enforcement against the job's own
        deliverable).
        """
        from .models import _now

        with self._connection() as conn:
            row = conn.execute(
                "SELECT idea, plan, source_meta FROM jobs WHERE id = %s", (job_id,)
            ).fetchone()
            if row is None:
                return
            meta = dict(row["source_meta"] or {})
            scope = dict(meta.get("scope") or {})
            allowed_paths = list(scope.get("allowed_paths") or [])
            plan = json.loads(row["plan"]) if row["plan"] else None
            for p in job_declared_scope_paths(row["idea"] or "", plan):
                if p not in allowed_paths:
                    allowed_paths.append(p)
            for p in paths:
                if p not in allowed_paths:
                    allowed_paths.append(p)
            scope["allowed_paths"] = allowed_paths
            expansions = list(scope.get("expansions") or [])
            expansions.append({"paths": paths, "justification": justification, "at": _now()})
            scope["expansions"] = expansions
            meta["scope"] = scope
            conn.execute(
                "UPDATE jobs SET source_meta = %s, updated_at = %s WHERE id = %s",
                (json.dumps(meta), _now(), job_id),
            )

    def find_recent_duplicate(
        self, repo_path: str, idea: str, within_seconds: int = 10
    ) -> Job | None:
        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=within_seconds)).isoformat()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE repo_path = %s AND idea = %s "
                "AND created_at >= %s "
                "AND status != 'cancelled' ORDER BY id DESC LIMIT 1",
                (repo_path, idea, cutoff),
            ).fetchone()
        return Job.from_row(row) if row else None

    def get_by_idempotency_key(self, project_id: int, idempotency_key: str) -> Job | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE project_id = %s AND idempotency_key = %s",
                (project_id, idempotency_key),
            ).fetchone()
        return Job.from_row(row) if row else None

    def get(self, job_id: int) -> Job | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()
        return Job.from_row(row) if row else None

    def save(self, job: Job) -> None:
        from .models import _now

        job.updated_at = _now()
        # A job reaching DONE succeeded; any failure/error text on it describes a
        # superseded attempt (e.g. a gate rejection the fix loop corrected) and
        # must not survive into the terminal row. Full history stays in job_events.
        error = "" if job.status == JobStatus.DONE else job.error
        failure = "" if job.status == JobStatus.DONE else job.failure
        executing_step = job.executing_step if job.status == JobStatus.RUNNING else None
        clear_failure = job.status == JobStatus.DONE
        with self._connection() as conn:
            saved = conn.execute(
                "UPDATE jobs SET title=%s, branch=%s, stage=%s, status=%s, plan=%s, review=%s, "
                "security_review=%s, design_review=%s, "
                "error=%s, attempts=%s, rebase_attempts=%s, rebase_retry_after=%s, "
                "timeout_attempts=%s, plan_reask_attempts=%s, "
                "failure=%s, project_id=%s, epic_id=%s, "
                "agent_id=%s, provider=%s, owner=%s, lease_until=%s, "
                "deployed_commit=%s, resolution=%s, implementation_summary=%s, "
                "merge_delta_sha=%s, executing_step=%s, failed_step=%s, failure_code=%s, "
                "failure_origin=%s, retry_disposition=%s, failure_detail=%s, updated_at=%s "
                "WHERE id=%s AND status NOT IN (%s, %s, %s) RETURNING source_meta",
                (
                    job.title,
                    job.branch,
                    job.stage.value,
                    job.status.value,
                    json.dumps(job.plan) if job.plan is not None else None,
                    json.dumps(job.review) if job.review is not None else None,
                    json.dumps(job.security_review) if job.security_review is not None else None,
                    json.dumps(job.design_review) if job.design_review is not None else None,
                    error,
                    job.attempts,
                    job.rebase_attempts,
                    job.rebase_retry_after,
                    job.timeout_attempts,
                    job.plan_reask_attempts,
                    failure,
                    job.project_id,
                    job.epic_id,
                    job.agent_id,
                    job.provider,
                    job.owner,
                    job.lease_until,
                    job.deployed_commit,
                    job.resolution,
                    job.implementation_summary,
                    job.merge_delta_sha,
                    executing_step,
                    None if clear_failure else job.failed_step,
                    None if clear_failure else job.failure_code,
                    None if clear_failure else job.failure_origin,
                    None if clear_failure else job.retry_disposition,
                    None
                    if clear_failure
                    else json.dumps(job.failure_detail, default=_json_safe_default)
                    if job.failure_detail is not None
                    else None,
                    job.updated_at,
                    job.id,
                    JobStatus.DONE.value,
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                ),
            ).fetchone()
            if job.status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
                self._clear_active_remediation_conn(conn, job.id)
            if job.status == JobStatus.DONE and saved is not None:
                self._propagate_successful_remediation_conn(conn, job.id, saved["source_meta"])

    def set_executing_step(self, job_id: int, step: str) -> None:
        """Record the actual operation about to run and retire prior failure state."""
        with self._connection() as conn:
            conn.execute(
                "UPDATE jobs SET executing_step=%s, failed_step=NULL, failure_code=NULL, "
                "failure_origin=NULL, retry_disposition=NULL, failure_detail=NULL "
                "WHERE id=%s AND status=%s",
                (step, job_id, JobStatus.RUNNING.value),
            )

    def clear_executing_step(self, job_id: int) -> None:
        with self._connection() as conn:
            conn.execute("UPDATE jobs SET executing_step=NULL WHERE id=%s", (job_id,))

    # --- project methods --------------------------------------------------

    def create_project(
        self,
        name: str,
        repo_path: str,
        description: str = "",
        stack: str = "",
        spec: dict | None = None,
        github_url: str = "",
    ) -> Project:
        from .models import _now

        now = _now()
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO projects(name, repo_path, description, stack, spec, github_url, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT(repo_path) DO NOTHING",
                (
                    name,
                    repo_path,
                    description,
                    stack,
                    json.dumps(spec) if spec is not None else None,
                    github_url,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM projects WHERE repo_path = %s", (repo_path,)
            ).fetchone()
            # Give a brand-new project its default agent (idempotent: only if none).
            has_agent = conn.execute(
                "SELECT 1 FROM project_agents WHERE project_id = %s LIMIT 1", (row["id"],)
            ).fetchone()
            if not has_agent:
                self._insert_default_agent(conn, row["id"], now)
        return Project.from_row(row)

    def create_project_exclusive(
        self,
        name: str,
        repo_path: str,
        description: str = "",
        stack: str = "",
        spec: dict | None = None,
        github_url: str = "",
    ) -> Project | None:
        """Atomically create a new project, or return None if repo_path is already registered."""
        from .models import _now

        now = _now()
        with self._connection() as conn:
            row = conn.execute(
                "INSERT INTO projects(name, repo_path, description, stack, spec, github_url, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT(repo_path) DO NOTHING RETURNING *",
                (
                    name,
                    repo_path,
                    description,
                    stack,
                    json.dumps(spec) if spec is not None else None,
                    github_url,
                    now,
                    now,
                ),
            ).fetchone()
            if row is None:
                return None
            self._insert_default_agent(conn, row["id"], now)
        return Project.from_row(row)

    def get_project(self, project_id: int) -> Project | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone()
        return Project.from_row(row) if row else None

    def get_project_by_repo(self, repo_path: str) -> Project | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE repo_path = %s", (repo_path,)
            ).fetchone()
        return Project.from_row(row) if row else None

    def ensure_project_for_repo(self, repo_path: str) -> Project:
        from pathlib import PurePath

        name = PurePath(repo_path).name or repo_path
        return self.create_project(name, repo_path)

    def list_projects(self) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT p.*, COUNT(j.id) AS job_count, MAX(j.updated_at) AS last_activity "
                "FROM projects p LEFT JOIN jobs j ON j.project_id = p.id "
                "GROUP BY p.id ORDER BY p.id DESC"
            ).fetchall()
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "repo_path": r["repo_path"],
                "description": r["description"] or "",
                "status": r["status"],
                "max_fix_attempts": r["max_fix_attempts"],
                "stack": r["stack"] or "",
                "spec": r["spec"],
                "deploy_config": r["deploy_config"] or "",
                "github_url": r.get("github_url", "") or "",
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "job_count": r["job_count"],
                "last_activity": r["last_activity"],
            }
            for r in rows
        ]

    def update_project(
        self,
        project_id: int,
        name: str | None = None,
        description: str | None = None,
        status: str | None = None,
        max_fix_attempts: object = _UNSET,
        stack: str | None = None,
        spec: object = _UNSET,
        deploy_config: str | None = None,
        github_url: str | None = None,
    ) -> Project | None:
        from .models import _now

        fields: dict[str, object] = {}
        if name is not None:
            fields["name"] = name
        if description is not None:
            fields["description"] = description
        if status is not None:
            fields["status"] = status
        if max_fix_attempts is not _UNSET:
            fields["max_fix_attempts"] = max_fix_attempts
        if stack is not None:
            fields["stack"] = stack
        if spec is not _UNSET:
            fields["spec"] = json.dumps(spec) if spec is not None else None
        if deploy_config is not None:
            fields["deploy_config"] = deploy_config
        if github_url is not None:
            fields["github_url"] = github_url
        if not fields:
            return self.get_project(project_id)

        fields["updated_at"] = _now()
        set_clause = ", ".join(f"{k}=%s" for k in fields)
        values = list(fields.values()) + [project_id]
        with self._connection() as conn:
            conn.execute(f"UPDATE projects SET {set_clause} WHERE id=%s", values)
            row = conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone()
        return Project.from_row(row) if row else None

    # --- epic methods -----------------------------------------------------

    def create_epic(self, project_id: int, name: str, description: str = "") -> Epic:
        from .models import _now

        now = _now()
        with self._connection() as conn:
            try:
                row = conn.execute(
                    "INSERT INTO epics(project_id, name, description, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING *",
                    (project_id, name, description, now, now),
                ).fetchone()
            except psycopg.errors.ForeignKeyViolation:
                raise ValueError(f"project {project_id} not found")
        return Epic.from_row(row)

    def get_epic(self, epic_id: int) -> Epic | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM epics WHERE id = %s", (epic_id,)).fetchone()
        return Epic.from_row(row) if row else None

    def list_epics(
        self, project_id: int | None = None, include_archived: bool = False
    ) -> list[dict]:
        archive_clause = "" if include_archived else " AND e.archived = FALSE"
        counts_select = (
            "COUNT(j.id) AS job_count, MAX(j.updated_at) AS last_activity, "
            "COUNT(j.id) FILTER (WHERE j.status = 'done') AS done_count, "
            "COUNT(j.id) FILTER (WHERE j.status = 'running') AS running_count, "
            "COUNT(j.id) FILTER (WHERE j.status = 'failed') AS failed_count "
        )
        # counts_select and archive_clause are constant literals (no user input
        # interpolated); the only user value, project_id, is passed separately
        # as a bound param to execute() below, so this is not injectable.
        with self._connection() as conn:
            if project_id is not None:
                rows = conn.execute(
                    f"SELECT e.*, {counts_select}"  # nosec B608
                    "FROM epics e LEFT JOIN jobs j ON j.epic_id = e.id "
                    f"WHERE e.project_id = %s{archive_clause} "
                    "GROUP BY e.id ORDER BY e.id DESC",
                    (project_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT e.*, {counts_select}"  # nosec B608
                    "FROM epics e LEFT JOIN jobs j ON j.epic_id = e.id "
                    f"WHERE TRUE{archive_clause} "
                    "GROUP BY e.id ORDER BY e.id DESC"
                ).fetchall()
        return [
            {
                "id": r["id"],
                "project_id": r["project_id"],
                "name": r["name"],
                "description": r["description"] or "",
                "status": r["status"],
                "archived": bool(r["archived"]) if r["archived"] is not None else False,
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "job_count": r["job_count"],
                "last_activity": r["last_activity"],
                "status_counts": {
                    "done": r["done_count"],
                    "running": r["running_count"],
                    "failed": r["failed_count"],
                },
            }
            for r in rows
        ]

    def list_epicless_jobs(self) -> list[int]:
        """Return IDs of non-archived jobs with no epic_id assigned."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE epic_id IS NULL AND archived = FALSE"
            ).fetchall()
        return [int(r["id"]) for r in rows]

    # --- environment methods -----------------------------------------------

    def create_environment(
        self,
        project_id: int,
        name: str,
        kind: str,
        status: str = "active",
        auto_deploy: bool = False,
    ) -> Environment:
        from .models import _now

        now = _now()
        with self._connection() as conn:
            try:
                row = conn.execute(
                    "INSERT INTO environments(project_id, name, kind, status, auto_deploy, "
                    "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
                    (project_id, name, kind, status, auto_deploy, now, now),
                ).fetchone()
            except psycopg.errors.ForeignKeyViolation:
                raise ValueError(f"project {project_id} not found")
        return Environment.from_row(row)

    def get_environment(self, environment_id: int) -> Environment | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM environments WHERE id = %s", (environment_id,)
            ).fetchone()
        return Environment.from_row(row) if row else None

    def list_environments(self, project_id: int) -> list[Environment]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM environments WHERE project_id = %s ORDER BY created_at, id",
                (project_id,),
            ).fetchall()
        return [Environment.from_row(r) for r in rows]

    def get_or_create_default_environment(self, project_id: int) -> Environment:
        """The project's implicit 'prod' environment, creating it lazily if absent.

        The one-time seed migration backfills 'prod' for projects that existed
        when it ran, but a project created afterward has none yet — so this
        cannot assume the row is already there.
        """
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM environments WHERE project_id = %s AND name = %s",
                (project_id, "prod"),
            ).fetchone()
        if row:
            return Environment.from_row(row)
        return self.create_environment(project_id, "prod", "prod", auto_deploy=True)

    def get_environment_config(self, environment_id: int) -> dict:
        """This environment's own deploy config, falling back to its project's
        `deploy_config` when the environment hasn't set one of its own — keeps
        pre-multi-env projects behaving exactly as before."""
        environment = self.get_environment(environment_id)
        if environment is None:
            raise ValueError(f"environment {environment_id} not found")
        if environment.config:
            return json.loads(environment.config)
        project = self.get_project(environment.project_id)
        if project is None or not project.deploy_config:
            return {}
        return json.loads(project.deploy_config)

    def set_environment_config(self, environment_id: int, config: dict) -> Environment:
        from .models import _now

        with self._connection() as conn:
            row = conn.execute(
                "UPDATE environments SET config = %s, updated_at = %s WHERE id = %s RETURNING *",
                (json.dumps(config), _now(), environment_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"environment {environment_id} not found")
        return Environment.from_row(row)

    def set_environment_auto_deploy(self, environment_id: int, auto_deploy: bool) -> Environment:
        from .models import _now

        with self._connection() as conn:
            row = conn.execute(
                "UPDATE environments SET auto_deploy = %s, updated_at = %s WHERE id = %s "
                "RETURNING *",
                (auto_deploy, _now(), environment_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"environment {environment_id} not found")
        return Environment.from_row(row)

    # --- host methods --------------------------------------------------------

    def create_host(self, name: str, public_key: str | None = None, status: str = "active") -> Host:
        from .models import _now

        now = _now()
        with self._connection() as conn:
            row = conn.execute(
                "INSERT INTO hosts(name, public_key, status, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING *",
                (name, public_key, status, now, now),
            ).fetchone()
        return Host.from_row(row)

    def get_host(self, host_id: int) -> Host | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM hosts WHERE id = %s", (host_id,)).fetchone()
        return Host.from_row(row) if row else None

    def get_host_by_name(self, name: str) -> Host | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM hosts WHERE name = %s", (name,)).fetchone()
        return Host.from_row(row) if row else None

    def get_host_by_public_key(self, public_key: str) -> Host | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM hosts WHERE public_key = %s", (public_key,)
            ).fetchone()
        return Host.from_row(row) if row else None

    def list_hosts(self) -> list[Host]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM hosts ORDER BY created_at, id").fetchall()
        return [Host.from_row(r) for r in rows]

    def set_environment_host(self, environment_id: int, host_id: int | None) -> Environment:
        from .models import _now

        with self._connection() as conn:
            try:
                row = conn.execute(
                    "UPDATE environments SET host_id = %s, updated_at = %s WHERE id = %s "
                    "RETURNING *",
                    (host_id, _now(), environment_id),
                ).fetchone()
            except psycopg.errors.ForeignKeyViolation:
                raise ValueError(f"host {host_id} not found")
        if row is None:
            raise ValueError(f"environment {environment_id} not found")
        return Environment.from_row(row)

    # --- release methods -----------------------------------------------------

    def create_release(
        self,
        project_id: int,
        source_commit: str,
        image_ref: str,
        image_digest: str | None,
        *,
        manifest: str | None = None,
        signature_ref: str | None = None,
        built_by: str = "",
    ) -> Release:
        from .models import _now

        now = _now()
        with self._connection() as conn:
            try:
                row = conn.execute(
                    "INSERT INTO releases(project_id, source_commit, image_ref, image_digest, "
                    "manifest, signature_ref, built_at, built_by, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                    (
                        project_id,
                        source_commit,
                        image_ref,
                        image_digest,
                        manifest,
                        signature_ref,
                        now,
                        built_by,
                        now,
                        now,
                    ),
                ).fetchone()
            except psycopg.errors.ForeignKeyViolation:
                raise ValueError(f"project {project_id} not found")
        return Release.from_row(row)

    def get_release(self, release_id: int) -> Release | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM releases WHERE id = %s", (release_id,)).fetchone()
        return Release.from_row(row) if row else None

    def get_release_by_digest(self, project_id: int, image_digest: str) -> Release | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM releases WHERE project_id = %s AND image_digest = %s "
                "ORDER BY built_at DESC LIMIT 1",
                (project_id, image_digest),
            ).fetchone()
        return Release.from_row(row) if row else None

    def list_releases(self, project_id: int) -> list[Release]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM releases WHERE project_id = %s ORDER BY built_at DESC",
                (project_id,),
            ).fetchall()
        return [Release.from_row(r) for r in rows]

    def set_environment_current_release(
        self, environment_id: int, release_id: int | None
    ) -> Environment:
        from .models import _now

        with self._connection() as conn:
            try:
                row = conn.execute(
                    "UPDATE environments SET current_release_id = %s, updated_at = %s "
                    "WHERE id = %s RETURNING *",
                    (release_id, _now(), environment_id),
                ).fetchone()
            except psycopg.errors.ForeignKeyViolation:
                raise ValueError(f"release {release_id} not found")
        if row is None:
            raise ValueError(f"environment {environment_id} not found")
        return Environment.from_row(row)

    # --- promotion methods -----------------------------------------------

    def create_promotion(
        self,
        project_id: int,
        release_id: int,
        target_env_id: int,
        kind: PromotionKind,
        *,
        source_env_id: int | None = None,
        requested_by: str = "",
    ) -> Promotion | None:
        """Insert a new promotion, or return None as a no-op when release_id
        already equals the target environment's current_release_id."""
        from .models import _now

        target_env = self.get_environment(target_env_id)
        if target_env is None:
            raise ValueError(f"environment {target_env_id} not found")
        if promotions.is_noop_promotion(release_id, target_env.current_release_id):
            return None

        state = promotions.initial_state(kind)
        now = _now()
        with self._connection() as conn:
            try:
                row = conn.execute(
                    "INSERT INTO promotions(project_id, release_id, source_env_id, "
                    "target_env_id, kind, state, requested_by, requested_at, "
                    "created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                    (
                        project_id,
                        release_id,
                        source_env_id,
                        target_env_id,
                        kind.value,
                        state.value,
                        requested_by,
                        now,
                        now,
                        now,
                    ),
                ).fetchone()
            except psycopg.errors.ForeignKeyViolation:
                raise ValueError(
                    f"unknown project {project_id}, release {release_id}, "
                    f"or target environment {target_env_id}"
                )
        return Promotion.from_row(row)

    def get_promotion(self, promotion_id: int) -> Promotion | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM promotions WHERE id = %s", (promotion_id,)).fetchone()
        return Promotion.from_row(row) if row else None

    def list_promotions(self, target_env_id: int) -> list[Promotion]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM promotions WHERE target_env_id = %s ORDER BY created_at DESC",
                (target_env_id,),
            ).fetchall()
        return [Promotion.from_row(r) for r in rows]

    def list_promotions_for_project(self, project_id: int) -> list[Promotion]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM promotions WHERE project_id = %s ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
        return [Promotion.from_row(r) for r in rows]

    def transition_promotion(
        self,
        promotion_id: int,
        target_state: PromotionState,
        *,
        approved_by: str | None = None,
        applied_by: str | None = None,
        deploy_job_id: int | None = None,
    ) -> Promotion:
        from .models import _now

        current = self.get_promotion(promotion_id)
        if current is None:
            raise ValueError(f"promotion {promotion_id} not found")
        promotions.validate_transition(current.state, target_state)

        now = _now()
        with self._connection() as conn:
            row = conn.execute(
                "UPDATE promotions SET state = %s, updated_at = %s, "
                "approved_by = COALESCE(%s, approved_by), "
                "applied_by = COALESCE(%s, applied_by), "
                "applied_at = COALESCE(%s, applied_at), "
                "deploy_job_id = COALESCE(%s, deploy_job_id) "
                "WHERE id = %s RETURNING *",
                (
                    target_state.value,
                    now,
                    approved_by,
                    applied_by,
                    now if applied_by is not None else None,
                    deploy_job_id,
                    promotion_id,
                ),
            ).fetchone()
        return Promotion.from_row(row)

    def supersede_prior_promotions(
        self, target_env_id: int, *, exclude_promotion_id: int | None = None
    ) -> list[Promotion]:
        from .models import _now

        with self._connection() as conn:
            rows = conn.execute(
                "UPDATE promotions SET state = %s, updated_at = %s "
                "WHERE target_env_id = %s "
                "AND state IN ('dispatched', 'available', 'deploying') "
                "AND id != COALESCE(%s, -1) "
                "RETURNING *",
                (
                    PromotionState.SUPERSEDED.value,
                    _now(),
                    target_env_id,
                    exclude_promotion_id,
                ),
            ).fetchall()
        return [Promotion.from_row(r) for r in rows]

    def promote_release(
        self,
        project_id: int,
        release_id: int,
        target_env_id: int,
        *,
        requested_by: str = "",
        chat_id: int = 0,
    ) -> Promotion | None:
        """Dispatch a release to an environment: create+supersede the promotion
        ledger row, then enqueue the DEPLOY-stage job it tracks. None as a
        no-op when release_id already equals the target environment's
        current_release_id."""
        from .models import _now

        target_env = self.get_environment(target_env_id)
        if target_env is None:
            raise ValueError(f"environment {target_env_id} not found")
        if promotions.is_noop_promotion(release_id, target_env.current_release_id):
            return None

        self.supersede_prior_promotions(target_env_id)
        promotion = self.create_promotion(
            project_id,
            release_id,
            target_env_id,
            PromotionKind.PROMOTE,
            requested_by=requested_by,
        )
        if promotion is None:
            return None

        project = self.get_project(project_id)
        job = self.create(
            idea=f"Promote release {release_id} to environment {target_env_id}",
            repo_path=project.repo_path,
            chat_id=chat_id,
            source=JobSource.MCP,
            source_actor=requested_by,
            source_meta={
                "environment_id": target_env_id,
                "release_id": release_id,
                "promotion_id": promotion.id,
            },
            initial_stage=Stage.DEPLOY,
        )

        with self._connection() as conn:
            row = conn.execute(
                "UPDATE promotions SET deploy_job_id = %s, updated_at = %s "
                "WHERE id = %s RETURNING *",
                (job.id, _now(), promotion.id),
            ).fetchone()
        return Promotion.from_row(row)

    def offer_release(
        self,
        project_id: int,
        release_id: int,
        target_env_id: int,
        *,
        requested_by: str = "",
    ) -> Promotion | None:
        """Make a release available for a client-kind environment's own
        approver to pull and apply. Unlike ``promote_release``, this never
        enqueues a deploy job — the client's ``hyqs-pipeline apply`` command
        does the cutover, entirely on the client host. None as a no-op when
        release_id already equals the target environment's current release."""
        target_env = self.get_environment(target_env_id)
        if target_env is None:
            raise ValueError(f"environment {target_env_id} not found")
        if promotions.is_noop_promotion(release_id, target_env.current_release_id):
            return None

        self.supersede_prior_promotions(target_env_id)
        return self.create_promotion(
            project_id,
            release_id,
            target_env_id,
            PromotionKind.OFFER,
            requested_by=requested_by,
        )

    def get_available_offer(self, environment_id: int) -> Promotion | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM promotions WHERE target_env_id = %s AND kind = %s "
                "AND state = %s ORDER BY created_at DESC LIMIT 1",
                (environment_id, PromotionKind.OFFER.value, PromotionState.AVAILABLE.value),
            ).fetchone()
        return Promotion.from_row(row) if row else None

    def list_environments_by_host(self, host_id: int) -> list[Environment]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM environments WHERE host_id = %s ORDER BY created_at, id",
                (host_id,),
            ).fetchall()
        return [Environment.from_row(r) for r in rows]

    def list_auto_deploy_environments(self) -> list[Environment]:
        """Every environment across all projects with auto_deploy=TRUE — the
        set the poller (#7b) will build-from-main for, once it consults this."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM environments WHERE auto_deploy = TRUE ORDER BY created_at, id"
            ).fetchall()
        return [Environment.from_row(r) for r in rows]

    def promotion_for_deploy_job(self, job_id: int) -> Promotion | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM promotions WHERE deploy_job_id = %s", (job_id,)
            ).fetchone()
        return Promotion.from_row(row) if row else None

    def advance_promotion_deploying(self, job_id: int) -> None:
        """Best-effort DISPATCHED -> DEPLOYING. No-op if unlinked or already
        past DISPATCHED."""
        promotion = self.promotion_for_deploy_job(job_id)
        if promotion is None or promotion.state != PromotionState.DISPATCHED:
            return
        self.transition_promotion(promotion.id, PromotionState.DEPLOYING)

    def advance_promotion_deployed(self, job_id: int, environment_id: int, release_id: int) -> None:
        """Best-effort -> DEPLOYED plus updating the environment's current
        release. No-op if unlinked or already terminal."""
        promotion = self.promotion_for_deploy_job(job_id)
        if promotion is None or promotions.is_terminal(promotion.state):
            return
        self.transition_promotion(promotion.id, PromotionState.DEPLOYED, applied_by=get_db_actor())
        self.set_environment_current_release(environment_id, release_id)

    def fail_promotion(self, job_id: int) -> None:
        """Best-effort -> FAILED. No-op if unlinked or already terminal."""
        promotion = self.promotion_for_deploy_job(job_id)
        if promotion is None or promotions.is_terminal(promotion.state):
            return
        self.transition_promotion(promotion.id, PromotionState.FAILED)

    def create_webhook(
        self,
        project_id: int,
        url: str,
        event_type: str,
        created_by: str = "",
        kind: str = "http",
    ) -> Webhook:
        from .models import _now

        now = _now()
        with self._connection() as conn:
            try:
                row = conn.execute(
                    "INSERT INTO webhooks(project_id, url, event_type, created_by, created_at, kind) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
                    (project_id, url, event_type, created_by, now, kind),
                ).fetchone()
            except psycopg.errors.ForeignKeyViolation:
                raise ValueError(f"project {project_id} not found")
        return Webhook.from_row(row)

    def list_webhooks(self, project_id: int) -> list[Webhook]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM webhooks WHERE project_id = %s ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
        return [Webhook.from_row(r) for r in rows]

    def get_webhook(self, webhook_id: int) -> Webhook | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM webhooks WHERE id = %s", (webhook_id,)).fetchone()
        return Webhook.from_row(row) if row else None

    def set_webhook_active(self, webhook_id: int, active: bool) -> Webhook:
        with self._connection() as conn:
            row = conn.execute(
                "UPDATE webhooks SET active = %s WHERE id = %s RETURNING *",
                (active, webhook_id),
            ).fetchone()
        return Webhook.from_row(row)

    def delete_webhook(self, webhook_id: int) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM webhooks WHERE id = %s", (webhook_id,))

    def set_project_slack_token(
        self, project_id: int, bot_token: str, created_by: str = ""
    ) -> None:
        from .crypto import encrypt_str
        from .models import _now

        now = _now()
        encrypted = encrypt_str(bot_token)
        with self._connection() as conn:
            try:
                conn.execute(
                    "INSERT INTO project_slack_credentials"
                    "(project_id, bot_token_encrypted, created_by, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (project_id) DO UPDATE SET "
                    "bot_token_encrypted = EXCLUDED.bot_token_encrypted, "
                    "updated_at = EXCLUDED.updated_at",
                    (project_id, encrypted, created_by, now, now),
                )
            except psycopg.errors.ForeignKeyViolation:
                raise ValueError(f"project {project_id} not found")

    def get_project_slack_token(self, project_id: int) -> str | None:
        from .crypto import decrypt_str

        with self._connection() as conn:
            row = conn.execute(
                "SELECT bot_token_encrypted FROM project_slack_credentials WHERE project_id = %s",
                (project_id,),
            ).fetchone()
        return decrypt_str(row["bot_token_encrypted"]) if row else None

    def get_slack_thread_ts(self, job_id: int, webhook_id: int) -> str | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT thread_ts FROM job_slack_threads WHERE job_id = %s AND webhook_id = %s",
                (job_id, webhook_id),
            ).fetchone()
        return row["thread_ts"] if row else None

    def set_slack_thread_ts(self, job_id: int, webhook_id: int, thread_ts: str) -> None:
        from .models import _now

        with self._connection() as conn:
            conn.execute(
                "INSERT INTO job_slack_threads(job_id, webhook_id, thread_ts, created_at) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (job_id, webhook_id) DO NOTHING",
                (job_id, webhook_id, thread_ts, _now()),
            )

    def update_epic(
        self,
        epic_id: int,
        name: str | None = None,
        description: str | None = None,
        status: str | None = None,
    ) -> Epic | None:
        from .models import _now

        fields: dict[str, object] = {}
        if name is not None:
            fields["name"] = name
        if description is not None:
            fields["description"] = description
        if status is not None:
            fields["status"] = status
        if not fields:
            return self.get_epic(epic_id)

        fields["updated_at"] = _now()
        set_clause = ", ".join(f"{k}=%s" for k in fields)
        values = list(fields.values()) + [epic_id]
        with self._connection() as conn:
            conn.execute(f"UPDATE epics SET {set_clause} WHERE id=%s", values)
            row = conn.execute("SELECT * FROM epics WHERE id = %s", (epic_id,)).fetchone()
        return Epic.from_row(row) if row else None

    # --- agent roster methods --------------------------------------------

    @staticmethod
    def _clean_tasks(tasks: list[str] | None) -> list[str]:
        """Keep only known task names, preserving the canonical order."""
        given = {str(t).strip().lower() for t in (tasks or [])}
        return [t for t in ALL_TASKS if t in given]

    def list_agents(self, project_id: int) -> list[AgentSpec]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM project_agents WHERE project_id = %s ORDER BY id", (project_id,)
            ).fetchall()
        return [AgentSpec.from_row(r) for r in rows]

    def get_agent(self, agent_id: int) -> AgentSpec | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM project_agents WHERE id = %s", (agent_id,)).fetchone()
        return AgentSpec.from_row(row) if row else None

    def create_agent(
        self,
        project_id: int,
        name: str,
        provider: str = "claude",
        model: str = "",
        allowed_tasks: list[str] | None = None,
        max_concurrency: int = 1,
        enabled: bool = True,
    ) -> AgentSpec:
        from .models import _now

        tasks = self._clean_tasks(allowed_tasks if allowed_tasks is not None else ALL_TASKS)
        now = _now()
        with self._connection() as conn:
            if (
                conn.execute("SELECT 1 FROM projects WHERE id = %s", (project_id,)).fetchone()
                is None
            ):
                raise ValueError(f"project {project_id} not found")
            row = conn.execute(
                "INSERT INTO project_agents(project_id, name, provider, model, allowed_tasks, "
                "max_concurrency, enabled, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (
                    project_id,
                    name,
                    provider,
                    model,
                    json.dumps(tasks),
                    max(1, int(max_concurrency)),
                    bool(enabled),
                    now,
                    now,
                ),
            ).fetchone()
        return AgentSpec.from_row(row)

    def update_agent(
        self,
        agent_id: int,
        name: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        allowed_tasks: list[str] | None = None,
        max_concurrency: int | None = None,
        enabled: bool | None = None,
    ) -> AgentSpec | None:
        from .models import _now

        fields: dict[str, object] = {}
        if name is not None:
            fields["name"] = name
        if provider is not None:
            fields["provider"] = provider
        if model is not None:
            fields["model"] = model
        if allowed_tasks is not None:
            fields["allowed_tasks"] = json.dumps(self._clean_tasks(allowed_tasks))
        if max_concurrency is not None:
            fields["max_concurrency"] = max(1, int(max_concurrency))
        if enabled is not None:
            fields["enabled"] = bool(enabled)
        if not fields:
            return self.get_agent(agent_id)
        fields["updated_at"] = _now()
        set_clause = ", ".join(f"{k}=%s" for k in fields)
        values = list(fields.values()) + [agent_id]
        with self._connection() as conn:
            conn.execute(f"UPDATE project_agents SET {set_clause} WHERE id=%s", values)
            row = conn.execute("SELECT * FROM project_agents WHERE id = %s", (agent_id,)).fetchone()
        return AgentSpec.from_row(row) if row else None

    def delete_agent(self, agent_id: int) -> bool:
        with self._connection() as conn:
            cur = conn.execute("DELETE FROM project_agents WHERE id = %s", (agent_id,))
            return cur.rowcount > 0

    def delete_project(self, project_id: int) -> bool:
        with self._connection() as conn:
            conn.execute(
                "DELETE FROM job_dependencies WHERE job_id IN "
                "(SELECT id FROM jobs WHERE project_id = %s) "
                "OR depends_on_job_id IN (SELECT id FROM jobs WHERE project_id = %s)",
                (project_id, project_id),
            )
            conn.execute(
                "DELETE FROM supervisor_events WHERE job_id IN "
                "(SELECT id FROM jobs WHERE project_id = %s)",
                (project_id,),
            )
            conn.execute(
                "DELETE FROM job_events WHERE job_id IN "
                "(SELECT id FROM jobs WHERE project_id = %s)",
                (project_id,),
            )
            conn.execute(
                "DELETE FROM usage WHERE job_id IN (SELECT id FROM jobs WHERE project_id = %s)",
                (project_id,),
            )
            conn.execute("DELETE FROM jobs WHERE project_id = %s", (project_id,))
            conn.execute("DELETE FROM epics WHERE project_id = %s", (project_id,))
            conn.execute("DELETE FROM project_agents WHERE project_id = %s", (project_id,))
            conn.execute("DELETE FROM project_members WHERE project_id = %s", (project_id,))
            conn.execute(
                "UPDATE intake_sessions SET project_id = NULL WHERE project_id = %s",
                (project_id,),
            )
            cur = conn.execute("DELETE FROM projects WHERE id = %s", (project_id,))
            return cur.rowcount > 0

    def cancel(self, job_id: int) -> bool:
        """Cancel a job if it is not already in a terminal state.

        Returns True if the job was cancelled, False if it was not found or
        was already done/failed/cancelled (callers should 409 in that case).
        """
        from .models import _now

        with self._connection() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status=%s, owner=NULL, lease_until=0, executing_step=NULL, "
                "error='Cancelled by user', updated_at=%s "
                "WHERE id=%s AND status NOT IN (%s, %s, %s)",
                (
                    JobStatus.CANCELLED.value,
                    _now(),
                    job_id,
                    JobStatus.DONE.value,
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                ),
            )
            if cur.rowcount:
                self._clear_active_remediation_conn(conn, job_id)
            return cur.rowcount > 0

    def retry(self, job_id: int, *, force: bool = False) -> Job | None:
        """Reset a failed or cancelled job to QUEUED/PENDING so the worker re-plans it.

        When ``force`` is true, merges ``{"scope_gate_bypass": True}`` into the job's
        existing ``source_meta`` (preserving any other keys) as part of the same
        UPDATE, so a submitter who has confirmed an oversized plan is legitimate can
        push it past the PLAN-stage scope gate (see stages/plan.py) on retry. A
        ``force`` retry also unparks a ``needs_split`` job — dropping the
        ``needs_split = FALSE`` guard and resetting ``needs_split``/``archived`` back
        to false — since ``scope_gate_bypass`` makes the plan stage skip the gate
        that parked it in the first place. A bare (non-force) retry still refuses any
        ``needs_split`` job exactly as before.

        Returns the refreshed Job, or None if not found or not in a retryable state.
        """
        from .models import _now

        with self._connection() as conn:
            source_meta_sql = ""
            params: list = [
                JobStatus.PENDING.value,
                Stage.QUEUED.value,
            ]
            if force:
                row = conn.execute(
                    "SELECT source_meta FROM jobs WHERE id = %s", (job_id,)
                ).fetchone()
                if row is None:
                    return None
                meta = dict(row["source_meta"] or {})
                meta["scope_gate_bypass"] = True
                source_meta_sql = "source_meta=%s, "
                params.append(json.dumps(meta))
            needs_split_set_sql = "needs_split=FALSE, archived=FALSE, " if force else ""
            needs_split_where_sql = "" if force else " AND needs_split = FALSE"
            params.append(_now())
            params.extend([job_id, JobStatus.FAILED.value, JobStatus.CANCELLED.value])
            row = conn.execute(
                "UPDATE jobs SET status=%s, stage=%s, owner=NULL, lease_until=0, "
                "error=NULL, failure=NULL, branch=NULL, attempts=0, executing_step=NULL, "
                "failed_step=NULL, failure_code=NULL, failure_origin=NULL, "
                "retry_disposition=NULL, failure_detail=NULL, "
                "rebase_attempts=0, rebase_retry_after=NULL, timeout_attempts=0, "
                "plan_reask_attempts=0, "
                f"plan=NULL, review=NULL, {needs_split_set_sql}{source_meta_sql}updated_at=%s "
                f"WHERE id=%s AND status IN (%s, %s){needs_split_where_sql} RETURNING *",
                params,
            ).fetchone()
        return Job.from_row(row) if row else None

    def claimable(self) -> Job | None:
        """Next job the runner should work on: pending and not yet done.

        Boost-aware: fetches a bounded, SQL-ordered candidate set and re-sorts
        it by effective priority (base priority plus remediation/critical-path/
        aging boosts) before picking the front of the line.
        """
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT j.* FROM jobs j WHERE j.status = %s AND j.stage != %s "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM job_dependencies d JOIN jobs p ON p.id=d.depends_on_job_id "
                "  WHERE d.job_id=j.id AND p.status <> %s "
                "  AND COALESCE(p.resolution, '') <> %s"
                ") ORDER BY j.priority DESC, j.id ASC LIMIT %s",
                (
                    JobStatus.PENDING.value,
                    Stage.DONE.value,
                    _DEPENDENCY_SATISFIED_STATUS,
                    _DEPENDENCY_SATISFIED_RESOLUTION,
                    _CLAIMABLE_CANDIDATE_LIMIT,
                ),
            ).fetchall()
            if not rows:
                return None
            candidates = [Job.from_row(row) for row in rows]
            candidates.sort(
                key=lambda candidate: (
                    -self._effective_priority_conn(conn, candidate)[0],
                    candidate.id,
                )
            )
            return candidates[0]

    async def claim(
        self,
        worker_id: str,
        now: float,
        lease_ttl: float,
        project_id: int | None = None,
        worker_host_id: int | None = None,
        stage_allowlist: set[Stage] | None = None,
    ) -> Job | None:
        """Atomically claim the next runnable job for this worker — capability-aware.

        Cross-process safe: a transaction-level advisory lock serializes the whole
        read-decide-write across every worker on every host, so exactly one wins
        each job and per-agent ``max_concurrency`` is honored. The decision is
        pure, deterministic SQL/Python — no AI — honoring the rule that the control
        plane can't depend on the very thing that's unavailable when a provider is
        rate-limited.

        A job is claimable when, for the agentic task its current stage needs, the
        project has an *enabled* agent that (a) is allowed that task, (b) whose
        provider isn't paused, and (c) is under its ``max_concurrency``. Stages
        whose next step is deterministic (test, merge) need no agent and are
        claimable by any worker.

        ``project_id``, when given, scopes candidates to that project only — used
        by tests that need to isolate claim() from concurrently-running jobs in a
        shared database; production callers omit it to claim fleet-wide.

        ``worker_host_id`` is this worker's enrolled host id (None for the local/
        default host). A DEPLOY-stage job whose target environment is pinned to a
        specific host (non-NULL ``environments.host_id``) is skipped unless it
        matches; every other stage ignores host pinning entirely.

        ``stage_allowlist``, when given, restricts claiming to jobs whose current
        stage is in the set (e.g. a deploy-only worker passing ``{Stage.DEPLOY}``);
        None (default) claims any stage, preserving today's behavior.
        """
        from .models import _now, stage_task

        def _do() -> Job | None:
            with self._connection() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_CLAIM,))
                # Reclaim jobs whose owning worker died (lease expired).
                conn.execute(
                    "UPDATE jobs SET status=%s, owner=NULL, lease_until=0, executing_step=NULL "
                    "WHERE status=%s AND lease_until > 0 AND lease_until < %s",
                    (JobStatus.PENDING.value, JobStatus.RUNNING.value, now),
                )
                paused = self._paused_providers(conn, now)
                running = self._running_counts(conn)
                query = (
                    "SELECT j.* FROM jobs j WHERE j.status=%s AND j.stage!=%s "
                    "AND (j.rebase_retry_after IS NULL OR j.rebase_retry_after <= %s) "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM job_dependencies d JOIN jobs p ON p.id=d.depends_on_job_id "
                    "  WHERE d.job_id=j.id AND p.status <> %s "
                    "  AND COALESCE(p.resolution, '') <> %s"
                    ")"
                )
                params = [
                    JobStatus.PENDING.value,
                    Stage.DONE.value,
                    now,
                    _DEPENDENCY_SATISFIED_STATUS,
                    _DEPENDENCY_SATISFIED_RESOLUTION,
                ]
                if project_id is not None:
                    query += " AND j.project_id=%s"
                    params.append(project_id)
                query += " ORDER BY j.priority DESC, j.id ASC"
                candidates = conn.execute(query, tuple(params)).fetchall()
                jobs = [Job.from_row(row) for row in candidates]
                jobs.sort(
                    key=lambda candidate: (
                        -self._effective_priority_conn(conn, candidate)[0],
                        candidate.id,
                    )
                )
                for job in jobs:
                    allowed_paths = ((job.source_meta or {}).get("scope") or {}).get(
                        "allowed_paths"
                    ) or []
                    if allowed_paths and self._manifest_conflicts_with_running(
                        conn, job.project_id, job.id, allowed_paths
                    ):
                        continue  # overlaps a RUNNING job's manifest; stays PENDING for a later poll
                    if stage_allowlist is not None and job.stage not in stage_allowlist:
                        continue  # this worker only claims stages in the allowlist
                    if job.stage == Stage.DEPLOY:
                        env_id = (job.source_meta or {}).get("environment_id")
                        target_host_id = self._deploy_stage_host_id(conn, job.project_id, env_id)
                        if target_host_id is not None and target_host_id != worker_host_id:
                            continue  # pinned to a different host; stays PENDING for that worker
                    task = stage_task(job.stage)
                    if task is None:
                        agent = None  # deterministic step: any worker may run it
                    else:
                        agent = self._pick_agent(conn, job.project_id, task.value, paused, running)
                        if agent is None:
                            continue  # no capable+available agent with free capacity
                        self._resolve_failover_destination(conn, job.id, job.stage, agent.provider)
                    job.status = JobStatus.RUNNING
                    job.owner = worker_id
                    job.lease_until = now + lease_ttl
                    job.agent_id = agent.id if agent else None
                    job.provider = agent.provider if agent else ""
                    job.updated_at = _now()
                    conn.execute(
                        "UPDATE jobs SET status=%s, owner=%s, lease_until=%s, agent_id=%s, "
                        "provider=%s, executing_step=NULL, failed_step=NULL, failure_code=NULL, "
                        "failure_origin=NULL, retry_disposition=NULL, failure_detail=NULL, "
                        "updated_at=%s WHERE id=%s",
                        (
                            job.status.value,
                            job.owner,
                            job.lease_until,
                            job.agent_id,
                            job.provider,
                            job.updated_at,
                            job.id,
                        ),
                    )
                    return job
                return None

        return await _db_with_retry(_do)

    async def claim_fastpath(
        self,
        job_id: int,
        worker_id: str,
        now: float,
        lease_ttl: float,
        worker_host_id: int | None = None,
        stage_allowlist: set[Stage] | None = None,
    ) -> Job | None:
        """Atomically re-claim one specific PENDING job whose next step is deterministic.

        The fast-path: after a worker finishes a stage, it keeps the job and runs
        the next stage immediately — skipping the save → requeue → claim-poll →
        cold-start round trip — but ONLY when that next step needs no AI agent
        (lint, test, merge, deploy), so roster capability/pause routing is never
        bypassed. Same advisory lock and claim conditions as ``claim`` (pending,
        not DONE, no rebase backoff, dependencies satisfied), scoped to ``job_id``;
        returns None when another worker got there first or the job is no longer
        eligible (e.g. cancelled).

        ``worker_host_id`` applies the same DEPLOY-stage host pin as ``claim``: a
        mismatch returns None so the caller falls back to the normal claim loop
        (where the host-matching worker can pick the job up instead).

        ``stage_allowlist`` applies the same stage restriction as ``claim``: a job
        whose stage isn't in the set returns None; None (default) allows any stage.
        """
        from .models import _now, stage_task

        def _do() -> Job | None:
            with self._connection() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_CLAIM,))
                row = conn.execute(
                    "SELECT j.* FROM jobs j WHERE j.id=%s AND j.status=%s AND j.stage!=%s "
                    "AND (j.rebase_retry_after IS NULL OR j.rebase_retry_after <= %s) "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM job_dependencies d JOIN jobs p ON p.id=d.depends_on_job_id "
                    "  WHERE d.job_id=j.id AND p.status <> %s "
                    "  AND COALESCE(p.resolution, '') <> %s"
                    ")",
                    (
                        job_id,
                        JobStatus.PENDING.value,
                        Stage.DONE.value,
                        now,
                        _DEPENDENCY_SATISFIED_STATUS,
                        _DEPENDENCY_SATISFIED_RESOLUTION,
                    ),
                ).fetchone()
                if row is None:
                    return None
                job = Job.from_row(row)
                if stage_allowlist is not None and job.stage not in stage_allowlist:
                    return None  # this worker only claims stages in the allowlist
                if job.stage == Stage.DEPLOY:
                    env_id = (job.source_meta or {}).get("environment_id")
                    target_host_id = self._deploy_stage_host_id(conn, job.project_id, env_id)
                    if target_host_id is not None and target_host_id != worker_host_id:
                        return None  # pinned to a different host; fall back to the normal claim
                if stage_task(job.stage) is not None:
                    return None  # next step is agentic: go through the normal claim
                job.status = JobStatus.RUNNING
                job.owner = worker_id
                job.lease_until = now + lease_ttl
                job.agent_id = None
                job.provider = ""
                job.updated_at = _now()
                conn.execute(
                    "UPDATE jobs SET status=%s, owner=%s, lease_until=%s, agent_id=NULL, "
                    "provider='', executing_step=NULL, failed_step=NULL, failure_code=NULL, "
                    "failure_origin=NULL, retry_disposition=NULL, failure_detail=NULL, "
                    "updated_at=%s WHERE id=%s",
                    (job.status.value, job.owner, job.lease_until, job.updated_at, job.id),
                )
                return job

        return await _db_with_retry(_do)

    def _find_manifest_conflict(
        self,
        conn: psycopg.Connection,
        project_id: int | None,
        job_id: int,
        allowed_paths: list[str],
    ) -> tuple[int, list[str]] | None:
        """The first RUNNING job in ``project_id`` (other than ``job_id``) whose scope
        manifest overlaps ``allowed_paths``, plus the specific overlapping pattern(s) —
        the deterministic hot-file serialization gate for architect-scoped jobs."""
        if project_id is None:
            return None
        rows = conn.execute(
            "SELECT id, source_meta FROM jobs WHERE project_id=%s AND status=%s AND id != %s "
            "ORDER BY id",
            (project_id, JobStatus.RUNNING.value, job_id),
        ).fetchall()
        for row in rows:
            other_paths = ((row["source_meta"] or {}).get("scope") or {}).get("allowed_paths") or []
            if other_paths and check_manifest_conflict(allowed_paths, other_paths):
                overlapping = [
                    p for p in allowed_paths if check_manifest_conflict([p], other_paths)
                ]
                return row["id"], overlapping
        return None

    def _manifest_conflicts_with_running(
        self,
        conn: psycopg.Connection,
        project_id: int | None,
        job_id: int,
        allowed_paths: list[str],
    ) -> bool:
        """True if a RUNNING job in ``project_id`` (other than ``job_id``) carries a
        scope manifest whose allowed_paths overlap ``allowed_paths`` — the deterministic
        hot-file serialization gate for architect-scoped jobs."""
        return self._find_manifest_conflict(conn, project_id, job_id, allowed_paths) is not None

    def _deploy_stage_host_id(
        self,
        conn: psycopg.Connection,
        project_id: int | None,
        environment_id: int | None = None,
    ) -> int | None:
        """The host a DEPLOY-stage job's target environment is pinned to.

        When ``environment_id`` is given, looks up that exact ``environments``
        row instead of the default 'prod' one. None means unpinned — no
        environment row (yet), or a NULL ``host_id`` (the universal state
        before any host is pinned) — both claimable by any worker, matching
        today's single-host behavior."""
        if environment_id is not None:
            row = conn.execute(
                "SELECT host_id FROM environments WHERE id=%s",
                (environment_id,),
            ).fetchone()
            return row["host_id"] if row else None
        if project_id is None:
            return None
        row = conn.execute(
            "SELECT host_id FROM environments WHERE project_id=%s AND name='prod'",
            (project_id,),
        ).fetchone()
        return row["host_id"] if row else None

    def _running_counts(self, conn: psycopg.Connection) -> dict[int, int]:
        rows = conn.execute(
            "SELECT agent_id, COUNT(*) AS n FROM jobs WHERE status=%s AND agent_id IS NOT NULL "
            "GROUP BY agent_id",
            (JobStatus.RUNNING.value,),
        ).fetchall()
        return {r["agent_id"]: r["n"] for r in rows}

    def _pick_agent(
        self,
        conn: psycopg.Connection,
        project_id: int | None,
        task: str,
        paused: set[str],
        running: dict[int, int],
    ) -> AgentSpec | None:
        """Lowest-id enabled agent for the project that can do ``task`` with capacity."""
        if project_id is None:
            return None
        rows = conn.execute(
            "SELECT * FROM project_agents WHERE project_id=%s AND enabled ORDER BY id",
            (project_id,),
        ).fetchall()
        for row in rows:
            agent = AgentSpec.from_row(row)
            if task not in agent.allowed_tasks:
                continue
            if agent.provider in paused:
                continue
            if running.get(agent.id, 0) >= agent.max_concurrency:
                continue
            return agent
        return None

    def get_scheduler_wait(self, job: Job, waiting_on: list[int]) -> SchedulerWait | None:
        """Why ``job`` isn't running right now, derived live from the same locks,
        manifest overlap, provider pauses, and per-agent capacity ``claim`` uses —
        or None if nothing is currently blocking it. Read-only diagnostic: never
        persists anything, and disappears the moment the underlying condition clears.

        Explicit dependency blocking (a non-empty ``waiting_on``) always takes
        precedence and is reported separately by the caller, so this returns None
        immediately whenever ``waiting_on`` is non-empty, along with any job that
        isn't PENDING.
        """
        if job.status != JobStatus.PENDING or waiting_on:
            return None
        with self._connection() as conn:
            if job.stage == Stage.MERGE:
                row = conn.execute(
                    "SELECT owner FROM schema_locks WHERE project_id=%s", (job.project_id,)
                ).fetchone()
                if row is not None:
                    owner_job_id = parse_lock_owner_id(row["owner"])
                    if owner_job_id is not None and owner_job_id != job.id:
                        return SchedulerWait(
                            reason=SchedulerWaitReason.SCHEMA_LOCK,
                            summary=(
                                f"Waiting for the project schema lock — held by #{owner_job_id}."
                            ),
                            blocking_job_ids=[owner_job_id],
                            conflicting_paths=[],
                        )
                row = conn.execute(
                    "SELECT owner FROM merge_locks WHERE repo_path=%s", (job.repo_path,)
                ).fetchone()
                if row is not None:
                    owner_job_id = parse_lock_owner_id(row["owner"])
                    if owner_job_id is not None and owner_job_id != job.id:
                        return SchedulerWait(
                            reason=SchedulerWaitReason.MERGE_LOCK,
                            summary=(
                                f"Waiting for the project merge lock — held by #{owner_job_id}."
                            ),
                            blocking_job_ids=[owner_job_id],
                            conflicting_paths=[],
                        )

            allowed_paths = ((job.source_meta or {}).get("scope") or {}).get("allowed_paths") or []
            if allowed_paths:
                conflict = self._find_manifest_conflict(conn, job.project_id, job.id, allowed_paths)
                if conflict is not None:
                    other_job_id, overlapping_paths = conflict
                    return SchedulerWait(
                        reason=SchedulerWaitReason.FILE_OVERLAP,
                        summary=(
                            f"Waiting for project execution slot — #{other_job_id} overlaps "
                            f"{', '.join(overlapping_paths)}."
                        ),
                        blocking_job_ids=[other_job_id],
                        conflicting_paths=overlapping_paths,
                    )

            task = stage_task(job.stage)
            if task is None or job.project_id is None:
                return None
            rows = conn.execute(
                "SELECT * FROM project_agents WHERE project_id=%s AND enabled ORDER BY id",
                (job.project_id,),
            ).fetchall()
            capable = [
                agent
                for agent in (AgentSpec.from_row(row) for row in rows)
                if task.value in agent.allowed_tasks
            ]
            if not capable:
                return None

            now = time.time()
            paused = self._paused_providers(conn, now)
            running = self._running_counts(conn)
            for agent in capable:
                if (
                    agent.provider not in paused
                    and running.get(agent.id, 0) < agent.max_concurrency
                ):
                    return None  # this agent could claim it right now

            paused_agent = next((a for a in capable if a.provider in paused), None)
            if paused_agent is not None:
                from datetime import datetime, timezone

                row = conn.execute(
                    "SELECT value FROM meta WHERE key=%s",
                    (self._PAUSE_PREFIX + paused_agent.provider,),
                ).fetchone()
                resume_iso = ""
                if row is not None:
                    try:
                        resume_iso = datetime.fromtimestamp(
                            float(row["value"]), tz=timezone.utc
                        ).isoformat()
                    except (TypeError, ValueError):
                        resume_iso = ""
                return SchedulerWait(
                    reason=SchedulerWaitReason.PROVIDER_CAPACITY,
                    summary=(
                        f"Waiting for provider capacity — {paused_agent.provider} paused "
                        f"until {resume_iso}."
                    ),
                    blocking_job_ids=[],
                    conflicting_paths=[],
                )

            occupying_ids: set[int] = set()
            for agent in capable:
                occ_rows = conn.execute(
                    "SELECT id FROM jobs WHERE agent_id=%s AND project_id=%s AND status=%s",
                    (agent.id, job.project_id, JobStatus.RUNNING.value),
                ).fetchall()
                occupying_ids.update(r["id"] for r in occ_rows)
            occupying = sorted(occupying_ids)
            return SchedulerWait(
                reason=SchedulerWaitReason.PROJECT_SLOT_OCCUPIED,
                summary=(
                    "Waiting for project execution slot — occupied by "
                    f"{', '.join('#' + str(i) for i in occupying)}."
                ),
                blocking_job_ids=occupying,
                conflicting_paths=[],
            )

    def _resolve_failover_destination(
        self, conn: psycopg.Connection, job_id: int, stage: Stage, destination_provider: str
    ) -> None:
        """Attach the just-selected agent's provider to this job's latest
        unresolved ``provider_failover`` event at ``stage``, if one exists.

        Runs inside the caller's (``claim``'s) existing advisory-locked
        transaction. Never inserts a new event — only fills in the one
        ``destination_provider IS NULL`` event, so a job that never failed
        over is untouched and a job re-claimed after resolution is never
        rewritten."""
        row = conn.execute(
            "SELECT id, detail FROM job_events WHERE job_id=%s AND stage=%s "
            "AND status='provider_failover' "
            "AND (detail::jsonb ->> 'destination_provider') IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (job_id, stage.value),
        ).fetchone()
        if row is None:
            return
        try:
            detail = json.loads(row["detail"]) if row["detail"] else {}
        except (TypeError, ValueError):
            detail = {}
        detail["destination_provider"] = destination_provider
        conn.execute("UPDATE job_events SET detail=%s WHERE id=%s", (json.dumps(detail), row["id"]))

    # --- lease + per-provider pause (deterministic control plane) ---------
    async def renew_lease(self, job_id: int, owner: str, until: float) -> None:
        """Heartbeat: extend a running job's lease, but only if we still own it."""

        def _do() -> None:
            with self._connection() as conn:
                conn.execute(
                    "UPDATE jobs SET lease_until=%s WHERE id=%s AND owner=%s AND status=%s",
                    (until, job_id, owner, JobStatus.RUNNING.value),
                )

        await _db_with_retry(_do)

    _PAUSE_PREFIX = "paused_until:"

    def _paused_providers(self, conn: psycopg.Connection, now: float) -> set[str]:
        """Providers currently paused — read inside the caller's transaction."""
        rows = conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE %s", (self._PAUSE_PREFIX + "%",)
        ).fetchall()
        out: set[str] = set()
        for r in rows:
            try:
                if float(r["value"]) > now:
                    out.add(r["key"][len(self._PAUSE_PREFIX) :])
            except (TypeError, ValueError):
                continue
        return out

    def paused_providers(self, now: float) -> set[str]:
        with self._connection() as conn:
            return self._paused_providers(conn, now)

    def set_provider_pause(self, provider: str, until: float) -> None:
        self.set_meta(self._PAUSE_PREFIX + provider, str(until))

    async def record_provider_failover(
        self,
        job_id: int,
        owner: str,
        stage: Stage,
        source_provider: str,
        resets_at: float,
        now: float,
        project_id: int | None = None,
        failed_step: str = "",
    ) -> ProviderFailoverTransition:
        """Atomically requeue a job after a confirmed provider limit.

        Under the same claim advisory lock used by ``claim``/``claim_fastpath``,
        conditionally moves the job (id, owner, RUNNING, stage, provider) back to
        PENDING at its existing stage, clears owner/lease/agent/provider, pauses
        ``source_provider`` in ``meta``, and records exactly one durable
        ``provider_failover`` job_event — all in one transaction, so a stale
        worker that lost the race can never partially overwrite newer state:
        when the conditional guard doesn't match, nothing is written (no pause,
        no job mutation, no event) and ``applied`` is False. Functional
        ``attempts``/``rebase_attempts``/``timeout_attempts``/``plan_reask_attempts``
        are never touched. The job's failure snapshot fields (``failed_step``,
        ``failure_code``, ``failure_origin``, ``retry_disposition``,
        ``failure_detail``) are refreshed to describe this pause rather than
        left stale from an earlier, unrelated failure cycle.

        The recorded event's ``alternate_available`` reports whether a
        compatible enabled agent on an unpaused provider already exists for the
        stage's next task (or True outright for a deterministic next step),
        computed after ``source_provider``'s pause is applied. Its
        ``destination_provider`` starts null; a later ``claim`` that selects an
        agent for this same pending stage fills it in (see
        ``_resolve_failover_destination``).
        """
        from .models import _now, stage_task

        def _do() -> ProviderFailoverTransition:
            with self._connection() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_CLAIM,))
                row = conn.execute(
                    "UPDATE jobs SET status=%s, owner='', lease_until=0, agent_id=NULL, "
                    "provider='', executing_step=NULL, failed_step=%s, "
                    "failure_code='provider_unavailable', failure_origin='provider', "
                    "retry_disposition='same_step', failure_detail=%s, updated_at=%s "
                    "WHERE id=%s AND owner=%s AND status=%s AND stage=%s AND provider=%s "
                    "RETURNING id",
                    (
                        JobStatus.PENDING.value,
                        failed_step or None,
                        json.dumps({"provider": source_provider, "resets_at": resets_at}),
                        _now(),
                        job_id,
                        owner,
                        JobStatus.RUNNING.value,
                        stage.value,
                        source_provider,
                    ),
                ).fetchone()
                if row is None:
                    return ProviderFailoverTransition(
                        applied=False,
                        job_id=job_id,
                        stage=stage,
                        source_provider=source_provider,
                        reset_at=resets_at,
                    )
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES (%s, %s) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (self._PAUSE_PREFIX + source_provider, str(resets_at)),
                )
                task = stage_task(stage)
                if task is None:
                    alternate_available = True  # deterministic next step needs no agent
                else:
                    paused = self._paused_providers(conn, now)
                    paused.add(source_provider)
                    running = self._running_counts(conn)
                    alternate_available = (
                        project_id is not None
                        and self._pick_agent(conn, project_id, task.value, paused, running)
                        is not None
                    )
                detail = {
                    "source_provider": source_provider,
                    "destination_provider": None,
                    "stage": stage.value,
                    "reset_at": resets_at,
                    "alternate_available": alternate_available,
                }
                event_row = conn.execute(
                    "INSERT INTO job_events(job_id, stage, status, attempt, summary, detail, "
                    "tokens, cost_usd, started_at, ended_at, agent_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (
                        job_id,
                        stage.value,
                        "provider_failover",
                        0,
                        f"{source_provider} unavailable at {stage.value}; requeued",
                        json.dumps(detail),
                        0,
                        0.0,
                        _now(),
                        _now(),
                        None,
                    ),
                ).fetchone()
                return ProviderFailoverTransition(
                    applied=True,
                    job_id=job_id,
                    stage=stage,
                    source_provider=source_provider,
                    reset_at=resets_at,
                    alternate_available=alternate_available,
                    event_id=event_row["id"],
                )

        return await _db_with_retry(_do)

    # --- per-repo merge lock (serialize merges; builds stay parallel) ------
    def try_acquire_merge_lock(self, repo_path: str, owner: str, now: float, ttl: float) -> bool:
        """Grab the merge lock for ``repo_path``; reclaim it if a holder went stale."""
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO merge_locks(repo_path, owner, acquired_at) VALUES (%s, %s, %s) "
                "ON CONFLICT(repo_path) DO UPDATE SET owner=excluded.owner, "
                "acquired_at=excluded.acquired_at "
                "WHERE merge_locks.owner=excluded.owner OR merge_locks.acquired_at < %s",
                (repo_path, owner, now, now - ttl),
            )
            row = conn.execute(
                "SELECT owner FROM merge_locks WHERE repo_path=%s", (repo_path,)
            ).fetchone()
            return bool(row) and row["owner"] == owner

    def release_merge_lock(self, repo_path: str, owner: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "DELETE FROM merge_locks WHERE repo_path=%s AND owner=%s", (repo_path, owner)
            )

    def list_merge_locks(self) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT repo_path, owner, acquired_at FROM merge_locks ORDER BY repo_path"
            ).fetchall()
        return [dict(r) for r in rows]

    # --- per-project schema lock (serialize schema jobs merge->deploy) ----
    def try_acquire_schema_lock(self, project_id: int, owner: str, now: float, ttl: float) -> bool:
        """Grab the schema lock for ``project_id``; reclaim it if a holder went stale."""
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO schema_locks(project_id, owner, acquired_at) VALUES (%s, %s, %s) "
                "ON CONFLICT(project_id) DO UPDATE SET owner=excluded.owner, "
                "acquired_at=excluded.acquired_at "
                "WHERE schema_locks.owner=excluded.owner OR schema_locks.acquired_at < %s",
                (project_id, owner, now, now - ttl),
            )
            row = conn.execute(
                "SELECT owner FROM schema_locks WHERE project_id=%s", (project_id,)
            ).fetchone()
            return bool(row) and row["owner"] == owner

    def release_schema_lock(self, project_id: int, owner: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "DELETE FROM schema_locks WHERE project_id=%s AND owner=%s", (project_id, owner)
            )

    def list_schema_locks(self) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT project_id, owner, acquired_at FROM schema_locks ORDER BY project_id"
            ).fetchall()
        return [dict(r) for r in rows]

    # --- live worker fleet (the C2 view) ---------------------------------
    async def worker_heartbeat(
        self,
        worker_id: str,
        host: str,
        pid: int,
        *,
        status: str,
        now: float,
        role: str = "executor",
        job_id: int | None = None,
        stage: str = "",
        provider: str = "",
        started_at: float | None = None,
    ) -> None:
        """Upsert a worker's live state. ``started_at`` is kept across heartbeats
        of the same job (only set when a new job/stage begins).

        ``status`` values: ``'idle'``, ``'busy'``, ``'draining'``, ``'leader'``, ``'standby'``."""

        def _do() -> None:
            with self._connection() as conn:
                conn.execute(
                    "INSERT INTO workers(id, host, pid, status, role, job_id, stage, provider, started_at, last_seen) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT(id) DO UPDATE SET status=excluded.status, role=excluded.role, "
                    "job_id=excluded.job_id, "
                    "stage=excluded.stage, provider=excluded.provider, last_seen=excluded.last_seen, "
                    "started_at=COALESCE(excluded.started_at, workers.started_at)",
                    (
                        worker_id,
                        host,
                        pid,
                        status,
                        role,
                        job_id,
                        stage,
                        provider,
                        started_at if started_at is not None else now,
                        now,
                    ),
                )

        await _db_with_retry(_do)

    def worker_offline(self, worker_id: str) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM workers WHERE id=%s", (worker_id,))

    def list_workers(self, now: float, stale_after: float) -> list[dict]:
        """All known workers with liveness; prunes long-dead rows as a side effect."""
        with self._connection() as conn:
            conn.execute(
                "DELETE FROM workers WHERE last_seen < %s", (now - max(stale_after * 5, 600),)
            )
            rows = conn.execute("SELECT * FROM workers ORDER BY id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["alive"] = (now - (r["last_seen"] or 0)) < stale_after
            out.append(d)
        return out

    def reclaim_orphaned_worker_slots(self) -> list[str]:
        """Idle every executor worker slot whose job_id no longer references a
        RUNNING/DEPLOYING job — the job-status-mismatch class of a wedged fleet
        (job #3988: a hung stage's heartbeat kept renewing status='busy' forever,
        holding a slot for a job that had already gone PENDING/FAILED elsewhere).

        Conservative by construction: this keys purely on the referenced job's
        status, never on heartbeat/last_seen age, so a legitimately long-running
        stage on a genuinely RUNNING/DEPLOYING job is never touched.
        """
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT w.id FROM workers w "
                "LEFT JOIN jobs j ON j.id = w.job_id "
                "WHERE w.role = 'executor' AND w.job_id IS NOT NULL "
                "AND (j.id IS NULL OR j.status NOT IN (%s, %s))",
                (JobStatus.RUNNING.value, JobStatus.DEPLOYING.value),
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                conn.execute(
                    "UPDATE workers SET status = 'idle', job_id = NULL, stage = '', "
                    "provider = '' WHERE id = ANY(%s)",
                    (ids,),
                )
        return ids

    def has_fresh_replacement_worker(
        self,
        host: str,
        exclude_pid: int,
        max_age_seconds: float = 60.0,
        now: float | None = None,
    ) -> bool:
        """True if a DIFFERENT pid on ``host`` has a fresh executor/supervisor
        heartbeat — proof a blue-green replacement fleet is already alive."""
        _now = now if now is not None else time.time()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM workers WHERE host=%s AND pid<>%s AND role IN ('supervisor', 'executor') "
                "AND last_seen >= %s LIMIT 1",
                (host, exclude_pid, _now - max_age_seconds),
            ).fetchone()
        return row is not None

    # --- supervisor leadership (session-level advisory lock) -------------
    def try_acquire_supervisor_lock(self, conn: "psycopg.Connection") -> bool:
        """Non-blocking: return True if we just acquired the fleet-wide supervisor lock.

        The lock is session-level (not xact-level), so it persists across commits and is
        released automatically when ``conn`` closes (i.e. when the process dies).
        Must be called on a dedicated long-lived connection (not a pool connection).
        """
        row = conn.execute(
            "SELECT pg_try_advisory_lock(%s) AS acquired", (_LOCK_SUPERVISOR,)
        ).fetchone()
        return bool(row["acquired"]) if row else False

    def release_supervisor_lock(self, conn: "psycopg.Connection") -> None:
        """Explicitly release the supervisor advisory lock on ``conn``."""
        conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_SUPERVISOR,))

    # --- supervisor janitor helpers --------------------------------------
    def get_failed_jobs(self) -> "list[Job]":
        """FAILED jobs the janitor should scan — archived jobs are excluded.

        An archived job has been retired by a human; the supervisor must not
        re-queue, reconcile, or escalate it, so it never enters the scan.
        """
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status = %s AND archived = FALSE ORDER BY id",
                (JobStatus.FAILED.value,),
            ).fetchall()
        return [Job.from_row(r) for r in rows]

    def get_active_job_ids(self) -> "set[int]":
        """IDs of all PENDING, RUNNING, or DEPLOYING jobs — the do-not-touch set for GC."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE status IN (%s, %s, %s)",
                (JobStatus.PENDING.value, JobStatus.RUNNING.value, JobStatus.DEPLOYING.value),
            ).fetchall()
        return {int(r["id"]) for r in rows}

    def get_git_artifact_protected_job_ids(self) -> "set[int]":
        """Incident IDs whose candidate git artifacts are still required.

        Unresolved, unarchived FAILED jobs remain recoverable across restarts.
        Once a supervisor remediation has durably recorded that it seeded the
        source candidate, that remediation no longer needs the incident's git
        artifacts; resolved and archived incidents are likewise released.
        """
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT i.id FROM jobs i WHERE i.status=%s AND i.archived=FALSE "
                "AND COALESCE(i.resolution, '')<>%s AND (NOT EXISTS ("
                " SELECT 1 FROM jobs r WHERE r.source_meta->>'ai_fix_for'=i.id::text "
                " AND r.status IN (%s, %s, %s) "
                " AND COALESCE((r.source_meta->'remediation_source'->>'captured')::boolean, FALSE)"
                ") OR EXISTS ("
                " SELECT 1 FROM jobs r WHERE r.source_meta->>'ai_fix_for'=i.id::text "
                " AND r.status IN (%s, %s, %s) "
                " AND r.source_meta ? 'remediation_source' "
                " AND NOT COALESCE((r.source_meta->'remediation_source'->>'captured')::boolean, FALSE)"
                "))",
                (
                    JobStatus.FAILED.value,
                    "resolved",
                    JobStatus.PENDING.value,
                    JobStatus.RUNNING.value,
                    JobStatus.DEPLOYING.value,
                    JobStatus.PENDING.value,
                    JobStatus.RUNNING.value,
                    JobStatus.DEPLOYING.value,
                ),
            ).fetchall()
        return {int(row["id"]) for row in rows}

    def mark_remediation_source_captured(self, remediation_job_id: int) -> None:
        """Durably mark a remediation's persisted source candidate as seeded."""
        from .models import _now

        with self._connection() as conn:
            row = conn.execute(
                "SELECT source_meta FROM jobs WHERE id=%s FOR UPDATE", (remediation_job_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"job {remediation_job_id} not found")
            meta = dict(row["source_meta"] or {})
            source = dict(meta.get("remediation_source") or {})
            if not source.get("branch") or not source.get("sha"):
                raise ValueError("remediation source provenance is missing")
            source["captured"] = True
            meta["remediation_source"] = source
            conn.execute(
                "UPDATE jobs SET source_meta=%s, updated_at=%s WHERE id=%s",
                (json.dumps(meta), _now(), remediation_job_id),
            )

    def requeue_job(self, job_id: int, *, zero_attempts: bool = False) -> None:
        """Reset a FAILED job to PENDING/QUEUED so the pipeline re-runs it.

        ``zero_attempts=True`` wipes the fix-attempt and rebase counters (used for
        stale-branch/orphaned-worktree where we want a completely fresh run).
        ``zero_attempts=False`` preserves fix attempts so the existing retry cap still
        applies (used for transient failures that briefly go away).
        """
        from .models import _now

        now = _now()
        with self._connection() as conn:
            if zero_attempts:
                conn.execute(
                    "UPDATE jobs SET status=%s, stage=%s, error='', failure='', owner=NULL, "
                    "executing_step=NULL, failed_step=NULL, failure_code=NULL, "
                    "failure_origin=NULL, retry_disposition=NULL, failure_detail=NULL, "
                    "lease_until=0, attempts=0, rebase_attempts=0, rebase_retry_after=NULL, "
                    "timeout_attempts=0, plan_reask_attempts=0, updated_at=%s "
                    "WHERE id=%s AND status=%s",
                    (
                        JobStatus.PENDING.value,
                        Stage.QUEUED.value,
                        now,
                        job_id,
                        JobStatus.FAILED.value,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET status=%s, stage=%s, error='', owner=NULL, lease_until=0, "
                    "executing_step=NULL, failed_step=NULL, failure_code=NULL, "
                    "failure_origin=NULL, retry_disposition=NULL, failure_detail=NULL, "
                    "updated_at=%s WHERE id=%s AND status=%s",
                    (
                        JobStatus.PENDING.value,
                        Stage.QUEUED.value,
                        now,
                        job_id,
                        JobStatus.FAILED.value,
                    ),
                )

    def requeue_job_at_stage(self, job_id: int, stage: "Stage", *, failure: str = "") -> None:
        """Reset a FAILED job to PENDING at ``stage``, preserving attempt counters.

        Like ``requeue_job`` but targets a specific stage instead of always going
        back to QUEUED. Used by the gate-no-changes supervisor path to requeue a
        job past the gate it was wrongly blocked on, and by the attributable-deploy
        path to requeue to FIX with the error preserved in ``failure``.
        """
        from .models import _now

        now = _now()
        with self._connection() as conn:
            conn.execute(
                "UPDATE jobs SET status=%s, stage=%s, error='', failure=%s, "
                "owner=NULL, lease_until=0, executing_step=NULL, failed_step=NULL, "
                "failure_code=NULL, failure_origin=NULL, retry_disposition=NULL, "
                "failure_detail=NULL, updated_at=%s "
                "WHERE id=%s AND status=%s",
                (
                    JobStatus.PENDING.value,
                    stage.value,
                    failure,
                    now,
                    job_id,
                    JobStatus.FAILED.value,
                ),
            )

    def requeue_no_diff_build(self, job_id: int) -> bool:
        """Atomically schedule the sole evidence-informed retry of a failed BUILD."""
        from .models import _now

        with self._connection() as conn:
            row = conn.execute(
                "UPDATE jobs SET status=%s, stage=%s, error='', owner=NULL, lease_until=0, "
                "executing_step=NULL, retry_disposition=%s, "
                "failure_detail=jsonb_set(failure_detail, '{retry_attempt}', '1'::jsonb), "
                "updated_at=%s WHERE id=%s AND status=%s AND failed_step='build' "
                "AND failure_code='no_diff_verification_failed' "
                "AND retry_disposition='retry_build' "
                "AND COALESCE((failure_detail->>'retry_attempt')::int, 0)=0 RETURNING id",
                (
                    JobStatus.PENDING.value,
                    Stage.PLAN.value,
                    "retry_build",
                    _now(),
                    job_id,
                    JobStatus.FAILED.value,
                ),
            ).fetchone()
        return row is not None

    def add_job_dependency(
        self,
        job_id: int,
        depends_on_job_id: int,
        provenance: DependencyProvenance = DependencyProvenance.USER,
    ) -> None:
        """Insert a job dependency without the id-ordering constraint from create().

        For supervisor use: the gate-fix job has a higher id than the blocked
        job, so the ordering guard in create() would reject it. Unlike that
        ordering guard, this checks the *entire* persisted dependency graph
        (plus the proposed edge) for a cycle via the same acyclicity logic
        create_batch relies on, and refuses self-edges and edges that would
        close a direct or transitive cycle. The check and insert happen under
        a fleet-wide advisory lock (``_LOCK_JOB_DEPENDENCIES``), not a lock on
        just the two endpoint rows, because two concurrent calls with
        disjoint endpoints (e.g. add(A,B) and add(C,D), with pre-existing
        B->C and D->A) can each pass a cycle check that only locks its own
        endpoints and jointly commit a cycle that neither call alone
        created. Serializing every call fleet-wide closes that race.
        """
        with self._connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_JOB_DEPENDENCIES,))
            rows = conn.execute("SELECT job_id, depends_on_job_id FROM job_dependencies").fetchall()
            edges: dict[int, set[int]] = {}
            for row in rows:
                edges.setdefault(row["job_id"], set()).add(row["depends_on_job_id"])
            edges.setdefault(job_id, set()).add(depends_on_job_id)
            nodes = set(edges) | {v for deps in edges.values() for v in deps}
            if self._dependency_topo_order(nodes, edges) is None:
                raise ValueError(
                    f"job {job_id} cannot depend on job {depends_on_job_id}: "
                    "would create a dependency cycle"
                )
            inserted = self._add_job_dependency_conn(conn, job_id, depends_on_job_id, provenance)
            if inserted and provenance == DependencyProvenance.AUTO:
                self._dependency_event_conn(
                    conn,
                    job_id,
                    "added",
                    depends_on_job_id,
                    provenance.value,
                    "external_queue_collision",
                )

    def remove_job_dependency(self, job_id: int, dep_id: int) -> None:
        with self._connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_JOB_DEPENDENCIES,))
            conn.execute(
                "DELETE FROM job_dependencies WHERE job_id=%s AND depends_on_job_id=%s",
                (job_id, dep_id),
            )

    def reconcile_auto_dependencies(self, project_id: int) -> list[dict]:
        """Atomically minimize generated scheduler edges for one project."""
        with self._connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_JOB_DEPENDENCIES,))
            return self._reconcile_auto_dependencies_conn(conn, project_id)

    def _reconcile_auto_dependencies_conn(
        self, conn: psycopg.Connection, project_id: int
    ) -> list[dict]:
        """Minimize generated edges while the caller holds the dependency lock."""
        job_rows = conn.execute(
            "SELECT id, source_meta FROM jobs WHERE project_id=%s", (project_id,)
        ).fetchall()
        job_ids = {int(row["id"]) for row in job_rows}
        scopes = {int(row["id"]): (row["source_meta"] or {}).get("scope") for row in job_rows}
        if not job_ids:
            return []
        rows = conn.execute(
            "SELECT job_id, depends_on_job_id, provenance FROM job_dependencies "
            "WHERE job_id = ANY(%s) AND depends_on_job_id = ANY(%s)",
            (list(job_ids), list(job_ids)),
        ).fetchall()
        original = [
            (int(row["job_id"]), int(row["depends_on_job_id"]), row["provenance"]) for row in rows
        ]
        kept = set(minimize_auto_dependencies(scopes, original))
        changes: list[dict] = []
        for edge in sorted(set(original) - kept):
            job_id, dependency_id, provenance = edge
            conn.execute(
                "DELETE FROM job_dependencies WHERE job_id=%s "
                "AND depends_on_job_id=%s AND provenance='auto'",
                (job_id, dependency_id),
            )
            left = classify_dependency_scope(scopes.get(job_id))
            right = classify_dependency_scope(scopes.get(dependency_id))
            reason = (
                "known_disjoint_scopes"
                if left.known
                and right.known
                and not check_manifest_conflict(list(left.paths), list(right.paths))
                else "transitively_redundant"
            )
            self._dependency_event_conn(conn, job_id, "removed", dependency_id, provenance, reason)
            changes.append(
                {
                    "action": "removed",
                    "job_id": job_id,
                    "depends_on_job_id": dependency_id,
                    "provenance": provenance,
                    "reason": reason,
                }
            )
        return changes

    _SUPERVISOR_REQUEUE_PREFIX = "supervisor_requeue:"
    _SUPERVISOR_NOTIFIED_PREFIX = "supervisor_notified:"

    def count_supervisor_events(self, job_id: int, action: str) -> int:
        """How many times the supervisor recorded ``action`` for this job."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM supervisor_events WHERE job_id=%s AND action=%s",
                (job_id, action),
            ).fetchone()
        return int(row["n"] or 0)

    def has_supervisor_event(self, job_id: int, action: str) -> bool:
        """Whether the supervisor already recorded ``action`` for this job (dedupe)."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM supervisor_events WHERE job_id=%s AND action=%s LIMIT 1",
                (job_id, action),
            ).fetchone()
        return row is not None

    def has_ai_fix_job(self, source_job_id: int) -> bool:
        """Whether the AI analyst already filed a fix job for this failed job."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM jobs WHERE source_meta ->> 'ai_fix_for' = %s LIMIT 1",
                (str(source_job_id),),
            ).fetchone()
        return row is not None

    def supervisor_requeue_count(self, job_id: int) -> int:
        """How many times the janitor has re-queued this job (dead-letter cap tracking)."""
        key = f"{self._SUPERVISOR_REQUEUE_PREFIX}{job_id}"
        try:
            return int(self.get_meta(key, "0"))
        except (TypeError, ValueError):
            return 0

    def increment_supervisor_requeue(self, job_id: int) -> int:
        """Atomically increment and return the re-queue count for ``job_id``."""
        key = f"{self._SUPERVISOR_REQUEUE_PREFIX}{job_id}"
        with self._connection() as conn:
            row = conn.execute(
                "INSERT INTO meta(key, value) VALUES (%s, '1') "
                "ON CONFLICT(key) DO UPDATE SET value = (CAST(meta.value AS INTEGER) + 1)::TEXT "
                "RETURNING value",
                (key,),
            ).fetchone()
        return int(row["value"]) if row else 1

    def is_supervisor_notified(self, job_id: int) -> bool:
        """Return True if the janitor has already sent a judgment-class alert for this job."""
        key = f"{self._SUPERVISOR_NOTIFIED_PREFIX}{job_id}"
        return self.get_meta(key, "") != ""

    def mark_supervisor_notified(self, job_id: int) -> None:
        """Record that a judgment-class notification has been sent for this job."""
        key = f"{self._SUPERVISOR_NOTIFIED_PREFIX}{job_id}"
        self.set_meta(key, "1")

    def list_supervisor_requeues(self) -> list[dict]:
        """All supervisor requeue counters from the meta table."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT key, value FROM meta WHERE key LIKE %s",
                (f"{self._SUPERVISOR_REQUEUE_PREFIX}%",),
            ).fetchall()
        prefix_len = len(self._SUPERVISOR_REQUEUE_PREFIX)
        return [{"job_id": int(r["key"][prefix_len:]), "count": int(r["value"])} for r in rows]

    def record_supervisor_event(
        self, job_id: int, action: str, failure_class: str, detail: str = ""
    ) -> None:
        """Insert one janitor remediation event. Best-effort: never raises."""
        try:
            with self._connection() as conn:
                conn.execute(
                    "INSERT INTO supervisor_events(job_id, action, failure_class, detail) "
                    "VALUES (%s, %s, %s, %s)",
                    (job_id, action, failure_class, detail),
                )
        except Exception as exc:
            _log.debug("record_supervisor_event job %s failed: %s", job_id, exc)

    def list_supervisor_events_for_job(self, job_id: int) -> list[dict]:
        """Return the incident-analyst/janitor events for one job, oldest-first."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT id, ts, job_id, action, failure_class, detail "
                "FROM supervisor_events WHERE job_id=%s ORDER BY id",
                (job_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["detail"] = json.loads(d["detail"]) if d["detail"] else {}
            except (TypeError, ValueError):
                d["detail"] = {}
            out.append(d)
        return out

    def list_supervisor_events(self, limit: int = 100) -> list[dict]:
        """Return janitor events newest-first, up to limit rows."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT id, ts, job_id, action, failure_class, detail "
                "FROM supervisor_events ORDER BY ts DESC LIMIT %s",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def has_deploy_fix_job(self, failed_job_id: int) -> bool:
        """True if a supervisor-created deploy-fix job already exists for failed_job_id."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM jobs WHERE source = %s "
                "AND source_meta->>'deploy_fix_for' = %s LIMIT 1",
                (JobSource.SUPERVISOR.value, str(failed_job_id)),
            ).fetchone()
        return row is not None

    def count_deploy_fix_jobs(self, project_id: int) -> int:
        """Count supervisor-created deploy-fix jobs for project_id (budget check)."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE project_id = %s AND source = %s "
                "AND (source_meta->>'deploy_fix_for') IS NOT NULL",
                (project_id, JobSource.SUPERVISOR.value),
            ).fetchone()
        return int(row["n"]) if row else 0

    def has_alembic_merge_fix_job(self, failed_job_id: int) -> bool:
        """True if a supervisor-created alembic-merge-fix job already exists for failed_job_id."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM jobs WHERE source = %s "
                "AND source_meta->>'alembic_merge_fix_for' = %s LIMIT 1",
                (JobSource.SUPERVISOR.value, str(failed_job_id)),
            ).fetchone()
        return row is not None

    def count_alembic_merge_fix_jobs(self, project_id: int) -> int:
        """Count supervisor-created alembic-merge-fix jobs for project_id (budget check)."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE project_id = %s AND source = %s "
                "AND (source_meta->>'alembic_merge_fix_for') IS NOT NULL",
                (project_id, JobSource.SUPERVISOR.value),
            ).fetchone()
        return int(row["n"]) if row else 0

    @staticmethod
    def _source_meta(row: dict) -> dict:
        value = row.get("source_meta") or {}
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _is_remediation_job(source_meta: dict | None) -> bool:
        """True iff ``source_meta`` links this job to an automated remediation chain."""
        if not isinstance(source_meta, dict):
            return False
        return any(source_meta.get(key) is not None for key in REMEDIATION_LINK_KEYS)

    def _effective_priority_conn(self, conn, job: Job) -> tuple[int, list[PriorityBoost]]:
        """Compute one job's effective priority against an already-held connection.

        Walks the dependent tree (jobs that transitively depend on ``job``) via
        the same reverse edge ``get_dependent_jobs`` exposes, bounded by
        ``_REMEDIATION_ANCESTRY_LIMIT`` levels, to size the critical-path boost.
        """
        from datetime import datetime, timezone

        is_remediation = self._is_remediation_job(job.source_meta)
        unresolved_dependent_count = 0
        max_dependent_depth = 0
        seen = {job.id}
        frontier = [job.id]
        depth = 0
        while frontier and depth < _REMEDIATION_ANCESTRY_LIMIT:
            depth += 1
            rows = conn.execute(
                "SELECT DISTINCT j.id, j.status FROM jobs j "
                "JOIN job_dependencies d ON d.job_id = j.id "
                "WHERE d.depends_on_job_id = ANY(%s)",
                (frontier,),
            ).fetchall()
            next_frontier = []
            for row in rows:
                dependent_id = int(row["id"])
                if dependent_id in seen:
                    continue
                seen.add(dependent_id)
                next_frontier.append(dependent_id)
                if JobStatus(row["status"]) not in TERMINAL_JOB_STATUSES:
                    unresolved_dependent_count += 1
                    max_dependent_depth = max(max_dependent_depth, depth)
            frontier = next_frontier

        try:
            created = datetime.fromisoformat(job.created_at)
            age_hours = max(0.0, (datetime.now(timezone.utc) - created).total_seconds() / 3600.0)
        except (TypeError, ValueError):
            age_hours = 0.0

        return compute_effective_priority(
            job.priority,
            is_remediation=is_remediation,
            unresolved_dependent_count=unresolved_dependent_count,
            max_dependent_depth=max_dependent_depth,
            age_hours=age_hours,
        )

    def get_effective_priority(self, job: Job) -> tuple[int, list[dict]]:
        """Job's priority plus capped remediation/critical-path/aging boosts.

        Only PENDING jobs need scheduling boosts — anything else (running,
        done, failed, cancelled) returns the untouched base priority.
        """
        if job.status != JobStatus.PENDING:
            return job.priority, []
        with self._connection() as conn:
            effective, reasons = self._effective_priority_conn(conn, job)
        return effective, [reason.to_dict() for reason in reasons]

    def _resolve_remediation_lineage_conn(self, conn, job_id: int) -> RemediationLineage:
        current_id = job_id
        seen: set[int] = set()
        inferred_depth = 0
        for _ in range(_REMEDIATION_ANCESTRY_LIMIT):
            if current_id in seen:
                return RemediationLineage(min(seen), inferred_depth)
            seen.add(current_id)
            row = conn.execute(
                "SELECT id, source_meta FROM jobs WHERE id=%s", (current_id,)
            ).fetchone()
            if row is None:
                return RemediationLineage(current_id, inferred_depth)
            meta = self._source_meta(row)
            try:
                explicit_root = int(meta["remediation_root_job_id"])
                explicit_depth = int(meta.get("remediation_depth", inferred_depth))
            except (KeyError, TypeError, ValueError):
                explicit_root = 0
                explicit_depth = inferred_depth
            if explicit_root > 0:
                return RemediationLineage(explicit_root, max(0, explicit_depth))
            parent = meta.get("ai_fix_for", meta.get("fixes_job_id", meta.get("fix_for")))
            try:
                parent_id = int(parent)
            except (TypeError, ValueError):
                return RemediationLineage(current_id, inferred_depth)
            if parent_id <= 0:
                return RemediationLineage(current_id, inferred_depth)
            inferred_depth += 1
            current_id = parent_id
        return RemediationLineage(min(seen), inferred_depth)

    def resolve_remediation_lineage(self, job_id: int) -> RemediationLineage:
        """Resolve durable or legacy ``ai_fix_for`` ancestry, safely and boundedly."""
        with self._connection() as conn:
            return self._resolve_remediation_lineage_conn(conn, job_id)

    @staticmethod
    def _clear_active_remediation_conn(conn, remediation_job_id: int) -> None:
        conn.execute(
            "UPDATE jobs SET active_remediation_for=NULL WHERE active_remediation_for=%s",
            (remediation_job_id,),
        )

    def _propagate_successful_remediation_conn(
        self, conn, remediation_job_id: int, source_meta: object
    ) -> list[int]:
        """Resolve terminal incidents repaired by a job that reached ``DONE``.

        Explicit one-to-one fix links close their target immediately.  An
        ``ai_fix_for`` link can be one member of a supervisor-created batch, so
        its incident closes only after every sibling is either done or has
        itself been resolved by a later remediation.  Walking upward makes a
        successful fix of a failed child eligible to close its original parent.
        """
        from .models import _now

        direct_keys = (
            "fix_for",
            "fixes_job_id",
            "deploy_fix_for",
            "alembic_merge_fix_for",
        )
        queue: list[tuple[int, str, int]] = []

        def enqueue_links(meta_value: object, completed_job_id: int) -> None:
            meta = self._source_meta({"source_meta": meta_value})
            for key in direct_keys:
                try:
                    parent_id = int(meta[key])
                except (KeyError, TypeError, ValueError):
                    continue
                if parent_id > 0:
                    queue.append((parent_id, key, completed_job_id))
            try:
                parent_id = int(meta["ai_fix_for"])
            except (KeyError, TypeError, ValueError):
                return
            if parent_id > 0:
                queue.append((parent_id, "ai_fix_for", completed_job_id))

        enqueue_links(source_meta, remediation_job_id)
        resolved: list[int] = []
        seen: set[tuple[int, str]] = set()
        while queue and len(seen) < _REMEDIATION_ANCESTRY_LIMIT:
            incident_id, link_key, completed_job_id = queue.pop(0)
            marker = (incident_id, link_key)
            if marker in seen:
                continue
            seen.add(marker)
            if link_key == "ai_fix_for":
                unresolved_sibling = conn.execute(
                    "SELECT 1 FROM jobs WHERE source_meta->>'ai_fix_for'=%s "
                    "AND status<>%s AND COALESCE(resolution, '')<>%s LIMIT 1",
                    (str(incident_id), JobStatus.DONE.value, "resolved"),
                ).fetchone()
                if unresolved_sibling is not None:
                    continue
            row = conn.execute(
                "UPDATE jobs SET resolution=%s, updated_at=%s "
                "WHERE id=%s AND status IN (%s, %s) "
                "AND COALESCE(resolution, '')<>%s RETURNING source_meta",
                (
                    "resolved",
                    _now(),
                    incident_id,
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                    "resolved",
                ),
            ).fetchone()
            if row is None:
                existing = conn.execute(
                    "SELECT resolution, source_meta FROM jobs WHERE id=%s", (incident_id,)
                ).fetchone()
                if existing is None or existing["resolution"] != "resolved":
                    continue
                parent_meta = existing["source_meta"]
            else:
                resolved.append(incident_id)
                parent_meta = row["source_meta"]
                conn.execute(
                    "INSERT INTO supervisor_events(job_id, action, failure_class, detail) "
                    "VALUES (%s, %s, %s, %s)",
                    (
                        incident_id,
                        "auto_resolved_by_remediation",
                        "remediated",
                        json.dumps({"remediation_job_id": completed_job_id}),
                    ),
                )
            enqueue_links(parent_meta, incident_id)
        return resolved

    def propagate_successful_remediation(self, remediation_job_id: int) -> list[int]:
        """Reconcile closure for an already-successful remediation, idempotently."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT status, source_meta FROM jobs WHERE id=%s", (remediation_job_id,)
            ).fetchone()
            if row is None or row["status"] != JobStatus.DONE.value:
                return []
            return self._propagate_successful_remediation_conn(
                conn, remediation_job_id, row["source_meta"]
            )

    @staticmethod
    def _active_remediation_is_viable_conn(
        conn, active_remediation_job_id: int, incident_job_id: int
    ) -> bool:
        """Return whether an active remediation can run before its incident.

        A non-terminal remediation is still stale when it transitively depends on
        the incident it is meant to repair, or on another terminally failed job.
        """
        row = conn.execute(
            "WITH RECURSIVE deps(id) AS ("
            " SELECT depends_on_job_id FROM job_dependencies WHERE job_id=%s"
            " UNION"
            " SELECT d.depends_on_job_id FROM job_dependencies d JOIN deps ON d.job_id=deps.id"
            ") SELECT EXISTS(SELECT 1 FROM deps WHERE id=%s) AS depends_on_incident,"
            " EXISTS(SELECT 1 FROM deps JOIN jobs j ON j.id=deps.id "
            "WHERE j.status IN (%s, %s)) AS has_terminal_failure",
            (
                active_remediation_job_id,
                incident_job_id,
                JobStatus.FAILED.value,
                JobStatus.CANCELLED.value,
            ),
        ).fetchone()
        return not bool(row["depends_on_incident"] or row["has_terminal_failure"])

    def set_active_remediation(self, incident_job_id: int, remediation_job_id: int | None) -> None:
        """Record the active remediation on the resolved incident root."""
        with self._connection() as conn:
            lineage = self._resolve_remediation_lineage_conn(conn, incident_job_id)
            conn.execute(
                "UPDATE jobs SET active_remediation_for=%s WHERE id=%s",
                (remediation_job_id, lineage.root_job_id),
            )

    def supersede_failed_with_split(self, job_id: int, child_ids: list[int]) -> bool:
        """Cancel a failed parent after its saved plan has been split into children."""
        from .models import _now

        if not child_ids:
            raise ValueError("child_ids must not be empty")
        with self._connection() as conn:
            row = conn.execute(
                "UPDATE jobs SET status=%s, resolution=%s, error=%s, owner=NULL, "
                "lease_until=0, updated_at=%s WHERE id=%s AND status=%s RETURNING id",
                (
                    JobStatus.CANCELLED.value,
                    "superseded-by-saved-plan-split",
                    f"superseded by saved-plan jobs {child_ids}",
                    _now(),
                    job_id,
                    JobStatus.FAILED.value,
                ),
            ).fetchone()
            if row is not None:
                self._clear_active_remediation_conn(conn, job_id)
        return row is not None

    def get_active_remediation(self, incident_job_id: int) -> int | None:
        """Return the non-terminal active remediation for the resolved root."""
        with self._connection() as conn:
            lineage = self._resolve_remediation_lineage_conn(conn, incident_job_id)
            row = conn.execute(
                "SELECT r.active_remediation_for, a.status AS active_status "
                "FROM jobs r LEFT JOIN jobs a ON a.id=r.active_remediation_for WHERE r.id=%s",
                (lineage.root_job_id,),
            ).fetchone()
            if row is None or row["active_remediation_for"] is None:
                return None
            active_id = int(row["active_remediation_for"])
            if row["active_status"] in (
                JobStatus.DONE.value,
                JobStatus.FAILED.value,
                JobStatus.CANCELLED.value,
                None,
            ) or not self._active_remediation_is_viable_conn(conn, active_id, incident_job_id):
                conn.execute(
                    "UPDATE jobs SET active_remediation_for=NULL "
                    "WHERE id=%s AND active_remediation_for=%s",
                    (lineage.root_job_id, row["active_remediation_for"]),
                )
                return None
            return active_id

    def create_supervisor_remediation(
        self,
        incident_job_id: int,
        jobs_spec: list[dict],
        *,
        failure_class: str,
        source_branch: str | None = None,
        source_sha: str | None = None,
    ) -> SupervisorRemediationResult:
        """Atomically create one automated remediation chain for a root lineage.

        Each ``jobs_spec`` entry may carry ``depends_on_job_ids``: existing job
        ids (e.g. from a pre-creation ``survey_active_job_queue`` check) to
        depend the new job on, inserted alongside its intra-chain
        ``depends_on_indexes`` edges in the same transaction.
        """
        if not jobs_spec:
            raise ValueError("jobs_spec must not be empty")
        if bool(source_branch) != bool(source_sha):
            raise ValueError("remediation source branch and SHA must be provided together")
        with self._connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_JOB_DEPENDENCIES,))
            lineage = self._resolve_remediation_lineage_conn(conn, incident_job_id)
            root = conn.execute(
                "SELECT * FROM jobs WHERE id=%s FOR UPDATE", (lineage.root_job_id,)
            ).fetchone()
            incident = conn.execute("SELECT * FROM jobs WHERE id=%s", (incident_job_id,)).fetchone()
            if root is None or incident is None:
                raise ValueError(f"job {incident_job_id} not found")
            active_id = root["active_remediation_for"]
            if active_id is not None:
                active = conn.execute(
                    "SELECT status FROM jobs WHERE id=%s", (active_id,)
                ).fetchone()
                if (
                    active
                    and active["status"]
                    not in (
                        JobStatus.DONE.value,
                        JobStatus.FAILED.value,
                        JobStatus.CANCELLED.value,
                    )
                    and self._active_remediation_is_viable_conn(
                        conn, int(active_id), incident_job_id
                    )
                ):
                    return SupervisorRemediationResult([], lineage, int(active_id))
                conn.execute(
                    "UPDATE jobs SET active_remediation_for=NULL WHERE id=%s",
                    (lineage.root_job_id,),
                )

            next_depth = lineage.depth + 1
            created_ids: list[int] = []
            proposed_edges: list[tuple[int, int, DependencyProvenance]] = []
            for spec in jobs_spec:
                meta = dict(spec.get("source_meta") or {})
                meta.update(
                    {
                        "ai_fix_for": incident_job_id,
                        "failure_class": failure_class,
                        "remediation_root_job_id": lineage.root_job_id,
                        "remediation_depth": next_depth,
                    }
                )
                # Only roots of the remediation DAG need the failed candidate
                # delta. Descendants branch after their dependencies merge and
                # therefore already contain that exact source state.
                if source_branch and source_sha and not spec.get("depends_on_indexes"):
                    meta["remediation_source"] = {
                        "incident_job_id": incident_job_id,
                        "branch": source_branch,
                        "sha": source_sha,
                        "captured": False,
                    }
                new_id = self._create_job_conn(
                    conn,
                    idea=spec["idea"],
                    title=spec.get("title") or "",
                    repo_path=incident["repo_path"],
                    chat_id=incident["chat_id"],
                    project_id=incident["project_id"],
                    epic_id=incident["epic_id"],
                    priority=int(root["priority"]),
                    source=JobSource.SUPERVISOR,
                    source_actor="incident-analyst",
                    source_meta=meta,
                    initial_stage=Stage.QUEUED,
                )
                for dep_index in spec.get("depends_on_indexes") or []:
                    proposed_edges.append(
                        (new_id, created_ids[dep_index], DependencyProvenance.SEMANTIC)
                    )
                for external_id in spec.get("depends_on_job_ids") or []:
                    proposed_edges.append((new_id, external_id, DependencyProvenance.AUTO))
                created_ids.append(new_id)
            proposed_edges.append((incident_job_id, created_ids[-1], DependencyProvenance.SEMANTIC))
            rows = conn.execute("SELECT job_id, depends_on_job_id FROM job_dependencies").fetchall()
            edges: dict[int, set[int]] = {}
            for row in rows:
                edges.setdefault(int(row["job_id"]), set()).add(int(row["depends_on_job_id"]))
            for job_id, dependency_id, _provenance in proposed_edges:
                edges.setdefault(job_id, set()).add(dependency_id)
            nodes = set(edges) | {
                dependency_id for dependencies in edges.values() for dependency_id in dependencies
            }
            if self._dependency_topo_order(nodes, edges) is None:
                raise ValueError("supervisor remediation would create a dependency cycle")
            for job_id, dependency_id, provenance in proposed_edges:
                inserted = self._add_job_dependency_conn(conn, job_id, dependency_id, provenance)
                if inserted and provenance == DependencyProvenance.AUTO:
                    self._dependency_event_conn(
                        conn,
                        job_id,
                        "added",
                        dependency_id,
                        provenance.value,
                        "external_queue_collision",
                    )
            self._reconcile_auto_dependencies_conn(conn, int(incident["project_id"]))
            conn.execute(
                "UPDATE jobs SET active_remediation_for=%s WHERE id=%s",
                (created_ids[-1], lineage.root_job_id),
            )
        created = []
        for created_id in created_ids:
            created_job = self.get(created_id)
            if created_job is not None:
                created.append(created_job)
        return SupervisorRemediationResult(
            created, RemediationLineage(lineage.root_job_id, next_depth)
        )

    @staticmethod
    def _baseline_security_identity(source_meta: object) -> tuple[str, ...] | None:
        """Read current and legacy baseline-remediation identity metadata safely."""
        if not isinstance(source_meta, dict):
            return None
        candidates = [
            source_meta.get("baseline_security_remediation"),
            source_meta.get("security_remediation"),
            source_meta,
        ]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            raw_ids = candidate.get("advisory_ids")
            if not isinstance(raw_ids, (list, tuple)):
                continue
            ids = sorted(
                {value.strip() for value in raw_ids if isinstance(value, str) and value.strip()}
            )
            if ids and len(ids) == len(raw_ids):
                return tuple(ids)
        return None

    def file_baseline_security_remediation(
        self,
        triggering_job_id: int,
        findings: list[dict],
        verification_commands: list[str],
        *,
        blocking: bool = False,
    ) -> BaselineSecurityRemediationResult:
        """Create or reuse one active remediation job for an exact advisory set.

        The project row is the serialization lock, making lookup and creation one
        atomic decision across workers without introducing another ledger.
        """
        advisory_ids = tuple(
            sorted(
                {
                    finding["advisory_id"].strip()
                    for finding in findings
                    if isinstance(finding, dict)
                    and isinstance(finding.get("advisory_id"), str)
                    and finding["advisory_id"].strip()
                }
            )
        )
        if not advisory_ids:
            raise ValueError("baseline remediation requires advisory IDs")
        commands = list(
            dict.fromkeys(
                command.strip()
                for command in verification_commands
                if isinstance(command, str) and command.strip()
            )
        )
        with self._connection() as conn:
            trigger = conn.execute(
                "SELECT * FROM jobs WHERE id=%s", (triggering_job_id,)
            ).fetchone()
            if trigger is None:
                raise ValueError(f"job {triggering_job_id} not found")
            conn.execute(
                "SELECT id FROM projects WHERE id=%s FOR UPDATE",
                (trigger["project_id"],),
            ).fetchone()
            rows = conn.execute(
                "WITH RECURSIVE depends_on_trigger(id) AS ("
                "SELECT job_id FROM job_dependencies WHERE depends_on_job_id=%s "
                "UNION "
                "SELECT dependency.job_id FROM job_dependencies dependency "
                "JOIN depends_on_trigger ancestor "
                "ON dependency.depends_on_job_id=ancestor.id"
                ") "
                "SELECT * FROM jobs WHERE project_id=%s AND source=%s "
                "AND id<>%s AND id NOT IN (SELECT id FROM depends_on_trigger) "
                "AND status NOT IN (%s, %s, %s) "
                "AND archived=FALSE ORDER BY id",
                (
                    triggering_job_id,
                    trigger["project_id"],
                    JobSource.SUPERVISOR.value,
                    triggering_job_id,
                    JobStatus.DONE.value,
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                ),
            ).fetchall()
            remediation_row = next(
                (
                    row
                    for row in rows
                    if self._baseline_security_identity(self._source_meta(row)) == advisory_ids
                ),
                None,
            )
            created = remediation_row is None
            if created:
                identity = {
                    "kind": "npm_unchanged_baseline",
                    "advisory_ids": list(advisory_ids),
                }
                evidence = [dict(finding) for finding in findings]
                idea = (
                    "Remediate unchanged baseline npm security advisories\n\n"
                    f"Advisory IDs: {', '.join(advisory_ids)}\n\n"
                    "Use the structured findings and verification commands in "
                    "source_meta.baseline_security_remediation."
                )
                remediation_id = self._create_job_conn(
                    conn,
                    idea=idea,
                    title="Remediate baseline npm security debt",
                    repo_path=trigger["repo_path"],
                    chat_id=trigger["chat_id"],
                    project_id=trigger["project_id"],
                    epic_id=None,
                    priority=int(trigger["priority"]),
                    source=JobSource.SUPERVISOR,
                    source_actor="security-stage",
                    source_meta={
                        "filing_channel": "supervisor",
                        "baseline_security_remediation": {
                            **identity,
                            "findings": evidence,
                            "verification_commands": commands,
                            "triggering_job_id": triggering_job_id,
                        },
                    },
                )
                remediation_row = conn.execute(
                    "SELECT * FROM jobs WHERE id=%s", (remediation_id,)
                ).fetchone()
            if blocking:
                self._add_job_dependency_conn(conn, triggering_job_id, remediation_row["id"])
                from .models import _now

                conn.execute(
                    "UPDATE jobs SET status=%s, stage=%s, owner=NULL, lease_until=0, "
                    "executing_step=NULL, error='', updated_at=%s WHERE id=%s",
                    (
                        JobStatus.PENDING.value,
                        Stage.REVIEW.value,
                        _now(),
                        triggering_job_id,
                    ),
                )
        return BaselineSecurityRemediationResult(
            Job.from_row(remediation_row),
            created,
            blocking,
            advisory_ids,
        )

    def reconcile_to_done(self, job_id: int) -> None:
        """Force a stuck job to DONE, bypassing the terminal-state guard in save().

        Used by the janitor to reconcile jobs that were already merged on GitHub but
        whose runner died before advancing the local state to DONE.
        """
        from .models import _now

        with self._connection() as conn:
            conn.execute(
                "UPDATE jobs SET status=%s, stage=%s, error='', failure='', executing_step=NULL, "
                "failed_step=NULL, failure_code=NULL, failure_origin=NULL, "
                "retry_disposition=NULL, failure_detail=NULL, updated_at=%s"
                " WHERE id=%s AND status=%s",
                (JobStatus.DONE.value, Stage.DONE.value, _now(), job_id, JobStatus.FAILED.value),
            )
            self._clear_active_remediation_conn(conn, job_id)

    def list_provider_pauses(self, now: float) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT key, value FROM meta WHERE key LIKE %s", (self._PAUSE_PREFIX + "%",)
            ).fetchall()
        out = []
        for r in rows:
            try:
                until = float(r["value"])
            except (TypeError, ValueError):
                continue
            if until > now:
                out.append({"provider": r["key"][len(self._PAUSE_PREFIX) :], "until": until})
        return out

    def stuck_at_merge(self) -> list[Job]:
        """Jobs stuck in RUNNING or FAILED at the REVIEW, SECURITY, or MERGE stage."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status IN (%s, %s) AND stage IN (%s, %s, %s) ORDER BY id",
                (
                    JobStatus.RUNNING.value,
                    JobStatus.FAILED.value,
                    Stage.REVIEW.value,
                    Stage.SECURITY.value,
                    Stage.MERGE.value,
                ),
            ).fetchall()
        return [Job.from_row(r) for r in rows]

    def get_deploying_jobs(self) -> list[Job]:
        """Jobs waiting for post-restart commit verification."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status = %s ORDER BY id",
                (JobStatus.DEPLOYING.value,),
            ).fetchall()
        return [Job.from_row(r) for r in rows]

    def save_deploying(self, job_id: int, deployed_commit: str) -> None:
        """Atomically mark a job DEPLOYING with its commit hash. Skips if cancelled."""
        if not deployed_commit:
            raise RuntimeError(f"save_deploying: deployed_commit must be non-empty (job {job_id})")
        from .models import _now

        with self._connection() as conn:
            conn.execute(
                "UPDATE jobs SET status=%s, stage=%s, deployed_commit=%s, executing_step=NULL, "
                "updated_at=%s "
                "WHERE id=%s AND status NOT IN (%s)",
                (
                    JobStatus.DEPLOYING.value,
                    Stage.DEPLOY.value,
                    deployed_commit,
                    _now(),
                    job_id,
                    JobStatus.CANCELLED.value,
                ),
            )

    def finalize_deploy_done(self, job_id: int, deployed_commit: str) -> None:
        """Atomically mark a job DONE with its verified commit hash. Skips if cancelled."""
        if not deployed_commit:
            raise RuntimeError(
                f"finalize_deploy_done: deployed_commit must be non-empty (job {job_id})"
            )
        from .models import _now

        with self._connection() as conn:
            conn.execute(
                "UPDATE jobs SET status=%s, stage=%s, deployed_commit=%s, "
                "failure='', error='', executing_step=NULL, failed_step=NULL, failure_code=NULL, "
                "failure_origin=NULL, retry_disposition=NULL, failure_detail=NULL, updated_at=%s "
                "WHERE id=%s AND status NOT IN (%s)",
                (
                    JobStatus.DONE.value,
                    Stage.DONE.value,
                    deployed_commit,
                    _now(),
                    job_id,
                    JobStatus.CANCELLED.value,
                ),
            )
            self._clear_active_remediation_conn(conn, job_id)

    def finalize_deploying_job(self, job_id: int, *, deployed_commit: str | None = None) -> None:
        """Transition a DEPLOYING job to DONE. No-op if status is not 'deploying'."""
        from .models import _now

        with self._connection() as conn:
            conn.execute(
                "UPDATE jobs SET status=%s, stage=%s, "
                "deployed_commit=COALESCE(%s, deployed_commit), failure='', error='', "
                "executing_step=NULL, failed_step=NULL, failure_code=NULL, "
                "failure_origin=NULL, retry_disposition=NULL, failure_detail=NULL, "
                "updated_at=%s "
                "WHERE id=%s AND status=%s",
                (
                    JobStatus.DONE.value,
                    Stage.DONE.value,
                    deployed_commit,
                    _now(),
                    job_id,
                    JobStatus.DEPLOYING.value,
                ),
            )
            self._clear_active_remediation_conn(conn, job_id)

    def _stale_deploy_lock_owner_terminal(self, owner: str) -> bool:
        """True if ``owner`` (``"job-<id>"``) names a job that is terminal/archived/missing.

        A terminal or archived job cannot still be deploying, so a lock row it
        still holds is a leak from a process that died mid-deploy.
        """
        m = re.match(r"^job-(\d+)$", owner)
        if not m:
            return False
        job = self.get(int(m.group(1)))
        if job is None:
            return True
        return job.archived or job.status in (
            JobStatus.DONE,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        )

    def _deploy_lock_is_stale(self, owner: str, acquired_at: str, stale_ttl_s: float) -> bool:
        """True if a deploy_locks row is safe to reap: dead owner, or past its max hold."""
        if self._stale_deploy_lock_owner_terminal(owner):
            return True
        from datetime import datetime, timezone

        try:
            acquired = datetime.fromisoformat(acquired_at)
            age_s = (datetime.now(timezone.utc) - acquired).total_seconds()
        except (TypeError, ValueError):
            return False
        return age_s >= stale_ttl_s

    def _resolve_environment_id(self, project_id: int, environment_id: int | None) -> int:
        """``environment_id`` if given, else *project_id*'s implicit prod environment."""
        if environment_id is not None:
            return environment_id
        return self.get_or_create_default_environment(project_id).id

    def _takeover_stale_deploy_lock(
        self,
        project_id: int,
        new_owner: str,
        stale_ttl_s: float,
        *,
        environment_id: int | None = None,
    ) -> bool:
        """Atomically replace a stale deploy_locks row with one owned by ``new_owner``.

        Reads the current row, then deletes it conditioned on the exact
        (owner, acquired_at) just observed — if that DELETE affects 0 rows, a
        concurrent reaper already won, and this call reports no takeover.
        """
        from .models import _now

        env_id = self._resolve_environment_id(project_id, environment_id)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT owner, acquired_at FROM deploy_locks WHERE environment_id = %s",
                (env_id,),
            ).fetchone()
        if row is None:
            return False
        owner, acquired_at = row["owner"], row["acquired_at"]
        if not self._deploy_lock_is_stale(owner, acquired_at, stale_ttl_s):
            return False
        with self._connection() as conn:
            deleted = conn.execute(
                "DELETE FROM deploy_locks WHERE environment_id = %s AND owner = %s AND acquired_at = %s",
                (env_id, owner, acquired_at),
            )
            if deleted.rowcount == 0:
                return False
            conn.execute(
                "INSERT INTO deploy_locks(project_id, owner, acquired_at, environment_id) "
                "VALUES (%s, %s, %s, %s)",
                (project_id, new_owner, _now(), env_id),
            )
        _log.warning(
            "deploy lock takeover: project %s reaped stale owner=%s acquired_at=%s new_owner=%s",
            project_id,
            owner,
            acquired_at,
            new_owner,
        )
        return True

    async def acquire_deploy_lock(
        self,
        project_id: int,
        owner: str,
        timeout_s: float = 120.0,
        *,
        stale_ttl_s: float = 3600.0,
        environment_id: int | None = None,
    ) -> bool:
        """Spin-wait until the per-environment deploy lock is free. Returns False on timeout.

        A lock row is taken over immediately (skipping the wait) when its owner
        job is terminal/archived/missing, or it has been held past
        ``stale_ttl_s`` — guards against a leaked row from a process that died
        mid-deploy (see Job #1215). Callers that only pass ``project_id``
        resolve to that project's implicit 'prod' environment.
        """
        import time as _time

        from .models import _now

        env_id = self._resolve_environment_id(project_id, environment_id)
        deadline = _time.monotonic() + timeout_s
        while True:
            try:
                with self._connection() as conn:
                    conn.execute(
                        "INSERT INTO deploy_locks(project_id, owner, acquired_at, environment_id)"
                        " VALUES (%s, %s, %s, %s)",
                        (project_id, owner, _now(), env_id),
                    )
                return True
            except psycopg.errors.UniqueViolation:
                if self._takeover_stale_deploy_lock(
                    project_id, owner, stale_ttl_s, environment_id=env_id
                ):
                    return True
            if _time.monotonic() >= deadline:
                return False
            await asyncio.sleep(3)

    def sweep_stale_deploy_locks(self) -> list[dict]:
        """Delete deploy_locks rows whose owner job is terminal/archived/missing.

        Meant to run once at pipeline boot, clearing leaks left by a process
        that died mid-deploy immediately instead of waiting for the next
        acquire's TTL-based takeover. Returns the reaped {project_id, owner,
        environment_id} rows.
        """
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT project_id, owner, environment_id FROM deploy_locks"
            ).fetchall()
        reaped = [
            {
                "project_id": r["project_id"],
                "owner": r["owner"],
                "environment_id": r["environment_id"],
            }
            for r in rows
            if self._stale_deploy_lock_owner_terminal(r["owner"])
        ]
        if reaped:
            with self._connection() as conn:
                for r in reaped:
                    if r["environment_id"] is not None:
                        conn.execute(
                            "DELETE FROM deploy_locks WHERE environment_id = %s AND owner = %s",
                            (r["environment_id"], r["owner"]),
                        )
                    else:
                        conn.execute(
                            "DELETE FROM deploy_locks WHERE project_id = %s "
                            "AND owner = %s AND environment_id IS NULL",
                            (r["project_id"], r["owner"]),
                        )
            for r in reaped:
                _log.warning(
                    "startup: reaped stale deploy lock project=%s owner=%s",
                    r["project_id"],
                    r["owner"],
                )
        return reaped

    def release_deploy_lock(
        self, project_id: int, owner: str, *, environment_id: int | None = None
    ) -> None:
        """Release the per-environment deploy lock held by owner."""
        env_id = self._resolve_environment_id(project_id, environment_id)
        with self._connection() as conn:
            conn.execute(
                "DELETE FROM deploy_locks WHERE environment_id = %s AND owner = %s",
                (env_id, owner),
            )

    def record_deploy(
        self,
        project_id: int,
        deployed_commit: str,
        previous_commit: str | None,
        trigger: str,
        *,
        job_id: int | None = None,
        verified: bool = True,
        summary: str | None = None,
        environment_id: int | None = None,
        image_ref: str | None = None,
        image_digest: str | None = None,
        signed: bool = False,
    ) -> None:
        from .models import _now

        env_id = self._resolve_environment_id(project_id, environment_id)
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO deploys(project_id, deployed_commit, previous_commit, deployed_at, "
                "verified, trigger, job_id, summary, environment_id, image_ref, image_digest, signed) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    project_id,
                    deployed_commit,
                    previous_commit,
                    _now(),
                    verified,
                    trigger,
                    job_id,
                    summary,
                    env_id,
                    image_ref,
                    image_digest,
                    signed,
                ),
            )

    def get_last_deploy(self, project_id: int, *, environment_id: int | None = None) -> dict | None:
        env_id = self._resolve_environment_id(project_id, environment_id)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT deployed_commit, deployed_at FROM deploys "
                "WHERE environment_id = %s ORDER BY deployed_at DESC LIMIT 1",
                (env_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "deployed_commit": str(row["deployed_commit"]),
            "finished_at": str(row["deployed_at"]),
        }

    def list_deploys(self, project_id: int, limit: int = 50) -> list[dict]:
        """Return up to *limit* deploy rows for *project_id*, newest first."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT id, deployed_commit, previous_commit, deployed_at, verified, trigger, job_id "
                "FROM deploys WHERE project_id = %s ORDER BY deployed_at DESC LIMIT %s",
                (project_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_active_deploy_job(self, project_id: int) -> Job | None:
        """Return the most-recent PENDING/RUNNING/DEPLOYING deploy-stage job for this project."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE project_id = %s AND stage = 'deploy' "
                "AND status IN ('pending', 'running', 'deploying') "
                "ORDER BY id DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        return Job.from_row(row) if row else None

    def get_active_project_job(self, project_id: int) -> Job | None:
        """Return the most-recent active (any stage) job for this project, or None.

        Broader than get_active_deploy_job: matches a job in any stage, not just
        stage='deploy'. Used by the auto-deploy poller to detect an in-flight
        feature/fix job that will ship the current origin advance itself.
        """
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE project_id = %s "
                "AND status IN ('pending', 'running', 'deploying') "
                "ORDER BY id DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        return Job.from_row(row) if row else None

    # Maps the `status` filter param to an SQL WHERE clause fragment.
    # Values are hardcoded strings (not user data), so f-string interpolation is safe.
    _STATUS_CLAUSES: dict[str, str | None] = {
        "active": "status IN ('pending','running','deploying') AND archived = FALSE",
        "pending": "status = 'pending' AND archived = FALSE",
        "running": "status = 'running' AND archived = FALSE",
        "deploying": "status = 'deploying' AND archived = FALSE",
        "done": "status = 'done' AND archived = FALSE",
        "failed": "status = 'failed' AND archived = FALSE",
        "cancelled": "status = 'cancelled' AND archived = FALSE",
        "terminal": "status IN ('done','failed','cancelled') AND archived = FALSE",
        "archived": "archived = TRUE",
        "all": None,
    }

    def list_jobs_page(
        self,
        project_id: int,
        status: str | None = None,
        cursor: int | None = None,
    ) -> JobsPage:
        """Return up to 50 project jobs ordered newest-first by stable id."""
        status = status or "active"
        if status not in self._STATUS_CLAUSES:
            raise ValueError(f"unknown job status filter: {status}")
        if cursor is not None and (
            not isinstance(cursor, int) or isinstance(cursor, bool) or cursor <= 0
        ):
            raise ValueError("cursor must be a positive integer")

        conditions = ["project_id = %s"]
        params: list[object] = [project_id]
        clause = self._STATUS_CLAUSES[status]
        if clause:
            conditions.append(clause)
        if cursor is not None:
            conditions.append("id < %s")
            params.append(cursor)
        params.append(51)
        where = " AND ".join(conditions)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM jobs WHERE {where} ORDER BY id DESC LIMIT %s",  # nosec B608
                tuple(params),
            ).fetchall()

        jobs = [Job.from_row(row) for row in rows[:50]]
        next_cursor = jobs[-1].id if len(rows) > 50 else None
        return JobsPage(jobs=jobs, next_cursor=next_cursor)

    def list_active(
        self,
        limit: int = 20,
        project_id: int | None = None,
        epic_id: int | None = None,
        status: str = "active",
    ) -> list[Job]:
        clause = self._STATUS_CLAUSES.get(status)
        with self._connection() as conn:
            if project_id is not None:
                where = f"project_id = %s{' AND ' + clause if clause else ''}"
                rows = conn.execute(
                    f"SELECT * FROM jobs WHERE {where} ORDER BY id DESC LIMIT %s",
                    (project_id, limit),
                ).fetchall()
            elif epic_id is not None:
                where = f"epic_id = %s{' AND ' + clause if clause else ''}"
                rows = conn.execute(
                    f"SELECT * FROM jobs WHERE {where} ORDER BY id DESC LIMIT %s",
                    (epic_id, limit),
                ).fetchall()
            else:
                if clause:
                    rows = conn.execute(
                        f"SELECT * FROM jobs WHERE {clause} ORDER BY id DESC LIMIT %s",
                        (limit,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM jobs ORDER BY id DESC LIMIT %s", (limit,)
                    ).fetchall()
        return [Job.from_row(r) for r in rows]

    def mark_needs_split(self, job_id: int) -> Job | None:
        """Park a job whose oversized plan survived the bounded re-ask.

        Sets both ``needs_split`` and ``archived`` so it drops out of the active
        view and ``retry()`` refuses to requeue it — the submitter must refile a
        smaller job instead.
        """
        from .models import _now

        with self._connection() as conn:
            row = conn.execute(
                "UPDATE jobs SET needs_split = TRUE, archived = TRUE, updated_at = %s "
                "WHERE id = %s RETURNING *",
                (_now(), job_id),
            ).fetchone()
        return Job.from_row(row) if row else None

    def set_archived(self, job_id: int, value: bool) -> bool:
        from .models import _now

        with self._connection() as conn:
            result = conn.execute(
                "UPDATE jobs SET archived = %s, updated_at = %s WHERE id = %s",
                (value, _now(), job_id),
            )
            return result.rowcount > 0

    def set_archived_epic(self, epic_id: int, value: bool) -> bool:
        from .models import _now

        with self._connection() as conn:
            result = conn.execute(
                "UPDATE epics SET archived = %s, updated_at = %s WHERE id = %s",
                (value, _now(), epic_id),
            )
            return result.rowcount > 0

    def update_job_epic(self, job_id: int, epic_id: int | None) -> bool:
        from .models import _now

        with self._connection() as conn:
            result = conn.execute(
                "UPDATE jobs SET epic_id = %s, updated_at = %s WHERE id = %s",
                (epic_id, _now(), job_id),
            )
            return result.rowcount > 0

    def set_priority(self, job_id: int, value: int) -> bool:
        from .models import _now

        with self._connection() as conn:
            result = conn.execute(
                "UPDATE jobs SET priority = %s, updated_at = %s WHERE id = %s",
                (value, _now(), job_id),
            )
            return result.rowcount > 0

    def update_job_title(self, job_id: int, title: str) -> bool:
        from .models import _now

        with self._connection() as conn:
            result = conn.execute(
                "UPDATE jobs SET title=%s, updated_at=%s WHERE id=%s",
                (title, _now(), job_id),
            )
            return result.rowcount > 0

    def update_job_idea(self, job_id: int, idea: str) -> bool:
        from .models import _now

        with self._connection() as conn:
            result = conn.execute(
                "UPDATE jobs SET idea=%s, updated_at=%s WHERE id=%s",
                (idea, _now(), job_id),
            )
            return result.rowcount > 0

    def update_job_fields(
        self,
        job_id: int,
        *,
        title: object = _UNSET,
        idea: object = _UNSET,
        priority: object = _UNSET,
        epic_id: object = _UNSET,
    ) -> Job | None:
        from .models import _now

        fields: dict[str, object] = {}
        if title is not _UNSET:
            fields["title"] = title
        if idea is not _UNSET:
            fields["idea"] = idea
        if priority is not _UNSET:
            fields["priority"] = priority
        if epic_id is not _UNSET:
            fields["epic_id"] = epic_id
        if not fields:
            return self.get(job_id)

        fields["updated_at"] = _now()
        set_clause = ", ".join(f"{k}=%s" for k in fields)
        values = list(fields.values()) + [job_id]
        with self._connection() as conn:
            conn.execute(f"UPDATE jobs SET {set_clause} WHERE id=%s", values)  # nosec B608
        return self.get(job_id)

    def set_job_resolution(self, job_id: int, resolution: str) -> bool:
        from .models import _now

        with self._connection() as conn:
            result = conn.execute(
                "UPDATE jobs SET resolution=%s, updated_at=%s WHERE id=%s",
                (resolution, _now(), job_id),
            )
            return result.rowcount > 0

    def get_dependencies(self, job_id: int) -> list[int]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT depends_on_job_id FROM job_dependencies WHERE job_id=%s ORDER BY depends_on_job_id",
                (job_id,),
            ).fetchall()
        return [int(r["depends_on_job_id"]) for r in rows]

    def get_unsatisfied_deps(self, job_id: int) -> list[int]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT d.depends_on_job_id FROM job_dependencies d "
                "JOIN jobs p ON p.id=d.depends_on_job_id "
                "WHERE d.job_id=%s AND p.status != %s "
                "AND COALESCE(p.resolution, '') != %s ORDER BY d.depends_on_job_id",
                (
                    job_id,
                    _DEPENDENCY_SATISFIED_STATUS,
                    _DEPENDENCY_SATISFIED_RESOLUTION,
                ),
            ).fetchall()
        return [int(r["depends_on_job_id"]) for r in rows]

    def dependency_block_still_applies(self, job: Job) -> bool:
        """True if the dependency recorded in ``job.failure_detail`` is still unsatisfied.

        Returns True (still genuinely blocked) when there is no recorded
        ``dependency_id`` to check, or when it is still present in
        ``get_unsatisfied_deps``. Returns False once that specific dependency
        landed DONE, was manually resolved, or its edge was rewired onto a
        different dependency.
        """
        dep_id = (job.failure_detail or {}).get("dependency_id")
        if dep_id is None:
            return True
        return dep_id in self.get_unsatisfied_deps(job.id)

    def get_dependent_jobs(self, job_id: int) -> "list[Job]":
        """Jobs whose job_dependencies point at ``job_id`` (reverse lookup)."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT j.* FROM jobs j JOIN job_dependencies d ON d.job_id = j.id "
                "WHERE d.depends_on_job_id = %s ORDER BY j.id",
                (job_id,),
            ).fetchall()
        return [Job.from_row(r) for r in rows]

    def get_dependency_jobs(self, job_id: int) -> "list[Job]":
        """Jobs that ``job_id`` depends on (forward lookup)."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT j.* FROM jobs j JOIN job_dependencies d ON d.depends_on_job_id = j.id "
                "WHERE d.job_id = %s ORDER BY j.id",
                (job_id,),
            ).fetchall()
        return [Job.from_row(r) for r in rows]

    def repoint_split_dependents(self, parent_id: int, child_ids: list[int]) -> list[int]:
        """Atomically move every dependent of ``parent_id`` onto ``child_ids``.

        Used only when ``parent_id`` was cancelled with resolution
        superseded-by-split: rewrites job_dependencies so each dependent that
        waited on the parent now waits on the split DAG's terminal (sink)
        children only — not every child — instead (deduped if it already
        depended on one of them). A child is terminal when no other id in
        ``child_ids`` has a job_dependencies row depending on it, i.e. no
        split sibling was created with it as a predecessor (see
        ``_supersede_with_split`` in stages/plan.py, which persists those
        predecessor edges via ``store.create(depends_on=...)``). When none of
        ``child_ids`` depend on each other (today's independent-children
        usage, and every historical split predating this DAG behavior), every
        child is terminal and the fan-out is unchanged from before. Before any
        mutation, validates the resulting hypothetical graph (existing edges
        minus the parent-pointing rows, plus the candidate fan-out edges) is
        acyclic via ``_dependency_topo_order`` — mirroring
        ``add_job_dependency``'s whole-graph safety net — and raises
        ``ValueError`` without touching ``job_dependencies`` if a cycle would
        result. On success, immediately reconciles the project's auto
        dependencies under the same held lock (the same direct-call pattern
        the supervisor-remediation path already uses) so a child edge that is
        now known-disjoint scope is removed right away instead of waiting for
        the periodic sweep. Returns the ids of dependents that were rewritten
        ([] if the parent had none).
        """
        with self._connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_JOB_DEPENDENCIES,))
            rows = conn.execute(
                "SELECT job_id, provenance FROM job_dependencies WHERE depends_on_job_id = %s",
                (parent_id,),
            ).fetchall()
            dep_ids = list(dict.fromkeys(int(r["job_id"]) for r in rows))
            if not dep_ids:
                return []
            sibling_edges = conn.execute(
                "SELECT depends_on_job_id FROM job_dependencies "
                "WHERE job_id = ANY(%s) AND depends_on_job_id = ANY(%s)",
                (child_ids, child_ids),
            ).fetchall()
            non_terminal = {int(r["depends_on_job_id"]) for r in sibling_edges}
            terminal_ids = [cid for cid in child_ids if cid not in non_terminal]
            fan_out = repoint_dependency_provenance(
                [(int(r["job_id"]), r["provenance"]) for r in rows], terminal_ids
            )

            all_rows = conn.execute(
                "SELECT job_id, depends_on_job_id FROM job_dependencies"
            ).fetchall()
            edges: dict[int, set[int]] = {}
            for row in all_rows:
                edges.setdefault(int(row["job_id"]), set()).add(int(row["depends_on_job_id"]))
            for dep_id in dep_ids:
                edges[dep_id] = edges.get(dep_id, set()) - {parent_id}
            for dependent_id, child_id, _provenance in fan_out:
                edges.setdefault(dependent_id, set()).add(child_id)
            nodes = set(edges) | {v for deps in edges.values() for v in deps}
            if self._dependency_topo_order(nodes, edges) is None:
                raise ValueError(
                    f"repointing job {parent_id}'s dependents onto {child_ids} "
                    "would create a dependency cycle"
                )

            conn.execute(
                "DELETE FROM job_dependencies WHERE depends_on_job_id = %s",
                (parent_id,),
            )
            for dependent_id, child_id, provenance in fan_out:
                self._add_job_dependency_conn(
                    conn, dependent_id, child_id, DependencyProvenance(provenance)
                )

            project_row = conn.execute(
                "SELECT project_id FROM jobs WHERE id = %s", (parent_id,)
            ).fetchone()
            if project_row is not None:
                self._reconcile_auto_dependencies_conn(conn, int(project_row["project_id"]))
        return dep_ids

    def list_split_parents_with_pending_dependents(self) -> list[dict]:
        """superseded-by-split parents that still have a job_dependencies row pointing at them.

        Reads each parent's split children out of its recorded plan-split
        event (``detail.superseded_by``). Startup-reconcile candidate list for
        healing legacy orphans left over before edge-transfer existed at split
        time; once ``repoint_split_dependents`` runs for a parent, it drops
        out of this list (no more job_dependencies rows point at it).
        """
        with self._connection() as conn:
            parent_rows = conn.execute(
                "SELECT DISTINCT p.id FROM jobs p "
                "JOIN job_dependencies d ON d.depends_on_job_id = p.id "
                "WHERE p.resolution = %s",
                ("superseded-by-split",),
            ).fetchall()
            out: list[dict] = []
            for pr in parent_rows:
                parent_id = int(pr["id"])
                event_row = conn.execute(
                    "SELECT detail FROM job_events WHERE job_id = %s AND stage = 'plan' "
                    "AND status = 'cancelled' AND summary LIKE %s ORDER BY id DESC LIMIT 1",
                    (parent_id, "plan split into%"),
                ).fetchone()
                if event_row is None:
                    continue
                try:
                    detail = json.loads(event_row["detail"]) if event_row["detail"] else {}
                except (TypeError, ValueError):
                    detail = {}
                child_ids = detail.get("superseded_by") or []
                if child_ids:
                    out.append({"parent_id": parent_id, "child_ids": [int(c) for c in child_ids]})
        return out

    def fix_forward_and_repoint(
        self,
        job_id: int,
        idea: str,
        title: str,
        source_actor: str,
        repoint_job_ids: list[int],
    ) -> Job:
        """File a fix-forward job for ``job_id``, re-point selected dependents onto
        it, and archive ``job_id`` — all in one transaction.

        Only the dependents listed in ``repoint_job_ids`` have their edge to
        ``job_id`` swapped to the new job; each dependent's other dependencies
        are left untouched. The existing supervisor dependency-cascade picks
        up the repointed dependents automatically once the new job reaches
        DONE — no separate cascade is triggered here.
        """
        from .models import _now

        failed = self.get(job_id)
        if failed is None:
            raise ValueError(f"job {job_id} not found")
        project = self.ensure_project_for_repo(failed.repo_path)
        lineage = self.resolve_remediation_lineage(job_id)
        source_meta = {"fix_for": job_id}
        if lineage.root_job_id != job_id or lineage.depth > 0:
            source_meta.update(
                {
                    "remediation_root_job_id": lineage.root_job_id,
                    "remediation_depth": lineage.depth,
                }
            )
        if not title:
            title = idea.split("\n")[0].strip()[:80]
        now = _now()
        with self._connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_JOB_DEPENDENCIES,))
            row = conn.execute(
                "INSERT INTO jobs(idea, title, repo_path, chat_id, project_id, epic_id, priority, "
                "source, source_actor, source_meta, stage, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    idea,
                    title,
                    failed.repo_path,
                    failed.chat_id,
                    project.id,
                    failed.epic_id,
                    failed.priority,
                    JobSource.UI.value,
                    source_actor,
                    json.dumps(source_meta),
                    Stage.QUEUED.value,
                    now,
                    now,
                ),
            ).fetchone()
            new_job_id = int(row["id"])
            if repoint_job_ids:
                rows = conn.execute(
                    "SELECT job_id, provenance FROM job_dependencies "
                    "WHERE depends_on_job_id=%s AND job_id=ANY(%s)",
                    (job_id, repoint_job_ids),
                ).fetchall()
                conn.execute(
                    "DELETE FROM job_dependencies WHERE depends_on_job_id=%s AND job_id=ANY(%s)",
                    (job_id, repoint_job_ids),
                )
                for dependency in rows:
                    provenance = DependencyProvenance(dependency["provenance"])
                    if provenance == DependencyProvenance.AUTO:
                        provenance = DependencyProvenance.SEMANTIC
                    self._add_job_dependency_conn(
                        conn, int(dependency["job_id"]), new_job_id, provenance
                    )
            conn.execute(
                "UPDATE jobs SET archived = TRUE, updated_at = %s WHERE id = %s",
                (now, job_id),
            )
        return self.get(new_job_id)  # type: ignore[return-value]

    def list_pending_with_unsatisfied_deps(self) -> "list[Job]":
        """PENDING jobs blocked on a dependency that is not satisfied yet.

        Candidate list for the janitor's dependency-propagation scan: for each
        job returned here, the caller inspects its blocking deps via
        ``get_unsatisfied_deps``/``get`` to tell a dependency that's merely
        retrying apart from one that's genuinely terminal.
        """
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT j.* FROM jobs j "
                "JOIN job_dependencies d ON d.job_id=j.id "
                "JOIN jobs p ON p.id=d.depends_on_job_id "
                "WHERE j.status=%s AND p.status<>%s "
                "AND COALESCE(p.resolution, '')<>%s",
                (
                    JobStatus.PENDING.value,
                    _DEPENDENCY_SATISFIED_STATUS,
                    _DEPENDENCY_SATISFIED_RESOLUTION,
                ),
            ).fetchall()
        return [Job.from_row(r) for r in rows]

    def archive_terminal(self, project_id: int | None = None) -> list[Job]:
        """Archive every unarchived terminal job (optionally scoped to a project).

        Returns the jobs it archived (via ``RETURNING``) so the caller can tear
        down each one's git/GitHub artifacts.
        """
        from .models import _now

        with self._connection() as conn:
            if project_id is not None:
                rows = conn.execute(
                    "UPDATE jobs SET archived = TRUE, updated_at = %s "
                    "WHERE status IN ('done','failed','cancelled') AND archived = FALSE AND project_id = %s "
                    "RETURNING *",
                    (_now(), project_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "UPDATE jobs SET archived = TRUE, updated_at = %s "
                    "WHERE status IN ('done','failed','cancelled') AND archived = FALSE "
                    "RETURNING *",
                    (_now(),),
                ).fetchall()
            return [Job.from_row(r) for r in rows]

    # --- usage tracking ------------------------------------------------
    def record_usage(self, source: str, usage: Usage, job_id: int | None = None) -> None:
        from .models import _now

        with self._connection() as conn:
            conn.execute(
                "INSERT INTO usage(job_id, source, model, provider, input_tokens, output_tokens, "
                "cache_creation_tokens, cache_read_tokens, cost_usd, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    job_id,
                    source,
                    usage.model,
                    usage.provider,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cache_creation_tokens,
                    usage.cache_read_tokens,
                    usage.cost_usd,
                    _now(),
                ),
            )

    # --- resource usage tracking ------------------------------------------
    def record_resource_usage(
        self, job_id: int, stage: str, attempt: int, r: ResourceUsage
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO resource_usage(job_id, stage, attempt, cpu_seconds, "
                "peak_rss_bytes, io_read_bytes, io_write_bytes, wall_seconds, "
                "net_bytes, disk_bytes, net_bytes_approx) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (
                    job_id,
                    stage,
                    attempt,
                    r.cpu_seconds,
                    r.peak_rss_bytes,
                    r.io_read_bytes,
                    r.io_write_bytes,
                    r.wall_seconds,
                    r.net_bytes,
                    r.disk_bytes,
                    r.net_bytes_approx,
                ),
            )

    def get_resource_usage(self, job_id: int) -> list[ResourceUsage]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM resource_usage WHERE job_id = %s ORDER BY id",
                (job_id,),
            ).fetchall()
        return [ResourceUsage.from_row(r) for r in rows]

    # --- page-view analytics ---------------------------------------------
    def record_page_view(
        self,
        *,
        path: str,
        ref: str,
        visitor_hash: str,
        country: str | None,
        region: str | None,
        city: str | None,
        referrer_host: str | None,
        ua_family: str | None,
        os_family: str | None,
        lang: str | None,
        tz: str | None,
        screen: str | None,
        os_version: str = "",
        arch: str = "",
        platform: str = "",
        langs: str = "",
        win: str = "",
        viewport: str = "",
        color_scheme: str = "",
        device_memory: str = "",
        touch: str = "",
        hour_cycle: str = "",
    ) -> PageView:
        with self._connection() as conn:
            row = conn.execute(
                "INSERT INTO page_views(path, ref, visitor_hash, country, region, city, "
                "referrer_host, ua_family, os_family, lang, tz, screen, os_version, arch, "
                "platform, langs, win, viewport, color_scheme, device_memory, touch, hour_cycle) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (
                    path,
                    ref,
                    visitor_hash,
                    country,
                    region,
                    city,
                    referrer_host,
                    ua_family,
                    os_family,
                    lang,
                    tz,
                    screen,
                    os_version,
                    arch,
                    platform,
                    langs,
                    win,
                    viewport,
                    color_scheme,
                    device_memory,
                    touch,
                    hour_cycle,
                ),
            ).fetchone()
        return PageView.from_row(row)

    def list_page_views(
        self,
        since: str | None = None,
        until: str | None = None,
        path: str | None = None,
    ) -> list[PageView]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM page_views "
                "WHERE (%s::timestamptz IS NULL OR ts >= %s::timestamptz) "
                "AND (%s::timestamptz IS NULL OR ts <= %s::timestamptz) "
                "AND (%s::text IS NULL OR path = %s::text) ORDER BY ts, id",
                (since, since, until, until, path, path),
            ).fetchall()
        return [PageView.from_row(row) for row in rows]

    def page_view_admin_summary(
        self,
        *,
        since: str,
        until: str,
        path: str | None = None,
    ) -> PageViewSummary:
        """Aggregate page_views into totals, breakdowns, and a recent sample.

        Every query is bounded by ``ts >= since AND ts <= until`` (using the
        ``ix_page_views_ts``/``ix_page_views_path_ts`` indexes) and, when
        ``path`` is given, by an exact path match. ``totals.unique_visitors``
        is the sum of each day's distinct visitor count rather than a global
        ``COUNT(DISTINCT visitor_hash)``, since the visitor hash rotates daily.
        Because that salt rotates daily, ``returning`` undercounts returning
        people and is a floor rather than a count of all returning visitors.
        The ``recent`` sample never selects ``visitor_hash``.
        """
        where = "ts >= %s AND ts <= %s"
        params: tuple = (since, until)
        if path is not None:
            where += " AND path = %s"
            params = (*params, path)

        def _iso(value):
            return value.isoformat() if hasattr(value, "isoformat") else value

        with self._connection() as conn:
            day_rows = conn.execute(
                "SELECT date_trunc('day', ts) AS day, COUNT(*) AS views, "
                "COUNT(DISTINCT visitor_hash) AS unique_visitors "
                f"FROM page_views WHERE {where} GROUP BY day ORDER BY day",  # nosec B608
                params,
            ).fetchall()
            by_day = [
                {
                    "day": _iso(row["day"]),
                    "views": int(row["views"]),
                    "unique_visitors": int(row["unique_visitors"]),
                }
                for row in day_rows
            ]

            totals_row = conn.execute(
                "SELECT COUNT(*) AS views, MIN(ts) AS first_seen, MAX(ts) AS last_seen "
                f"FROM page_views WHERE {where}",  # nosec B608
                params,
            ).fetchone()
            totals = {
                "views": int(totals_row["views"]),
                "unique_visitors": sum(row["unique_visitors"] for row in by_day),
                "days_with_traffic": len(by_day),
                "first_seen": _iso(totals_row["first_seen"]),
                "last_seen": _iso(totals_row["last_seen"]),
            }

            path_rows = conn.execute(
                "SELECT path, COUNT(*) AS views, COUNT(DISTINCT visitor_hash) AS unique_visitors "
                f"FROM page_views WHERE {where} GROUP BY path ORDER BY views DESC, path",  # nosec B608
                params,
            ).fetchall()
            by_path = [
                {
                    "path": row["path"],
                    "views": int(row["views"]),
                    "unique_visitors": int(row["unique_visitors"]),
                }
                for row in path_rows
            ]

            viewport_rows = conn.execute(
                "SELECT COALESCE(viewport, '') AS viewport, COUNT(*) AS views, "
                "COUNT(DISTINCT visitor_hash) AS unique_visitors "
                f"FROM page_views WHERE {where} GROUP BY 1 ORDER BY views DESC, viewport",  # nosec B608
                params,
            ).fetchall()
            by_viewport = [
                {
                    "viewport": row["viewport"],
                    "views": int(row["views"]),
                    "unique_visitors": int(row["unique_visitors"]),
                }
                for row in viewport_rows
            ]

            color_rows = conn.execute(
                "SELECT COALESCE(NULLIF(color_scheme, ''), 'unknown') AS color_scheme, COUNT(*) AS views "
                f"FROM page_views WHERE {where} GROUP BY 1 ORDER BY views DESC, color_scheme",  # nosec B608
                params,
            ).fetchall()
            by_color_scheme = [
                {"color_scheme": row["color_scheme"], "views": int(row["views"])}
                for row in color_rows
            ]

            lang_rows = conn.execute(
                "SELECT BTRIM(language) AS lang, "
                "COUNT(*) AS views, COUNT(DISTINCT visitor_hash) AS unique_visitors "
                "FROM page_views CROSS JOIN LATERAL regexp_split_to_table("
                "COALESCE(NULLIF(langs, ''), NULLIF(lang, ''), 'unknown'), ',') AS language "
                f"WHERE {where} GROUP BY 1 ORDER BY views DESC, lang LIMIT 15",  # nosec B608
                params,
            ).fetchall()
            by_lang = [
                {
                    "lang": row["lang"],
                    "views": int(row["views"]),
                    "unique_visitors": int(row["unique_visitors"]),
                }
                for row in lang_rows
            ]

            hour_rows = conn.execute(
                "SELECT EXTRACT(HOUR FROM ts)::int AS hour, COUNT(*) AS views "
                f"FROM page_views WHERE {where} GROUP BY 1",  # nosec B608
                params,
            ).fetchall()
            hour_counts = {int(row["hour"]): int(row["views"]) for row in hour_rows}
            by_hour = [{"hour": hour, "views": hour_counts.get(hour, 0)} for hour in range(24)]

            os_version_rows = conn.execute(
                "SELECT COALESCE(NULLIF(os_family, ''), 'unknown') AS os_family, "
                "COALESCE(NULLIF(os_version, ''), 'unknown') AS os_version, COUNT(*) AS views "
                f"FROM page_views WHERE {where} GROUP BY 1, 2 "  # nosec B608
                "ORDER BY views DESC, os_family, os_version LIMIT 15",
                params,
            ).fetchall()
            by_os_version = [
                {
                    "os_family": row["os_family"],
                    "os_version": row["os_version"],
                    "views": int(row["views"]),
                }
                for row in os_version_rows
            ]

            depth_rows = conn.execute(
                "WITH visitor_depth AS (SELECT date_trunc('day', ts) AS day, visitor_hash, "
                "COUNT(DISTINCT path) AS pages FROM page_views WHERE "
                f"{where} GROUP BY 1, 2) "  # nosec B608
                "SELECT LEAST(pages, 5)::int AS pages, COUNT(*) AS visitors, "
                "SUM(pages) AS total_pages FROM visitor_depth "
                "GROUP BY 1 ORDER BY 1",
                params,
            ).fetchall()
            depth_counts = {int(row["pages"]): int(row["visitors"]) for row in depth_rows}
            pages_per_visitor = [
                {"pages": "5+" if pages == 5 else pages, "visitors": depth_counts.get(pages, 0)}
                for pages in range(1, 6)
            ]
            daily_visitor_count = sum(depth_counts.values())
            total_visitor_pages = sum(int(row["total_pages"]) for row in depth_rows)
            totals["paths_seen"] = len(by_path)
            totals["avg_pages_per_visitor"] = (
                round(total_visitor_pages / daily_visitor_count, 2) if daily_visitor_count else 0.0
            )

            returning_row = conn.execute(
                "WITH visitor_days AS (SELECT visitor_hash, COUNT(DISTINCT date_trunc('day', ts)) AS days "
                f"FROM page_views WHERE {where} GROUP BY visitor_hash) "  # nosec B608
                "SELECT COUNT(*) FILTER (WHERE days = 1) AS single_day, "
                "COUNT(*) FILTER (WHERE days > 1) AS multi_day FROM visitor_days",
                params,
            ).fetchone()
            returning = {
                "single_day": int(returning_row["single_day"]),
                "multi_day": int(returning_row["multi_day"]),
            }

            country_rows = conn.execute(
                "SELECT COALESCE(country, 'Unknown') AS country, COUNT(*) AS views, "
                "COUNT(DISTINCT visitor_hash) AS unique_visitors "
                f"FROM page_views WHERE {where} GROUP BY country "  # nosec B608
                "ORDER BY views DESC LIMIT 25",
                params,
            ).fetchall()
            by_country = [
                {
                    "country": row["country"],
                    "views": int(row["views"]),
                    "unique_visitors": int(row["unique_visitors"]),
                }
                for row in country_rows
            ]

            city_rows = conn.execute(
                "SELECT COALESCE(country, 'Unknown') AS country, city, COUNT(*) AS views "
                f"FROM page_views WHERE {where} AND city IS NOT NULL "  # nosec B608
                "GROUP BY country, city ORDER BY views DESC LIMIT 25",
                params,
            ).fetchall()
            by_city = [
                {"country": row["country"], "city": row["city"], "views": int(row["views"])}
                for row in city_rows
            ]

            ref_rows = conn.execute(
                "SELECT COALESCE(NULLIF(ref, ''), '(no token)') AS ref, COUNT(*) AS views, "
                "COUNT(DISTINCT visitor_hash) AS unique_visitors "
                f"FROM page_views WHERE {where} GROUP BY ref ORDER BY views DESC",  # nosec B608
                params,
            ).fetchall()
            by_ref = [
                {
                    "ref": row["ref"],
                    "views": int(row["views"]),
                    "unique_visitors": int(row["unique_visitors"]),
                }
                for row in ref_rows
            ]

            referrer_rows = conn.execute(
                "SELECT referrer_host, COUNT(*) AS views FROM page_views "
                f"WHERE {where} GROUP BY referrer_host ORDER BY views DESC LIMIT 25",  # nosec B608
                params,
            ).fetchall()
            by_referrer = [
                {"referrer_host": row["referrer_host"], "views": int(row["views"])}
                for row in referrer_rows
            ]

            device_rows = conn.execute(
                "SELECT os_family, ua_family, COUNT(*) AS views, "
                "COUNT(DISTINCT visitor_hash) AS unique_visitors "
                f"FROM page_views WHERE {where} GROUP BY os_family, ua_family "  # nosec B608
                "ORDER BY views DESC LIMIT 25",
                params,
            ).fetchall()
            by_device = [
                {
                    "os_family": row["os_family"],
                    "ua_family": row["ua_family"],
                    "views": int(row["views"]),
                    "unique_visitors": int(row["unique_visitors"]),
                }
                for row in device_rows
            ]

            recent_rows = conn.execute(
                "SELECT ts, path, ref, country, city, os_family, ua_family, lang, tz, screen "
                f"FROM page_views WHERE {where} ORDER BY ts DESC LIMIT 100",  # nosec B608
                params,
            ).fetchall()
            recent = [
                {
                    "ts": _iso(row["ts"]),
                    "path": row["path"],
                    "ref": row["ref"],
                    "country": row["country"],
                    "city": row["city"],
                    "os_family": row["os_family"],
                    "ua_family": row["ua_family"],
                    "lang": row["lang"],
                    "tz": row["tz"],
                    "screen": row["screen"],
                }
                for row in recent_rows
            ]

        return PageViewSummary(
            totals=totals,
            by_day=by_day,
            by_country=by_country,
            by_city=by_city,
            by_ref=by_ref,
            by_referrer=by_referrer,
            by_device=by_device,
            recent=recent,
            by_path=by_path,
            by_viewport=by_viewport,
            by_color_scheme=by_color_scheme,
            by_lang=by_lang,
            by_hour=by_hour,
            by_os_version=by_os_version,
            pages_per_visitor=pages_per_visitor,
            returning=returning,
        )

    def _operational_exclusion_clause(self, alias: str = "j") -> str:
        """SQL fragment matching jobs that are auto-deploy operational churn.

        Matches either the current ``source_actor = 'auto-deploy'`` tag or the
        legacy ``source = 'supervisor'`` + ``title LIKE 'Auto-deploy:%'`` shape
        used before that tag existed. ``alias`` is always an internal fixed
        literal (the joined jobs table alias), never user input.
        """
        return (
            f"({alias}.source_actor = 'auto-deploy' OR "
            f"({alias}.source = 'supervisor' AND {alias}.title LIKE 'Auto-deploy:%'))"
        )

    # Stage -> gate-stop bucket used by ``site_stats``. Every stage here is a
    # deterministic gate that can halt a job; stages not listed here never
    # count toward a gate bucket.
    _GATE_STAGE_BUCKETS: dict[str, str] = {
        "lint": "lint",
        "lockfile-drift": "lint",
        "test": "test",
        "import-smoke": "test",
        "frontend-build": "test",
        "invariants": "test",
        "symbol-collision": "test",
        "alembic-heads": "test",
        "review": "review",
        "security": "security",
        "design_review": "design",
    }

    def site_stats(self) -> SiteStats:
        """Global, cross-project operational telemetry for the platform.

        Every job-, event-, and usage-derived figure excludes archived jobs
        and auto-deploy operational jobs (see ``_operational_exclusion_clause``),
        matching the analytics convention already applied by
        ``performance_headline_stats``/``usage_summary``. ``humans``,
        active ``projects``, and ``workers_online`` are not job-derived and
        are therefore unfiltered.
        """
        from datetime import datetime, timedelta, timezone

        op_clause = self._operational_exclusion_clause("j").replace("%", "%%")
        now = datetime.now(timezone.utc)
        cutoff_7d = (now - timedelta(days=7)).isoformat()
        cutoff_24h = (now - timedelta(hours=24)).isoformat()
        worker_cutoff = time.time() - 300

        with self._connection() as conn:
            ship_row = conn.execute(
                "SELECT "
                "  COUNT(*) AS shipped_total, "
                "  COUNT(*) FILTER (WHERE j.updated_at >= %s) AS shipped_last_7d, "
                "  COUNT(*) FILTER (WHERE j.updated_at >= %s) AS shipped_last_24h, "
                "  MAX(j.updated_at::timestamptz) AS last_ship_at, "
                "  COUNT(*) FILTER (WHERE j.attempts > 0) AS self_healed_shipped, "
                "  PERCENTILE_CONT(0.5) WITHIN GROUP ("
                "    ORDER BY EXTRACT(EPOCH FROM (j.updated_at::timestamptz - j.created_at::timestamptz)) / 60"
                "  ) AS median_lead_minutes, "
                "  PERCENTILE_CONT(0.25) WITHIN GROUP ("
                "    ORDER BY EXTRACT(EPOCH FROM (j.updated_at::timestamptz - j.created_at::timestamptz)) / 60"
                "  ) AS p25_lead_minutes, "
                "  COUNT(*) FILTER ("
                "    WHERE EXTRACT(ISODOW FROM (j.updated_at::timestamptz AT TIME ZONE 'UTC')) IN (6, 7) "
                "       OR EXTRACT(HOUR FROM (j.updated_at::timestamptz AT TIME ZONE 'UTC')) "
                "           NOT BETWEEN 8 AND 19"
                "  ) AS off_hours_count "
                "FROM jobs j "
                f"WHERE j.status = %s AND NOT j.archived AND NOT {op_clause}",  # nosec B608
                (cutoff_7d, cutoff_24h, JobStatus.DONE.value),
            ).fetchone()

            first_job_row = conn.execute(
                "SELECT MIN(j.created_at::timestamptz)::date AS first_job_at "
                "FROM jobs j "
                f"WHERE NOT j.archived AND NOT {op_clause}"  # nosec B608
            ).fetchone()

            humans_row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
            projects_row = conn.execute(
                "SELECT COUNT(*) AS n FROM projects WHERE status = 'active'"
            ).fetchone()

            gate_rows = conn.execute(
                "SELECT je.stage, COUNT(*) AS n "
                "FROM job_events je "
                "JOIN jobs j ON j.id = je.job_id "
                "WHERE je.status = 'failed' AND je.stage = ANY(%s) "
                f"  AND NOT j.archived AND NOT {op_clause} "  # nosec B608
                "GROUP BY je.stage",
                (list(self._GATE_STAGE_BUCKETS),),
            ).fetchall()

            events_row = conn.execute(
                "SELECT "
                "  COUNT(*) FILTER (WHERE je.stage = 'fix' AND je.status = 'done') AS fix_rounds, "
                "  COUNT(*) FILTER (WHERE je.stage = 'merge' AND je.status = 'conflict') "
                "    AS conflicts_resolved "
                "FROM job_events je "
                "JOIN jobs j ON j.id = je.job_id "
                f"WHERE NOT j.archived AND NOT {op_clause}"  # nosec B608
            ).fetchone()

            cost_row = conn.execute(
                "WITH job_cost AS ("
                "  SELECT j.id, SUM(u.cost_usd) AS total_cost "
                "  FROM jobs j "
                "  JOIN usage u ON u.job_id = j.id "
                f"  WHERE j.status = %s AND NOT j.archived AND NOT {op_clause} "  # nosec B608
                "  GROUP BY j.id "
                ") "
                "SELECT SUM(total_cost) AS sum_cost, "
                "  PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY total_cost) AS median_cost "
                "FROM job_cost",
                (JobStatus.DONE.value,),
            ).fetchone()

            running_row = conn.execute(
                "SELECT COUNT(*) AS n FROM jobs j "
                f"WHERE j.status = %s AND NOT j.archived AND NOT {op_clause}",  # nosec B608
                (JobStatus.RUNNING.value,),
            ).fetchone()

            workers_row = conn.execute(
                "SELECT COUNT(*) AS n FROM workers WHERE last_seen >= %s",
                (worker_cutoff,),
            ).fetchone()

        shipped_total = int(ship_row["shipped_total"] or 0)
        shipped_last_7d = int(ship_row["shipped_last_7d"] or 0)

        gate_stops_by_gate = dict.fromkeys(("lint", "test", "review", "security", "design"), 0)
        for row in gate_rows:
            bucket = self._GATE_STAGE_BUCKETS.get(row["stage"])
            if bucket is not None:
                gate_stops_by_gate[bucket] += int(row["n"])

        last_ship_at = ship_row["last_ship_at"]
        minutes_since_last_ship = (
            round((now - last_ship_at).total_seconds() / 60, 2)
            if last_ship_at is not None
            else None
        )

        first_job_at_date = first_job_row["first_job_at"] if first_job_row else None
        first_job_at = first_job_at_date.isoformat() if first_job_at_date is not None else None
        active_days = (
            (now.date() - first_job_at_date).days if first_job_at_date is not None else None
        )

        sum_cost = cost_row["sum_cost"] if cost_row else None
        cost_per_shipped_usd = (
            round(float(sum_cost or 0.0) / shipped_total, 2) if shipped_total > 0 else None
        )
        median_cost = cost_row["median_cost"] if cost_row else None
        median_cost_shipped_usd = round(float(median_cost), 2) if median_cost is not None else None

        off_hours_count = int(ship_row["off_hours_count"] or 0)
        off_hours_share = round(off_hours_count / shipped_total, 4) if shipped_total > 0 else None

        median_lead_minutes = ship_row["median_lead_minutes"]
        p25_lead_minutes = ship_row["p25_lead_minutes"]

        return SiteStats(
            generated_at=now.isoformat(),
            shipped_total=shipped_total,
            shipped_last_7d=shipped_last_7d,
            shipped_last_24h=int(ship_row["shipped_last_24h"] or 0),
            minutes_since_last_ship=minutes_since_last_ship,
            avg_minutes_between_ships_last_7d=(
                round(10080.0 / shipped_last_7d, 2) if shipped_last_7d > 0 else None
            ),
            first_job_at=first_job_at,
            active_days=active_days,
            humans=int(humans_row["n"]),
            projects=int(projects_row["n"]),
            gate_stops_total=sum(gate_stops_by_gate.values()),
            gate_stops_by_gate=gate_stops_by_gate,
            self_healed_shipped=int(ship_row["self_healed_shipped"] or 0),
            fix_rounds=int(events_row["fix_rounds"] or 0),
            conflicts_resolved=int(events_row["conflicts_resolved"] or 0),
            median_lead_minutes=(
                round(float(median_lead_minutes), 2) if median_lead_minutes is not None else None
            ),
            p25_lead_minutes=(
                round(float(p25_lead_minutes), 2) if p25_lead_minutes is not None else None
            ),
            off_hours_share=off_hours_share,
            cost_per_shipped_usd=cost_per_shipped_usd,
            median_cost_shipped_usd=median_cost_shipped_usd,
            running_now=int(running_row["n"]),
            workers_online=int(workers_row["n"]),
        )

    def usage_summary(
        self,
        since: str | None = None,
        project_id: int | None = None,
        include_operational: bool = False,
    ) -> dict:
        """Aggregate token + cost totals, broken down by source.

        When ``project_id`` is given, only usage rows whose job belongs to that
        project are included (dev-session usage with ``job_id IS NULL`` is
        excluded). Omitting it preserves the unscoped, global rollup.

        By default (``include_operational=False``), usage attributed to
        auto-deploy operational jobs (see ``_operational_exclusion_clause``) is
        excluded from ``by_source``, ``total_tokens``, ``total_cost_usd``, and
        the ``projects`` rollup, while dev-session usage (``job_id IS NULL``)
        is always included. Pass ``include_operational=True`` to restore
        today's unfiltered totals.
        """
        # Doubled '%' because this fragment is spliced into raw SQL text executed
        # via conn.execute(), whose placeholder parser treats a bare '%' as an
        # (invalid) parameter marker even outside any quoted string literal.
        op_clause = self._operational_exclusion_clause("j").replace("%", "%%")
        with self._connection() as conn:
            if project_id is not None:
                where = "WHERE j.project_id = %s"
                params: tuple = (project_id,)
                if since is not None:
                    where += " AND u.created_at >= %s"
                    params += (since,)
                if not include_operational:
                    where += f" AND NOT {op_clause}"
                rows = conn.execute(
                    "SELECT u.source AS source, "
                    "SUM(u.input_tokens) i, SUM(u.output_tokens) o, "
                    "SUM(u.cache_creation_tokens) cc, SUM(u.cache_read_tokens) cr, "
                    "SUM(u.cost_usd) cost, COUNT(*) n "
                    "FROM usage u JOIN jobs j ON j.id = u.job_id "
                    f"{where} GROUP BY u.source ORDER BY cost DESC",
                    params,
                ).fetchall()
                total = conn.execute(
                    "SELECT SUM(u.input_tokens + u.output_tokens + u.cache_creation_tokens + "
                    "u.cache_read_tokens) tokens, SUM(u.cost_usd) cost "
                    "FROM usage u JOIN jobs j ON j.id = u.job_id "
                    f"{where}",
                    params,
                ).fetchone()
            elif since is not None:
                op_guard = (
                    f" AND (j.id IS NULL OR NOT {op_clause})" if not include_operational else ""
                )
                # op_guard is built from op_clause (a fixed literal from
                # _operational_exclusion_clause, alias is always 'j') plus a
                # constant WHERE/AND keyword — never user input; real values
                # are bound via params below, so this is not injectable.
                rows = conn.execute(
                    "SELECT u.source AS source, "  # nosec B608
                    "SUM(u.input_tokens) i, SUM(u.output_tokens) o, "
                    "SUM(u.cache_creation_tokens) cc, SUM(u.cache_read_tokens) cr, "
                    "SUM(u.cost_usd) cost, COUNT(*) n "
                    "FROM usage u LEFT JOIN jobs j ON j.id = u.job_id "
                    f"WHERE u.created_at >= %s{op_guard} "
                    "GROUP BY u.source ORDER BY cost DESC",
                    (since,),
                ).fetchall()
                total = conn.execute(
                    "SELECT SUM(u.input_tokens + u.output_tokens + u.cache_creation_tokens + "  # nosec B608
                    "u.cache_read_tokens) tokens, SUM(u.cost_usd) cost "
                    "FROM usage u LEFT JOIN jobs j ON j.id = u.job_id "
                    f"WHERE u.created_at >= %s{op_guard}",
                    (since,),
                ).fetchone()
            else:
                op_guard = (
                    f" WHERE (j.id IS NULL OR NOT {op_clause})" if not include_operational else ""
                )
                # op_guard is built from op_clause (a fixed literal from
                # _operational_exclusion_clause, alias is always 'j') plus a
                # constant WHERE keyword — never user input; this query takes
                # no params at all, so there is nothing to inject.
                rows = conn.execute(
                    "SELECT u.source AS source, "  # nosec B608
                    "SUM(u.input_tokens) i, SUM(u.output_tokens) o, "
                    "SUM(u.cache_creation_tokens) cc, SUM(u.cache_read_tokens) cr, "
                    "SUM(u.cost_usd) cost, COUNT(*) n "
                    "FROM usage u LEFT JOIN jobs j ON j.id = u.job_id "
                    f"{op_guard} GROUP BY u.source ORDER BY cost DESC"
                ).fetchall()
                total = conn.execute(
                    "SELECT SUM(u.input_tokens + u.output_tokens + u.cache_creation_tokens + "  # nosec B608
                    "u.cache_read_tokens) tokens, SUM(u.cost_usd) cost "
                    "FROM usage u LEFT JOIN jobs j ON j.id = u.job_id "
                    f"{op_guard}"
                ).fetchone()

            if include_operational:
                operational_jobs_excluded = 0
            else:
                excl_clauses = [op_clause]
                excl_params: tuple = ()
                if project_id is not None:
                    excl_clauses.append("j.project_id = %s")
                    excl_params += (project_id,)
                if since is not None:
                    excl_clauses.append("u.created_at >= %s")
                    excl_params += (since,)
                # excl_clauses are constant literals (op_clause plus "col = %s"
                # fragments); every user value is passed separately as a bound
                # param below, so this string assembly is not injectable.
                excluded = conn.execute(
                    "SELECT COUNT(DISTINCT u.job_id) n FROM usage u "  # nosec B608
                    "JOIN jobs j ON j.id = u.job_id "
                    f"WHERE {' AND '.join(excl_clauses)}",
                    excl_params,
                ).fetchone()
                operational_jobs_excluded = (excluded["n"] or 0) if excluded else 0

            projects = self._usage_by_project(
                conn, since=since, project_id=project_id, include_operational=include_operational
            )
        by_source = [
            {
                "source": r["source"],
                "input": r["i"] or 0,
                "output": r["o"] or 0,
                "cache_creation": r["cc"] or 0,
                "cache_read": r["cr"] or 0,
                "tokens": (r["i"] or 0) + (r["o"] or 0) + (r["cc"] or 0) + (r["cr"] or 0),
                "cost_usd": round(r["cost"] or 0.0, 4),
                "runs": r["n"],
            }
            for r in rows
        ]
        return {
            "by_source": by_source,
            "projects": projects,
            "total_tokens": (total["tokens"] or 0) if total else 0,
            "total_cost_usd": round((total["cost"] or 0.0), 4) if total else 0.0,
            "excludes_operational": not include_operational,
            "operational_jobs_excluded": operational_jobs_excluded,
        }

    def jobs_filed_by_breakdown(self, since: str | None = None) -> list[dict]:
        """Job counts grouped by (source, source_actor), across all projects.

        A global, unscoped rollup for the platform-wide admin page — unlike
        ``usage_summary``, this never takes a ``project_id`` filter.
        """
        with self._connection() as conn:
            if since is not None:
                rows = conn.execute(
                    "SELECT source, source_actor, COUNT(*) n FROM jobs "
                    "WHERE created_at >= %s GROUP BY source, source_actor "
                    "ORDER BY n DESC, source, source_actor",
                    (since,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT source, source_actor, COUNT(*) n FROM jobs "
                    "GROUP BY source, source_actor ORDER BY n DESC, source, source_actor"
                ).fetchall()
        return [
            {"source": r["source"], "source_actor": r["source_actor"], "count": r["n"]}
            for r in rows
        ]

    def get_job_usage(self, job_id: int) -> list[dict]:
        """Per (source, model, provider) token + cost totals for one job."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT source, model, provider, "
                "SUM(input_tokens) i, SUM(output_tokens) o, "
                "SUM(cache_creation_tokens) cc, SUM(cache_read_tokens) cr, "
                "SUM(cost_usd) cost, COUNT(*) n "
                "FROM usage WHERE job_id = %s GROUP BY source, model, provider "
                "ORDER BY source",
                (job_id,),
            ).fetchall()
        return [
            {
                "source": r["source"],
                "model": r["model"],
                "provider": r["provider"],
                "input_tokens": r["i"] or 0,
                "output_tokens": r["o"] or 0,
                "cache_creation_tokens": r["cc"] or 0,
                "cache_read_tokens": r["cr"] or 0,
                "cost_usd": round(r["cost"] or 0.0, 4),
                "runs": r["n"],
            }
            for r in rows
        ]

    def _usage_by_project(
        self,
        conn: psycopg.Connection,
        since: str | None = None,
        project_id: int | None = None,
        include_operational: bool = False,
    ) -> list[dict]:
        """Token + cost rolled up Project → Epic → Job.

        Usage rows carry a ``job_id``; jobs carry ``project_id``/``epic_id``, so a
        single join attributes every metered run up the hierarchy. Jobs with no
        epic fall under an "Unassigned" bucket. Usage with no job (interactive
        dev-session usage from the Stop-hook collector) rolls up directly onto a
        pseudo-project (``project_id`` ``None``) with no epics — there is no
        per-job breakdown for that usage. When ``project_id`` is given, the result
        contains at most one project entry (that project's own rollup); dev-session
        usage (no job) is excluded since it can never match a project.

        By default (``include_operational=False``), auto-deploy operational jobs
        (see ``_operational_exclusion_clause``) are excluded from every project's
        rollup, without ever excluding the ``project_id`` ``None`` dev-session
        bucket. Pass ``include_operational=True`` to include them.
        """
        clauses = []
        params: tuple = ()
        if project_id is not None:
            clauses.append("j.project_id = %s")
            params += (project_id,)
        if since is not None:
            clauses.append("u.created_at >= %s")
            params += (since,)
        if not include_operational:
            op_clause = self._operational_exclusion_clause("j").replace("%", "%%")
            clauses.append(f"(j.id IS NULL OR NOT {op_clause})")
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        rows = conn.execute(
            "SELECT u.job_id, j.project_id, j.epic_id, j.idea, j.title, "
            "p.name AS project_name, e.name AS epic_name, "
            "SUM(u.input_tokens + u.output_tokens + u.cache_creation_tokens + "
            "u.cache_read_tokens) AS tokens, SUM(u.cost_usd) AS cost, COUNT(*) AS runs "
            "FROM usage u "
            "LEFT JOIN jobs j ON j.id = u.job_id "
            "LEFT JOIN projects p ON p.id = j.project_id "
            "LEFT JOIN epics e ON e.id = j.epic_id "
            f"{where}"
            "GROUP BY u.job_id, j.project_id, j.epic_id, j.idea, j.title, p.name, e.name "
            "ORDER BY cost DESC",
            params,
        ).fetchall()

        projects: dict = {}
        for r in rows:
            pid = r["project_id"]
            eid = r["epic_id"]
            tokens, cost, runs = r["tokens"] or 0, r["cost"] or 0.0, r["runs"]
            proj = projects.setdefault(
                pid,
                {
                    "project_id": pid,
                    "name": r["project_name"]
                    or (
                        "Interactive dev sessions (Claude Code)"
                        if pid is None
                        else f"project #{pid}"
                    ),
                    "tokens": 0,
                    "cost_usd": 0.0,
                    "runs": 0,
                    "_epics": {},
                },
            )
            proj["tokens"] += tokens
            proj["cost_usd"] += cost
            proj["runs"] += runs
            if pid is None:
                # Dev-session usage has no job, so no epic/job breakdown is possible.
                continue
            epic = proj["_epics"].setdefault(
                eid,
                {
                    "epic_id": eid,
                    "name": r["epic_name"] or "Unassigned",
                    "tokens": 0,
                    "cost_usd": 0.0,
                    "runs": 0,
                    "jobs": [],
                },
            )
            epic["tokens"] += tokens
            epic["cost_usd"] += cost
            epic["runs"] += runs
            if r["job_id"] is not None:
                epic["jobs"].append(
                    {
                        "job_id": r["job_id"],
                        "idea": r["idea"] or "",
                        "title": r["title"] or "" if "title" in r.keys() else "",
                        "tokens": tokens,
                        "cost_usd": round(cost, 4),
                        "runs": runs,
                    }
                )

        out = []
        for proj in sorted(projects.values(), key=lambda p: p["cost_usd"], reverse=True):
            epics = sorted(proj.pop("_epics").values(), key=lambda e: e["cost_usd"], reverse=True)
            for epic in epics:
                epic["cost_usd"] = round(epic["cost_usd"], 4)
                epic["jobs"].sort(key=lambda j: j["cost_usd"], reverse=True)
            proj["cost_usd"] = round(proj["cost_usd"], 4)
            proj["epics"] = epics
            out.append(proj)
        return out

    # --- per-step activity timeline -----------------------------------
    def add_event(
        self,
        job_id: int,
        stage: str,
        status: str,
        *,
        summary: str = "",
        detail: dict | None = None,
        tokens: int = 0,
        cost_usd: float = 0.0,
        started_at: str = "",
        ended_at: str = "",
        attempt: int = 0,
        agent_id: int | None = None,
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO job_events(job_id, stage, status, attempt, summary, detail, "
                "tokens, cost_usd, started_at, ended_at, agent_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    job_id,
                    stage,
                    status,
                    attempt,
                    summary,
                    json.dumps(detail or {}, default=_json_safe_default),
                    tokens,
                    cost_usd,
                    started_at,
                    ended_at,
                    agent_id,
                ),
            )

    def agent_stats(self, project_id: int, include_operational: bool = False) -> list[dict]:
        """Cost, avg stage duration, and fix rate per roster agent for a project.

        Left-joins from project_agents so every agent appears even with no events;
        those rows return zeros / null for duration.

        By default (``include_operational=False``), job_events belonging to
        auto-deploy operational jobs (see ``_operational_exclusion_clause``) are
        excluded from the join, so they contribute nothing to
        total_cost_usd/avg_duration_s/fix_rate. Because the exclusion is applied
        on the join's ON clause rather than a WHERE filter, an agent whose only
        events are operational still appears in the roster with the same
        zero-safe defaults (0 cost, null duration, 0.0 fix_rate) as an agent
        with genuinely zero events. Pass ``include_operational=True`` to restore
        today's unfiltered join.
        """
        # Doubled '%' because this fragment is spliced into raw SQL text executed
        # via conn.execute(), whose placeholder parser treats a bare '%' as an
        # (invalid) parameter marker even outside any quoted string literal.
        op_clause = self._operational_exclusion_clause("jx").replace("%", "%%")
        # join_guard is built from op_clause (a fixed literal from
        # _operational_exclusion_clause, alias is always 'jx') plus constant
        # SQL keywords — never user input; the only bound parameter is
        # project_id below, so this is not injectable.
        join_guard = (
            f" AND NOT EXISTS (SELECT 1 FROM jobs jx WHERE jx.id = je.job_id AND {op_clause})"  # nosec B608
            if not include_operational
            else ""
        )
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT pa.id AS agent_id, "  # nosec B608
                "COALESCE(SUM(je.cost_usd), 0) AS total_cost_usd, "
                "AVG(CASE WHEN je.started_at IS NOT NULL AND je.started_at != '' "
                "         AND je.ended_at IS NOT NULL AND je.ended_at != '' "
                "    THEN EXTRACT(EPOCH FROM (je.ended_at::timestamptz - je.started_at::timestamptz)) END) AS avg_duration_s, "
                "CASE WHEN COUNT(DISTINCT je.job_id) = 0 THEN 0.0 "
                "     ELSE COUNT(DISTINCT CASE WHEN je.stage = 'fix' THEN je.job_id END)::float "
                "          / COUNT(DISTINCT je.job_id) END AS fix_rate "
                "FROM project_agents pa "
                f"LEFT JOIN job_events je ON je.agent_id = pa.id{join_guard} "
                "WHERE pa.project_id = %s "
                "GROUP BY pa.id",
                (project_id,),
            ).fetchall()
        return [
            {
                "agent_id": r["agent_id"],
                "total_cost_usd": round(float(r["total_cost_usd"] or 0), 6),
                "avg_duration_s": float(r["avg_duration_s"])
                if r["avg_duration_s"] is not None
                else None,
                "fix_rate": round(float(r["fix_rate"] or 0), 4),
            }
            for r in rows
        ]

    def _perf_filters(
        self,
        from_date: str | None,
        to_date: str | None,
        stage: str | None,
        epic_id: int | None,
        status: str | None,
        provider: str | None = None,
        include_operational: bool = False,
    ) -> tuple[list[str], list]:
        frags: list[str] = []
        params: list = []
        if from_date is not None:
            frags.append("je.started_at::timestamptz >= %s::timestamptz")
            params.append(from_date)
        if to_date is not None:
            frags.append("je.started_at::timestamptz <= %s::timestamptz")
            params.append(to_date)
        if stage is not None:
            frags.append("je.stage = %s")
            params.append(stage)
        if epic_id is not None:
            frags.append("j.epic_id = %s")
            params.append(epic_id)
        if status is not None:
            frags.append("j.status = %s")
            params.append(status)
        if provider is not None:
            frags.append("j.provider = %s")
            params.append(provider)
        if not include_operational:
            # Doubled '%' because this fragment is spliced into raw SQL text
            # executed via conn.execute(), whose placeholder parser treats a
            # bare '%' as an (invalid) parameter marker even outside any
            # quoted string literal.
            frags.append(f"NOT {self._operational_exclusion_clause('j').replace('%', '%%')}")
        return frags, params

    def performance_stage_stats(
        self,
        project_id: int,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
        stage: str | None = None,
        epic_id: int | None = None,
        status: str | None = None,
        provider: str | None = None,
        include_operational: bool = False,
    ) -> list[dict]:
        """Per-stage run count and duration percentiles for a project.

        By default (``include_operational=False``), job_events belonging to
        auto-deploy operational jobs (see ``_operational_exclusion_clause``)
        are excluded from every stat. Pass ``include_operational=True`` to
        restore today's unfiltered figures.
        """
        extra_frags, extra_params = self._perf_filters(
            from_date, to_date, stage, epic_id, status, provider, include_operational
        )
        extra_sql = "".join(f"  AND {f} " for f in extra_frags)
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT je.stage, "
                "COUNT(*) AS run_count, "
                "AVG(EXTRACT(EPOCH FROM (je.ended_at::timestamptz - je.started_at::timestamptz))) AS avg_duration_s, "
                "PERCENTILE_CONT(0.5) WITHIN GROUP ("
                "  ORDER BY EXTRACT(EPOCH FROM (je.ended_at::timestamptz - je.started_at::timestamptz))"
                ") AS p50_duration_s, "
                "PERCENTILE_CONT(0.95) WITHIN GROUP ("
                "  ORDER BY EXTRACT(EPOCH FROM (je.ended_at::timestamptz - je.started_at::timestamptz))"
                ") AS p95_duration_s "
                "FROM job_events je "
                "JOIN jobs j ON j.id = je.job_id "
                "WHERE j.project_id = %s "
                "  AND je.started_at IS NOT NULL AND je.started_at != '' "
                "  AND je.ended_at IS NOT NULL AND je.ended_at != '' "
                + extra_sql
                + "GROUP BY je.stage "
                "ORDER BY avg_duration_s DESC NULLS LAST",
                (project_id, *extra_params),
            ).fetchall()
        return [
            {
                "stage": r["stage"],
                "run_count": int(r["run_count"]),
                "avg_duration_s": round(float(r["avg_duration_s"]), 2)
                if r["avg_duration_s"] is not None
                else None,
                "p50_duration_s": round(float(r["p50_duration_s"]), 2)
                if r["p50_duration_s"] is not None
                else None,
                "p95_duration_s": round(float(r["p95_duration_s"]), 2)
                if r["p95_duration_s"] is not None
                else None,
            }
            for r in rows
        ]

    def performance_slowest_jobs(
        self,
        project_id: int,
        limit: int = 20,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
        stage: str | None = None,
        epic_id: int | None = None,
        status: str | None = None,
        provider: str | None = None,
        include_operational: bool = False,
    ) -> list[dict]:
        """The slowest jobs by total stage duration for a project.

        By default (``include_operational=False``), auto-deploy operational
        jobs (see ``_operational_exclusion_clause``) are excluded from the
        listing and its durations. Pass ``include_operational=True`` to
        restore today's unfiltered figures.
        """
        extra_frags, extra_params = self._perf_filters(
            from_date, to_date, stage, epic_id, status, provider, include_operational
        )
        # extra_frags are constant literals from _perf_filters (e.g. "je.stage = %s");
        # every user value is passed separately as a bound param below, so this
        # string assembly is not injectable.
        extra_sql = "".join(f"  AND {f} " for f in extra_frags)
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT j.id AS job_id, j.idea, j.title, j.status, j.stage, j.created_at, "
                "SUM(EXTRACT(EPOCH FROM (je.ended_at::timestamptz - je.started_at::timestamptz))) AS total_duration_s "
                "FROM jobs j "
                "JOIN job_events je ON je.job_id = j.id "
                "WHERE j.project_id = %s "
                "  AND je.started_at IS NOT NULL AND je.started_at != '' "
                "  AND je.ended_at IS NOT NULL AND je.ended_at != '' "
                + extra_sql  # nosec B608
                + "GROUP BY j.id, j.idea, j.title, j.status, j.stage, j.created_at "
                "ORDER BY total_duration_s DESC NULLS LAST "
                "LIMIT %s",
                (project_id, *extra_params, limit),
            ).fetchall()
        return [
            {
                "job_id": int(r["job_id"]),
                "idea": r["idea"],
                "title": r["title"] or "",
                "status": r["status"],
                "stage": r["stage"],
                "created_at": r["created_at"],
                "total_duration_s": round(float(r["total_duration_s"]), 2)
                if r["total_duration_s"] is not None
                else None,
            }
            for r in rows
        ]

    def performance_headline_stats(
        self,
        project_id: int,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
        stage: str | None = None,
        epic_id: int | None = None,
        status: str | None = None,
        provider: str | None = None,
        include_operational: bool = False,
    ) -> dict:
        """Project-wide job count, success rate, cycle time, and usage totals.

        By default (``include_operational=False``), auto-deploy operational
        jobs (see ``_operational_exclusion_clause``) are excluded from
        ``total_jobs``/``success_rate``/``avg_cycle_time_s``/``total_tokens``/
        ``total_cost_usd``, and the response reports ``excludes_operational``
        plus ``operational_jobs_excluded`` (the count of operational jobs
        matching the same filters). Pass ``include_operational=True`` to
        restore today's unfiltered totals.
        """
        extra_frags, extra_params = self._perf_filters(
            from_date, to_date, stage, epic_id, status, provider, include_operational
        )
        extra_sql = "".join(f"    AND {f} " for f in extra_frags)
        with self._connection() as conn:
            row = conn.execute(
                "WITH per_job AS ( "
                "  SELECT je.job_id, "
                "    j.status, "
                "    EXTRACT(EPOCH FROM (MAX(je.ended_at::timestamptz) - MIN(je.started_at::timestamptz))) AS cycle_s, "
                "    SUM(je.tokens) AS tokens, "
                "    SUM(je.cost_usd) AS cost_usd "
                "  FROM job_events je "
                "  JOIN jobs j ON j.id = je.job_id "
                "  WHERE j.project_id = %s "
                "    AND je.started_at IS NOT NULL AND je.started_at != '' "
                "    AND je.ended_at IS NOT NULL AND je.ended_at != '' "
                + extra_sql
                + "  GROUP BY je.job_id, j.status "
                ") "
                "SELECT "
                "  COUNT(DISTINCT job_id) AS total_jobs, "
                "  CASE WHEN COUNT(DISTINCT job_id) = 0 THEN NULL "
                "       ELSE COUNT(DISTINCT CASE WHEN status = 'done' THEN job_id END)::float "
                "            / COUNT(DISTINCT job_id) END AS success_rate, "
                "  AVG(cycle_s) AS avg_cycle_time_s, "
                "  SUM(tokens) AS total_tokens, "
                "  SUM(cost_usd) AS total_cost_usd "
                "FROM per_job",
                (project_id, *extra_params),
            ).fetchone()
            if include_operational:
                operational_jobs_excluded = 0
            else:
                # Re-derive the filter fragments without the operational
                # exclusion (already folded into extra_frags above), then AND
                # in the un-negated clause explicitly to count what was cut.
                excl_frags, excl_params = self._perf_filters(
                    from_date, to_date, stage, epic_id, status, provider, include_operational=True
                )
                excl_sql = "".join(f"    AND {f} " for f in excl_frags)
                op_clause = self._operational_exclusion_clause("j").replace("%", "%%")
                excluded = conn.execute(
                    "SELECT COUNT(DISTINCT je.job_id) n "  # nosec B608
                    "FROM job_events je "
                    "JOIN jobs j ON j.id = je.job_id "
                    "WHERE j.project_id = %s "
                    "  AND je.started_at IS NOT NULL AND je.started_at != '' "
                    "  AND je.ended_at IS NOT NULL AND je.ended_at != '' "
                    f"  AND {op_clause} " + excl_sql,
                    (project_id, *excl_params),
                ).fetchone()
                operational_jobs_excluded = (excluded["n"] or 0) if excluded else 0
        if row is None or row["total_jobs"] == 0:
            return {
                "total_jobs": 0,
                "success_rate": None,
                "avg_cycle_time_s": None,
                "total_tokens": 0,
                "total_cost_usd": 0.0,
                "excludes_operational": not include_operational,
                "operational_jobs_excluded": operational_jobs_excluded,
            }
        return {
            "total_jobs": int(row["total_jobs"]),
            "success_rate": round(float(row["success_rate"]), 4)
            if row["success_rate"] is not None
            else None,
            "avg_cycle_time_s": round(float(row["avg_cycle_time_s"]), 2)
            if row["avg_cycle_time_s"] is not None
            else None,
            "total_tokens": int(row["total_tokens"] or 0),
            "total_cost_usd": round(float(row["total_cost_usd"] or 0), 6),
            "excludes_operational": not include_operational,
            "operational_jobs_excluded": operational_jobs_excluded,
        }

    def performance_trend(
        self,
        project_id: int,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
        include_operational: bool = False,
    ) -> list[dict]:
        """Daily cost/tokens/job-outcome buckets for a project's performance trend.

        Cost and tokens come from ``usage`` rows joined to their job (bucketed by
        ``usage.created_at``); completed/failed counts come from ``jobs`` directly
        (bucketed by ``jobs.updated_at``). The two are merged in Python by day
        since they aggregate over different tables/timestamps.

        By default (``include_operational=False``), auto-deploy operational jobs
        (see ``_operational_exclusion_clause``) are excluded from both the usage
        and jobs buckets. Pass ``include_operational=True`` to restore today's
        unfiltered per-day figures.
        """
        op_clause = self._operational_exclusion_clause("j").replace("%", "%%")

        usage_frags: list[str] = []
        usage_params: list = [project_id]
        if from_date is not None:
            usage_frags.append("u.created_at::timestamptz >= %s::timestamptz")
            usage_params.append(from_date)
        if to_date is not None:
            usage_frags.append("u.created_at::timestamptz <= %s::timestamptz")
            usage_params.append(to_date)
        if not include_operational:
            usage_frags.append(f"NOT {op_clause}")
        # frags are constant literals defined above (e.g. "u.created_at::timestamptz
        # >= %s::timestamptz"); every user value is passed separately as a bound
        # param below, so this string assembly is not injectable.
        usage_extra_sql = "".join(f"  AND {f} " for f in usage_frags)

        jobs_frags: list[str] = []
        jobs_params: list = [project_id]
        if from_date is not None:
            jobs_frags.append("j.updated_at::timestamptz >= %s::timestamptz")
            jobs_params.append(from_date)
        if to_date is not None:
            jobs_frags.append("j.updated_at::timestamptz <= %s::timestamptz")
            jobs_params.append(to_date)
        if not include_operational:
            jobs_frags.append(f"NOT {op_clause}")
        jobs_extra_sql = "".join(f"  AND {f} " for f in jobs_frags)

        with self._connection() as conn:
            usage_rows = conn.execute(
                "SELECT date_trunc('day', u.created_at::timestamptz) AS day, "
                "SUM(u.cost_usd) AS cost_usd, "
                "SUM(u.input_tokens + u.output_tokens + u.cache_creation_tokens + "
                "u.cache_read_tokens) AS tokens "
                "FROM usage u JOIN jobs j ON j.id = u.job_id "
                "WHERE j.project_id = %s "
                + usage_extra_sql  # nosec B608
                + "GROUP BY day",
                usage_params,
            ).fetchall()
            jobs_rows = conn.execute(
                "SELECT date_trunc('day', j.updated_at::timestamptz) AS day, "
                "COUNT(*) FILTER (WHERE j.status = 'done') AS jobs_completed, "
                "COUNT(*) FILTER (WHERE j.status = 'failed') AS jobs_failed "
                "FROM jobs j "
                "WHERE j.project_id = %s AND j.status IN ('done', 'failed') "
                + jobs_extra_sql  # nosec B608
                + "GROUP BY day",
                jobs_params,
            ).fetchall()

        days: dict[str, dict] = {}
        for r in usage_rows:
            key = r["day"].date().isoformat()
            bucket = days.setdefault(
                key,
                {"cost_usd": 0.0, "tokens": 0, "jobs_completed": 0, "jobs_failed": 0},
            )
            bucket["cost_usd"] += float(r["cost_usd"] or 0.0)
            bucket["tokens"] += int(r["tokens"] or 0)
        for r in jobs_rows:
            key = r["day"].date().isoformat()
            bucket = days.setdefault(
                key,
                {"cost_usd": 0.0, "tokens": 0, "jobs_completed": 0, "jobs_failed": 0},
            )
            bucket["jobs_completed"] += int(r["jobs_completed"] or 0)
            bucket["jobs_failed"] += int(r["jobs_failed"] or 0)

        out = []
        for day in sorted(days):
            bucket = days[day]
            completed, failed = bucket["jobs_completed"], bucket["jobs_failed"]
            total = completed + failed
            out.append(
                {
                    "date": day,
                    "cost_usd": round(bucket["cost_usd"], 4),
                    "tokens": bucket["tokens"],
                    "jobs_completed": completed,
                    "jobs_failed": failed,
                    "success_rate": (completed / total) if total > 0 else None,
                }
            )
        return out

    def deployment_reliability_breakdown(
        self,
        *,
        since: str | None = None,
        project_id: int | None = None,
    ) -> list[dict]:
        """Per-project reliability of auto-deploy operational jobs.

        Unlike every other usage/performance rollup in this file, this reports
        only the jobs the general ``include_operational`` exclusion filters
        out (see ``_operational_exclusion_clause``) — it is the intentional
        counterpart to that exclusion, letting operators see how reliable the
        auto-deploy jobs themselves are.
        """
        op_clause = self._operational_exclusion_clause("j").replace("%", "%%")
        clauses = [op_clause]
        params: list = []
        if since is not None:
            clauses.append("j.created_at >= %s")
            params.append(since)
        if project_id is not None:
            clauses.append("j.project_id = %s")
            params.append(project_id)
        where = " AND ".join(clauses)
        # clauses are constant literals (op_clause plus "col = %s" fragments);
        # every user value is passed separately as a bound param below, so
        # this string assembly is not injectable.
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT j.project_id, p.name AS project_name, "  # nosec B608
                "COUNT(*) FILTER (WHERE j.status = 'done') AS done, "
                "COUNT(*) FILTER (WHERE j.status = 'failed') AS failed, "
                "COUNT(*) FILTER (WHERE j.status = 'cancelled') AS cancelled, "
                "COUNT(*) AS total "
                "FROM jobs j LEFT JOIN projects p ON p.id = j.project_id "
                f"WHERE {where} "
                "GROUP BY j.project_id, p.name ORDER BY j.project_id",
                params,
            ).fetchall()
        out = []
        for r in rows:
            total = r["total"] or 0
            failed = r["failed"] or 0
            out.append(
                {
                    "project_id": r["project_id"],
                    "project_name": r["project_name"],
                    "total": total,
                    "done": r["done"] or 0,
                    "failed": failed,
                    "cancelled": r["cancelled"] or 0,
                    "failure_rate": (failed / total) if total > 0 else None,
                }
            )
        return out

    # --- audit trail ----------------------------------------------------
    def list_audit(
        self,
        *,
        table: str | None = None,
        actor: str | None = None,
        row_pk: str | None = None,
        limit: int = 200,
        before_id: int | None = None,
    ) -> list[dict]:
        """Recent audit_log rows, newest first. ``before_id`` pages backward."""
        frags = ["TRUE"]
        params: list = []
        if table:
            frags.append("table_name = %s")
            params.append(table)
        if actor:
            frags.append("actor ILIKE %s")
            params.append(f"%{actor}%")
        if row_pk:
            frags.append("row_pk = %s")
            params.append(row_pk)
        if before_id is not None:
            frags.append("id < %s")
            params.append(before_id)
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT id, at, actor, table_name, row_pk, action, changed, "
                "backend_pid, client_addr, backend_start "
                f"FROM audit_log WHERE {' AND '.join(frags)} "  # nosec B608
                "ORDER BY id DESC LIMIT %s",
                (*params, min(max(int(limit), 1), 1000)),
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "at": r["at"].isoformat() if r["at"] is not None else None,
                "actor": r["actor"],
                "table_name": r["table_name"],
                "row_pk": r["row_pk"],
                "action": r["action"],
                "changed": r["changed"],
                "backend_pid": r["backend_pid"],
                "client_addr": r["client_addr"],
                "backend_start": r["backend_start"].isoformat()
                if r["backend_start"] is not None
                else None,
            }
            for r in rows
        ]

    def prune_audit_log(self, *, days: int = 90) -> int:
        """Delete audit rows older than ``days``; returns rows removed."""
        with self._connection() as conn:
            cur = conn.execute(
                "DELETE FROM audit_log WHERE at < now() - make_interval(days => %s)", (days,)
            )
            return cur.rowcount or 0

    _EVENT_SELECT = (
        "SELECT je.id, je.job_id, je.stage, je.status, je.attempt, je.summary, "
        "je.detail, je.tokens, je.cost_usd, je.started_at, je.ended_at, je.agent_id, "
        "pa.name AS agent_name, pa.provider AS agent_provider, pa.model AS agent_model "
        "FROM job_events je "
        "LEFT JOIN project_agents pa ON pa.id = je.agent_id "
    )

    @staticmethod
    def _event_dict(row) -> dict:
        event = dict(row)
        try:
            event["detail"] = json.loads(event["detail"]) if event["detail"] else {}
        except (TypeError, ValueError):
            event["detail"] = {}
        return event

    def list_events(self, job_id: int) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute(
                self._EVENT_SELECT + "WHERE je.job_id = %s ORDER BY je.id",
                (job_id,),
            ).fetchall()
        return [self._event_dict(row) for row in rows]

    def list_events_after(self, job_id: int, after_id: int = 0) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute(
                self._EVENT_SELECT + "WHERE je.job_id = %s AND je.id > %s ORDER BY je.id LIMIT 200",
                (job_id, after_id),
            ).fetchall()
        return [self._event_dict(row) for row in rows]

    # --- durable runner state -----------------------------------------
    def get_meta(self, key: str, default: str = "") -> str:
        with self._connection() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = %s", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (%s, %s) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_or_create_page_view_salt(self) -> str:
        salt = self.get_meta("page_view_salt")
        if salt:
            return salt
        self.set_meta("page_view_salt", secrets.token_hex(32))
        return self.get_meta("page_view_salt")

    # --- live subprocess log lines (test + deploy stages) ----------------
    def append_log(self, job_id: int, stage: str, line: str, attempt: int = 0) -> None:
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO job_logs(job_id, stage, line, ts, attempt) VALUES (%s, %s, %s, %s, %s)",
                (job_id, stage, line, time.time(), attempt),
            )

    def tail_logs(self, job_id: int, after_id: int = 0) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT id, stage, line, attempt FROM job_logs "
                "WHERE job_id = %s AND id > %s ORDER BY id LIMIT 500",
                (job_id, after_id),
            ).fetchall()
        return [dict(r) for r in rows]

    # --- per-project symbol index (deterministic; AI only consumes) ----------
    def refresh_symbol_index(self, project_id: int, repo_path: str) -> int:
        """Extract public symbols from repo_path and upsert them into symbol_index.

        Symbols no longer present in the repo (updated_at older than this run's
        timestamp) are deleted, keeping the index in sync with the codebase.
        Returns the number of live rows after the refresh.
        """
        from . import symbols as _symbols

        now_ts = time.time()
        rows = _symbols.extract_symbols(repo_path)
        with self._connection() as conn:
            for row in rows:
                conn.execute(
                    "INSERT INTO symbol_index(project_id, module, symbol, kind, signature, summary, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (project_id, module, symbol) DO UPDATE SET "
                    "kind=EXCLUDED.kind, signature=EXCLUDED.signature, "
                    "summary=EXCLUDED.summary, updated_at=EXCLUDED.updated_at",
                    (
                        project_id,
                        row["module"],
                        row["symbol"],
                        row["kind"],
                        row["signature"],
                        row["summary"],
                        now_ts,
                    ),
                )
            conn.execute(
                "DELETE FROM symbol_index WHERE project_id = %s AND updated_at < %s",
                (project_id, now_ts),
            )
            result = conn.execute(
                "SELECT COUNT(*) AS n FROM symbol_index WHERE project_id = %s", (project_id,)
            ).fetchone()
        return int(result["n"]) if result else 0

    def get_symbol_index_names(self, project_id: int) -> dict[str, list[dict]]:
        """Return the symbol index for project_id as {name: [{module, kind, signature}]}.

        Used by the collision checker to compare the pre-build snapshot against
        symbols extracted from the post-build worktree.
        """
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT symbol, module, kind, signature FROM symbol_index WHERE project_id = %s",
                (project_id,),
            ).fetchall()
        result: dict[str, list[dict]] = {}
        for r in rows:
            result.setdefault(r["symbol"], []).append(
                {
                    "module": r["module"],
                    "kind": r["kind"],
                    "signature": r["signature"],
                }
            )
        return result

    def get_symbol_index_text(self, project_id: int, limit: int = 300) -> str:
        """Return the symbol index for project_id as a compact plain-text block.

        Each symbol is one line; summary is appended as a comment when present.
        Returns '' if no symbols are indexed for this project.
        """
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT module, symbol, kind, signature, summary "
                "FROM symbol_index WHERE project_id = %s "
                "ORDER BY module, symbol LIMIT %s",
                (project_id, limit),
            ).fetchall()
        if not rows:
            return ""
        lines = [f"## Codebase symbols ({len(rows)} shown)"]
        for r in rows:
            line = f"  {r['kind']:10} {r['module']}.{r['symbol']}  {r['signature']}"
            if r["summary"]:
                line += f"  # {r['summary']}"
            lines.append(line)
        return "\n".join(lines)

    def reset_running(self) -> None:
        """On startup, flip any interrupted RUNNING jobs back to PENDING.

        Single-host only: with multiple workers/hosts this would rip live jobs out
        from under peers, so the runner relies on lease expiry instead. Kept for
        the single-process case and API compatibility.
        """
        with self._connection() as conn:
            conn.execute(
                "UPDATE jobs SET status = %s WHERE status = %s",
                (JobStatus.PENDING.value, JobStatus.RUNNING.value),
            )

    def close(self) -> None:
        self._pool.close()

    # --- intake sessions ------------------------------------------------------

    def create_intake_session(self, created_by: str) -> IntakeSession:
        from uuid import uuid4

        from hyqs.intake.models import IntakeSession

        from .models import _now

        session_id = str(uuid4())
        now = _now()
        with self._connection() as conn:
            row = conn.execute(
                "INSERT INTO intake_sessions(session_id, created_by, draft_spec, messages, "
                "status, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (session_id, created_by, json.dumps({}), json.dumps([]), "in_progress", now, now),
            ).fetchone()
        return IntakeSession.from_row(row)

    def get_intake_session(self, session_id: str) -> IntakeSession | None:
        from hyqs.intake.models import IntakeSession

        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM intake_sessions WHERE session_id = %s", (session_id,)
            ).fetchone()
        return IntakeSession.from_row(row) if row else None

    def list_intake_sessions(self, created_by: str) -> list[IntakeSession]:
        from hyqs.intake.models import IntakeSession

        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM intake_sessions WHERE created_by = %s ORDER BY created_at DESC",
                (created_by,),
            ).fetchall()
        return [IntakeSession.from_row(r) for r in rows]

    def confirm_intake_session(self, session_id: str, project_id: int) -> None:
        from .models import _now

        with self._connection() as conn:
            conn.execute(
                "UPDATE intake_sessions SET status='confirmed', project_id=%s, updated_at=%s "
                "WHERE session_id=%s AND status='in_progress'",
                (project_id, _now(), session_id),
            )

    def update_intake_session(
        self,
        session_id: str,
        *,
        draft_spec: dict | None = None,
        messages: list[dict] | None = None,
        status: str | None = None,
        project_id: int | None = None,
    ) -> IntakeSession:
        from hyqs.intake.models import IntakeSession

        from .models import _now

        fields: dict = {}
        if draft_spec is not None:
            fields["draft_spec"] = json.dumps(draft_spec)
        if messages is not None:
            fields["messages"] = json.dumps(messages)
        if status is not None:
            fields["status"] = status
        if project_id is not None:
            fields["project_id"] = project_id
        fields["updated_at"] = _now()
        set_clause = ", ".join(f"{k}=%s" for k in fields)
        values = list(fields.values()) + [session_id]
        with self._connection() as conn:
            conn.execute(f"UPDATE intake_sessions SET {set_clause} WHERE session_id=%s", values)
            row = conn.execute(
                "SELECT * FROM intake_sessions WHERE session_id=%s", (session_id,)
            ).fetchone()
        return IntakeSession.from_row(row)

    # --- chat sessions -------------------------------------------------------

    def create_chat_session(self, project_id: int, created_by: str) -> ChatSession:
        from uuid import uuid4

        from .models import _now

        session_id = str(uuid4())
        now = _now()
        with self._connection() as conn:
            row = conn.execute(
                "INSERT INTO chat_sessions(session_id, project_id, created_by, messages, "
                "proposed_jobs, status, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (
                    session_id,
                    project_id,
                    created_by,
                    json.dumps([]),
                    json.dumps([]),
                    "active",
                    now,
                    now,
                ),
            ).fetchone()
        return ChatSession.from_row(row)

    def get_chat_session(self, session_id: str) -> ChatSession | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM chat_sessions WHERE session_id = %s", (session_id,)
            ).fetchone()
        return ChatSession.from_row(row) if row else None

    def update_chat_session(
        self,
        session_id: str,
        *,
        messages: list[dict] | None = None,
        proposed_jobs: list[dict] | None = None,
        status: str | None = None,
        source_backlog_item_ids: list[int] | None = None,
    ) -> ChatSession:
        from .models import _now

        fields: dict = {}
        if messages is not None:
            fields["messages"] = json.dumps(messages)
        if proposed_jobs is not None:
            fields["proposed_jobs"] = json.dumps(proposed_jobs)
        if status is not None:
            fields["status"] = status
        if source_backlog_item_ids is not None:
            fields["source_backlog_item_ids"] = json.dumps(source_backlog_item_ids)
        fields["updated_at"] = _now()
        set_clause = ", ".join(f"{k}=%s" for k in fields)
        values = list(fields.values()) + [session_id]
        with self._connection() as conn:
            conn.execute(f"UPDATE chat_sessions SET {set_clause} WHERE session_id=%s", values)
            row = conn.execute(
                "SELECT * FROM chat_sessions WHERE session_id=%s", (session_id,)
            ).fetchone()
        return ChatSession.from_row(row)

    def list_chat_sessions(self, project_id: int, created_by: str) -> list[ChatSession]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM chat_sessions WHERE project_id=%s AND created_by=%s "
                "AND status != 'abandoned' ORDER BY created_at DESC LIMIT 20",
                (project_id, created_by),
            ).fetchall()
        return [ChatSession.from_row(r) for r in rows]

    # --- role permissions ----------------------------------------------------

    def get_role_permissions(self) -> dict[str, list[str]]:
        """Return all role→permission grants grouped by role."""
        result: dict[str, list[str]] = {role: [] for role in _SEED_PERMISSIONS}
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT role, permission FROM role_permissions ORDER BY role, permission"
            ).fetchall()
        for row in rows:
            r = row["role"]
            if r not in result:
                result[r] = []
            result[r].append(row["permission"])
        return result

    def set_role_permission(self, role: str, permission: str, enabled: bool) -> None:
        """Grant or revoke a permission for a role.

        Raises ValueError for unknown roles/permissions or locked platform_admin permissions.
        """
        if role not in _KNOWN_ROLES:
            raise ValueError(f"unknown role: {role!r}")
        if permission not in _KNOWN_PERMISSIONS:
            raise ValueError(f"unknown permission: {permission!r}")
        if (
            role == "platform_admin"
            and not enabled
            and permission in _PLATFORM_ADMIN_LOCKED_PERMISSIONS
        ):
            raise ValueError(f"platform_admin cannot lose {permission}")
        with self._connection() as conn:
            if enabled:
                conn.execute(
                    "INSERT INTO role_permissions(role, permission) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (role, permission),
                )
            else:
                conn.execute(
                    "DELETE FROM role_permissions WHERE role = %s AND permission = %s",
                    (role, permission),
                )

    # --- project members -----------------------------------------------------

    def list_project_members(self, project_id: int) -> list[ProjectMember]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM project_members WHERE project_id = %s ORDER BY id",
                (project_id,),
            ).fetchall()
        return [ProjectMember.from_row(r) for r in rows]

    def add_project_member(self, project_id: int, user_id: str, role: str) -> ProjectMember:
        from .models import _now

        now = _now()
        with self._connection() as conn:
            row = conn.execute(
                "INSERT INTO project_members(user_id, project_id, role, created_at) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT(user_id, project_id) DO UPDATE SET role = EXCLUDED.role "
                "RETURNING *",
                (user_id, project_id, role, now),
            ).fetchone()
        return ProjectMember.from_row(row)

    def remove_project_member(self, project_id: int, user_id: str) -> bool:
        with self._connection() as conn:
            n = conn.execute(
                "DELETE FROM project_members WHERE project_id = %s AND user_id = %s",
                (project_id, user_id),
            ).rowcount
        return n > 0

    def update_member_role(self, project_id: int, user_id: str, role: str) -> ProjectMember | None:
        with self._connection() as conn:
            row = conn.execute(
                "UPDATE project_members SET role = %s "
                "WHERE project_id = %s AND user_id = %s RETURNING *",
                (role, project_id, user_id),
            ).fetchone()
        return ProjectMember.from_row(row) if row else None

    def get_user_project_ids(self, user_id: str) -> list[int]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT project_id FROM project_members WHERE user_id = %s",
                (user_id,),
            ).fetchall()
        return [int(r["project_id"]) for r in rows]

    def get_user_memberships(self, user_id: int) -> list[ProjectMember]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM project_members WHERE user_id = %s ORDER BY id",
                (str(user_id),),
            ).fetchall()
        return [ProjectMember.from_row(r) for r in rows]

    # --- environment members (environment-scoped RBAC grants) -----------------

    def add_environment_member(
        self, environment_id: int, user_id: str, role: str
    ) -> EnvironmentMember:
        from .models import _now

        now = _now()
        with self._connection() as conn:
            row = conn.execute(
                "INSERT INTO environment_members(user_id, environment_id, role, created_at) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT(user_id, environment_id) DO UPDATE SET role = EXCLUDED.role "
                "RETURNING *",
                (user_id, environment_id, role, now),
            ).fetchone()
        return EnvironmentMember.from_row(row)

    def get_environment_member(self, environment_id: int, user_id: str) -> EnvironmentMember | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM environment_members WHERE environment_id = %s AND user_id = %s",
                (environment_id, user_id),
            ).fetchone()
        return EnvironmentMember.from_row(row) if row else None

    def list_environment_members(self, environment_id: int) -> list[EnvironmentMember]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM environment_members WHERE environment_id = %s ORDER BY id",
                (environment_id,),
            ).fetchall()
        return [EnvironmentMember.from_row(r) for r in rows]

    # --- API tokens (project-scoped, least-privilege) -------------------------

    def create_api_token(
        self, project_id: int, name: str, role: str, created_by: str = ""
    ) -> tuple[ApiToken, str]:
        """Create a project-scoped API token. Returns (ApiToken, plaintext secret).

        The plaintext secret is generated here and returned exactly once — only
        its sha256 hash and last4 are ever persisted.
        """
        from .models import _now

        secret = "hpat_" + secrets.token_urlsafe(32)  # pragma: allowlist secret
        token_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        now = _now()
        with self._connection() as conn:
            row = conn.execute(
                "INSERT INTO api_tokens(project_id, name, role, token_hash, last4, created_by, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (project_id, name, role, token_hash, secret[-4:], created_by, now),
            ).fetchone()
        return ApiToken.from_row(row), secret

    def list_api_tokens(self, project_id: int) -> list[ApiToken]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM api_tokens WHERE project_id = %s ORDER BY id", (project_id,)
            ).fetchall()
        return [ApiToken.from_row(r) for r in rows]

    def get_api_token_by_secret(self, secret: str) -> ApiToken | None:
        token_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM api_tokens WHERE token_hash = %s AND revoked_at IS NULL",
                (token_hash,),
            ).fetchone()
        return ApiToken.from_row(row) if row else None

    def touch_api_token(self, token_id: int) -> None:
        from .models import _now

        with self._connection() as conn:
            conn.execute(
                "UPDATE api_tokens SET last_used_at = %s WHERE id = %s", (_now(), token_id)
            )

    def revoke_api_token(self, project_id: int, token_id: int) -> bool:
        from .models import _now

        with self._connection() as conn:
            cur = conn.execute(
                "UPDATE api_tokens SET revoked_at = %s "
                "WHERE id = %s AND project_id = %s AND revoked_at IS NULL",
                (_now(), token_id, project_id),
            )
            return cur.rowcount > 0

    # --- users + sessions (S1) -----------------------------------------------

    def create_user(
        self,
        email: str,
        password_plaintext: str,
        display_name: str = "",
        is_platform_admin: bool = False,
    ) -> User:
        from .models import _now

        pw_hash = bcrypt.hashpw(password_plaintext.encode("utf-8"), bcrypt.gensalt()).decode(
            "utf-8"
        )
        now = _now()
        with self._connection() as conn:
            row = conn.execute(
                "INSERT INTO users(email, password_hash, display_name, is_platform_admin, is_active, created_at) "
                "VALUES (%s, %s, %s, %s, TRUE, %s) RETURNING *",
                (email, pw_hash, display_name, is_platform_admin, now),
            ).fetchone()
        return User.from_row(row)

    def get_user_by_email(self, email: str) -> User | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT id, email, display_name, is_platform_admin, is_active, oauth_provider, created_at "
                "FROM users WHERE email = %s",
                (email,),
            ).fetchone()
        return User.from_row(row) if row else None

    def get_user_by_id(self, user_id: int) -> User | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT id, email, display_name, is_platform_admin, is_active, oauth_provider, created_at "
                "FROM users WHERE id = %s",
                (user_id,),
            ).fetchone()
        return User.from_row(row) if row else None

    def list_users(self) -> list[User]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT id, email, display_name, is_platform_admin, is_active, oauth_provider, created_at "
                "FROM users ORDER BY id"
            ).fetchall()
        return [User.from_row(r) for r in rows]

    def grant_platform_permission(self, user_id: int, permission: str, granted_by: str) -> None:
        from .models import _now

        now = _now()
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO user_platform_permissions(user_id, permission, granted_at, granted_by) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (user_id, permission, now, granted_by),
            )

    def revoke_platform_permission(self, user_id: int, permission: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "DELETE FROM user_platform_permissions WHERE user_id = %s AND permission = %s",
                (user_id, permission),
            )

    def list_platform_permissions(self, user_id: int) -> list[str]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT permission FROM user_platform_permissions WHERE user_id = %s ORDER BY permission",
                (user_id,),
            ).fetchall()
        return [r["permission"] for r in rows]

    def authenticate_user(self, email: str, password: str) -> User | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE email = %s",
                (email,),
            ).fetchone()
        eligible = row is not None and bool(row["is_active"]) and bool(row["password_hash"])
        target_hash = row["password_hash"] if eligible else _DUMMY_PASSWORD_HASH
        try:
            valid = bcrypt.checkpw(password.encode("utf-8"), target_hash.encode("utf-8"))
        except ValueError:
            return None
        if not eligible or not valid:
            return None
        return User.from_row(row)

    def create_session(self, user_id: int, ttl_days: int = 30) -> str:
        from .models import _now

        token = secrets.token_hex(32)
        expires_at = time.time() + ttl_days * 86400
        now = _now()
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO sessions(token, user_id, expires_at, created_at) VALUES (%s, %s, %s, %s)",
                (token, user_id, expires_at, now),
            )
        return token

    def get_session(self, token: str) -> User | None:
        now = time.time()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT u.id, u.email, u.display_name, u.is_platform_admin, u.is_active, "
                "u.oauth_provider, u.created_at "
                "FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token = %s AND s.expires_at > %s AND u.is_active = TRUE",
                (token, now),
            ).fetchone()
        return User.from_row(row) if row else None

    def delete_session(self, token: str) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM sessions WHERE token = %s", (token,))

    def update_user(
        self,
        user_id: int,
        *,
        display_name: str | None = None,
        is_active: bool | None = None,
        is_platform_admin: bool | None = None,
    ) -> User | None:
        fields: dict[str, object] = {}
        if display_name is not None:
            fields["display_name"] = display_name
        if is_active is not None:
            fields["is_active"] = is_active
        if is_platform_admin is not None:
            fields["is_platform_admin"] = is_platform_admin
        if not fields:
            return self.get_user_by_id(user_id)
        set_clause = ", ".join(f"{k}=%s" for k in fields)
        values = list(fields.values()) + [user_id]
        with self._connection() as conn:
            conn.execute(f"UPDATE users SET {set_clause} WHERE id=%s", values)
            row = conn.execute(
                "SELECT id, email, display_name, is_platform_admin, is_active, created_at "
                "FROM users WHERE id = %s",
                (user_id,),
            ).fetchone()
        return User.from_row(row) if row else None

    def delete_user_sessions(self, user_id: int) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))

    def delete_user(self, user_id: int) -> bool:
        with self._connection() as conn:
            conn.execute("DELETE FROM project_members WHERE user_id = %s", (str(user_id),))
            n = conn.execute("DELETE FROM users WHERE id = %s", (user_id,)).rowcount
        return n > 0

    # --- invitations + OAuth provisioning -----------------------------------

    def create_invitation(self, email: str, invited_by: str = "", ttl_days: int = 7) -> Invitation:
        from .models import _now

        token = secrets.token_urlsafe(32)
        expires_at = time.time() + ttl_days * 86400
        now = _now()
        with self._connection() as conn:
            row = conn.execute(
                "INSERT INTO invitations(token, email, invited_by, expires_at, created_at) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING *",
                (token, email, invited_by, expires_at, now),
            ).fetchone()
        return Invitation.from_row(row)

    def consume_invitation(self, email: str) -> Invitation | None:
        now = time.time()
        with self._connection() as conn:
            row = conn.execute(
                "UPDATE invitations SET consumed_at = %s "
                "WHERE email = %s AND consumed_at IS NULL AND expires_at > %s "
                "RETURNING *",
                (now, email, now),
            ).fetchone()
        return Invitation.from_row(row) if row else None

    def list_invitations(self) -> list[Invitation]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM invitations ORDER BY created_at DESC").fetchall()
        return [Invitation.from_row(r) for r in rows]

    def revoke_invitation(self, token: str) -> bool:
        with self._connection() as conn:
            result = conn.execute(
                "DELETE FROM invitations WHERE token = %s RETURNING id", (token,)
            ).fetchone()
        return result is not None

    # --- backlog ---------------------------------------------------------------

    def create_backlog_item(
        self,
        project_id: int,
        title: str,
        body: str,
        type: str,
        proposed_by: str,
        *,
        epic_hint: str | None = None,
    ) -> BacklogItem:
        from .models import _now

        valid_types = {t.value for t in BacklogItemType}
        if type not in valid_types:
            raise ValueError(
                f"invalid backlog item type '{type}': must be one of {sorted(valid_types)}"
            )

        now = _now()
        with self._connection() as conn:
            row = conn.execute(
                "INSERT INTO backlog_items(project_id, title, body, type, proposed_by, "
                "epic_hint, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (project_id, title, body, type, proposed_by, epic_hint, now, now),
            ).fetchone()
            item_id = row["id"]
        item = self.get_backlog_item(item_id)
        if item is None:
            raise RuntimeError(f"backlog item {item_id} missing immediately after insert")
        return item

    def list_backlog_items(
        self,
        project_id: int,
        *,
        status: str | None = None,
        type: str | None = None,
    ) -> list[BacklogItem]:
        clauses = ["project_id = %s"]
        params: list = [project_id]
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        if type is not None:
            clauses.append("type = %s")
            params.append(type)
        # clauses are constant literals ("col = %s"); every user value is passed
        # separately as a bound param to execute() below, so this is not injectable.
        where = " AND ".join(clauses)
        sql = f"SELECT * FROM backlog_items WHERE {where} ORDER BY created_at DESC"  # nosec B608
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        items = []
        for row in rows:
            try:
                items.append(BacklogItem.from_row(row))
            except ValueError:
                _log.warning("skipping corrupted backlog_items row id=%s", row["id"])
        return items

    def get_backlog_item(self, item_id: int) -> BacklogItem | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM backlog_items WHERE id = %s", (item_id,)).fetchone()
        return BacklogItem.from_row(row) if row else None

    def vote_backlog_item(self, item_id: int, user_id: str) -> int:
        from .models import _now

        with self._connection() as conn:
            try:
                with conn.transaction():
                    conn.execute(
                        "INSERT INTO backlog_votes(item_id, user_id) VALUES (%s, %s)",
                        (item_id, user_id),
                    )
                    row = conn.execute(
                        "UPDATE backlog_items SET votes = votes + 1, updated_at = %s "
                        "WHERE id = %s RETURNING votes",
                        (_now(), item_id),
                    ).fetchone()
            except psycopg.errors.UniqueViolation:
                conn.execute(
                    "DELETE FROM backlog_votes WHERE item_id = %s AND user_id = %s",
                    (item_id, user_id),
                )
                row = conn.execute(
                    "UPDATE backlog_items SET votes = votes - 1, updated_at = %s "
                    "WHERE id = %s RETURNING votes",
                    (_now(), item_id),
                ).fetchone()
        return int(row["votes"])

    def set_backlog_status(self, item_id: int, status: str) -> bool:
        from .models import _now

        with self._connection() as conn:
            result = conn.execute(
                "UPDATE backlog_items SET status = %s, updated_at = %s WHERE id = %s",
                (status, _now(), item_id),
            )
        return result.rowcount == 1

    def patch_backlog_item(
        self,
        item_id: int,
        *,
        status: str | None = None,
        epic_hint: str | None = _UNSET,  # type: ignore[assignment]
    ) -> bool:
        from .models import _now

        if status is not None:
            valid_statuses = {s.value for s in BacklogItemStatus}
            if status not in valid_statuses:
                raise ValueError(
                    f"invalid backlog item status '{status}': must be one of "
                    f"{sorted(valid_statuses)}"
                )

        sets = ["updated_at = %s"]
        params: list = [_now()]
        if status is not None:
            sets.append("status = %s")
            params.append(status)
        if epic_hint is not _UNSET:
            sets.append("epic_hint = %s")
            params.append(epic_hint)
        params.append(item_id)
        sql = f"UPDATE backlog_items SET {', '.join(sets)} WHERE id = %s"
        with self._connection() as conn:
            result = conn.execute(sql, params)
        return result.rowcount == 1

    def link_backlog_jobs(self, item_id: int, job_ids: list[int]) -> bool:
        from .models import _now

        with self._connection() as conn:
            result = conn.execute(
                "UPDATE backlog_items SET linked_job_ids = %s::jsonb, status = 'converted', "
                "updated_at = %s WHERE id = %s",
                (json.dumps(job_ids), _now(), item_id),
            )
        return result.rowcount == 1

    def get_backlog_items_for_job(self, job_id: int) -> list[BacklogItem]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM backlog_items WHERE linked_job_ids @> %s::jsonb",
                (json.dumps([job_id]),),
            ).fetchall()
        return [BacklogItem.from_row(r) for r in rows]

    # --- OAuth / users -------------------------------------------------------

    def provision_oauth_user(
        self,
        email: str,
        display_name: str,
        provider: str,
        *,
        is_platform_admin: bool = False,
    ) -> User:
        from .models import _now

        now = _now()
        with self._connection() as conn:
            row = conn.execute(
                "INSERT INTO users(email, password_hash, display_name, oauth_provider, "
                "is_platform_admin, is_active, created_at) "
                "VALUES (%s, '', %s, %s, %s, TRUE, %s) RETURNING *",
                (email, display_name, provider, is_platform_admin, now),
            ).fetchone()
        return User.from_row(row)
