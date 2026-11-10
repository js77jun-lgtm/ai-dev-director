'use strict';
const frame=document.getElementById('workspace');
const labels={NONE:'작업 대기',IDLE:'작업 대기',PLANNING:'계획 수립',ORDER_READY:'구현 준비',BUILDING:'코드 작성 중',VERIFYING:'테스트 실행 중',REVIEWING:'독립 검토 중',DECIDING:'결과 판단 중',PASS:'검증 통과',RECOVERED:'복구 완료 · 검증 통과',FAIL:'검증 실패',HUMAN_REQUIRED:'확인 · 수정 필요',TIMEOUT:'실행 시간 초과'};
let busy=false,doc;
function el(tag,text,cls){const n=doc.createElement(tag);n.textContent=text;if(cls)n.className=cls;return n;}
function setup(){
 doc=frame.contentDocument;if(!doc?.getElementById('main'))return;
 if(doc.getElementById('mission-control'))return;
 doc.body.classList.add('director-v2');
 const css=doc.createElement('link');css.rel='stylesheet';css.href='/console.css';doc.head.append(css);
 const panel=el('section','','mission-control');panel.id='mission-control';
 const heading=el('div','','mission-heading'),identity=el('div');identity.append(el('span','ANTIGRAVITY / DEVELOPMENT WORKSPACE','mission-kicker'),el('h1','세 AI와 함께 만드는 작업실'));
 const navigation=el('div','','workspace-links');for(const [name,url] of [['솔로몬 집무실','http://127.0.0.1:8710'],['원본 화면','/classic']]){const a=el('a',name+' ↗');a.href=url;a.target='_top';navigation.append(a);}heading.append(identity,navigation);
 const summary=el('div','','mission-summary'),state=el('div','','mission-state');state.append(el('span','현재 작업','mission-kicker'),el('strong','연결 중'));state.lastChild.id='mission-state';
 const next=el('div','','mission-next');next.append(el('span','다음 행동','mission-kicker'),el('p','실행 기록을 읽고 있습니다.'));next.lastChild.id='mission-next';summary.append(state,next);
 const meta=el('div','','mission-meta');for(const id of ['mission-project','mission-updated','mission-tests']){const n=el('span','—');n.id=id;meta.append(n);}
 const stages=el('div','','mission-stages');stages.setAttribute('aria-label','작업 진행 단계');for(const [key,name] of [['PLANNING','01 계획'],['BUILDING','02 구현'],['VERIFYING','03 테스트'],['REVIEWING','04 검토'],['DECIDING','05 판단']]){const n=el('span',name);n.dataset.stage=key;stages.append(n);}
 const evidence=el('details','','mission-evidence');evidence.append(el('summary','현재 실행의 상세 원인과 ID'));const body=el('pre');body.id='mission-details';evidence.append(body);
 panel.append(heading,summary,meta,stages,evidence);doc.getElementById('corestrip').before(panel);
 const inbox=el('details','','mission-evidence');inbox.append(el('summary','솔로몬 작업함 · 연결된 개발 요청'));const entries=el('div','','bridge-inbox');inbox.append(entries);inbox.addEventListener('toggle',async()=>{if(!inbox.open)return;entries.replaceChildren(el('p','요청 기록을 읽고 있습니다.'));try{const response=await fetch('/ops/state',{cache:'no-store'});if(!response.ok)throw Error('작업함 연결 실패');const ops=await response.json();entries.replaceChildren();for(const order of ops.orders||[]){const card=el('article');card.append(el('strong',labels[order.current_state]||order.current_state||'접수'),el('p',order.goal||order.error||'개발 요청'),el('small',order.project_path||''));const more=el('details');more.append(el('summary','실행 ID와 요청 정보'));const raw=el('pre',JSON.stringify(order,null,2));more.append(raw);card.append(more);entries.append(card);}if(!entries.children.length)entries.append(el('p','솔로몬에서 전달한 개발 요청이 아직 없습니다.'));}catch(e){entries.replaceChildren(el('p',e.message));}});panel.append(inbox);
 const tools=el('div','','conversation-tools');
 const focus=el('button','대화로 이동 ↓');focus.type='button';focus.onclick=()=>doc.getElementById('chat-panel').scrollIntoView({behavior:'smooth',block:'start'});
 const noise=el('button','시스템 알림 숨기기');noise.type='button';noise.setAttribute('aria-pressed','false');noise.onclick=()=>{const hidden=doc.body.classList.toggle('hide-system');noise.textContent=hidden?'시스템 알림 보기':'시스템 알림 숨기기';noise.setAttribute('aria-pressed',String(hidden));};
 const latest=el('button','최신 대화 ↓');latest.type='button';latest.onclick=()=>{const box=doc.getElementById('chat-box');box.scrollTop=box.scrollHeight;doc.getElementById('chat-panel').scrollIntoView({behavior:'smooth',block:'start'});};
 tools.append(focus,noise,latest);doc.getElementById('chat-head').after(tools);
 setupRoles();
 setupFolderPicker();
 setupActivity(panel);
 doc.getElementById('chat-box').setAttribute('aria-label','HEAD 대화 기록');refresh();
 for(const id of ['normal-mode-btn','triad-mode-btn'])doc.getElementById(id)?.addEventListener('click',()=>refresh());
}
async function refresh(){if(busy||!doc||!doc.getElementById('mission-control'))return;busy=true;try{
 const response=await fetch('/status',{cache:'no-store'});if(!response.ok)throw Error('상태 조회 실패');const s=await response.json(),run=s.run||{},state=run.state||'NONE',research=doc.getElementById('triad-mode-btn')?.classList.contains('on'),r=s.research||{};
 doc.getElementById('mission-state').textContent=research?(r.current_stage_ko||'연구 대기'):(labels[state]||state);
 doc.getElementById('mission-control').dataset.state=research?(r.status||'NONE'):state;
 let next=s.next_step||s.stage_sentence||'새 작업을 입력해 주세요.';
 if(state==='HUMAN_REQUIRED'&&run.details){next=run.details.includes('PERMISSION_SETUP_FAILED')?'실행 준비 오류로 중단됐습니다. 작업 경로와 권한 규칙을 수정한 뒤 다시 검증해야 합니다.':run.details.includes('BASELINE_FAILED')?'구현 전 검사에서 중단됐습니다. 상세 원인에서 실패한 테스트와 환경을 확인하세요.':run.details.includes('P1')?'코드 검토에서 수정할 결함이 발견됐습니다. 수정 후 테스트와 검토를 다시 진행해야 합니다.':next;}
 doc.getElementById('mission-next').textContent=research?(r.next_step||'연구 기록을 기다리고 있습니다.'):next;
 doc.getElementById('mission-project').textContent='프로젝트 · '+(s.project_name||'미선택');
 const timestamp=run.updated_at?new Date(run.updated_at+(/Z|[+]\d\d:\d\d$/.test(run.updated_at)?'':'Z')):null;
 const elapsed=timestamp?Math.max(0,Math.floor((Date.now()-timestamp.getTime())/1000)):null;
 doc.getElementById('mission-updated').textContent=elapsed===null?'아직 실행 기록이 없습니다.':'최근 단계 기록 · '+(elapsed<60?elapsed+'초 전':Math.floor(elapsed/60)+'분 전');
 doc.getElementById('mission-tests').textContent='적용 대기 '+(s.pending_result_count||0)+'건 · 승인 대기 '+(s.human_required_count||0)+'건';
 doc.getElementById('mission-details').textContent=research?((r.session_id||'연구 세션 없음')+'\n사이클 '+(r.cycle_number||0)+' / '+(r.cycle_budget||0)+'\n'+(r.moonshot||'')):((run.run_id||'작업 없음')+'\n'+(run.details||s.stage_sentence||'')+'\n최근 실행 기록: '+(run.updated_at||'없음'));
 const order=['PLANNING','ORDER_READY','BUILDING','VERIFYING','REVIEWING','DECIDING','PASS'],at=order.indexOf(state);
 const researchStage=(r.current_stage||'');const mapped=researchStage.includes('BUILD')?'BUILDING':researchStage.includes('VERIFY')?'VERIFYING':researchStage.includes('REVIEW')||researchStage.includes('CRITIQUE')?'REVIEWING':researchStage.includes('INTERPRET')||researchStage.includes('COMPLETE')?'DECIDING':'PLANNING';
 for(const n of doc.querySelectorAll('.mission-stages span')){const i=order.indexOf(n.dataset.stage);n.classList.toggle('current',research?mapped===n.dataset.stage:(state===n.dataset.stage||(state==='ORDER_READY'&&n.dataset.stage==='PLANNING')));n.classList.toggle('paused',!research&&state==='HUMAN_REQUIRED'&&run.last_stage===n.dataset.stage);n.classList.toggle('done',!research&&at>i);}
 document.getElementById('shell-error').hidden=true;offlineVeil(0);
 }catch(e){const error=document.getElementById('shell-error');error.hidden=false;error.textContent='상태 연결을 확인하고 있습니다 · '+e.message;offlineVeil((window.__pollMisses||0)+1);}finally{busy=false;}}
