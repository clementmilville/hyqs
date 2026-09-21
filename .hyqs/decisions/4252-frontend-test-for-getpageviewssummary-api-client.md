# Job #4252: Frontend test for getPageViewsSummary API client

**Date:** 2026-09-06

This diff converts a single parameter-filtering test into a parameterized test covering three scenarios: full parameters, partial parameters with an empty string, and no parameters at all. It also renames the test to more accurately reflect the behavior (omitting "absent or empty values" rather than just "falsy values"), and fixes an adjacent assertion to use `.toEqual(new Error(...))` instead of `.toThrow()` for more precise error matching.
This diff converts a single parameter-filtering test into a parameterized test covering three scenarios: full parameters, partial parameters with an empty string, and no parameters at all. It also renames the test to more accurately reflect the behavior (omitting "absent or empty values" rather than just "falsy values"), and fixes an adjacent assertion to use `.toEqual(new Error(...))` instead of `.toThrow()` for more precise error matching. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/web/frontend/src/api.test.js
