'use strict';
const $=id=>document.getElementById(id);
let token='', nextRoom=null, nextNews=null, lastSearch=null, busy=false;
const states={not_connected:'아직 연결되지 않았습니다.',code_required:'텔레그램에서 받은 인증 코드를 입력하세요.',password_required:'2단계 인증 비밀번호를 입력하세요.',connected:'연결되었습니다. 이제 종목분석 스킬이 로컬 MCP로 방과 뉴스를 직접 조회합니다. 이 창은 닫아도 됩니다.'};
const errors={invalid_code:'인증 코드를 확인하세요.',invalid_password:'비밀번호를 확인하세요.',invalid_phone:'전화번호를 국가번호 포함 형식으로 입력하세요.',invalid_api_credentials:'API ID와 Hash를 확인하세요.',code_expired_restart_login:'코드가 만료되었습니다. 연결 종료 후 다시 로그인하세요.',telegram_rate_limited:'텔레그램 요청 제한입니다. 안내된 시간 이후 다시 시도하세요.',dependency_missing:'검색 라이브러리 설치가 필요합니다.',authentication_required:'먼저 로그인하세요.',telegram_request_failed:'텔레그램 요청이 실패했습니다. 연결 상태와 입력을 확인하세요.',session_expired:'접속이 만료되었습니다. 연결 종료 후 다시 로그인하세요.',invalid_dates:'날짜 순서와 검색 기간(최대 367일)을 확인하세요.'};
function renderState(r){
  if(r.state){$('restore-session').hidden=!(r.state==='not_connected'&&r.saved_session_available);$('persist-session').hidden=!(r.state==='connected'&&!r.session_saved&&r.session_persistence);$('session-note').textContent=r.session_saved?'로그인 세션 암호화 저장 완료 · 재시작 후 자동 연결':r.restore_error?'저장된 연결을 복원하지 못했습니다. 네트워크를 확인하고 다시 시도하세요. 세션 무효일 때만 로그아웃 후 재인증하세요.':r.state==='connected'&&r.session_persistence?'로그인은 완료됐지만 세션 저장이 필요합니다. 저장 다시 시도를 눌러 주세요.':'';}

  if(typeof r.api_credentials_saved==='boolean'){$('saved-note').textContent=r.api_credentials_saved?'API ID·Hash가 이 PC에 암호화 저장되어 있습니다. 아래 전화번호로 로그인하세요.':'API ID·Hash를 입력하고 암호화 저장하세요.';$('credentials-form').hidden=r.api_credentials_saved;$('clear-credentials').hidden=!r.api_credentials_saved;$('login-form').hidden=!r.api_credentials_saved||Boolean(r.saved_session_available);}
  if(r.state){$('login').hidden=r.state!=='not_connected';$('verify').hidden=!['code_required','password_required'].includes(r.state);$('password-label').hidden=r.state!=='password_required';$('code-label').hidden=r.state==='password_required';$('search').hidden=r.state!=='connected';$('logout').hidden=r.state==='not_connected'&&!r.saved_session_available;}
  $('status').textContent=r.error?(errors[r.error]||`요청을 완료하지 못했습니다: ${r.error}`)+(r.retry_after_seconds?` (${r.retry_after_seconds}초 후)`:''):(states[r.state]||'요청을 확인했습니다.');
}
async function request(op,args){
  const response=await fetch('/ui',{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify({op,args})});
  const r=await response.json();renderState(r);return r;
}
async function action(fn){if(busy)return;busy=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);try{await fn();}catch{ $('status').textContent='연결 프로그램에 접근할 수 없습니다. 실행 상태를 확인하세요.';}finally{busy=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);}}
$('credentials-form').addEventListener('submit',e=>{e.preventDefault();action(async()=>{const args={api_id:Number($('api-id').value),api_hash:$('api-hash').value.trim()};$('api-hash').value='';$('api-id').value='';await request('save_credentials',args);});});
$('clear-credentials').addEventListener('click',()=>action(()=>request('clear_credentials',{})));
$('login-form').addEventListener('submit',e=>{e.preventDefault();action(async()=>{const args={phone:$('phone').value.trim()};$('phone').value='';await request('login',args);});});
$('verify-form').addEventListener('submit',e=>{e.preventDefault();action(async()=>{const args=$('password-label').hidden?{code:$('code').value.trim()}:{password:$('password').value};$('code').value='';$('password').value='';await request('verify',args);});});
$('restore-session').addEventListener('click',()=>action(()=>request('restore',{})));
$('persist-session').addEventListener('click',()=>action(()=>request('persist_session',{})));
$('logout').addEventListener('click',()=>action(async()=>{await request('logout',{});$('results').replaceChildren();$('room').replaceChildren();nextRoom=nextNews=lastSearch=null;$('more-news').hidden=$('more-rooms').hidden=true;}));
async function rooms(refresh){const r=await request('dialogs',{offset:refresh?0:nextRoom,limit:100,refresh});if(r.error)return;if(refresh)$('room').replaceChildren();for(const room of r.rooms){const o=document.createElement('option');o.value=room.id;o.textContent=`${room.title} · ${room.kind==='channel'?'채널':'그룹'}`;$('room').append(o);}nextRoom=r.next_offset;$('more-rooms').hidden=nextRoom===null;$('rooms-note').textContent=`가입 채널·그룹 ${r.total}개. 개인 대화는 제외합니다.`+(r.source_truncated?' 목록 조회 상한에 도달해 일부 방이 빠질 수 있습니다.':'');}
$('rooms-button').addEventListener('click',()=>action(()=>rooms(true)));
$('more-rooms').addEventListener('click',()=>action(()=>rooms(false)));
function link(parent,url,label){if(!url)return;try{const u=new URL(url);if(!['http:','https:'].includes(u.protocol))return;}catch{return;}const a=document.createElement('a');a.href=url;a.target='_blank';a.rel='noreferrer noopener';a.textContent=label;parent.append(a,document.createElement('br'));}
async function search(offset){const r=await request('search',{...lastSearch,offset_id:offset,limit:30});if(r.error)return;nextNews=r.next_offset_id;$('more-news').hidden=nextNews===null;$('results').replaceChildren();const note=document.createElement('p');note.textContent=`조회 시각 ${r.fetched_at} · 검색 일치 ${r.scanned}건 중 뉴스 후보 ${r.news_candidates}건. ${r.classification_note}`;$('results').append(note);for(const item of r.items){const a=document.createElement('article'),h=document.createElement('strong'),p=document.createElement('p');h.textContent=`${item.room_title} · ${item.published_at}`;p.textContent=item.text+(item.text_truncated?'\n[긴 글 일부 생략]':'');a.append(h,p);link(a,item.message_url,'텔레그램 원문');for(const u of item.article_urls)link(a,u,'기사 링크');$('results').append(a);}}
$('search-form').addEventListener('submit',e=>{e.preventDefault();action(async()=>{lastSearch={room_id:$('room').value,query:$('query').value.trim(),start:$('start').value,end:$('end').value};nextNews=null;$('more-news').hidden=true;await search(0);});});
$('more-news').addEventListener('click',()=>action(()=>search(nextNews)));
const day=d=>new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).format(d);
$('end').value=day(new Date());$('start').value=day(new Date(Date.now()-7*86400000));
action(async()=>{const r=await(await fetch('/ui-state',{cache:'no-store'})).json();token=r.token;renderState(r);});
