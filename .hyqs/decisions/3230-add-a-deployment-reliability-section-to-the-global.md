# Job #3230: Add a Deployment Reliability section to the global Admin Usage & Cost page

**Date:** 2026-08-02

Adds a new `DeploymentReliability` component to the admin dashboard that displays deployment reliability metrics grouped by project, including totals, completion rates, failure counts, and failure percentages. The component supports period filtering, error handling with retry capability, and displays an empty state when no deployment data is available. Includes comprehensive tests verifying the table rendering with calculated percentages and the empty state behavior.
Adds a new `DeploymentReliability` component to the admin dashboard that displays deployment reliability metrics grouped by project, including totals, completion rates, failure counts, and failure percentages. The component supports period filtering, error handling with retry capability, and displays an empty state when no deployment data is available. Includes comprehensive tests verifying the table rendering with calculated percentages and the empty state behavior.

## Files touched
- hyqs/web/frontend/src/views/AdminUsageCost.jsx
- hyqs/web/frontend/src/views/AdminUsageCost.test.jsx
