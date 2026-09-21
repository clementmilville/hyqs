# Job #3028: Propagate web serve() failure so a bind conflict is detectable

**Date:** 2026-08-01

The deploy script now diagnoses why the new web unit fails during release by checking journalctl for address-already-in-use errors, falling back to a bare restart only for port conflicts and exiting with an error if the unit fails for any other reason. The web server startup code was refactored to use `asyncio.wait()` with proper signal handling and cleanup, ensuring that immediate server startup failures are propagated to the caller rather than hidden by the shutdown sequence. These changes prevent silent failures during release and make deployment errors more transparent and actionable. The test suite was updated to validate both the refined error detection in the shell script and the improved failure propagation in the Python startup code.
The deploy script now diagnoses why the new web unit fails during release by checking journalctl for address-already-in-use errors, falling back to a bare restart only for port conflicts and exiting with an error if the unit fails for any other reason. The web server startup code was refactored to use `asyncio.wait()` with proper signal handling and cleanup, ensuring that immediate server startup failures are propagated to the caller rather than hidden by the shutdown sequence. These changes prevent silent failures during release and make deployment errors more transparent and actionable. The test suite was updated to validate both the refined error detection in the shell script and the improved failure propagation in the Python startup code.

## Files touched
- deploy/release.sh
- hyqs/web/__main__.py
- tests/test_release_sh.py
