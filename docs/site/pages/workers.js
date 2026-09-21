(function(){
  // ---- fleet sizing ----
  const $=id=>document.getElementById(id);
  const sizing=()=>{
    const s=+$("sSlots").value,p=+$("sProcs").value,h=+$("sHosts").value,r=+$("sRoster").value;
    $("oSlots").textContent=s;$("oProcs").textContent=p;$("oHosts").textContent=h;$("oRoster").textContent=r;
    const slots=s*p*h;$("rSlots").textContent=slots.toLocaleString();
    $("rSup").textContent=`1 + ${p*h-1}`;
    const proj=Math.min(slots,r);$("rProj").textContent=proj;
    $("rBound").textContent=r<slots?"roster":(r>slots?"fleet slots":"both, equal");
  };
  ["sSlots","sProcs","sHosts","sRoster"].forEach(id=>$(id).addEventListener("input",sizing));sizing();

  // ---- roster editor ----
  const TASKS=["plan","build","fix","review","security"];
  const roster=[
    {name:"Coder (Sonnet 5)",provider:"claude",model:"claude-sonnet-5",tasks:new Set(TASKS),cap:1,on:true},
    {name:"Codex Builder",provider:"codex",model:"gpt-5.6-sol",tasks:new Set(TASKS),cap:1,on:true},
    {name:"Reviewer (Opus 5)",provider:"claude",model:"claude-opus-5",tasks:new Set(["review","security"]),cap:2,on:false},
  ];
  const tb=document.querySelector("#rosterTbl tbody"),out=$("rosterOut");
  const render=()=>{
    tb.innerHTML="";
    roster.forEach((a,i)=>{
      const tr=document.createElement("tr");
      tr.innerHTML=`<td>${a.name}</td><td><span class="pill ${a.provider==="claude"?"ai":"det"}">${a.provider}</span></td><td class="mono">${a.model}</td>`+
        TASKS.map(t=>`<td style="text-align:center"><input type="checkbox" data-i="${i}" data-t="${t}" ${a.tasks.has(t)?"checked":""} aria-label="${a.name} ${t}"></td>`).join("")+
        `<td><input type="number" min="1" max="12" value="${a.cap}" data-i="${i}" data-cap style="width:64px;font:inherit;padding:4px 6px;border:1px solid var(--line2);border-radius:4px;background:var(--ground);color:var(--ink)"></td>`+
        `<td style="text-align:center"><input type="checkbox" data-i="${i}" data-on ${a.on?"checked":""} aria-label="${a.name} enabled"></td>`;
      tb.appendChild(tr);
    });
    const per=TASKS.map(t=>{const rows=roster.filter(a=>a.on&&a.tasks.has(t));return {t,cap:rows.reduce((s,a)=>s+a.cap,0),prov:[...new Set(rows.map(a=>a.provider))]};});
    const total=roster.filter(a=>a.on).reduce((s,a)=>s+a.cap,0);
    out.innerHTML=`<div><b>${total}</b><small>parallel jobs for this project (Σ enabled max concurrency)</small></div>`+
      per.map(p=>`<div><b>${p.cap}</b><small>${p.t}: ${p.cap?p.prov.join(" + "):"<span class='rt'>no agent, jobs at this step wait</span>"}</small></div>`).join("");
  };
  tb.addEventListener("change",e=>{const el=e.target,i=+el.dataset.i;if(el.dataset.t){el.checked?roster[i].tasks.add(el.dataset.t):roster[i].tasks.delete(el.dataset.t);}else if(el.hasAttribute("data-cap")){roster[i].cap=Math.max(1,+el.value||1);}else if(el.hasAttribute("data-on")){roster[i].on=el.checked;}render();});
  render();

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
  const stb=document.querySelector("#setTbl tbody"),q=$("setSearch"),cnt=$("setCount");let grp="all";if(!stb){return;}
  const draw=()=>{const s=q.value.trim().toLowerCase();stb.innerHTML="";let n=0;S.forEach(r=>{if(grp!=="all"&&r[3]!==grp)return;if(s&&!(r[0]+" "+r[1]+" "+r[2]).toLowerCase().includes(s))return;n++;const tr=document.createElement("tr");tr.innerHTML=`<td><span class="mono">${r[0]}</span></td><td class="num">${r[1]}</td><td>${r[2]}</td><td><span class="pill det">${r[3]}</span></td>`;stb.appendChild(tr);});cnt.textContent=`${n} of ${S.length} settings shown`;};
  q.addEventListener("input",draw);
  document.querySelectorAll(".search .chipbtn").forEach(b=>b.addEventListener("click",()=>{document.querySelectorAll(".search .chipbtn").forEach(x=>x.setAttribute("aria-pressed","false"));b.setAttribute("aria-pressed","true");grp=b.dataset.g;draw();}));
  draw();
})();
