# Job #4251: Frontend tests for PageViewsSection

**Date:** 2026-09-06

This diff refactors a test's async handling to use React Testing Library's recommended `act` pattern. It imports the `act` utility, replaces three `waitFor` wrapper calls with `await act(async () => {})` blocks, and moves the assertions outside the wrapped calls, making them direct expectations rather than conditional assertions. The fake timer setup is simplified by removing the `shouldAdvanceTime: true` option. These changes align the test with current best practices for handling React state updates in tests.
This diff refactors a test's async handling to use React Testing Library's recommended `act` pattern. It imports the `act` utility, replaces three `waitFor` wrapper calls with `await act(async () => {})` blocks, and moves the assertions outside the wrapped calls, making them direct expectations rather than conditional assertions. The fake timer setup is simplified by removing the `shouldAdvanceTime: true` option. These changes align the test with current best practices for handling React state updates in tests. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/web/frontend/src/views/AdminUsageCost.test.jsx
