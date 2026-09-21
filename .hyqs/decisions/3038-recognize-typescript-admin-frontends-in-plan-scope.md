# Job #3038: Recognize TypeScript admin frontends in PLAN scope completeness

**Date:** 2026-08-01

Extended the `_is_frontend_path` function to recognize `.js` and `.ts` files as frontend code, not just `.jsx` and `.tsx`. This fixes job #3037, where a plan with TypeScript API client code in `admin_ui/src/` wasn't recognized as having frontend coverage and failed validation. Added three test cases to verify the function now correctly identifies plain JavaScript and TypeScript files as frontend paths, and that plan validation accepts mixed backend/frontend changes in TypeScript.
Extended the `_is_frontend_path` function to recognize `.js` and `.ts` files as frontend code, not just `.jsx` and `.tsx`. This fixes job #3037, where a plan with TypeScript API client code in `admin_ui/src/` wasn't recognized as having frontend coverage and failed validation. Added three test cases to verify the function now correctly identifies plain JavaScript and TypeScript files as frontend paths, and that plan validation accepts mixed backend/frontend changes in TypeScript.

## Files touched
- hyqs/pipeline/collision.py
- tests/test_collision.py