frame.addEventListener('load',setup);if(frame.contentDocument?.readyState==='complete')setup();setInterval(refresh,5000);

async function setupRoles(){
 const domains={OPENAI:'openai',GOOGLE:'antigravity',ANTHROPIC:'claude'};
 const order=['head','builder','reviewer'],names={head:'HEAD · 작업 지휘',builder:'BUILDER · 코드 작성',reviewer:'REVIEWER · 검수'};
 let roles,csrf,saving=false;
 const notice=el('p','역할 불러오는 중…','role-notice');notice.setAttribute('aria-live','polite');doc.getElementById('corestrip').after(notice);
 const buttons={};
 function paint(){for(const [domain,provider] of Object.entries(domains)){const role=order.find(r=>roles[r]===provider);buttons[domain].textContent='다음: '+names[role]+' ↻';buttons[domain].disabled=saving;buttons[domain].setAttribute('aria-label',domain+' 역할 변경. 현재 예약 '+names[role]);}}
 for(const [domain,provider] of Object.entries(domains)){
  const button=el('button','역할 연결 중…','role-cycle');button.type='button';button.disabled=true;buttons[domain]=button;doc.getElementById('node-'+domain)?.append(button);
  button.onclick=async()=>{if(saving||!roles)return;saving=true;paint();const next={...roles},current=order.find(r=>roles[r]===provider),target=order[(order.indexOf(current)+1)%3];next[current]=roles[target];next[target]=provider;
   try{const response=await fetch('/ops/roles',{method:'POST',headers:{'Content-Type':'application/json','X-Solomon-Bridge':csrf},body:JSON.stringify(next)});const result=await response.json();if(!response.ok)throw Error(result.error||'저장 실패');roles=result.roles;notice.textContent=result.message;}
   catch(error){notice.textContent='역할 변경 실패 · '+error.message;}finally{saving=false;paint();}};
 }
 try{const response=await fetch('/ops/roles',{cache:'no-store'});if(!response.ok)throw Error('설정 조회 실패');const result=await response.json();roles=result.roles;csrf=result.csrf;paint();notice.textContent='버튼을 누르면 역할이 순환하고 상대 AI와 교환됩니다. 다음 대화/작업부터 적용 · 현재 실행과 TRIAD 연구 배정은 유지';}catch(error){notice.textContent=error.message;}
}

