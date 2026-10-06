'use strict';
window.WorkbenchTrailing = (() => {
 const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const labels={price_sessions_incomplete:'완료 거래일 이력에 공백 또는 비거래일이 있습니다.',account_stale_or_incomplete:'잔고가 24시간을 넘었거나 보유 확인이 불완전합니다.',holding_unconfirmed:'보유 수량·평단 확인이 필요합니다.',market_identity_unconfirmed:'종목·통화 대응 확인이 필요합니다.',market_identity_or_basis_changed:'종목 식별자 또는 가격 기준이 바뀌었습니다.',completed_price_stale_or_missing:'최신 완료 거래일의 가격이 없습니다.',atr_history_missing:'ATR 계산 이력이 부족합니다.',processed_price_history_changed:'이미 처리한 가격 이력이 변경되었거나 연결 봉이 없습니다.',price_date_regressed:'가격 기준일이 과거입니다.'};
 function series(p){return [{label:'종가',currency:p.currency,color:'#6db8f6',data:(p.prices||[]).map(b=>({time:b.date,value:b.close}))},{label:'최종 트레일링',currency:p.currency,color:'#f5b85b',data:(p.history||[]).filter(b=>!p.prices?.length||b.date>=p.prices[0].date).map(b=>({time:b.date,value:b.line}))}];}
 async function mount(host){
  host.innerHTML='<h1>가격 위험 경고</h1><p>저장된 완료 일봉 판정을 불러오는 중…</p>';
  const response=await fetch('/api/trailing');if(!response.ok)throw Error('가격 경고를 불러오지 못했습니다.');const data=await response.json();
  if(!host.isConnected||!location.hash.startsWith('#risk'))return;
  const positions=data.positions.filter(p=>p.status!=='closed'||p.issue);
  host.innerHTML='<h1>가격 위험 경고</h1><p>완료 종가 기준 · 트레일링과 당일 MA5 접촉을 각각 판정합니다.</p>'+(!positions.length?'<div class="empty">아직 감시 가능한 저장 보유 자료가 없습니다.</div>':'')+positions.map((p,i)=>`<section class="panel"><div class="panel-body"><h2>${esc(p.name)} <small>${esc(p.symbol)} · ${esc(p.account)}</small></h2><p>${p.issue?'판정 보류 — '+esc(labels[p.issue]||p.issue):p.cost_pending?'새 평단 하한 반영 · 다음 완료봉에서 판정':p.active?(p.status==='warning'?'트레일링 접촉·이탈 상태':'감시 중'):'수익 상태 진입 대기'} · 가격 ${esc(p.last_date||p.prices?.at(-1)?.date||'미확인')} · 잔고 ${esc(p.observed_account_as_of||p.account_as_of||'미확인')}</p><div id="trailing-chart-${i}" class="chart" aria-label="종가와 최종 트레일링 가격선"></div><p id="trailing-legend-${i}" class="chart-caption">종가 · 최종 트레일링 (${esc(p.currency)})</p></div></section>`).join('')+`<section class="panel"><div class="panel-body"><h2>발생한 경고</h2>${data.events.length?data.events.slice(0,100).map(e=>`<p><strong>${esc(e.date)} · ${esc(e.symbol)} · ${e.kind==='ma5'?'MA5 접촉·이탈':'트레일링 접촉·이탈'}</strong><br>${esc(e.reason)} · 종가 ${WorkbenchFormat.money(e.close,e.currency)} / 기준 ${WorkbenchFormat.money(e.threshold,e.currency)} ${esc(e.currency)} · ${esc(e.account)}</p>`).join(''):'<p>기록된 경고가 없습니다. 자료 보류는 정상 판정을 뜻하지 않습니다.</p>'}</div></section>`;
  positions.forEach((p,i)=>WorkbenchCharts.create(host.querySelector('#trailing-chart-'+i),series(p),{lastBars:126,legend:host.querySelector('#trailing-legend-'+i)}));
 }
 return {mount,series};
})();
