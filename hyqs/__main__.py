"""Entry point: boot Hyqs (web + pipeline + scheduler) and run until interrupted."""

from __future__ import annotations

import asyncio
import logging
import signal

from hyqs import web
from hyqs.config import Config
from hyqs.core import Orchestrator, ReminderScheduler
from hyqs.memory import MemoryStore
from hyqs.pipeline import JobStore, PipelineRunner
from hyqs.pipeline.__main__ import _build_notifier
from hyqs.pipeline.logging_setup import configure_logging

configure_logging(Config.from_env().log_format)
log = logging.getLogger("hyqs")


async def run() -> None:
    config = Config.from_env()
    memory = MemoryStore(config.data_dir / "hyqs.db")
    jobs = JobStore(config.db_url)
    orchestrator = Orchestrator(config, memory)

    notify, close_notifier = _build_notifier(jobs, config)

    async def _notify(chat_id: int, text: str) -> None:
        await notify(chat_id, text)

    scheduler = ReminderScheduler(memory, send=_notify)
    runner = PipelineRunner(jobs, config, notify=notify)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        scheduler.start()
        if config.pipeline_enabled:
            await runner.start()
        tasks = []
        if config.web_enabled:
            tasks.append(
                asyncio.create_task(web.serve(config, orchestrator, jobs), name="hyqs-web")
            )

        log.info(
            "Hyqs is live (web%s). Ctrl-C to stop.",
            " + pipeline" if config.pipeline_enabled else " (pipeline: external worker)",
        )
        await stop.wait()

        log.info("shutting down...")
        await scheduler.stop()
        await runner.stop()
        for t in tasks:
            t.cancel()
        await orchestrator.aclose()
    finally:
        await close_notifier()
        memory.close()
        jobs.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
