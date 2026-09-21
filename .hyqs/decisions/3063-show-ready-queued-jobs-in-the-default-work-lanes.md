# Job #3063: Show ready queued jobs in the default Work lanes

**Date:** 2026-08-01

A new "Queued" work lane now displays pending jobs that have no dependencies, making ready jobs visible by default instead of requiring users to navigate elsewhere. The lane is positioned between "Running Now" and "Recently Finished" per convention, and includes a "next action" column for additional context. The implementation updates the empty-state logic to account for queued jobs and adds test coverage verifying the distinction between queued (unblocked) and blocked (dependency-waiting) jobs.
A new "Queued" work lane now displays pending jobs that have no dependencies, making ready jobs visible by default instead of requiring users to navigate elsewhere. The lane is positioned between "Running Now" and "Recently Finished" per convention, and includes a "next action" column for additional context. The implementation updates the empty-state logic to account for queued jobs and adds test coverage verifying the distinction between queued (unblocked) and blocked (dependency-waiting) jobs.

## Files touched
- hyqs/web/frontend/src/components/WorkLanes.jsx
- hyqs/web/frontend/src/components/WorkLanes.test.jsx
