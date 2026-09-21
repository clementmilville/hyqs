#!/usr/bin/env bash
set -euo pipefail

hyqs_data_dir="${HYQS_DATA_DIR:-${HOME}/.hyqs/data}"
release_marker="${hyqs_data_dir}/release-success-commit"
current_commit=""
skip_web_release=0

# Only a positively identified docs/tests/decision-only release may avoid a
# web swap. Every missing or ambiguous piece of Git/marker state deliberately
# leaves skip_web_release at its fail-safe default of 0.
if current_commit="$(git rev-parse --verify HEAD 2>/dev/null)" \
    && [[ -r "${release_marker}" ]]; then
    previous_commit=""
    if previous_commit="$(<"${release_marker}")" 2>/dev/null \
        && [[ -n "${previous_commit}" ]] \
        && git cat-file -e "${previous_commit}^{commit}" 2>/dev/null; then
        changed_paths_file=""
        if changed_paths_file="$(mktemp)"; then
            if git diff --name-only -z "${previous_commit}" "${current_commit}" \
                > "${changed_paths_file}"; then
                mapfile -d '' -t release_changed_paths < "${changed_paths_file}"
                skip_web_release=1
                for changed_path in "${release_changed_paths[@]}"; do
                    case "${changed_path}" in
                        .hyqs/decisions/*|docs/*|tests/*) ;;
                        *)
                            skip_web_release=0
                            break
                            ;;
                    esac
                done
            fi
            rm -f "${changed_paths_file}"
        fi
    fi
fi

if [[ "${skip_web_release}" -eq 1 ]]; then
    echo "SKIP_WEB_RELEASE: all changed paths are limited to decisions, docs, or tests"
else
# Install deps first so a newly-declared dependency (e.g. a new package.json
# entry landed by a build job) is present in node_modules before the build.
# `npm ci` is exact-and-clean from the lockfile; without this a new dep bricks
# every deploy with an unresolved-import error.
if [[ "${SKIP_NPM_CI:-0}" != "1" ]]; then
    npm --prefix hyqs/web/frontend ci
fi
npm --prefix hyqs/web/frontend run build

# --- Web: overlapping-fleet (blue-green) replacement -------------------------
# A bare `systemctl restart hyqs-web` tears the process down and brings it
# back up with a gap in between during which nothing is listening on
# HYQS_WEB_PORT — every HTTP/MCP request that lands in that window 502s at
# the host nginx vhost, which proxies straight through with no retry and no
# backup upstream. As of this change hyqs/web/app.py's serve() binds its
# listening socket with SO_REUSEPORT, so a NEW hyqs-web@<generation> instance
# can bind the same port ALONGSIDE the currently running one; once the new
# instance proves it's the one actually answering (via its pid in
# /api/health), the old instance(s) are stopped and the port never goes
# silent. Mirrors the pipeline blue-green block below.
mapfile -t old_web_units < <(
    systemctl --user list-units 'hyqs-web*' --state=running --no-legend --plain 2>/dev/null \
        | awk '{print $1}'
)

if [[ ${#old_web_units[@]} -eq 0 ]]; then
    echo "no active hyqs-web* unit found; skipping web blue-green replacement"
else
    user_unit_dir="${HOME}/.config/systemd/user"
    template_src="deploy/hyqs-web@.service"
    template_dst="${user_unit_dir}/hyqs-web@.service"
    mkdir -p "${user_unit_dir}"
    if [[ ! -f "${template_dst}" ]] || ! cmp -s "${template_src}" "${template_dst}"; then
        echo "installing/refreshing ${template_dst} from ${template_src}…"
        cp "${template_src}" "${template_dst}"
        systemctl --user daemon-reload
    fi

    generation="$(date +%s)"
    new_web_unit="hyqs-web@${generation}"
    echo "starting ${new_web_unit} on the new code…"
    systemctl --user start "${new_web_unit}"

    # Bounded retry budget, same style as the pipeline heartbeat gate below.
    # Overridable so tests don't have to sleep through the full production
    # budget; unset in real deploys, where it's 90 retries at 1s each.
    web_gate_retries="${HYQS_WEB_HEALTH_GATE_RETRIES:-90}"
    web_gate_sleep="${HYQS_WEB_HEALTH_GATE_SLEEP:-1}"
    web_health_url="http://127.0.0.1:${HYQS_WEB_PORT:-8787}/api/health"

    pid_list=""
    new_web_healthy=0
    # One-time transition: the web process currently running was started by
    # the OLD code and never set SO_REUSEPORT, so the new generation can't
    # bind alongside it and systemd marks the unit failed. Detected here and
    # handled below by falling back to a bare restart for THIS deploy only —
    # every deploy after this one has two SO_REUSEPORT-aware processes and
    # is gapless.
    new_web_failed=0
    for _ in $(seq 1 "${web_gate_retries}"); do
        active_state="$(systemctl --user show -p ActiveState --value "${new_web_unit}" 2>/dev/null || true)"
        if [[ "${active_state}" == "failed" ]]; then
            new_web_failed=1
            break
        fi

        # Re-resolve the unit's pid set on every iteration (same reasoning
        # as the pipeline gate below: the uv wrapper's child forks in a
        # moment after `systemctl start` returns).
        current_main_pid="$(systemctl --user show -p MainPID --value "${new_web_unit}" 2>/dev/null || true)"
        pid_list="${current_main_pid}"
        control_group="$(systemctl --user show -p ControlGroup --value "${new_web_unit}" 2>/dev/null || true)"
        if [[ -n "${control_group}" ]]; then
            cgroup_procs_path="/sys/fs/cgroup${control_group}/cgroup.procs"
            if [[ -r "${cgroup_procs_path}" ]]; then
                mapfile -t cgroup_pids < "${cgroup_procs_path}"
                if [[ ${#cgroup_pids[@]} -gt 0 ]]; then
                    pid_list="$(IFS=,; echo "${cgroup_pids[*]}")"
                fi
            fi
        fi

        if [[ -n "${pid_list}" && "${pid_list}" != "0" ]]; then
            # With a shared port both generations answer /api/health — the
            # only proof the NEW one is serving is a response carrying a pid
            # from its own process/cgroup set.
            health_pid="$(
                curl -fsS --max-time 2 "${web_health_url}" 2>/dev/null \
                    | grep -o '"pid"[[:space:]]*:[[:space:]]*[0-9]*' \
                    | grep -o '[0-9]*$' || true
            )"
            if [[ -n "${health_pid}" ]]; then
                IFS=',' read -ra candidate_pids <<< "${pid_list}"
                for candidate in "${candidate_pids[@]}"; do
                    if [[ "${candidate}" == "${health_pid}" ]]; then
                        new_web_healthy=1
                        break
                    fi
                done
            fi
        fi
        if [[ "${new_web_healthy}" -eq 1 ]]; then
            break
        fi
        sleep "${web_gate_sleep}"
    done

    if [[ "${new_web_failed}" -eq 1 ]]; then
        bind_conflict=0
        if journalctl --user-unit "${new_web_unit}" --no-pager 2>/dev/null \
            | grep -Eiq 'address already in use|EADDRINUSE|\[Errno 98\]'; then
            bind_conflict=1
        fi
        systemctl --user stop "${new_web_unit}" >/dev/null 2>&1 || true
        if [[ "${bind_conflict}" -eq 1 ]]; then
            echo "WARNING: ${new_web_unit} failed with an address-already-in-use error because the currently running hyqs-web process predates SO_REUSEPORT and still holds the port; falling back to a bare restart for this one transition…" >&2
            systemctl --user restart hyqs-web
        else
            echo "ERROR: ${new_web_unit} failed without address-already-in-use evidence; leaving old instance(s) running" >&2
            exit 1
        fi
    elif [[ "${new_web_healthy}" -ne 1 ]]; then
        echo "ERROR: ${new_web_unit} (checked pids: ${pid_list:-none}) never answered ${web_health_url} with its own pid; stopping it and leaving old instance(s) running" >&2
        systemctl --user stop "${new_web_unit}" >/dev/null 2>&1 || true
        exit 1
    else
        echo "${new_web_unit} (checked pids: ${pid_list}) is healthy (matching pid confirmed via ${web_health_url})."
        for unit in "${old_web_units[@]}"; do
            if [[ "${unit}" == "${new_web_unit}.service" || "${unit}" == "${new_web_unit}" ]]; then
                continue
            fi
            echo "stopping old instance ${unit}…"
            # See the pipeline block below for why this must be backgrounded
            # with redirected output: this script's stdout is a pipe read
            # until EOF by the deploy stage's subprocess measurement.
            systemctl --user stop "${unit}" >/dev/null 2>&1 &
            disown
        done
    fi
fi

fi # frontend build and web replacement

# --- Pipeline: overlapping-fleet (blue-green) replacement -------------------
# A same-unit `restart` would send SIGTERM to every in-flight stage. As of
# job #627, SIGTERM now means "graceful drain" (see hyqs/pipeline/__main__.py)
# rather than an immediate kill, but that drain can legitimately take up to
# HYQS_PIPELINE_DEPLOY_DRAIN_TIMEOUT (default 2400s/40min) for a long-running
# build/fix stage — restarting the same unit would still make it stop
# claiming new work for that whole window. Instead: start a NEW
# hyqs-pipeline@<generation> instance on the code already on disk (this
# script runs after the new code is checked out, so the new instance boots on
# the new code immediately), confirm it comes up, then ask each OLD instance
# to stop — mapped to the same graceful long drain — without blocking the
# release on that drain finishing. This assumes hosts running the pipeline
# use the templated hyqs-pipeline@.service unit; a host running only the
# singular hyqs-pipeline.service still gets a fresh hyqs-pipeline@<gen>
# started alongside it, and the singular unit is then drained/stopped the
# same way.
mapfile -t old_pipeline_units < <(
    systemctl --user list-units 'hyqs-pipeline*' --state=running --no-legend --plain 2>/dev/null \
        | awk '{print $1}'
)

if [[ ${#old_pipeline_units[@]} -eq 0 ]]; then
    echo "no active hyqs-pipeline* unit found; skipping pipeline blue-green replacement"
else
    # The blue-green swap starts a hyqs-pipeline@<generation> instance, which
    # requires the templated unit to already be installed in
    # ~/.config/systemd/user/ (deploy/README.md documents this as a manual
    # one-time step). Make the release self-sufficient by installing/
    # refreshing it here instead of assuming it was done by hand.
    user_unit_dir="${HOME}/.config/systemd/user"
    template_src="deploy/hyqs-pipeline@.service"
    template_dst="${user_unit_dir}/hyqs-pipeline@.service"
    mkdir -p "${user_unit_dir}"
    if [[ ! -f "${template_dst}" ]] || ! cmp -s "${template_src}" "${template_dst}"; then
        echo "installing/refreshing ${template_dst} from ${template_src}…"
        cp "${template_src}" "${template_dst}"
        systemctl --user daemon-reload
    fi

    generation="$(date +%s)"
    new_pipeline_unit="hyqs-pipeline@${generation}"
    echo "starting ${new_pipeline_unit} on the new code…"
    systemctl --user start "${new_pipeline_unit}"

    # Confirm the new instance has actually done real work before touching the
    # old ones: `systemctl is-active` only proves the process forked (job #630
    # taught us a Type=simple crash-loop still reports 'active'). Instead, wait
    # for the new instance's own MainPID to write a fresh heartbeat row into
    # the shared `workers` table.
    main_pid="$(systemctl --user show -p MainPID --value "${new_pipeline_unit}")"
    if [[ -z "${main_pid}" || "${main_pid}" == "0" ]]; then
        echo "ERROR: ${new_pipeline_unit} has no MainPID; leaving old instance(s) running" >&2
        systemctl --user stop "${new_pipeline_unit}" >/dev/null 2>&1 || true
        exit 1
    fi

    # MainPID is the `uv run` wrapper process; the actual hyqs-pipeline python
    # process (the one that writes heartbeats) is its CHILD, and it forks in
    # a moment *after* `systemctl start` returns. Job #642 resolved the
    # cgroup's pid set exactly once, right here, before the child existed —
    # every poll iteration then matched only the wrapper pid and the gate
    # could never see a heartbeat (job #643). Re-resolve MainPID and the
    # cgroup's pid set on EVERY iteration instead, so once the child forks
    # in it's picked up; this also tolerates MainPID changing out from under
    # us if the unit is crash-looping (Restart=always), in which case pid
    # resolution just keeps chasing new pids that never write a heartbeat
    # and the gate correctly fails once the 90s budget is exhausted.
    pid_list=""
    new_unit_healthy=0
    for _ in $(seq 1 90); do
        current_main_pid="$(systemctl --user show -p MainPID --value "${new_pipeline_unit}" 2>/dev/null || true)"
        pid_list="${current_main_pid}"
        control_group="$(systemctl --user show -p ControlGroup --value "${new_pipeline_unit}" 2>/dev/null || true)"
        if [[ -n "${control_group}" ]]; then
            cgroup_procs_path="/sys/fs/cgroup${control_group}/cgroup.procs"
            if [[ -r "${cgroup_procs_path}" ]]; then
                mapfile -t cgroup_pids < "${cgroup_procs_path}"
                if [[ ${#cgroup_pids[@]} -gt 0 ]]; then
                    pid_list="$(IFS=,; echo "${cgroup_pids[*]}")"
                fi
            fi
        fi
        if [[ -z "${pid_list}" || "${pid_list}" == "0" ]]; then
            sleep 1
            continue
        fi
        if uv run hyqs-pipeline --healthcheck --pid "${pid_list}" >/dev/null 2>&1; then
            new_unit_healthy=1
            break
        fi
        sleep 1
    done
    if [[ "${new_unit_healthy}" -ne 1 ]]; then
        echo "ERROR: ${new_pipeline_unit} (checked pids: ${pid_list:-none}) never wrote a fresh heartbeat; stopping it and leaving old instance(s) running" >&2
        systemctl --user stop "${new_pipeline_unit}" >/dev/null 2>&1 || true
        exit 1
    fi
    echo "${new_pipeline_unit} (checked pids: ${pid_list}) is healthy (fresh heartbeat confirmed)."

    for unit in "${old_pipeline_units[@]}"; do
        if [[ "${unit}" == "${new_pipeline_unit}.service" || "${unit}" == "${new_pipeline_unit}" ]]; then
            continue
        fi
        echo "signaling old instance ${unit} to stop (SIGTERM -> graceful long drain)…"
        # `systemctl stop` sends SIGTERM and blocks up to TimeoutStopSec; run
        # it in the background so the release doesn't wait for the old
        # fleet's in-flight stages to finish — we only need delivery confirmed.
        # This script's own stdout/stderr is a pipe consumed by
        # hyqs.pipeline.resources.measure_subprocess, which reads until EOF;
        # an unredirected background child keeps that pipe open (and the
        # deploy stage blocked) until the child itself exits, so redirect its
        # output away from the parent's inherited fds.
        systemctl --user stop "${unit}" >/dev/null 2>&1 &
        disown
    done
fi

# This marker is host-local by design: putting deploy state in the checkout
# would dirty the shared repository and create further deploy churn. Advance it
# only after every release step above has completed successfully.
if [[ -z "${current_commit}" ]]; then
    current_commit="$(git rev-parse --verify HEAD)"
fi
mkdir -p "${hyqs_data_dir}"
marker_tmp="${release_marker}.tmp.$$"
printf '%s\n' "${current_commit}" > "${marker_tmp}"
mv "${marker_tmp}" "${release_marker}"
