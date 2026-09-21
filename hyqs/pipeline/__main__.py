"""Standalone pipeline worker — run ONLY the build pipeline, headless.

This lets the autonomous pipeline live as its own OS process, independent of
the web console and of any interactive Claude session. Jobs are created
elsewhere (the web API) into the shared Postgres; this worker just advances
them through the stages, self-healing and pausing on rate limits as usual.

Run it with ``hyqs-pipeline`` or ``python -m hyqs.pipeline`` (see
``deploy/hyqs-pipeline.service``). Notifications always go to the log, and
also reach a project's configured Slack workspace when one exists.

Run exactly ONE pipeline runner against a given database: if you use this
worker, set ``HYQS_PIPELINE_DISABLED=1`` on the web daemon so it doesn't run a
second one.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import signal
import socket
import sys
import time
from pathlib import Path

import httpx

from hyqs.config import Config
from hyqs.pipeline import (
    JobStore,
    PipelineRunner,
    Stage,
    deployer_state,
    docker_deploy,
    hosts,
    notify_slack,
)
from hyqs.pipeline.logging_setup import configure_logging
from hyqs.pipeline.models import JobStatus, PromotionState
from hyqs.pipeline.supervisor import SupervisorRunner

configure_logging(Config.from_env().log_format)
log = logging.getLogger("hyqs.pipeline")


def _build_notifier(jobs: JobStore, config: Config):
    """Return ``(notify, aclose)``: logs always, and delivers to the project's
    configured Slack workspace and matching generic webhooks."""

    http_client = httpx.AsyncClient(timeout=5.0, trust_env=False)

    async def notify(
        chat_id: int,
        text: str,
        project_id: int | None = None,
        job_id: int | None = None,
        event_type: str | None = None,
        reason: str | None = None,
    ) -> None:
        log.info("job→chat %s: %s", chat_id, text.replace("\n", " ")[:160])
        if project_id is None:
            return
        webhooks = jobs.list_webhooks(project_id)
        job = jobs.get(job_id) if job_id is not None else None

        if job_id is not None:
            token = jobs.get_project_slack_token(project_id)
            for webhook in webhooks:
                if webhook.kind == "slack" and webhook.active and token is not None:
                    existing_ts = jobs.get_slack_thread_ts(job_id, webhook.id)
                    send_text = text
                    if existing_ts is None:
                        if job is not None:
                            if job.title:
                                send_text = f"*Job #{job_id}: {job.title}*\n{text}"
                            else:
                                send_text = f"*Job #{job_id}*\n{text}"
                        job_url = notify_slack.build_job_url(
                            config.web_base_url, project_id, job_id
                        )
                        if job_url is not None:
                            send_text += f"\n<{job_url}|View job>"
                    returned_ts = await notify_slack.send_slack(
                        token, webhook.url, send_text, thread_ts=existing_ts
                    )
                    if existing_ts is None and returned_ts is not None:
                        jobs.set_slack_thread_ts(job_id, webhook.id, returned_ts)

        if event_type is None and job is not None and job.status == JobStatus.DONE:
            event_type = "deploy" if job.deployed_commit is not None else "job_complete"
        if event_type is None:
            return
        payload = notify_slack.build_webhook_payload(
            project_id=project_id,
            job_id=job_id,
            event_type=event_type,
            summary=text.replace("\x00", ""),
            epic_id=job.epic_id if job is not None else None,
            reason=reason,
        )
        deliveries = [
            notify_slack.send_http_webhook(webhook.url, payload, client=http_client)
            for webhook in webhooks
            if webhook.kind == "http" and webhook.active and webhook.event_type == event_type
        ]
        if deliveries:
            await asyncio.gather(*deliveries)

    async def aclose() -> None:
        await http_client.aclose()

    return notify, aclose


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    stop: asyncio.Event,
    drain: asyncio.Event,
    term_drain: asyncio.Event,
) -> None:
    """Wire up shutdown signals.

    SIGINT is always an immediate stop (interactive Ctrl-C). SIGTERM — what
    systemd sends on ``systemctl stop``/restart, i.e. every self-deploy — asks
    for a graceful drain on its FIRST delivery, using the long deploy-drain
    ceiling so a mid-flight stage isn't killed by a release; it then re-arms
    itself so a SECOND SIGTERM forces an immediate stop (an operator giving up
    on the drain). SIGUSR1 is the pre-existing manual/operator drain, using
    the short drain timeout, unchanged.
    """

    def _on_sigterm() -> None:
        loop.remove_signal_handler(signal.SIGTERM)
        loop.add_signal_handler(signal.SIGTERM, stop.set)
        term_drain.set()

    loop.add_signal_handler(signal.SIGINT, stop.set)
    loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
    loop.add_signal_handler(signal.SIGUSR1, drain.set)


async def _shutdown_on_signal(
    runner: PipelineRunner,
    supervisor: SupervisorRunner,
    stop: asyncio.Event,
    drain: asyncio.Event,
    term_drain: asyncio.Event,
    config: Config,
) -> None:
    """Wait for a shutdown signal, then stop or drain the runner accordingly."""
    stop_task = asyncio.create_task(stop.wait())
    drain_task = asyncio.create_task(drain.wait())
    term_drain_task = asyncio.create_task(term_drain.wait())
    await asyncio.wait(
        {stop_task, drain_task, term_drain_task}, return_when=asyncio.FIRST_COMPLETED
    )
    for t in (stop_task, drain_task, term_drain_task):
        t.cancel()

    await supervisor.stop()
    if stop.is_set():
        log.info("shutting down…")
        await runner.stop()
    elif term_drain.is_set():
        log.info(
            "SIGTERM received; draining in-flight work (deploy timeout=%.0fs)…",
            config.pipeline_deploy_drain_timeout,
        )
        await runner.drain(config.pipeline_deploy_drain_timeout)
    else:
        log.info(
            "drain signal received; finishing in-flight work (timeout=%.0fs)…",
            config.pipeline_drain_timeout,
        )
        await runner.drain(config.pipeline_drain_timeout)


async def run(deployer_mode: bool = False) -> None:
    config = Config.from_env()
    jobs = JobStore(config.db_url)
    jobs.sweep_stale_deploy_locks()
    for p in jobs.list_projects():
        if not p.get("github_url"):
            log.warning(
                "project %d (%s) has no github_url — re-provision to create the remote",
                p["id"],
                p["name"],
            )
    notify, close_notifier = _build_notifier(jobs, config)

    drain = asyncio.Event()
    term_drain = asyncio.Event()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, stop, drain, term_drain)

    runner = PipelineRunner(
        jobs,
        config,
        notify=notify,
        drain=drain,
        stage_allowlist={Stage.DEPLOY} if deployer_mode else None,
        deployer_mode=deployer_mode,
    )
    supervisor = SupervisorRunner(jobs, config, notify=notify)

    try:
        await runner.start()
        await supervisor.start()
        log.info(
            "Hyqs pipeline worker is live (data_dir=%s). Ctrl-C or 2nd SIGTERM=stop now, "
            "1st SIGTERM=graceful deploy drain (timeout=%.0fs), SIGUSR1=graceful manual "
            "drain (timeout=%.0fs).",
            config.data_dir,
            config.pipeline_deploy_drain_timeout,
            config.pipeline_drain_timeout,
        )

        await _shutdown_on_signal(runner, supervisor, stop, drain, term_drain, config)
    finally:
        await close_notifier()
        jobs.close()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="hyqs-pipeline")
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument("--pid", type=str, default=None)
    parser.add_argument("--fresh-seconds", type=float, default=30.0)
    parser.add_argument("--plan-epic", type=int, default=None, metavar="EPIC_ID")
    parser.add_argument("--goal", type=str, default=None)
    parser.add_argument(
        "--deployer",
        action="store_true",
        help="Run as a constrained deploy-only worker, pinned to HYQS_HOST_NAME.",
    )

    subparsers = parser.add_subparsers(dest="command")
    enroll_parser = subparsers.add_parser("enroll", help="Enroll this host with the control plane.")
    enroll_parser.add_argument("--name", type=str, required=True)
    subparsers.add_parser("status", help="Print this deploy host's local identity/state.")

    apply_parser = subparsers.add_parser(
        "apply",
        help="Pull-by-digest apply the current available offer for this host's environment(s).",
    )
    apply_parser.add_argument("--operator", type=str, default=None)

    decline_parser = subparsers.add_parser(
        "decline",
        help="Decline the current available offer for this host's environment(s).",
    )
    decline_parser.add_argument("--reason", type=str, required=True)
    decline_parser.add_argument("--operator", type=str, default=None)

    args = parser.parse_args(argv)
    if args.healthcheck and args.pid is None:
        parser.error("--healthcheck requires --pid")
    if args.plan_epic is not None and not (args.goal or "").strip():
        parser.error("--plan-epic requires --goal")
    if args.pid is not None:
        try:
            args.pids = [int(p) for p in args.pid.split(",") if p.strip()]
        except ValueError:
            parser.error("--pid must be a comma-separated list of integers")
    else:
        args.pids = []
    return args


def _run_plan_epic(epic_id: int, goal: str) -> int:
    """Dry-run the ARCHITECT for one epic: print the proposed job DAG as JSON.

    Filing is never done here — "the AI only draws the map". Confirmation still
    goes through the existing /api/jobs/batch flow once a human reviews the DAG.
    Returns a process exit code; never mutates the jobs table.
    """
    from . import agents, decisions
    from .providers import build_backend

    config = Config.from_env()
    store = JobStore(config.db_url)
    try:
        epic = store.get_epic(epic_id)
        if epic is None:
            print(f"epic {epic_id} not found", file=sys.stderr)
            return 1
        project = store.get_project(epic.project_id)
        if project is None:
            print(f"project for epic {epic_id} not found", file=sys.stderr)
            return 1

        symbol_index_text = store.get_symbol_index_text(project.id)
        decision_digest = decisions.load_digest(project.repo_path)
        prompt = agents._build_architect_prompt(
            project.name,
            project.repo_path,
            epic.name,
            epic.description,
            goal,
            symbol_index_text,
            decision_digest,
        )
        backend = build_backend("claude", config=config)

        async def _run() -> dict:
            result: dict = {"summary": "", "rationale": "", "jobs": []}
            async for event in agents.architect(backend, prompt, project.repo_path):
                if event.get("type") == "result":
                    result = {
                        "summary": event.get("summary", ""),
                        "rationale": event.get("rationale", ""),
                        "jobs": event.get("jobs", []),
                    }
            return result

        print(json.dumps(asyncio.run(_run())))
        return 0
    finally:
        store.close()


def _worker_is_healthy(
    store: JobStore,
    pids: list[int],
    *,
    host: str | None = None,
    now: float | None = None,
    fresh_seconds: float = 30.0,
) -> bool:
    """True iff a worker on ``host`` with any pid in ``pids`` has a fresh heartbeat.

    A systemd unit's MainPID is the ``uv run`` wrapper process, not the
    ``hyqs-pipeline`` python child that actually writes heartbeats — so the
    caller must pass the whole cgroup's pid set, not just MainPID.
    """
    host = host or socket.gethostname()
    now = now if now is not None else time.time()
    pid_set = set(pids)
    return any(
        w["host"] == host and w["pid"] in pid_set and w["alive"]
        for w in store.list_workers(now, fresh_seconds)
    )


def _run_enroll(name: str) -> int:
    """Enroll this host with the control plane and persist its local identity.

    The one control-plane call an enroll is allowed to make is
    ``hosts.enroll_host`` — everything else here is local file I/O.
    """
    config = Config.from_env()
    private_pem, public_pem = hosts.generate_host_keypair()
    key_path = Path(config.data_dir) / "deployer" / "host_key.pem"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(private_pem)
    key_path.chmod(0o600)

    store = JobStore(config.db_url)
    try:
        host = hosts.enroll_host(store, name, public_pem)
    finally:
        store.close()

    deployer_state.write_host_config(config.data_dir, name, str(key_path))
    print(f"enrolled host {host.name!r} (id={host.id})")
    return 0


def _run_status() -> int:
    """Print this deploy host's local identity/state — pure file I/O, no DB."""
    config = Config.from_env()
    host_config = deployer_state.read_host_config(config.data_dir)
    if host_config is None:
        print("not enrolled yet — run `hyqs-pipeline enroll --name <host-name>`")
        return 1

    state = deployer_state.read_state(config.data_dir)
    print(f"host: {host_config['host_name']}")
    print(f"private key: {host_config['private_key_path']}")
    print(f"environments served: {state.get('environments_served', [])}")
    print(f"last claim: {state.get('last_claim')}")
    print(f"last deploy: {state.get('last_deploy')}")
    return 0


