const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const source=fs.readFileSync(path.join(__dirname,'../dashboard/shell/app.js'),'utf8');
const code=source.slice(source.indexOf('async function pollDaily()'));
const failed={enabled:true,view_ready:true,state:'failed',day:'2026-10-04',attempt_id:'a1',reused:true,retry_available:true,error:'market_worker_failed',targets:{}};
function element(){
 const e={children:[],classList:{toggle(){}},append(child){this.children.push(child);},setAttribute(){},querySelector(){return null;}};
 Object.defineProperty(e,'innerHTML',{get(){return this.html||'';},set(v){this.html=v;this.children=[];}});
 return e;
}
function setup(initial=failed,hash='#stock/US/AAA'){
 const host=element(),timers=new Map(),calls=[];
 let status={...initial},serial=0,readError=false,closeAllowed=true,healthGate=null,postOverride=null;
 const saved={marker:'visible-before'};let next={marker:'new-result'};
 const c=vm.createContext({$:()=>host,esc:x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),num:String,stamp:String,
  document:{createElement:element},location:{hash},DATA:saved,
  WorkbenchWorkspace:{closeActive:async()=>{calls.push(['close']);return closeAllowed;}},
  route:async()=>{calls.push(['route']);},
  setTimeout(fn,delay){const id=++serial;timers.set(id,{fn,delay});return id;},
  clearTimeout(id){timers.delete(id);},
  fetch:async(url,options={})=>{
   calls.push([url,options]);
   if(url==='/health'){if(healthGate)await healthGate;return reply({token:'session-token'});}
   if(url==='/api/home')return reply(next);
   if(url==='/api/daily-update'&&options.method==='POST'){
    if(postOverride)return postOverride();
    status={...status,attempt_id:'a2',state:'running',view_ready:false,reused:false,manual_retry:true,retry_available:false,error:null};
    return reply(status,202);
   }
   if(readError)throw Error('offline');
   return reply(status);
  }
 });
 const reply=(body,status=200)=>({ok:status>=200&&status<300,status,json:async()=>body});
 vm.runInContext(code,c);
 return {c,host,calls,timers,saved,get next(){return next;},set next(value){next=value;},reply,
  set status(v){status=v;},get status(){return status;},
  set readError(v){readError=v;},set closeAllowed(v){closeAllowed=v;},set healthGate(v){healthGate=v;},set postOverride(v){postOverride=v;},
  button(id){return host.children.flatMap(e=>e.children||[]).find(e=>e.id===id);},
  text(){return host.innerHTML+' '+host.children.flatMap(e=>e.children||[]).map(e=>e.textContent||'').join(' ');},
  async start(){return c.pollDaily();},
  async refresh(){return c.pollDaily.controller.refresh();}
 };
}
(async()=>{
 let observer=setup({...failed,observe_only:true,retry_available:false});await observer.start();
 assert.match(observer.text(),/새 수집 결과 자동 반영/);assert.equal(observer.button('daily-retry'),undefined);
 let h=setup({...failed,view_ready:false,retry_available:false,error:'basis_document_changed_review_required',account_attempted:true,account_state:'received'});await h.start();
 assert.match(h.text(),/오늘의 갱신에 오류/);
 assert.match(h.text(),/공식 문서 변경/);
 assert.match(h.text(),/계좌 갱신 완료/);
 assert.match(h.text(),/저장 화면 준비 중/);
 assert.doesNotMatch(h.text(),/오늘의 종가·계좌 갱신 중|갱신 진행 중…/);
 assert.equal(h.button('daily-retry').textContent,'화면 준비 중…');
 assert.equal(h.button('daily-retry').disabled,true);
 h.status={...h.status,view_ready:true,retry_available:true};await h.refresh();
 assert.equal(h.button('daily-retry').disabled,false);
 assert.equal(h.button('daily-retry').textContent,'오늘의 갱신 재시도');
 h=setup({...failed,state:'succeeded',error:null,view_ready:false,retry_available:false});await h.start();
 assert.match(h.text(),/오늘의 갱신 완료/);
 assert.doesNotMatch(h.text(),/오늘의 종가·계좌 갱신 중/);
 assert.equal(h.button('daily-retry').disabled,true);
 h=setup();await h.start();
 assert.match(h.text(),/시장 자료 수집 작업이 오류로 종료/);
 assert.equal(h.button('daily-retry').disabled,false);
 assert.equal(h.calls.filter(c=>c[0]==='/api/home').length,1,'stock view automatically opens the current saved result');
 assert.equal([...h.timers.values()][0].delay,10000,'idle continues to observe another window');
 await h.start();assert.equal(h.timers.size,1,'controller is not duplicated');

 let unlock;h.healthGate=new Promise(resolve=>{unlock=resolve;});
 const first=h.button('daily-retry').onclick();await Promise.resolve();
 assert.equal(h.button('daily-retry').disabled,true);
 await h.button('daily-retry').onclick();
 unlock();await first;
 const posts=h.calls.filter(c=>c[1]?.method==='POST');
 assert.equal(posts.length,1,'double click starts only one request');
 assert.equal(posts[0][1].headers['X-Workbench-Token'],'session-token');
 assert.deepEqual(JSON.parse(posts[0][1].body),{retry:true,attempt_id:'a1'});
 assert.equal(h.button('daily-retry').disabled,true);
 assert.match(h.text(),/수동 재시도/);
 assert.equal(h.timers.size,1);assert.equal([...h.timers.values()][0].delay,3000);

 h.status={...h.status,state:'succeeded',view_ready:true,finished_at:'done',retry_available:false};
 const before=h.c.DATA;h.closeAllowed=false;
 await h.refresh();assert.ok(h.button('daily-result'));assert.equal(h.c.DATA,before);
 await h.button('daily-result').onclick();
 assert.equal(h.c.DATA,before,'cancelled unsaved-drawing guard keeps displayed data');
 assert.ok(h.button('daily-result'));
 h.closeAllowed=true;await h.button('daily-result').onclick();assert.equal(h.c.DATA,h.next);
 assert.equal(h.button('daily-result'),undefined);assert.equal(h.calls.filter(c=>c[0]==='route').length,2);

 h=setup(failed,'#home');await h.start();
 assert.equal(h.calls.filter(c=>c[0]==='/api/home').length,1);
 await h.refresh();assert.equal(h.calls.filter(c=>c[0]==='/api/home').length,1);
 await h.button('daily-retry').onclick();
 h.status={...h.status,state:'partial',view_ready:true,retry_available:true,finished_at:'later'};
 await h.refresh();assert.equal(h.calls.filter(c=>c[0]==='/api/home').length,2,'new attempt publishes once');
 assert.equal(h.button('daily-retry').disabled,false,'partial can be explicitly retried');
 await h.refresh();assert.equal(h.calls.filter(c=>c[0]==='/api/home').length,2);


 // In-progress drawing edits defer automatic replacement until safe.
 h=setup();h.c.WorkbenchWorkspace.current=()=>({canAutoRefresh:()=>false});await h.start();
 assert.equal(h.c.DATA,h.saved);assert.ok(h.button('daily-result'));
 h.c.WorkbenchWorkspace.current=()=>({canAutoRefresh:()=>true});await h.refresh();
 assert.equal(h.c.DATA,h.next);

 // A display-only retry keeps the market attempt identity but publishes a new view revision.
 for(const hash of ['#home','#stock/US/AAA']){
  h=setup({...failed,error:'dashboard_preparation_failed',revision:0,finished_at:'unchanged-finish'},hash);
  await h.start();
  const previous=h.c.DATA;
  assert.equal(h.calls.filter(c=>c[0]==='/api/home').length,1);
  h.next={marker:'repaired-view'};
  h.status={...h.status,state:'succeeded',error:null,retry_available:false,revision:1};
  await h.refresh();
  assert.equal(h.c.DATA,h.next,'new view revision applies fresh home data with unchanged attempt identity');
  assert.equal(h.calls.filter(c=>c[0]==='/api/home').length,2);
  await h.refresh();
  assert.equal(h.calls.filter(c=>c[0]==='/api/home').length,2,'each published view revision applies only once');
 }

 h=setup();await h.start();h.readError=true;await h.refresh();
 assert.match(h.text(),/시장 자료 수집 작업이 오류로 종료/,'old status remains visible through network errors');
 assert.match(h.text(),/마지막 확인 상태를 유지/);assert.ok(h.button('daily-status-retry'));
 assert.equal(h.button('daily-retry').disabled,true);assert.equal(h.c.DATA,h.next);
 h.readError=false;await h.button('daily-status-retry').onclick();assert.equal(h.button('daily-status-retry'),undefined);
 assert.equal(h.button('daily-retry').disabled,false);

 h=setup();await h.start();
 h.postOverride=()=>{h.status={...h.status,attempt_id:'other',state:'running',view_ready:false,retry_available:false};return h.reply({error:'stale_attempt'},409);};
 await h.button('daily-retry').onclick();
 assert.match(h.text(),/다른 창에서 갱신 상태/);
 assert.equal(h.button('daily-retry').disabled,true);
 assert.equal(h.calls.filter(c=>c[1]?.method==='POST').length,1,'409 refreshes without automatically retrying');

 h=setup();await h.start();h.postOverride=()=>h.reply({error:'daily_retry_unavailable'},503);
 await h.button('daily-retry').onclick();assert.match(h.text(),/바탕화면의 투자 대시보드를 다시 열어/);
 assert.equal(h.calls.filter(c=>c[1]?.method==='POST').length,1);

 h=setup({...failed,display_updated:true,display_stale:true});await h.start();
 assert.match(h.text(),/이후 저장 결과 표시/);assert.doesNotMatch(h.text(),/갱신 전 저장 결과/);
 assert.match(h.text(),/오늘의 갱신에 오류/,'new saved data does not erase failed daily attempt');

 h=setup({...failed,state:'interrupted',error:'previous_launch_interrupted'});await h.start();
 assert.equal(h.button('daily-retry').disabled,false);assert.match(h.text(),/이전 실행이 끝나기 전에 중단/);
 h.status={...failed,state:'running',view_ready:false,retry_available:false,attempt_id:'other-tab',stage:'finished'};
 await h.refresh();assert.match(h.text(),/화면 준비/);assert.equal(h.button('daily-retry').disabled,true);
 assert.equal(h.timers.size,1);

 h=setup({...failed,error:'dashboard_preparation_failed'});await h.start();assert.match(h.text(),/화면 자료를 준비하지 못/);
 h=setup({...failed,account_state:'failed',account_error:'http_400_IGW40043'});await h.start();
 assert.match(h.text(),/NH 인증 복구 후에도 토큰이 거절/);
 assert.match(h.text(),/IGW40043/);
 h=setup({...failed,account_state:'failed',account_error:'http_400_IGW40012'});await h.start();
 assert.match(h.text(),/NH가 계좌 요청을 거절/);
 h=setup({...failed,state:'running',view_ready:false,stage:'adr_metadata'});await h.start();
 assert.match(h.text(),/ADR 시총·업종 보강/);
 h=setup({enabled:false});await h.start();assert.equal(h.host.hidden,true);
 console.log('PASS: daily retry, authentication header, duplicate requests/pollers, repeated completion, unsaved drawings, network recovery, 409/503, cross-window state and error messages.');
})().catch(e=>{console.error(e);process.exitCode=1;});
