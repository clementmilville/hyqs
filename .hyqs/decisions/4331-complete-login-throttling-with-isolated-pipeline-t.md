# Job #4331: Complete login throttling with isolated pipeline test database

**Date:** 2026-09-15

This diff adds two security features: progressive rate limiting on login attempts (with dual tiers—short burst and longer sustained—tracked per IP and per-email) and secure test database provisioning that isolates pytest runs into uniquely-named disposable databases rather than sharing a single test DB. The second change prevents the worker's privileged database DSN from leaking to untrusted subprocess (addressing job #4308), while the first defends against credential-stuffing attacks by rate-limiting both per-source-IP (catching distributed attacks) and per-normalized-email (catching spray attacks from a single IP). Both refactor existing patterns—the login endpoint now invokes centralized throttling helpers, and page-view/site-stats beacons now share a common sliding-window primitive.
This diff adds two security features: progressive rate limiting on login attempts (with dual tiers—short burst and longer sustained—tracked per IP and per-email) and secure test database provisioning that isolates pytest runs into uniquely-named disposable databases rather than sharing a single test DB. The second change prevents the worker's privileged database DSN from leaking to untrusted subprocess (addressing job #4308), while the first defends against credential-stuffing attacks by rate-limiting both per-source-IP (catching distributed attacks) and per-normalized-email (catching spray attacks from a single IP). Both refactor existing patterns—the login endpoint now invokes centralized throttling helpers, and page-view/site-stats beacons now share a common sliding-window primitive. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/pipeline/testing.py
- hyqs/web/app.py
- tests/test_login_rate_limit.py
- tests/test_testing.py
