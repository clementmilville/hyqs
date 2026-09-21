# Job #4258: Advertise the fastapi blueprint in KNOWN_STACKS

**Date:** 2026-09-07

Made KNOWN_STACKS derive from the blueprint registry instead of maintaining a separate hardcoded list, eliminating the risk of the two getting out of sync when blueprints are added or removed. Added two tests to verify that all registered stacks are discoverable: one checking that fastapi is included (catching the recent addition), and another validating that every stack in KNOWN_STACKS resolves via get_blueprint. This shifts the source of truth from a duplicate list to a single registry definition.
Made KNOWN_STACKS derive from the blueprint registry instead of maintaining a separate hardcoded list, eliminating the risk of the two getting out of sync when blueprints are added or removed. Added two tests to verify that all registered stacks are discoverable: one checking that fastapi is included (catching the recent addition), and another validating that every stack in KNOWN_STACKS resolves via get_blueprint. This shifts the source of truth from a duplicate list to a single registry definition. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/pipeline/blueprints/__init__.py
- tests/test_deployer_onboarding.py
