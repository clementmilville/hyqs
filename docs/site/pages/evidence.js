(function(){
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
  const m=document.getElementById("matrix");if(m){
  let h='<table><thead><tr><th>Permission</th>'+Object.keys(ROLES).map(r=>`<th title="${ROLES[r].d}">${r}</th>`).join("")+'</tr></thead><tbody>';
  PERMS.forEach(p=>{h+=`<tr><td class="mono">${p}</td>`+Object.keys(ROLES).map(r=>`<td class="${ROLES[r].p.includes(p)?"y":"n"}">${ROLES[r].p.includes(p)?"✓":"·"}</td>`).join("")+"</tr>";});
  h+="</tbody></table>";m.innerHTML=h;
  m.querySelectorAll("th[title]").forEach((th,i)=>{th.style.cursor="help";});}
  const Q=[
    ["Who changed this job, project, user or permission, when, and from what to what?","audit_log plus created_by and updated_by on the row","GET /api/admin/audit?table=&row_pk= (view_audit); Admin → Access → Audit","Per-column old and new values, truncated to 300 characters each."],
    ["Was the change made by a person, a worker, the supervisor, or out-of-band SQL?","audit_log.actor prefix","same","user:, apitoken:, mcp:, worker:, supervisor:, system:, db:"],
    ["Can the actor string be trusted?","audit_log.backend_pid, client_addr, backend_start","API only, not rendered yet","Observed by the database about the connection, independent of what the application claims."],
    ["Who filed this job and through which channel?","jobs.source, source_actor, source_meta","GET /api/jobs/filed-by-breakdown (view_audit); job detail Overview","Client address, browser and role are kept for console filings."],
    ["What did this job cost, by stage, model and provider?","usage","GET /api/jobs/{id}/usage; Cost tab","Four token classes priced per model; cost from the SDK when it reports one."],
    ["What did each stage do, how long, at what cost?","job_events with structured detail","GET /api/jobs/{id}/events; MCP job_events; Stages tab","Plan stories, commits and patches, test output, gate findings and verdicts."],
    ["What hardware did it use?","resource_usage","GET /api/jobs/{id}/resources; Hardware tab","cgroup v2 via a transient systemd scope per subprocess."],
    ["What automated remediation touched it?","supervisor_events","GET /api/jobs/{id}/events; MCP job_supervisor_events; Resolution tab","Classification, requeues, filed fix jobs, dead-letters, escalations."],
    ["Why was this change made?",".hyqs/decisions/<job>-<slug>.md in the repository","GET /api/projects/{id}/decisions; History → Changelog → Why","Committed on merge, versioned in your own git history."],
    ["What is live in production and what triggered it?","deploys register","GET /api/projects/{id}/deploy-status, /changelog; Overview badge","Commit, previous commit, trigger, verified, image digest, signed."],
    ["Is production behind the main branch?","live origin comparison against deploys","GET /api/projects/{id}/deploy-status → is_stale; Overview","The supervisor also alerts after six hours of drift."],
    ["How reliable is the automation itself?","jobs filtered to auto-deploy operations","GET /api/deployment-reliability (view_audit); Usage and cost","Total, done, failed, cancelled, failure rate per project."],
    ["Delivery rate, cycle time, slowest stage?","job_events and jobs","/performance/headline, /stage-stats, /slowest-jobs; MCP project_performance; Analytics","p50 and p95 per stage; operational jobs excluded by default and stated."],
    ["Who visited the public site?","page_views, no IP stored","GET /api/admin/page-views (view_audit); Usage and cost","Daily-rotating salted hash; country and city from a local database."],
    ["What ran at 03:00 on host X?","JSON logs with job_id, stage, project_id bound; workers heartbeats","journald; GET /api/workers","One JSON line per record from every process."],
  ];
  const qa=document.getElementById("qa");
  qa.innerHTML=Q.map(([q,r,a,n])=>`<details><summary>${q}</summary><div class="body"><dl><dt>Record</dt><dd>${r}</dd><dt>Access</dt><dd><span class="mono">${a}</span></dd><dt>Note</dt><dd>${n}</dd></dl></div></details>`).join("");
})();