def _find_available_offer(store: JobStore, host):
    """The first available offer among this host's pinned environments, and
    the environment it targets — (None, None) if there is none."""
    for env in store.list_environments_by_host(host.id):
        offer = store.get_available_offer(env.id)
        if offer is not None:
            return offer, env
    return None, None


async def _apply_offer(store: JobStore, data_dir: str, offer, env, operator: str) -> int:
    """Run the pull-by-digest apply path for ``offer`` locally, on this host,
    then advance the promotion ledger. Returns a process exit code."""
    store.transition_promotion(offer.id, PromotionState.DEPLOYING)
    release = store.get_release(offer.release_id)
    project = store.get_project(offer.project_id)

    result = await docker_deploy.pull_deploy(
        project.repo_path, release, json.dumps(store.get_environment_config(env.id))
    )

    if result.get("deployed") and result.get("verified", True):
        store.transition_promotion(offer.id, PromotionState.DEPLOYED, applied_by=operator)
        store.set_environment_current_release(env.id, release.id)
        deployer_state.record_apply(
            data_dir,
            promotion_id=offer.id,
            project_id=offer.project_id,
            environment_id=env.id,
            release_id=release.id,
            applied_by=operator,
        )
        print(f"applied release {release.id} to environment {env.id}")
        return 0

    store.transition_promotion(offer.id, PromotionState.FAILED)
    print(result.get("output", "apply failed"), file=sys.stderr)
    return 1


