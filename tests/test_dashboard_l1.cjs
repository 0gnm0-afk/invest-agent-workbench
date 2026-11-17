'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const root=path.resolve(__dirname,'..');
const source=file=>fs.readFileSync(path.join(root,file),'utf8');
const plain=value=>JSON.parse(JSON.stringify(value));
const settle=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};
function clock(){
  let now=0,id=0;const jobs=new Map();
  return {setTimeout(fn,ms){jobs.set(++id,{at:now+ms,fn});return id;},clearTimeout(id){jobs.delete(id);},
    tick(ms){now+=ms;for(const [id,job] of [...jobs])if(job.at<=now){jobs.delete(id);job.fn();}},size:()=>jobs.size};
}
function element(){
  const children=new Map();
  return {textContent:'',style:{},dataset:{},clientHeight:400,children:[],classList:{toggle(){}},
    querySelector(s){if(!children.has(s))children.set(s,element());return children.get(s);},querySelectorAll(){return [];},
    setAttribute(){},append(...values){this.children.push(...values);},replaceChildren(...values){this.children=values;},
    insertAdjacentElement(position,value){this.adjacent=value;},remove(){this.removed=true;}};
}
function fixture(){
  const timer=clock(),host=element(),plot=element(),legend=element(),documentRoot=element();plot.parentElement=element();
  const requests=[],pending=[],added=[],removed=[],listeners=new Set();let current=true,chartRemoved=false;
  let visible={from:'2026-08-03',to:'2026-09-30'},logical={from:0,to:29};
  const scale={getVisibleRange:()=>visible,getVisibleLogicalRange:()=>logical,
    setVisibleLogicalRange(value){logical=value;listeners.forEach(fn=>fn(value));},
    subscribeVisibleLogicalRangeChange:fn=>listeners.add(fn),unsubscribeVisibleLogicalRangeChange:fn=>listeners.delete(fn)};
  const price={priceScale:()=>({applyOptions(){}})};
  const chart={timeScale:()=>scale,subscribeCrosshairMove(){},addSeries(type,options){
    const ref={setData(data){this.data=data;}};added.push({type,options,ref});return ref;
  },removeSeries(ref){removed.push(ref);}};
  const state={averages:[],drawings:[{id:'saved-user-line',kind:'trend',points:[{time:'2026-08-03',price:10},{time:'2026-09-30',price:20}]}]};
  const packet={state,revision:7,drawing_limit:300,drawing_extensions:'bounded-v1',average_limit:7,
    drawing_kinds:['trend','riskreward'],extension_kinds:['box','riskreward']};
  const bars=[{date:'2026-08-03',open:10,high:12,low:9,close:11,volume:4},
    {date:'2026-09-30',open:20,high:22,low:19,close:21,volume:5}];
  const context={console,URLSearchParams,Date,Map,Math,Event:function(type){this.type=type;},...timer,
    document:{createElement:element,querySelector:s=>documentRoot.querySelector(s),addEventListener(){},removeEventListener(){}},
    window:{addEventListener(){},removeEventListener(){},dispatchEvent(){}},
    LightweightCharts:{LineSeries:{},LineStyle:{Solid:0,Dashed:2}},WorkbenchFormat:{money:String},
    DrawingTools:{labels:{trend:'추세선'},withAnchorTimes:values=>values,timeline:values=>values,index:()=>0,
      attach(options){return {isEditing:()=>false,destroy(){},select(){},getDrawings:options.getDrawings};}},
    WorkbenchCharts:{create(el,series,options){options.onReady(chart,[{ref:price}]);return chart;},
      enableFreePan:()=>()=>{},remove(){chartRemoved=true;}},
    fetch(url,options={}){
      requests.push({url,options});
      if(url.startsWith('/api/l1-trendlines/'))return new Promise((resolve,reject)=>pending.push({url,resolve,reject}));
      if(url.startsWith('/api/chart-state/'))return Promise.resolve({ok:true,json:async()=>packet});
      if(url.startsWith('/api/chart/'))return Promise.resolve({ok:true,json:async()=>({bars})});
      if(url.startsWith('/api/averages/'))return Promise.resolve({ok:true,json:async()=>({})});
      throw Error('Unexpected request: '+url);
    }};
  vm.createContext(context);
  vm.runInContext(source('dashboard/drawings/l1-trendlines.js'),context);
  context.WorkbenchL1Trendlines=context.window.WorkbenchL1Trendlines;
  vm.runInContext(source('dashboard/drawings/chart-workspace.js'),context);
  return {context,timer,requests,pending,added,removed,packet,plot,listeners,
    mount:frame=>context.window.WorkbenchWorkspace.mount({host,plot,legend,stock:{bars,timeframe:frame},market:'US',symbol:'SYNTH',currency:'USD',isCurrent:()=>current}),
    move(value){visible=value;listeners.forEach(fn=>fn(logical));},setCurrent:value=>current=value,
    close:()=>context.window.WorkbenchWorkspace.closeActive(),chartRemoved:()=>chartRemoved};
}
function result(timeframe='1d'){
  return {status:'ok',window:{timeframe,start:'2026-08-03',end:'2026-09-30'},
    calculated_window:{start:'2026-08-03',end:'2026-09-30'},lines:[
      {direction:'up',in_progress:false,points:[{time:'2026-08-03',price:10},{time:'2026-08-20',price:12}]},
      {direction:'down',in_progress:true,points:[{time:'2026-08-21',price:20},{time:'2026-09-30',price:18}]}]};
}
const respond=(request,packet)=>request.resolve({ok:true,json:async()=>packet});
async function viewportContract(){
  const f=fixture(),before=JSON.stringify(f.packet);await f.mount('1d');
  f.timer.tick(179);assert.equal(f.pending.length,0);
  f.move({from:'2026-08-10',to:'2026-09-25'});f.timer.tick(179);assert.equal(f.pending.length,0);
  f.move({from:{year:2026,month:8,day:17},to:{year:2026,month:9,day:30}});f.timer.tick(180);
  assert.equal(f.pending.length,1);
  assert.equal(f.pending[0].url,'/api/l1-trendlines/US%3ASYNTH?start=2026-08-17&end=2026-09-30&timeframe=1d');
  // Moving immediately invalidates an in-flight result, before the next debounce expires.
  f.move({from:'2026-08-21',to:'2026-09-30'});respond(f.pending[0],result());await settle();assert.equal(f.added.length,0);
  f.timer.tick(180);respond(f.pending[1],result());await settle();assert.equal(f.added.length,2);
  assert.equal(f.added[0].options.lineStyle,0);assert.equal(f.added[1].options.lineStyle,2);
  assert.match(f.plot.parentElement.adjacent.textContent,/2026-08-03 ~ 2026-09-30/);
  assert.match(f.plot.parentElement.adjacent.textContent,/진행 중 \(점선\)/);
  assert.match(f.plot.parentElement.adjacent.textContent,/참고선/);
  f.move({from:'2026-08-21',to:'2026-09-30'});f.timer.tick(180);assert.equal(f.pending.length,2,'unchanged window is not recalculated');
  f.move({from:'2026-09-01',to:'2026-09-30'});f.timer.tick(180);assert.equal(f.pending.length,3);
  await f.close();respond(f.pending[2],result());await settle();assert.equal(f.added.length,2);
  assert.equal(f.chartRemoved(),true);assert.equal(f.plot.parentElement.adjacent.removed,true);
  assert.equal(f.timer.size(),0);assert.equal(f.packet.revision,7);assert.equal(JSON.stringify(f.packet),before);
  assert(f.requests.every(r=>(r.options.method||'GET')==='GET'),'viewport changes never write, collect, adopt or monitor');
}
async function frameContract(){
  for(const frame of ['1d','1w','1mo']){
    const f=fixture();await f.mount(frame);f.timer.tick(180);
    assert.equal(new URL(f.pending[0].url,'http://fixture.invalid').searchParams.get('timeframe'),frame);
    respond(f.pending[0],result(frame));await settle();assert.equal(f.added.length,2);
    f.move({from:'2026-08-04',to:'2026-09-30'});f.timer.tick(180);f.setCurrent(false);
    respond(f.pending[1],result(frame));await settle();assert.equal(f.added.length,2,'old stock route response ignored');
    await f.close();
  }
}
async function stockEntryContract(){
  const nodes=new Map(),node=s=>{if(!nodes.has(s))nodes.set(s,element());return nodes.get(s);};
  const calls=[],mounts=[],frames=['1d','1w','1mo'].map(frame=>({...element(),dataset:{frame}}));
  const document={querySelector:node,querySelectorAll:s=>s==='[data-frame]'?frames:[]};
  let activeFrame='1d';
  const value=()=>({timeframe:activeFrame,bars:[],history:{state:'not_requested',daily_count:0,can_retry:false}});
  const context={document,console,Date,Event:function(){},WorkbenchFormat:{money:String},
    window:{dispatchEvent(){}},WorkbenchWorkspace:{closeActive:async()=>true,mount:async options=>{mounts.push(options);return {range(){},context:()=>({})};}},
    fetch:async(url,options={})=>{calls.push({url,options});activeFrame=new URL(url,'http://fixture.invalid').searchParams.get('timeframe')||activeFrame;
      if(url==='/health')return {ok:true,json:async()=>({token:'fixture'})};
      return {ok:true,json:async()=>value()};}};
  vm.createContext(context);vm.runInContext(source('dashboard/charts/stock-chart.js'),context);
  await context.window.WorkbenchStockChart.mount({market:'US',symbol:'SYNTH',currency:'USD',isCurrent:()=>true});
  assert.deepEqual(calls.map(c=>c.url),['/api/chart/US%3ASYNTH?timeframe=1d']);
  await frames[1].onclick();assert.equal(mounts.at(-1).stock.timeframe,'1w');
  await frames[2].onclick();assert.equal(mounts.at(-1).stock.timeframe,'1mo');
  assert(calls.every(c=>(c.options.method||'GET')==='GET'));
  await node('#history-retry').onclick();
  assert.equal(calls.filter(c=>c.options.method==='POST').length,1,'only explicit retry requests chart-history');
  assert.equal(calls.find(c=>c.options.method==='POST').url,'/api/chart-history/US%3ASYNTH');
}
(async()=>{await viewportContract();await frameContract();await stockEntryContract();
  console.log('M10 L1 integration: viewport/zoom debounce 180ms, latest-only, 3 frames, cleanup, immutable drawings and saved-only chart entry PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
