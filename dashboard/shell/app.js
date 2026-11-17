'use strict';
const $ = (s) => document.querySelector(s);
const esc = (x) => String(x ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = (x, digits = 2) => x == null || x === '' ? '미확인' : Number(x).toLocaleString('ko-KR', {maximumFractionDigits:digits});
const money = WorkbenchFormat.money;
const pct = x => x == null ? '결측' : num(x * 100) + '%';
const stamp = x => x ? new Date(x).toLocaleString('ko-KR', {timeZone:'Asia/Seoul',hour12:false}) + ' KST' : '미확인';
const state = {received:'조회 성공',partial:'일부 결측',failed:'조회 실패',not_requested:'저장값 사용',unavailable:'미확인'};
const tag = (text, cls = '') => `<span class="tag ${cls}">${esc(text)}</span>`;
const stockLink = (market, symbol, name) => `<a href="#stock/${encodeURIComponent(market)}/${encodeURIComponent(symbol)}"><span class="stock-name">${esc(name || symbol)}</span><span class="stock-code">${esc(market)} · ${esc(symbol)}</span></a>`;
const block = (title, body, link = '') => `<section class="panel"><div class="panel-head"><h2>${title}</h2>${link}</div>${body}</section>`;
const explain = x => ({protection_uses_saved_completed_bars_check_each_price_date:'보호 기준선은 과거 저장 봉으로 계산했습니다. 종목별 봉 기준일과 판정일을 확인하세요.',KR_US_scope_not_all_assets:'국내·미국 주식 범위이며 전체 금융자산이 아닙니다.',cash_and_liabilities_not_reconciled:'현금·부채 대조 미완료',working_orders_not_reconciled:'미체결·예약은 이번 조회 범위에 포함하지 않습니다.',adopted_protection_not_applicable_to_this_snapshot:'채택 보호선과 현재 잔고의 대응을 확인하지 못했습니다.',risk_calculation_unavailable:'위험 계산에 필요한 자료가 부족합니다.'})[x] || (x.includes('cash_inventory_incomplete') || x.includes('gross_cash_balance_unavailable') ? '계좌 현금 구성·총액 미확인' : x);
const metric = (label, value) => `<div class="metric"><small>${WorkbenchHelp.text(label)}</small><strong>${esc(value)}</strong></div>`;
let DATA, routeVersion = 0, lastRouteHash = location.hash;
function heading(title, eyebrow, subtitle) { return `<div class="heading"><div><div class="eyebrow">${eyebrow}</div><h1>${esc(title)}</h1><p class="sub">${esc(subtitle)}</p></div><div class="date-box">자료 기준 · 완료 일봉<strong>KR ${esc(DATA.market.session_dates?.KR || '미확인')}<br>US ${esc(DATA.market.session_dates?.US || '미확인')}</strong></div></div>`; }
// Global stock lookup stays mounted while the page route changes.
function mountStockSearch() {
 const host = document.getElementById('stock-search');
 if (!host || host.dataset.mounted) return;
 host.dataset.mounted = 'true';
 const input = host.querySelector('input'), clear = host.querySelector('button');
 const panel = host.querySelector('.stock-search-panel'), list = host.querySelector('[role="listbox"]');
 const status = host.querySelector('[role="status"]'), note = host.querySelector('.stock-search-note');
 let items = [], active = -1, sequence = 0, timer, controller, composing = false;
 const query = () => input.value.trim();
 function cancel() { clearTimeout(timer); controller?.abort(); sequence++; }
 function close() {
  cancel(); panel.hidden = true; input.setAttribute('aria-expanded', 'false');
  input.removeAttribute('aria-activedescendant'); active = -1; items = []; list.replaceChildren();
 }
 function show(message) {
  panel.hidden = false; input.setAttribute('aria-expanded', 'true');
  status.textContent = message;
 }
 function select(index) {
  const item = items[index]; if (!item) return;
  close(); input.value = ''; clear.hidden = true; input.blur();
  location.hash = '#stock/' + encodeURIComponent(item.market) + '/' + encodeURIComponent(item.symbol);
 }
 function highlight(value, needle) {
  const text = String(value ?? ''), start = text.toLocaleLowerCase().indexOf(needle.toLocaleLowerCase());
  return start < 0 ? esc(text) : esc(text.slice(0, start)) + '<mark>' + esc(text.slice(start, start + needle.length)) + '</mark>' + esc(text.slice(start + needle.length));
 }
 function activate(index, scroll = false) {
  active = index;
  Array.from(list.children).forEach((row, i) => row.setAttribute('aria-selected', String(i === active)));
  const row = list.children[active];
  if (row) { input.setAttribute('aria-activedescendant', row.id); if (scroll) row.scrollIntoView({block:'nearest'}); }
  else input.removeAttribute('aria-activedescendant');
 }
 async function search(value, request) {
  controller = new AbortController();
  try {
   const response = await fetch('/api/stock-search?q=' + encodeURIComponent(value), {signal:controller.signal});
   if (!response.ok) throw Error('stock_search_failed');
   const result = await response.json();
   if (request !== sequence || value !== query() || document.activeElement !== input) return;
   items = result.items; list.replaceChildren();
   items.forEach((item, i) => {
    const row = document.createElement('div');
    row.id = 'stock-search-option-' + i; row.className = 'stock-search-option';
    row.setAttribute('role', 'option'); row.setAttribute('aria-selected', 'false');
    row.innerHTML = '<span class="stock-search-identity"><strong>' + highlight(item.name || item.symbol, value) + '</strong><span>' + highlight(item.symbol.replace(/\.(KS|KQ)$/, ''), value) + '</span></span><span class="stock-search-market">' + esc(item.exchange || item.market) + '<small>' + (item.market === 'KR' ? '한국' : '미국') + '</small></span>';
    row.addEventListener('pointerdown', event => event.preventDefault());
    row.addEventListener('click', () => select(i));
    row.addEventListener('pointermove', () => activate(i));
    list.append(row);
   });
   show(items.length ? '검색 결과 ' + result.total + '개' : '일치하는 종목이 없습니다. 종목명이나 티커를 확인해 주세요.');
   status.classList.toggle('stock-search-sr', items.length > 0);
   note.textContent = items.length ? '저장된 종목 · ' + items.length + (result.total > items.length ? ' / ' + result.total + '개 · 더 입력하면 좁혀집니다' : '개') + ' · ↑↓ 선택 · Enter 열기' : '저장된 한국·미국 종목에서 검색합니다';
   activate(items.length ? 0 : -1);
  } catch (error) {
   if (request !== sequence || error.name === 'AbortError') return;
   items = []; list.replaceChildren(); input.removeAttribute('aria-activedescendant');
   status.classList.remove('stock-search-sr');
   show('검색을 불러오지 못했습니다. 잠시 후 다시 입력해 주세요.');
   note.textContent = '연결이 끊겼다면 대시보드를 다시 열어 주세요';
  }
 }
 function schedule() {
  cancel(); items = []; active = -1; list.replaceChildren(); input.removeAttribute('aria-activedescendant');
  clear.hidden = !input.value;
  if (!query()) { close(); return; }
  status.classList.remove('stock-search-sr'); show('검색 중…'); note.textContent = '저장된 한국·미국 종목에서 검색합니다';
  const request = sequence, value = query();
  timer = setTimeout(() => search(value, request), 150);
 }
 input.disabled = false;
 input.addEventListener('input', () => { clear.hidden = !input.value; if (!composing) schedule(); });
 input.addEventListener('compositionstart', () => { composing = true; close(); });
 input.addEventListener('compositionend', () => { composing = false; schedule(); });
 input.addEventListener('focus', () => { if (query()) schedule(); });
 input.addEventListener('keydown', event => {
  if (composing || event.isComposing || event.keyCode === 229) return;
  if (event.key === 'Escape') { event.preventDefault(); close(); return; }
  if (event.key === 'Tab') { close(); return; }
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
   event.preventDefault();
   if (panel.hidden) { if (query()) schedule(); return; }
   if (items.length) activate((active + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length, true);
  }
  if (event.key === 'Enter') { event.preventDefault(); if (!panel.hidden && active >= 0) select(active); }
 });
 clear.addEventListener('click', () => { input.value = ''; clear.hidden = true; close(); input.focus(); });
 host.addEventListener('focusout', event => { if (!host.contains(event.relatedTarget)) close(); });
 document.addEventListener('pointerdown', event => { if (!host.contains(event.target)) close(); });
 window.addEventListener('hashchange', close);
}
function marketStatus() {
 const m=DATA.market;
 const data=({succeeded:'확보 완료',complete:'확보 완료',partial:'일부 결측',failed:'수집 실패',unavailable:'미준비'})[m.quality?.state]||'미확인';
 const execution=m.state==='failed'?'실패':({completed:'완료',completed_with_exceptions:'완료 · 자료 예외',failed:'실패'})[m.processing_state]||({partial:'일부 미완료',succeeded:'완료',unavailable:'미준비'})[m.state]||'미확인';
 return `자료 ${data} · 실행 ${execution}`;
}
function qualityNotice() {
  const m = DATA.market, q = m.quality || {};
  const gaps=Object.entries({price_history:'가격 이력 주의',market_cap:'시총 미확보'}).filter(([key])=>m.quality_gaps?.[key]>0).map(([key,label])=>label+' '+num(m.quality_gaps[key],0)+'종목').join(' · ');
  return `<div class="notice ${m.state === 'unavailable' ? 'error' : ''}"><strong>${esc(marketStatus())}</strong><span>수신 ${num(q.received,0)} / ${num(q.requested,0)} · 실패 ${num(q.failed,0)} · 재계산 ${stamp(m.finished_at)}${gaps?'<br>'+esc(gaps)+' · 해당 종목의 계산·분류가 제한됩니다.':''}<br>수집 ${stamp(m.data_times?.fetched_at)} → ${stamp(m.data_times?.collected_until)}${m.errors?.length ? '<br>' + esc(m.errors.join(' / ')) : ''}${DATA.daily_update?.saved_only?'<br>시장 최신화 생략 · '+(DATA.daily_update.display_stale?'이전 검증 가능 저장 자료 표시 · ':'')+'최근 시장 실행 상태 '+esc(({partial:'완료 · 일부 자료 결측',succeeded:'완료',failed:'실패',running:'진행 중',interrupted:'중단',unavailable:'미준비'})[DATA.daily_update.latest_state]||'미확인')+' · 장기 차트 기준일과 구분하세요.':''}</span></div>`;
}
function accountCards() {
 const a=DATA.accounts;
 return `<div class="panel-body"><p class="sub">${tag(state[a.query_state] || a.query_state,a.query_state==='failed'?'bad':'neutral')} ${a.source_mode==='historical_saved'?tag(a.stale?'과거 저장값':'저장된 실조회',a.stale?'warn':'neutral'):tag('읽기 전용')} ${a.stale?tag('24시간 경과','warn'):''}</p><p class="chart-caption">잔고 기준 ${stamp(a.as_of)}</p>${a.accounts.map((x,i)=>`<a class="account-card" href="#account/${encodeURIComponent(x.alias)}"><div class="row"><div><span class="account-title">${esc(x.provider==='meritz'||x.alias.startsWith('meritz-')?'메리츠':'NH')} 계좌 ${i+1}</span><div class="account-id">${esc(x.alias)}</div></div><span class="account-count">${x.holdings_complete?x.positions.filter(WorkbenchRisk.isEquity).length:'—'}<small>보유 주식 →</small></span></div><div class="account-summary">${esc(state[x.query_state]||x.query_state||'')} · ${x.stale?'24시간 경과 · ':''}잔고 기준 ${stamp(x.as_of||a.as_of)} · ${x.holdings_complete?'국내·미국 잔고 확인':'잔고 일부 미확인'} · 현금성 자산 ${x.cash_complete?'확인':'대조 필요'}</div></a>`).join('') || '<div class="empty">표시할 계좌가 없습니다.</div>'}${a.reason?`<p class="danger-text">조회 오류: ${esc(a.reason)}</p>`:''}<p class="chart-caption">계좌를 선택하면 보유 내역과 관측 예수금을 확인할 수 있습니다.</p></div>`;
}
async function stockDetail(market, symbol, version) {
 $('#main').innerHTML='<div class="loading">저장 가격 자료를 여는 중…</div>';
 const response=await fetch('/api/stock/'+encodeURIComponent(market+':'+symbol));if(!response.ok)throw Error('price_request_failed');const s=await response.json();if(version!==routeVersion)return;
 const currency=s.currency || ({KR:'KRW',US:'USD'})[market];
 const bars=s.bars||[], last=bars.at(-1);
 $('#main').innerHTML=`<div class="stock-detail"><header class="stock-heading"><div class="stock-identity"><a class="back" href="#home">← 작업대</a><div class="stock-title"><h1>${esc(symbol.replace(/\.(KS|KQ)$/,''))}</h1><h2>${esc(s.name||symbol)}</h2></div><p class="sub">${esc(market)} / ${esc(symbol)}</p></div><div class="stock-quote"><strong id="stock-quote-price">${money(last?.close,currency)}</strong><span>${esc(currency)} · <span id="stock-quote-date">저장 가격 ${esc(last?.date||'미확인')}</span></span></div></header><nav class="stock-section-nav" aria-label="종목 상세 영역"><button data-section="stock-price" class="selected">차트</button><button data-section="financials">재무정보</button><button data-section="qualitative-report">정성 리포트</button><span>저장 자료 · ${esc(currency)}</span></nav><details class="stock-data-status"><summary>${esc(marketStatus())} · 기준일과 수집 내역</summary>${qualityNotice()}</details><section class="stock-chart-panel" id="stock-price" aria-label="가격과 거래량"><div class="stock-chart-toolbar"><button id="stock-tools-toggle" aria-expanded="false" aria-controls="chart-workspace"><span aria-hidden="true">✎</span> 작도 · SMA</button><div class="chart-toolbar" id="timeframe-controls" aria-label="봉 주기"><button data-frame="1d" class="selected">일봉</button><button data-frame="1w">주봉</button><button data-frame="1mo">월봉</button></div><div class="chart-toolbar" id="history-range-controls" aria-label="차트 기간"><button data-range="3">3개월</button><button data-range="6" class="selected">6개월</button><button data-range="12">1년</button><button data-range="60">5년</button><button data-range="all">전체 이력</button></div><button id="stock-chart-expand" aria-pressed="false"><span aria-hidden="true">⛶</span> 차트 확대</button></div><div id="chart-workspace" hidden></div><div class="stock-plot-wrap"><div id="price-chart" class="chart large" role="img" aria-label="저장 OHLC 가격과 거래량 차트"></div><div id="price-legend" class="chart-legend stock-price-legend">차트 위에서 날짜별 수치를 확인하세요.</div></div><div class="stock-chart-foot"><p id="history-price" class="chart-caption"></p><details class="history-info"><summary>가격 이력 · 품질 · 기준일</summary><p id="history-status" role="status" class="chart-caption">장기 가격 이력 준비 중…</p><details id="history-quality" class="chart-caption" hidden><summary>자료 품질 상세</summary><p></p></details><p class="chart-caption">저장 원본 ${bars.length}개 일봉 · 수집 ${stamp(s.collected_at)} · 거래량은 공급자 조정 기준.</p></details><button id="history-retry" class="history-retry" hidden>장기 이력 재시도</button></div></section>`+WorkbenchFinancials.shell()+`<div class="placeholder-grid" id="qualitative-report">${block('정성 리포트',`<div class="empty">${tag('미준비','neutral')}<strong>연결된 검증 리포트가 없습니다.</strong>사업·촉매·위험·반증 근거<br>공시·IR 인용과 사용자 판단을 분리해 연결합니다.</div>`)}</div></div>`;
 document.querySelectorAll('[data-section]').forEach(b=>b.onclick=()=>{document.getElementById(b.dataset.section)?.scrollIntoView({behavior:'smooth',block:'start'});document.querySelectorAll('[data-section]').forEach(x=>x.classList.toggle('selected',x===b));});
 $('#stock-tools-toggle').onclick=()=>{const host=$('#chart-workspace');host.hidden=!host.hidden;$('#stock-tools-toggle').setAttribute('aria-expanded',String(!host.hidden));};
 $('#stock-chart-expand').onclick=()=>{const panel=$('#stock-price'),expanded=panel.classList.toggle('expanded');$('#stock-chart-expand').setAttribute('aria-pressed',String(expanded));$('#stock-chart-expand').innerHTML=expanded?'⛶ 기본 크기':'⛶ 차트 확대';};
 WorkbenchFinancials.mount({market,symbol,isCurrent:()=>version===routeVersion});
 const analysis=await WorkbenchAnalysis.initial({market,symbol});
 if(analysis&&analysis.case.stock_key!==market+':'+symbol)throw Error('분석건 종목이 일치하지 않습니다.');
 const controller=await WorkbenchStockChart.mount({stock:s,market,symbol,currency,isCurrent:()=>version===routeVersion,context:analysis?.case.context});
 if(version===routeVersion)await WorkbenchAnalysis.mount({market,symbol,currency,controller,isCurrent:()=>version===routeVersion,initialDetail:analysis});
 if(version===routeVersion&&window.WorkbenchScenarios)await WorkbenchScenarios.mountStock({market,symbol,currency,controller,isCurrent:()=>version===routeVersion});
}
function accountWorkspace(title='나의 계좌',withAccounts=true) {return `<div id="risk-account" data-title="${esc(title)}">${block(title,withAccounts?accountCards():'<div class="panel-body">저장된 계획을 확인합니다.</div>')}</div>`;}
function accounts() {$('#main').innerHTML=heading('나의 계좌','READ-ONLY ACCOUNTS','NH·메리츠의 저장된 국내·미국 잔고를 확인합니다.')+accountWorkspace('계좌 정보와 계획')+`<div class="notice"><span>조회 시도 ${stamp(DATA.accounts.attempted_at)} · 갱신하려면 일일 갱신을 실행하거나 Codex에서 계좌 갱신을 요청하세요. 브라우저 새로고침은 저장된 화면만 다시 엽니다.</span></div>`;}
function accountQuantity(value) {
 const m=/^(\d+)(?:\.(\d+))?$/.exec(String(value??''));
 if(!m)return '미확인';
 const fraction=(m[2]||'').replace(/0+$/,'');
 return m[1].replace(/\B(?=(\d{3})+(?!\d))/g,',')+(fraction?'.'+fraction:'');
}
function accountDetail(alias){
 const a=DATA.accounts.accounts.find(a=>a.alias===alias);if(!a)return missing();
 $('#main').innerHTML='<a class="back" href="#accounts">← 나의 계좌</a>'+heading('계좌 상세','ACCOUNT / READ ONLY',alias+' · 잔고 기준 '+stamp(a.as_of||DATA.accounts.as_of))+accountWorkspace('계좌 자산 구성',false)+block('보유 주식',`<div class="scroll"><table><thead><tr><th>종목</th><th>수량</th><th>관측 가격</th></tr></thead><tbody>${a.positions.filter(WorkbenchRisk.isEquity).map(p=>`<tr><td>${stockLink(p.market,p.quote_symbol||p.symbol,p.name)}</td><td>${accountQuantity(p.quantity)}</td><td>${money(p.price,p.currency)} ${esc(p.currency)}</td></tr>`).join('')}</tbody></table></div>`);
}
async function route(){const version=++routeVersion;if(!await WorkbenchWorkspace.closeActive()){history.replaceState(null,'',lastRouteHash||'#home');return;}if(version!==routeVersion)return;lastRouteHash=location.hash;WorkbenchFinancials.close();WorkbenchCharts.clear();const [page,...args]=(location.hash.slice(1).split('?')[0]||'home').split('/').map(decodeURIComponent);document.body.classList.toggle('stock-page',page==='stock');document.querySelectorAll('[data-nav]').forEach(a=>a.classList.toggle('active',a.dataset.nav===page||page==='account'&&a.dataset.nav==='accounts'));$('#breadcrumb').textContent=({home:'작업대',risk:'가격 위험 경고',accounts:'나의 계좌',account:'계좌 상세',stock:'종목 상세'})[page]||'작업대';try{if(page==='home')home();else if(page==='risk')await WorkbenchTrailing.mount($('#main'));else if(page==='accounts')accounts();else if(page==='account'){accountDetail(...args);WorkbenchAnalysis.account(args[0],$('#main'));}else if(page==='stock')await stockDetail(...args,version);else missing();if(['home','accounts','account'].includes(page)&&window.WorkbenchRisk){const host=$('#risk-account');if(host)WorkbenchRisk.mount(host,{accountAlias:page==='account'?args[0]:null,title:host.dataset.title,accountHTML:page==='account'?'':accountCards(),showAccountLink:page==='home',accountBundle:DATA.accounts});}window.scrollTo(0,0);}catch(e){$('#main').innerHTML='<div class="empty"><strong>화면 자료를 표시하지 못했습니다.</strong>'+esc(e.message)+'<br><a href="#home">작업대로</a></div>';console.error(e);}}
function home() {
 $('#main').innerHTML=heading('오늘의 투자 작업대','','')+`<details class="compact-quality"><summary>${esc(marketStatus())} · 기준일과 수집 상세</summary>${qualityNotice()}</details>`+accountWorkspace();
}

fetch('/api/home').then(r=>{if(!r.ok)throw Error('dashboard_load_failed');return r.json();}).then(d=>{DATA=d;mountStockSearch();window.addEventListener('hashchange',route);route();pollDaily();WorkbenchExclusions.mount($('#market-exclusions'),async()=>{if(!await WorkbenchWorkspace.closeActive())return false;const r=await fetch('/api/home');if(!r.ok)throw Error('home_unavailable');DATA=await r.json();await route();return true;});}).catch(e=>{$('#main').textContent='저장 자료 불러오기 실패: '+e.message;});

$('.skip').onclick = event => { event.preventDefault(); $('#main').focus(); };

async function pollDaily() {
 if(pollDaily.controller)return pollDaily.controller;
 const host=$('#daily-update');
 let latest=null,timer=null,checking=null,submitting=false,applying=false,stopped=false;
 let publishedKey='',offeredKey='',rendered='',statusNote='',statusFailed=false;
 const stages={starting:'저장 자료 준비',accounts:'계좌 잔고 갱신',calendar:'완료 거래일 확인',inputs:'입력 확인',collection:'종가 자료 수집',adr_metadata:'ADR 시총·업종 보강',nh_fallback:'실패 종목 보완',quality:'자료 검사',report:'보고서 저장',finished:'화면 준비',waiting:'다른 실행을 기다리는 중'};
 const errors={basis_document_changed_review_required:'실행 당시 Toss 공식 문서 변경이 감지되어 시장 갱신을 중단했습니다. 문서 변경 검토가 반영된 뒤 다시 시도할 수 있습니다.',basis_document_refresh_unavailable:'Toss 공식 기준 문서를 확인하지 못해 시장 갱신을 중단했습니다. 연결 상태를 확인한 뒤 다시 시도해주세요.',basis_document_not_current:'Toss 가격·거래량 기준 문서의 최신 확인이 필요합니다.',market_authentication_http_403_ip_not_allowed:'Toss가 현재 접속 IP를 허용하지 않았습니다(403). Toss Open API 설정에서 허용 IP를 확인해주세요.',market_http_403_ip_not_allowed:'Toss가 현재 접속 IP를 허용하지 않았습니다(403). Toss Open API 설정에서 허용 IP를 확인해주세요.',market_authentication_http_403:'Toss 인증 거절(403). API 접근 확인이 필요합니다.',market_http_403:'Toss가 시장 자료 접근을 거절했습니다(403). Toss Open API 설정에서 허용 IP와 API 접근을 확인해주세요.',market_listing_unavailable:'거래 대상 종목 목록을 받지 못했습니다. 연결 상태를 확인한 뒤 다시 시도해주세요.',previous_launch_interrupted:'이전 실행이 끝나기 전에 중단됐습니다.',market_worker_failed:'시장 자료 수집 작업이 오류로 종료됐습니다. 아래 버튼으로 다시 시도할 수 있습니다.',daily_update_unavailable:'현재 서버에서 갱신을 시작할 수 없습니다. 바탕화면의 투자 대시보드를 다시 열어주세요.',dashboard_preparation_failed:'시장 처리 후 화면 자료를 준비하지 못했습니다. 아래 버튼으로 다시 시도할 수 있습니다.'};
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
  const running=u?.state==='running',preparing=!!u&&!u.view_ready&&!running,locked=busy(u),bad=u&&(['failed','interrupted'].includes(u.state)||['failed','partial'].includes(u.account_state));
  host.classList.toggle('error',!!bad||statusFailed);
  if(u?.enabled){
   const label=u.observe_only&&!running?'저장 결과 · 새 수집 결과 자동 반영':running?'오늘의 종가·계좌 갱신 중':({succeeded:'오늘의 갱신 완료',partial:'오늘의 갱신 완료 · 일부 자료 결측',failed:'오늘의 갱신에 오류가 있습니다',interrupted:'오늘의 갱신이 중단됐습니다'})[u.state]||'오늘의 저장 결과';
   const reason=u.observe_only?'저장 자료 조회':u.manual_retry?'수동 재시도':u.reused?'오늘 기록 재사용':'하루 첫 실행';
   const ipDenied=['market_authentication_http_403_ip_not_allowed','market_http_403_ip_not_allowed'].includes(u.error);
   const display=u.display_updated?'이후 저장 결과 표시':u.display_stale?'이전 저장 자료 표시':'';
   const p=u.progress;
   const accountError=u.account_error ? (/IGW40043|IGW40044/.test(u.account_error)?'NH 인증 복구 후에도 토큰이 거절됐습니다.':/http_400/.test(u.account_error)?'NH가 계좌 요청을 거절했습니다.':'계좌 갱신을 완료하지 못했습니다.')+' ('+u.account_error+')' : '';
   host.innerHTML='<details '+((open??(running||bad))?'open':'')+'><summary><strong>'+esc(label)+'</strong> · '+reason+(u.account_attempted?' · 계좌 '+({received:'갱신 완료',partial:'일부 조회 실패',failed:'조회 실패'}[u.account_state]||'확인 중'):'')+(ipDenied?' · Toss 허용 IP 거절403':u.error==='market_authentication_http_403'?' · Toss 인증403':'')+(display?' · '+display:'')+' · 상세</summary><span>'+esc(u.day)+' · 목표 종가 KR '+esc(u.targets?.KR||'확인 중')+' / US '+esc(u.targets?.US||'확인 중')+'<br>'+(preparing?'저장 화면 준비 중 · 시장 처리는 종료됨':running?esc(stages[u.stage]||'갱신 중')+(p?.requested?' · '+num(p.completed,0)+' / '+num(p.requested,0)+'종목':''):(u.manual_retry?'수동 재시도 처리':u.reused?'오늘 실행 기록 재사용 · 수집 재실행 없음':'하루 첫 실행 처리'))+(u.finished_at?' · '+stamp(u.finished_at):'')+(u.error?'<br>'+esc(errors[u.error]||'갱신 중 오류가 발생했습니다. 아래 재시도 버튼과 실제 자료 기준일을 확인하세요.'):'')+(accountError?'<br>'+esc(accountError):'')+(u.display_updated?'<br>갱신 오류 이후에 저장된 결과를 표시합니다. 위 오류는 당시 실행 기록이며 아래 실제 자료 기준일을 확인하세요.':u.display_stale?'<br>갱신 전 저장 결과를 표시합니다. 아래 실제 자료 기준일을 확인하세요.':'')+(bad||u.state==='partial'?'<br>같은 날에는 자동으로 재시도하지 않습니다.':'')+'</span></details>';
  }else host.innerHTML='<strong>갱신 상태 확인 필요</strong>';
  const actions=document.createElement('div');actions.className='daily-actions';
  if(u?.enabled&&!u.observe_only&&(u.retry_available||locked||submitting))button(actions,'daily-retry',submitting?'재시도 요청 중…':running?'갱신 진행 중…':preparing?'화면 준비 중…':'오늘의 갱신 재시도',retry,locked||submitting||statusFailed);
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
  if(u.enabled&&u.view_ready&&!busy(u)&&key(u)!==publishedKey&&key(u)!==offeredKey&&WorkbenchWorkspace.current?.()?.canAutoRefresh?.()!==false&&!document.activeElement?.matches?.('input,textarea,select,[contenteditable=true]'))await apply(key(u));
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
