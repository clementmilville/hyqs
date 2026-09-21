"""Background scheduler that makes Hyqs 'living': it fires due reminders.

Polls the memory store and pushes any due reminder to its chat. This is the
seam for future proactive behaviour (digests, monitoring, autonomous tasks).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

from hyqs.memory import MemoryStore

log = logging.getLogger("hyqs.scheduler")

# (chat_id, text) -> coroutine that delivers the message.
Sender = Callable[[int, str], Awaitable[None]]


class ReminderScheduler:
    def __init__(self, store: MemoryStore, send: Sender, interval: float = 15.0) -> None:
        self.store = store
        self.send = send
        self.interval = interval
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="hyqs-scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        log.info("reminder scheduler started (every %ss)", self.interval)
        while True:
            try:
                now = datetime.now(timezone.utc).isoformat()
                for row in self.store.due_reminders(now):
                    await self.send(row["chat_id"], f"⏰ Reminder: {row['text']}")
                    self.store.mark_fired(row["id"])
            except Exception:  # noqa: BLE001 - never let the loop die
                log.exception("scheduler tick failed")
            await asyncio.sleep(self.interval)