// ---------------------------------------------------------------- AI work visibility (missions, who works now, workload)
const PROVIDER={ANTHROPIC:'Claude',OPENAI:'Codex',GOOGLE:'Antigravity'},ROLE={HEAD:'HEAD · 지휘',BUILDER:'BUILDER · 구현',REVIEWER:'REVIEWER · 검수',OTHER:'기타'};
const STEP_CLASS={PASS:'ok',FAIL:'bad',TIMEOUT:'bad',HUMAN_REQUIRED:'warn'};
const MISSION_STATUS={DONE:'완료',INCOMPLETE:'예산 소진',BLOCKED:'사용자 결정 필요',FAILED:'실패'};
let activityBusy=false,usageWindow='mission';
function fmtTime(sec){sec=Math.round(sec||0);if(sec<60)return sec+'초';const m=Math.floor(sec/60);if(m<60)return m+'분'+(m<10&&sec%60?' '+(sec%60)+'초':'');return Math.floor(m/60)+'시간 '+(m%60)+'분';}
function fmtTokens(b){if(!b)return '—';if(!b.tokens)return b.unknown_calls?'기록 전':'—';const n=b.tokens,t=n>=10000?(n/10000).toFixed(1)+'만':n.toLocaleString('ko-KR');return (b.exact_tokens<b.tokens?'~':'')+t+(b.unknown_calls?' (+기록 전 '+b.unknown_calls+'회)':'');}
function ago(iso){if(!iso)return '';const t=new Date(iso+(/Z|[+]\d\d:\d\d$/.test(iso)?'':'Z')),s=Math.max(0,Math.floor((Date.now()-t)/1000));return s<60?s+'초 전':s<3600?Math.floor(s/60)+'분 전':s<86400?Math.floor(s/3600)+'시간 전':Math.floor(s/86400)+'일 전';}
function setupActivity(panel){
 const box=el('section','','ai-activity');box.id='ai-activity';
 const head=el('div','','ai-activity-head');head.append(el('span','AI 작업 현황 · 5초마다 갱신','mission-kicker'));
 const now=el('div','','ai-now');now.id='ai-now';now.append(el('span','작업 기록을 읽고 있습니다.'));
 const grid=el('div','','ai-grid');
 const mission=el('div','','ai-card');mission.id='ai-mission';
 const work=el('div','','ai-card');const tabs=el('div','','ai-tabs');
 for(const [key,name] of [['mission','이번 미션'],['today','오늘'],['all','전체']]){const b=el('button',name);b.type='button';b.dataset.win=key;b.onclick=()=>{usageWindow=key;refreshActivity();};tabs.append(b);}
 const table=el('div','','ai-table');table.id='ai-work';
 work.append(el('strong','AI별 작업량'),tabs,table,el('p','토큰: ~ 표시는 글자 수 기반 추정, 없으면 CLI가 보고한 실제 값. "기록 전"은 작업 기록부 도입(9/30) 이전 호출이라 시간만 집계됨.','ai-footnote'));
 grid.append(mission,work);
 const calls=el('details','','mission-evidence');calls.append(el('summary','최근 AI 호출'));const callList=el('div','','ai-list');callList.id='ai-calls';calls.append(callList);
 const events=el('details','','mission-evidence');events.append(el('summary','최근 진행 기록 (단계 전환)'));const eventList=el('div','','ai-list');eventList.id='ai-events';events.append(eventList);
 box.append(head,now,grid,calls,events);panel.append(box);
 refreshActivity();setInterval(refreshActivity,5000);
}
function paintNow(a,focus){
 const now=doc.getElementById('ai-now');now.replaceChildren();
 if((a.working_now||[]).length){for(const w of a.working_now){const line=el('div','','ai-now-line working');line.append(el('span','','dot'),el('strong',(ROLE[w.role]||w.role||'AI')+' · '+(PROVIDER[w.provider]||w.provider)),el('span',' 작업 중 · '+fmtTime(w.elapsed_sec)+(w.stage_ko?' · '+w.stage_ko:'')+(w.mission_id?' · '+w.mission_id:'')));now.append(line);}return;}
 const line=el('div','','ai-now-line idle');line.append(el('span','','dot'),el('span',focus&&focus.live?'다음 AI 호출을 준비하고 있습니다.':'지금 실행 중인 AI 작업은 없습니다.'));now.append(line);
}
function paintMission(focus){
 const m=doc.getElementById('ai-mission'),opened=new Set([...m.querySelectorAll('details[open]>summary')].map(s=>s.textContent)),scroll=m.scrollTop;m.replaceChildren();
 if(!focus){m.append(el('strong','미션'),el('p','아직 미션 기록이 없습니다. 채팅으로 요청하면 여기에 단계별 진행이 표시됩니다.'));return;}
 const status=focus.status==='RUNNING'?(focus.live?'진행 중':'중단됨 · 재개 대기'):(MISSION_STATUS[focus.status]||focus.status);
 const title=el('div','','ai-mission-title');title.append(el('strong','미션 · '+focus.project),el('span',status,'ai-badge '+(focus.status==='DONE'?'ok':focus.status==='RUNNING'&&focus.live?'run':'warn')));
 m.append(title,el('p',focus.goal,'ai-goal'));
 const bar=el('div','','ai-steps');
 for(let i=1;i<=Math.max(focus.max_steps||0,focus.steps.length);i++){const s=focus.steps.find(x=>x.index===i);const chip=el('span',String(i),'chip '+(s?(STEP_CLASS[s.state]||'run'):'todo'));chip.title=s?(i+'단계 · '+(s.state_ko||'진행 중')+' · '+s.goal):'남은 단계 예산';bar.append(chip);}
 m.append(bar,el('small',focus.passed_steps+'개 단계 통과 · '+focus.steps.length+'/'+focus.max_steps+'단계 사용 · 갱신 '+ago(focus.updated_at),'ai-sub'));
 const list=el('details');list.append(el('summary','단계별 결과'));
 for(const s of [...focus.steps].reverse()){const row=el('div','','ai-step '+(STEP_CLASS[s.state]||'run'));const t=s.tests?(' · 테스트 '+s.tests.passed+'/'+s.tests.total):'';row.append(el('strong',s.index+'단계 · '+(s.state_ko||s.state||'진행 중')+t+(s.review?' · 리뷰 '+s.review:'')+(s.repairs?' · 자동 수정 '+s.repairs+'회':'')+(s.builds_on_failed_attempt?' · 이전 시도 이어받음':'')),el('p',s.goal));if(s.note)row.append(el('small',s.note));list.append(row);}
 m.append(list);
 if(focus.summary){const sm=el('details');sm.append(el('summary','HEAD 요약'),el('p',focus.summary));m.append(sm);}
 if(focus.blocked_reason){m.append(el('small','멈춘 이유: '+focus.blocked_reason,'ai-sub'));}
 if(focus.deliverable)m.append(el('small','결과물 위치: '+focus.deliverable,'ai-sub path'));
 for(const d of m.querySelectorAll('details'))if(opened.has(d.querySelector('summary').textContent))d.open=true;m.scrollTop=scroll;
}
function paintWork(a){
 for(const b of doc.querySelectorAll('.ai-tabs button')){b.classList.toggle('on',b.dataset.win===usageWindow);b.disabled=b.dataset.win==='mission'&&!a.usage.mission;}
 if(usageWindow==='mission'&&!a.usage.mission)usageWindow='today';
 const win=a.usage[usageWindow],table=doc.getElementById('ai-work');table.replaceChildren();
 const header=el('div','','ai-row head');for(const h of ['역할','담당 AI (호출 수)','호출','CLI 작업 시간','토큰'])header.append(el('span',h));table.append(header);
 for(const role of ['HEAD','BUILDER','REVIEWER','OTHER']){const b=win.by_role[role];if(!b&&role==='OTHER')continue;const who=b?Object.entries(b.providers||{}).sort((x,y)=>y[1]-x[1]).map(([p,n])=>(PROVIDER[p]||p)+' '+n).join(', '):'기록 없음 (9/30 이전 호출은 미집계)';const row=el('div','','ai-row');row.append(el('span',ROLE[role]),el('span',who),el('span',b?String(b.calls)+(b.failed?' (실패 '+b.failed+')':''):'0'),el('span',b?fmtTime(b.seconds):'—'),el('span',fmtTokens(b)));table.append(row);}
 const tot=el('div','','ai-row total');tot.append(el('span','합계'),el('span',''),el('span',String(win.total.calls)),el('span',fmtTime(win.total.seconds)),el('span',fmtTokens(win.total)));table.append(tot);
 for(const p of ['OPENAI','GOOGLE','ANTHROPIC']){const node=doc.getElementById('node-'+p);if(!node)continue;let line=node.querySelector('.ai-card-usage');if(!line){line=el('p','','ai-card-usage');node.append(line);}const b=a.usage.today.by_provider[p],busy=(a.working_now||[]).find(w=>w.provider===p);line.textContent=(busy?'● 작업 중 · '+(ROLE[busy.role]||busy.role)+' · '+fmtTime(busy.elapsed_sec)+' — ':'')+'오늘 '+(b?b.calls+'회 · '+fmtTime(b.seconds)+' · 토큰 '+fmtTokens(b):'호출 없음');line.classList.toggle('busy',!!busy);}
}
function paintLists(a){
 const calls=doc.getElementById('ai-calls');calls.replaceChildren();
 for(const c of a.recent_calls||[])calls.append(el('div',ago(c.at)+' · '+(ROLE[c.role]||c.role||'?')+' · '+(PROVIDER[c.provider]||c.provider)+' · '+fmtTime(c.seconds)+' · '+(c.tokens?((c.exact?'':'~')+c.tokens.toLocaleString('ko-KR')+' 토큰'):'토큰 기록 전')+(c.status!=='OK'?' · '+c.status:''),'ai-item'));
 if(!calls.children.length)calls.append(el('div','아직 기록된 AI 호출이 없습니다.','ai-item'));
 const events=doc.getElementById('ai-events');events.replaceChildren();
 for(const e of a.events||[])events.append(el('div',ago(e.at)+' · '+e.project+' · '+(e.state_ko||e.to_state)+(e.details?' — '+e.details:''),'ai-item'));
}
async function refreshActivity(){if(activityBusy||!doc||!doc.getElementById('ai-activity'))return;activityBusy=true;try{
 const response=await fetch('/ops/activity',{cache:'no-store'});if(!response.ok)throw Error('작업 현황 조회 실패');const a=await response.json();if(a.error)throw Error(a.error);
 const focus=(a.missions||[]).find(m=>m.mission_id===a.focus_mission_id);
 paintNow(a,focus);paintMission(focus);paintWork(a);paintLists(a);
 }catch(e){const now=doc.getElementById('ai-now');if(now)now.textContent='작업 현황 연결 중 · '+e.message;}finally{activityBusy=false;}}

