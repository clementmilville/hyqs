# Job #3068: Exclude a remediation's own incident job from its queue-survey dependencies

**Date:** 2026-08-01

The change prevents remediation jobs from creating dependencies on jobs in their own remediation lineage by excluding the current job and its root ancestor from the queue dependency calculation. When analyzing which active jobs might conflict with a fix, the code now tracks `lineage_excluded_job_ids` separately and subtracts them from the set of overlapping and unknown-scope jobs before determining final dependencies. The test updates verify this logic by confirming that jobs belonging to the lineage (100 and 543) are correctly filtered out of `depends_on_job_ids` even when they appear in the overlap or unknown-scope lists.
The change prevents remediation jobs from creating dependencies on jobs in their own remediation lineage by excluding the current job and its root ancestor from the queue dependency calculation. When analyzing which active jobs might conflict with a fix, the code now tracks `lineage_excluded_job_ids` separately and subtracts them from the set of overlapping and unknown-scope jobs before determining final dependencies. The test updates verify this logic by confirming that jobs belonging to the lineage (100 and 543) are correctly filtered out of `depends_on_job_ids` even when they appear in the overlap or unknown-scope lists.

## Files touched
- hyqs/pipeline/supervisor.py
- tests/test_auto_requeue_after_fix.py
