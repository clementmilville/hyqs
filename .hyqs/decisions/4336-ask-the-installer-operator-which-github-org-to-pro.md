# Job #4336: Ask the installer operator which GitHub org to provision into

**Date:** 2026-09-10

This diff adds support for specifying a GitHub organization when provisioning project repositories during Hyqs installation. The `--github-org` flag allows operators to designate where new repos are created, with optional interactive prompting in terminal sessions and validation to ensure valid GitHub organization names. The installer also adds a preflight check for GitHub CLI authentication and documents that while authentication isn't required to install or run Hyqs, it's needed for project provisioning; warnings are emitted if `gh` is missing or unauthenticated, but installation proceeds regardless. The `.env` configuration now includes the `HYQS_GITHUB_ORG` variable, which persists across installer reruns and defaults to the authenticated user's personal account if omitted.
This diff adds support for specifying a GitHub organization when provisioning project repositories during Hyqs installation. The `--github-org` flag allows operators to designate where new repos are created, with optional interactive prompting in terminal sessions and validation to ensure valid GitHub organization names. The installer also adds a preflight check for GitHub CLI authentication and documents that while authentication isn't required to install or run Hyqs, it's needed for project provisioning; warnings are emitted if `gh` is missing or unauthenticated, but installation proceeds regardless. The `.env` configuration now includes the `HYQS_GITHUB_ORG` variable, which persists across installer reruns and defaults to the authenticated user's personal account if omitted. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- README.md
- deploy/install.sh
