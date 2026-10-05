'use strict';
// The reusable view accepts transport functions. All amounts are calculated in Python.
window.WorkbenchRisk = (() => {
 const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const money = (v,c='KRW') => WorkbenchFormat.money(v,c);
 const stamp = s => s ? new Date(s).toLocaleString('ko-KR',{timeZone:'Asia/Seoul',hour12:false})+' KST' : '미확인';
 const labels = {complete:'계산 가능',partial:'확인된 부분만',unavailable:'계산 불가',deferred:'입력 확인 필요'};
 const issueText = s => ({account_inventory_incomplete:'계좌 구성에 확인되지 않은 항목이 있습니다.',cost_assumption_required:'비용을 포함할지, 제외할지 정해야 합니다.',response_input_missing:'대응에 필요한 체결·경로 입력이 부족합니다.',price_path_required:'조건 발생 경로를 입력해야 합니다.',quantity_oversold:'가정 매도 수량이 배정 또는 잔여 물량을 초과합니다.',fill_price_required:'가정 체결가격이 필요합니다.',missing_or_invalid_number:'가격·수량·환율에 비어 있거나 사용할 수 없는 숫자가 있습니다.',response_position_unavailable:'해당 보유 종목의 평가 입력이 부족해 대응 결과를 계산할 수 없습니다.'}[s] || (s.includes('position_inputs_missing') ? '해당 보유 종목의 가격·환율 등 입력이 부족합니다.' : s.includes('cash_inputs_missing') ? '현금 평가에 필요한 환율 등 입력이 부족합니다.' : s));
 const issues = r => (r.missing||[]).length ? `<ul class="risk-issues">${r.missing.map(v=>`<li>${esc(issueText(v))}</li>`).join('')}</ul>` : '';
 function cards(result) {
  return `<div class="risk-cards">${[['현재 기준자산',result.baseline],['무대응 가정',result.no_response],['계획대응 가정',result.response]].map(([title,r])=>`<div class="risk-card"><small>${title}</small><strong>${money(r.total)} <span class="risk-muted">KRW</span></strong><em>${esc(labels[r.state]||r.state)}${r.total==null && r.state==='partial'?' · 확인된 부분합 '+money(r.known_subtotal)+'원':''}${r!==result.baseline&&r.change!=null?' · '+(r.state==='partial'?'같은 계산 범위 ':'현재 대비 ')+money(r.change)+'원':''}</em></div>`).join('')}</div>`;
 }
 async function mount(root, transport) {
  const data = await transport.load();
  const linkedId=data.projection.position_id, linked=data.snapshot.positions.find(p=>p.id===linkedId);
  if(!linked || !data.scenario.prices[linkedId]) throw Error('linked_scenario_required');
  const currency=linked.currency, symbol=linked.symbol;
  let scenario=structuredClone(data.scenario), result=data.result, view='home', generation=0, example='complete';
  const draw = () => {
   root.innerHTML=`<div class="risk-eyebrow">ACCOUNT & SCENARIOS</div><h1>내 계좌와 매매 시나리오</h1><p class="risk-muted">${esc(data.connection)}<br>계좌 기준 ${stamp(data.snapshot.as_of)} · 가정 평가시점 ${stamp(scenario.horizon)}</p><nav class="risk-tabs" aria-label="위험관리 화면"><button data-view="home" aria-selected="${view==='home'}">내 계좌</button><button data-view="record" aria-selected="${view==='record'}">종목 기록</button><button data-view="scenario" aria-selected="${view==='scenario'}">시나리오 비교</button></nav><div id="risk-content"></div>`;
   root.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>{view=b.dataset.view;draw();});
   const content=root.querySelector('#risk-content');
   if(view==='home') {
    content.innerHTML=cards(result)+`<div class="risk-notice">검증용 가상 계좌입니다. 표시된 계획·가격·환율은 실제 투자 결정이나 현재 계좌가 아닙니다.</div><section class="risk-panel"><h2>보유 종목과 계획 연결</h2><div class="risk-table-wrap"><table class="risk-table"><thead><tr><th>종목 · 계좌</th><th>보유 수량</th><th>기준가격</th><th>시나리오</th></tr></thead><tbody>${data.snapshot.positions.map(p=>`<tr><td><strong>${esc(p.symbol)}</strong><small>${esc(p.account)}</small></td><td>${esc(p.quantity)}주</td><td>${money(p.price,p.currency)} ${esc(p.currency)}</td><td>${p.id===linkedId?'<button class="risk-link" id="risk-open-record">공통 기록 v1 읽기 →</button>':'합성 계산 가정'}<small>${p.id===linkedId?'동일 종목 식별·계좌 연결 확인':'기록 연결 전 · 계산 예시만'}</small></td></tr>`).join('')}</tbody></table></div></section><div class="risk-grid"><section class="risk-panel"><h2>현금과 CMA</h2>${data.snapshot.cash.map(c=>`<p>${esc(c.kind==='cma'?'CMA':'현금')} <strong>${money(c.amount,c.currency)} ${esc(c.currency)}</strong></p>`).join('')}<p class="risk-muted">합성 예제에서 중복 제외를 확인했습니다. 매수가능현금과는 별도입니다.</p></section><section class="risk-panel"><h2>지금 확인할 내용</h2><p>원문에는 환율·비용이 미정입니다.</p><p class="risk-muted">이 비교에서는 환율 1,100원과 비용 제외를 명시한 별도 가정을 사용합니다. 원문 계획은 바뀌지 않습니다.</p><button class="risk-action" id="risk-open-scenario">계좌 영향 비교하기 →</button></section></div>`;
    content.querySelector('#risk-open-record').onclick=()=>{view='record';draw();};
    content.querySelector('#risk-open-scenario').onclick=()=>{view='scenario';draw();};
   } else if(view==='record') {
    const p=data.shared.plan_record.plan;
    content.innerHTML=`<section class="risk-panel"><span class="risk-tag">공통 계약 collaboration.v1 · 합성 채택 버전</span><h2>${esc(p.title)}</h2><p class="risk-muted">${esc(p.account_alias)} · 목적 ${esc(p.purpose)} · 배정 ${esc(p.allocation.quantity)}주</p><div class="risk-source">${p.conditions.map(c=>`<p><strong>${esc(c.action==='review'?'다음 검토':'대응 조건')}</strong><br>${esc(c.statement)}</p>`).join('')}</div><p class="risk-muted">조건 확인가격과 가정 체결가격은 구분합니다. 이 예제의 가정 체결가격은 89.00 USD입니다.</p><div class="risk-notice">원 기록의 환율·비용 미정과 정성 검토 조건은 유지합니다. ‘성장 논리 훼손’을 임의의 숫자로 바꾸지 않습니다.</div><p class="risk-code">기록 ${esc(data.shared.plan_record.id)}<br>계획 ${esc(data.shared.plan_id)} · v${esc(data.shared.plan_version)}<br>입력 ${esc(data.shared.snapshot_id)}</p></section><div class="risk-flow"><span>누적 종목 기록</span><span>→ 버전 고정 · 입력 구조화</span><span>→ Python 계산</span><span>→ 계좌 영향 논의</span></div><section class="risk-panel"><h2>조건과 사건 처리</h2><p class="risk-muted">반복 관측·자료 소실·해소·재발을 구분합니다. 아래 버튼은 임시 DB와 가짜 전달기로만 검증합니다.</p><button class="risk-action" id="risk-events">조건·가짜 알림 예제 확인</button><div id="risk-event-output" aria-live="polite"></div></section>`;
    content.querySelector('#risk-events').onclick=async()=>{
     const target=content.querySelector('#risk-event-output');target.textContent='합성 사건 처리 중…';
     try {const e=await transport.events();target.innerHTML=`<div class="risk-table-wrap"><table class="risk-table"><thead><tr><th>관측</th><th>조건 상태</th><th>이탈 유지</th><th>새 사건</th></tr></thead><tbody>${e.transitions.map(t=>`<tr><td>${esc(t.label)}</td><td>${esc({matched:'충족',clear:'해소',deferred:'판정 보류'}[t.state]||t.state)}</td><td>${t.latched?'유지':'없음'}</td><td>${esc({triggered:'발생',resolved:'해소',recurred:'재발'}[t.event_kind]||'추가 없음')}</td></tr>`).join('')}</tbody></table></div><p>가짜 전달: 확실한 미전송 후 재시도 → 전달 불명 보존</p><p class="risk-muted">재시작 후 자동 재전송 없음 · 가짜 호출 ${e.fake_calls}회 · 실제 외부 전송 0회</p>`;} catch {target.textContent='검증 예제를 불러오지 못했습니다.';}
    };
   } else {
    content.innerHTML=`<section class="risk-panel"><h2>이번 계산의 가정</h2><p class="risk-muted">원문 계획·실제 계좌를 바꾸지 않습니다. 입력 변경 후 저장된 합성 계좌를 Python으로 다시 계산합니다.</p><div class="risk-fields"><label>${esc(symbol)} 최종 평가가격 · ${esc(currency)}<input id="risk-final" type="number" min="0" step="any" value="${esc(scenario.prices[linkedId].price)}"></label><label>가정 체결가격 · ${esc(currency)}<input id="risk-fill" type="number" min="0" step="any" value="${esc(scenario.response.steps[0].fill.price)}"></label><label>최초 배정 물량의 매도 비율<input id="risk-quantity" type="number" min="0" max="1" step="any" value="${esc(scenario.response.steps[0].quantity.value)}"></label><label>가정 환율 · ${esc(data.snapshot.base_currency)} / ${esc(currency)}<input id="risk-fx" type="number" min="0" step="any" value="${esc(scenario.fx_to_base[currency])}"></label></div><label class="risk-check"><input type="checkbox" id="risk-no-fill"> 가정 체결가격이 없는 경우 확인</label><div class="risk-toolbar"><label for="risk-example">계좌 자료</label><select id="risk-example"><option value="complete">완전한 합성 계좌</option><option value="partial">계좌 일부 미확인</option><option value="missing_fx">기준 USD 환율 결측</option></select><span class="risk-muted">비용 제외를 명시한 예제</span></div><div class="risk-live" id="risk-progress" role="status">입력값을 바꾸면 자동 재계산합니다.</div></section><div id="risk-results"></div>`;
    const renderResults = () => {
     const r=result.response;
     content.querySelector('#risk-results').innerHTML=cards(result)+issues(result.no_response)+issues(r)+`<section class="risk-panel"><h2>대응에 따른 수량과 현금</h2>${r.state==='deferred'?'<p class="risk-state">필요한 입력이 없어 대응 결과를 보류했습니다.</p>':`<p>${esc(symbol)} 잔여 <strong>${esc(r.remaining_quantities?.[linkedId]??'미확인')}주</strong> · ${r.cash_changes?.length?'가정 대응 후 '+esc(currency)+' 현금 <strong>'+money(r.cash_changes[0].after,currency)+'</strong>':'현금 이동 없음'}</p><div class="risk-table-wrap"><table class="risk-table"><thead><tr><th>조건 확인가격</th><th>가정 체결가격</th><th>가정 수량</th><th>현금 변화</th></tr></thead><tbody>${(r.ledger||[]).map(l=>`<tr><td>${money(l.condition_price,currency)}</td><td>${money(l.fill_price,currency)}</td><td>${esc(l.quantity??'미발동')}</td><td>${money(l.cash_delta,currency)} ${esc(currency)}</td></tr>`).join('')}</tbody></table></div>`}<p class="risk-muted">실제 최대손실이나 체결 결과가 아닙니다. 매도대금은 원통화 현금으로 보존하고, 기준 통화 평가에 가정 환율을 한 번 적용합니다.</p></section><p class="risk-code">계산 ${esc(result.calculator_version)} · 입력 ${esc(result.snapshot_id)}<br>계획 ${esc(result.plan_refs.map(p=>p.id+' v'+p.version).join(', '))}</p>`;
    };
    content.querySelector('#risk-example').value=example;
    content.querySelector('#risk-no-fill').checked=scenario.response.steps[0].fill.price==null;
    let timer;
    const recalculate=async()=>{
     const request=++generation, status=content.querySelector('#risk-progress');status.textContent='Python으로 계산 중…';
     const val=id=>{const v=content.querySelector(id).value;return v===''?null:v;};
     scenario.prices[linkedId].price=val('#risk-final');scenario.fx_to_base[currency]=val('#risk-fx');
     scenario.response.steps[0].quantity.value=val('#risk-quantity');
     scenario.response.steps[0].fill.price=content.querySelector('#risk-no-fill').checked?null:val('#risk-fill');
     example=content.querySelector('#risk-example').value;
     try {const next=await transport.calculate(scenario,example);if(request!==generation)return;result=next;renderResults();status.textContent='재계산 완료 · 원문과 실제 보유는 그대로입니다.';} catch {if(request===generation)status.textContent='입력 또는 연결을 확인해 주세요. 이전 결과가 표시되어 있습니다.';}
    };
    content.querySelectorAll('input,select').forEach(i=>i.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(recalculate,180);}));
    renderResults();
   }
  };
  draw();
 }
 return {mount};
})();

if(document.getElementById('risk-root')) {
 let token;
 const post=async(path,payload)=>{const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Risk-Preview-Token':token},body:JSON.stringify(payload)});if(!response.ok)throw Error('risk_request_failed');return response.json();};
 WorkbenchRisk.mount(document.getElementById('risk-root'),{
  load:async()=>{const response=await fetch('/api/risk-demo');if(!response.ok)throw Error('risk_load_failed');const data=await response.json();token=data.token;return data;},
  calculate:(scenario,example)=>post('/api/risk-calculate',{scenario,example}),
  events:()=>post('/api/risk-events-demo',{})
 }).catch(()=>{document.getElementById('risk-root').textContent='격리 검증 화면을 불러오지 못했습니다.';});
}
