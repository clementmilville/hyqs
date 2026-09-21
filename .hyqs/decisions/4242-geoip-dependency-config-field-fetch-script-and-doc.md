# Job #4242: GeoIP dependency, config field, fetch script, and docs

**Date:** 2026-09-06

This diff adds local GeoIP geolocation support to Hyqs using the DB-IP City Lite database in MaxMind format. It introduces a new `HYQS_GEOIP_DB` environment variable with a sensible default path under the data directory, adds the `maxminddb` Python library dependency, and provides a fetch script that downloads and installs the current month's database snapshot. The configuration is wired into the Config class with proper fallback logic, tests verify that the default and custom paths work correctly, and the deployment documentation explains the setup process and the CC BY 4.0 attribution requirement for the free database.
This diff adds local GeoIP geolocation support to Hyqs using the DB-IP City Lite database in MaxMind format. It introduces a new `HYQS_GEOIP_DB` environment variable with a sensible default path under the data directory, adds the `maxminddb` Python library dependency, and provides a fetch script that downloads and installs the current month's database snapshot. The configuration is wired into the Config class with proper fallback logic, tests verify that the default and custom paths work correctly, and the deployment documentation explains the setup process and the CC BY 4.0 attribution requirement for the free database. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- .env.example
- deploy/README.md
- hyqs/config.py
- pyproject.toml
- scripts/fetch_geoip_db.sh
- tests/test_config.py
- uv.lock
