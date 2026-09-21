# Job #3971: Keep pipeline-internal gate_history bookkeeping out of the fixer's prompt

**Date:** 2026-08-17

The diff removes the `gate_history` field from failure details before passing them to the fix agent, filtering it out in the fix stage of the pipeline. Test updates confirm that `gate_history` is now being added to the job's failure_detail structure earlier in the pipeline, but it's stripped away when routing to the fix agent to keep the context focused on relevant debugging information. This prevents potentially large or irrelevant gate history data from cluttering the fix agent's input.
The diff removes the `gate_history` field from failure details before passing them to the fix agent, filtering it out in the fix stage of the pipeline. Test updates confirm that `gate_history` is now being added to the job's failure_detail structure earlier in the pipeline, but it's stripped away when routing to the fix agent to keep the context focused on relevant debugging information. This prevents potentially large or irrelevant gate history data from cluttering the fix agent's input.

## Files touched
- hyqs/pipeline/stages/fix.py
- tests/test_pipeline_loop_fixes.py
