"""Standalone web console — run the web UI/API without the pipeline worker.

Boots just the Orchestrator + JobStore + web server, so you can use the
dashboard (live pipeline jobs, usage) and the chat console. Run it with
``hyqs-web`` or ``python -m hyqs.web``.

It reads the same Postgres database as the daemon / pipeline worker, so jobs
queued anywhere show up here live.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from hyqs import web
from hyqs.config import Config
from hyqs.core import Orchestrator
from hyqs.memory import MemoryStore
from hyqs.pipeline import JobStore
from hyqs.pipeline.logging_setup import configure_logging

configure_logging(Config.from_env().log_format)
log = logging.getLogger("hyqs.web")


async def run() -> None:
    from hyqs.pipeline.store import set_db_actor

    set_db_actor("system:hyqs-web")
    config = Config.from_env()
    memory = MemoryStore(config.data_dir / "hyqs.db")
    jobs = JobStore(config.db_url)
    orchestrator = Orchestrator(config, memory)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    server = asyncio.create_task(web.serve(config, orchestrator, jobs), name="hyqs-web")
    shutdown = asyncio.create_task(stop.wait(), name="hyqs-web-shutdown")
    log.info("Hyqs web console at http://%s:%s — Ctrl-C to stop.", config.web_host, config.web_port)
    try:
        done, _pending = await asyncio.wait((server, shutdown), return_when=asyncio.FIRST_COMPLETED)
        if server in done:
            server.result()
        else:
            log.info("shutting down…")
            server.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server
    finally:
        shutdown.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await shutdown
        await orchestrator.aclose()
        memory.close()
        jobs.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
