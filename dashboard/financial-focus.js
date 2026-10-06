/* Official financial-focus contract: all financial calculations come from Python. */
window.WorkbenchFinancials=(()=>{
  let active;
  const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const n=x=>Number(x).toLocaleString('ko-KR',{maximumFractionDigits:2});
  const colors={improved:'#31c58b',worsened:'#ed6572',neutral:'#a8b3c3'};
  const sourceName=c=>({company_reported:'회사 공시값',opendart_indicator:'OpenDART 제공 지표',calculated:'계산값'}[c?.source_type]||'원자료');
  function value(c,currency){
    if(c?.display_text)return c.display_text;
    if(c?.value==null)return '자료 없음';
    if(c.unit==='percentage_point')return n(c.value*100)+'%p';
    if(c.kind==='percent')return n(c.value*100)+'%';
    if(c.unit==='multiple'||c.kind==='multiple')return n(c.value)+'배';
    if(c.unit?.startsWith(currency))return WorkbenchFormat.money(c.value,currency);
    return n(c.value);
  }
  function shell(){return '<section class="panel financial-panel" id="financials" aria-label="재무정보"><div class="panel-head"><div><h2>재무정보</h2><p class="chart-caption">공식 공시 기반 실적·성장·건전성</p></div><div class="financial-actions"><button id="financial-load">재무요약 만들기</button><button id="financial-refresh" hidden>재무요약 다시 만들기</button></div></div><div class="panel-body"><p id="financial-status" role="status"></p><div id="financial-content"></div></div></section>';}
  function clear(ctx){ctx.charts.splice(0).forEach(c=>WorkbenchCharts.remove(c));}
  function close(){if(active){active.closed=true;clearTimeout(active.timer);clear(active);active=null;}}
  async function mount({market,symbol,isCurrent}){
    close();const el=document.querySelector('#financials');if(!el)return;
    const ctx={el,key:market+':'+symbol,frame:'annual',comparison:'growth',charts:[],cells:[],isCurrent};active=ctx;
    const live=()=>!ctx.closed&&isCurrent()&&el.isConnected,endpoint='/api/financials/'+encodeURIComponent(ctx.key);
    async function load(refresh){
      try{
        let options;
        if(typeof refresh==='boolean'){
          const health=await(await fetch('/health')).json();
          options={method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Token':health.token},body:JSON.stringify({refresh})};
        }
        const response=await fetch(endpoint,options);if(!response.ok)throw Error('재무자료 요청 실패 ('+response.status+')');
        const packet=await response.json();if(!live())return;
        render(ctx,packet);if(packet.state==='running')ctx.timer=setTimeout(()=>load(),1500);
      }catch(e){if(live()){el.querySelector('#financial-status').textContent=e.message;el.querySelectorAll('.financial-actions button').forEach(b=>b.disabled=false);}}
    }
    el.querySelector('#financial-load').onclick=()=>load(false);el.querySelector('#financial-refresh').onclick=()=>load(true);
    await load();
  }
  function links(ctx,c){
    const all=[];
    if(c?.receipt)all.push({id:'DART 공시 '+c.receipt,url:'https://dart.fss.or.kr/dsaf001/main.do?rcpNo='+encodeURIComponent(c.receipt)});
    if(c?.accession&&ctx.result.identity?.cik)all.push({id:'SEC '+c.accession,url:'https://www.sec.gov/Archives/edgar/data/'+Number(ctx.result.identity.cik)+'/'+c.accession.replaceAll('-','')+'/'+c.accession+'-index.html'});
    for(const id of c?.sources||[]){const s=ctx.result.sources?.find(x=>x.id===id);if(s)all.push(s);}
    return all.filter(s=>/^https?:\/\//.test(s.url||'')).map(s=>`<a href="${esc(s.url)}" target="_blank" rel="noreferrer">${esc(s.id)} ↗</a>`).join(' · ')||'원자료 미확보';
  }
  function evidence(ctx,cell){
    if(!cell)return;
    function detail(c,depth=0){
      const text=[c.label||c.tag,sourceName(c),c.reason,c.formula,c.definition,c.explanation,c.account,c.revenue_note,c.comparison_note,c.period_note,c.eps_basis&&'EPS 종류: '+c.eps_basis,c.revision_basis&&'정정 비교 기준: '+c.revision_basis].filter(Boolean).map(esc).join(' · ');
      return `<div class="financial-input"><p><strong>${esc(value(c,ctx.result.currency))} ${esc(c.unit)}</strong> · ${text}</p>${c.change?'<p>비교기간 대비 변화 '+esc(value(c.change,ctx.result.currency))+' · '+esc(c.change.reason||c.change.compared_to||'')+'</p>':''}<p>기간 ${esc(c.start||'기말')} ~ ${esc(c.end||'미확인')} · 공시 ${esc(c.filed||'미확인')} · ${links(ctx,c)}</p>${c.revision_history?.length?'<p>과거 공시 변경 '+c.revision_history.length+'건 · '+esc(JSON.stringify(c.revision_history))+'</p>':''}${depth<3?(c.inputs||[]).map(x=>detail(x,depth+1)).join(''):''}${depth===0&&c.change?.previous?'<h4>변화 비교 원값</h4>'+detail(c.change.previous,1):''}</div>`;
    }
    ctx.el.querySelector('#financial-evidence').open=true;ctx.el.querySelector('#financial-evidence-body').innerHTML=detail(cell);
  }
  function button(ctx,c,label){
    const id=ctx.cells.push(c?{...c,label:label||c.label}:null)-1;
    return `<button class="financial-value ${esc(c?.trend||'neutral')}" data-focus-cell="${id}" title="${esc([sourceName(c),c?.formula,c?.reason].filter(Boolean).join(' · '))}">${esc(value(c,ctx.result.currency))}</button>`;
  }
  function render(ctx,packet){
    const r=packet.result,el=ctx.el;
    el.querySelector('#financial-load').hidden=!!r;el.querySelector('#financial-refresh').hidden=!r;
    el.querySelectorAll('.financial-actions button').forEach(b=>b.disabled=packet.state==='running');
    el.querySelector('#financial-status').textContent=packet.state==='running'?'공식 자료 수집 중 · '+(packet.attempt?.stage||''):
      [packet.attempt?.error&&'최근 갱신 실패: '+packet.attempt.error,r?'자료 수집 '+new Date(r.collected_at).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'})+' · 재무기간 '+r.financial_as_of:'저장된 재무자료가 없습니다.',r?.recalculated_at&&'재계산 '+new Date(r.recalculated_at).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'})].filter(Boolean).join(' · ');
    if(!r){el.querySelector('#financial-content').innerHTML='<p class="empty">만들기 버튼으로 선택 종목의 공식 자료를 수집합니다.</p>';return;}
    if(r.calculation_version!=='financial-focus-1'){el.querySelector('#financial-content').innerHTML='<p class="empty">이전 형식의 저장 결과입니다. 재무요약 다시 만들기 또는 저장 자료 재계산이 필요합니다. 과거 결과는 보존됩니다.</p>';return;}
    if(ctx.snapshot===r.snapshot_id)return;ctx.snapshot=r.snapshot_id;ctx.result=r;paint(ctx);
  }
  function paint(ctx){
    clear(ctx);ctx.cells=[];const r=ctx.result,rows=r[ctx.frame]||[],last=rows.at(-1),mode=ctx.frame==='annual'?'growth':ctx.comparison;
    const performance=mode==='qoq'?'performance_qoq':'performance';
    const metrics=r.performance_metrics||[],coverage=r.coverage_metadata?.[ctx.frame];
    ctx.el.querySelector('#financial-content').innerHTML=`
      <div class="financial-meta"><span>${esc(r.standard)} · ${esc(r.currency)} · ${esc(r.calculation_version)}</span><a target="_blank" rel="noreferrer" href="/api/financials/${encodeURIComponent(ctx.key)}">화면·MCP 공통 결과 JSON ↗</a></div>
      <div class="financial-chart-head"><div><h3>실적과 성장률</h3><p class="chart-caption">연간 5개 · 분기 12개 목표 / 확보 ${rows.length}개 · ${esc(coverage?.first||'없음')} ~ ${esc(coverage?.last||'없음')}</p></div><div class="chart-toolbar"><button data-focus-frame="annual" aria-pressed="${ctx.frame==='annual'}">연간</button><button data-focus-frame="quarterly" aria-pressed="${ctx.frame==='quarterly'}">분기</button>${ctx.frame==='quarterly'?`<button data-focus-comparison="growth" aria-pressed="${mode==='growth'}">YoY 전년 대비</button><button data-focus-comparison="qoq" aria-pressed="${mode==='qoq'}">QoQ 전분기 대비</button>`:''}</div></div>
      <p class="chart-caption">막대: 실적 · 점선: ${mode==='growth'?'YoY 전년':'QoQ 전분기'} 대비 성장률 · 왼쪽: 금액/주당금액 · 오른쪽: 성장률 % · 색상은 비교기간 대비 개선/악화이며 매수·매도 판단이 아닙니다.</p>
      ${(last?.warnings||[]).map(w=>`<p class="financial-equity-warning" role="alert">${esc(w.message)} ${button(ctx,w.evidence)}</p>`).join('')}
      <div class="financial-focus-health">${Object.entries(last?.metrics||{}).map(([id,c])=>`<article class="${id==='operating_margin'?'financial-margin':''}"><h4>${esc(c.label)}</h4>${button(ctx,c)}<small>전년 대비 ${esc(value(c.change,r.currency))} · ${esc(sourceName(c))}</small><p>${esc(c.explanation)} · ${esc(c.definition)}</p></article>`).join('')}</div>
      <div class="financial-focus-charts">${metrics.map(m=>`<article class="financial-chart-card"><h3>${esc(m.label)}</h3><div id="focus-chart-${m.id}" class="financial-chart" role="img" aria-label="${esc(m.label)} 실적 막대와 성장률 점선"></div><p id="focus-legend-${m.id}" class="chart-caption"></p></article>`).join('')}</div>
      <details class="financial-periods" open><summary>기간별 수치·성장률·건전성</summary><div class="scroll"><table class="financial-performance-table"><thead><tr><th>항목</th>${rows.map(row=>`<th>${esc(row.label)}<small>${esc(row.date)}</small></th>`).join('')}</tr></thead><tbody>${metrics.map(m=>`<tr><th>${esc(m.label)}<small>${esc(r.currency)}${m.id.includes('eps')?'/주':''}</small></th>${rows.map(row=>`<td>${button(ctx,row[performance]?.[m.id])}<div>${mode==='growth'?'YoY':'QoQ'} ${button(ctx,row[mode]?.[m.id])}</div></td>`).join('')}</tr>`).join('')}${['operating_margin','debt_ratio','icr'].map(id=>`<tr><th>${esc(last?.metrics?.[id]?.label||id)}</th>${rows.map(row=>`<td>${button(ctx,row.metrics?.[id])}<small>전년 대비 ${esc(value(row.metrics?.[id]?.change,r.currency))}</small></td>`).join('')}</tr>`).join('')}<tr><th>자본총계 0 이하</th>${rows.map(row=>`<td>${row.warnings?.length?'경고':row.values?.total_equity?.value==null?'자료 없음':'해당 없음'}</td>`).join('')}</tr></tbody></table></div></details>
      <details class="financial-evidence" id="financial-evidence"><summary>값을 눌러 공시 원값·계산 근거·결측 사유 보기</summary><div id="financial-evidence-body"></div></details>
      <details><summary>자료 범위와 한계</summary><ul>${(r.limitations||[]).map(x=>`<li>${esc(x)}</li>`).join('')}</ul></details>`;
    ctx.el.querySelectorAll('[data-focus-frame]').forEach(b=>b.onclick=()=>{ctx.frame=b.dataset.focusFrame;paint(ctx);});
    ctx.el.querySelectorAll('[data-focus-comparison]').forEach(b=>b.onclick=()=>{ctx.comparison=b.dataset.focusComparison;paint(ctx);});
    ctx.el.querySelectorAll('[data-focus-cell]').forEach(b=>b.onclick=()=>evidence(ctx,ctx.cells[Number(b.dataset.focusCell)]));
    for(const metric of metrics){
      const key=metric.id,legend=ctx.el.querySelector('#focus-legend-'+key),unit=r.currency+(key.includes('eps')?'/주':'');
      const label=row=>row?row.label+' · '+value(row[performance]?.[key],r.currency)+' '+unit+' · '+(mode==='growth'?'YoY ':'QoQ ')+value(row[mode]?.[key],r.currency):'자료 없음';
      legend.textContent=label(last);
      const series=[{type:'volume',label:metric.label,data:rows.map(row=>({time:row.date,...(row[performance]?.[key]?.value==null?{}:{value:row[performance][key].value,color:colors[row[performance][key].trend]||colors.neutral})})),options:{priceScaleId:'left',lastValueVisible:false,priceFormat:{type:'custom',minMove:r.currency==='KRW'?1:.01,formatter:x=>WorkbenchFormat.money(x,r.currency)}}},
        {type:'line',label:mode==='growth'?'YoY %':'QoQ %',data:rows.map(row=>({time:row.date,...(row[mode]?.[key]?.value==null?{}:{value:row[mode][key].value*100,color:colors[row[mode][key].trend]||colors.neutral})})),options:{priceScaleId:'right',lineStyle:2,lineWidth:2,pointMarkersVisible:true,lastValueVisible:false,priceFormat:{type:'custom',minMove:.01,formatter:x=>n(x)+'%'}}}];
      const chart=WorkbenchCharts.create(ctx.el.querySelector('#focus-chart-'+key),series,{height:240,onReady(chart){
        chart.applyOptions({leftPriceScale:{visible:true,borderVisible:false},rightPriceScale:{visible:true,borderVisible:false}});
        chart.timeScale().applyOptions({tickMarkFormatter:t=>ctx.frame==='annual'?String(t).slice(0,4):String(t).slice(2,7)});
        chart.subscribeCrosshairMove(p=>{legend.textContent=label(rows.find(row=>row.date===String(p.time))||last);});
        chart.subscribeClick(p=>{const row=rows.find(row=>row.date===String(p.time));if(row)evidence(ctx,row[performance]?.[key]);});
      }});if(chart)ctx.charts.push(chart);
    }
  }
  return {shell,mount,close};
})();
