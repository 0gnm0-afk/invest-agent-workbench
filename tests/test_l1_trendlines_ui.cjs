'use strict';
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
global.window={};
vm.runInThisContext(fs.readFileSync(__dirname+'/../dashboard/drawings/l1-trendlines.js','utf8'));
const api=window.WorkbenchL1Trendlines;
function fakeClock() {
  let now=0, serial=0;const pending=new Map();
  return {
    setTimer(fn,ms){const id=++serial;pending.set(id,{at:now+ms,fn});return id;},
    clearTimer(id){pending.delete(id);},
    tick(ms){now+=ms;const due=[...pending].filter(([,v])=>v.at<=now);for(const [id,v] of due){pending.delete(id);v.fn();}},
    size:()=>pending.size
  };
}
const settle=async()=>{await Promise.resolve();await Promise.resolve();};
const packet=(tag='2026-09-30')=>({status:'ok',window:{start:'2026-08-01',end:tag,timeframe:'1d'},
  calculated_window:{start:'2026-08-03',end:tag,timeframe:'1d'},lines:[
    {direction:'up',points:[{time:'2026-08-03',price:100,logical:0},{time:'2026-08-20',price:110,logical:13}],in_progress:false},
    {direction:'down',points:[{time:'2026-08-21',price:120,logical:14},{time:tag,price:115,logical:40}],in_progress:true}
  ]});
