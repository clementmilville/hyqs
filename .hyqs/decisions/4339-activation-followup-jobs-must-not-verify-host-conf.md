# Job #4339: Activation-followup jobs must not verify host config from inside a worktree

**Date:** 2026-09-21

This diff introduces host-configuration verification for activation-followup jobs, which verify that deployed changes (typically gitignored `.env` variables) were actually switched on live. The new `activation` module provides pure functions to extract `HYQS_*` environment variable names from job metadata and check their values against the host's resolved environment. The `PipelineRunner` now detects activation-followup jobs and routes them to a specialized `_verify_activation_followup()` method that checks `os.environ` directly instead of attempting the normal worktree checkout path, which would permanently fail since gitignored files structurally cannot appear in the isolated worktree. This fixes job #4337, which was failing terminally because the old code asked an AI reviewer to find a file that could never be there.
This diff introduces host-configuration verification for activation-followup jobs, which verify that deployed changes (typically gitignored `.env` variables) were actually switched on live. The new `activation` module provides pure functions to extract `HYQS_*` environment variable names from job metadata and check their values against the host's resolved environment. The `PipelineRunner` now detects activation-followup jobs and routes them to a specialized `_verify_activation_followup()` method that checks `os.environ` directly instead of attempting the normal worktree checkout path, which would permanently fail since gitignored files structurally cannot appear in the isolated worktree. This fixes job #4337, which was failing terminally because the old code asked an AI reviewer to find a file that could never be there. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/pipeline/activation.py
- hyqs/pipeline/runner.py
- tests/test_activation.py
- tests/test_pipeline_activation_followup_verify.py
