'use strict';
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const short = (s,n=160) => {s=String(s??'');return esc(s.length>n?s.slice(0,n).trim()+'…':s)};
const label = s => String(s??'unknown').replaceAll('_',' ');
const badge = (s,kind='task') => `<button type="button" class="badge status-badge ${esc(/^[a-z_]+$/.test(s)?s:'')}" data-help-status="${esc(s)}" data-help-kind="${esc(kind)}" aria-label="Explain ${esc(STATUS_HELP[kind]?.title||'record')} status: ${esc(label(s))}" title="${esc(statusExplanation(kind,s))}">${esc(label(s))} <span aria-hidden="true">ⓘ</span></button>`;
const timestamp = t => typeof t==='number'?t*1000:Date.parse(t);
const date = t => {const d=new Date(timestamp(t));return Number.isNaN(+d)?'Time not recorded':d.toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})};
const when = r => timestamp(r.updated||r.finished||r.created||r.time||r.started)||0;
const newest = rows => [...rows].sort((a,b)=>when(b)-when(a));
const empty = text => `<div class="empty">${esc(text)}</div>`;
const section = (title,body) => body?`<section class="detail-section"><h2>${esc(title)}</h2>${body}</section>`:'';
const prose = text => text?`<div class="prose">${esc(typeof text==='string'?text:JSON.stringify(text,null,2))}</div>`:'';
const bullets = items => items?.length?`<ul>${items.map(x=>`<li>${esc(typeof x==='string'?x:JSON.stringify(x))}</li>`).join('')}</ul>`:'';
const action = (type,id,text) => `<button class="link-button" data-type="${esc(type)}" data-id="${esc(id)}">${esc(text)}</button>`;
const finished = new Set(['accepted','completed','done','cancelled','rejected']);
const waiting = new Set(['queued','deferred','partial','blocked','needs_resources','waiting_children','waiting_jobs','awaiting_review']);
const pages=[['overview','◈','Overview'],['story','▧','Research story'],['notes','✎','Research notes'],['chat','◌','Research chat'],['work','▤','Work'],['conversations','◌','Conversations'],['experiments','⌁','Experiments'],['library','▧','Library'],['server','▥','Server']];
let usageRefreshPending=0, usageRefreshMessage='';
const selectedStoryPdfs=new Set();
const expandedDigestPapers=new Set();
const defaultFilter = view => view==='library'?'digests':'all';
let pdfDownloading=false, pdfDownloadMessage='';
let db=null, projectId='', page='overview', query='', filter='all', dialogRecord=null, lastSuccess=0, refreshing=false;
const project = () => db.projects.find(p=>p.id===projectId);
const rows = kind => (db[kind]||[]).filter(r=>r.project===projectId);
const task = id => db.tasks.find(t=>t.id===id);
const tokenCount = n => new Intl.NumberFormat(undefined,{notation:'compact',maximumFractionDigits:1}).format(n);
function specialistUsage(role){
 const assignments=rows('tasks').filter(t=>t.role===role), ids=new Set(assignments.map(t=>t.id));
 const sessions=rows('sessions').filter(s=>s.role===role||ids.has(s.task));
 const measured=sessions.filter(s=>s.usage&&Object.keys(s.usage).length);
 const total=key=>measured.reduce((n,s)=>n+(s.usage[key]||0),0);
 const seconds=sessions.reduce((n,s)=>n+(s.seconds??(s.started?Math.max(0,(s.finished||((['running','stopping'].includes(s.status))?db.fetched_at:s.started))-s.started):0)),0);
 const helpers=rows('helpers').filter(h=>ids.has(h.task));
 const jobs=rows('jobs').filter(j=>ids.has(j.task));
 const metric=(name,value)=>`<div><dt>${esc(name)}</dt><dd>${esc(value)}</dd></div>`;
 return `<dl class="specialist-metrics">${metric('CLI time',seconds>=3600?`${(seconds/3600).toFixed(1)} h`:`${Math.round(seconds/60)} min`)}${metric('CLI sessions',`${sessions.length} · ${sessions.filter(s=>['running','stopping'].includes(s.status)).length} active`)}${metric('Input / output tokens',measured.length?`${tokenCount(total('input_tokens'))} / ${tokenCount(total('output_tokens'))}`:'Not recorded')}${metric('Cache read / write tokens',measured.length?`${tokenCount(total('cache_read_input_tokens'))} / ${tokenCount(total('cache_creation_input_tokens'))}`:'Not recorded')}${metric('Recorded sub-agents',`${helpers.length} · ${helpers.filter(h=>h.status==='running').length} active`)}${metric('Experiment jobs',`${jobs.length} · ${jobs.filter(j=>j.status==='running').length} running`)}</dl><small>Project lifetime · CLI wall time summed across sessions; excludes experiment runtime. Tokens recorded for ${measured.length}/${sessions.length} sessions; active sessions may be incomplete. CLI totals include reported native-helper usage. Sub-agent counts reflect saved helper records.</small>`;
}
function card(type,r,title,description,meta){
 return `<article class="record" data-type="${esc(type)}" data-id="${esc(r.id)}"><div class="record-top"><span class="role">${esc(meta||r.role||type)}</span>${badge(r.status||r.read_depth||'recorded',({task:'task',report:'report',job:'job',paper:'paper',wake:'wake',request:'request',meeting:'meeting',thread:'conversation'})[type]||'record')}</div><h3><button type="button" class="record-open">${short(title,180)}</button></h3>${description?`<p>${short(description,170)}</p>`:''}<p class="meta">${date(r.updated||r.created||r.time||r.at)}</p></article>`;
}
const taskCard = t => card('task',t,t.goal,t.launch_hold?.reason||t.summary||t.defer_reason||(t.depends?.length?`${t.depends.length} recorded dependencies`:''),t.role);
const reportCard = r => card('report',r,r.summary||'Research report',r.findings?.[0],`${r.role} report`);
const digestCard = d => `<article class="record digest-card" data-type="digest" data-id="${esc(d.id)}"><div class="record-top"><span class="role">${esc(d.topic)}</span><span class="digest-period">${esc(d.period_start)} – ${esc(d.period_end)}</span></div><h3><button type="button" class="record-open">${esc(d.title)}</button></h3><p>${esc(d.summary)}</p><p class="meta">Weekly digest · Issue ending ${esc(d.period_end)} · Revision ${esc(d.version)}</p></article>`;
const jobCard = j => card('job',j,j.name,`${j.hours??'?'}h allocation · ${j.gpus||0} GPUs${j.gpus?' · '+(j.gpu_mode||'exclusive')+(j.gpu_mode==='shared'?' · '+j.gpu_memory_mib+' MiB reserved/GPU':''):''} · ${j.cpu_threads??'auto'} CPU threads${j.launch_hold?.reason?' · '+j.launch_hold.reason:''}`,'Experiment');
const panel = (title,items,body) => `<section class="panel"><div class="panel-heading"><h2>${esc(title)}</h2><span class="count">${items.length}</span></div>${body||empty('Nothing here yet.')}</section>`;
function accountUsage(){
 const accounts=db.accounts||[];
 const names={five_hour:'Five-hour',seven_day:'Weekly',seven_day_opus:'Weekly Opus',seven_day_sonnet:'Weekly Sonnet',seven_day_oauth_apps:'Weekly Claude Code / apps'};
 return `<section class="subscription-section" aria-labelledby="subscription-title"><div class="panel-heading"><h2 id="subscription-title">Claude accounts</h2><button class="help-button" data-refresh-usage ${usageRefreshPending?'disabled':''}>${usageRefreshPending?'Refreshing usage…':'Refresh account usage'}</button></div><p class="meta" role="status">${esc(usageRefreshMessage||'Account usage is checked as needed for active or pending agent work, at most about every five minutes. Idle readings can become stale. Refresh account usage requests a new provider check; page refresh reloads saved records.')}</p><p class="text-muted">New sessions use the available Claude account with the most remaining capacity for their model. Running sessions keep their account.</p><div class="account-grid">${accounts.map(a=>{
  const state=a.auth_required?'login required':a.renewal_needed?'renewal needed':a.cooldown_until?'cooldown':a.status==='ok'&&!a.metadata_error?'connected':'connection unknown';
  const windows=Object.entries(a.windows||{});
  return `<article class="account-card"><div class="record-top"><h3>${esc(a.id)}</h3>${badge(state.replaceAll(' ','_'),'account')}</div><p class="meta">${a.active_sessions} active session${a.active_sessions===1?'':'s'} · ${esc(a.status==='ok'?'Fresh usage':a.status==='stale'?'Stale usage':'Usage unavailable')}</p>${a.cooldown_until?`<p>Cooldown until ${date(a.cooldown_until)}</p>`:''}${windows.length?windows.map(([name,w])=>{
   const resetPassed=w.resets_at&&timestamp(w.resets_at)<=Date.now();
   const remaining=Number(w.remaining_percentage), used=Number(w.used_percentage);
   return `<div class="usage-window"><div class="record-top"><strong>${esc(names[name]||label(name))}</strong><span>${esc(used.toFixed(1))}% used</span></div><progress class="usage-progress ${a.status!=='ok'||resetPassed?'usage-stale':''}" max="100" value="${used}" aria-label="${esc(a.id+' '+(names[name]||name)+' usage')}">${used}%</progress><small>${remaining.toFixed(1)}% remaining · ${resetPassed?'Reset passed; awaiting updated usage':w.resets_at?'Resets '+date(w.resets_at):'Reset time unavailable'}</small></div>`;
  }).join(''):empty('No usage snapshot yet.')}${a.metadata_error?`<p class="text-muted">${a.renewal_needed?'Access token expired. Usage refresh cannot renew it; this does not prove you are signed out.':'Latest usage refresh failed. Previous readings are retained.'}</p>`:''}${a.renewal_needed?`<button class="help-button" data-renew-account="${esc(a.id)}" ${a.renewal?.status==='running'?'disabled':''}>Renew connection · one tiny Haiku request</button>`:''}${a.renewal?`<p class="meta">${esc(a.renewal.message)}</p>`:''}<p class="meta">${a.observed_at?'Usage checked '+date(a.observed_at)+' · '+Math.floor(a.age_seconds/60)+' min ago':'Usage has not been fetched yet.'}</p></article>`;
 }).join('')||empty('Account information will appear after the lab records subscription usage.')}</div><p class="meta">Account-wide subscription snapshots include usage outside this lab. Percentages are approximate capacity indicators; stale readings do not determine which account has more capacity.</p></section>`;
}
function serverStatus(){
 const s=db.server||{status:'unavailable'}, cpu=s.cpu||{}, mem=s.memory||{}, gpus=s.gpus||[];
 const number=(v,suffix='')=>typeof v==='number'&&Number.isFinite(v)?esc(v.toFixed(1))+suffix:'Unavailable';
 const gib=v=>number(typeof v==='number'?v/1073741824:null,' GiB');
 const meter=(value,name)=>typeof value==='number'&&Number.isFinite(value)?`<progress class="usage-progress ${s.status==='ok'?'':'usage-stale'}" max="100" value="${Math.max(0,Math.min(100,value))}" aria-label="${esc(name)}">${value}%</progress>`:'';
 return `<section class="subscription-section" aria-labelledby="server-title"><div class="panel-heading"><h2 id="server-title">Server resources</h2>${badge(s.status==='ok'?'live':s.status,'server')}</div><p class="meta">${s.observed_at?'Measured '+date(s.observed_at)+' · '+s.age_seconds+' seconds ago':'Waiting for the daemon to record server telemetry.'} · Across all projects and other server activity</p><div class="account-grid"><article class="account-card"><h3>CPU</h3><p><strong>${number(cpu.utilization_percent,'%')}</strong> utilization${s.status==='ok'&&cpu.utilization_percent===null?' · collecting the first sample':''}</p>${meter(cpu.utilization_percent,'CPU utilization')}<p>${esc(cpu.threads??'Unknown')} logical CPU threads · ${esc(cpu.lab_reserved_threads??'Unknown')} reserved by lab jobs</p><p>${esc(cpu.schedulable_threads??'Unknown')} threads eligible for new lab jobs (load-based estimate)</p><small class="text-muted">Load average, 1 / 5 / 15 min: ${(cpu.load_average||[]).map(v=>number(v)).join(' / ')||'Unavailable'}. Load is not a utilization percentage.</small></article><article class="account-card"><h3>System RAM</h3><p><strong>${gib(mem.used_bytes)}</strong> in use / ${gib(mem.total_bytes)}</p>${meter(mem.total_bytes?100*mem.used_bytes/mem.total_bytes:null,'System RAM utilization')}<p>${gib(mem.available_bytes)} available</p><small class="text-muted">Usage is total RAM minus Linux's available-memory estimate.</small></article></div>${s.cpu_error?`<p class="text-muted">${esc(s.cpu_error)}</p>`:''}<div class="account-grid section-space">${gpus.map(g=>`<article class="account-card"><div class="record-top"><h3>GPU ${esc(g.index)} · ${esc(g.name)}</h3>${badge(g.lab_reserved?'lab_reserved':g.available_for_lab?'available':'unavailable','gpu')}</div><div class="usage-window"><div class="record-top"><strong>GPU utilization</strong><span>${number(g.utilization_percent,'%')}</span></div>${meter(g.utilization_percent,'GPU '+g.index+' utilization')}</div><div class="usage-window"><div class="record-top"><strong>GPU memory</strong><span>${number(g.memory_used_mib/1024,' GiB')} / ${number(g.memory_total_mib/1024,' GiB')}</span></div>${meter(g.memory_total_mib?100*g.memory_used_mib/g.memory_total_mib:null,'GPU '+g.index+' memory')}<small>${number(g.memory_free_mib/1024,' GiB')} free${g.temperature_c!==null&&g.temperature_c!==undefined?' · '+number(g.temperature_c,' °C'):''}</small></div><p class="meta">Shared jobs: ${esc((g.shared_jobs||[]).length)} · ${number((g.reserved_memory_mib||0)/1024,' GiB')} reserved<br>${g.shared_available?'Shared admission: up to '+number(g.shared_available_mib/1024,' GiB')+' for another job':esc((g.shared_unavailable_reasons||[]).join(' · ')||'Sharing unavailable')}</p><div>${(db.jobs||[]).filter(j=>['running','stopping'].includes(j.status)&&j.allocation?.gpus?.includes(g.uuid)).map(j=>`<p>${action('job',j.id,j.name)} · ${esc(j.gpu_mode||'exclusive')}${j.gpu_mode==='shared'?' · '+esc(j.gpu_memory_mib)+' MiB reserved':''}</p>`).join('')}</div><p class="meta">${g.available_for_lab?'Eligible for a new exclusive GPU job at this measurement':esc((g.unavailable_reasons||[]).join(' · ')||'Availability unknown')}</p></article>`).join('')||empty(s.gpu_error||'No GPU telemetry available.')}</div>${s.gpu_error&&gpus.length?`<p class="error">${esc(s.gpu_error)}</p>`:''}<p class="meta">GPU availability follows the lab's allocation rules; low utilization alone does not make a GPU available. ${s.status==='stale'?'These readings are stale; current hardware use may differ.':''}</p></section>`;
}
function overview(){
 const p=project(), tasks=rows('tasks'), jobs=rows('jobs'), reports=newest(rows('reports'));
 const liveSessions=rows('sessions').filter(s=>['running','stopping'].includes(s.status));
 const liveTaskIds=new Set(liveSessions.map(s=>s.task).filter(Boolean));
 const active=tasks.filter(t=>['running','stopping'].includes(t.status)||liveTaskIds.has(t.id)).map(t=>liveTaskIds.has(t.id)?{...t,status:'running',launch_hold:null}:t), upcoming=tasks.filter(t=>waiting.has(t.status)&&!liveTaskIds.has(t.id));
 const otherSessions=liveSessions.filter(s=>!s.task||!tasks.some(t=>t.id===s.task));
 const runningJobs=jobs.filter(j=>['running','stopping'].includes(j.status));
 const activeJobs=jobs.filter(j=>['running','queued','approval'].includes(j.status));
 const done=tasks.filter(t=>['accepted','completed'].includes(t.status));
 const attention=[...jobs.filter(j=>j.status==='approval').map(j=>action('job',j.id,`Approval requested: ${j.name}`)),...tasks.filter(t=>['blocked','needs_resources'].includes(t.status)).map(t=>action('task',t.id,`${label(t.status)}: ${t.goal}`))];
 const scheduler=rows('scheduler')[0], budget=scheduler?.budget_usage;
 const wakes=rows('wakeups').filter(w=>w.status==='pending');
 const requests=rows('requests').filter(r=>['queued','running'].includes(r.status));
 return `<div class="eyebrow">YOUR RESEARCH, IN VIEW</div><div class="title-row"><h1>${esc(p.name)}</h1>${badge(p.status,'project')}</div><p class="subtitle">${esc(p.purpose||'A window into the work behind your research.')}</p>
 <div class="stats">${[['IN PROGRESS',active.length,'active specialist assignments'],['COMPLETED',done.length,'completed or accepted assignments'],['EXPERIMENTS',jobs.length,`${activeJobs.length} running, queued or awaiting approval`],['PAPERS',papers().length,'linked to this project']].map(([title,num,caption])=>`<div class="stat"><span class="eyebrow">${title}</span><strong>${num}</strong><small>${esc(caption)}</small></div>`).join('')}</div>
 ${attention.length?`<div class="attention"><h2>Needs your attention</h2>${attention.map(x=>`<p>${x}</p>`).join('')}<p>Use the existing Slack discussion for approvals and instructions.</p></div>`:''}
 ${budget?`<div class="notice"><strong>Daily model-session budget</strong> · Project ${budget.project_used_minutes.toFixed(1)} / ${budget.project_limit_minutes} min · All projects ${budget.global_used_minutes.toFixed(1)} / ${budget.global_limit_minutes} min<br>Resets ${date(budget.resets_at)} · ${esc(budget.timezone)} accounting. Includes active session reservations. Snapshot ${date(scheduler.updated)}.</div>`:''}
 ${db.agent_backend?.provider?`<p class="meta"><strong>Agent backend:</strong> ${esc(db.agent_backend.provider)}${db.agent_backend.model?` · ${esc(db.agent_backend.model)} / ${esc(db.agent_backend.effort)}`:""}. </p>`:""}
 ${accountUsage()}
 ${serverStatus()}
 <div class="grid-three">${panel('Happening now',[...active,...otherSessions,...runningJobs],active.map(taskCard).join('')+otherSessions.map(s=>`<div class="session-row panel-body">${badge(s.status,'session')} <strong>${esc(label(s.role))}</strong><p>${esc(s.model)} / ${esc(s.effort)}${s.account?' · Account: '+esc(s.account):''} · active model session</p></div>`).join('')+runningJobs.map(jobCard).join('')||empty('No agent session or experiment is currently running. Queued work has not started.'))}${panel('Coming next',[...upcoming,...wakes],upcoming.sort((a,b)=>b.priority-a.priority).map(taskCard).join('')+wakes.map(w=>card('wake',w,w.reason,`Scheduled ${date(w.at)}`,'Scheduled review')).join(''))}${panel('Recent reports',reports,reports.slice(0,4).map(reportCard).join(''))}</div>
 ${activeJobs.length?`<div class="notice">Experiments: ${runningJobs.length} running · ${jobs.filter(j=>j.status==='queued').length} queued · ${jobs.filter(j=>j.status==='approval').length} awaiting approval. An active engineer session does not mean GPU training has started.</div>`:''}
 ${requests.length?`<div class="notice">${requests.length} supervisor request${requests.length===1?'':'s'} queued or running. ${requests.map(r=>action('request',r.id,r.reason||'Supervisor response')).join(' · ')}</div>`:''}
 <div class="specialists">${['engineer','literature','verifier'].map(role=>{const ts=tasks.filter(t=>t.role===role);const current=ts.find(t=>t.status==='running')||newest(ts)[0];return `<div class="specialist"><div class="record-top"><strong>${esc({engineer:'Engineering',literature:'Literature',verifier:'Verification'}[role])}</strong>${current?badge(current.status):badge('idle')}</div><p>${current?action('task',current.id,String(current.goal).slice(0,140)): 'No assignments recorded.'}</p><small>${ts.length} assignments · ${ts.filter(t=>['accepted','completed'].includes(t.status)).length} completed</small>${specialistUsage(role)}</div>`}).join('')}</div>
 <div class="grid-two"><section class="panel"><div class="panel-heading"><h2>Activity</h2><small>Latest recorded events</small></div>${activity()}</section><section class="panel"><div class="panel-heading"><h2>Current understanding</h2><small>Saved project brief</small></div><div class="panel-body brief">${esc(p.brief||'No project brief recorded yet.')}</div></section></div>`;
}
function activity(){
 const ts=new Set(rows('tasks').map(t=>t.id)),ss=new Set(rows('sessions').map(s=>s.id)),js=new Set(rows('jobs').map(j=>j.id));
 const events=db.events.filter(e=>e.project===projectId||ts.has(e.task)||ss.has(e.session)||js.has(e.job)).slice(0,16);
 return events.length?`<div class="timeline">${events.map(e=>{const t=task(e.task)||task(db.sessions.find(s=>s.id===e.session)?.task);const j=db.jobs.find(j=>j.id===e.job);const text=({work_authorized:'Assignment authorized',result_applied:'Report received',human_message:'Message received',experiment_requested:'Experiment requested',experiment_approval:'Experiment approval recorded',channel_enrolled:'Project connected',action:`Supervisor action: ${label(e.type)}`})[e.action]||label(e.action);return `<div class="event"><p>${esc(text)}</p>${t?action('task',t.id,t.goal.slice(0,100)):j?action('job',j.id,j.name):''}<small>${date(e.time)}</small></div>`}).join('')}</div>`:empty('No activity events recorded.');
}
function papers(){const links=new Map(rows('paper_links').map(l=>[l.paper,l]));return db.papers.filter(p=>links.has(p.canonical_id)).map(p=>({...p,id:p.canonical_id,project:projectId,relevance:links.get(p.canonical_id).relevance}));}
function threads(){
 const map=new Map();
 for(const m of [...rows('messages').map(m=>({...m,speaker:'You'})),...rows('outbox').filter(m=>m.text).map(m=>({...m,speaker:'Supervisor'}))]){
  const id=`${m.channel}:${m.thread}`;if(!map.has(id))map.set(id,{id,channel:m.channel,thread:m.thread,messages:[]});map.get(id).messages.push(m);
 }
 return [...map.values()].map(t=>{t.messages.sort((a,b)=>when(a)-when(b));const first=t.messages.find(m=>m.speaker==='You')||t.messages[0];return {...t,title:first?.text||'Discussion',created:t.messages.at(-1)?.created,status:'recorded'};}).sort((a,b)=>when(b)-when(a));
}
function toolbar(options){return `<div class="toolbar"><input id="search" type="search" placeholder="Search ${esc(page)}…" aria-label="Search ${esc(page)}" value="${esc(query)}"><select id="filter" aria-label="Filter records">${options.map(([v,t])=>`<option value="${esc(v)}" ${filter===v?'selected':''}>${esc(t)}</option>`).join('')}</select></div><div id="results"></div>`;}
function digestCitation(paper){
 const field=name=>(paper.details||'').match(new RegExp('^\\*\\*'+name+':\\*\\*\\s*(.+)$','im'))?.[1]?.trim();
 const combined=field('Venue and Year');
 if(combined)return combined;
 const venue=field('Venue'),year=field('Year');
 return [venue,year].filter(Boolean).join(' · ');
}
function digestPaperKey(digest,topic,paper){return `${digest.id}:${topic}:${paper.canonical_id||paper.url||paper.title}`;}
function digestShelf(){
 const issues=[...rows('digests')].sort((a,b)=>String(b.period_end).localeCompare(String(a.period_end))||when(b)-when(a));
 const topics=new Map(), legacy=[];
 for(const issue of issues){
  if(!Array.isArray(issue.sections)||!issue.sections.length){legacy.push(issue);continue;}
  for(const section of issue.sections){
   if(!topics.has(section.topic))topics.set(section.topic,{name:section.topic,entries:[],updates:[]});
   const topic=topics.get(section.topic);
   if(section.summary)topic.updates.push({issue,text:section.summary});
   for(const paper of section.papers||[])topic.entries.push({issue,paper});
  }
 }
 const needle=query.trim().toLowerCase();
 let total=0;
 const content=[...topics.values()].map(topic=>{
  const topicMatch=topic.name.toLowerCase().includes(needle);
  const entries=topic.entries.filter(({issue,paper})=>topicMatch||JSON.stringify({paper,title:issue.title,date:issue.period_end}).toLowerCase().includes(needle));
  const updates=topic.updates.filter(u=>topicMatch||u.text.toLowerCase().includes(needle));
  if(needle&&!entries.length&&!updates.length)return '';
  total+=entries.length;
  return `<section class="digest-topic" aria-label="${esc(topic.name)}"><div class="digest-topic-heading"><h2>${esc(topic.name)}</h2><span class="text-muted">${entries.length} paper${entries.length===1?'':'s'}</span></div>${updates.map(u=>`<p class="digest-topic-update">${esc(u.text)} <small>${esc(u.issue.period_end)}</small></p>`).join('')}<div class="digest-paper-list">${entries.map(({issue,paper})=>{
   const key=digestPaperKey(issue,topic.name,paper);
   const link=/^https?:\/\//i.test(paper.url||'')?`<a class="digest-source" href="${esc(paper.url)}" target="_blank" rel="noopener noreferrer">Read paper ↗</a>`:'';
   return `<article class="digest-paper"><p class="digest-paper-issue">${esc(issue.period_end)} · ${esc(issue.title)}</p><h3>${esc(paper.title)}${digestCitation(paper)?`<span class="digest-paper-citation">${esc(digestCitation(paper))}</span>`:'<span class="digest-paper-citation">Venue and year not recorded</span>'}</h3><p class="digest-paper-summary">${esc(paper.summary)}</p>${link}<details class="digest-paper-details" data-digest-paper="${esc(key)}"${expandedDigestPapers.has(key)?' open':''}><summary>Paper details</summary><div class="research-story digest-paper-body">${storyMarkdown(paper.details||'No further details recorded.')}</div></details></article>`;
  }).join('')||empty('No papers selected for this topic yet.')}</div></section>`;
 }).join('');
 const unstructured=legacy.filter(issue=>JSON.stringify(issue).toLowerCase().includes(needle)).map(issue=>`<section class="digest-topic"><h2>${esc(issue.topic)}</h2><p class="meta">${esc(issue.period_start)} – ${esc(issue.period_end)} · ${esc(issue.title)}</p><p>${esc(issue.summary)}</p><div class="research-story">${storyMarkdown(issue.text)}</div></section>`).join('');
 const notes=issues.filter(issue=>issue.sections?.length&&(!needle||JSON.stringify({title:issue.title,text:issue.text,summary:issue.summary}).toLowerCase().includes(needle))).map(issue=>`<details class="digest-issue-notes"><summary>${esc(issue.period_end)} · ${esc(issue.title)} — synthesis and coverage</summary><p>${esc(issue.summary)}</p><div class="research-story">${storyMarkdown(issue.text)}</div></details>`).join('');
 return `<p class="text-muted digest-shelf-intro">Browse by topic. Paper summaries are shown below; expand Paper details for methods, findings, limitations and reading notes.</p><p class="text-muted">${total} paper${total===1?'':'s'}${legacy.length?' · Includes earlier ungrouped issues':''}</p>${content||unstructured?'':empty(needle?'No papers match your search.':'Your first research digest will appear here when published.')}<div class="digest-topics">${content}${unstructured}</div>${notes?`<section class="digest-coverage"><h2>Issue notes</h2>${notes}</section>`:''}`;
}
function renderResults(){
 if(page==='library'&&filter==='digests'){$('#results').innerHTML=digestShelf();return;}
 let entries=[];
 if(page==='work')entries=newest(rows('tasks')).filter(t=>(t.status!=='cancelled'||filter==='cancelled')&&(filter==='all'||t.role===filter||t.status===filter)).map(t=>({text:JSON.stringify(t),html:taskCard(t)}));
 if(page==='conversations'){
  if(filter!=='meetings')entries.push(...threads().map(t=>({text:JSON.stringify(t),html:card('thread',t,t.title,`${t.messages.length} saved messages`,'Discussion')})));
  if(filter!=='threads')entries.push(...rows('meetings').map(m=>({text:JSON.stringify(m),html:card('meeting',m,m.question,`${m.turn}/${m.max_turns} turns`,'Specialist meeting')})));
 }
 if(page==='experiments')entries=newest(rows('jobs')).filter(j=>filter==='all'||j.status===filter).map(j=>({text:JSON.stringify(j),html:jobCard(j)}));
 if(page==='library'){
  if(['all','digests'].includes(filter))entries.push(...[...rows('digests')].sort((a,b)=>b.period_end.localeCompare(a.period_end)).map(d=>({text:JSON.stringify(d),html:digestCard(d)})));
  if(['all','reports'].includes(filter))entries.push(...newest(rows('reports')).map(r=>({text:JSON.stringify(r),html:reportCard(r)})));
  if(['all','papers'].includes(filter))entries.push(...papers().map(p=>({text:JSON.stringify(p),html:card('paper',p,p.title,p.relevance||p.method,`${p.year||'Year unknown'} · paper`)})));
  if(['all','decisions'].includes(filter))entries.push(...newest(rows('decisions')).map(d=>({text:JSON.stringify(d),html:card('decision',d,d.text,d.disagreement,'Decision')})));
  if(['all','files'].includes(filter))entries.push(...newest(rows('files').filter(f=>['notes','decisions','experiments'].includes(f.section))).map(f=>({text:JSON.stringify(f),html:card('file',{...f,created:f.modified},f.path,`${Math.ceil(f.size/1024)} KB`,'Research file')})));
 }
 entries=entries.filter(e=>e.text.toLowerCase().includes(query.toLowerCase()));
 $('#results').innerHTML=`<p class="text-muted">${entries.length} record${entries.length===1?'':'s'}</p><div class="list">${entries.map(e=>e.html).join('')||empty(query?'No records match your search.':'No records in this view yet.')}</div>`;
}
function storyMarkdown(text){
 const fileFor=path=>rows('files').find(f=>f.path===path.replace(/^\.\//,''));
 const inline=value=>{
  const tokens=[];
  const marked=String(value).replace(/\[([^\]]+)\]\(([^\s)]+)\)/g,(_,title,target)=>{
   const file=fileFor(target);
   const html=file?`<button class="link-button" data-type="file" data-id="${esc(file.id)}">${esc(title)}</button>`:/^https?:\/\//.test(target)?`<a href="${esc(target)}" target="_blank" rel="noopener noreferrer">${esc(title)}</a>`:esc(title)+' <span class="text-muted">('+esc(target)+')</span>';
   tokens.push(html);return '\u0001'+(tokens.length-1)+'\u0002';
  });
  return esc(marked).replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/\u0001(\d+)\u0002/g,(_,i)=>tokens[Number(i)]||'');
 };
 let figure=0;
 const cells=line=>line.trim().replace(/^\|/,'').replace(/\|$/,'').split(/(?<!\\)\|/).map(c=>c.trim().replace(/\\\|/g,'|'));
 const lines=String(text||'').split('\n'), result=[];
 for(let i=0;i<lines.length;){
  const line=lines[i];if(!line.trim()){i++;continue;}
  const image=line.match(/^!\[([^\]]*)\]\(([^\s)]+)\)\s*$/);
  if(image){
   figure++;const file=fileFor(image[2]);
   result.push(file&&/\.(png|jpe?g|webp)$/i.test(file.path)?`<figure class="story-figure"><a href="/api/file?id=${encodeURIComponent(file.id)}" target="_blank" rel="noopener noreferrer"><img loading="lazy" src="/api/file?id=${encodeURIComponent(file.id)}" alt="${esc(image[1])}"></a><figcaption><strong>Figure ${figure}.</strong> ${inline(image[1])}</figcaption></figure>`:`<p class="notice">Figure unavailable: ${esc(image[1])}. ${inline('['+image[2]+']('+image[2]+')')}</p>`);
   i++;continue;
  }
  if(i+1<lines.length&&line.includes('|')&&/^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[i+1])){
   const headers=cells(line);i+=2;const body=[];
   while(i<lines.length&&lines[i].trim()&&lines[i].includes('|')){body.push(cells(lines[i]));i++;}
   result.push('<div class="story-table"><table><thead><tr>'+headers.map(c=>'<th scope="col">'+inline(c)+'</th>').join('')+'</tr></thead><tbody>'+body.map(row=>'<tr>'+headers.map((_,j)=>'<td>'+inline(row[j]||'')+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>');continue;
  }
  const heading=line.match(/^(#{1,4}) (.+)$/);
  if(heading){const level=Math.min(4,heading[1].length+1);result.push(`<h${level}>${inline(heading[2])}</h${level}>`);i++;continue;}
  if(/^[-*] /.test(line)){const items=[];while(i<lines.length&&/^[-*] /.test(lines[i])){items.push('<li>'+inline(lines[i++].slice(2))+'</li>');}result.push('<ul>'+items.join('')+'</ul>');continue;}
  const paragraph=[];do{paragraph.push(lines[i++]);}while(i<lines.length&&lines[i].trim()&&!/^(#{1,4} |!\[|[-*] )/.test(lines[i])&&!(lines[i].includes('|')&&i+1<lines.length&&/---/.test(lines[i+1])));
  result.push('<p>'+inline(paragraph.join('\n')).replaceAll('\n','<br>')+'</p>');
 }
 return result.join('');
}
function studyStatus(study){
 const has=key=>rows('files').some(f=>f.path===study[key]);
 const available=Number(has('paper_pdf'))+Number(has('detailed_pdf'));
 const assignments=rows('tasks').filter(t=>t.study_id===study.study_id&&t.work_kind==='documentation');
 const active=assignments.filter(t=>rows('sessions').some(s=>s.task===t.id&&['running','stopping'].includes(s.status)));
 let reports=available===2?'Both PDFs available':available===1?(has('paper_pdf')?'Paper section available; detailed report not yet available':'Detailed report available; paper section not yet available'):'Reports not yet available';
 if(active.length)reports=active.some(t=>t.document_phase==='check')?'Report review underway':'Report writing underway';
 else if(assignments.some(t=>t.status==='queued'))reports=db.maintenance_hold?.paused||project()?.status!=='active'?'Report work paused':'Report work queued';
 const execution={planned:'Study planned',running:'Study ongoing',completed:'Study completed',failed:'Study failed'}[study.execution]||'Study status not recorded';
 return `<div>${esc(execution)}</div><div class="meta">${esc(reports)}</div>${study.status_note?`<div class="meta">${esc(study.status_note)}</div>`:''}`;
}
function researchStory(){
 const studies=rows('studies');
 const available=storyPdfFiles();
 for(const id of selectedStoryPdfs)if(!available.some(f=>f.id===id))selectedStoryPdfs.delete(id);
 const pdf=(study,key)=>{const file=rows('files').find(f=>f.path===study[key]);return file&&/\.pdf$/i.test(file.path)?`<span class="pdf-choice"><input type="checkbox" data-story-pdf="${esc(file.id)}" aria-label="Select ${esc(study.title)} ${key==='paper_pdf'?'paper section':'detailed report'}" ${selectedStoryPdfs.has(file.id)?'checked':''}>${action('file',file.id,'PDF')}</span>`:'<span class="text-muted">Not available yet</span>';};
 return `<div class="eyebrow">${esc(project()?.name)}</div><h1>Research story</h1><p class="subtitle">One current row per study. Summaries and status notes are maintained by the supervisor; links are replaced when reports are updated.</p>${studies.length?`<div class="pdf-download-toolbar"><button class="help-button" data-select-story-pdfs ${!available.length?'disabled':''}>Select all</button><button class="help-button" data-clear-story-pdfs ${!selectedStoryPdfs.size?'disabled':''}>Clear selection</button><button class="help-button" data-download-story-pdfs="selected" ${!selectedStoryPdfs.size||pdfDownloading?'disabled':''}>Download selected (${selectedStoryPdfs.size})</button><button class="help-button" data-download-story-pdfs="all" ${!available.length||pdfDownloading?'disabled':''}>Download all PDFs (${available.length})</button><span class="meta" role="status" id="pdf-download-status">${esc(pdfDownloadMessage||'Downloads one ZIP containing the current linked PDFs.')}</span></div><div class="story-table"><table><thead><tr><th scope="col">Study</th><th scope="col">Brief summary</th><th scope="col">Status</th><th scope="col">Paper section</th><th scope="col">Detailed report</th></tr></thead><tbody>${studies.map(s=>`<tr><th scope="row">${esc(s.title)}</th><td>${esc(s.question)}</td><td>${studyStatus(s)}</td><td>${pdf(s,'paper_pdf')}</td><td>${pdf(s,'detailed_pdf')}</td></tr>`).join('')}</tbody></table></div>`:empty('No studies have been registered yet.')}`;
}
function storyPdfFiles(){
 const linked=new Set(rows('studies').flatMap(s=>[s.paper_pdf,s.detailed_pdf]).filter(Boolean));
 return rows('files').filter(f=>linked.has(f.path)&&/\.pdf$/i.test(f.path));
}
async function downloadStoryPdfs(mode){
 if(pdfDownloading)return;
 const params=new URLSearchParams({project:projectId});
 if(mode==='selected'){
  if(!selectedStoryPdfs.size)return;
  for(const id of selectedStoryPdfs)params.append('id',id);
 }
 pdfDownloading=true;pdfDownloadMessage='Preparing PDF download…';render();
 try{
  const response=await fetch('/api/story-pdfs?'+params);
  if(!response.ok){const error=await response.json();throw Error(error.error||'Download unavailable');}
  const url=URL.createObjectURL(await response.blob()), link=document.createElement('a');
  link.href=url;link.download='research-story-pdfs.zip';document.body.appendChild(link);link.click();link.remove();
  setTimeout(()=>URL.revokeObjectURL(url),60000);
  pdfDownloadMessage='ZIP download started.';
 }catch(error){pdfDownloadMessage=String(error.message||error);}
 finally{pdfDownloading=false;render();}
}
function render(){
 renderContent();
 if(page!=='notes'&&db.maintenance_hold?.paused)$('#main').insertAdjacentHTML('afterbegin','<section class="notice"><strong>Maintenance hold — no new agents or experiment jobs will start.</strong><p>The report workflow is being reset. Existing computations may finish; saved reports and results remain available. An allowance reset will not resume work.</p></section>');
}
function renderContent(){
 const p=project();$('#project').innerHTML=db.projects.map(p=>`<option value="${esc(p.id)}" ${p.id===projectId?'selected':''}>${esc(p.name)}</option>`).join('');
 $('#nav').innerHTML=pages.map(([id,icon,name])=>`<a class="nav-item ${page===id?'active':''}" ${page===id?'aria-current="page"':''} href="#${id}"><span>${icon}</span><span>${name}</span></a>`).join('');
 $('#breadcrumb').textContent=`WORKSPACE / ${page.toUpperCase()}`;
 window.ResearchChatUI?.sync(p,page);
 if(page==='notes'&&p){window.ResearchNotesUI?.mount($('#main'),p);return;}
 window.ResearchNotesUI?.unmount();
 if(page==='chat'&&p){window.ResearchChatUI?.mount($('#main'),p);return;}
 if(page==='story'){$('#main').innerHTML=researchStory();return;}
 if(page==='server'){$('#main').innerHTML='<h1>Server status</h1>'+serverStatus();return;}
 if(!p){$('#main').innerHTML=empty(db.warnings[0]||'No projects yet. Invite the supervisor to a Slack channel to begin.')+accountUsage()+serverStatus();return;}
 if(page==='overview')$('#main').innerHTML=overview();
 else{
  const descriptions={work:'Assignments, dependencies, specialist progress, and saved outcomes.',conversations:'Saved discussions and bounded specialist meetings, connected to the work.',experiments:'Experiment allocations, results, metrics, and reproducible evidence.',library:'Weekly topic digests, paper notes, reports, and the evidence behind your research.'};
  let options={work:[['all','Current and completed work'],['engineer','Engineering'],['literature','Literature'],['verifier','Verification'],['worker','Workers'],...['running','queued','waiting_jobs','waiting_children','deferred','partial','blocked','needs_resources','awaiting_review','accepted','cancelled'].map(s=>[s,label(s)])],conversations:[['all','All conversations'],['threads','Discussions'],['meetings','Specialist meetings']],experiments:[['all','All experiments'],...['running','queued','approval','done','failed','cancelled'].map(s=>[s,label(s)])],library:[['digests','Research digests'],['all','All evidence'],['reports','Reports'],['papers','Papers'],['decisions','Decisions'],['files','Research files']]}[page];
  $('#main').innerHTML=`<div class="eyebrow">${esc(p.name)}</div><h1>${esc(pages.find(p=>p[0]===page)[2])}</h1><p class="subtitle">${descriptions[page]}</p>${page==='work'?'<p class="meta">An assignment is an agent’s work order. A study is the scientific question; experiment jobs are the programs an engineer launches to investigate it. Retired assignments are available under Cancelled.</p>':''}${toolbar(options)}`;renderResults();
 }
 if(db.warnings.length)$('#main').insertAdjacentHTML('afterbegin',`<div class="notice">${db.warnings.map(esc).join('<br>')}</div>`);
}
function relatedReports(id){return rows('reports').filter(r=>r.task===id);}
function fileLink(f){return `<button class="file-link link-button" data-type="file" data-id="${esc(f.id)}">${esc(f.path)} <span class="text-muted">· ${Math.ceil(f.size/1024)} KB</span></button>`;}
function reportBody(r){
 const evidence=(r.evidence||[]).map(e=>{const f=db.files.find(f=>e.path?.endsWith('/'+f.path)&&f.project===r.project);return `<p>${esc(e.description||'Evidence')}${f?fileLink(f):`<span class="text-muted"> · ${esc(e.path||'File unavailable')}</span>`}</p>`}).join('');
 return section('Summary',prose(r.summary))+section('Findings',bullets(r.findings))+section('Limitations',bullets(r.limitations))+section('Suggested next steps',bullets(r.next_steps))+section('Verification',r.verdict?badge(r.verdict,'verification'):'')+section('Evidence',evidence)+section('Saved response',prose(r.reply));
}
function relations(title,rs,type,titleFn){return section(title,rs.length?`<div class="panel">${rs.map(r=>type==='task'?taskCard(r):type==='job'?jobCard(r):reportCard(r)).join('')}</div>`:'');}
function metricView(metrics){
 if(!metrics||typeof metrics!=='object')return '';
 return Object.entries(metrics).map(([name,value])=>{
  if(Array.isArray(value)&&value.length&&value.every(n=>typeof n==='number'&&Number.isFinite(n))){const lo=Math.min(...value),hi=Math.max(...value);const points=value.map((v,i)=>`${10+i/Math.max(1,value.length-1)*560},${125-(v-lo)/(hi-lo||1)*105}`).join(' ');return `<div class="metric"><strong>${esc(name)}</strong><span>${value.length} values · ${lo.toPrecision(4)} – ${hi.toPrecision(4)}</span></div><svg class="chart" viewBox="0 0 580 140" role="img" aria-label="${esc(name)} values in recorded order"><polyline class="chart-line" points="${points}"/></svg><small class="text-muted">Recorded order; values may represent seeds rather than time.</small>`;}
  return `<div class="metric"><strong>${esc(name)}</strong><span>${esc(typeof value==='object'?JSON.stringify(value):value)}</span></div>`;
 }).join('');
}
function detail(type,id){
 let title='',content='',r=null;
 if(type==='task'){
  r=task(id);if(!r)return;title=r.goal;
  const checkpoint=db.checkpoints.find(c=>c.task===id),helpers=db.helpers.filter(h=>h.task===id),sessions=newest(db.sessions.filter(s=>s.task===id));
  content=`<div class="tagline">${badge(r.status,'task')}<span>${esc(r.role)} · requested allocation ${esc(r.model)} / ${esc(r.effort)} · priority ${r.priority}</span></div>`+section('Assignment',prose(r.goal))+section('Completion criteria',prose(r.acceptance))+section('Latest saved outcome',prose(r.summary))+section('Launch blocker',r.launch_hold?prose(`${r.launch_hold.reason}\nNext session reservation: ${r.launch_hold.requested_minutes} minutes${r.launch_hold.retry_at?'\nRetry / reset: '+date(r.launch_hold.retry_at):''}`):'')+section('Waiting / scheduling',prose([r.defer_reason,r.not_before?`Not before ${date(r.not_before)}`:'',project().status!=='active'?`Project is ${project().status}.`:''].filter(Boolean).join('\n')))+section('Saved progress',checkpoint?prose(`Saved ${date(checkpoint.saved_at)}`)+bullets(checkpoint.completed)+prose(checkpoint.next_step?`Next: ${checkpoint.next_step}`:'')+prose(checkpoint.blocked?`Blocked: ${checkpoint.blocked}`:''):'')+section('Suggested next steps',bullets(r.next_steps));
  content+=relations('Dependencies',(r.depends||[]).map(task).filter(Boolean),'task')+relations('Parent assignment',r.parent&&task(r.parent)?[task(r.parent)]:[],'task')+relations('Child assignments',rows('tasks').filter(t=>t.parent===id),'task')+relations('Verification',rows('tasks').filter(t=>t.verification_target===id),'task')+relations('Verification target',r.verification_target&&task(r.verification_target)?[task(r.verification_target)]:[],'task');
  content+=section('Native helpers',helpers.map(h=>`<div class="session-row">${badge(h.status,'helper')} <strong>${esc(h.type)}</strong><p class="meta">Last recorded ${date(h.updated)}</p>${prose(h.summary)}</div>`).join(''));
  content+=relations('Reports',newest(relatedReports(id)),'report')+relations('Experiments',rows('jobs').filter(j=>j.task===id),'job');
  const thread=threads().find(t=>t.channel===r.channel&&t.thread===r.thread);content+=section('Discussion',thread?action('thread',thread.id,thread.title.slice(0,180)):'');
  content+=section('Sessions',sessions.map(s=>`<div class="session-row">${badge(s.status,'session')} ${esc(s.role)} · ${esc(s.model)} / ${esc(s.effort)}${s.account?` · Account: ${esc(s.account)}`:''}${s.history_parent?' · Continued conversation':''}<br><span class="text-muted">${date(s.created)}${s.seconds?` · ${(s.seconds/60).toFixed(1)} min`:''}</span>${s.stop_reason?prose(s.stop_reason):''}</div>`).join(''));
  content+=section('Recorded notes',prose(r.notes?.length?r.notes:null))+section('Last error',prose(r.last_error));
 }else if(type==='digest'){
  r=rows('digests').find(d=>d.id===id);if(!r)return;title=r.title;
  content=`<div class="tagline"><strong>${esc(r.topic)}</strong><span>${esc(r.period_start)} – ${esc(r.period_end)} · Revision ${esc(r.version)}</span></div>`+section('This week',prose(r.summary))+`<div class="research-story digest-reading">${storyMarkdown(r.text)}</div>`+(r.sections||[]).map(s=>section(s.topic,(s.papers||[]).map(p=>`<h3>${esc(p.title)}</h3><p>${esc(p.summary)}</p><div class="research-story">${storyMarkdown(p.details)}</div>`).join(''))).join('');
 }else if(type==='report'){
  r=rows('reports').find(r=>r.id===id);if(!r)return;title=r.summary||'Research report';content=`<div class="tagline">${badge(r.status,'report')}<span>${esc(r.role)} · ${date(r.created)}</span></div>`+reportBody(r)+section('Assignment',task(r.task)?action('task',r.task,task(r.task).goal):'')+section('Source record',fileLink(db.files.find(f=>f.id===r.file)));
 }else if(type==='job'){
  r=(db.jobs||[]).find(r=>r.id===id);if(!r)return;title=r.name;const files=(db.files||[]).filter(f=>f.project===r.project&&f.path.startsWith(`runs/${id}/`));
  content=`<div class="tagline">${badge(r.status,'job')}<span>${date(r.created)}</span></div>`+(r.status==='approval'?'<div class="notice">This run needs approval. Respond in its existing Slack discussion.</div>':'')+section('Resources',prose(`${r.hours} allocated hours · ${r.gpus||0} GPUs · ${r.gpu_mode||'exclusive'}${r.gpu_mode==='shared'?' · '+r.gpu_memory_mib+' MiB reserved/GPU (admission reservation)':''} · ${r.cpu_threads||'automatic'} CPU threads\nSeeds: ${(r.seeds||[]).join(', ')||'not recorded'}\nStarted: ${r.started?date(r.started):'not started'}\nEnded: ${r.ended?date(r.ended):'not recorded'}${r.exit_code!==undefined?`\nExit code: ${r.exit_code}`:''}`))+section('Allocated GPU devices',prose((r.allocation?.gpus||[]).join(', ')||'Not allocated'))+section('Metrics',metricView(r.metrics)||prose('No saved metrics available for this run.'))+section('Metric summaries',r.summaries?prose(r.summaries):'')+section('Metrics warning',prose(r.metrics_error))+section('Command',`<pre>${esc(Array.isArray(r.command)?r.command.join(' '):r.command||'Not recorded')}</pre>`)+section('Assignment',task(r.task)?action('task',r.task,task(r.task).goal):'')+section('Run files',files.map(fileLink).join(''));
 }else if(type==='paper'){
  r=papers().find(p=>p.id===id);if(!r)return;title=r.title;const safe=/^https?:\/\//i.test(r.url||'');
  content=`<div class="tagline">${badge(r.read_depth,'paper')}<span>${esc(r.year)} · version ${r.version}</span></div>`+section('Authors',prose((r.authors||[]).join(', ')))+section('Source',safe?`<a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">Open paper ↗</a>`:'')+section('Relevance to this project',prose(r.relevance))+section('Method',prose(r.method))+section('Results',prose(r.results))+section('Limitations',prose(r.limitations))+section('Sections read',bullets(r.sections_read))+section('Supporting sections',bullets(r.supporting_sections))+section('Research assignment',task(r.task)?action('task',r.task,task(r.task).goal):'');
 }else if(type==='thread'){
  r=threads().find(t=>t.id===id);if(!r)return;title=String(r.title).slice(0,200);
  content='<p class="text-muted">Saved messages received by the lab and supervisor replies. Messages missed while offline may be absent.</p>'+r.messages.map(m=>`<div class="message ${m.speaker==='You'?'human':''}"><div class="meta">${esc(m.speaker)} · ${date(m.created||m.time)} ${m.speaker==='Supervisor'?`· ${esc(m.status)}`:''}</div>${prose(m.text)}</div>`).join('')+relations('Related assignments',rows('tasks').filter(t=>t.thread===r.thread&&t.channel===r.channel),'task');
 }else if(type==='decision'){
  r=rows('decisions').find(d=>d.id===id);if(!r)return;title='Research decision';content=prose(date(r.time))+section('Decision',prose(r.text))+section('Disagreement / caveats',prose(r.disagreement))+section('Sources',(r.sources||[]).map(s=>{const f=rows('files').find(f=>f.path===s);return f?fileLink(f):prose(s)}).join(''));
 }else if(type==='meeting'){
  r=rows('meetings').find(m=>m.id===id);if(!r)return;title=r.question;content=badge(r.status,'meeting')+section('Progress',prose(`${r.turn} of at most ${r.max_turns} turns`))+relations('Origin assignment',task(r.origin)?[task(r.origin)]:[],'task')+relations('Meeting assignments',rows('tasks').filter(t=>t.goal?.includes(r.question)),'task')+section('Saved reports',(r.reports||[]).map(path=>{const f=rows('files').find(f=>path.endsWith('/'+f.path));return f?fileLink(f):prose(path)}).join(''));
 }else if(type==='wake'||type==='request'){
  r=rows(type==='wake'?'wakeups':'requests').find(r=>r.id===id);if(!r)return;title=r.reason;content=badge(r.status,type==='wake'?'wake':'request')+section(type==='wake'?'Scheduled review':'Supervisor request',prose(type==='wake'?`${r.recurrence==='weekly'?'Repeats weekly · '+esc(r.timezone||'UTC')+' · Next':'Scheduled'} ${date(r.at)}. Respects project pauses and quota gates.`:`Priority ${r.priority} · created ${date(r.created)}`));
 }else if(type==='file'){
  r=(db.files||[]).find(f=>f.id===id);if(!r)return;title=r.path;const url=`/api/file?id=${encodeURIComponent(r.id)}`;
  content=`<p class="text-muted">${Math.ceil(r.size/1024)} KB · modified ${date(r.modified)}</p><a href="${url}" target="_blank" rel="noopener noreferrer">Open source file ↗</a>`;
  if(/\.(png|jpg|jpeg|webp)$/i.test(r.path))content+=`<section class="detail-section"><img class="preview-image" src="${url}" alt="${esc(r.name)}"></section>`;
  else if(!/\.pdf$/i.test(r.path)&&r.size<=1_000_000){content+='<pre id="file-preview">Loading preview…</pre>';fetch(url).then(async res=>{if(!res.ok)throw Error('File unavailable');return res.text()}).then(text=>{if(dialogRecord?.type==='file'&&dialogRecord.id===id&&$('#file-preview'))$('#file-preview').textContent=text}).catch(()=>{if($('#file-preview'))$('#file-preview').textContent='Preview unavailable. Try opening the source file.'});}
 }
 if(!r)return;dialogRecord={type,id};$('#detail-content').innerHTML=`<h1 id="detail-title">${esc(title)}</h1>${content}`;if(!$('#detail').open)$('#detail').showModal();$('#detail').scrollTop=0;
}
async function refreshAccountUsage(){
 if(usageRefreshPending)return;
 usageRefreshPending=Date.now()/1000;usageRefreshMessage='Requesting account usage from the daemon…';render();
 try{
  const response=await fetch('/api/subscription-refresh',{method:'POST'});
  const result=await response.json();if(!response.ok)throw Error(result.error||'Account refresh unavailable');
  usageRefreshPending=result.requested_at;usageRefreshMessage='Refresh requested. Waiting for the daemon; provider rate-limit backoff still applies.';
 }catch(e){usageRefreshPending=0;usageRefreshMessage=String(e.message||e);}
 render();await refresh();
}
async function renewAccount(account){
 try{
  const response=await fetch('/api/subscription-renew/'+encodeURIComponent(account),{method:'POST'});
  const result=await response.json();if(!response.ok)throw Error(result.error||'Renewal unavailable');
  usageRefreshMessage='Connection renewal requested. The daemon will make one tiny Haiku request and check usage again.';
 }catch(e){usageRefreshMessage=String(e.message||e);}
 await refresh();
}
async function refresh(){
 if(refreshing)return;refreshing=true;
 try{const response=await fetch('/api/snapshot',{cache:'no-store'});if(!response.ok)throw Error('Unable to read records');db=await response.json();if(usageRefreshPending&&db.subscription_refresh?.requested_at>=usageRefreshPending){usageRefreshPending=0;usageRefreshMessage=((db.accounts||[]).some(a=>a.metadata_error))?'Refresh checked; some accounts are unavailable or backing off. See their cards.':'Saved account readings updated. See each card’s timestamp for its latest successful check.';}lastSuccess=Date.now();if(!db.projects.some(p=>p.id===projectId))projectId=db.projects.find(p=>p.status==='active')?.id||db.projects[0]?.id||'';render();$('#freshness').textContent=`Updated ${new Date(lastSuccess).toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'})}`;$('#freshness').classList.remove('error');}
 catch(e){$('#freshness').textContent=lastSuccess?'Connection lost · showing saved view':'Connection unavailable';$('#freshness').classList.add('error');if(!db)$('#main').innerHTML=empty('Cannot read the lab records. Check the dashboard terminal and use Refresh to retry.');}
 finally{refreshing=false;}
}
document.addEventListener('click',e=>{
 const download=e.target.closest('[data-download-story-pdfs]');if(download){downloadStoryPdfs(download.dataset.downloadStoryPdfs);return;}
 if(e.target.closest('[data-select-story-pdfs]')){for(const file of storyPdfFiles())selectedStoryPdfs.add(file.id);render();return;}
 if(e.target.closest('[data-clear-story-pdfs]')){selectedStoryPdfs.clear();render();return;}
 const status=e.target.closest('[data-help-status]');
 if(status){e.preventDefault();e.stopPropagation();showStatusHelp(status.dataset.helpKind,status.dataset.helpStatus);return;}
 const renew=e.target.closest('[data-renew-account]');if(renew){e.preventDefault();renewAccount(renew.dataset.renewAccount);return;}
 if(e.target.closest('[data-refresh-usage]')){e.preventDefault();refreshAccountUsage();return;}
 if(e.target.closest('[data-view-help]')){e.preventDefault();showViewHelp();return;}
 const button=e.target.closest('[data-type]');if(button&&db)detail(button.dataset.type,button.dataset.id);
});
$('#close-help').onclick=()=>$('#status-help').close();
$('#status-help').addEventListener('click',e=>{if(e.target===$('#status-help')){const b=e.target.getBoundingClientRect();if(e.clientX<b.left||e.clientX>b.right||e.clientY<b.top||e.clientY>b.bottom)e.target.close()}});
$('#close-detail').onclick=()=>{$('#detail').close();dialogRecord=null};
$('#detail').addEventListener('close',()=>{dialogRecord=null});
$('#detail').addEventListener('click',e=>{if(e.target===$('#detail')){const b=$('#detail').getBoundingClientRect();if(e.clientX<b.left||e.clientX>b.right)$('#detail').close()}});
$('#refresh').onclick=refresh;
$('#project').onchange=e=>{if(page==='notes'&&window.ResearchNotesUI&&!window.ResearchNotesUI.canLeave()){e.target.value=projectId;return;}projectId=e.target.value;if(page==='notes')history.replaceState(null,'','#notes');selectedStoryPdfs.clear();pdfDownloadMessage='';query='';filter=defaultFilter(page);$('#detail').close();render()};
document.addEventListener('toggle',e=>{const item=e.target;if(!item.isConnected||!item.matches?.('[data-digest-paper]'))return;item.open?expandedDigestPapers.add(item.dataset.digestPaper):expandedDigestPapers.delete(item.dataset.digestPaper)},true);
document.addEventListener('input',e=>{if(e.target.id==='search'){query=e.target.value;renderResults()}});
document.addEventListener('change',e=>{if(e.target.matches('[data-story-pdf]')){const id=e.target.dataset.storyPdf;e.target.checked?selectedStoryPdfs.add(id):selectedStoryPdfs.delete(id);const focus=id;render();document.querySelector(`[data-story-pdf="${focus}"]`)?.focus();return;}if(e.target.id==='filter'){filter=e.target.value;renderResults()}});
function navigate(){const target=location.hash.slice(1).split('/')[0],next=pages.some(p=>p[0]===target)?target:'overview';if(next!==page&&page==='notes'&&window.ResearchNotesUI&&!window.ResearchNotesUI.canLeave()){history.replaceState(null,'','#notes');return;}page=next;query='';filter=defaultFilter(page);if(db)render()}
window.addEventListener('hashchange',navigate);navigate();refresh();
setInterval(()=>{if(!document.hidden&&!$('#detail').open&&!$('#status-help').open&&document.activeElement?.id!=='search')refresh()},15000);