// ---------------------------------------------------------------- project folder: native Windows dialog instead of typing a path
async function chooseProjectFolder(){
 const roles=await fetch('/ops/roles',{cache:'no-store'}).then(r=>r.json());
 const picked=await fetch('/ops/pick-folder',{method:'POST',headers:{'X-Solomon-Bridge':roles.csrf}}).then(r=>r.json());
 if(picked.error)throw Error(picked.error);
 if(!picked.path)return null;
 const res=await fetch('/project',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8','Accept':'application/json'},body:new URLSearchParams({path:picked.path}).toString()}).then(r=>r.json());
 return {path:picked.path,ok:res.status==='ok',message:res.message||''};
}
function setupFolderPicker(){
 const old=doc.getElementById('project-change'),form=doc.getElementById('project-form');if(!old||!form)return;
 const button=old.cloneNode(true);old.replaceWith(button);button.textContent='폴더 선택…';
 const manual=el('button','경로 직접 입력','ghost');manual.type='button';manual.onclick=()=>{form.style.display=form.style.display==='none'?'':'none';if(form.style.display!=='none')doc.getElementById('project-input').focus();};
 button.after(' ',manual);
 const note=doc.getElementById('project-result');
 button.onclick=async()=>{if(button.disabled)return;button.disabled=true;const label=button.textContent;button.textContent='폴더 선택 창 열림…';
  try{const r=await chooseProjectFolder();if(r){form.style.display='';if(note)note.textContent=r.ok?'선택됨 · '+r.path:r.message;if(r.ok){form.style.display='none';doc.getElementById('project-name').textContent=r.path.split(/[\/]/).filter(Boolean).pop();doc.getElementById('project-path').textContent=r.path;}}}
  catch(e){form.style.display='';if(note)note.textContent='폴더 선택 창을 열지 못했습니다. 경로를 직접 입력해 주세요. ('+e.message+')';}
  finally{button.disabled=false;button.textContent=label;refresh();}};
}

// ---------------------------------------------------------------- 시스템 중지 (2026-11-02): stop the companion from the console
function showVeil(title,detail){
 let veil=document.getElementById('shutdown-veil');
 if(!veil){veil=document.createElement('div');veil.id='shutdown-veil';document.body.append(veil);}
 veil.innerHTML='<div><strong></strong><p class="veil-detail"></p><p>다시 켜려면 솔로몬 집무실 상단의 <code>AI DEV 켜기</code> 버튼, 또는 <code>start-companion.ps1</code>을 실행하세요.</p></div>';
 veil.querySelector('strong').textContent=title;veil.querySelector('.veil-detail').textContent=detail||'';
 document.body.classList.add('offline');
}
function offlineVeil(misses){
 window.__pollMisses=misses;
 if(misses===0){if(!window.__stoppedByButton){document.getElementById('shutdown-veil')?.remove();document.body.classList.remove('offline');}return;}
 if(misses>=3&&!document.getElementById('shutdown-veil'))showVeil('AI DEV가 꺼져 있습니다','서버(8081)가 응답하지 않습니다. 화면의 움직임은 브라우저 안의 연출일 뿐, 실행 중인 작업은 없습니다.');
}
// heartbeat: the stage view does not run the workshop status poll, so check the server itself every 5 s
setInterval(async()=>{try{const r=await fetch('/ops/roles',{cache:'no-store'});if(!r.ok)throw Error(r.status);offlineVeil(0);}catch(e){offlineVeil((window.__pollMisses||0)+1);}},5000);
(()=>{
 const b=document.getElementById('btn-shutdown');if(!b)return;
 b.onclick=async()=>{
  if(!confirm('AI DEV를 멈출까요?\n진행 중인 실행이 있으면 중단됩니다. 기록과 작업본은 남고, 다시 켜면 중단된 미션을 이어갑니다.'))return;
  b.disabled=true;b.textContent='멈추는 중…';
  try{
   const roles=await fetch('/ops/roles',{cache:'no-store'}).then(r=>r.json());
   const r=await fetch('/ops/shutdown',{method:'POST',headers:{'X-Solomon-Bridge':roles.csrf}});const j=await r.json();
   if(!r.ok)throw Error(j.error||'중지 실패');
   window.__stoppedByButton=true;showVeil('AI DEV가 멈췄습니다',j.message);
  }catch(e){alert('중지 실패 · '+e.message);b.disabled=false;b.textContent='시스템 중지';}
 };
})();

// ---------------------------------------------------------------- 작업 정지 (2026-11-03): stop running missions, keep the dashboard
(()=>{
 const b=document.getElementById('btn-halt');if(!b)return;
 b.onclick=async()=>{
  if(!confirm('실행 중인 미션을 멈출까요?\n지금 돌고 있는 AI 프로세스를 끊고 미션을 정지 상태로 둡니다. 기록과 작업본은 남고, 나중에 재개할 수 있습니다.'))return;
  b.disabled=true;const was=b.textContent;b.textContent='정지 중…';
  try{
   const roles=await fetch('/ops/roles',{cache:'no-store'}).then(r=>r.json());
   const r=await fetch('/ops/missions/stop',{method:'POST',headers:{'Content-Type':'application/json','X-Solomon-Bridge':roles.csrf},body:JSON.stringify({reason:'console 작업 정지 버튼'})});
   const j=await r.json();if(!r.ok)throw Error(j.error||'정지 실패');
   alert(j.status==='stopped'?('미션 '+j.missions.join(', ')+' 정지 · AI 프로세스 '+j.killed.length+'개 종료'):'실행 중인 미션이 없습니다.');
  }catch(e){alert('정지 실패 · '+e.message);}finally{b.disabled=false;b.textContent=was;}
 };
})();