async function controllerContract() {
  const clock=fakeClock(), requests=[],renders=[],states=[];let clears=0,current=true;
  const renderer={render:p=>renders.push(p),clear:()=>clears++,status:s=>states.push(s)};
  const controller=api.createController({request:input=>new Promise((resolve,reject)=>requests.push({input,resolve,reject})),
    renderer,isCurrent:()=>current,...clock});
  const state={drawings:[{id:'user-only',points:[{time:'2026-01-01',price:20}]}]}, before=JSON.stringify(state);
  controller.update({stock_key:'US:SYNTH',start:'2026-06-01',end:'2026-09-30',timeframe:'1d',state});
  clock.tick(499);assert.equal(requests.length,0);
  controller.update({stock_key:'US:SYNTH',start:'2026-08-01',end:'2026-09-30',timeframe:'1d',state});
  clock.tick(499);assert.equal(requests.length,0);clock.tick(1);assert.equal(requests.length,1);
  assert.deepEqual(requests[0].input,{stock_key:'US:SYNTH',start:'2026-08-01',end:'2026-09-30',timeframe:'1d'});
  // Invalidate an active request at viewport movement, before the debounce fires.
  controller.update({stock_key:'US:SYNTH',timeframe:'1w'});
  requests[0].resolve(packet());await settle();assert.equal(renders.length,0);
  clock.tick(500);assert.deepEqual(requests[1].input,{stock_key:'US:SYNTH',timeframe:'1w'});
  controller.update({stock_key:'US:SYNTH',timeframe:'1mo'});clock.tick(500);
  assert.deepEqual(requests[2].input,{stock_key:'US:SYNTH',timeframe:'1mo'});
  requests[2].resolve({...packet(),window:{start:'2024-01-01',end:'2026-09-01',timeframe:'1mo'}});
  await settle();assert.equal(renders.length,1);
  requests[1].reject(Error('stale'));await settle();assert.equal(renders.length,1);assert(!states.includes('failed'));
  controller.update({stock_key:'US:SYNTH'});clock.tick(500);
  assert.deepEqual(requests[3].input,{stock_key:'US:SYNTH',timeframe:'1d'});
  requests[3].reject(Error('read failed'));await settle();assert.equal(states.at(-1),'failed');
  controller.update({stock_key:'US:SYNTH'});clock.tick(500);current=false;
  requests[4].resolve(packet());await settle();assert.equal(renders.length,1);
  current=true;controller.update({stock_key:'US:SYNTH'});clock.tick(500);
  controller.dispose();requests[5].resolve(packet());await settle();assert.equal(renders.length,1);
  controller.update({stock_key:'US:SYNTH'});clock.tick(500);assert.equal(requests.length,6);assert.equal(clock.size(),0);
  controller.dispose();assert.equal(JSON.stringify(state),before);assert(clears>=7);
  const waiting=api.createController({request:()=>{throw Error('must stay cancelled');},renderer,...clock});
  waiting.update({stock_key:'US:SYNTH'});waiting.dispose();clock.tick(500);assert.equal(clock.size(),0);
  const invalid=api.createController({request:()=>{},renderer,...clock});
  assert.throws(()=>invalid.update({stock_key:'US:SYNTH',timeframe:'1h'}),/unsupported_timeframe/);
  assert.throws(()=>invalid.update({stock_key:'US:SYNTH',start:'2026-10-02',end:'2026-10-01'}),/invalid_window/);
  assert.throws(()=>invalid.update({stock_key:'US:SYNTH',start:'invalid'}),/invalid_window/);
  assert.throws(()=>invalid.update({}),/stock_key_required/);invalid.dispose();
}
function overlayContract() {
  const added=[],removed=[],legend={textContent:''},lineSeries={};
  const chart={addSeries(type,options){const ref={setData(data){this.data=data;}};added.push({type,options,ref});return ref;},removeSeries:ref=>removed.push(ref)};
  const overlay=api.createChartOverlay({chart,lineSeries,lineStyles:{Solid:10,Dashed:20},legend});
  const value=packet(),before=JSON.stringify(value);
  overlay.render(value);assert.equal(added.length,2);
  assert.equal(added[0].type,lineSeries);assert.equal(added[0].options.lineStyle,10);assert.equal(added[1].options.lineStyle,20);
  assert.match(added[1].options.title,/진행 중/);assert(!added[0].options.title.includes('진행 중'));
  assert.equal(added[0].options.autoscaleInfoProvider(),null);
  assert.equal(added[0].options.priceLineVisible,false);assert.equal(added[0].options.lastValueVisible,false);
  assert.deepEqual(added[0].ref.data,[{time:'2026-08-03',value:100},{time:'2026-08-20',value:110}]);
  assert.match(legend.textContent,/2026-08-03 ~ 2026-09-30 · 일봉/);assert.match(legend.textContent,/마지막 구간 진행 중 \(점선\)/);
  assert.equal(JSON.stringify(value),before);
  overlay.render({...packet(),lines:[]});assert.equal(removed.length,2);assert.match(legend.textContent,/0개/);assert(!legend.textContent.includes('진행 중'));
  for(const status of ['insufficient_data','invalid_data','unavailable','solver_failed']){
    overlay.render({...packet(),status});assert.equal(added.length,2);assert(!legend.textContent.includes('진행 중'));
  }
  assert.throws(()=>overlay.render({...packet(),lines:[{...packet().lines[0],points:[{time:'2026-01-01',price:NaN},{time:'2026-01-02',price:1}]}]}),/invalid_l1_line/);
  assert.equal(added.length,2);overlay.clear();assert.equal(legend.textContent,'');
  return {chart,lineSeries,legend};
}
function geometryParity() {
  vm.runInThisContext(fs.readFileSync(__dirname+'/../dashboard/drawings/drawing-tools.js','utf8'));
  // Same fixed projection vectors as AnalysisTests.test_geometry_all_kinds_frame_and_js_projection_parity.
  const days=['2026-09-18','2026-09-19','2026-09-21','2026-09-22','2026-09-23','2027-01-05'];
  const frames={
    '1d':{bars:['2026-09-18','2026-09-21','2026-09-22','2026-09-23'].map(date=>({date})),expected:[0,1/3,1,2,3,77]},
    '1w':{bars:[{date:'2026-09-14',last_session:'2026-09-18'},{date:'2026-09-21',last_session:'2026-09-23'}],expected:[-.4+.8*4/7,-.4+.8*5/7,.6,.6+.8/7,.6+.8*2/7,15.6+.8/7]},
    '1mo':{bars:[{date:'2026-09-01',last_session:'2026-09-23'}],expected:[-.4+.8*17/30,-.4+.8*18/30,-.4+.8*20/30,-.4+.8*21/30,-.4+.8*22/30,3.6+.8*4/31]}
  };
  for(const [frame,{bars,expected}] of Object.entries(frames)){
    const timeline=window.DrawingTools.timeline(bars,frame);
    days.forEach((day,i)=>assert(Math.abs(window.DrawingTools.index(day,timeline,frame)-expected[i])<1e-10,frame+':'+day));
  }
}
(async()=>{
  await controllerContract();const chartOptions=overlayContract();geometryParity();
  const clock=fakeClock();let calls=0;
  const attached=api.attach({...chartOptions,...clock,request:async()=>{calls++;return packet();}});
  attached.update({stock_key:'US:SYNTH'});clock.tick(500);await settle();assert.equal(calls,1);
  assert.match(chartOptions.legend.textContent,/자동 L1/);attached.dispose();assert.equal(chartOptions.legend.textContent,'');
  console.log('L1 UI: debounce, latest-only responses, close/cancel, default frames, read errors, line styles, immutable payload; 18 Python/JS projection vectors PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
