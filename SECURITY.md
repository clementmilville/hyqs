# Security Policy

## Reporting a vulnerability

Please report security issues privately through
[GitHub Security Advisories](../../security/advisories/new) rather than opening
a public issue. You should get an acknowledgement within a few days.

## Trust boundary

Hyqs runs AI agents that execute code: it clones repositories, runs test and
lint suites, builds containers, and writes to a database and a web console.
It is built for a **trusted operator running their own projects**.

It is *not* hardened as a multi-tenant service. In particular, a principal who
can create a project or queue a job can cause code from that project to run on
the host. Do not expose a Hyqs instance to untrusted users, and do not point it
at repositories you do not control.

## Operating it safely

- Keep `.env` out of version control (the shipped `.gitignore` already does this).
- Set `HYQS_NGINX_DOMAINS`; it is an allowlist and **fails closed when unset**.
- Set `HYQS_WEB_BASE_URL` so MCP OAuth does not fall back to a default host.
- Grant the `admin_console` permission deliberately — it reaches an orchestrator
  that can run shell commands as the service user.
- Put the web console behind TLS and an authenticating proxy.
