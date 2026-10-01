/* Official financial snapshot UI. No browser-side financial calculations. */
window.WorkbenchFinancials = (() => {
  let active=null;
  const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  // Older running servers may not serve the new help asset until restarted.
  window.WorkbenchHelp ||= {text:esc,term:esc,financial:cell=>cell.formula||cell.reason||cell.label};
  const n=(x,d=2)=>Number(x).toLocaleString('ko-KR',{maximumFractionDigits:d});
  function value(cell,currency,scaled=false){
    if(cell?.value==null)return '—';
    if(cell.kind==='percent')return n(cell.value*100)+'%';
    if(cell.kind==='multiple')return n(cell.value)+'배';
    if(cell.unit?.startsWith(currency)){
      const scale=scaled?(currency==='KRW'?1e8:1e9):1;
      return WorkbenchFormat.money(cell.value/scale,currency)+(scaled?(currency==='KRW'?' 억원':' B USD'):'');
    }
    return n(cell.value,cell.unit==='shares'?0:2);
  }
  function shell(){return '<section class="panel financial-panel" id="financials" aria-label="재무정보"><div class="panel-head"><div><h2>재무정보</h2><p class="chart-caption">버튼을 누를 때 공식 자료 수집·계산 · 이후 저장 결과 재사용</p></div><div class="financial-actions"><button id="financial-load">재무요약 만들기</button><button id="financial-refresh" hidden>재무요약 다시 만들기</button></div></div><div class="panel-body"><p id="financial-status" role="status">저장된 재무자료 확인 중…</p><div id="financial-content"></div></div></section>';}
  function clearCharts(ctx){ctx.charts.splice(0).forEach(c=>WorkbenchCharts.remove(c));}
  function close(){if(active){active.closed=true;clearTimeout(active.timer);clearCharts(active);active=null;}}
  async function mount({market,symbol,isCurrent}){
    close();const el=document.querySelector('#financials');if(!el)return;
    const ctx={el,key:market+':'+symbol,charts:[],frame:'annual',closed:false,timer:null,isCurrent};active=ctx;
    const live=()=>!ctx.closed&&isCurrent()&&el.isConnected;
    const endpoint='/api/financials/'+encodeURIComponent(ctx.key);
    async function load(){
      try{const response=await fetch(endpoint);if(!response.ok)throw Error('저장 재무자료를 읽지 못했습니다.');const payload=await response.json();if(!live())return;render(ctx,payload);if(payload.state==='running')ctx.timer=setTimeout(load,1500);}
      catch(error){if(live())el.querySelector('#financial-status').textContent=error.message;}
    }
    async function request(refresh){
      const buttons=el.querySelectorAll('.financial-actions button');buttons.forEach(b=>b.disabled=true);
      el.querySelector('#financial-status').textContent='공식 자료 요청 중… 종목을 이동해도 수집은 계속됩니다.';
      try{
        const health=await (await fetch('/health')).json();
        const response=await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Token':health.token},body:JSON.stringify({refresh})});
        if(!response.ok)throw Error('재무자료 요청 실패 ('+response.status+')');
        const payload=await response.json();if(!live())return;render(ctx,payload);if(payload.state==='running')ctx.timer=setTimeout(load,1500);
      }catch(error){if(live())el.querySelector('#financial-status').textContent=error.message;}
      finally{if(live())buttons.forEach(b=>b.disabled=ctx.state==='running');}
    }
    el.querySelector('#financial-load').onclick=()=>request(false);el.querySelector('#financial-refresh').onclick=()=>request(true);
    await load();
  }
  function render(ctx,payload){
    const {el}=ctx, r=payload.result, running=payload.state==='running';ctx.state=payload.state;
    el.querySelector('#financial-load').hidden=!!r;el.querySelector('#financial-refresh').hidden=!r;
    el.querySelectorAll('.financial-actions button').forEach(b=>b.disabled=running);
    const status=el.querySelector('#financial-status');
    const errors={SEC_USER_AGENT_REQUIRED:'SEC_USER_AGENT 설정 필요: secrets/financial.json에 연락 가능한 이메일을 포함한 User-Agent를 설정하세요.',DART_API_KEY_REQUIRED:'DART_API_KEY 설정 필요: opendart.fss.or.kr에서 인증키를 발급받아 환경변수 또는 secrets/financial.json에 저장하세요.'};
    const error=payload.attempt?.error;
    status.textContent=running?'공식 자료 수집 중 · '+(payload.attempt.stage||'요청 준비'):
      error?'최근 갱신 '+(payload.state==='interrupted'?'중단':'실패')+' · '+(errors[error]||error)+(r?' · 아래는 이전 성공 자료입니다.':''):
      r?'저장 결과 · 수집 '+new Date(r.collected_at).toLocaleString('ko-KR')+' · 재무기간 '+r.financial_as_of+' · 시세 '+(r.price_as_of||'결측'):
      '이 종목의 저장된 재무자료가 없습니다. 만들기 버튼을 누르면 공식 공시를 요청합니다.';
    status.className=error?'financial-status error':'financial-status';
    if(!r){el.querySelector('#financial-content').innerHTML='<div class="empty">SEC / DART 공식 실적에서 요약표와 EPS·매출·주식수 그래프를 준비합니다.<br>전 종목을 자동으로 수집하지 않습니다.</div>';return;}
    if(ctx.snapshot===r.snapshot_id)return;
    ctx.snapshot=r.snapshot_id;ctx.result=r;clearCharts(ctx);
    const columnSize=Math.ceil(r.summary.length/6), columns=Array.from({length:6},(_,i)=>r.summary.slice(i*columnSize,(i+1)*columnSize)).filter(x=>x.length);
    el.querySelector('#financial-content').innerHTML=`<div class="financial-meta"><span class="tag">${esc(r.standard)}</span><span>${esc(r.currency)} · 기말 주식수 / 기간 실적 구분</span><a href="${'/api/financials/'+encodeURIComponent(ctx.key)}" target="_blank" rel="noreferrer">동일 계산 결과 JSON ↗</a></div><div class="financial-summary">${columns.map(column=>`<div class="financial-group"><h3>${esc([...new Set(column.map(x=>x.group))].join(" · "))}</h3><table><tbody>${column.map(x=>`<tr><th scope="row">${WorkbenchHelp.term(x.label,WorkbenchHelp.financial(x,r))}</th><td><button class="financial-value ${x.value==null?'missing':''}" data-metric="${esc(x.id)}" data-help="${esc(WorkbenchHelp.financial(x,r))}">${esc(value(x,r.currency,x.kind==='money'&&!x.unit.includes('/share')&&!['price','previous_close','high52','low52','atr'].includes(x.id)))}</button></td></tr>`).join('')}</tbody></table></div>`).join('')}</div><details class="financial-evidence" id="financial-evidence"><summary>값을 누르면 계산 근거·기준일·결측 사유를 확인할 수 있습니다</summary><div id="financial-evidence-body"></div></details><div class="financial-chart-head"><div><h3>실적과 주식수 추이</h3><p class="chart-caption">실제 회계기간 종료일 · 추정치 제외 · ${esc(r.provider)}</p></div><div class="chart-toolbar" aria-label="재무 그래프 기간"><button data-financial-frame="annual" aria-pressed="true">연간</button><button data-financial-frame="quarterly" aria-pressed="false">분기</button></div></div><div class="financial-charts">${['eps','sales','shares'].map(k=>`<article class="financial-chart-card"><h3>${k==='eps'?(r.provider==='SEC'?'GAAP':'K-IFRS')+' 희석 EPS':k==='sales'?'매출 · Sales':'유통주식수 · Shares Outstanding'}</h3><div id="financial-latest-${k}" class="financial-latest"></div><div id="financial-chart-${k}" class="financial-chart" role="img" aria-label="${k} 재무 그래프"></div><p id="financial-legend-${k}" class="chart-caption"></p></article>`).join('')}</div><details class="financial-periods"><summary>기간별 값과 공시 출처</summary><div id="financial-period-table" class="scroll"></div></details>${r.guidance?.length?'<div class="financial-guidance"><h3>회사 공식 가이던스 · 실적과 별도</h3>'+r.guidance.map(g=>`<p>${esc(g.period)} · ${esc(g.standard)} · 발표 ${esc(g.published)}<br>${esc(g.text)} <a href="${esc(g.url)}" target="_blank" rel="noreferrer">공식 발표 ↗</a></p>`).join('')+'</div>':''}<details class="financial-limits"><summary>자료 범위와 한계</summary><ul>${r.limitations.map(x=>`<li>${esc(x)}</li>`).join('')}<li>가치배수는 저장 시세와 최근 공시 실적의 조합이며 실시간 지표가 아닙니다.</li><li>기관·내부자·공매도 자료 미연결 항목은 사유와 함께 결측으로 표시합니다.</li></ul></details>`;
    el.querySelectorAll('[data-metric]').forEach(b=>b.onclick=()=>showEvidence(ctx,r.summary.find(x=>x.id===b.dataset.metric)));
    el.querySelectorAll('[data-financial-frame]').forEach(b=>b.onclick=()=>{ctx.frame=b.dataset.financialFrame;renderCharts(ctx);});renderCharts(ctx);
  }
  function sources(ctx,cell){
    const links=[];
    if(cell?.receipt)links.push({url:'https://dart.fss.or.kr/dsaf001/main.do?rcpNo='+cell.receipt,id:'DART 공시 '+cell.receipt});
    if(cell?.accession){const cik=String(Number(ctx.result.identity.cik));links.push({url:'https://www.sec.gov/Archives/edgar/data/'+cik+'/'+cell.accession.replaceAll('-','')+'/'+cell.accession+'-index.html',id:'SEC '+cell.accession});}
    for(const id of cell?.sources||[]){const source=ctx.result.sources.find(x=>x.id===id);if(source)links.push(source);}
    return links.map(s=>`<a href="${esc(s.url)}" target="_blank" rel="noreferrer">${esc(s.id)} ↗</a>`).join(' · ')||'저장 가격 자료 / JSON 원자료 참조';
  }
  function showEvidence(ctx,cell){
    const d=ctx.el.querySelector('#financial-evidence');d.open=true;
    ctx.el.querySelector('#financial-evidence-body').innerHTML=`<strong>${esc(cell.label||cell.tag||'공시 값')} · ${esc(value(cell,ctx.result.currency))} ${esc(cell.unit)}</strong><p>${esc(cell.reason||cell.formula)}</p><p>기간 ${esc(cell.start||'시점')} ~ ${esc(cell.end||'미확인')} · 공시 ${esc(cell.filed||'해당 없음/미확인')}</p><p>${sources(ctx,cell)}</p>${cell.inputs?'<ul>'+cell.inputs.map(x=>`<li>${esc(x.formula)} · ${esc(value(x,ctx.result.currency))} ${esc(x.unit)} · ${esc(x.end)}</li>`).join('')+'</ul>':''}`;
  }
  function renderCharts(ctx){
    clearCharts(ctx);const r=ctx.result, rows=r[ctx.frame];
    ctx.el.querySelectorAll('[data-financial-frame]').forEach(b=>{b.classList.toggle('selected',b.dataset.financialFrame===ctx.frame);b.setAttribute('aria-pressed',String(b.dataset.financialFrame===ctx.frame));});
    for(const key of ['eps','sales','shares']){
      const scale=key==='sales'?(r.currency==='KRW'?1e8:1e9):key==='shares'?1e6:1;
      const unit=key==='sales'?(r.currency==='KRW'?'억원':'B USD'):key==='shares'?'백만 주':r.currency+'/주';
      const data=rows.map(row=>({time:row.date,...(row.values[key]?.value==null?{}:{value:row.values[key].value/scale,color:key==='shares'?'#6c9dde':row.values[key].value<0?'#ed6572':'#31b884'})}));
      const latest=rows.at(-1)?.values[key];
      const formatter=x=>key==='shares'?n(x):WorkbenchFormat.money(x,r.currency);
      ctx.el.querySelector('#financial-latest-'+key).textContent=(latest?.value==null?'—':formatter(latest.value/scale))+' '+unit;
      const legend=ctx.el.querySelector('#financial-legend-'+key);
      const missing=rows.filter(x=>x.values[key]?.value==null).length;
      legend.textContent=rows.length+'개 기간 · '+unit+(missing?' · 결측 '+missing+'개':'')+' · '+(rows.at(-1)?.date||'');
      const chart=WorkbenchCharts.create(ctx.el.querySelector('#financial-chart-'+key),[{type:'volume',label:unit,data,options:{title:'',lastValueVisible:false,priceLineVisible:false,priceFormat:{type:'custom',minMove:key==='shares'?.01:r.currency==='KRW'?1:.01,formatter}}}],{height:250,onReady(chart,refs){
        chart.timeScale().applyOptions({tickMarkFormatter:time=>ctx.frame==='annual'?String(time).slice(0,4):String(time).slice(2,7)});
        chart.subscribeCrosshairMove(p=>{if(!p.time)return;const row=rows.find(x=>x.date===String(p.time));const cell=row?.values[key];legend.textContent=(row?.label||String(p.time))+' · '+(cell?.value==null?'결측 · '+(cell?.reason||'공시 미확보'):formatter(cell.value/scale)+' '+unit);});
        chart.subscribeClick(p=>{const row=rows.find(x=>x.date===String(p.time));if(row?.values[key])showEvidence(ctx,{...row.values[key],label:row.label+' · '+key});});
      }});if(chart)ctx.charts.push(chart);
    }
    ctx.el.querySelector('#financial-period-table').innerHTML=`<table><thead><tr><th>회계기간 종료</th><th>희석 EPS (${esc(r.currency)}/주)</th><th>매출 (${r.currency==='KRW'?'억원':'B USD'})</th><th>보통주 유통주식수</th><th>공시 근거</th></tr></thead><tbody>${[...rows].reverse().map(row=>`<tr><th>${esc(row.label)}</th><td title="${esc(row.values.eps?.reason||'')}">${esc(value(row.values.eps,r.currency))}</td><td>${esc(value(row.values.sales,r.currency,true))}</td><td>${esc(value(row.values.shares,r.currency))}</td><td>${sources(ctx,row.values.sales)}</td></tr>`).join('')}</tbody></table>`;
  }
  return {shell,mount,close};
})();