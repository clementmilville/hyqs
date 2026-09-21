# Job #4322: Regression tests for the login rate limiter

**Date:** 2026-09-15

This diff adds two test cases to the login rate-limiting test suite. The first test verifies that successful login attempts return a 200 status code with a valid token and user details in the response body. The second test confirms that IP-based rate throttling keys off the rightmost trusted IP in the x-forwarded-for header rather than the leftmost one, which prevents attackers from spoofing IPs on the left side of a proxy chain to bypass rate limits.
This diff adds two test cases to the login rate-limiting test suite. The first test verifies that successful login attempts return a 200 status code with a valid token and user details in the response body. The second test confirms that IP-based rate throttling keys off the rightmost trusted IP in the x-forwarded-for header rather than the leftmost one, which prevents attackers from spoofing IPs on the left side of a proxy chain to bypass rate limits. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- tests/test_login_rate_limit.py
