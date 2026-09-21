# Job #4317: Add trusted_proxy_depth config setting

**Date:** 2026-09-08

This diff adds a new configuration parameter `trusted_proxy_depth` to control how many reverse-proxy hops are trusted in front of the hyqs process, defaulting to 1 (the current nginx hop). The setting is exposed via the `HYQS_TRUSTED_PROXY_DEPTH` environment variable for deployment flexibility. Two tests verify the default behavior and that custom values are properly read from the environment.
This diff adds a new configuration parameter `trusted_proxy_depth` to control how many reverse-proxy hops are trusted in front of the hyqs process, defaulting to 1 (the current nginx hop). The setting is exposed via the `HYQS_TRUSTED_PROXY_DEPTH` environment variable for deployment flexibility. Two tests verify the default behavior and that custom values are properly read from the environment. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/config.py
- tests/test_config.py
