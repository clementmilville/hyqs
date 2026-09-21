# Job #4332: Expose intake spec completeness on the session API

**Date:** 2026-09-10

The change adds a `missing_required` field to serialized intake sessions, computed from `check_completeness()` to indicate which required draft-spec fields are still outstanding. Previously this completeness check was computed but discarded in responses; now `intake_session_to_dict()` includes it so all endpoints that serialize sessions (create, list, get, message, patch) expose this information automatically. The diff adds comprehensive regression tests covering all five affected routes to verify the field appears correctly in both incomplete and complete specs.
The change adds a `missing_required` field to serialized intake sessions, computed from `check_completeness()` to indicate which required draft-spec fields are still outstanding. Previously this completeness check was computed but discarded in responses; now `intake_session_to_dict()` includes it so all endpoints that serialize sessions (create, list, get, message, patch) expose this information automatically. The diff adds comprehensive regression tests covering all five affected routes to verify the field appears correctly in both incomplete and complete specs. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/web/app.py
- tests/test_intake_session_endpoints.py
