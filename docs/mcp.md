# Hyqs MCP server

Hyqs exposes its job queue over the [Model Context
Protocol](https://modelcontextprotocol.io) at `/mcp` on the web service, so an
agent client (Claude Code, Claude Desktop, or your own) can file jobs, watch
them run, and read their diffs without going through the console.

The tools mirror the platform's HTTP API and enforce the **same
project-membership and permission checks** as the equivalent REST routes. There
is no privileged back door here: a caller sees exactly what it would see in the
web UI.

## Connecting

The endpoint is `<HYQS_WEB_BASE_URL>/mcp`. Set `HYQS_WEB_BASE_URL` in `.env` —
it is also what the server advertises as its MCP resource URL, and leaving it
unset means OAuth clients get a wrong or missing discovery document.

With Claude Code:

```bash
claude mcp add --transport http hyqs https://hyqs.example.com/mcp
```

## Authentication

Every request needs a bearer token:

```
Authorization: Bearer <token>
```

Two kinds are accepted, and a request without a valid one is rejected with HTTP
401 before any tool runs.

**Project-scoped API tokens** — for automation. Mint one in the console under
*Settings → API tokens*. The token is bound to a single project and a single
role at creation time, and that binding is enforced server-side on every call:
it cannot reach another project, and **it can never hold platform-admin
rights**, no matter who created it. This is the right choice for scripts, CI,
and agent clients.

**Google OIDC** — for interactive human clients. The server composes a
`OAuthProxy` in front of Google that adds Dynamic Client Registration (RFC 7591)
and Protected Resource Metadata (RFC 9728), which Google does not provide
itself, so standards-compliant MCP clients can discover and register
automatically. Requires `HYQS_GOOGLE_CLIENT_ID` / `HYQS_GOOGLE_CLIENT_SECRET`,
with `<HYQS_WEB_BASE_URL>/mcp/auth/callback` registered as an authorized
redirect URI on a **Web application** OAuth client.

Both are tried in turn: Google first, falling back to the API-token verifier
when the bearer token is not a valid Google-issued one. If no public URL is
configured, the server degrades to API-token-only auth rather than running
unauthenticated.

Project-scoped tools reject non-members with an error string beginning
`"error: forbidden"`.

## Tools

### Reading projects and jobs

| Tool | Parameters | Description |
|---|---|---|
| `list_projects` | — | Projects accessible to the caller. |
| `get_project_spec` | `project_id` | A project's config and agent roster. |
| `list_jobs` | `project_id`, `status="active"`, `summary=False`, `cursor=None` | Jobs for a project. `status`: `active`, `pending`, `running`, `deploying`, `done`, `failed`, `cancelled`, `archived`, `all`. Pass the last returned job id as `cursor` for the next older page; `summary=True` returns a reduced projection. |
| `get_job` | `job_id`, `summary=False` | A single job. |
| `job_events` | `job_id` | Stage event timeline. |
| `job_supervisor_events` | `job_id` | Supervisor remediation timeline — janitor classify / requeue / gate-fix / deploy-fix actions. |
| `job_diff` | `job_id` | Git diff of the job's branch against the default branch; empty string if it has no branch yet. |
| `watch_job` | `job_id`, `timeout_seconds=120` | Streams each stage transition as an MCP notification, then returns the final job and full event list once it reaches a terminal status or the timeout elapses. Use this instead of polling `get_job`. |
| `list_epics` | `project_id` | Epics for a project (excludes archived). |

### Creating and steering work

| Tool | Parameters | Description |
|---|---|---|
| `create_job` | `idea`, `project_id`, `title=""`, `epic_id=None`, `depends_on=None`, `fixes_job_id=None`, `allow_parallel_remediation=False`, `idempotency_key=""` | Create a job. **Prefer several small jobs over one large one** — big jobs tend to exhaust the fix-attempt budget and land as dangling commits, and review is scoped to a job's diff. Chain jobs touching the same files with `depends_on` so they run sequentially instead of contending. |
| `create_job_wave` | `project_id`, `jobs`, `epic_id=None` | Atomically create a wave of interdependent jobs. Each entry is `{title, idea, depends_on, target_files, priority, epic_id, idempotency_key}` (only `title` and `idea` required); `depends_on` holds 0-based indexes into the same wave. |
| `survey_job_queue` | `project_id`, `candidates` | Check candidate jobs' target files for path collisions against the project's active queue *before* creating them. File-path collisions only — not title similarity. |
| `update_job` | `job_id`, `title=None`, `idea=None`, `priority=None`, `epic_id=None` | Atomically update a job. Omitted parameters are left unchanged. |
| `cancel_job` | `job_id` | Cancel a pending or running job. |
| `retry_job` | `job_id`, `force=False` | Retry a `FAILED`/`CANCELLED` job from `PLAN`. `force=True` bypasses the PLAN scope gate when you've confirmed the size is legitimate. |
| `requeue_job` | `job_id`, `stage` | Requeue a `FAILED` job from a specific stage (`queued`, `lint`, `build`, `test`, `review`, `security`, `deploy`). Needs `resolve_job`. |
| `resolve_job` | `job_id`, `resolution="resolved"` | Mark a `FAILED`/`CANCELLED` job manually resolved. Needs `resolve_job`. |
| `fix_forward_job` | `job_id`, `idea`, `title=""`, `repoint_dependent_ids=None`, `override_active_remediation=False` | File a remediation job linked to `job_id`. Rejected if one is already active unless overridden. Needs `resolve_job`. |
| `archive_job` / `unarchive_job` | `job_id` | Archive (tearing down git/GitHub artifacts if terminal) or restore a job. |
| `add_job_dependency` / `remove_job_dependency` | `job_id`, `depends_on_job_id` | Edit the dependency graph. Needs `edit_job_deps`. |
| `get_job_dependency_graph` | `job_id` | The full graph, including completed upstreams. |
| `add_backlog_item` | `project_id`, `title`, `body=""`, `type="idea"`, `epic_hint=None` | Add to the backlog. Needs `propose_backlog`. |
| `create_epic` | `project_id`, `name`, `description=""` | Create an epic. |
| `update_epic` | `epic_id`, `name=None`, `description=None`, `status=None`, `archived=None` | Update an epic. `status`: `active`, `paused`, `done`, `archived`. |

### Analytics

| Tool | Parameters | Description |
|---|---|---|
| `project_performance` | `project_id`, `include_operational=False` | Headline and per-stage stats. Auto-deploy jobs excluded unless requested. |
| `list_agents` | `project_id` | The project's agent roster. |
| `agent_stats` | `project_id`, `include_operational=False` | Cost, average duration, and fix-rate per agent. |

### Configuration and delivery

| Tool | Parameters | Description |
|---|---|---|
| `update_project` | `project_id`, `name`, `description`, `status`, `stack`, `deploy_config`, `github_url`, `max_fix_attempts` (all optional) | Update project config. Needs `edit_project`. |
| `resync_nginx_vhost` | `project_id` | Re-apply the nginx vhost from the current `deploy_config`. Idempotent. Needs `edit_project`. |
| `register_webhook` | `project_id`, `url`, `event_type`, `kind="http"` | Subscribe to `job_complete`, `deploy`, or `needs_attention`. `kind` is `http` (an http(s) URL) or `slack` (a channel). Needs `manage_webhooks`. |
| `list_webhooks` | `project_id` | List a project's webhooks. |
| `set_webhook_active` | `webhook_id`, `active` | Enable or disable one. Needs `manage_webhooks`. |
| `delete_webhook` | `webhook_id` | Delete one. Needs `manage_webhooks`. |
| `set_slack_credential` | `project_id`, `bot_token` | Store the Slack bot token for a project's notifications. Encrypted at rest and never echoed back. Needs `manage_webhooks`. |
| `promote_release` | `project_id`, `release_id`, `target_env_id` | Promote a release to an environment, dispatching a DEPLOY job. Needs `deploy.promote`. |
| `offer_release` | `project_id`, `release_id`, `target_env_id` | Offer a release for a client environment's own approver to apply — enqueues no job; they run `hyqs-pipeline apply` on their host. Needs `deploy.offer`. |
