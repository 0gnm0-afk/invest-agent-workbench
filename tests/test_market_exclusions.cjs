const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.join(__dirname,'../dashboard/market-exclusions.js'),'utf8');
function element(tag='div'){
 const n={tag,children:[],dataset:{},hidden:false,append(...children){this.children.push(...children);},replaceChildren(...children){this.children=children;},setAttribute(k,v){this[k]=v;}};
 Object.defineProperty(n,'innerHTML',{set(){throw Error('unsafe HTML insertion');}});return n;
}
const issue={id:'issue-a',market:'US',symbol:'FAIL',name:'<img src=x onerror=alert(1)>',field:'시가총액',toss_reason:'자료 없음',nh_reason:'NH HTTP 400',exclude_allowed:true};
const initial={review_id:'review-a',issues:[issue,{...issue,id:'blocked',symbol:'UNKNOWN',exclude_allowed:false,block_reason:'식별자 없음'}],job:null,busy:false,view_ready:true};
const clone=x=>JSON.parse(JSON.stringify(x));
function setup(hash='#stock/US/KEEP'){
 const host=element(),timers=new Map(),calls=[];let status=clone(initial),serial=0,readFail=false,postReply=null,healthGate=null,resultAllowed=true,opens=0;
 const reply=(body,code=200)=>({ok:code>=200&&code<300,status:code,json:async()=>clone(body)});
 const context=vm.createContext({window:{},document:{createElement:element},location:{hash},setTimeout(fn,delay){const id=++serial;timers.set(id,{fn,delay});return id;},clearTimeout(id){timers.delete(id);},
  fetch:async(url,options={})=>{calls.push({url,options});
   if(url==='/health'){if(healthGate)await healthGate;return reply({token:'browser-token'});}
   if(options.method==='POST'){
    if(postReply)return postReply();
    status={...status,review_id:'review-approved',issues:[],busy:true,view_ready:false,job:{id:'job',count:1,state:'applying'}};
    return reply(status,202);
   }
   if(readFail)throw Error('read failure');return reply(status);
  }});
 vm.runInContext(source,context);
 const api=context.window.WorkbenchExclusions.mount(host,async()=>{opens++;return resultAllowed;});
 const walk=n=>[n,...n.children.flatMap(walk)];
 const find=id=>walk(host).find(n=>n.id===id);
 return {host,api,timers,calls,find,walk:()=>walk(host),set status(v){status=clone(v);},get status(){return status;},set readFail(v){readFail=v;},set postReply(v){postReply=v;},set healthGate(v){healthGate=v;},set resultAllowed(v){resultAllowed=v;},get opens(){return opens;},
  async select(){const box=walk(host).find(n=>n.dataset.issueId==='issue-a');box.checked=true;box.onchange();},
  posts(){return calls.filter(c=>c.options.method==='POST');}};
}
(async()=>{
 const a=setup();await a.api.refresh();assert.equal(a.host.hidden,false);assert.equal(a.find('scope-approve').disabled,true);
 assert.ok(a.walk().some(n=>n.textContent===issue.name));assert.equal(a.walk().find(n=>n.dataset.issueId==='blocked').disabled,true);
 await a.select();assert.equal(a.find('scope-approve').disabled,false);assert.deepEqual([...a.api.selected],['issue-a']);
 let release;a.healthGate=new Promise(resolve=>release=resolve);
 const click=a.find('scope-approve').onclick;const applying=click();await click();assert.equal(a.find('scope-approve').disabled,true);release();await applying;
 assert.equal(a.posts().length,1);const sent=JSON.parse(a.posts()[0].options.body);
 assert.deepEqual(sent,{action:'approve',review_id:'review-a',issue_ids:['issue-a']});assert.equal(a.posts()[0].options.headers['X-Workbench-Token'],'browser-token');
 assert.ok(a.walk().some(n=>n.textContent?.includes('재계산')));assert.ok([...a.timers.values()].some(t=>t.delay===3000));
 a.status={...a.status,issues:[issue],busy:false,view_ready:true,job:{id:'job',state:'completed',count:1}};await a.api.refresh();assert.equal(a.opens,0);
 a.resultAllowed=false;await a.find('scope-open').onclick();assert.ok(a.find('scope-open'),'cancelled navigation retains result button');
 a.resultAllowed=true;await a.find('scope-open').onclick();assert.equal(a.find('scope-open'),undefined);a.status={...a.status,issues:[]};await a.api.refresh();assert.equal(a.host.hidden,true);a.api.stop();assert.equal(a.timers.size,0);

 const b=setup();await b.api.refresh();await b.select();b.postReply=()=>{b.status={...initial,review_id:'review-new'};return {ok:false,status:409,json:async()=>({error:'exclusion_review_changed'})};};
 await b.find('scope-approve').onclick();assert.equal(b.api.selected.length,0);assert.ok(b.walk().some(n=>n.textContent?.includes('다른 갱신')));b.api.stop();

 const c=setup();await c.api.refresh();c.status={...initial,issues:[],job:{id:'persisted',state:'failed',count:1,retry_available:true}};await c.api.refresh();
 await c.find('scope-retry').onclick();assert.deepEqual(JSON.parse(c.posts()[0].options.body),{action:'retry',job_id:'persisted'});c.api.stop();

 const d=setup('#home');await d.api.refresh();d.readFail=true;await d.api.refresh();assert.ok(d.find('scope-refresh'));d.readFail=false;
 d.status={...initial,issues:[],job:{id:'done',state:'completed',count:1}};await d.api.refresh();assert.equal(d.opens,1);await d.api.refresh();assert.equal(d.opens,1);d.api.stop();

 const e=setup();await e.api.refresh();e.status={...initial,issues:[]};await e.api.refresh();assert.equal(e.host.hidden,true);e.api.stop();

 // Recovery of a read error for the same completed job must hide the entire panel.
 for(const hash of ['#home','#stock/US/KEEP']){
  const f=setup(hash);await f.api.refresh();f.status={...initial,issues:[],job:{id:'done',state:'completed',count:5}};await f.api.refresh();
  f.readFail=true;await f.api.refresh();assert.equal(f.host.hidden,false);f.readFail=false;await f.api.refresh();assert.equal(f.host.hidden,true);f.api.stop();
 }
 // Selection can retry before it can approve exclusion. Each provider has its own gate.
 const f=setup();await f.api.refresh();f.status={...initial,issues:[{...issue,exclude_allowed:false,retry:{toss:true,nh:false,attempted:[]}}]};await f.api.refresh();await f.select();
 assert.equal(f.find('scope-recollect-toss').disabled,false);assert.equal(f.find('scope-recollect-nh').disabled,true);assert.equal(f.find('scope-approve').disabled,true);
 f.postReply=()=>{f.status={...f.status,issues:[{...issue,exclude_allowed:false,retry:{toss:false,nh:true,attempted:['toss']}}],job:{id:'toss',kind:'recollect',provider:'toss',state:'completed',count:1}};return {ok:true,status:202,json:async()=>clone(f.status)}};
 await f.find('scope-recollect-toss').onclick();assert.deepEqual(JSON.parse(f.posts()[0].options.body),{action:'recollect',provider:'toss',review_id:'review-a',issue_ids:['issue-a']});
 assert.equal(f.find('scope-recollect-toss').disabled,true);assert.equal(f.find('scope-recollect-nh').disabled,false);assert.equal(f.find('scope-approve').disabled,true);
 f.postReply=()=>{f.status={...f.status,issues:[{...issue,retry:{toss:false,nh:false,attempted:['toss','nh']}}],job:{id:'nh',kind:'recollect',provider:'nh',state:'completed',count:1}};return {ok:true,status:202,json:async()=>clone(f.status)}};
 await f.find('scope-recollect-nh').onclick();assert.equal(f.find('scope-approve').disabled,false);assert.equal(JSON.parse(f.posts()[1].options.body).provider,'nh');
 f.status={...f.status,issues:[]};await f.api.refresh();assert.equal(f.host.hidden,true);f.api.stop();
 console.log('NH exclusion UI: selection, safe text, double click, stale review, retry, navigation and polling passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
