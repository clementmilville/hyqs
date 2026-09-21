# Job #4306: Validate the deploy_config port before it reaches nginx config

**Date:** 2026-09-08

This diff adds centralized input validation for nginx configuration—ports and domain names. Two new validator functions (`normalize_port()` and `_validate_domains()`) check that ports are valid integers in the TCP range (1–65535) and that domains are strings matching a hostname pattern with configured SSL snippets, rejecting type mismatches and potential injection attempts like `8080; include /etc/shadow;`. The validation is applied consistently across `render_site()`, `register_site()`, and `resolve_domains()`, with the MCP server now delegating domain checks to `resolve_domains()` instead of checking locally. Comprehensive test coverage verifies both valid and malicious inputs are handled correctly, and that validation happens before any side effects like file writes.
This diff adds centralized input validation for nginx configuration—ports and domain names. Two new validator functions (`normalize_port()` and `_validate_domains()`) check that ports are valid integers in the TCP range (1–65535) and that domains are strings matching a hostname pattern with configured SSL snippets, rejecting type mismatches and potential injection attempts like `8080; include /etc/shadow;`. The validation is applied consistently across `render_site()`, `register_site()`, and `resolve_domains()`, with the MCP server now delegating domain checks to `resolve_domains()` instead of checking locally. Comprehensive test coverage verifies both valid and malicious inputs are handled correctly, and that validation happens before any side effects like file writes. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/pipeline/nginx_sites.py
- hyqs/pipeline/provision.py
- hyqs/web/mcp_server.py
- tests/test_mcp_server.py
- tests/test_nginx_sites.py
