# Job #4308: Stop leaking the pipeline environment into build and test subprocesses

**Date:** 2026-09-08

This diff extracts a shared subprocess environment allowlist (`minimal_subprocess_env()`) in `resources.py` to prevent pipeline worker secrets (Anthropic credentials, database URLs, etc.) from leaking into untrusted, job-controlled subprocesses like docker build, pytest, npm, and ruff. The allowlist is applied across docker_deploy, linting, and testing modules, and npm lifecycle scripts are now disabled by default with an opt-in marker that must already exist on the base branch to prevent a malicious diff from bypassing the protection. Comprehensive tests verify secret isolation works correctly across all subprocess callers, and a minor fix addresses test flakiness in page view fingerprint assertions.
This diff extracts a shared subprocess environment allowlist (`minimal_subprocess_env()`) in `resources.py` to prevent pipeline worker secrets (Anthropic credentials, database URLs, etc.) from leaking into untrusted, job-controlled subprocesses like docker build, pytest, npm, and ruff. The allowlist is applied across docker_deploy, linting, and testing modules, and npm lifecycle scripts are now disabled by default with an opt-in marker that must already exist on the base branch to prevent a malicious diff from bypassing the protection. Comprehensive tests verify secret isolation works correctly across all subprocess callers, and a minor fix addresses test flakiness in page view fingerprint assertions. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/pipeline/docker_deploy.py
- hyqs/pipeline/linting.py
- hyqs/pipeline/resources.py
- hyqs/pipeline/testing.py
- tests/test_docker_deploy.py
- tests/test_linting.py
- tests/test_page_views.py
- tests/test_resources.py
- tests/test_testing.py
- tests/test_worktree_isolation.py
