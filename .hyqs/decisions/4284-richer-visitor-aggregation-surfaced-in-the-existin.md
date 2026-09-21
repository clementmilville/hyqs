# Job #4284: Richer visitor aggregation, surfaced in the existing Page views section

**Date:** 2026-09-07

This diff adds eight new analytics dimensions to the admin page-views dashboard: breakdowns by path, viewport, color scheme, language, hour-of-day, OS version, pages-per-visitor depth, and returning visitor counts (single-day vs multi-day). The backend queries aggregate these dimensions from the page_views table with appropriate grouping and filtering, while the frontend renders them as tables and charts, with OS versions hidden behind a toggle. The new fields include default factories to maintain backward compatibility with older API responses, and comprehensive tests verify both the new aggregation logic and graceful degradation when older clients lack these fields.
This diff adds eight new analytics dimensions to the admin page-views dashboard: breakdowns by path, viewport, color scheme, language, hour-of-day, OS version, pages-per-visitor depth, and returning visitor counts (single-day vs multi-day). The backend queries aggregate these dimensions from the page_views table with appropriate grouping and filtering, while the frontend renders them as tables and charts, with OS versions hidden behind a toggle. The new fields include default factories to maintain backward compatibility with older API responses, and comprehensive tests verify both the new aggregation logic and graceful degradation when older clients lack these fields. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/pipeline/models.py
- hyqs/pipeline/store.py
- hyqs/web/frontend/src/views/AdminUsageCost.jsx
- hyqs/web/frontend/src/views/AdminUsageCost.test.jsx
- tests/test_models_page_view_summary.py
- tests/test_page_views_admin.py
- tests/test_store.py
