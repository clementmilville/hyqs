"""Domain specialist checklists spliced into REVIEW/SECURITY/DESIGN_REVIEW prompts.

Selected deterministically by :func:`hyqs.pipeline.classify.classify_diff` — additive
prompt content only. The base reviewer mandate and output contract are unchanged.
"""

from __future__ import annotations

_DB_PERSONA = """\
### DB Specialist Checklist
- Is any migration step destructive or irreversible (DROP COLUMN/TABLE, data-losing \
type change) without an explicit backfill/rollback plan?
- Does a new foreign key or hot-query filter/join column lack a supporting index?
- Does the diff introduce an N+1 query pattern (a query inside a loop over rows)?
- Are transaction boundaries correct — is a multi-step write wrapped so a partial \
failure can't leave inconsistent state?
- If this is a live app, is a data backfill safe to run against production traffic \
(chunked, idempotent, non-locking)?"""

_FRONTEND_PERSONA = """\
### Frontend Specialist Checklist
Cite specific violations with line refs against this project's CONVENTIONS.md design \
contract:
- Are colors/spacing/typography drawn from design tokens (tokens.css) rather than \
hard-coded values?
- Does the diff reuse shared components instead of duplicating markup/styles?
- Are there inline styles where a token or shared class should be used instead?
- Does the layout work at 375px width with no horizontal scroll?
- Are interactive targets at least 44px?"""

_AUTHZ_PERSONA = """\
### Authz Specialist Checklist
Security boundaries must be EXERCISED, not diff-read: when feasible, attempt the \
bypass in the sandbox (call the endpoint/flow as an unauthorized or under-privileged \
actor) rather than reasoning about it from the diff alone. Fail closed on any doubt \
about whether an authorization or authentication check actually holds."""

_INFRA_PERSONA = """\
### Infra Specialist Checklist
Shared-host compose contract: the web service must bind to $PORT, databases must stay \
internal (not exposed on host ports), and the project's docker-compose must not leak \
the host's os.environ into the container. No secrets committed to the repo."""

_PERSONAS_BY_DOMAIN = {
    "db": _DB_PERSONA,
    "frontend": _FRONTEND_PERSONA,
    "authz": _AUTHZ_PERSONA,
    "infra": _INFRA_PERSONA,
}

_DOMAIN_ORDER = ("db", "frontend", "authz", "infra")


def build_checklist(domains: set[str]) -> str:
    """Join the persona snippets for ``domains``, in a fixed deterministic order."""
    snippets = [_PERSONAS_BY_DOMAIN[d] for d in _DOMAIN_ORDER if d in domains]
    return "\n\n".join(snippets)
