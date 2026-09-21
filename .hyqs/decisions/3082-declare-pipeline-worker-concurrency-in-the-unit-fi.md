# Job #3082: Declare pipeline worker concurrency in the unit files instead of defaulting to one

**Date:** 2026-08-02

This diff re-enables pipeline worker concurrency at 2 (down from a pre-incident default of 6) to recover job throughput, making it safe by pairing it with explicit cgroup memory ceilings (MemoryMax=14G and MemoryAccounting) that contain the blast radius of runaway workers. The change is applied to both the main and templated systemd service units and keeps them synchronized via comments. A new test is added to prevent silent regressions by ensuring both service files actively declare concurrency above 1 and declare the memory accounting limits that make the concurrency bump safe.
This diff re-enables pipeline worker concurrency at 2 (down from a pre-incident default of 6) to recover job throughput, making it safe by pairing it with explicit cgroup memory ceilings (MemoryMax=14G and MemoryAccounting) that contain the blast radius of runaway workers. The change is applied to both the main and templated systemd service units and keeps them synchronized via comments. A new test is added to prevent silent regressions by ensuring both service files actively declare concurrency above 1 and declare the memory accounting limits that make the concurrency bump safe.

## Files touched
- deploy/hyqs-pipeline.service
- deploy/hyqs-pipeline@.service
- tests/test_release_sh.py
