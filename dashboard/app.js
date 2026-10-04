'use strict';
const $ = (s) => document.querySelector(s);
const esc = (x) => String(x ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = (x, digits = 2) => x == null || x === '' ? '미확인' : Number(x).toLocaleString('ko-KR', {maximumFractionDigits:digits});
const money = WorkbenchFormat.money;
const pct = x => x == null ? '결측' : num(x * 100) + '%';
const stamp = x => x ? new Date(x).toLocaleString('ko-KR', {timeZone:'Asia/Seoul',hour12:false}) + ' KST' : '미확인';
const phase = {watch:'관찰 시작', ready:'돌파 대기', entry:'진입 조건 충족'};
const state = {received:'조회 성공',partial:'일부 결측',failed:'조회 실패',not_requested:'저장값 사용',unavailable:'미확인'};
const tag = (text, cls = '') => `<span class="tag ${cls}">${esc(text)}</span>`;
const stockLink = (market, symbol, name) => `<a href="#stock/${encodeURIComponent(market)}/${encodeURIComponent(symbol)}"><span class="stock-name">${esc(name || symbol)}</span><span class="stock-code">${esc(market)} · ${esc(symbol)}</span></a>`;
const block = (title, body, link = '') => `<section class="panel"><div class="panel-head"><h2>${title}</h2>${link}</div>${body}</section>`;
const explain = x => ({protection_uses_saved_completed_bars_check_each_price_date:'보호 기준선은 과거 저장 봉으로 계산했습니다. 종목별 봉 기준일과 판정일을 확인하세요.',KR_US_scope_not_all_assets:'국내·미국 주식 범위이며 전체 금융자산이 아닙니다.',cash_and_liabilities_not_reconciled:'현금·부채 대조 미완료',working_orders_not_reconciled:'미체결·예약은 이번 조회 범위에 포함하지 않습니다.',adopted_protection_not_applicable_to_this_snapshot:'채택 보호선과 현재 잔고의 대응을 확인하지 못했습니다.',risk_calculation_unavailable:'위험 계산에 필요한 자료가 부족합니다.'})[x] || (x.includes('cash_inventory_incomplete') || x.includes('gross_cash_balance_unavailable') ? '계좌 현금 구성·총액 미확인' : x);
const metric = (label, value) => `<div class="metric"><small>${WorkbenchHelp.text(label)}</small><strong>${esc(value)}</strong></div>`;
let DATA, routeVersion = 0, lastRouteHash = location.hash;
function heading(title, eyebrow, subtitle) { return `<div class="heading"><div><div class="eyebrow">${eyebrow}</div><h1>${esc(title)}</h1><p class="sub">${esc(subtitle)}</p></div><div class="date-box">자료 기준 · 완료 일봉<strong>KR ${esc(DATA.market.session_dates?.KR || '미확인')}<br>US ${esc(DATA.market.session_dates?.US || '미확인')}</strong></div></div>`; }
function marketStatus() {
 const m=DATA.market;
 const data=({succeeded:'확보 완료',complete:'확보 완료',partial:'일부 결측',failed:'수집 실패',unavailable:'미준비'})[m.quality?.state]||'미확인';
 const execution=m.state==='failed'?'실패':({completed:'완료',completed_with_exceptions:'완료 · 자료 예외',failed:'실패'})[m.processing_state]||({partial:'일부 미완료',succeeded:'완료',unavailable:'미준비'})[m.state]||'미확인';
 return `자료 ${data} · 실행 ${execution}`;
}
function qualityNotice() {
  const m = DATA.market, q = m.quality || {};
  const gaps=Object.entries({price_history:'가격 이력 주의',market_cap:'시총 미확보',classification:'업종 분류 미확인'}).filter(([key])=>m.quality_gaps?.[key]>0).map(([key,label])=>label+' '+num(m.quality_gaps[key],0)+'종목').join(' · ');
  return `<div class="notice ${m.state === 'unavailable' ? 'error' : ''}"><strong>${esc(marketStatus())}</strong><span>수신 ${num(q.received,0)} / ${num(q.requested,0)} · 실패 ${num(q.failed,0)} · 재계산 ${stamp(m.finished_at)}${gaps?'<br>'+esc(gaps)+' · 해당 종목의 계산·분류가 제한됩니다.':''}<br>수집 ${stamp(m.data_times?.fetched_at)} → ${stamp(m.data_times?.collected_until)}${m.errors?.length ? '<br>' + esc(m.errors.join(' / ')) : ''}${DATA.daily_update?.saved_only?'<br>시장 최신화 생략 · '+(DATA.daily_update.display_stale?'이전 검증 가능 스크리닝 표시 · ':'')+'최근 시장 실행 상태 '+esc(({partial:'완료 · 일부 자료 결측',succeeded:'완료',failed:'실패',running:'진행 중',interrupted:'중단',unavailable:'미준비'})[DATA.daily_update.latest_state]||'미확인')+' · 장기 차트 기준일과 구분하세요.':''}</span></div>`;
}
function stats() {
 const e=DATA.market.entries, fields=[['관찰 시작',e.filter(x=>x.phase==='watch').length,'추세와 조정 구조를 관찰'],['돌파 대기',e.filter(x=>x.phase==='ready').length,'피벗 돌파·유지 확인'],['진입 조건 충족',DATA.market.entry_candidate_count,'현재 저장 결과 · 0개도 그대로 표시'],['연결 계좌',DATA.accounts.accounts.length,`NH PLUG · ${state[DATA.accounts.query_state] || DATA.accounts.query_state}`]];
 return `<div class="stats">${fields.map(([l,v,d],i)=>`<div class="stat ${i===2?'emphasis':''}"><div class="label">${l}</div><div class="value">${DATA.market.candidate_count == null && i < 3 ? '—' : num(v,0)}<small style="font-size:13px;margin-left:6px">${i===3?'개':'종목'}</small></div><div class="detail">${d}</div></div>`).join('')}</div>`;
}
function accountCards() {
 const a=DATA.accounts;
 return `<div class="panel-body"><p class="sub">${tag(state[a.query_state] || a.query_state,a.query_state==='failed'?'bad':'neutral')} ${a.source_mode==='historical_saved'?tag(a.stale?'과거 저장값':'저장된 실조회',a.stale?'warn':'neutral'):tag('읽기 전용')} ${a.stale?tag('24시간 경과','warn'):''}</p><p class="chart-caption">잔고 기준 ${stamp(a.as_of)}</p>${a.accounts.map((x,i)=>`<a class="account-card" href="#account/${encodeURIComponent(x.alias)}"><div class="row"><div><span class="account-title">NH 계좌 ${i+1}</span><div class="account-id">${esc(x.alias)}</div></div><span class="account-count">${x.holdings_complete?x.positions.length:'—'}<small>보유 항목 →</small></span></div><div class="account-summary">${x.holdings_complete?'국내·미국 잔고 확인':'잔고 일부 미확인'} · 현금 ${x.cash_complete?'확인':'대조 필요'}</div></a>`).join('') || '<div class="empty">표시할 계좌가 없습니다.</div>'}${a.reason?`<p class="danger-text">조회 오류: ${esc(a.reason)}</p>`:''}<p class="chart-caption">총자산·매수 가능 현금은 별도 대조 전 미확인입니다.</p></div>`;
}
function screeningTable(entries) {
 if (!entries.length) return '<div class="empty"><strong>조건에 맞는 후보가 없습니다.</strong>저장 결과의 0개 상태를 그대로 표시합니다.</div>';
 return `<div class="scroll"><table><thead><tr><th>종목</th><th>시가총액</th><th>단계 / 경로</th><th>선별 근거</th><th>피벗까지</th><th>다음 확인</th></tr></thead><tbody>${entries.map(e=>`<tr><td>${stockLink(e.market,e.symbol,e.name)}</td><td class="number">${esc(capText(e))}</td><td>${tag(phase[e.phase] || e.phase,e.phase==='ready'?'warn':'')}<span class="stock-code">${esc((e.origins||[]).map(r=>({pdf_momentum:'PDF 모멘텀',sperandeo:'스페란데오',base_watch:'바닥·눌림 횡보'}[r]||r)).join(' · '))}</span></td><td>${esc(e.entry_signal?.label || e.reasons?.join(' · '))}</td><td class="number">${pct(e.entry_signal?.pivot_gap_pct)}</td><td class="cell-note">${esc(e.entry_signal?.pending?.[0] || '조건 유지 확인')}</td></tr>`).join('')}</tbody></table></div>`;
}
function sectorDetail(market, code) {
 const s=DATA.market.sectors.find(s=>s.market===market&&s.group_code===code);if(!s) return missing();const c=s.current||{};const classification=DATA.market.focus?.items.find(x=>x.market===market&&x.code===code)?.classification;
 $('#main').innerHTML='<a class="back" href="#sectors">← 섹터 관찰</a>'+heading(s.group_name,'SECTOR DETAIL',market+' · '+(c.label||'미확인')+' · '+(c.summary||''))+qualityNotice()+block('섹터 가격 추이',`<div class="panel-body"><div class="metrics">${metric('최근 상대강도',pct(c.rs_now))}${metric('거래활동',num(c.activity_now)+'배')}${metric('상승 참여',pct(c.breadth))}${metric('유효 / 확인 구성',num(c.used,0)+' / '+num(c.matched,0))}</div><p class="chart-caption">${classificationNote(classification)}</p><div id="sector-price" class="chart" role="img" aria-label="섹터 동일비중 가격지수"></div><p class="chart-caption">126세션 고정 구성·시작 100. 현행 분류의 과거 재구성으로 당시 구성 백테스트가 아닙니다.</p><div id="sector-rs" class="chart" role="img" aria-label="섹터 최근 상대강도 63세션 추이"></div><p class="chart-caption">RS 비율(%) · KR 대상 전체 동일비중 / US SPY 프록시 대비 · 날짜별 구성 변경 가능</p><ul class="bullet">${(c.issues||[]).map(x=>`<li>${esc(x)}</li>`).join('')}</ul></div>`)+`<div class="details-grid">${block('주도 종목',leaderTable(s.leaders||[],market))}${block('약세 기여 종목',leaderTable(s.weak_contributors||[],market))}</div>`+block('상태 변화',`<div class="panel-body"><ul class="bullet">${(s.transitions||[]).slice(-8).map(t=>`<li>${esc(t.date)} · ${esc(t.from)} → ${esc(t.to)}</li>`).join('')||'<li>기록된 전환 없음</li>'}</ul></div>`);
 WorkbenchCharts.create($('#sector-price'),[{label:'동일비중 지수',data:WorkbenchCharts.lineData(s.horizons?.['126']?.index||[],'value')}]);WorkbenchCharts.create($('#sector-rs'),[{label:'최근 RS (%)',data:WorkbenchCharts.lineData(s.history||[],'rs_now',100)}]);
}
function leaderTable(rows,market){return rows.length?`<div class="scroll"><table><thead><tr><th>종목</th><th>최근 수익률</th><th>추정 거래 비중</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${stockLink(market,r.symbol,r.name)}</td><td>${pct(r.return_recent)}</td><td>${pct(r.proxy_share)}</td></tr>`).join('')}</tbody></table></div>`:'<div class="empty">검증된 구성 자료 없음</div>';}
async function stockDetail(market, symbol, version) {
 $('#main').innerHTML='<div class="loading">저장 가격 자료를 여는 중…</div>';
 const response=await fetch('/api/stock/'+encodeURIComponent(market+':'+symbol));if(!response.ok)throw Error('price_request_failed');const s=await response.json();if(version!==routeVersion)return;
 const currency=s.currency || ({KR:'KRW',US:'USD'})[market];
 const e=DATA.market.entries.find(e=>e.market===market&&e.symbol===symbol);const sig=e?.entry_signal;const bars=s.bars||[], last=bars.at(-1);
 $('#main').innerHTML=`<div class="stock-detail"><header class="stock-heading"><div class="stock-identity"><a class="back" href="#home">← 작업대</a><div class="stock-title"><h1>${esc(symbol.replace(/\.(KS|KQ)$/,''))}</h1><h2>${esc(s.name||symbol)}</h2></div><p class="sub">${esc(market)} / ${esc(symbol)} · ${esc(e?phase[e.phase]:'현재 스크리너 후보 아님')}</p></div><div class="stock-quote"><strong id="stock-quote-price">${money(last?.close,currency)}</strong><span>${esc(currency)} · <span id="stock-quote-date">스크리닝 가격 ${esc(last?.date||'미확인')}</span></span></div></header><nav class="stock-section-nav" aria-label="종목 상세 영역"><button data-section="stock-price" class="selected">차트</button><button data-section="financials">재무정보</button><button data-section="stock-evidence">선별 근거</button><button data-section="qualitative-report">정성 리포트</button><span>저장 자료 · ${esc(currency)}</span></nav><details class="stock-data-status"><summary>${esc(marketStatus())} · 기준일과 수집 내역</summary>${qualityNotice()}${e?.display_cap?`<p class="chart-caption">저장 시가총액 ${esc(capText(e))} · 기준 ${stamp(e.display_cap.as_of)} · 가격×발행주식수 파생값</p>`:''}</details><section class="stock-chart-panel" id="stock-price" aria-label="가격과 거래량"><div class="stock-chart-toolbar"><button id="stock-tools-toggle" aria-expanded="false" aria-controls="chart-workspace"><span aria-hidden="true">✎</span> 작도 · SMA</button><div class="chart-toolbar" id="timeframe-controls" aria-label="봉 주기"><button data-frame="1d" class="selected">일봉</button><button data-frame="1w">주봉</button><button data-frame="1mo">월봉</button></div><div class="chart-toolbar" id="history-range-controls" aria-label="차트 기간"><button data-range="3">3개월</button><button data-range="6" class="selected">6개월</button><button data-range="12">1년</button><button data-range="60">5년</button><button data-range="all">전체 이력</button></div><button id="stock-chart-expand" aria-pressed="false"><span aria-hidden="true">⛶</span> 차트 확대</button></div><div id="chart-workspace" hidden></div><div class="stock-plot-wrap"><div id="price-chart" class="chart large" role="img" aria-label="저장 OHLC 가격과 거래량 차트"></div><div id="price-legend" class="chart-legend stock-price-legend">차트 위에서 날짜별 수치를 확인하세요.</div></div><div class="stock-chart-foot"><p id="history-price" class="chart-caption"></p><details class="history-info"><summary>가격 이력 · 품질 · 기준일</summary><p id="history-status" role="status" class="chart-caption">장기 가격 이력 준비 중…</p><details id="history-quality" class="chart-caption" hidden><summary>자료 품질 상세</summary><p></p></details><p class="chart-caption">스크리닝 원본 ${bars.length}개 일봉 · 수집 ${stamp(s.collected_at)} · 거래량은 공급자 조정 기준. 피벗·무효화 가격은 저장 관찰값이며 주문 기준이 아닙니다.</p></details><button id="history-retry" class="history-retry" hidden>장기 이력 재시도</button></div><div class="metrics stock-reference-metrics">${metric('스크리닝 당시 종가',money(last?.close,currency)+' '+(currency||''))}${metric('관찰 피벗',money(sig?.pivot,currency))}${metric('피벗까지',pct(sig?.pivot_gap_pct))}${metric('구조 무효화 참고',money(sig?.structure_stop,currency))}</div></section>`+WorkbenchFinancials.shell()+`<div class="details-grid" id="stock-evidence">${block('선별 근거',`<div class="panel-body">${e?tag(phase[e.phase]):tag('후보 아님','neutral')}<h3 style="margin-top:13px">${esc(sig?.label||'현재 선별 근거 없음')}</h3><ul class="bullet">${(e?.reasons||['계좌 보유/섹터 연결에서 열린 종목입니다. 현재 후보로 자동 승격하지 않습니다.']).map(r=>`<li>${WorkbenchHelp.text(WorkbenchFormat.priceText(r,currency))}</li>`).join('')}</ul><p class="chart-caption">${e?'PDF·스페란데오·바닥/눌림 경로의 독립 선정 근거입니다. 바닥/눌림은 관찰 단계만 허용합니다.':''}</p></div>`)}${block('다음 확인과 주의',`<div class="panel-body"><ul class="bullet">${(sig?.pending||['현재 저장된 후속 확인 조건 없음']).concat(sig?.warnings||[]).map(r=>`<li>${WorkbenchHelp.text(WorkbenchFormat.priceText(r,currency))}</li>`).join('')}</ul>${e?`<p class="chart-caption">${WorkbenchHelp.text("구조 위험")} ${pct(sig?.structure_risk_pct)} · ${WorkbenchHelp.text("RS 백분위")} ${num(e.analysis?.rs_percentile)} · 원문 페이지 ${esc(e.analysis?.source_pages?.join(', ')||'해당 없음')}</p><details><summary>저장된 상세 근거 펼치기 · 금액 표시 형식 적용</summary><pre>${esc(JSON.stringify(WorkbenchFormat.analysis(e.analysis,currency),null,2))}</pre></details>`:''}</div>`)}</div><div class="placeholder-grid" id="qualitative-report">${block('정성 리포트',`<div class="empty">${tag('미준비','neutral')}<strong>연결된 검증 리포트가 없습니다.</strong>사업·촉매·위험·반증 근거<br>공시·IR 인용과 사용자 판단을 분리해 연결합니다.</div>`)}</div></div>`;
 document.querySelectorAll('[data-section]').forEach(b=>b.onclick=()=>{document.getElementById(b.dataset.section)?.scrollIntoView({behavior:'smooth',block:'start'});document.querySelectorAll('[data-section]').forEach(x=>x.classList.toggle('selected',x===b));});
 $('#stock-tools-toggle').onclick=()=>{const host=$('#chart-workspace');host.hidden=!host.hidden;$('#stock-tools-toggle').setAttribute('aria-expanded',String(!host.hidden));};
 $('#stock-chart-expand').onclick=()=>{const panel=$('#stock-price'),expanded=panel.classList.toggle('expanded');$('#stock-chart-expand').setAttribute('aria-pressed',String(expanded));$('#stock-chart-expand').innerHTML=expanded?'⛶ 기본 크기':'⛶ 차트 확대';};
 WorkbenchFinancials.mount({market,symbol,isCurrent:()=>version===routeVersion});
 const analysis=await WorkbenchAnalysis.initial();
 if(analysis&&analysis.case.stock_key!==market+':'+symbol)throw Error('분석건 종목이 일치하지 않습니다.');
 const controller=await WorkbenchStockChart.mount({stock:s,market,symbol,currency,isCurrent:()=>version===routeVersion,context:analysis?.case.context});
 if(version===routeVersion)await WorkbenchAnalysis.mount({market,symbol,currency,controller,isCurrent:()=>version===routeVersion,initialDetail:analysis});
}
function accounts() {$('#main').innerHTML=heading('나의 계좌','READ-ONLY ACCOUNTS','NH 국내·미국 잔고 조회. 주문·미체결·예약 조회는 포함하지 않습니다.')+block('조회 결과',accountCards())+`<div class="notice"><span>조회 시도 ${stamp(DATA.accounts.attempted_at)} · 새로 조회하려면 dashboard.ps1을 실행하세요. 브라우저 새로고침은 저장된 화면만 다시 엽니다.</span></div>`;}
function accountDetail(alias) {
 const a=DATA.accounts.accounts.find(a=>a.alias===alias);if(!a)return missing();const bundle=DATA.accounts;const index=bundle.accounts.indexOf(a)+1;const risk=(bundle.risk?.positions||[]).filter(r=>r.account===alias);
 const labels={needs_stop:'보호선 미입력',stop_breached:'보호선 이탈·위험 미확정',review:'보호선 검토',unavailable:'값 미확인'};
 $('#main').innerHTML='<a class="back" href="#accounts">← 나의 계좌</a>'+heading('NH 계좌 '+index,'ACCOUNT / READ ONLY',alias+' · 잔고 기준 '+stamp(bundle.as_of))+`<div class="notice ${bundle.query_state==='failed'?'error':''}"><strong>${state[bundle.query_state]||esc(bundle.query_state)} ${bundle.stale?'· 24시간 경과':''}</strong><span>${bundle.source_mode==='historical_saved'?'과거 저장 잔고를 표시합니다. 새 조회 성공으로 보지 않습니다.':'NH 실제 읽기 전용 조회 결과입니다.'} ${esc(bundle.reason||'')}<br>현금·부채·전체 자산 대조 전입니다. 결측을 0으로 합산하지 않습니다.</span></div>`+block('계좌 관측 상태',`<div class="panel-body"><div class="metrics">${metric('보유 항목',a.holdings_complete?num(a.positions.length,0):'일부 미확인')}${metric('현금 확정',a.cash_complete?'확인':'대조 필요')}${metric('매수 가능 현금','미확인')}${metric('계좌 총자산','미확인')}</div><div class="row">${Object.entries(a.query_markets||{}).map(([m,v])=>`<span>${esc(m)} ${tag(state[v.state]||v.state,v.state==='unavailable'?'bad':'')} ${esc(v.reason||'')}</span>`).join('')}</div><p class="chart-caption">관측 예수금은 출금 가능액·순 매수여력이 아닙니다.</p><div class="scroll"><table><thead><tr><th>시장</th><th>관측 항목</th><th>값</th></tr></thead><tbody>${Object.entries(a.observed_deposits||{}).flatMap(([m,v])=>Object.entries(v).map(([k,n])=>`<tr><td>${esc(m)}</td><td>${esc(({dca:'예수금 (KRW)',nxt_dd_dca:'D+1 예수금 (KRW)',nxt2_dd_dca:'D+2 예수금 (KRW)',fc_dca:'외화 예수금 (USD 조회)',krw_dca:'원화 예수금 (KRW)'})[k]||k)}</td><td>${esc(money(n,({dca:'KRW',nxt_dd_dca:'KRW',nxt2_dd_dca:'KRW',krw_dca:'KRW',fc_dca:'USD'})[k]))}</td></tr>`)).join('')||'<tr><td colspan="3">관측 예수금 결측</td></tr>'}</tbody></table></div></div>`)+block('보유와 기존 위험관리',`<div class="scroll"><table><thead><tr><th>보유 종목</th><th>수량</th><th>관측 가격</th><th>채택 보호선</th><th>보호선까지 위험 (KRW)</th><th>상태</th></tr></thead><tbody>${a.positions.map(p=>{const r=risk.find(r=>r.symbol===p.symbol&&r.market===p.market)||{};return `<tr><td>${p.data_unavailable_reason?`<strong>${esc(p.name||p.symbol)}</strong>`:stockLink(p.market,p.quote_symbol||p.symbol,p.name)}<span class="stock-code">${esc(p.data_unavailable_reason||'')}</span></td><td>${num(p.quantity)}</td><td>${esc(money(p.price,p.currency))} ${esc(p.currency)}</td><td>${esc(money(r.stop_price,p.currency))} ${r.stop_price?esc(p.currency):''}<span class="stock-code">${esc(p.adopted_stop?.rule ? p.adopted_stop.rule.line_type+p.adopted_stop.rule.period+' · '+p.adopted_stop.rule.timeframe+' · '+p.adopted_stop.rule.breach_basis : '')}</span><span class="stock-code">저장 봉 ${esc(r.technical_context?.as_of || '미확인')} · 판정 ${esc(r.protection_check_as_of || '미확인')}</span></td><td>${esc(money(r.stop_exposure_base,'KRW'))}</td><td>${tag((p.adopted_stop?.rule ? ({maintained:'저장 봉 기준 유지',adopted_protection_breached:'저장 봉 기준 이탈',unavailable:'보호규칙 채택·가격/이력 미확인'})[r.protection_state] || '보호규칙 확인 필요' : labels[r.state]) || '미확인',r.state==='stop_breached'?'bad':'neutral')}</td></tr>`;}).join('')||`<tr><td colspan="6">${a.holdings_complete?'조회된 보유 항목 0개':'잔고 조회 미확인·빈 계좌로 단정하지 않음'}</td></tr>`}</tbody></table></div><div class="mini-note">사용자 채택 보호선과 기존 Python 위험 계산을 재사용합니다. 현재 계좌 가격과 과거 완료봉 기준선을 비교한 참고값이며 오늘의 보호선 판정이 아닙니다. 보호선 이탈/결측의 잔여 위험은 0이 아닙니다. 새로운 기준을 채택하지 않습니다.</div>`)+block('미확인 항목과 근거',`<div class="panel-body"><ul class="bullet">${bundle.limitations.concat(bundle.risk?.errors||[]).filter(x=>!x.includes('nh-')||x.includes(alias)).map(x=>`<li>${esc(explain(x))}</li>`).join('')}</ul><p class="chart-caption">계좌 스냅샷 SHA-256 ${esc(bundle.snapshot_sha256)}<br>위험 계산 기준 ${stamp(bundle.risk?.as_of)}</p></div>`);
}
function missing(){ $('#main').innerHTML='<div class="empty"><strong>저장된 항목을 찾을 수 없습니다.</strong><a href="#home">작업대로 돌아가기</a></div>'; }
async function route(){const version=++routeVersion;if(!await WorkbenchWorkspace.closeActive()){history.replaceState(null,'',lastRouteHash||'#home');return;}if(version!==routeVersion)return;lastRouteHash=location.hash;WorkbenchFinancials.close();WorkbenchCharts.clear();const [page,...args]=(location.hash.slice(1).split('?')[0]||'home').split('/').map(decodeURIComponent);document.body.classList.toggle('stock-page',page==='stock');document.querySelectorAll('[data-nav]').forEach(a=>a.classList.toggle('active',a.dataset.nav===page||page==='stock'&&a.dataset.nav==='screen'||page==='account'&&a.dataset.nav==='accounts'||(page==='sector'||page==='focus')&&a.dataset.nav==='sectors'));$('#breadcrumb').textContent=({home:'작업대',screen:'스크리닝',sectors:'섹터 관찰',sector:'섹터 상세',focus:'활동 변화 상세',accounts:'나의 계좌',account:'계좌 상세',stock:'종목 상세'})[page]||'작업대';try{if(page==='home')home();else if(page==='screen')screen(...args);else if(page==='sectors')sectors();else if(page==='sector')sectorDetail(...args);else if(page==='focus')focusDetail(...args);else if(page==='accounts')accounts();else if(page==='account'){accountDetail(...args);WorkbenchAnalysis.account(args[0],$('#main'));}else if(page==='stock')await stockDetail(...args,version);else missing();window.scrollTo(0,0);}catch(e){$('#main').innerHTML='<div class="empty"><strong>화면 자료를 표시하지 못했습니다.</strong>'+esc(e.message)+'<br><a href="#home">작업대로</a></div>';console.error(e);}}
const pp = x => x == null ? '결측' : (x > 0 ? '+' : '') + num(x * 100) + '%p';
const signedPct = x => x == null ? '결측' : (x > 0 ? '+' : '') + pct(x);
function focusLink(s) { return '#focus/'+encodeURIComponent(s.market)+'/'+encodeURIComponent(s.code); }
function focusCard(s) {
 const c=s.current;return `<a class="focus-card ${c.state==='unavailable'?'muted-card':''}" href="${focusLink(s)}"><div class="row"><span class="eyebrow">${esc(s.market)} · ${s.level==='industry'?'세부업종':'대분류'}</span>${tag(c.label,c.state==='building'?'':c.state==='price_weak'?'bad':'neutral')}</div><h3>${esc(s.name)} <span>↗</span></h3><p class="sub">${esc(s.parent||'')} · ${esc(c.date||'기준일 미확인')}</p><div class="focus-numbers"><div><small>거래활동 증가율</small><strong>${signedPct(c.growth)}</strong></div><div><small>증가율의 가속</small><strong>${pp(c.acceleration)}</strong></div></div><p class="focus-evidence">평소 대비 ${num(c.activity_now)}배 · 증가 동참 ${pct(c.participation)}<br>최근 가격 ${signedPct(c.return_recent)} · ${num(c.used,0)}종목 · 활동 계산 ${num(c.activity_used,0)}종목</p><p class="sub">${classificationNote(s.classification)}</p>${c.issues?.length?`<p class="danger-text">${esc(c.issues.join(' · '))}</p>`:''}</a>`;
}
function focusGuide() {
 const c=DATA.market.focus?.settings||{window:5,baseline:20,min_change:.1,min_acceleration:.05,min_participation:.5,min_persistence:3,min_members:5,min_coverage:.8,price_floor:-.005};
 return `<details class="focus-guide"><summary>어떻게 읽나요? · 계산과 상단 노출 기준</summary><div class="guide-grid"><div><strong>① 평소보다 활발한가</strong><p>종목별 하루 거래량 ÷ 그 전 ${c.baseline}일 평균. 업종 대표값은 종목 비율의 중앙값입니다. 1배가 평소 수준입니다.</p></div><div><strong>② 증가 속도가 커졌는가</strong><p>최근 ${c.window}일 평균 ÷ 직전 ${c.window}일 평균 − 1이 증가율입니다. 이 증가율에서 한 구간 전 증가율을 뺀 값이 가속(%p)입니다.</p></div><div><strong>③ 함께 움직이는가</strong><p>거래활동이 늘어난 종목 비율과 지속 일수를 확인합니다. 가격이 하락하면서 거래가 늘면 별도 주의 상태로 표시합니다.</p></div></div><p>상단 기준: 증가율 ${pct(c.min_change)} 이상 · 가속 ${pp(c.min_acceleration)} 이상 · 증가 동참 ${pct(c.min_participation)} 이상 · 최근 ${c.window}일 중 ${c.min_persistence}일 이상 직전 구간 수준 상회 · 시장 내 추정 거래 비중 증가 · 가격 변화 ${pct(c.price_floor)} 이상. 최소 ${c.min_members}종목, 시세·분류 유효 비율 ${pct(c.min_coverage)} 이상입니다.</p><p>세부업종을 먼저, 각 분류 안에서는 가속 → 증가율 순으로 최대 3개를 보여줍니다. 거래 규모·시가총액은 섹터 순위에 쓰지 않습니다. 예: 증가율이 5%→20%면 가속은 +15%p입니다. 평소 아래에서도 개선 중이면 표시할 수 있습니다.</p><p class="sub">이 기준은 수정 가능한 관찰 가정이며 조기 포착·수익성을 검증한 전략이 아닙니다. 거래량과 종가×거래량은 실제 순유입이 아닙니다. 현재 분류로 과거 가격을 묶으므로 당시 구성의 백테스트가 아닙니다.</p></details>`;
}
const classificationDay = value => value ? new Date(value).toLocaleDateString('sv-SE',{timeZone:'Asia/Seoul'}) : '';
function classificationNote(c) {
 if(!c)return '';
 if(c.user_defined&&!c.observed_from)return `사용자 지정 기타 ${num(c.user_defined,0)}종목 · 공식 업종 미확인`;
 return `${c.user_defined?'사용자 지정 기타 '+num(c.user_defined,0)+'종목 포함 · ':''}분류 확인 ${esc(classificationDay(c.observed_from)||'미확인')}${classificationDay(c.observed_until)!==classificationDay(c.observed_from)?' ~ '+esc(classificationDay(c.observed_until)||''):''}${c.source_as_of?' · 분류 기준 '+esc(c.source_as_of):''}${c.refresh_due?' · 갱신 필요 '+num(c.refresh_due,0)+'종목 (기존 분류 사용)':''}`;
}
function focusCoverage(market) {
 const m=DATA.market.focus?.markets?.[market];if(!m)return '계산 자료 미준비';
 return `${market} 분류 ${num(m.matched,0)}/${num(m.target,0)}종목 · ${pct(m.classification_coverage)}${market==='KR'?' · 기존 WICS 대분류':''} · ${classificationNote(m.classification)}`;
}
function focusPanel() {
 return block('관심이 늘어나는 곳',`<div class="panel-body"><div class="chart-toolbar"><select id="focus-home-market" aria-label="관심 업종 시장"><option value="KR">한국</option><option value="US">미국</option></select><span class="sub">거래 규모보다 변화의 속도 · 시장별 최대 3개</span></div><div id="focus-home-results"></div><p class="chart-caption">한국은 기존 WICS 대분류, 미국은 확인된 섹터·세부업종을 관찰합니다. 분류 기준일 또는 근거 확인일이 7일을 넘으면 갱신 필요로 표시하며 기존 분류를 사용합니다.</p>${focusGuide()}</div>`,'<a href="#sectors">전체 섹터·업종 보기 →</a>');
}
function bindHomeFocus() {
 const f=DATA.market.focus,sel=$('#focus-home-market');
 sel.value=f?.markets?.KR?.selected.length?'KR':f?.markets?.US?.selected.length?'US':'KR';
 const render=()=>{const m=sel.value,info=f?.markets?.[m],rows=(f?.items||[]).filter(s=>s.market===m&&info?.selected.includes(s.code));rows.sort((a,b)=>info.selected.indexOf(a.code)-info.selected.indexOf(b.code));$('#focus-home-results').innerHTML=(rows.length?`<div class="focus-card-row">${rows.map(focusCard).join('')}</div>`:`<div class="focus-empty"><strong>${info?.classification_coverage<.8?'분류 자료 부족으로 판단을 보류합니다.':!info?'계산 자료 미준비':'지금 기준을 충족한 섹터가 없습니다.'}</strong><p>전체 목록에서 변화와 미충족 근거를 볼 수 있습니다.</p></div>`)+`<p class="chart-caption">${focusCoverage(m)} · ${info?.classification_coverage<.8?'판정 보류':'조건 충족 '+num(info?.qualifying,0)+'개'}</p>`;};sel.onchange=render;render();
}
function capText(e) {const c=e.display_cap;return c?.value==null?'시총 미확인':money(c.value,c.currency)+' '+c.currency;}
function screenPreview(market) {
 const groups=(DATA.market.screen_groups||[]).filter(g=>g.market===market);
 return `<div class="screen-previews">${groups.map(g=>`<section class="screen-preview"><div class="row"><h3>${esc(g.label)}</h3><a class="text-link" href="#screen/${market}/${g.route}">전체 ${num(g.count,0)}개 →</a></div>${g.top.map(symbol=>{const e=DATA.market.entries.find(e=>e.market===market&&e.symbol===symbol);const sig=e.route_signals?.[g.route]||e.entry_signal;return `<div class="preview-stock"><div class="row">${stockLink(e.market,e.symbol,e.name)}${tag(phase[sig?.phase||e.phase]||'관찰')}</div><div class="preview-cap">${esc(capText(e))}</div><p class="sub">${esc(sig?.label||'저장 근거 확인')}</p></div>`;}).join('')||'<p class="sub">시총이 확인된 후보 없음</p>'}<p class="chart-caption">시총 내림차순 · 결측 ${num(g.missing_cap,0)}개는 전체 목록에서 확인</p></section>`).join('')}</div>`;
}
function home() {
 $('#main').innerHTML=heading('오늘의 투자 작업대','CHANGES BEFORE SIZE.','평소와 달라지는 거래활동부터 살펴보세요.')+`<details class="compact-quality"><summary>${esc(marketStatus())} · 기준일과 수집 상세</summary>${qualityNotice()}</details>`+focusPanel()+block('유형별 스크리닝',`<div class="panel-body"><div class="chart-toolbar"><label for="preview-market">시장</label><select id="preview-market"><option value="KR">한국 · KRW</option><option value="US">미국 · USD</option></select><span class="sub">유형별 시총 상위 3개 · 전체 고유 후보 ${num(DATA.market.candidate_count,0)}개 · 진입 조건 충족 ${num(DATA.market.entry_candidate_count,0)}개</span></div><div id="screen-preview"></div><p class="chart-caption">저장 시총 기준입니다. 유형 간 중복 종목은 각 유형에 남으며, 시총 순서는 매수 우선순위가 아닙니다.</p></div>`,'<a href="#screen">전체 후보 보기 →</a>')+block('나의 계좌',`<div class="panel-body"><div class="account-strip">${DATA.accounts.accounts.map((a,i)=>`<a href="#account/${encodeURIComponent(a.alias)}"><strong>NH 계좌 ${i+1} →</strong><span>보유 ${a.holdings_complete?a.positions.length:'미확인'} · ${state[DATA.accounts.query_state]||'조회 확인 필요'}</span></a>`).join('')||'<span>표시할 계좌 없음</span>'}</div><p class="chart-caption">잔고 기준 ${stamp(DATA.accounts.as_of)} · 총자산·현금은 별도 대조 필요</p></div>`,'<a href="#accounts">조회 상태·위험관리 →</a>');
 bindHomeFocus();const select=$('#preview-market');select.onchange=()=>$('#screen-preview').innerHTML=screenPreview(select.value);select.onchange();
}
function screen(initialMarket='',initialRoute='') {
 $('#main').innerHTML=heading('스크리닝','SCREENING','시총은 시장별로 정렬합니다. 원래의 후보·선별 근거는 보존합니다.')+qualityNotice()+block('저장된 전체 후보',`<div class="filters"><input id="search" aria-label="종목 검색" placeholder="종목명 또는 코드 검색"><select id="market-filter" aria-label="시장"><option value="">전체 시장</option><option>KR</option><option>US</option></select><select id="route-filter" aria-label="스크리닝 유형"><option value="">전체 유형</option><option value="pdf_momentum">PDF 모멘텀</option><option value="sperandeo">스페란데오 S3·S4</option><option value="base_watch">바닥·눌림 횡보</option></select><select id="phase-filter" aria-label="관찰 단계"><option value="">전체 단계</option><option value="watch">관찰 시작</option><option value="ready">돌파 대기</option><option value="entry">진입 조건 충족</option></select><span id="screen-count"></span></div><div id="screen-table"></div><p class="mini-note">시장별 시총 내림차순 · 결측은 각 시장 끝에 표시 · 통화 간 환산 순위 없음. 시총 기준일은 종목 상세에서 확인합니다.</p>`);
 $('#market-filter').value=['KR','US'].includes(initialMarket)?initialMarket:'';$('#route-filter').value=initialRoute;
 const render=()=>{const q=$('#search').value.toLowerCase(),m=$('#market-filter').value,r=$('#route-filter').value,p=$('#phase-filter').value;const rows=DATA.market.entries.filter(e=>(!m||e.market===m)&&(!r||e.origins?.includes(r))&&(!p||(r?e.route_signals?.[r]?.phase:e.phase)===p)&&((e.name||'')+' '+e.symbol).toLowerCase().includes(q));rows.sort((a,b)=>a.market.localeCompare(b.market)||(a.display_cap?.value==null)-(b.display_cap?.value==null)||(b.display_cap?.value||0)-(a.display_cap?.value||0)||a.symbol.localeCompare(b.symbol));$('#screen-count').textContent=rows.length+'종목';$('#screen-table').innerHTML=screeningTable(r?rows.map(e=>({...e,phase:e.route_signals?.[r]?.phase||e.phase,entry_signal:e.route_signals?.[r]||e.entry_signal})):rows);};['#search','#market-filter','#route-filter','#phase-filter'].forEach(id=>$(id).oninput=render);render();
}
function sectors() {
 $('#main').innerHTML=heading('섹터·업종 관찰','ACTIVITY DELTA','평소 대비 활동의 증가율과 가속을 비교합니다.')+qualityNotice()+focusGuide()+block('전체 관찰 목록',`<div class="filters"><select id="focus-market" aria-label="섹터 시장"><option value="">전체 시장</option><option>KR</option><option>US</option></select><select id="focus-level" aria-label="분류 수준"><option value="">대분류·세부업종</option><option value="industry">세부업종</option><option value="sector">대분류</option></select><select id="focus-state" aria-label="관찰 상태"><option value="">전체 상태</option><option value="building">관심 증가 가속</option><option value="price_weak">활동 증가·가격 약세</option><option value="slowing">증가 속도 둔화</option><option value="watch">조건 일부 미충족</option><option value="unavailable">자료 확인 필요</option></select><input id="focus-search" aria-label="업종 검색" placeholder="섹터·업종 이름 검색"><span id="focus-count"></span></div><p class="mini-note">${focusCoverage('KR')}<br>${focusCoverage('US')}</p><div id="focus-list" class="sector-tiles focus-list"></div>`);
 const render=()=>{const rows=(DATA.market.focus?.items||[]).filter(s=>(!$('#focus-market').value||s.market===$('#focus-market').value)&&(!$('#focus-level').value||s.level===$('#focus-level').value)&&(!$('#focus-state').value||s.current.state===$('#focus-state').value)&&(s.name+' '+(s.parent||'')).toLowerCase().includes($('#focus-search').value.toLowerCase()));rows.sort((a,b)=>(a.current.state==='unavailable')-(b.current.state==='unavailable')||(b.current.acceleration??-1e100)-(a.current.acceleration??-1e100)||a.name.localeCompare(b.name));$('#focus-count').textContent=rows.length+'개';$('#focus-list').innerHTML=rows.map(focusCard).join('')||'<div class="empty">해당하는 저장 관찰 결과 없음</div>';};['#focus-market','#focus-level','#focus-state','#focus-search'].forEach(id=>$(id).oninput=render);render();
}
function focusDetail(market,code) {
 const s=DATA.market.focus?.items.find(s=>s.market===market&&s.code===code);if(!s)return missing();const c=s.current;
 $('#main').innerHTML='<a class="back" href="#sectors">← 전체 섹터·업종</a>'+heading(s.name,'ACTIVITY DETAIL',market+' · '+(s.parent||'대분류')+' · '+c.label)+qualityNotice()+block('관찰 근거',`<div class="panel-body"><div class="metrics">${metric('거래활동 증가율',signedPct(c.growth))}${metric('증가율의 가속',pp(c.acceleration))}${metric('평소 대비 활동',num(c.activity_now)+'배')}${metric('활동 증가 동참',pct(c.participation))}</div><p>이전 ${num(c.activity_earlier)}배 → 직전 ${num(c.activity_previous)}배 → 최근 ${num(c.activity_now)}배<br>이전 증가율 ${signedPct(c.previous_growth)} → 최근 증가율 ${signedPct(c.growth)} · 최근 가격 ${signedPct(c.return_recent)}</p><p class="sub">직전 수준을 넘은 날 ${num(c.persistent_days,0)}일 · 시장 내 추정 거래 비중 변화 ${signedPct(c.share_change)} · 상위3종목 거래 비중 ${pct(c.top3_share)}<br>${classificationNote(s.classification)}<br>시세 유효 ${num(c.used,0)} / 분류 ${num(c.matched,0)} · 활동 계산 ${num(c.activity_used,0)}종목 · 최근 구간 ${esc(c.current_start)} ~ ${esc(c.date)}</p>${c.issues?.length?`<div class="notice error">${esc(c.issues.join(' · '))}</div>`:''}${Object.keys(c.activity_exclusions||{}).length?`<details><summary>거래활동 계산 제외 ${Object.keys(c.activity_exclusions).length}종목 · 나머지 유효 자료로 계산</summary><p>비교 구간 내 동일한 구성으로 계산합니다. 설정된 최소 종목 수와 유효 비율을 충족할 때만 판정합니다. 기준은 아래 계산 설명에서 확인할 수 있습니다.</p><ul>${Object.entries(c.activity_exclusions).map(([symbol,reason])=>`<li>${esc(symbol)} · ${reason==='comparison_activity_zero'?'비교 구간 거래활동 0으로 증가율 계산 불가':'평균 거래량 0으로 상대 거래량 계산 불가'}</li>`).join('')}</ul></details>`:''}<div class="focus-checks">${(c.checks||[]).map(k=>`<span>${tag(k.passed===true?'충족':k.passed===false?'미충족':'결측',k.passed===true?'':'neutral')} ${esc(k.label)}</span>`).join('')}</div><div id="focus-activity" class="chart" role="img" aria-label="평소 대비 거래활동 추이"></div><p class="chart-caption">평소 대비 거래활동(배) · 1배 기준 · 날짜별 유효 구성이 바뀔 수 있습니다.</p><div id="focus-delta" class="chart" role="img" aria-label="거래활동 증가율과 가속 추이"></div><p class="chart-caption">증가율(%)과 가속(%p)을 구분해 읽으세요. 실제 순유입 금액을 뜻하지 않습니다.</p>${focusGuide()}</div>`)+block('활동 증가에 기여한 종목',`<div class="scroll"><table><thead><tr><th>종목</th><th>활동 증가율</th><th>평소 대비</th><th>최근 가격</th></tr></thead><tbody>${s.leaders.map(r=>`<tr><td>${stockLink(market,r.symbol,r.name)}</td><td>${signedPct(r.growth)}</td><td>${num(r.activity)}배</td><td>${signedPct(r.return_recent)}</td></tr>`).join('')||'<tr><td colspan="4">유효 종목 자료 없음</td></tr>'}</tbody></table></div>`)+(s.level==='sector'?block('기존 섹터 지표',`<div class="panel-body"><a class="text-link" href="#sector/${encodeURIComponent(market)}/${encodeURIComponent(code)}">가격·상대강도·상태 변화 상세 →</a></div>`):'');
 const usable=(s.history||[]).map(r=>r.state==='unavailable'?{date:r.date}:r);
 WorkbenchCharts.create($('#focus-activity'),[{label:'거래활동 (배)',data:WorkbenchCharts.lineData(usable,'activity_now')},{label:'평소 1배',data:usable.map(r=>({time:r.date,...(r.activity_now==null?{}:{value:1})})),color:'#9da8a1'}]);
 WorkbenchCharts.create($('#focus-delta'),[{label:'증가율 (%)',data:WorkbenchCharts.lineData(usable,'growth',100)},{label:'가속 (%p)',data:WorkbenchCharts.lineData(usable,'acceleration',100),color:'#c39345'}]);
}

fetch('/api/home').then(r=>{if(!r.ok)throw Error('dashboard_load_failed');return r.json();}).then(d=>{DATA=d;window.addEventListener('hashchange',route);route();pollDaily();}).catch(e=>{$('#main').textContent='저장 자료 불러오기 실패: '+e.message;});

$('.skip').onclick = event => { event.preventDefault(); $('#main').focus(); };

async function pollDaily() {
 if(pollDaily.controller)return pollDaily.controller;
 const host=$('#daily-update');
 let latest=null,timer=null,checking=null,submitting=false,applying=false,stopped=false;
 let publishedKey='',offeredKey='',rendered='',statusNote='',statusFailed=false;
 const stages={starting:'저장 자료 준비',calendar:'완료 거래일 확인',inputs:'입력 확인',collection:'종가 자료 수집',nh_fallback:'실패 종목 보완',quality:'자료 검사',stock_screen:'스크리닝 계산',sector_screen:'섹터 계산',priority_review:'후보 선별',charts:'차트 저장',sector_states:'섹터 상태 계산',report:'보고서 저장',finished:'화면 준비',waiting:'다른 실행을 기다리는 중'};
 const errors={market_authentication_http_403_ip_not_allowed:'Toss가 현재 접속 IP를 허용하지 않았습니다(403). Toss Open API 설정에서 허용 IP를 확인해주세요.',market_http_403_ip_not_allowed:'Toss가 현재 접속 IP를 허용하지 않았습니다(403). Toss Open API 설정에서 허용 IP를 확인해주세요.',market_authentication_http_403:'Toss 인증 거절(403). API 접근 확인이 필요합니다.',market_http_403:'Toss가 시장 자료 접근을 거절했습니다(403). Toss Open API 설정에서 허용 IP와 API 접근을 확인해주세요.',market_listing_unavailable:'거래 대상 종목 목록을 받지 못했습니다. 연결 상태를 확인한 뒤 다시 시도해주세요.',previous_launch_interrupted:'이전 실행이 끝나기 전에 중단됐습니다.',market_worker_failed:'시장 자료 수집 작업이 오류로 종료됐습니다. 아래 버튼으로 다시 시도할 수 있습니다.',daily_update_unavailable:'현재 서버에서 갱신을 시작할 수 없습니다. 바탕화면의 투자 대시보드를 다시 열어주세요.',dashboard_preparation_failed:'시장 처리 후 화면 자료를 준비하지 못했습니다. 아래 버튼으로 다시 시도할 수 있습니다.'};
 const busy=u=>!!u&&(!u.view_ready||u.state==='running');
 const key=u=>JSON.stringify([u.attempt_id||u.started_at||u.day,u.revision,u.finished_at,u.display_updated]);
 function schedule(){
  clearTimeout(timer);
  if(!stopped&&!submitting)timer=setTimeout(check,busy(latest)?3000:10000);
 }
 function button(parent,id,label,action,disabled=false){
  const b=document.createElement('button');b.id=id;b.type='button';b.textContent=label;b.disabled=disabled;b.onclick=action;parent.append(b);
 }
 function render(){
  const u=latest,signature=JSON.stringify([u,submitting,applying,publishedKey,statusNote,statusFailed]);
  if(signature===rendered)return;rendered=signature;
  if(u&&!u.enabled&&!statusNote){host.hidden=true;return;}
  const open=host.querySelector?.('details')?.open;
  host.hidden=false;
  const running=busy(u),bad=u&&['failed','interrupted'].includes(u.state);
  host.classList.toggle('error',!!bad||statusFailed);
  if(u?.enabled){
   const label=running?'오늘의 종가·스크리닝 갱신 중':({succeeded:'오늘의 갱신 완료',partial:'오늘의 갱신 완료 · 일부 자료 결측',failed:'오늘의 갱신에 오류가 있습니다',interrupted:'오늘의 갱신이 중단됐습니다'})[u.state]||'오늘의 저장 결과';
   const reason=u.manual_retry?'수동 재시도':u.reused?'오늘 기록 재사용':'하루 첫 실행';
   const ipDenied=['market_authentication_http_403_ip_not_allowed','market_http_403_ip_not_allowed'].includes(u.error);
   const display=u.display_updated?'이후 저장 결과 표시':u.display_stale?'이전 저장 자료 표시':'';
   const p=u.progress;
   host.innerHTML='<details '+((open??(running||bad))?'open':'')+'><summary><strong>'+esc(label)+'</strong> · '+reason+(ipDenied?' · Toss 허용 IP 거절403':u.error==='market_authentication_http_403'?' · Toss 인증403':'')+(display?' · '+display:'')+' · 상세</summary><span>'+esc(u.day)+' · 목표 종가 KR '+esc(u.targets?.KR||'확인 중')+' / US '+esc(u.targets?.US||'확인 중')+'<br>'+(running?esc(stages[u.stage]||'갱신 중')+(p?.requested?' · '+num(p.completed,0)+' / '+num(p.requested,0)+'종목':''):(u.manual_retry?'수동 재시도 처리':u.reused?'오늘 실행 기록 재사용 · 수집·스크리닝 재실행 없음':'하루 첫 실행 처리'))+(u.finished_at?' · '+stamp(u.finished_at):'')+(u.error?'<br>'+esc(errors[u.error]||'갱신 중 오류가 발생했습니다. 아래 재시도 버튼과 실제 자료 기준일을 확인하세요.'):'')+(u.display_updated?'<br>갱신 오류 이후에 저장된 결과를 표시합니다. 위 오류는 당시 실행 기록이며 아래 실제 자료 기준일을 확인하세요.':u.display_stale?'<br>갱신 전 저장 결과를 표시합니다. 아래 실제 자료 기준일을 확인하세요.':'')+(bad||u.state==='partial'?'<br>같은 날에는 자동으로 재시도하지 않습니다.':'')+'</span></details>';
  }else host.innerHTML='<strong>갱신 상태 확인 필요</strong>';
  const actions=document.createElement('div');actions.className='daily-actions';
  if(u?.enabled&&(u.retry_available||running||submitting))button(actions,'daily-retry',submitting?'재시도 요청 중…':running?'갱신 진행 중…':'오늘의 갱신 재시도',retry,running||submitting||statusFailed);
  if(u?.enabled&&u.view_ready&&!running&&key(u)!==publishedKey)button(actions,'daily-result',applying?'결과 여는 중…':'갱신 결과 열기',()=>apply(key(latest)),applying||submitting);
  if(statusNote){const note=document.createElement('span');note.className='daily-message';note.setAttribute?.('role','status');note.textContent=statusNote;actions.append(note);}
  if(statusFailed)button(actions,'daily-status-retry','상태 다시 확인',check,submitting);
  host.append(actions);
 }
 async function apply(expected){
  if(applying||busy(latest)||!latest?.view_ready||key(latest)!==expected)return;
  applying=true;offeredKey=expected;render();
  try{
   const r=await fetch('/api/home');if(!r.ok)throw Error();
   const next=await r.json();
   if(busy(latest)||key(latest)!==expected||!await WorkbenchWorkspace.closeActive())return;
   DATA=next;publishedKey=expected;await route();
  }catch{statusNote='갱신 결과를 열지 못했습니다. 현재 화면은 유지됩니다. 결과 열기를 다시 눌러주세요.';}
  finally{applying=false;render();}
 }
 async function accept(u){
  latest=u;render();
  if(u.enabled&&u.view_ready&&!busy(u)&&key(u)!==publishedKey&&key(u)!==offeredKey&&(!location.hash||location.hash==='#home'))await apply(key(u));
 }
 async function check(){
  if(stopped||submitting)return;
  if(checking)return checking;
  clearTimeout(timer);
  checking=(async()=>{
   try{
    const response=await fetch('/api/daily-update');if(!response.ok)throw Error();
    const u=await response.json();
    if(statusFailed){if(statusNote==='갱신 상태를 확인하지 못했습니다. 현재 화면과 마지막 확인 상태를 유지합니다.')statusNote='';statusFailed=false;}
    await accept(u);
   }catch{
    statusFailed=true;statusNote='갱신 상태를 확인하지 못했습니다. 현재 화면과 마지막 확인 상태를 유지합니다.';render();
   }
  })();
  try{await checking;}finally{checking=null;schedule();}
 }
 async function retry(){
  if(submitting||busy(latest)||!latest?.retry_available||statusFailed)return;
  const attempt=latest.attempt_id||latest.started_at;
  submitting=true;statusNote='';clearTimeout(timer);render();
  let refresh=false;
  try{
   // Let an already-running status read finish before accepting the new attempt.
   if(checking)await checking;
   clearTimeout(timer);
   const health=await fetch('/health');if(!health.ok)throw Error('daily_update_unavailable');
   const {token}=await health.json();if(!token)throw Error('daily_update_unavailable');
   const response=await fetch('/api/daily-update',{method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Token':token},body:JSON.stringify({retry:true,attempt_id:attempt})});
   const u=await response.json();
   if(response.status===409){statusNote='다른 창에서 갱신 상태가 바뀌었습니다. 최신 상태를 확인합니다.';refresh=true;}
   else if(!response.ok)throw Error(u.error||'daily_update_unavailable');
   else{statusFailed=false;await accept(u);}
  }catch(e){
   statusNote=errors[e.message==='daily_retry_unavailable'?'daily_update_unavailable':e.message]||'재시도 요청 결과를 확인하지 못했습니다. 상태를 확인한 뒤 다시 시도하세요.';
   statusFailed=true;refresh=true;
  }finally{
   submitting=false;render();
   if(refresh)await check();else schedule();
  }
 }
 pollDaily.controller={refresh:check,stop(){stopped=true;clearTimeout(timer);}};
 await check();
 return pollDaily.controller;
}
