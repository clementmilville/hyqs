# Job #4335: Let project provisioning create GitHub repos in an organization

**Date:** 2026-09-10

This diff adds support for provisioning new projects under a specified GitHub organization instead of the authenticated user's personal namespace. It introduces a new `HYQS_GITHUB_ORG` config variable that, when set, routes repo creation to `gh repo create org/slug` rather than just `slug`. The org name is validated against GitHub's naming rules (alphanumeric with hyphens) before any subprocess invocation, and the feature defaults to empty (preserving today's behavior). Tests confirm the bare-slug and org/slug paths work correctly and that invalid org names are rejected early.
This diff adds support for provisioning new projects under a specified GitHub organization instead of the authenticated user's personal namespace. It introduces a new `HYQS_GITHUB_ORG` config variable that, when set, routes repo creation to `gh repo create org/slug` rather than just `slug`. The org name is validated against GitHub's naming rules (alphanumeric with hyphens) before any subprocess invocation, and the feature defaults to empty (preserving today's behavior). Tests confirm the bare-slug and org/slug paths work correctly and that invalid org names are rejected early. Activation: update .env — set HYQS_GITHUB_ORG (read by hyqs.config.Config.from_env); expected live effect: Once HYQS_GITHUB_ORG is set to a valid GitHub org login and the process restarts, newly provisioned projects' `gh repo create` call targets `<org>/<slug>` instead of the authenticated user's personal account. Leaving it empty (the default) preserves today's personal-account behavior for every existing install..

## Files touched
- .env.example
- hyqs/config.py
- hyqs/pipeline/provision.py
- tests/test_config.py
- tests/test_provision.py
