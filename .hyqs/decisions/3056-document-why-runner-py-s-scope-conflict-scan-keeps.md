# Job #3056: Document why runner.py's scope-conflict scan keeps using list_active

**Date:** 2026-08-01

This diff adds a clarifying comment to the job listing loop that fetches active manifests for a project. The comment explains that the code must paginate through all results to completion rather than stopping early, because a downstream scope-conflict check requires the complete set of active jobs to function correctly. No actual behavior changes—it's purely a documentation update to prevent future confusion about why the pagination loop doesn't short-circuit.
This diff adds a clarifying comment to the job listing loop that fetches active manifests for a project. The comment explains that the code must paginate through all results to completion rather than stopping early, because a downstream scope-conflict check requires the complete set of active jobs to function correctly. No actual behavior changes—it's purely a documentation update to prevent future confusion about why the pagination loop doesn't short-circuit.

## Files touched
- hyqs/pipeline/runner.py
