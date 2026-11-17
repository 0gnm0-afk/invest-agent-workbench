const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const source=fs.readFileSync(path.resolve(__dirname,'../dashboard/analysis/analysis.js'),'utf8');

async function flow({test=false,existing=false}={}){
  const copied=[],calls=[],routes=[];
  function node(){return {innerHTML:'',textContent:'',value:'',checked:false,disabled:false,isConnected:true,querySelectorAll:()=>[],replaceChildren(){this.innerHTML='';}};}
  const selectors={};for(const key of ['analysis-select','analysis-title','analysis-start','analysis-new','analysis-copy','analysis-state','analysis-test','analysis-proposals'])selectors['#'+key]=node();
  const panel=node();panel.querySelector=s=>selectors[s];
  const reports=node(), note=node(),holding=node();reports.querySelector=s=>({'#analysis-user-note':note,'#analysis-holding':holding}[s]);
  const detail={case:{id:'an_fixture',stock_key:'US:SYNTH',title:'합성',test_record:test,created_at:'2026-11-17',context:{}},items:existing?[{id:'it_fixture',kind:'discussion'}]:[],snapshots:[],links:{prompt:'조사 an_fixture US:SYNTH',resume_prompt:'후속 an_fixture US:SYNTH',copy_text:'차트 an_fixture US:SYNTH'}};
  let created=existing;
  const sandbox={window:{WorkbenchCollaboration:{show(){},clear(){}},addEventListener(){}},document:{activeElement:null,createElement:()=>panel,querySelector:s=>s==='#stock-price'?{before(){}}:reports},WorkbenchWorkspace:{current:()=>null},DrawingTools:{labels:{}},location:{hash:''},history:{replaceState:(_a,_b,route)=>routes.push(route)},URLSearchParams,FormData,console,setTimeout(){},navigator:{clipboard:{writeText:async text=>copied.push(text)}},fetch:async(url,opts)=>{
    if(url==='/health')return {json:async()=>({token:'synthetic',collaboration_protocol:1})};
    const {op,arguments:args}=JSON.parse(opts.body);calls.push([op,args]);
    if(op==='create'){created=true;assert.equal(args.stock_key,'US:SYNTH');assert.equal(args.test_record,test);}
    const value=op==='list'?{cases:created?[detail.case]:[]}:detail;
    return {ok:true,json:async()=>value};
  }};
  sandbox.WorkbenchCollaboration=sandbox.window.WorkbenchCollaboration;
  vm.runInNewContext(source,sandbox);
  await sandbox.window.WorkbenchAnalysis.mount({market:'US',symbol:'SYNTH',currency:'USD',controller:{ready:()=>true,context:()=>({}),restore:async()=>{}},isCurrent:()=>true,initialDetail:existing?detail:null});
  selectors['#analysis-test'].checked=test;
  await selectors['#analysis-start'].onclick();
  await selectors['#analysis-copy'].onclick();
  assert.deepEqual(copied,[existing?detail.links.resume_prompt:detail.links.prompt,existing?detail.links.resume_prompt:detail.links.prompt]);
  assert.equal(sandbox.window.WorkbenchAnalysis.currentKey(),'an_fixture');
  if(!existing)assert(routes.some(route=>route.endsWith('?analysis=an_fixture')));
  assert(panel.innerHTML.includes('세션에 붙여 넣을 문맥 복사'));
  assert(!/codex|[a-z]+:\/\/new|analysis-open/i.test(panel.innerHTML));
  for(const [op,args] of calls.filter(([op])=>op==='context'))assert.equal(args.analysis_key,'an_fixture');
}

(async()=>{
  await flow();await flow({existing:true});await flow({test:true});
  assert(!/codex|analysis-open|location\.href/i.test(source));
  console.log('analysis UI: new, continuation and validation copy the same analysis context PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
