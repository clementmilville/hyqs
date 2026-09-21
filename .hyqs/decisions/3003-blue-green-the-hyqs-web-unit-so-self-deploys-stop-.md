# Job #3003: Blue-green the hyqs-web unit so self-deploys stop dropping requests

**Date:** 2026-08-01

This diff implements blue-green deployment for the web console to eliminate downtime during releases. The web process now binds its socket with `SO_REUSEPORT`, allowing a new generation to start and bind the same port alongside the currently running one while the old instance drains gracefully. During deployment, `release.sh` starts the new web process, polls its `/api/health` endpoint (which now returns the process PID) to confirm it's serving, and only then stops the old instance—the port never goes silent. A new systemd template service and comprehensive tests ensure the transition works correctly, with a fallback to bare restart if the old process predates this change and doesn't support port sharing.
This diff implements blue-green deployment for the web console to eliminate downtime during releases. The web process now binds its socket with `SO_REUSEPORT`, allowing a new generation to start and bind the same port alongside the currently running one while the old instance drains gracefully. During deployment, `release.sh` starts the new web process, polls its `/api/health` endpoint (which now returns the process PID) to confirm it's serving, and only then stops the old instance—the port never goes silent. A new systemd template service and comprehensive tests ensure the transition works correctly, with a fallback to bare restart if the old process predates this change and doesn't support port sharing.

## Files touched
- deploy/hyqs-web@.service
- deploy/release.sh
- hyqs/web/app.py
- tests/test_release_sh.py
