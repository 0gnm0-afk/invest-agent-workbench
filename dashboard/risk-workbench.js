'use strict';
window.WorkbenchRisk = (() => {
 const esc = x => String(x ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const money = (v,c) => WorkbenchFormat.money(v,c);
 // Display grouping requested by the user. This never supplies investable cash
 // or changes the Python risk calculation's reconciliation requirements.
 const numeric = x => x == null || x === '' || !Number.isFinite(Number(x)) ? null : Number(x);
 const isEquity = p => {
  if(p.instrument_class_hint==='CMA_note_from_broker_label')return false;
  const type=String(p.asset_type||p.product_type||p.security_type||'').toLowerCase();
  return !type || ['stock','equity','etf','adr','common_stock','commonshare','depositary_receipt'].includes(type);
 };
 function composition(bundle,alias=null) {
  const currency=bundle?.scenario_input?.base_currency||'KRW',rates=bundle?.scenario_input?.fx_to_base||{},rows=[];
  const add=(account,kind,label,amount,c,source)=>{amount=numeric(amount);const rate=c===currency?1:numeric(rates[c]);rows.push({account,kind,label,amount,currency:c,value:amount!=null&&rate>0?amount*rate:null,source});};
  const accounts=(bundle?.accounts||[]).filter(a=>!alias||a.alias===alias);
  for(const a of accounts){
   for(const p of a.positions||[]){
    const stock=isEquity(p),q=numeric(p.quantity),price=numeric(p.price);
    const observed=p.currency==='KRW'?numeric(p.observed_evaluation_krw):null;
    const amount=stock?(p.data_unavailable_reason?null:q!=null&&price!=null?q*price:null):numeric(p.native_value??p.market_value)??observed??(q!=null&&price!=null?q*price:null);
    add(a.alias,stock?'stock':'cash',p.name||p.symbol,amount,p.currency,'저장 평가');
   }
   const cash=a.cash||{},d=a.observed_deposits||{};
   for(const [c,v] of Object.entries(cash))add(a.alias,'cash',c+' 예수금',v,c,'저장 잔액');
   // D+1/D+2 and KRW representations of foreign cash are not extra balances.
   if(!Object.hasOwn(cash,'KRW')&&d.KR&&Object.hasOwn(d.KR,'dca'))add(a.alias,'cash','원화 예수금',d.KR.dca,'KRW','관측 예수금');
   if(!Object.hasOwn(cash,'USD')&&d.US&&Object.hasOwn(d.US,'fc_dca'))add(a.alias,'cash','외화 예수금',d.US.fc_dca,'USD','관측 예수금');
  }
  const groups=['stock','cash'].map(kind=>{const items=rows.filter(r=>r.kind===kind),known=items.filter(r=>r.value!=null);return {kind,items,subtotal:known.length?known.reduce((n,r)=>n+r.value,0):!items.length&&accounts.length&&accounts.every(a=>(kind==='stock'?a.holdings_complete:a.cash_complete)===true)?0:null,missing:items.filter(r=>r.value==null).length};});
  const total=groups.every(g=>g.subtotal!=null)?groups.reduce((n,g)=>n+g.subtotal,0):null;
  const partial=!accounts.length||accounts.some(a=>a.holdings_complete!==true||a.cash_complete!==true)||groups.some(g=>g.missing);
  return {currency,rows,groups,total,partial,canChart:total>0&&groups.every(g=>g.subtotal>=0&&!g.missing)};
 }
 function compositionHTML(bundle,alias) {
  const c=composition(bundle,alias),names={stock:'주식',cash:'현금성 자산'};
  const share=g=>c.canChart?(g.subtotal/c.total*100).toFixed(1)+'%':'비중 보류';
  return `<div class="account-composition"><div class="composition-heading"><h3>계좌 자산 구성</h3><span>${c.partial?'금액이 있는 저장 항목 기준':'저장 항목 기준'} · ${esc(c.currency)}</span></div>
   <div class="risk-stats">${c.groups.map(g=>`<div class="risk-stat"><span class="composition-key ${g.kind}">${names[g.kind]}</span><strong>${money(g.subtotal,c.currency)} <small>${esc(c.currency)}</small></strong><span>${share(g)} · ${g.items.filter(r=>r.kind==='stock'||r.amount!==0).length}개 항목${g.missing?' · 금액 미확인 '+g.missing+'개':''}</span></div>`).join('')}<div class="risk-stat"><span>표시 합계</span><strong>${money(c.total,c.currency)} <small>${esc(c.currency)}</small></strong><span>저장 평가액·예수금 기준 참고 합계</span></div></div>
   ${c.canChart?`<div class="composition-bar" role="img" aria-label="표시 합계 중 주식 ${share(c.groups[0])}, 현금성 자산 ${share(c.groups[1])}">${c.groups.map(g=>`<span class="${g.kind}" style="width:${g.subtotal/c.total*100}%"></span>`).join('')}</div>`:'<p>비중을 표시할 금액이 부족합니다.</p>'}
   <p>주식 외 자산은 현금성 자산으로 묶어 표시합니다. 표시 합계는 매수 가능액이나 부채를 차감한 순자산이 아닙니다.</p>
   <details class="composition-details"><summary>자산 구성 상세 · 금액과 분류 근거</summary><div class="scroll"><table><thead><tr><th>계좌 · 자산</th><th>분류</th><th>저장 금액</th><th>${esc(c.currency)} 환산</th></tr></thead><tbody>${c.rows.map(r=>`<tr><td>${esc(r.account)} · ${esc(r.label)}</td><td>${names[r.kind]}</td><td>${money(r.amount,r.currency)} ${esc(r.currency)}</td><td>${money(r.value,c.currency)}</td></tr>`).join('')||'<tr><td colspan="4">저장된 자산 항목이 없습니다.</td></tr>'}</tbody></table></div><p>환율은 저장 기준을 사용합니다. D+1·D+2 예수금과 외화 예수금의 원화 표시를 중복 합산하지 않습니다. 현금성 분류는 사용자 지정 표시이며, 예수금·CMA 중복 여부와 부채 대조는 위험 계산에서 별도로 확인합니다.</p></details></div>`;
 }
 const scenarioReady = r => r.plan.scenarios.some(s=>s.evaluation_at&&s.assumed_price!=null&&s.currency&&s.response&&(s.response==='none'||s.price_path?.length));
 const labels = {complete:'계산 완료',partial:'일부만 계산',unavailable:'계산 보류',deferred:'대응 계산 보류'};
 const message = x => ({saved_account_input_unavailable:'계산용 저장 계좌 입력이 아직 연결되지 않았습니다.',
  test_record_not_operational:'테스트 계획은 운영 계산에서 제외됩니다.',collaboration_changed_recalculate:'논의 또는 계획이 변경되었습니다. 다시 계산해 주세요.',
  saved_account_changed_recalculate:'저장 계좌가 변경되었습니다. 다시 계산해 주세요.',calculation_ticket_expired_or_used:'저장 확인 시간이 지났거나 이미 사용했습니다. 저장 이력을 확인해 주세요.',
  account_or_stable_instrument_link_missing_or_ambiguous:'계좌와 종목의 고유 식별자를 연결하지 못했습니다.',
  scenario_total_cost_required:'계획에 시나리오 총비용이 없습니다.',allocation_lifecycle_assumption_required:'배정 물량이 최초 물량인지 잔여 물량인지 확인이 필요합니다.',initial_allocation_unknown_for_remaining_basis:'현재 잔여 물량만 있어 최초 물량 비율을 계산할 수 없습니다.'})[x] || x;
 async function rpc(op,args) {
  const health=await fetch('/health');if(!health.ok)throw Error('서버 상태를 읽지 못했습니다.');
  const {token}=await health.json();
  const response=await fetch('/api/analysis-rpc',{method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Token':token},body:JSON.stringify({op,arguments:args})});
  const value=await response.json();if(!response.ok)throw Error(message(value.error));return value;
 }
 const input=(label,name,value='',type='text')=>`<label>${esc(label)}<input data-field="${esc(name)}" type="${type}" value="${esc(value)}"></label>`;
 const local=x=>x?new Date(new Date(x).getTime()+9*3600000).toISOString().slice(0,16):'';
 const iso=x=>x?x+':00+09:00':null;
 const stats=(result,c)=>['baseline','no_response','response'].map((key,i)=>{const r=result?.[key];return `<div class="risk-stat"><span>${['현재 저장 자산','가정: 대응하지 않을 때','가정: 계획대로 대응할 때'][i]}</span><strong>${money(r?.total,c)} <small>${esc(c||'')}</small></strong><span>${esc(labels[r?.state]||'계산 전')}${r?.total==null&&r?.known_subtotal!=null&&r?.included?.length?' · 확인된 부분 '+money(r.known_subtotal,c):''}</span>${i&&r?.change!=null?`<span>변화 ${money(r.change,c)} ${esc(c)}</span>`:''}</div>`;}).join('');
 function reviewHTML(item) {const r=item.record,c=r.calculation;return `<article class="risk-review"><strong>${esc(r.title)}</strong><p>${esc(r.created_at)} · ${esc(r.id)}</p><div class="risk-stats">${stats(c.result,c.result.base_currency)}</div><p>저장 검토 · 계좌 기준 ${esc(c.assumptions.account_snapshot?.as_of)} · 평가 ${esc(c.result.horizon)} · 현재 잔고와 다를 수 있습니다.</p><details><summary>계획 버전·입력 근거</summary><pre>${esc(JSON.stringify({plan_refs:c.result.plan_refs,account_hash:c.result.snapshot_hash,scenario_hash:c.result.scenario_hash,code_version:c.code_version,code_sha256:c.result.code_sha256,limitations:c.result.limitations,external_inputs:c.assumptions.external_inputs},null,2))}</pre></details><p>${esc(r.body)}</p><a href="${esc(item.links.dashboard_url)}">종목의 공통 기록 보기 →</a></article>`;}
 async function mount(host,{accountAlias=null,title='나의 계좌',accountHTML='',showAccountLink=true,accountBundle=null}={}) {
  if(!host)return;
  host.classList.add('risk-workbench');host.innerHTML=`<section class="panel"><div class="panel-head"><h2>${esc(title)}</h2></div><div class="panel-body"><div class="risk-account-info">${accountHTML}</div>저장 계좌와 계획을 읽는 중…</div></section>`;
  try {
   const data=await rpc('risk_overview',{account_alias:accountAlias});if(!host.isConnected)return;
   const s=data.snapshot, currency=s?.base_currency, holdings=s?.positions||[], readyCount=data.plans.filter(p=>scenarioReady(p.record)).length;
   host.innerHTML=`<section class="panel"><div class="panel-head"><h2>${esc(title)}</h2>${showAccountLink?'<a href="#accounts">계좌 상세 보기 →</a>':''}</div><div class="panel-body">
    <div class="risk-account-info">${accountHTML}</div>
    ${data.test_mode?'<p class="risk-note">격리 검증 · 합성 계좌와 합성 계획</p>':''}${accountHTML?'':`<p>저장 잔고 기준 ${esc(s?.as_of||'미확인')} · 새 조회 없이 계산합니다.</p>`}
    ${compositionHTML(accountBundle,accountAlias)}
    <p class="account-plan-summary">저장된 매매 계획 · 채택 ${data.plans.filter(p=>p.adopted).length}개 · 초안 ${data.plans.filter(p=>!p.adopted).length}개 · 위험 검토 <span data-review-count>${data.reviews.length}</span>개</p>
    <div class="risk-issues">${data.issues.map(x=>`<p>${esc(message(x))}</p>`).join('')}</div>
    <details><summary>보유 종목과 계산 가능 범위 · ${holdings.length}개</summary><div class="scroll"><table><thead><tr><th>계좌 · 종목</th><th>수량</th><th>저장 가격</th><th>계획 연결</th></tr></thead><tbody>${holdings.map(p=>`<tr><td>${esc(p.account)} · ${esc(p.symbol)}</td><td>${esc(p.quantity)}</td><td>${money(p.price,p.currency)} ${esc(p.currency)}</td><td>${p.stable_id?'식별자 있음':'고유 식별자 미확인'}</td></tr>`).join('')}</tbody></table></div><p>현금·CMA·부채는 대조된 항목만 합산합니다. 결측은 0으로 바꾸지 않습니다.</p><ul>${(data.baseline?.missing||[]).map(x=>`<li>${esc(message(x))}</li>`).join('')}</ul></details>
    <details class="risk-editor"><summary>계획으로 시나리오 계산하기 · ${data.plans.length}개 버전</summary><p>에이전트가 논의에서 계산용 시나리오를 준비합니다. 준비된 버전은 여기서 검토할 수 있으며 계산은 계획을 채택하거나 주문을 실행하지 않습니다.</p>
    <div class="risk-plan-list">${data.plans.map((p,i)=>{const r=p.record;return `<article class="risk-plan" data-plan="${i}"><label class="risk-choice"><input type="checkbox" data-select ${scenarioReady(r)?'':'disabled'}><strong>${esc(r.plan.title)} · v${r.plan_version}</strong> <span>${p.adopted?'채택한 버전':'초안 · 가정 계산'}</span></label><p>${esc(r.plan.account_alias||'계좌 미정')} · ${esc(r.identity.symbol)} · ${esc(r.plan.purpose||'목적 미정')} · ${esc(r.plan.allocation.quantity??'물량 미정')}주</p>${scenarioReady(r)?'':`<p>계산 가능한 시나리오가 아직 없습니다. 대화에서 확인된 조건으로 에이전트가 초안을 작성해야 합니다. <a href="${esc(p.links.resume_codex_url)}">에이전트와 계산 준비 이어가기 →</a></p>`}<select data-scenario aria-label="${esc(r.plan.title)} 시나리오">${r.plan.scenarios.map(v=>`<option value="${esc(v.id)}">${esc(v.id)} · ${esc(v.evaluation_at||'평가일 미정')} · ${money(v.assumed_price,v.currency)} ${esc(v.currency)} · ${v.response==='plan'?'계획 대응':v.response==='none'?'대응 없음':'대응 미정'}</option>`).join('')}</select><label>배정 물량의 계산 기준<select data-allocation><option value="">계획에 명시된 기준 사용 · 미정이면 보류</option><option value="initial">명시된 주수를 최초 배정 물량으로 가정</option><option value="current_remaining">명시된 주수를 현재 잔여 배정 물량으로 가정</option></select></label><div data-timing></div><details><summary>저장 계획의 조건과 미정</summary>${r.plan.conditions.map(c=>`<p>${esc(c.statement)} · ${esc(c.action)} · 기준 ${money(c.price,c.currency)} ${esc(c.currency)} · 가정 체결 ${money(c.execution_price,c.currency)} ${esc(c.currency)} · ${esc(c.quantity.basis||'수량 기준 미정')} ${esc(c.quantity.value??'')}</p>`).join('')}<p>${esc((r.blockers||[]).map(message).join(' · '))}</p><p>기록 ${esc(r.id)} · 입력 ${esc(r.plan_sha256)}</p><a href="${esc(p.links.dashboard_url)}">공통 기록 보기 →</a></details></article>`;}).join('')||'<p>저장된 계획이 없습니다. 종목에서 논의한 계획과 시나리오를 먼저 저장해 주세요.</p>'}</div>
    <div class="risk-calculation-controls" ${readyCount?'':'hidden'}><div class="risk-fields">${input('공통 평가 시각 (KST)','horizon','','datetime-local')}${[...new Set([...holdings.map(p=>p.currency),...(s?.cash||[]).map(c=>c.currency)])].filter(c=>c!==currency).map(c=>input('미래 환율 · 1 '+c+' → '+currency,'fx:'+c,'','number')).join('')}
    <label>비용 가정<select data-field="cost_policy"><option value="">선택해 주세요</option><option value="excluded">비용 제외를 명시적으로 가정</option><option value="record_total">각 계획의 시나리오 총비용을 한 번 반영</option></select></label></div>
    <details><summary>계획이 없는 보유 종목의 가정 가격</summary><p>선택한 계획으로 가격이 정해지는 종목은 비워 두세요. 빈 값은 해당 종목의 미래 평가를 보류합니다.</p><div class="risk-fields">${holdings.map(p=>input(p.account+' · '+p.symbol+' ('+p.currency+')','price:'+p.id,'','number')).join('')}</div></details>
    <label class="risk-choice"><input type="checkbox" data-field="combine">같은 종목의 서로 다른 목적 물량을 함께 계산합니다</label><p>같은 계획의 대안은 함께 선택할 수 없으며, 목적별 물량이 보유량을 넘으면 보류합니다.</p>
    <div class="risk-actions"><button type="button" data-calculate>선택한 계획 계산</button>${data.reviews.length?'<button type="button" data-reuse>최근 검토의 가정 불러오기</button>':''}</div></div></details>
    <p role="status" aria-live="polite" data-status></p><div data-result></div>
    <div data-save hidden><label>검토를 남길 종목 기록<select data-target></select></label>${input('검토 제목','review_title','계좌 시나리오 위험 검토')}<label>검토 메모<textarea data-body rows="3"></textarea></label><button type="button" data-save-button>위험 검토 저장</button></div>
    <details class="risk-history" ${data.reviews.length?'open':''}><summary>저장된 위험 검토 · ${data.reviews.length}개</summary><div data-reviews>${data.reviews.map(reviewHTML).join('')||'<p>아직 저장된 검토가 없습니다.</p>'}</div></details>
   </div></section>`;
   const q=x=>host.querySelector(x), all=x=>[...host.querySelectorAll(x)], val=name=>all('[data-field]').find(x=>x.dataset.field===name);
   let calculated=null,sequence=0;
   const invalidate=()=>{sequence++;calculated=null;q('[data-save]').hidden=true;q('[data-status]').textContent='가정이 바뀌었습니다. 다시 계산해 주세요.';};
   function timing(node, saved={}) {
    const r=data.plans[Number(node.dataset.plan)].record, scenario=r.plan.scenarios.find(x=>x.id===node.querySelector('[data-scenario]').value);
    node.querySelector('[data-timing]').innerHTML=!scenario||scenario.response!=='plan'?'':`<details><summary>대응 계산에 필요한 시각·완료봉 가정</summary><p>날짜 경로만으로 완료 일봉·주봉을 단정하지 않습니다. 모든 시각은 KST입니다.</p>${(scenario.price_path||[]).map(pt=>{const o=saved.path_observations?.[pt.at]||{};return `<div class="risk-path" data-day="${esc(pt.at)}"><strong>${esc(pt.at)} · ${money(pt.price,scenario.currency)} ${esc(scenario.currency)}</strong><div class="risk-fields">${input('관측 시각 (KST)','observed',local(o.as_of),'datetime-local')}<label>관측 주기<select data-frame><option value="">미정</option>${[['intraday','장중'],['1d','일봉'],['1w','주봉']].map(([v,l])=>`<option value="${v}" ${o.frame===v?'selected':''}>${l}</option>`).join('')}</select></label>${input('시장 세션 날짜','session',o.session||'','date')}</div><label class="risk-choice"><input type="checkbox" data-complete ${o.complete?'checked':''}>이 관측값은 완료된 봉으로 가정</label></div>`;}).join('')}${r.plan.conditions.filter(c=>['reduce','buy','add'].includes(c.action)).map(c=>{const f=saved.fills?.[c.id]||{};return `<div class="risk-fill" data-condition="${esc(c.id)}"><strong>${esc(c.statement)}</strong><div class="risk-fields"><label>연결할 경로 날짜<select data-path-date><option value="">미정</option>${(scenario.price_path||[]).map(pt=>`<option ${pt.at===f.path_date?'selected':''}>${esc(pt.at)}</option>`).join('')}</select></label>${input('가정 체결 시각 (KST)','fill_at',local(f.at),'datetime-local')}${input('체결 가정 근거','fill_note',f.assumption||'')}</div><p>저장된 가정 체결가격 ${money(c.execution_price,c.currency)} ${esc(c.currency)}. 바꾸려면 새 계획 버전을 저장하세요.</p></div>`;}).join('')}</details>`;
   }
   all('[data-plan]').forEach(node=>{timing(node);node.querySelector('[data-scenario]').onchange=()=>timing(node);});
   host.addEventListener('input',e=>{if(e.target.closest('.risk-editor'))invalidate();});
   function request() {
    const assumptions={id:'account-scenario',version:1,horizon:iso(val('horizon').value),fx_to_base:{[currency]:'1'},cost_policy:val('cost_policy').value,combine_allocations:val('combine').checked,per_plan:{},unplanned_prices:{}};
    all('[data-field]').forEach(x=>{if(x.dataset.field.startsWith('fx:')&&x.value)assumptions.fx_to_base[x.dataset.field.slice(3)]=x.value;if(x.dataset.field.startsWith('price:')&&x.value)assumptions.unplanned_prices[x.dataset.field.slice(6)]=x.value;});
    const selections=all('[data-plan]').filter(n=>n.querySelector('[data-select]').checked).map(n=>{const p=data.plans[Number(n.dataset.plan)],r=p.record,local={path_observations:{},fills:{},allocation_basis:n.querySelector('[data-allocation]').value};
     n.querySelectorAll('[data-day]').forEach(row=>{const field=k=>row.querySelector('[data-field="'+k+'"]').value, at=iso(field('observed'));if(at)local.path_observations[row.dataset.day]={snapshot_id:'assumed:'+r.id+':'+row.dataset.day,as_of:at,currency:r.identity.currency,price_basis:r.identity.price_basis,frame:row.querySelector('[data-frame]').value,session:field('session'),expected_session:field('session'),complete:row.querySelector('[data-complete]').checked,max_age_seconds:1};});
     n.querySelectorAll('[data-condition]').forEach(row=>{const at=iso(row.querySelector('[data-field="fill_at"]').value);if(at)local.fills[row.dataset.condition]={at,path_date:row.querySelector('[data-path-date]').value,assumption:row.querySelector('[data-field="fill_note"]').value};});
     assumptions.per_plan[r.id]=local;return {analysis_key:p.analysis_key,record_id:r.id,plan_version:r.plan_version,plan_sha256:r.plan_sha256,snapshot_sha256:r.snapshot_sha256,mode:p.adopted?'adopted':'hypothetical',scenario_id:n.querySelector('[data-scenario]').value};
    });return {selections,assumptions,account_alias:accountAlias};
   }
   q('[data-reuse]')?.addEventListener('click',()=>{const a=data.reviews[0].record.calculation.assumptions;val('horizon').value=local(a.horizon);val('cost_policy').value=a.cost_policy;val('combine').checked=a.combine_allocations===true;
    all('[data-field]').forEach(x=>{if(x.dataset.field.startsWith('fx:'))x.value=a.fx_to_base?.[x.dataset.field.slice(3)]||'';if(x.dataset.field.startsWith('price:'))x.value=a.unplanned_prices?.[x.dataset.field.slice(6)]||'';});
    all('[data-plan]').forEach(n=>{const r=data.plans[Number(n.dataset.plan)].record,selection=a.selected_inputs?.find(x=>x.record_id===r.id);n.querySelector('[data-select]').checked=!!selection;if(selection){n.querySelector('[data-scenario]').value=selection.scenario_id;n.querySelector('[data-allocation]').value=a.per_plan?.[r.id]?.allocation_basis||a.allocation_basis||'';timing(n,{...a,...a.per_plan?.[r.id]});}});invalidate();q('[data-status]').textContent='저장된 가정을 불러왔습니다. 현재 계좌·채택 상태로 다시 계산해 주세요.';});
   q('[data-calculate]').onclick=async()=>{const seq=++sequence;calculated=null;q('[data-save]').hidden=true;q('[data-status]').textContent='저장된 정확한 버전을 확인하고 계산 중…';q('[data-calculate]').disabled=true;
    try{const value=await rpc('risk_calculate',request());if(seq!==sequence||!host.isConnected)return;calculated=value;const r=value.result;
     q('[data-result]').innerHTML=`<div class="risk-stats">${stats(r,r.base_currency)}</div><p>가정 계산 · 평가 ${esc(r.horizon)}</p><ul>${[...new Set([...(r.no_response.missing||[]),...(r.response.missing||[]),...value.projections.flatMap(p=>p.blockers.concat(p.response_blockers))])].map(x=>`<li>${esc(message(x))}</li>`).join('')}</ul><details><summary>계획별 미정·정성 검토</summary>${value.projections.map(p=>`<p>${esc(p.ref.record_id)}: ${esc(p.record_blockers.concat(p.limitations||[]).map(message).join(' · ')||'없음')}</p>`).join('')}</details>`;
     q('[data-target]').innerHTML=value.save_targets.map(id=>`<option value="${esc(id)}">${esc(data.plans.find(p=>p.analysis_key===id)?.record.identity.symbol)} · ${esc(id)}</option>`).join('');q('[data-save]').hidden=false;q('[data-status]').textContent='계산 완료. 검토 저장은 선택한 종목 기록에 이 계좌 계산을 남깁니다.';
    }catch(e){q('[data-status]').textContent=e.message;}finally{q('[data-calculate]').disabled=false;}};
   q('[data-save-button]').onclick=async()=>{if(!calculated)return;const value=calculated;calculated=null;q('[data-save-button]').disabled=true;
    try{await rpc('risk_save',{ticket:value.ticket,analysis_key:q('[data-target]').value,title:val('review_title').value,body:q('[data-body]').value});const fresh=await rpc('risk_overview',{account_alias:accountAlias});if(!host.isConnected)return;data.reviews=fresh.reviews;q('[data-review-count]').textContent=fresh.reviews.length;q('[data-reviews]').innerHTML=fresh.reviews.map(reviewHTML).join('');q('.risk-history summary').textContent='저장된 위험 검토 · '+fresh.reviews.length+'개';q('.risk-history').open=true;q('[data-save]').hidden=true;q('[data-status]').textContent='위험 검토 저장 후 DB 재조회를 확인했습니다.';
    }catch(e){q('[data-status]').textContent=e.message+' 저장 이력을 재조회한 후 다시 계산해 주세요.';}finally{q('[data-save-button]').disabled=false;}};
  } catch(e) {if(host.isConnected)host.innerHTML=`<section class="panel"><div class="panel-head"><h2>${esc(title)}</h2></div><div class="panel-body"><div class="risk-account-info">${accountHTML}</div><p>계획 시나리오를 불러오지 못했습니다. ${esc(e.message)}</p><a href="#accounts">계좌 정보 보기 →</a></div></section>`;}
 }
 return {mount,composition,isEquity};
})();
