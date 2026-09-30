window.WorkbenchWorkspace = (() => {
  const clone=x=>JSON.parse(JSON.stringify(x));
  const safe=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let active=null;
  async function closeActive() {
    if(!active)return true;
    const current=active;
    if(!await current.save())return false;
    if(active===current){current.destroy();active=null;}return true;
  }
  async function mount({host,plot,stock,market,symbol,currency,legend,isCurrent}) {
    const frame=stock.timeframe||'1d', frameName=({'1d':'일봉','1w':'주봉','1mo':'월봉'})[frame];
    const key=encodeURIComponent(market+':'+symbol),url='/api/chart-state/'+key+'?timeframe='+frame;
    const bars=stock.bars||[];
    if(!bars.length){host.innerHTML='<div class="empty">검증된 가격 이력이 없어 이동평균·작도를 사용할 수 없습니다.</div>';return;}
    let packet;
    try {const r=await fetch(url);if(!r.ok)throw Error();packet=await r.json();}
    catch {host.innerHTML='<div class="notice error">작도 저장소를 읽지 못했습니다. 원본을 보호하기 위해 편집을 열지 않았습니다. 대시보드를 다시 실행한 뒤 재시도하세요.</div>';return;}
    if(!isCurrent())return;
    let state=packet.state,revision=packet.revision,lastSaved=JSON.stringify(state),history=[],future=[],selected=null;
    let previewDrawings=null;
    let saving=null,timer=null,dead=false,averageVersion=0,chart,price,drawings,averageRefs=[],averageLabels=new Map(),conflict=false;
    host.innerHTML=`<div class="chart-tools" role="toolbar" aria-label="차트 작도 도구">
      <button data-tool="select" class="selected">선택·이동</button><button data-tool="trend">추세선</button><button data-tool="channel">평행 채널</button><button data-tool="horizontal">수평선</button><button data-tool="box">박스</button><button data-tool="measure">구간 측정</button>
      <span class="tool-divider"></span><button data-action="undo" title="Ctrl+Z">실행 취소</button><button data-action="redo" title="Ctrl+Shift+Z">다시 실행</button><button data-action="delete">선택 삭제</button><button data-action="save" class="primary">저장</button>
      <span class="save-state" role="status" aria-live="polite"></span></div>
      <p class="drawing-hint"></p><div class="chart-controls"><details class="averages-control"><summary>이동평균선 · ${frameName} SMA</summary><div class="average-list"></div><form class="average-add"><label>추가 기간 <input name="period" type="number" min="1" max="500" value="30" required aria-label="추가 SMA 기간"></label><button>추가</button><small>1~500봉 · 최대 6개 · 종가 기준</small></form><p class="average-status" role="status"></p></details><details class="drawing-control"><summary>작도 목록·정밀 편집 <span class="drawing-count"></span></summary><div class="drawing-list"></div><div class="drawing-editor"></div></details></div>`;
    const q=s=>host.querySelector(s),qa=s=>host.querySelectorAll(s);
    function status(text,bad=false){q('.save-state').textContent=text;q('.save-state').classList.toggle('error-text',bad);}
    function dirty(){return JSON.stringify(state)!==lastSaved;}
    function schedule(){clearTimeout(timer);status('저장 대기');timer=setTimeout(()=>save(),600);}
    function push(before){history.push(clone(before));if(history.length>50)history.shift();future=[];}
    function commit(next,before=state) {previewDrawings=null;push(before);state=clone(next);refresh();schedule();}
    function refresh() {
      q('[data-action="undo"]').disabled=!history.length;q('[data-action="redo"]').disabled=!future.length;
      q('[data-action="delete"]').disabled=!selected;
      q('.drawing-count').textContent=`(${state.drawings.length})`;
      q('.drawing-list').innerHTML=state.drawings.map((d,i)=>`<button data-id="${safe(d.id)}" class="${d.id===selected?'selected':''}">${i+1}. ${DrawingTools.labels[d.kind]}</button>`).join('')||'<small>저장한 작도가 없습니다.</small>';
      q('.drawing-list').querySelectorAll('[data-id]').forEach(b=>b.onclick=()=>drawings.select(b.dataset.id));
      renderEditor();
    }
    function renderEditor() {
      const d=state.drawings.find(x=>x.id===selected),editor=q('.drawing-editor');
      if(!d){editor.innerHTML='<small>차트의 도형이나 목록을 선택하면 날짜·가격·색상을 수정할 수 있습니다.</small>';return;}
      editor.innerHTML=`<form class="point-form"><strong>${DrawingTools.labels[d.kind]}</strong><div class="point-grid">${d.points.map((p,i)=>`<label>점 ${i+1} 날짜<input type="date" name="time${i}" value="${safe(p.time)}" required></label><label>점 ${i+1} 가격 (${currency})<input type="text" inputmode="decimal" name="price${i}" value="${WorkbenchFormat.money(p.price,currency)}" required></label>`).join('')}</div><div class="editor-style"><label>선 색상<input type="color" name="color" value="${d.color}"></label><label>굵기<select name="width">${[1,2,3,4].map(n=>`<option ${d.width===n?'selected':''}>${n}</option>`).join('')}</select></label>${['trend','channel'].includes(d.kind)?`<label><input type="checkbox" name="extend" ${d.extend?'checked':''}>오른쪽 연장</label>`:''}<button>수정 적용</button></div><p class="point-error" role="alert"></p></form>${d.kind==='measure'?`<p class="measurement-result">${safe(DrawingTools.measure(d.points,bars,currency))}</p>`:''}`;
      editor.querySelector('form').onsubmit=e=>{
        e.preventDefault();const f=new FormData(e.target),next=clone(d);
        next.points=d.points.map((p,i)=>({time:f.get('time'+i),price:f.get('price'+i)===WorkbenchFormat.money(p.price,currency)?p.price:Number(String(f.get('price'+i)).replaceAll(',',''))}));
        if(next.points.some(p=>!bars.some(b=>b.date===p.time)||!Number.isFinite(p.price)||p.price<=0||p.price>1e15)) {q('.point-error').textContent='저장된 거래일과 0보다 큰 유효 가격을 입력하세요.';return;}
        if(['trend','channel'].includes(d.kind)&&next.points[0].time===next.points[1].time){q('.point-error').textContent='첫 두 점은 서로 다른 거래일이어야 합니다.';return;}
        next.color=f.get('color');next.width=Number(f.get('width'));next.extend=f.has('extend');
        commit({...state,drawings:state.drawings.map(x=>x.id===d.id?next:x)});
      };
    }
    function renderAverages() {
      q('.average-list').innerHTML=state.averages.map(a=>`<div class="average-row" data-period="${a.period}"><label><input type="checkbox" name="visible" ${a.visible?'checked':''} aria-label="SMA ${a.period} 표시">SMA ${a.period}</label><input type="color" name="color" value="${a.color}" aria-label="SMA ${a.period} 색상"><select name="width" aria-label="SMA ${a.period} 굵기">${[1,2,3,4].map(n=>`<option ${a.width===n?'selected':''}>${n}</option>`).join('')}</select><button type="button" data-remove="${a.period}" aria-label="SMA ${a.period} 삭제">삭제</button></div>`).join('');
      q('.average-list').querySelectorAll('.average-row').forEach(row=>{
        row.onchange=()=>{const period=Number(row.dataset.period);const averages=state.averages.map(a=>a.period===period?{...a,color:row.querySelector('[name="color"]').value,width:Number(row.querySelector('[name="width"]').value),visible:row.querySelector('[name="visible"]').checked}:a);commit({...state,averages});loadAverages();};
        row.querySelector('button').onclick=()=>{commit({...state,averages:state.averages.filter(a=>a.period!==Number(row.dataset.period))});renderAverages();loadAverages();};
      });
    }
    q('.average-add').onsubmit=e=>{
      e.preventDefault();const n=Number(new FormData(e.target).get('period'));
      if(!Number.isInteger(n)||n<1||n>500||state.averages.length>=6||state.averages.some(a=>a.period===n)){q('.average-status').textContent='중복 없이 1~500봉, 최대 6개를 입력하세요.';return;}
      commit({...state,averages:[...state.averages,{period:n,color:'#cc6963',width:2,visible:true}]});renderAverages();loadAverages();
    };
    async function loadAverages() {
      const version=++averageVersion;
      averageRefs.forEach(ref=>chart.removeSeries(ref));averageRefs=[];averageLabels.clear();
      const requested=state.averages.filter(a=>a.visible);q('.average-status').textContent='이동평균 계산 자료를 읽는 중…';
      try {
        const r=await fetch('/api/averages/'+key+'?periods='+requested.map(a=>a.period).join(',')+'&timeframe='+frame);if(!r.ok)throw Error();const values=await r.json();
        if(dead||version!==averageVersion)return;
        for(const a of requested) {
          const ref=chart.addSeries(LightweightCharts.LineSeries,{color:a.color,lineWidth:a.width,title:'SMA '+a.period,priceLineVisible:false,lastValueVisible:false,priceFormat:{type:'custom',minMove:currency==='KRW'?1:.01,formatter:v=>WorkbenchFormat.money(v,currency)}});
          ref.setData(values[a.period]);averageRefs.push(ref);averageLabels.set(ref,a.period);
        }
        const unavailable=requested.filter(a=>!(values[a.period]||[]).some(p=>p.value!=null)).map(a=>a.period);
        q('.average-status').textContent=unavailable.length?`이력 부족·결측: SMA ${unavailable.join(', ')} · 없는 값을 채우지 않습니다.`:`Python 계산 · ${frameName} 종가 SMA · 마지막 주/월은 기준일까지의 부분 봉일 수 있습니다.`;
      } catch {if(!dead&&version===averageVersion)q('.average-status').textContent='이동평균 자료 조회 실패. 설정을 다시 적용하거나 화면을 다시 여세요.';}
    }
    async function save() {
      clearTimeout(timer);
      if(saving){await saving;return dirty()?save():true;}
      if(!dirty())return true;
      if(conflict){status('다른 창에서 변경됨 · 새로고침 후 다시 편집하세요. 현재 편집 내용은 화면에 유지됩니다.',true);return false;}
      const snapshot=clone(state);status('저장 중…');
      saving=(async()=>{
        try {
          const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Token':packet.token},body:JSON.stringify({revision,state:snapshot})});
          if(r.status===409){conflict=true;throw Error('다른 창에서 저장했습니다. 새로고침 후 다시 편집하세요.');}
          if(!r.ok)throw Error('저장 실패 · 저장 버튼으로 재시도하세요.');
          const result=await r.json();revision=result.revision;lastSaved=JSON.stringify(snapshot);status('저장됨 · '+new Date(result.updated_at).toLocaleTimeString('ko-KR'));return true;
        } catch(e){status(e.message,true);return false;}
      })();
      const ok=await saving;saving=null;
      if(ok&&dirty())return save();return ok;
    }
    function undo(redo=false) {
      const from=redo?future:history,to=redo?history:future;if(!from.length)return;
      drawings.cancel();previewDrawings=null;to.push(clone(state));state=from.pop();selected=null;drawings.select(null);refresh();renderAverages();loadAverages();schedule();
    }
    function remove() {if(!selected)return;const id=selected;selected=null;drawings.select(null);commit({...state,drawings:state.drawings.filter(x=>x.id!==id)});}
    qa('[data-tool]').forEach(b=>b.onclick=()=>{qa('[data-tool]').forEach(x=>x.classList.toggle('selected',x===b));drawings.setMode(b.dataset.tool);});
    q('[data-action="save"]').onclick=save;q('[data-action="undo"]').onclick=()=>undo();q('[data-action="redo"]').onclick=()=>undo(true);q('[data-action="delete"]').onclick=remove;
    function keyboard(e) {
      if(e.target.closest('input,select,textarea'))return;
      if(e.key==='Escape'){drawings.cancel();drawings.setMode('select');qa('[data-tool]').forEach(b=>b.classList.toggle('selected',b.dataset.tool==='select'));}
      else if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='z'){e.preventDefault();undo(e.shiftKey);}
      else if(e.key==='Delete'){e.preventDefault();remove();}
    }
    function beforeUnload(e){if(dirty()){e.preventDefault();e.returnValue='';}}
    chart=WorkbenchCharts.create(plot,[{type:'candles',currency,label:currency,data:bars.map(b=>({time:b.date,open:b.open,high:b.high,low:b.low,close:b.close}))},{type:'volume',label:'거래량',pane:1,data:bars.map(b=>({time:b.date,...(b.volume==null?{}:{value:b.volume}),color:b.close>=b.open?'#b2d6c6':'#e8bdb8'})),options:{priceFormat:{type:'volume'}}}],{height:plot.clientHeight,legend,lastBars:126,onReady:(c,refs)=>{price=refs[0].ref;}});
    chart.subscribeCrosshairMove(p=>{
      if(!p.time||!legend)return;
      const values=averageRefs.map(ref=>{const v=p.seriesData.get(ref)?.value;return 'SMA '+averageLabels.get(ref)+': '+(v==null?'결측':WorkbenchFormat.money(v,currency));});
      if(values.length)legend.textContent+='  /  '+values.join('  /  ');
    });
    drawings=DrawingTools.attach({element:plot,chart,series:price,bars,currency,getDrawings:()=>previewDrawings||state.drawings,
      onHint:text=>{q('.drawing-hint').textContent=text;},
      onSelect:id=>{selected=id;qa('[data-tool]').forEach(b=>b.classList.toggle('selected',b.dataset.tool==='select'));refresh();},
      onChange:(next,options={})=>{
        if(options.preview){previewDrawings=next;return;}
        previewDrawings=null;
        const before=options.before?{...state,drawings:options.before}:state;
        commit({...state,drawings:next},before);
      }});
    document.addEventListener('keydown',keyboard);window.addEventListener('beforeunload',beforeUnload);
    refresh();renderAverages();loadAverages();status(packet.updated_at?'저장본 복원됨':'새 작업 · 변경 시 자동 저장');
    const missing=state.drawings.filter(d=>d.points.some(p=>!bars.some(b=>b.date===p.time))).length;
    if(missing)q('.drawing-hint').textContent=`현재 가격 이력에 없는 날짜의 작도 ${missing}개는 보존하며 차트에 표시하지 않습니다. 목록에서 수정할 수 있습니다.`;
    active={save,destroy(){dead=true;clearTimeout(timer);averageVersion++;drawings.destroy();document.removeEventListener('keydown',keyboard);window.removeEventListener('beforeunload',beforeUnload);},range(value){let from=0;if(value!=='all'){const end=new Date(bars.at(-1).date+'T00:00:00Z');end.setUTCMonth(end.getUTCMonth()-Number(value));const start=end.toISOString().slice(0,10);from=bars.findIndex(b=>b.date>=start);if(from<0)from=bars.length-1;}chart.timeScale().setVisibleLogicalRange({from,to:bars.length-1});}};
    return active;
  }
  return {mount,closeActive};
})();
