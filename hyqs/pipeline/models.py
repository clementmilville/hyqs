"""Pipeline data model: jobs and the stages they move through."""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Mapping


class Stage(str, enum.Enum):
    """The ordered stages a job advances through."""

    QUEUED = "queued"
    PLAN = "plan"
    LINT = "lint"  # deterministic: format+lint the worktree before tests
    BUILD = "build"
    TEST = "test"
    FIX = "fix"  # self-heal: re-edit the worktree after a failed TEST/REVIEW
    REVIEW = "review"
    SECURITY = "security"
    DESIGN_REVIEW = "design_review"
    MERGE = "merge"
    # Routing stage (not in STAGE_ORDER): lightweight re-verify after a merge-boundary
    # event (conflict resolution or clean base advance) before re-attempting the merge.
    MERGE_VERIFY = "merge_verify"
    DEPLOY = "deploy"
    DONE = "done"


class JobStatus(str, enum.Enum):
    PENDING = "pending"  # waiting to be picked up / between stages
    RUNNING = "running"  # a stage is currently executing
    DEPLOYING = "deploying"  # deploy ran; waiting for post-restart commit verification
    DONE = "done"  # merged successfully and deployment verified live
    FAILED = "failed"  # a stage failed; needs human attention
    CANCELLED = "cancelled"  # cancelled by user before completion


class DependencyProvenance(str, enum.Enum):
    """Why a dependency edge exists and whether policy may remove it."""

    USER = "user"
    SEMANTIC = "semantic"
    AUTO = "auto"


TERMINAL_JOB_STATUSES = frozenset({JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED})


@dataclass(frozen=True, slots=True)
class CurrentExecutor:
    """Presentation-safe projection of who is executing a job right now."""

    kind: Literal["agent", "pipeline", "waiting"]
    label: str
    agent_id: int | None = None
    provider: str = ""


@dataclass(frozen=True, slots=True)
class JobsPage:
    """One keyset-paginated page of jobs."""

    jobs: list[Job]
    next_cursor: int | None = None


class JobSource(str, enum.Enum):
    UI = "ui"
    SUPERVISOR = "supervisor"
    CLI = "cli"
    INTAKE = "intake"
    MCP = "mcp"
    UNKNOWN = "unknown"


class AgentTask(str, enum.Enum):
    """The agentic (AI) units of work a roster agent can be allowed to do.

    The deterministic steps (test, merge) are not tasks — any worker runs them.
    """

    PLAN = "plan"
    BUILD = "build"
    FIX = "fix"
    REVIEW = "review"
    SECURITY = "security"


ALL_TASKS: list[str] = [t.value for t in AgentTask]

# What agentic task a job at a given stage needs *next*. ``None`` means the next
# step is deterministic (run tests, merge) and needs no AI agent.
_STAGE_TASK = {
    Stage.QUEUED: AgentTask.PLAN,
    Stage.PLAN: AgentTask.BUILD,
    Stage.FIX: AgentTask.FIX,
    Stage.TEST: AgentTask.REVIEW,
    Stage.REVIEW: AgentTask.SECURITY,
    # MERGE_VERIFY runs AI review+security over the merge delta: claim it like an
    # agentic stage so the provider-pause gate and roster routing apply (otherwise
    # a paused provider gets hammered by doomed claim→call→pause cycles).
    Stage.MERGE_VERIFY: AgentTask.REVIEW,
    # At SECURITY the next step is the DESIGN_REVIEW gate (an agentic UX gate), so
    # it needs an agent claim; route it through the reviewer roster.
    Stage.SECURITY: AgentTask.REVIEW,
}


def stage_task(stage: "Stage") -> "AgentTask | None":
    return _STAGE_TASK.get(stage)


class SchedulerWaitReason(str, enum.Enum):
    PROJECT_SLOT_OCCUPIED = "project_slot_occupied"
    FILE_OVERLAP = "file_overlap"
    SCHEMA_LOCK = "schema_lock"
    MERGE_LOCK = "merge_lock"
    PROVIDER_CAPACITY = "provider_capacity"


@dataclass(frozen=True, slots=True)
class SchedulerWait:
    """Why a job is waiting instead of running, for scheduler diagnostics."""

    reason: SchedulerWaitReason
    summary: str
    blocking_job_ids: list[int]
    conflicting_paths: list[str]

    def to_dict(self) -> dict:
        return {
            "reason": self.reason.value,
            "summary": self.summary,
            "blocking_job_ids": self.blocking_job_ids,
            "conflicting_paths": self.conflicting_paths,
        }


