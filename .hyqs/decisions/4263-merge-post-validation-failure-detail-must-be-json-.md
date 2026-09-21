# Job #4263: Merge post-validation failure_detail must be JSON-serialisable (strip ResourceRecord)

**Date:** 2026-09-07

This diff fixes a serialization crash that occurs when merge validation test results include resource usage metrics (ResourceRecord dataclass). The merge stage now extracts non-JSON-serializable objects like resource records, records them separately via `rn._record_resource()`, and stores only safe fields in the failure detail. The store layer adds a defensive JSON encoder fallback that converts any remaining dataclasses or unserializable values to JSON-safe representations, preventing the pipeline from crashing if such objects reach the database. Tests verify both the extraction logic at merge time and the fallback serialization behavior in the event store.
This diff fixes a serialization crash that occurs when merge validation test results include resource usage metrics (ResourceRecord dataclass). The merge stage now extracts non-JSON-serializable objects like resource records, records them separately via `rn._record_resource()`, and stores only safe fields in the failure detail. The store layer adds a defensive JSON encoder fallback that converts any remaining dataclasses or unserializable values to JSON-safe representations, preventing the pipeline from crashing if such objects reach the database. Tests verify both the extraction logic at merge time and the fallback serialization behavior in the event store. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/pipeline/stages/merge.py
- hyqs/pipeline/store.py
- tests/test_stages_merge_post_merge_validation.py
- tests/test_store.py
