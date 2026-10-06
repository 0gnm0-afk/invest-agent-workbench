const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
class Element{
 constructor(dataset={}){this.dataset=dataset;this.innerHTML='';this.textContent='';this.isConnected=true;this.children=new Map();}
 querySelector(s){if(!this.children.has(s))this.children.set(s,new Element());return this.children.get(s);}
 querySelectorAll(s){
  if(s==='.financial-actions button')return [this.querySelector('#financial-load'),this.querySelector('#financial-refresh')];
  const m=s.match(/^\[data-focus-(frame|comparison|cell)\]$/);if(!m)return [];
  const content=this.querySelector('#financial-content').innerHTML,key='focus'+m[1][0].toUpperCase()+m[1].slice(1);this.lists||=new Map();
  if(!this.lists.has(s+content))this.lists.set(s+content,[...content.matchAll(new RegExp(`data-focus-${m[1]}="([^"]+)"`,'g'))].map(x=>new Element({[key]:x[1]})));
  return this.lists.get(s+content);
 }
}
function harness(result){
 const root=new Element(),charts=[],ctx={console,setTimeout,clearTimeout,document:{querySelector:()=>root},fetch:async()=>({ok:true,json:async()=>({state:'complete',result})}),WorkbenchCharts:{remove(){},create(el,series,options){if(!series.some(s=>s.data.some(p=>p.value!=null)))return;const chart={series,panes(){return [{setHeight(){}},{setHeight(){}}];},applyOptions(o){this.options=o;},timeScale(){return {applyOptions(){}};},subscribeClick(fn){this.click=fn;},subscribeCrosshairMove(fn){this.move=fn;}};charts.push(chart);options.onReady(chart);return chart;}}};
 ctx.window=ctx;vm.createContext(ctx);for(const name of ['format.js','financial-focus.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'../dashboard',name),'utf8'),ctx);
 return {root,charts,mount:()=>ctx.WorkbenchFinancials.mount({market:result.provider==='DART'?'KR':'US',symbol:'TEST',isCurrent:()=>true})};
}
function sample(provider='SEC',eps=['basic_eps','eps']){
 const currency=provider==='SEC'?'USD':'KRW',cell=(value,kind='money',extra={})=>({value,kind,unit:kind==='percent'?'ratio':currency,source_type:'calculated',formula:'검증 산식',trend:'improved',...extra});
 const keys=['sales','operating','income',...(provider==='SEC'?eps:[])],performance_metrics=keys.map(id=>({id,label:({basic_eps:'기본 EPS',eps:'희석 EPS',sales:'매출액',operating:'영업이익',income:'순이익'})[id]}));
 const row=date=>({date,label:date,performance:Object.fromEntries(keys.map(k=>[k,cell(1234.5)])),performance_qoq:Object.fromEntries(keys.map(k=>[k,cell(1234.5)])),growth:Object.fromEntries(keys.map(k=>[k,cell(.25,'percent')])),qoq:Object.fromEntries(keys.map(k=>[k,cell(null,'percent',{display_text:'적자축소'})])),metrics:{operating_margin:cell(.2,'percent',{label:'영업이익률'}),debt_ratio:cell(.5,'percent',{label:'부채비율',definition:'총부채 ÷ 자본총계'}),icr:cell(null,'multiple',{label:'ICR',reason:'이자비용 미확보'})},values:{total_equity:cell(10)},warnings:[]});
 return {calculation_version:'financial-focus-1',snapshot_id:provider+eps,collected_at:'2026-10-06T00:00:00Z',financial_as_of:'2025-12-31',provider,currency,standard:'공시',performance_metrics,annual:[2021,2022,2023,2024,2025].map(y=>row(y+'-12-31')),quarterly:[2023,2024,2025].flatMap(y=>['03-31','06-30','09-30','12-31'].map(d=>row(y+'-'+d))),limitations:[],sources:[]};
}
(async()=>{
 for(const provider of ['DART','SEC']){
  const result=sample(provider),before=JSON.stringify(result),h=harness(result);await h.mount();let html=h.root.querySelector('#financial-content').innerHTML;
  assert.match(html,/연간 5개 · 분기 12개/);assert.match(html,/총부채 ÷ 자본총계/);assert.doesNotMatch(html,/Finviz|기관 지분|유동비율/);
  assert.equal(h.charts.length,provider==='SEC'?5:3);assert.equal(h.charts[0].series[0].type,'volume');assert.equal(h.charts[0].series[1].options.lineStyle,2);
  assert.equal(h.charts[0].series[0].pane,1);assert.equal(h.charts[0].series[1].pane,0);assert.equal(h.charts[0].series[0].options.priceScaleId,'left');assert.equal(h.charts[0].series[1].options.priceScaleId,'right');assert.equal(h.charts[0].series[0].data.length,5);
  assert.equal(h.charts[0].series[0].options.priceFormat.formatter(1234.5),provider==='SEC'?'1.23K':'1,235원');
  const fmt=h.charts[0].series[0].options.priceFormat.formatter;
  for(const [x,text] of (provider==='DART'?[[1e14,'100조'],[1e8,'1억'],[1e4,'1만'],[-123456789,'-1.23억'],[0,'0원']]:[[1e12,'1T'],[1e9,'1B'],[1e6,'1M'],[-2e9,'-2B'],[0,'0.00']]))assert.equal(fmt(x),text);
  assert.match(html,provider==='SEC'?/1,234.50 USD/:/1,234.5 KRW/);
  if(provider==='DART')assert.doesNotMatch(html,/기본 EPS|희석 EPS/);else{assert.match(html,/기본 EPS/);assert.match(html,/희석 EPS/);}
  h.root.querySelectorAll('[data-focus-frame]').find(x=>x.dataset.focusFrame==='quarterly').onclick();assert.equal(h.charts.at(-1).series[0].data.length,12);
  h.root.querySelectorAll('[data-focus-comparison]').find(x=>x.dataset.focusComparison==='qoq').onclick();assert.match(h.root.querySelector('#financial-content').innerHTML,/적자축소/);assert.equal(h.charts.at(-1).series[1].data[0].value,undefined);
  h.charts.at(-1).click({time:'2025-12-31'});assert.match(h.root.querySelector('#financial-evidence-body').innerHTML,/검증 산식/);
  assert.equal(JSON.stringify(result),before);
 }
 for(const provider of ['DART','SEC']){
  const r=sample(provider);r.annual.forEach(row=>row.values.dividend={value:provider==='DART'?1668:.034,unit:r.currency+'/share',dividend_basis:provider==='DART'?'annual_common_cash':'declared',formula:'공시 원값'});
  r.annual[0].values.dividend.value=null;r.annual[1].values.dividend.value=0;
  const before=JSON.stringify(r),h=harness(r);await h.mount();
  assert.match(h.root.querySelector('#financial-content').innerHTML,/주당배당금/);if(provider==='SEC')assert.match(h.root.querySelector('#financial-content').innerHTML,/>0.034<\/button>/);
  assert.match(h.root.querySelector('#financial-content').innerHTML,provider==='DART'?/사업연도 보통주 현금배당/:/선언 기준/);
  const d=h.charts.at(-1);assert.equal(d.series[0].data[0].value,undefined);assert.equal(d.series[0].data[1].value,0);
  d.move({time:'2025-12-31'});assert.match(h.root.querySelector('#focus-legend-dividend').textContent,provider==='DART'?/1,668 KRW\/share/:/0.034 USD\/share/);
  h.root.querySelectorAll('[data-focus-frame]').find(x=>x.dataset.focusFrame==='quarterly').onclick();assert.match(h.root.querySelector('#financial-content').innerHTML,/분기 공시값이 없어 연간/);
  assert.equal(JSON.stringify(r),before);
 }
 const one=harness(sample('SEC',['basic_eps']));await one.mount();assert.doesNotMatch(one.root.querySelector('#financial-content').innerHTML,/희석 EPS/);
 const old=harness({...sample(),calculation_version:'old'});await old.mount();assert.equal(old.charts.length,0);assert.match(old.root.querySelector('#financial-content').innerHTML,/이전 형식/);
 console.log('financial focus UI: KR/US, both/single EPS, 5/12, YoY/QoQ, axes, dashed growth, missing, currency, evidence, immutability PASS');
})();
