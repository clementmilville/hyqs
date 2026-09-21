# Job #4316: Give each console user their own orchestrator chat session

**Date:** 2026-09-15

This diff fixes a session isolation bug where all users shared a single Claude orchestrator session via a hardcoded `WEB_CHAT_ID` constant, exposing conversation history and agent state across distinct principals. The fix introduces `_orchestrator_session_id()` to key each user's chat session to their authenticated identity (or a reserved sentinel for the web-token bypass), while keeping `WEB_CHAT_ID` as a separate job-provenance identifier for tracking which chat created jobs. The core agent docstrings are clarified to emphasize that `chat_id` must identify the caller's session, not be shared as a constant. A regression test suite verifies distinct users now get isolated sessions in both sync and streaming chat, while job creation continues recording `WEB_CHAT_ID` regardless of caller.
This diff fixes a session isolation bug where all users shared a single Claude orchestrator session via a hardcoded `WEB_CHAT_ID` constant, exposing conversation history and agent state across distinct principals. The fix introduces `_orchestrator_session_id()` to key each user's chat session to their authenticated identity (or a reserved sentinel for the web-token bypass), while keeping `WEB_CHAT_ID` as a separate job-provenance identifier for tracking which chat created jobs. The core agent docstrings are clarified to emphasize that `chat_id` must identify the caller's session, not be shared as a constant. A regression test suite verifies distinct users now get isolated sessions in both sync and streaming chat, while job creation continues recording `WEB_CHAT_ID` regardless of caller. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/core/agent.py
- hyqs/web/app.py
- tests/test_web_chat_session.py