REMEDIATION_PRIORITY_BOOST = 20
CRITICAL_PATH_BOOST_PER_DEPENDENT = 3
CRITICAL_PATH_BOOST_PER_DEPTH = 2
MAX_CRITICAL_PATH_BOOST = 30
PRIORITY_AGING_PER_HOUR = 1
MAX_PRIORITY_AGING_BOOST = 15

# Keys identifying an automated remediation/fix-forward job on its source_meta, for
# priority-boost purposes. Deliberately broader than and kept separate from
# supervisor.py's private ``_REMEDIATION_FIX_FOR_KEYS``, which drives narrower
# cycle-repair matching — do not merge the two lists.
REMEDIATION_LINK_KEYS: tuple[str, ...] = (
    "ai_fix_for",
    "deploy_fix_for",
    "alembic_merge_fix_for",
    "fix_for",
    "fixes_job_id",
    "parent_job_id",
    "remediation_root_job_id",
)


class PriorityBoostReason(str, enum.Enum):
    REMEDIATION = "remediation"
    CRITICAL_PATH = "critical_path"
    AGING = "aging"


@dataclass(frozen=True, slots=True)
class PriorityBoost:
    reason: PriorityBoostReason
    amount: int
    detail: str

    def to_dict(self) -> dict:
        return {
            "reason": self.reason.value,
            "amount": self.amount,
            "detail": self.detail,
        }


def compute_effective_priority(
    base_priority: int,
    *,
    is_remediation: bool,
    unresolved_dependent_count: int,
    max_dependent_depth: int,
    age_hours: float,
) -> tuple[int, list[PriorityBoost]]:
    """Pure combination of capped remediation/critical-path/aging boosts."""
    remediation_amount = REMEDIATION_PRIORITY_BOOST if is_remediation else 0
    critical_path_amount = min(
        MAX_CRITICAL_PATH_BOOST,
        unresolved_dependent_count * CRITICAL_PATH_BOOST_PER_DEPENDENT
        + max_dependent_depth * CRITICAL_PATH_BOOST_PER_DEPTH,
    )
    aging_amount = min(MAX_PRIORITY_AGING_BOOST, int(age_hours * PRIORITY_AGING_PER_HOUR))

    candidates = [
        PriorityBoost(
            reason=PriorityBoostReason.REMEDIATION,
            amount=remediation_amount,
            detail=f"remediation job (+{remediation_amount})",
        ),
        PriorityBoost(
            reason=PriorityBoostReason.CRITICAL_PATH,
            amount=critical_path_amount,
            detail=(
                f"{unresolved_dependent_count} unresolved dependent(s), "
                f"depth {max_dependent_depth} (+{critical_path_amount})"
            ),
        ),
        PriorityBoost(
            reason=PriorityBoostReason.AGING,
            amount=aging_amount,
            detail=f"waiting {age_hours:.1f}h (+{aging_amount})",
        ),
    ]
    reasons = [boost for boost in candidates if boost.amount != 0]
    total_boost = sum(boost.amount for boost in reasons)
    return base_priority + total_boost, reasons


def lock_owner_id(job_id: int) -> str:
    return f"job-{job_id}"


def parse_lock_owner_id(owner: str) -> int | None:
    """Inverse of ``lock_owner_id``; None for any non-matching owner string."""
    prefix = "job-"
    if not owner.startswith(prefix):
        return None
    suffix = owner[len(prefix) :]
    if not suffix.isdigit():
        return None
    return int(suffix)


@dataclass(frozen=True, slots=True)
class ProviderFailoverTransition:
    """Outcome of one atomic attempt to requeue a job after a confirmed provider limit.

    ``applied`` is False when the caller (job_id, owner, RUNNING, stage,
    source_provider) no longer matches the job's current row — a stale worker
    lost the claim/lease race to a newer assignment. The transition is then a
    safe no-op: no pause, no job mutation, no event. On success, the source
    provider's pause, the job's return to PENDING at the same stage with its
    lease/agent/provider cleared, and exactly one durable ``provider_failover``
    job_event all commit together. ``destination_provider`` starts unset here;
    a later claim fills it in once it selects a compatible alternate agent.
    """

    applied: bool
    job_id: int
    stage: "Stage"
    source_provider: str
    reset_at: float = 0.0
    alternate_available: bool = False
    event_id: int | None = None
    destination_provider: str | None = None