def _run_apply(operator: str) -> int:
    """Pull-by-digest apply the current available offer for this host's own
    environment(s), run entirely on the client host. See #1787's pull_deploy
    for the fail-closed pull + verify + secrets + health-gated cutover."""
    config = Config.from_env()
    host_config = deployer_state.read_host_config(config.data_dir)
    if host_config is None:
        print("not enrolled yet — run `hyqs-pipeline enroll --name <host-name>`")
        return 1

    store = JobStore(config.db_url)
    try:
        host = store.get_host_by_name(host_config["host_name"])
        if host is None:
            print(f"host {host_config['host_name']!r} not found on the control plane")
            return 1
        envs = store.list_environments_by_host(host.id)
        if not envs:
            print(f"no environments are pinned to host {host.name!r}")
            return 1
        offer, env = _find_available_offer(store, host)
        if offer is None:
            print("no available offer for this host's environment(s)")
            return 1
        return asyncio.run(_apply_offer(store, config.data_dir, offer, env, operator))
    finally:
        store.close()


def _run_decline(reason: str, operator: str) -> int:
    """Decline the current available offer for this host's own environment(s)."""
    config = Config.from_env()
    host_config = deployer_state.read_host_config(config.data_dir)
    if host_config is None:
        print("not enrolled yet — run `hyqs-pipeline enroll --name <host-name>`")
        return 1

    store = JobStore(config.db_url)
    try:
        host = store.get_host_by_name(host_config["host_name"])
        if host is None:
            print(f"host {host_config['host_name']!r} not found on the control plane")
            return 1
        offer, env = _find_available_offer(store, host)
        if offer is None:
            print("no available offer for this host's environment(s)")
            return 1
        store.transition_promotion(offer.id, PromotionState.DECLINED, applied_by=operator)
        deployer_state.record_decline(
            config.data_dir,
            promotion_id=offer.id,
            project_id=offer.project_id,
            environment_id=env.id,
            reason=reason,
            applied_by=operator,
        )
        print(f"declined offer {offer.id}: {reason}")
        return 0
    finally:
        store.close()


