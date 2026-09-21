# Job #3049: Skip the web rebuild and swap when a deploy changes no runtime code

**Date:** 2026-08-01

This diff adds a release-optimization feature that skips expensive frontend builds and web deployments when a release only modifies documentation, tests, or decision files. The script tracks the previous successful release commit in a marker file and compares against the current commit's changed paths to determine whether a full frontend/web swap is necessary. If all changes are restricted to `docs/`, `tests/`, or `.hyqs/decisions/`, the release proceeds with only the pipeline step; otherwise it runs the full build and deployment. The marker file is updated only after all release steps complete successfully, ensuring safe fail-over to a full swap if any step fails.
This diff adds a release-optimization feature that skips expensive frontend builds and web deployments when a release only modifies documentation, tests, or decision files. The script tracks the previous successful release commit in a marker file and compares against the current commit's changed paths to determine whether a full frontend/web swap is necessary. If all changes are restricted to `docs/`, `tests/`, or `.hyqs/decisions/`, the release proceeds with only the pipeline step; otherwise it runs the full build and deployment. The marker file is updated only after all release steps complete successfully, ensuring safe fail-over to a full swap if any step fails.

## Files touched
- deploy/release.sh
- tests/test_release_sh.py