# Forward order used by the runner to advance a job.
STAGE_ORDER: list[Stage] = [
    Stage.QUEUED,
    Stage.PLAN,
    Stage.LINT,
    Stage.BUILD,
    Stage.TEST,
    Stage.REVIEW,
    Stage.SECURITY,
    Stage.DESIGN_REVIEW,
    Stage.MERGE,
    Stage.DEPLOY,
    Stage.DONE,
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class Usage:
    """Token + cost usage for a single agent run."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
    provider: str = ""

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_tokens
            + self.cache_read_tokens
        )

    @classmethod
    def from_result(
        cls, usage: dict | None, cost_usd: float | None, model: str, provider: str = "claude"
    ) -> "Usage":
        u = usage or {}
        return cls(
            input_tokens=int(u.get("input_tokens", 0) or 0),
            output_tokens=int(u.get("output_tokens", 0) or 0),
            cache_creation_tokens=int(u.get("cache_creation_input_tokens", 0) or 0),
            cache_read_tokens=int(u.get("cache_read_input_tokens", 0) or 0),
            cost_usd=float(cost_usd or 0.0),
            model=model,
            provider=provider,
        )

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            model=self.model or other.model,
            provider=self.provider or other.provider,
        )


@dataclass(slots=True)
class Project:
    id: int
    name: str
    repo_path: str
    description: str = ""
    status: str = "active"
    max_fix_attempts: int | None = None
    stack: str = ""
    spec: dict | None = None
    deploy_config: str = ""
    github_url: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @staticmethod
    def from_row(row) -> "Project":
        keys = row.keys() if hasattr(row, "keys") else row
        return Project(
            id=row["id"],
            name=row["name"],
            repo_path=row["repo_path"],
            description=row["description"] or "",
            status=row["status"],
            max_fix_attempts=int(row["max_fix_attempts"])
            if "max_fix_attempts" in keys and row["max_fix_attempts"] is not None
            else None,
            stack=row["stack"] or "" if "stack" in keys else "",
            spec=row["spec"] if "spec" in keys else None,
            deploy_config=row["deploy_config"] or "" if "deploy_config" in keys else "",
            github_url=row["github_url"] or "" if "github_url" in keys else "",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(slots=True)
class Epic:
    id: int
    project_id: int
    name: str
    description: str = ""
    status: str = "active"
    archived: bool = False
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @staticmethod
    def from_row(row) -> "Epic":
        keys = row.keys() if hasattr(row, "keys") else []
        return Epic(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            description=row["description"] or "",
            status=row["status"],
            archived=bool(row["archived"])
            if "archived" in keys and row["archived"] is not None
            else False,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(slots=True)
class Environment:
    id: int
    project_id: int
    name: str
    kind: str
    status: str = "active"
    config: str = ""
    host_id: int | None = None
    current_release_id: int | None = None
    auto_deploy: bool = False
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @staticmethod
    def from_row(row) -> "Environment":
        keys = row.keys() if hasattr(row, "keys") else []
        return Environment(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            kind=row["kind"],
            status=row["status"] if "status" in keys and row["status"] is not None else "active",
            config=row["config"] or "" if "config" in keys else "",
            host_id=row["host_id"] if "host_id" in keys else None,
            current_release_id=row["current_release_id"] if "current_release_id" in keys else None,
            auto_deploy=bool(row["auto_deploy"])
            if "auto_deploy" in keys and row["auto_deploy"] is not None
            else False,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(slots=True)
class Release:
    id: int
    project_id: int
    source_commit: str = ""
    image_ref: str = ""
    image_digest: str | None = None
    manifest: str | None = None
    signature_ref: str | None = None
    built_at: str = field(default_factory=_now)
    built_by: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @staticmethod
    def from_row(row) -> "Release":
        keys = row.keys() if hasattr(row, "keys") else []
        return Release(
            id=row["id"],
            project_id=row["project_id"],
            source_commit=row["source_commit"] or ""
            if "source_commit" in keys and row["source_commit"] is not None
            else "",
            image_ref=row["image_ref"] or ""
            if "image_ref" in keys and row["image_ref"] is not None
            else "",
            image_digest=row["image_digest"] if "image_digest" in keys else None,
            manifest=row["manifest"] if "manifest" in keys else None,
            signature_ref=row["signature_ref"] if "signature_ref" in keys else None,
            built_at=row["built_at"],
            built_by=row["built_by"] or ""
            if "built_by" in keys and row["built_by"] is not None
            else "",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(slots=True)
class Host:
    id: int
    name: str
    public_key: str = ""
    status: str = "active"
    last_seen: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @staticmethod
    def from_row(row) -> "Host":
        keys = row.keys() if hasattr(row, "keys") else []
        return Host(
            id=row["id"],
            name=row["name"],
            public_key=row["public_key"] or ""
            if "public_key" in keys and row["public_key"] is not None
            else "",
            status=row["status"] if "status" in keys and row["status"] is not None else "active",
            last_seen=row["last_seen"] or ""
            if "last_seen" in keys and row["last_seen"] is not None
            else "",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(slots=True)
class Promotion:
    id: int
    project_id: int
    release_id: int
    target_env_id: int
    kind: PromotionKind
    state: PromotionState
    source_env_id: int | None = None
    requested_by: str = ""
    requested_at: str = field(default_factory=_now)
    approved_by: str | None = None
    applied_by: str | None = None
    applied_at: str | None = None
    deploy_job_id: int | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @staticmethod
    def from_row(row) -> "Promotion":
        keys = row.keys() if hasattr(row, "keys") else []
        return Promotion(
            id=row["id"],
            project_id=row["project_id"],
            release_id=row["release_id"],
            target_env_id=row["target_env_id"],
            kind=PromotionKind(row["kind"]),
            state=PromotionState(row["state"]),
            source_env_id=row["source_env_id"] if "source_env_id" in keys else None,
            requested_by=row["requested_by"] or ""
            if "requested_by" in keys and row["requested_by"] is not None
            else "",
            requested_at=row["requested_at"],
            approved_by=row["approved_by"] if "approved_by" in keys else None,
            applied_by=row["applied_by"] if "applied_by" in keys else None,
            applied_at=row["applied_at"] if "applied_at" in keys else None,
            deploy_job_id=row["deploy_job_id"] if "deploy_job_id" in keys else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(slots=True)
class Webhook:
    id: int
    project_id: int
    url: str
    event_type: str
    created_by: str = ""
    active: bool = True
    created_at: str = field(default_factory=_now)
    kind: str = "http"

    @staticmethod
    def from_row(row) -> "Webhook":
        return Webhook(
            id=row["id"],
            project_id=row["project_id"],
            url=row["url"],
            event_type=row["event_type"],
            created_by=row["created_by"] or "",
            active=bool(row["active"]),
            created_at=row["created_at"],
            kind=row["kind"],
        )


@dataclass(slots=True)
class AgentSpec:
    """One entry in a project's agent roster: a provider + which tasks it may do.

    ``max_concurrency`` caps how many jobs this agent runs at once — that's the
    "how many of this agent" knob, enforced at claim time rather than by spawning
    processes.
    """

    id: int
    project_id: int
    name: str
    provider: str = "claude"
    model: str = ""  # "" => backend's default model
    allowed_tasks: list[str] = field(default_factory=lambda: list(ALL_TASKS))
    max_concurrency: int = 1
    enabled: bool = True
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @staticmethod
    def from_row(row) -> "AgentSpec":
        try:
            tasks = json.loads(row["allowed_tasks"]) if row["allowed_tasks"] else []
        except (TypeError, ValueError):
            tasks = []
        return AgentSpec(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            provider=row["provider"] or "claude",
            model=row["model"] or "",
            allowed_tasks=[str(t) for t in tasks],
            max_concurrency=int(row["max_concurrency"] or 1),
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(slots=True)
class User:
    id: int
    email: str
    display_name: str = ""
    is_platform_admin: bool = False
    is_active: bool = True
    oauth_provider: str = ""
    created_at: str = field(default_factory=_now)

    @staticmethod
    def from_row(row) -> "User":
        _ca = row["created_at"]
        created_at = (_ca.isoformat() if hasattr(_ca, "isoformat") else _ca) or ""
        keys = row.keys() if hasattr(row, "keys") else []
        return User(
            id=row["id"],
            email=row["email"],
            display_name=row["display_name"] or "",
            is_platform_admin=bool(row["is_platform_admin"]),
            is_active=bool(row["is_active"]),
            oauth_provider=row["oauth_provider"] if "oauth_provider" in keys else "",
            created_at=created_at,
        )


@dataclass(slots=True)
class ProjectMember:
    id: int
    user_id: str
    project_id: int
    role: str
    created_at: str

    @staticmethod
    def from_row(row) -> "ProjectMember":
        return ProjectMember(
            id=row["id"],
            user_id=row["user_id"],
            project_id=row["project_id"],
            role=row["role"],
            created_at=str(row["created_at"]),
        )


@dataclass(slots=True)
class EnvironmentMember:
    id: int
    user_id: str
    environment_id: int
    role: str
    created_at: str

    @staticmethod
    def from_row(row) -> "EnvironmentMember":
        return EnvironmentMember(
            id=row["id"],
            user_id=row["user_id"],
            environment_id=row["environment_id"],
            role=row["role"],
            created_at=str(row["created_at"]),
        )


@dataclass(slots=True)
class ResourceUsage:
    """Hardware resource record for one deterministic pipeline stage."""

    job_id: int
    stage: str
    attempt: int
    wall_seconds: float
    sampled_at: str
    id: int = 0
    cpu_seconds: float | None = None
    peak_rss_bytes: int | None = None
    io_read_bytes: int | None = None
    io_write_bytes: int | None = None
    net_bytes: int | None = None
    net_bytes_approx: bool = False
    disk_bytes: int | None = None

    @staticmethod
    def from_row(row) -> "ResourceUsage":
        sa = row["sampled_at"]
        keys = row.keys() if hasattr(row, "keys") else []
        return ResourceUsage(
            id=int(row["id"]),
            job_id=int(row["job_id"]),
            stage=row["stage"],
            attempt=int(row["attempt"]),
            wall_seconds=float(row["wall_seconds"]),
            sampled_at=sa.isoformat() if hasattr(sa, "isoformat") else str(sa),
            cpu_seconds=float(row["cpu_seconds"]) if row["cpu_seconds"] is not None else None,
            peak_rss_bytes=int(row["peak_rss_bytes"])
            if row["peak_rss_bytes"] is not None
            else None,
            io_read_bytes=int(row["io_read_bytes"]) if row["io_read_bytes"] is not None else None,
            io_write_bytes=int(row["io_write_bytes"])
            if row["io_write_bytes"] is not None
            else None,
            net_bytes=int(row["net_bytes"]) if row["net_bytes"] is not None else None,
            net_bytes_approx=bool(row["net_bytes_approx"])
            if "net_bytes_approx" in keys and row["net_bytes_approx"] is not None
            else False,
            disk_bytes=int(row["disk_bytes"]) if row["disk_bytes"] is not None else None,
        )


@dataclass(slots=True)
class PageView:
    id: int
    ts: str
    path: str
    ref: str
    visitor_hash: str
    country: str | None
    region: str | None
    city: str | None
    referrer_host: str | None
    ua_family: str | None
    os_family: str | None
    lang: str | None
    tz: str | None
    screen: str | None
    os_version: str | None
    arch: str | None
    platform: str | None
    langs: str | None
    win: str | None
    viewport: str | None
    color_scheme: str | None
    device_memory: str | None
    touch: str | None
    hour_cycle: str | None

    @staticmethod
    def from_row(row) -> "PageView":
        ts = row["ts"]
        return PageView(
            id=int(row["id"]),
            ts=ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            path=row["path"],
            ref=row["ref"],
            visitor_hash=row["visitor_hash"],
            country=row["country"],
            region=row["region"],
            city=row["city"],
            referrer_host=row["referrer_host"],
            ua_family=row["ua_family"],
            os_family=row["os_family"],
            lang=row["lang"],
            tz=row["tz"],
            screen=row["screen"],
            os_version=row["os_version"],
            arch=row["arch"],
            platform=row["platform"],
            langs=row["langs"],
            win=row["win"],
            viewport=row["viewport"],
            color_scheme=row["color_scheme"],
            device_memory=row["device_memory"],
            touch=row["touch"],
            hour_cycle=row["hour_cycle"],
        )


@dataclass(slots=True)
class PageViewSummary:
    totals: dict
    by_day: list[dict]
    by_country: list[dict]
    by_city: list[dict]
    by_ref: list[dict]
    by_referrer: list[dict]
    by_device: list[dict]
    recent: list[dict]
    by_path: list[dict] = field(default_factory=list)
    by_viewport: list[dict] = field(default_factory=list)
    by_color_scheme: list[dict] = field(default_factory=list)
    by_lang: list[dict] = field(default_factory=list)
    by_hour: list[dict] = field(default_factory=list)
    by_os_version: list[dict] = field(default_factory=list)
    pages_per_visitor: list[dict] = field(default_factory=list)
    returning: dict = field(default_factory=lambda: {"single_day": 0, "multi_day": 0})

    def to_dict(self) -> dict:
        return {
            "totals": self.totals,
            "by_day": self.by_day,
            "by_country": self.by_country,
            "by_city": self.by_city,
            "by_ref": self.by_ref,
            "by_referrer": self.by_referrer,
            "by_device": self.by_device,
            "recent": self.recent,
            "by_path": self.by_path,
            "by_viewport": self.by_viewport,
            "by_color_scheme": self.by_color_scheme,
            "by_lang": self.by_lang,
            "by_hour": self.by_hour,
            "by_os_version": self.by_os_version,
            "pages_per_visitor": self.pages_per_visitor,
            "returning": self.returning,
        }


@dataclass(slots=True)
class SiteStats:
    generated_at: str
    shipped_total: int
    shipped_last_7d: int
    shipped_last_24h: int
    minutes_since_last_ship: float | None
    avg_minutes_between_ships_last_7d: float | None
    first_job_at: str | None
    active_days: int | None
    humans: int
    projects: int
    gate_stops_total: int
    gate_stops_by_gate: dict[str, int]
    self_healed_shipped: int
    fix_rounds: int
    conflicts_resolved: int
    median_lead_minutes: float | None
    p25_lead_minutes: float | None
    off_hours_share: float | None
    cost_per_shipped_usd: float | None
    median_cost_shipped_usd: float | None
    running_now: int
    workers_online: int

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "shipped_total": self.shipped_total,
            "shipped_last_7d": self.shipped_last_7d,
            "shipped_last_24h": self.shipped_last_24h,
            "minutes_since_last_ship": self.minutes_since_last_ship,
            "avg_minutes_between_ships_last_7d": self.avg_minutes_between_ships_last_7d,
            "first_job_at": self.first_job_at,
            "active_days": self.active_days,
            "humans": self.humans,
            "projects": self.projects,
            "gate_stops_total": self.gate_stops_total,
            "gate_stops_by_gate": self.gate_stops_by_gate,
            "self_healed_shipped": self.self_healed_shipped,
            "fix_rounds": self.fix_rounds,
            "conflicts_resolved": self.conflicts_resolved,
            "median_lead_minutes": self.median_lead_minutes,
            "p25_lead_minutes": self.p25_lead_minutes,
            "off_hours_share": self.off_hours_share,
            "cost_per_shipped_usd": self.cost_per_shipped_usd,
            "median_cost_shipped_usd": self.median_cost_shipped_usd,
            "running_now": self.running_now,
            "workers_online": self.workers_online,
        }


@dataclass(slots=True)
class Job:
    id: int
    idea: str
    repo_path: str
    chat_id: int
    title: str = ""
    branch: str = ""
    stage: Stage = Stage.QUEUED
    status: JobStatus = JobStatus.PENDING
    plan: dict | None = None  # parsed plan JSON
    review: dict | None = None  # parsed review verdict JSON
    security_review: dict | None = None  # parsed security verdict JSON
    design_review: dict | None = None  # parsed design/UX review verdict JSON
    error: str = ""
    attempts: int = 0  # fix-and-retry rounds spent so far
    rebase_attempts: int = 0  # merge-conflict rebase retries (separate budget)
    rebase_retry_after: float | None = None  # unix ts; None = not in backoff
    timeout_attempts: int = 0  # stage-timeout retries (separate budget; caps the reclaim loop)
    plan_reask_attempts: int = 0  # PLAN-stage oversized-plan re-ask retries (separate budget)
    failure: str = ""  # last TEST/REVIEW failure, fed to the FIX stage
    project_id: int | None = None
    epic_id: int | None = None
    # --- multi-worker / multi-provider routing ---
    agent_id: int | None = None  # roster agent handling the current stage
    provider: str = ""  # that agent's provider (for the pause gate)
    owner: str = ""  # worker id that claimed it (host:pid:n)
    lease_until: float = 0.0  # unix ts; a stale lease means the worker died
    archived: bool = False
    needs_split: bool = False  # PLAN-stage job parked after re-ask still oversized; refile instead
    priority: int = 0
    deployed_commit: str | None = None  # git commit hash confirmed live after deploy
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    source: "JobSource" = JobSource.UNKNOWN
    source_actor: str = ""
    source_meta: dict | None = None
    resolution: str = ""
    implementation_summary: str | None = None
    merge_delta_sha: str | None = None  # pre-conflict HEAD sha; scopes the MERGE_VERIFY diff
    executing_step: str | None = None
    failed_step: str | None = None
    failure_code: str | None = None
    failure_origin: str | None = None
    retry_disposition: str | None = None
    failure_detail: dict | None = None
    idempotency_key: str | None = None

    @staticmethod
    def from_row(row) -> "Job":
        keys = row.keys()
        return Job(
            id=row["id"],
            idea=row["idea"],
            repo_path=row["repo_path"],
            chat_id=row["chat_id"],
            title=row["title"] if "title" in keys else "",
            branch=row["branch"] or "",
            stage=Stage(row["stage"]),
            status=JobStatus(row["status"]),
            plan=json.loads(row["plan"]) if row["plan"] else None,
            review=json.loads(row["review"]) if row["review"] else None,
            security_review=json.loads(row["security_review"])
            if "security_review" in keys and row["security_review"]
            else None,
            design_review=json.loads(row["design_review"])
            if "design_review" in keys and row["design_review"]
            else None,
            error=row["error"] or "",
            attempts=row["attempts"] if "attempts" in keys else 0,
            rebase_attempts=int(row["rebase_attempts"])
            if "rebase_attempts" in keys and row["rebase_attempts"] is not None
            else 0,
            rebase_retry_after=float(row["rebase_retry_after"])
            if "rebase_retry_after" in keys and row["rebase_retry_after"] is not None
            else None,
            timeout_attempts=int(row["timeout_attempts"])
            if "timeout_attempts" in keys and row["timeout_attempts"] is not None
            else 0,
            plan_reask_attempts=int(row["plan_reask_attempts"])
            if "plan_reask_attempts" in keys and row["plan_reask_attempts"] is not None
            else 0,
            failure=(row["failure"] if "failure" in keys else "") or "",
            project_id=row["project_id"] if "project_id" in keys else None,
            epic_id=row["epic_id"] if "epic_id" in keys else None,
            agent_id=(row["agent_id"] if "agent_id" in keys else None),
            provider=(row["provider"] if "provider" in keys else "") or "",
            owner=(row["owner"] if "owner" in keys else "") or "",
            lease_until=float(row["lease_until"])
            if "lease_until" in keys and row["lease_until"]
            else 0.0,
            archived=bool(row["archived"])
            if "archived" in keys and row["archived"] is not None
            else False,
            needs_split=bool(row["needs_split"])
            if "needs_split" in keys and row["needs_split"] is not None
            else False,
            priority=int(row["priority"])
            if "priority" in keys and row["priority"] is not None
            else 0,
            deployed_commit=row["deployed_commit"] if "deployed_commit" in keys else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            source=JobSource(row["source"])
            if "source" in keys and row["source"]
            else JobSource.UNKNOWN,
            source_actor=(row["source_actor"] if "source_actor" in keys else "") or "",
            source_meta=row["source_meta"] if "source_meta" in keys else None,
            resolution=(row["resolution"] if "resolution" in keys else "") or "",
            implementation_summary=row["implementation_summary"]
            if "implementation_summary" in keys
            else None,
            merge_delta_sha=row["merge_delta_sha"] if "merge_delta_sha" in keys else None,
            executing_step=row["executing_step"] if "executing_step" in keys else None,
            failed_step=row["failed_step"] if "failed_step" in keys else None,
            failure_code=row["failure_code"] if "failure_code" in keys else None,
            failure_origin=row["failure_origin"] if "failure_origin" in keys else None,
            retry_disposition=row["retry_disposition"] if "retry_disposition" in keys else None,
            failure_detail=row["failure_detail"] if "failure_detail" in keys else None,
            idempotency_key=row["idempotency_key"] if "idempotency_key" in keys else None,
        )


# The only fields in a gate's failure_detail payload that are a stable
# classification of *what* failed rather than volatile diagnostic text (raw
# command output, log excerpts) — see stages/__init__.py's
# compose_authorized_gate_failure_detail_if_valid, which every gate handler
# uses to set them.
_STABLE_FAILURE_DETAIL_KEYS = ("check_id", "event_id")


def failure_signature(
    failed_step: str | None,
    failure_code: str | None,
    failure_detail: Mapping[str, object] | None,
) -> tuple[object, ...]:
    """A stable, comparable identity for one recorded failure.

    Derived only from the structured classification already recorded for a
    fix-and-retry attempt (failed step, failure code, and the few stable
    identifier fields a gate is required to set) — never from volatile
    diagnostic text or retry bookkeeping (e.g. an ``attempt``/``attempts``
    counter), so two attempts that failed for the same underlying reason
    compare equal even though their raw output differs.
    """
    detail = failure_detail if isinstance(failure_detail, Mapping) else {}
    stable = tuple(detail.get(key) for key in _STABLE_FAILURE_DETAIL_KEYS)
    evidence = detail.get("authorized_gate_failure")
    evidence_key: tuple[object, ...] | None = None
    if isinstance(evidence, Mapping):
        evidence_key = (
            evidence.get("gate"),
            evidence.get("check_id"),
            evidence.get("event_id"),
            tuple(evidence.get("failing_paths") or ()),
            tuple(evidence.get("categories") or ()),
        )
    return (failed_step, failure_code, *stable, evidence_key)


def gate_finding_fingerprint(gate_result: Mapping[str, object] | None) -> list:
    """A JSON-safe identity for one gate's rendered verdict.

    Built from plain lists (not tuples) throughout because ``gate_result``
    round-trips through ``job.failure_detail`` as JSON via the store — tuples
    would compare unequal to their post-reload list form, silently breaking
    oscillation detection across a job's persisted history.
    """
    result = gate_result if isinstance(gate_result, Mapping) else {}
    findings = result.get("findings")
    findings = findings if isinstance(findings, list) else []
    finding_keys = [[f.get("severity"), f.get("note")] for f in findings if isinstance(f, Mapping)]
    return [result.get("summary") or "", finding_keys]


def gate_conflict_detected(
    gate_history: list | None, failed_step: str, current_signature: list
) -> bool:
    """True if the last four gate failures strictly alternate A,B,A,B.

    Compares the two most recent gate/fingerprint pairs against their
    predecessors two slots back, so a job whose REVIEW and SECURITY gates
    keep flip-flopping on the same two findings is recognized as stuck
    rather than making genuine progress.
    """
    entries = [*(list(e) for e in (gate_history or ())), [failed_step, current_signature]]
    if len(entries) < 4:
        return False
    a, b, c, d = entries[-4:]
    return a[0] != b[0] and a == c and b == d


@dataclass(slots=True)
class Invitation:
    id: int
    token: str
    email: str
    invited_by: str
    expires_at: float
    consumed_at: float | None
    created_at: str

    @staticmethod
    def from_row(row) -> "Invitation":
        return Invitation(
            id=row["id"],
            token=row["token"],
            email=row["email"],
            invited_by=row["invited_by"] or "",
            expires_at=float(row["expires_at"]),
            consumed_at=float(row["consumed_at"]) if row["consumed_at"] is not None else None,
            created_at=row["created_at"] or "",
        )


@dataclass(slots=True)
class ChatSession:
    id: int
    session_id: str
    project_id: int
    created_by: str
    messages: list[dict]
    proposed_jobs: list[dict]
    status: str
    created_at: str
    updated_at: str
    source_backlog_item_ids: list[int] = field(default_factory=list)

    @staticmethod
    def from_row(row) -> "ChatSession":
        messages = row["messages"]
        if isinstance(messages, str):
            messages = json.loads(messages)
        elif messages is None:
            messages = []

        proposed_jobs = row["proposed_jobs"]
        if isinstance(proposed_jobs, str):
            proposed_jobs = json.loads(proposed_jobs)
        elif proposed_jobs is None:
            proposed_jobs = []

        raw_ids = (
            row["source_backlog_item_ids"] if "source_backlog_item_ids" in row.keys() else None
        )
        if isinstance(raw_ids, str):
            source_ids = [int(x) for x in json.loads(raw_ids)]
        elif isinstance(raw_ids, list):
            source_ids = [int(x) for x in raw_ids]
        else:
            source_ids = []

        return ChatSession(
            id=row["id"],
            session_id=row["session_id"],
            project_id=row["project_id"],
            created_by=row["created_by"],
            messages=messages,
            proposed_jobs=proposed_jobs,
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            source_backlog_item_ids=source_ids,
        )


class BacklogItemType(str, enum.Enum):
    FEATURE = "feature"
    BUG = "bug"
    CHORE = "chore"
    IDEA = "idea"


class BacklogItemStatus(str, enum.Enum):
    NEW = "new"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    CONVERTED = "converted"
    COMPLETED = "completed"


class PromotionKind(str, enum.Enum):
    PROMOTE = "promote"  # our-env-initiated push to a target environment
    OFFER = "offer"  # client-initiated: made available, awaiting client apply


class PromotionState(str, enum.Enum):
    DISPATCHED = "dispatched"  # promote: enqueued, not yet deploying
    AVAILABLE = "available"  # offer: awaiting the client to apply it
    DEPLOYING = "deploying"
    DEPLOYED = "deployed"  # terminal
    DECLINED = "declined"  # terminal: client rejected an offer
    FAILED = "failed"  # terminal
    SUPERSEDED = "superseded"  # terminal: overtaken by a later promotion


@dataclass(slots=True)
class BacklogItem:
    id: int
    project_id: int
    title: str
    body: str
    type: BacklogItemType
    proposed_by: str
    status: BacklogItemStatus = BacklogItemStatus.NEW
    votes: int = 0
    epic_hint: str | None = None
    linked_job_ids: list[int] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @staticmethod
    def from_row(row) -> "BacklogItem":
        raw = row["linked_job_ids"]
        if raw is None:
            job_ids: list[int] = []
        elif isinstance(raw, list):
            job_ids = [int(x) for x in raw]
        else:
            job_ids = [int(x) for x in json.loads(raw)]
        return BacklogItem(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            body=row["body"] or "",
            type=BacklogItemType(row["type"]),
            proposed_by=row["proposed_by"] or "",
            status=BacklogItemStatus(row["status"]),
            votes=int(row["votes"]),
            epic_hint=row["epic_hint"] or None,
            linked_job_ids=job_ids,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(slots=True)
class ApiToken:
    """A project-scoped, least-privilege API token bound to one role.

    The plaintext secret is never persisted — only its sha256 hash (for lookup)
    and last4 (for display) live in the row.
    """

    id: int
    project_id: int
    name: str
    role: str
    last4: str
    created_by: str = ""
    created_at: str = field(default_factory=_now)
    last_used_at: str | None = None
    revoked_at: str | None = None

    @staticmethod
    def from_row(row) -> "ApiToken":
        return ApiToken(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            role=row["role"],
            last4=row["last4"],
            created_by=row["created_by"] or "",
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            revoked_at=row["revoked_at"],
        )
