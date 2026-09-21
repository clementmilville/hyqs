import asyncio
import os
import signal

from hyqs.config import Config
from hyqs.pipeline.__main__ import _install_signal_handlers, _shutdown_on_signal


def _events() -> tuple[asyncio.Event, asyncio.Event, asyncio.Event]:
    return asyncio.Event(), asyncio.Event(), asyncio.Event()


async def _fire(sig: signal.Signals) -> None:
    # Give the event loop a tick to process the signal handler before we look
    # at the events it flipped.
    os.kill(os.getpid(), sig)
    await asyncio.sleep(0.05)


def test_sigterm_sets_term_drain_not_stop():
    async def _run():
        stop, drain, term_drain = _events()
        loop = asyncio.get_running_loop()
        _install_signal_handlers(loop, stop, drain, term_drain)
        await _fire(signal.SIGTERM)
        assert term_drain.is_set()
        assert not stop.is_set()
        assert not drain.is_set()

    asyncio.run(_run())


def test_double_sigterm_forces_stop():
    async def _run():
        stop, drain, term_drain = _events()
        loop = asyncio.get_running_loop()
        _install_signal_handlers(loop, stop, drain, term_drain)
        await _fire(signal.SIGTERM)
        await _fire(signal.SIGTERM)
        assert stop.is_set()

    asyncio.run(_run())


def test_sigint_always_stops_even_after_sigterm():
    async def _run():
        stop, drain, term_drain = _events()
        loop = asyncio.get_running_loop()
        _install_signal_handlers(loop, stop, drain, term_drain)
        await _fire(signal.SIGTERM)
        assert not stop.is_set()
        await _fire(signal.SIGINT)
        assert stop.is_set()

    asyncio.run(_run())


def test_sigusr1_sets_drain_unchanged():
    async def _run():
        stop, drain, term_drain = _events()
        loop = asyncio.get_running_loop()
        _install_signal_handlers(loop, stop, drain, term_drain)
        await _fire(signal.SIGUSR1)
        assert drain.is_set()
        assert not stop.is_set()
        assert not term_drain.is_set()

    asyncio.run(_run())


class _FakeRunner:
    def __init__(self) -> None:
        self.stop_calls = 0
        self.drain_calls: list[float] = []

    async def stop(self) -> None:
        self.stop_calls += 1

    async def drain(self, timeout: float) -> None:
        self.drain_calls.append(timeout)


class _FakeSupervisor:
    def __init__(self) -> None:
        self.stop_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1


def _config() -> Config:
    return Config(model="sonnet", permission_mode="acceptEdits")


def test_shutdown_calls_stop_when_stop_is_set():
    async def _run():
        stop, drain, term_drain = _events()
        stop.set()
        runner, supervisor = _FakeRunner(), _FakeSupervisor()
        await _shutdown_on_signal(runner, supervisor, stop, drain, term_drain, _config())
        assert runner.stop_calls == 1
        assert runner.drain_calls == []
        assert supervisor.stop_calls == 1

    asyncio.run(_run())


def test_shutdown_drains_with_deploy_timeout_when_term_drain_is_set():
    async def _run():
        stop, drain, term_drain = _events()
        term_drain.set()
        config = _config()
        runner, supervisor = _FakeRunner(), _FakeSupervisor()
        await _shutdown_on_signal(runner, supervisor, stop, drain, term_drain, config)
        assert runner.stop_calls == 0
        assert runner.drain_calls == [config.pipeline_deploy_drain_timeout]

    asyncio.run(_run())


def test_shutdown_drains_with_short_timeout_when_drain_is_set():
    async def _run():
        stop, drain, term_drain = _events()
        drain.set()
        config = _config()
        runner, supervisor = _FakeRunner(), _FakeSupervisor()
        await _shutdown_on_signal(runner, supervisor, stop, drain, term_drain, config)
        assert runner.stop_calls == 0
        assert runner.drain_calls == [config.pipeline_drain_timeout]

    asyncio.run(_run())
