# Job #4099: Fold activation into implementation_summary and auto-file the follow-up job

**Date:** 2026-08-22

The diff adds activation guidance support to the merge pipeline stage. When a job is successfully merged, the code now extracts activation instructions from the plan (whether a config change is required, where to apply it, and what effect to expect), appends this to the implementation summary, and conditionally files a followup job if manual activation is needed. This ensures operators know what configuration changes are required to activate merged features in production.
The diff adds activation guidance support to the merge pipeline stage. When a job is successfully merged, the code now extracts activation instructions from the plan (whether a config change is required, where to apply it, and what effect to expect), appends this to the implementation summary, and conditionally files a followup job if manual activation is needed. This ensures operators know what configuration changes are required to activate merged features in production.

## Files touched
- hyqs/pipeline/stages/merge.py
