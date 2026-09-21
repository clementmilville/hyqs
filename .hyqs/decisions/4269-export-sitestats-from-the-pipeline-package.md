# Job #4269: Export SiteStats from the pipeline package

**Date:** 2026-09-07

This diff exports `SiteStats` as part of the public API of the `hyqs.pipeline` module by adding it to both the import statement from `.models` and the `__all__` list. The change makes the `SiteStats` class publicly accessible to consumers of the pipeline package. No logic is modified—this is purely an API visibility change.
This diff exports `SiteStats` as part of the public API of the `hyqs.pipeline` module by adding it to both the import statement from `.models` and the `__all__` list. The change makes the `SiteStats` class publicly accessible to consumers of the pipeline package. No logic is modified—this is purely an API visibility change. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/pipeline/__init__.py
