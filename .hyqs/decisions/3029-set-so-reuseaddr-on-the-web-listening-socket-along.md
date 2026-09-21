# Job #3029: Set SO_REUSEADDR on the web listening socket alongside SO_REUSEPORT

**Date:** 2026-08-01

The diff adds SO_REUSEADDR socket option to Uvicorn's listener in hyqs/web/app.py, enabling the web server to rebind to a port immediately after restart even if the previous connection is in TIME_WAIT state. The comment was clarified to distinguish SO_REUSEADDR's purpose (handling TIME_WAIT recovery) from SO_REUSEPORT's purpose (blue-green deployment with concurrent listeners). Two tests were added: one verifying both socket options are enabled, and an integration test confirming the listener can successfully rebind to a port in TIME_WAIT state after a previous connection closes.
The diff adds SO_REUSEADDR socket option to Uvicorn's listener in hyqs/web/app.py, enabling the web server to rebind to a port immediately after restart even if the previous connection is in TIME_WAIT state. The comment was clarified to distinguish SO_REUSEADDR's purpose (handling TIME_WAIT recovery) from SO_REUSEPORT's purpose (blue-green deployment with concurrent listeners). Two tests were added: one verifying both socket options are enabled, and an integration test confirming the listener can successfully rebind to a port in TIME_WAIT state after a previous connection closes.

## Files touched
- hyqs/web/app.py
- tests/test_release_sh.py
