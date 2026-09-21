# Job #4268: Add PageViewSummary-style SiteStats dataclass to models.py

**Date:** 2026-09-07

This diff adds a `SiteStats` dataclass to track aggregated pipeline metrics across the entire hyqs system, including counts of shipped jobs (total, last 7 days, last 24 hours), timing statistics (lead time percentiles, time between ships), cost data, gate stop information, and real-time system status (workers online, jobs running). The dataclass includes a `to_dict()` method to serialize all fields for API responses or storage. This enables telemetry and observability around pipeline health, user activity, and operational costs.
This diff adds a `SiteStats` dataclass to track aggregated pipeline metrics across the entire hyqs system, including counts of shipped jobs (total, last 7 days, last 24 hours), timing statistics (lead time percentiles, time between ships), cost data, gate stop information, and real-time system status (workers online, jobs running). The dataclass includes a `to_dict()` method to serialize all fields for API responses or storage. This enables telemetry and observability around pipeline health, user activity, and operational costs. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/pipeline/models.py
