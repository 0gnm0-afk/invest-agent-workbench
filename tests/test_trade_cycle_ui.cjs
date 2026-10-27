'use strict';
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
global.window={};vm.runInThisContext(fs.readFileSync('dashboard/shell/format.js','utf8'));global.WorkbenchFormat=window.WorkbenchFormat;
vm.runInThisContext(fs.readFileSync('dashboard/cycles/trade-cycle.js','utf8'));
const ui=window.WorkbenchTradeCycle;
const base=ui.incompletePlan({stock_key:'US:SYNTH',currency:'USD',budget:'1,000.50',initial_holding:{quantity:null,average_price:null}});
const normalized=ui.normalizePlan(base);assert.equal(normalized.budget,'1000.50');assert.equal(normalized.initial_holding.quantity,null);assert.equal(normalized.buy_steps[0].confirmation_price,null);assert.equal(base.budget,'1,000.50');
assert.deepEqual(ui.incompletePlan({currency:'USD'}).initial_holding,{quantity:null,average_price:null});
assert.deepEqual(ui.normalizePlan({...base,initial_holding:null}).initial_holding,{quantity:null,average_price:null});
assert.deepEqual(ui.normalizePlan({...base,initial_holding:{quantity:0,average_price:'0'}}).initial_holding,{quantity:0,average_price:'0'});
const unavailable=ui.contextHTML({query_state:'failed',stale:true,accounts:[],as_of:'2026-10-01',initial_holding:{quantity:null,average_price:null}});
assert.match(unavailable,/조회 실패/);assert.match(unavailable,/오래된 저장 자료/);assert.match(unavailable,/2026-10-01/);assert.match(unavailable,/시작 보유 수량 미확인/);
const confirmed=ui.contextHTML({query_state:'received',stale:false,accounts:[{alias:'synthetic'}],as_of:'2026-10-13',initial_holding:{quantity:0,average_price:'0'}});
assert.match(confirmed,/계좌 자료 확인됨/);assert.doesNotMatch(confirmed,/보유 수량 미확인|계좌 목록 미확보|cycle-warning/);
assert.doesNotMatch(ui.contextHTML({context_state:'unavailable',context_error:'<script>bad</script>'}),/<script>/);
assert.throws(()=>ui.normalizePlan({...base,budget:'10e3'}),/숫자/);assert.throws(()=>ui.normalizePlan({...base,initial_holding:{quantity:1.5,average_price:'20'}}),/정수/);
const metric=pnl=>({phase:'holding',cash:'650.5',quantity:2,average_price:'100.50',total_pnl:pnl,realized_pnl:'0',unrealized_pnl:pnl,equity:'1100',return_pct:'10',mark_price:'150'});
const a={id:'a',buy_ratios:[20,30,50],sell_ratios:[50,30,20],outcomes:{bullish:metric('100.05'),neutral:metric('25'),bearish:metric('-50')},pareto:true};
const b={id:'b',buy_ratios:[50,30,20],sell_ratios:[20,30,50],outcomes:{bullish:metric('60'),neutral:metric('30'),bearish:metric('-20')},pareto:true};
const c={id:'c',buy_ratios:[30,20,50],sell_ratios:[20,50,30],outcomes:{bullish:metric('40'),neutral:metric('15'),bearish:metric('-80')},pareto:false,duplicate_of:'a'};
const result={combinations:[a,b,c],representatives:{upside:'a',defensive:'b',balanced:'b'},result_count:108,needs_input:[]};
assert.deepEqual(ui.representativeIds(result),['a','b']);assert.equal(ui.orderedCombinations(result,'upside')[0].id,'a');assert.equal(ui.orderedCombinations(result,'defensive')[0].id,'b');assert.equal(ui.orderedCombinations(result,'balanced')[0].id,'b');assert.equal(result.combinations[0].id,'a');
const representatives=ui.comparisonHTML(result,'a','USD','balanced',false),all=ui.comparisonHTML(result,'a','USD','balanced',true);
assert.match(representatives,/108 \/ 108개 경로 계산 완료/);assert.match(representatives,/100\.05/);assert.match(representatives,/-50\.00/);assert.doesNotMatch(representatives,/value="c"/);assert.match(all,/value="c"/);assert.match(all,/동일 계산 결과/);assert.equal((all.match(/data-ratio=/g)||[]).length,12);
const escaped=ui.editorHTML({...base,rationale:'<img src=x onerror=x()>',unresolved:['<script>bad</script>']});assert.match(escaped,/&lt;img/);assert.doesNotMatch(escaped,/<script>bad/);assert.match(escaped,/data-trigger="stop:kind"/);assert.match(escaped,/scenarios.bullish.assumption/);
const fills=[{kind:'sell',step:1,quantity:1,price:'120',at:'2026-10-13T01:00:00Z'},{kind:'buy',step:1,quantity:2,price:'100',at:'2026-10-12T01:00:00Z'}];
assert.deepEqual(ui.chartMarkers(fills).map(x=>x.time),['2026-10-12','2026-10-13']);assert.match(ui.chartMarkers(fills)[0].text,/실제 매수1 2주/);
assert.equal(ui.chartMarkers([{kind:'buy',step:1,quantity:1,at:'2026-10-12T16:30:00Z'}])[0].time,'2026-10-13');
let lines=[],markers=[];global.LightweightCharts={LineStyle:{Dashed:2},createSeriesMarkers:(_,m)=>markers=m};global.WorkbenchCharts={remove:()=>{},create:(_,series,options)=>{options.onReady({},[{ref:{createPriceLine:p=>lines.push(p)}}]);return {};}};
const plan={...normalized,buy_steps:[{confirmation_price:'100'},{confirmation_price:'110'}],sell_steps:[{confirmation_price:'120'}],stop:{confirmation_price:'80'}};
ui.mountChart({}, {currency:'USD',chart:{bars:[{date:'2026-10-12',open:99,high:110,low:90,close:100},{date:'2026-10-13',open:110,high:120,low:100,close:115}]}}, {fills,state:{phase:'selling',buy_completed:[1],cancelled_buy_steps:[2]}}, plan);
assert.equal(markers.length,2);assert.deepEqual(lines.map(x=>x.title),['가정 매도 1','가정 손절']);assert.equal(lines[0].price,120);
lines=[];ui.mountChart({}, {currency:'USD',chart:{bars:[{date:'2026-10-13',open:110,high:120,low:100,close:115}]}}, {fills,state:{phase:'closed'}}, plan);assert.equal(lines.length,0);
const cycle={adopted:{version:2,plan,combination_id:'a'},state:{...metric('15'),phase:'closed'},fills,original_outcomes:{bullish:metric('50')},current_outcomes:{bullish:metric('60')}};
const progress=ui.progressHTML(cycle,'USD');assert.match(progress,/원래 확정한 계획과 현재 전망 비교/);assert.match(progress,/50\.00/);assert.match(progress,/60\.00/);assert.match(progress,/실제 거래 기록/);assert.match(progress,/새 사이클 시작/);
assert.match(ui.progressHTML({...cycle,state:{...cycle.state,phase:'selling',cancelled_buy_steps:[2]}},'USD'),/매도 전환으로 취소/);
assert.match(ui.editorHTML({...base,stop:null}),/data-trigger="stop:kind"/);
const index=fs.readFileSync('dashboard/shell/index.html','utf8');assert.match(index,/src="\/trade-cycle.js"/);assert.match(index,/href="#cycles"/);
async function storedRecordWhileContextPending(){
 const original={fetch:global.fetch,location:global.location,document:global.document,setTimeout:global.setTimeout,clearTimeout:global.clearTimeout};
 // A small DOM substitute records rendering and input handlers without a server.
 class Element{
  constructor(){this.nodes=new Map();this.textContent='';this.value='';this.isConnected=true;this.classList={add(){},contains(){return false;},toggle(){}};}
  set innerHTML(value){this.html=value;this.nodes.clear();}
  get innerHTML(){return this.html||'';}
  querySelector(selector){if(selector==='[data-fill-form]')return null;if(!this.nodes.has(selector))this.nodes.set(selector,new Element());return this.nodes.get(selector);}
  querySelectorAll(selector){return selector==='[data-plan]'?[this.querySelector('[data-plan="budget"]')]:[];}
  addEventListener(){}
  contains(){return false;}
 }
 global.setTimeout=()=>1;global.clearTimeout=()=>{};global.document={activeElement:null};
 try{
  for(const outcome of ['failed','ready']){
   let resolveContext;const contextResponse=new Promise(resolve=>resolveContext=resolve),calls=[];
   const saved={...cycle,cycle_id:'cy_synthetic',stock_key:'US:SYNTH',revision:4,draft_version:2,
    draft:{version:2,plan,source:{as_of:'2026-10-01'}},next_steps:[]};
   const host=new Element();host.querySelector('[data-plan="budget"]').dataset={plan:'budget'};
   const queryAll=host.querySelectorAll.bind(host);host.querySelectorAll=selector=>{const rows=queryAll(selector);for(const row of rows)row.dataset={plan:'budget'};return rows;};
   global.location={hash:'#cycle?id=cy_synthetic'};
   global.fetch=async(url,options)=>{
    if(url==='/health')return {ok:true,json:async()=>({token:'synthetic'})};
    const request=JSON.parse(options.body);calls.push(request);
    if(request.op==='cycle_read')return {ok:true,json:async()=>structuredClone({cycle:saved,job:{state:'idle'}})};
    if(request.op==='cycle_context')return contextResponse;
    if(request.op==='cycle_calculate')return {ok:true,json:async()=>result};
    throw Error('unexpected RPC '+request.op);
   };
   const mounted=await Promise.race([ui.mount(host,{page:'cycle'}).then(()=>true),new Promise(resolve=>original.setTimeout(()=>resolve(false),200))]);
   assert.equal(mounted,true,'stored record must render without waiting for current context');
   assert.match(host.innerHTML,/현재 진행/);assert.match(host.innerHTML,/실제 거래 입력/);assert.match(host.innerHTML,/120\.00/);
   assert.match(host.querySelector('[data-cycle-context]').innerHTML,/불러오는 중/);
   const budget=host.querySelector('[data-plan="budget"]');budget.value='1234';budget.oninput();
   const rendered=host.innerHTML;
   resolveContext(outcome==='failed'?{ok:false,json:async()=>({error:'unknown_account'})}:{ok:true,json:async()=>({
    name:'Synthetic name',currency:'USD',query_state:'received',stale:false,accounts:[{alias:'synthetic'}],
    initial_holding:{quantity:0,average_price:'0'},as_of:'2026-10-13',chart:{bars:[{date:'2026-10-13',open:110,high:120,low:100,close:115}]}})});
   await new Promise(resolve=>original.setTimeout(resolve,0));
   assert.equal(host.innerHTML,rendered,'context completion must not replace the editor or actual ledger');
   assert.equal(host.querySelector('[data-plan="budget"]'),budget);assert.equal(budget.value,'1234');
   const notice=host.querySelector('[data-cycle-context]').innerHTML;
   if(outcome==='failed'){assert.match(notice,/현재 계좌 목록에 해당 계좌가 없습니다/);assert.match(notice,/계속 확인/);assert.match(notice,/2026-10-01/);}
   else{assert.match(notice,/확인됨/);assert.match(notice,/2026-10-13/);assert.match(notice,/계획 입력의 계좌 기준 2026-10-01/);assert.equal(host.querySelector('[data-cycle-name]').textContent,'Synthetic name');assert.match(host.querySelector('[data-cycle-price-date]').textContent,/2026-10-13/);}
   assert.equal(calls.filter(c=>c.op==='cycle_context').length,1);assert.ok(calls.every(c=>['cycle_read','cycle_context','cycle_calculate'].includes(c.op)));
  }
 }finally{Object.assign(global,original);}
}
storedRecordWhileContextPending().then(()=>console.log('trade-cycle UI PASS: unknown/confirmed holdings, account status, pending/failed/current context independent of saved records and edited input, existing calculation/format/marker/asset checks')).catch(error=>{console.error(error);process.exitCode=1;});
