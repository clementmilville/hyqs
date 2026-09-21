# Job #2912: Recognize explicit no-change path declarations

**Date:** 2026-07-31

This diff adds support for detecting and handling "no-change" declarations in incident narratives. The new `_no_change_declared_paths()` function parses text to find paths explicitly marked as "byte-for-byte unchanged" or similar phrases—either under a heading containing that marker or inline on the same line as a path name. These paths are then merged into the reference-paths set during planning-candidate generation, so they register as advisory references (for scope tracking) without being treated as target files requiring changes. The two new tests verify that paths declared unchanged under a section heading or inline stay classified as references, not targets, even when mentioned in the narrative.
This diff adds support for detecting and handling "no-change" declarations in incident narratives. The new `_no_change_declared_paths()` function parses text to find paths explicitly marked as "byte-for-byte unchanged" or similar phrases—either under a heading containing that marker or inline on the same line as a path name. These paths are then merged into the reference-paths set during planning-candidate generation, so they register as advisory references (for scope tracking) without being treated as target files requiring changes. The two new tests verify that paths declared unchanged under a section heading or inline stay classified as references, not targets, even when mentioned in the narrative.

## Files touched
- hyqs/pipeline/collision.py
- tests/test_collision.py
