(function(){
  // ---- permission matrix ----
  const PERMS=["queue_job","cancel_job","retry_job","archive_job","edit_job_deps","resolve_job","propose_backlog","triage_backlog","edit_project","edit_agents","manage_members","manage_api_tokens","manage_webhooks","delete_project","create_project","manage_users","manage_invitations","manage_roles","manage_providers","view_fleet","view_audit","deploy.view","deploy.promote","deploy.offer","deploy.promote_prod","deploy.apply"];
  const ROLES={
    viewer:{d:"Read a project, propose backlog items, see deployments.",p:["propose_backlog","deploy.view"]},
    contributor:{d:"File, cancel, retry, archive and resolve jobs; edit dependencies; propose to the backlog.",p:["queue_job","cancel_job","retry_job","archive_job","edit_job_deps","resolve_job","propose_backlog"]},
    project_admin:{d:"Contributor plus project settings, roster, members, backlog triage, webhooks, API tokens, promote and offer releases.",p:["queue_job","cancel_job","retry_job","archive_job","edit_project","edit_agents","edit_job_deps","resolve_job","manage_members","propose_backlog","triage_backlog","manage_api_tokens","manage_webhooks","deploy.promote","deploy.offer"]},
    platform_admin:{d:"Everything, including users, invitations, roles, providers, fleet, audit, production promotion and apply. Five of these cannot be removed from the role.",p:PERMS},
    automation_client:{d:"Unattended orchestration: file, cancel, retry, resolve, edit dependencies. Deliberately no archive.",p:["queue_job","cancel_job","retry_job","edit_job_deps","resolve_job"]},
    release_manager:{d:"Promote and offer releases to non-production environments.",p:["deploy.promote","deploy.offer","deploy.view"]},
    prod_promoter:{d:"Release manager plus promotion to production.",p:["deploy.promote","deploy.offer","deploy.view","deploy.promote_prod"]},
    client_approver:{d:"Apply an offered release on the client's own host. Environment-scoped.",p:["deploy.apply","deploy.view"]},
  };
  const m=document.getElementById("matrix");
  if(m){
    let h='<table><thead><tr><th>Permission</th>'+Object.keys(ROLES).map(r=>`<th title="${ROLES[r].d}" style="cursor:help">${r}</th>`).join("")+'</tr></thead><tbody>';
    PERMS.forEach(p=>{h+=`<tr><td class="mono">${p}</td>`+Object.keys(ROLES).map(r=>`<td class="${ROLES[r].p.includes(p)?"y":"n"}">${ROLES[r].p.includes(p)?"✓":"·"}</td>`).join("")+"</tr>";});
    m.innerHTML=h+"</tbody></table>";
  }

  // ---- settings explorer ----
  const S=[
    ["HYQS_PIPELINE_CONCURRENCY","1","Jobs one process runs in parallel; live fleet uses 8","capacity"],
    ["HYQS_PIPELINE_LEASE_TTL","90 s","Lease a worker renews while a stage runs; expired means the worker died and the job is reclaimed","capacity"],
    ["HYQS_PIPELINE_STAGE_TIMEOUT","1800 s","Hard wall-clock cap on one AI stage","capacity"],
    ["HYQS_PIPELINE_MERGE_LOCK_TTL","300 s","Safety net for a crashed holder of a repository's merge lock","capacity"],
    ["HYQS_PIPELINE_SCHEMA_LOCK_TTL","300 s","Safety net for the per-project schema lock held merge→deploy","capacity"],
    ["HYQS_PIPELINE_DRAIN_TIMEOUT","300 s","Operator drain (SIGUSR1): finish in-flight stages, then stop","capacity"],
    ["HYQS_PIPELINE_DEPLOY_DRAIN_TIMEOUT","2400 s","Release drain (first SIGTERM) so a mid-flight build survives a self-deploy","capacity"],
    ["HYQS_PIPELINE_DISABLED","off","Run the pipeline only as standalone workers, not inside the chat daemon","capacity"],
    ["HYQS_PIPELINE_MAX_ATTEMPTS","5","Fix→retest→re-review rounds before a job is handed to a person; overridable per project","budgets"],
    ["HYQS_PIPELINE_MAX_TIMEOUTS","2","Stage-timeout retries before failing instead of reclaiming forever","budgets"],
    ["HYQS_PIPELINE_LIMIT_BACKOFF","600 s","Pause when a provider is rate-limited without a reset time; max pause 6 h","budgets"],
    ["(code) rebase attempts","5","Merge-conflict fixes, backoff 30 s doubling to 10 min, separate from fix rounds","budgets"],
    ["(code) DB transient retries","3","Deadlocks and serialization failures, no budget spent","budgets"],
    ["(code) in-lock base advances","3","Clean base advances absorbed under the merge lock before re-verification","budgets"],
    ["(code) phantom-conflict recoveries","3","Force-refresh and re-poll when GitHub says not mergeable but the local merge is clean","budgets"],
    ["HYQS_MODEL","sonnet","Default Claude model for chat and, unless overridden, the pipeline","providers"],
    ["HYQS_PIPELINE_MODEL","(empty)","Pipeline default model when no agent row pins one","providers"],
    ["HYQS_PERMISSION_MODE","acceptEdits","How unattended the chat agent runs; the pipeline sets its own per role","providers"],
    ["HYQS_CODEX_BIN","codex","Path to the Codex CLI","providers"],
    ["HYQS_CODEX_MODEL","(empty)","Codex model; empty delegates to the authenticated CLI","providers"],
    ["HYQS_CODEX_ARGS","exec --full-auto","Codex CLI arguments","providers"],
    ["ANTHROPIC_API_KEY","(unset)","API key; unset means the logged-in Claude subscription is used","providers"],
    ["HYQS_DEPLOY_CMD","(empty)","Shell deploy command; a repo's deploy/release.sh takes precedence","deploy"],
    ["HYQS_GIT_PUSH","off","Push after a local merge for repositories without a GitHub remote","deploy"],
    ["HYQS_HOST_NAME","(empty)","This worker's enrolled host; required for --deployer, pins deploy steps","deploy"],
    ["HYQS_PROJECTS_DIR","~/projects","Where provisioned project repositories live","deploy"],
    ["HYQS_DATA_DIR","~/.hyqs/data","Worktrees, caches, deployer state; refused if inside a git repository","deploy"],
    ["HYQS_CREDENTIAL_ENCRYPTION_KEY","(required for Slack)","Fernet key for stored bot tokens; no key, no encryption, no silent plaintext","deploy"],
    ["HYQS_AUTO_DEPLOY","off","Supervisor poller that files a deploy-only job when origin moves ahead of the deployed commit; live fleet on","supervisor"],
    ["HYQS_AUTO_DEPLOY_INTERVAL","120 s","Poller cadence","supervisor"],
    ["HYQS_AUTO_DEPLOY_MAX_ATTEMPTS","3","Deploy jobs filed per unchanged origin tip","supervisor"],
    ["HYQS_PIPELINE_JOB_STALE_HOURS","24","Age at which an active job triggers an alert","supervisor"],
    ["HYQS_PIPELINE_INCIDENT_ANALYST_MODEL","sonnet","Model the incident analyst diagnoses with","supervisor"],
    ["HYQS_PIPELINE_INCIDENT_ANALYST_PROVIDER","auto","Prefer Claude, fall back to any healthy provider, or pin one","supervisor"],
    ["HYQS_PIPELINE_INCIDENT_ANALYST_SCAN_BUDGET","8","Analyst diagnoses per janitor scan","supervisor"],
    ["HYQS_PIPELINE_INCIDENT_ANALYST_JOB_CAP","6","Times any single job may ever be diagnosed","supervisor"],
    ["HYQS_PIPELINE_INCIDENT_ANALYST_PREDEADLETTER_DISABLED","off","Skip the one analyst look before a rule dead-letters a job","supervisor"],
    ["HYQS_LOG_FORMAT","json","One JSON line per record with job_id, stage, project_id bound; console for local dev","web"],
    ["HYQS_WEB_HOST / HYQS_WEB_PORT","0.0.0.0 / 8787","Console and API bind address","web"],
    ["HYQS_WEB_TOKEN","(empty)","Static bearer for the API, resolves to platform admin","web"],
    ["HYQS_WEB_BASE_URL","(empty)","Public console URL used in Slack deep links","web"],
    ["HYQS_MCP_RESOURCE_URL","(empty)","Public /mcp URL for OAuth discovery; needs the Google redirect registered","web"],
    ["HYQS_GOOGLE_CLIENT_ID / _SECRET","(empty)","Google single sign-on, shared by the console and MCP OAuth","web"],
    ["HYQS_APPLE_*","(empty)","Sign in with Apple: client id, team id, key id, private key","web"],
    ["HYQS_DB_URL (or DATABASE_URL)","(required)","The shared database; every worker, the console and the supervisor use it","web"],
    ["HYQS_REGISTRY_DATA_DIR / _HTPASSWD / _PORT","(required) / (required) / 5100","Zot registry storage, bcrypt credentials, loopback port behind nginx","registry"],
    ["HYQS_REGISTRY_PUSH_ENABLED, _DOMAIN, _PUSH_USER, _PUSH_PASS","(build host only)","Push credentials; must never be set on a deploy or client host","registry"],
    ["HYQS_COSIGN_KEY_PATH / _PASSWORD","(build host only)","Signing key for release images","registry"],
    ["HYQS_REGISTRY_PULL_ENABLED, _PULL_USER, _PULL_PASS","(deploy host only)","Pull-only credentials","registry"],
    ["HYQS_COSIGN_PUBLIC_KEY_PATH","(deploy host only)","Verification key; verify fails closed if unset","registry"],
  ];
  const stb=document.querySelector("#setTbl tbody"),q=document.getElementById("setSearch"),cnt=document.getElementById("setCount");
  if(stb){
    let grp="all";
    const draw=()=>{const s=q.value.trim().toLowerCase();stb.innerHTML="";let n=0;S.forEach(r=>{if(grp!=="all"&&r[3]!==grp)return;if(s&&!(r[0]+" "+r[1]+" "+r[2]).toLowerCase().includes(s))return;n++;const tr=document.createElement("tr");tr.innerHTML=`<td><span class="mono">${r[0]}</span></td><td class="num">${r[1]}</td><td>${r[2]}</td><td><span class="pill det">${r[3]}</span></td>`;stb.appendChild(tr);});cnt.textContent=`${n} of ${S.length} settings shown`;};
    q.addEventListener("input",draw);
    document.querySelectorAll("#settings .chipbtn").forEach(b=>b.addEventListener("click",()=>{document.querySelectorAll("#settings .chipbtn").forEach(x=>x.setAttribute("aria-pressed","false"));b.setAttribute("aria-pressed","true");grp=b.dataset.g;draw();}));
    draw();
  }

  // ---- MCP catalogue ----
  const T=[
    ["list_projects","Projects visible to the caller","authenticated","read"],["list_jobs","Paged jobs with cursor and summary mode","member","read"],["get_job","One job, optionally reduced","member","read"],["list_epics","Epics for a project","member","read"],
    ["job_events","Stage timeline of a job","member","read"],["job_supervisor_events","Supervisor remediation timeline","member","read"],["watch_job","Stream stage transitions until terminal","member","read"],["job_diff","Branch diff against the default branch","member","read"],
    ["survey_job_queue","Pre-flight file-collision check of candidate jobs","member","read"],["project_performance","Headline and per-stage statistics","member","read"],["list_agents","The project roster","member","read"],["agent_stats","Cost, duration and fix rate per agent","member","read"],["get_project_spec","Specification and configuration","member","read"],["list_webhooks","Notification subscriptions","manage_webhooks","read"],
    ["create_job","File one job: dedup, idempotency key, remediation link","queue_job","jobs"],["create_job_wave","Atomic wave, auto-chained on file overlap","queue_job","jobs"],["cancel_job","Cancel pending or running","cancel_job","jobs"],["retry_job","Re-run from plan; force bypasses the scope gate","retry_job","jobs"],["requeue_job","Re-run from a named stage","resolve_job","jobs"],["resolve_job","Mark failed or cancelled as resolved","resolve_job","jobs"],["fix_forward_job","File a linked remediation, optionally re-point dependents","resolve_job","jobs"],["update_job","Title, idea, priority, epic","edit_job_deps","jobs"],["archive_job","Tear down artefacts","member","jobs"],["unarchive_job","Restore","member","jobs"],["add_job_dependency","Add an edge, cycle-checked fleet-wide","edit_job_deps","jobs"],["remove_job_dependency","Remove an edge","edit_job_deps","jobs"],["get_job_dependency_graph","The full graph","member","jobs"],
    ["create_epic","New epic","member","plan"],["update_epic","Edit an epic","member","plan"],["add_backlog_item","Propose to the backlog","propose_backlog","plan"],
    ["register_webhook","Subscribe a URL or Slack channel","manage_webhooks","ops"],["set_webhook_active","Enable or disable","manage_webhooks","ops"],["delete_webhook","Remove","manage_webhooks","ops"],["set_slack_credential","Store a bot token, never echoed","manage_webhooks","ops"],["promote_release","Push a release to an environment; prod needs deploy.promote_prod","deploy.promote","ops"],["offer_release","Make a release available for a client approver to apply","deploy.offer","ops"],["update_project","Configuration and roster","edit_project","ops"],["resync_nginx_vhost","Re-apply the vhost from configuration","edit_project","ops"],
  ];
  const tb=document.querySelector("#mcpTbl tbody"),mq=document.getElementById("mcpSearch"),mc=document.getElementById("mcpCount");
  if(tb){
    let k="all";
    const draw=()=>{const s=mq.value.trim().toLowerCase();tb.innerHTML="";let n=0;T.forEach(r=>{if(k!=="all"&&r[3]!==k)return;if(s&&!(r[0]+" "+r[1]).toLowerCase().includes(s))return;n++;const tr=document.createElement("tr");tr.innerHTML=`<td><span class="mono">${r[0]}</span></td><td>${r[1]}</td><td><span class="pill det">${r[2]}</span></td>`;tb.appendChild(tr);});mc.textContent=`${n} of ${T.length} tools`;};
    mq.addEventListener("input",draw);
    document.querySelectorAll("#mcp .chipbtn").forEach(b=>b.addEventListener("click",()=>{document.querySelectorAll("#mcp .chipbtn").forEach(x=>x.setAttribute("aria-pressed","false"));b.setAttribute("aria-pressed","true");k=b.dataset.k;draw();}));
    draw();
  }
})();