def _run_healthcheck(pids: list[int], fresh_seconds: float = 30.0) -> bool:
    config = Config.from_env()
    store = JobStore(config.db_url)
    try:
        return _worker_is_healthy(store, pids, fresh_seconds=fresh_seconds)
    finally:
        store.close()


def main() -> None:
    args = _parse_args()
    if args.command == "enroll":
        raise SystemExit(_run_enroll(args.name))
    if args.command == "status":
        raise SystemExit(_run_status())
    if args.command == "apply":
        raise SystemExit(_run_apply(args.operator or getpass.getuser()))
    if args.command == "decline":
        raise SystemExit(_run_decline(args.reason, args.operator or getpass.getuser()))
    if args.healthcheck:
        healthy = _run_healthcheck(args.pids, fresh_seconds=args.fresh_seconds)
        raise SystemExit(0 if healthy else 1)
    if args.plan_epic is not None:
        raise SystemExit(_run_plan_epic(args.plan_epic, args.goal))
    if args.deployer and not Config.from_env().pipeline_host_name:
        print(
            "--deployer requires HYQS_HOST_NAME to be set — a deployer must be pinned to a host",
            file=sys.stderr,
        )
        raise SystemExit(1)

    from .store import set_db_actor

    set_db_actor("system:hyqs-pipeline")
    try:
        asyncio.run(run(deployer_mode=args.deployer))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
