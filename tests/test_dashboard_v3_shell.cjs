const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const read = name => fs.readFileSync(path.join(__dirname, '../dashboard/', name), 'utf8');
const source = read('shell/app.js'), index = read('shell/index.html');
const flush = () => new Promise(resolve => setImmediate(resolve));

function element() {
 const classes = new Set();
 return {dataset:{}, children:[], innerHTML:'', textContent:'', hidden:false, isConnected:true,
  classList:{add:x=>classes.add(x),remove:x=>classes.delete(x),toggle(x,value){const on=value??!classes.has(x);on?classes.add(x):classes.delete(x);return on;},contains:x=>classes.has(x)},
  append(child){this.children.push(child);},setAttribute(){},querySelector(){return null;},focus(){this.focused=true;},scrollIntoView(){}};
}
function setup(hash = '', update = {enabled:false,view_ready:true,state:'succeeded'}) {
 const elements=new Map(), get=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);};
 const location={hash}, calls=[], mounts=[], replacements=[], timers=[];
 const nav=['search','risk'].map(page=>Object.assign(element(),{dataset:{nav:page}}));
 const market={state:'succeeded',quality:{state:'complete',requested:1,received:1,failed:0},session_dates:{US:'2026-11-13'}};
 const document={body:element(),activeElement:null,getElementById:id=>id==='stock-search'?null:get(id),querySelector:selector=>get(selector.slice(1)),querySelectorAll:selector=>selector==='[data-nav]'?nav:[],createElement:()=>element()};
 let allowClose=true;
 const sandbox={document,location,URLSearchParams,console,AbortController,
  history:{replaceState(_s,_t,hash){location.hash=hash;replacements.push(hash);}},
  window:{addEventListener(){},scrollTo(){},WorkbenchScenarios:true},
  setTimeout(fn,delay){timers.push({fn,delay});return timers.length;},clearTimeout(){},
  WorkbenchFormat:{money:x=>x==null?'미확인':String(x)},
  WorkbenchWorkspace:{closeActive:async()=>allowClose,current:()=>null},
  WorkbenchFinancials:{close(){},shell:()=>'<section id="financials">재무정보</section>',mount:args=>mounts.push(['financials',args])},
  WorkbenchCharts:{clear(){}},
  WorkbenchTrailing:{mount:async host=>{host.innerHTML='가격 위험 경고';mounts.push(['risk']);}},
  WorkbenchAnalysis:{initial:async args=>{mounts.push(['initial',args,location.hash]);return {case:{stock_key:args.market+':'+args.symbol,context:{timeframe:'1d'}}};},mount:async args=>mounts.push(['analysis',args])},
  WorkbenchStockChart:{mount:async args=>{mounts.push(['chart',args]);return {fixture:'controller'};}},
  WorkbenchScenarios:{mountStock:async args=>mounts.push(['scenarios',args])},
  fetch:async url=>{calls.push(url);return {ok:true,json:async()=>url==='/api/home'?{market}:url==='/api/daily-update'?update:url.startsWith('/api/stock/')?{name:'합성 <종목>',currency:'USD',bars:[{date:'2026-11-13',close:12}]}:{}};}
 };
 const context=vm.createContext(sandbox);
 vm.runInContext(source+'\nglobalThis.testShell={route,selectedRoute,stockDetail,pollDaily};',context);
 return {context,calls,mounts,replacements,location,get,nav,timers,setClose:value=>{allowClose=value;},route:()=>context.testShell.route()};
}
async function startup(hash) {
 let replaced;
 const location={hash,replace:href=>{replaced=href;}};
 const context=vm.createContext({location,URL,document:{getElementById:()=>element(),querySelectorAll:()=>[]},addEventListener(){},setTimeout(){},fetch:async()=>({ok:true,json:async()=>({state:'ready',url:'http://127.0.0.1:8765/'})})});
 vm.runInContext(read('shell/startup.js'),context);await flush();return replaced;
}
(async()=>{
 assert.doesNotMatch(index, /data-nav="(?:home|screen|sectors?|focus|cycles?|accounts?)"|risk-workbench|market-exclusions|읽기 전용|codex:/i);
 assert.match(index,/href="#search" data-nav="search"/);
 assert.match(index,/src="\/analysis.js"/);
 assert.match(index,/src="\/scenario-monitor.js"/);
 assert.match(index,/src="\/trailing.js"/);
 assert.ok(index.indexOf('/assets/l1-trendlines.js')<index.indexOf('/chart-workspace.js'));
 assert.ok(index.indexOf('/assets/l1-trendlines.js')<index.indexOf('/stock-chart.js'));
 assert.doesNotMatch(source,/DATA\.accounts|WorkbenchRisk|WorkbenchExclusions|accountCards|accountWorkspace|codex:|Codex|nh_fallback|account_attempted|account_state/i);
 const css=read('shell/style.css');
 assert.doesNotMatch(css,/\.(?:account-|sector-|focus-|screen-|preview-|sg-)/);
 assert.equal((css.match(/{/g)||[]).length,(css.match(/}/g)||[]).length,'style blocks remain balanced');
 for(const selector of ['.stock-plot-wrap','.chart','.stock-price-legend','.trailing-price-legend','.holding-price-legend'])assert(css.includes(selector),'retained price warning style: '+selector);
 assert.match(index,/href="\/scenario-monitor.css"/);
 for(const hash of ['', '#home','#screen','#sectors','#sector/US/Tech','#focus','#cycles','#cycle/old','#accounts','#account/saved','#%broken']) {
  const h=setup(hash);await flush();
  assert.equal(h.location.hash,'#search',hash+' routes to search');
  assert.match(h.get('main').innerHTML,/종목 검색하기/);
  assert.equal(h.get('breadcrumb').textContent,'종목 검색');
  assert.equal(h.nav[0].classList.contains('active'),true);
  assert.deepEqual(h.calls,['/api/home','/api/daily-update'],'retired routes never request data');
  h.get('search-focus').onclick();assert.equal(h.get('stock-search-input').focused,true);
 }
 const h=setup('#stock/US/SYNTH?analysis=an_fixture');await flush();
 assert.equal(h.location.hash,'#stock/US/SYNTH?analysis=an_fixture','analysis ID survives route parsing');
 assert.match(h.get('main').innerHTML,/href="#search"/);
 assert.match(h.get('main').innerHTML,/id="financials"/);
 assert.match(h.get('main').innerHTML,/합성 &lt;종목&gt;/);
 assert.deepEqual(h.mounts.map(x=>x[0]),['financials','initial','chart','analysis','scenarios']);
 assert.equal(h.mounts.find(x=>x[0]==='chart')[1].context.timeframe,'1d');
 assert.equal(h.mounts.find(x=>x[0]==='analysis')[1].controller.fixture,'controller');
 assert.equal(h.mounts.find(x=>x[0]==='scenarios')[1].controller.fixture,'controller');
 h.setClose(false);h.location.hash='#risk';await h.route();
 assert.equal(h.location.hash,'#stock/US/SYNTH?analysis=an_fixture','unsaved editor refusal preserves previous route and analysis ID');
 h.setClose(true);await h.route();
 h.location.hash='#risk';await h.route();assert.equal(h.get('main').innerHTML,'가격 위험 경고');
 const accountFailure=setup('#search',{enabled:true,observe_only:true,view_ready:true,state:'succeeded',account_state:'failed',account_error:'NH rejected',account_attempted:true});
 await flush();await flush();
 assert.doesNotMatch(accountFailure.get('daily-update').innerHTML,/계좌|NH|잔고/);
 assert.equal(accountFailure.get('daily-update').classList.contains('error'),false,'saved account failure is not a market refresh failure');
 for(const hash of ['', '#home','#accounts','#cycles','#sector/US/Tech'])assert.equal(new URL(await startup(hash)).hash,'#search');
 assert.equal(new URL(await startup('#stock/US/SYNTH?analysis=an_fixture')).hash,'#stock/US/SYNTH?analysis=an_fixture');
 assert.equal(new URL(await startup('#risk')).hash,'#risk');
 console.log('PASS: M10 search entry and retired routes, retained chart/financial/analysis/scenario pieces without account/candidate input, analysis links and unsaved edits, startup routing, market-only refresh UI, retired assets/styles.');
})().catch(error=>{console.error(error);process.exitCode=1;});
