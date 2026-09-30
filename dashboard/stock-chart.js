window.WorkbenchStockChart = (() => {
  async function mount({stock,market,symbol,currency,isCurrent}) {
    const key=encodeURIComponent(market+':'+symbol),host=document.querySelector('#chart-workspace'),plot=document.querySelector('#price-chart');
    const status=document.querySelector('#history-status'),retry=document.querySelector('#history-retry');
    const frames=[...document.querySelectorAll('[data-frame]')],ranges=[...document.querySelectorAll('[data-range]')];
    let workspace,frame='1d',range='6',busy=false;
    function controls(disabled){frames.concat(ranges,[retry]).forEach(b=>b.disabled=disabled);}
    async function render(value) {
      if(!isCurrent())return;
      const h=value.history, labels={complete:'확보 완료',partial:'제공 이력 제한',failed:'조회 실패 · 기존 저장 이력 표시',pending:'다른 창에서 준비 중',not_requested:'기존 저장 이력'};
      status.classList.toggle('error-text',h.state==='failed');
      const ipDenied=['market_authentication_http_403_ip_not_allowed','market_http_403_ip_not_allowed'].includes(h.reason);
      status.textContent=`${labels[h.state]||h.state} · ${h.first||'미확인'} ~ ${h.last||'미확인'} · 일봉 ${h.daily_count.toLocaleString('ko-KR')}개 → ${({'1d':'일봉','1w':'주봉','1mo':'월봉'})[frame]} ${value.bars.length.toLocaleString('ko-KR')}개 · ${value.provider||'공급자 미확인'} · ${value.price_basis||'조정 기준 미확인'} · 수집 ${h.collected_at||'미확인'}${h.state==='partial'?' · 2016년까지 제공되지 않은 구간은 채우지 않습니다.':''}${h.state==='failed'?' · '+(ipDenied?'Toss 허용 IP 거절(403) · Toss API 허용 IP 설정 확인 필요':h.reason==='market_authentication_http_403'?'Toss 인증 거절(403) · API 접근 권한 확인 필요':h.reason):''}${h.screening_price_difference?' · 주의: 새 수정주가와 원 스크리닝 가격이 다릅니다. 기존 피벗·작도와 비교 전 조정 기준을 확인하세요.':''} · 주/월 봉 날짜는 기간 시작일, 첫·마지막 봉은 확보된 거래일만 집계합니다. 작도·SMA 설정은 봉 주기별로 저장합니다.`;
      retry.hidden=!['failed','pending'].includes(h.state);
      workspace=await WorkbenchWorkspace.mount({host,plot,stock:value,market,symbol,currency,legend:document.querySelector('#price-legend'),isCurrent});
      if(!isCurrent())return;
      workspace?.range(range);
      frames.forEach(b=>b.classList.toggle('selected',b.dataset.frame===frame));
      ranges.forEach(b=>b.classList.toggle('selected',b.dataset.range===range));
    }
    async function change(nextFrame,collect=false) {
      if(busy||!isCurrent())return;
      busy=true;controls(true);
      try {
        if(!await WorkbenchWorkspace.closeActive())return;
        if(!isCurrent())return;
        WorkbenchCharts.clear();plot.replaceChildren();host.replaceChildren();
        const priorFrame=frame;frame=nextFrame;
        if(priorFrame!==frame)range=frame==='1d'?'6':'all';
        let pending=false;
        if(collect){
          status.textContent='선택 종목의 2016년 이후 장기 이력을 확인하는 중… 첫 조회만 시간이 걸립니다.';
          const health=await fetch('/health');if(!health.ok)throw Error();const {token}=await health.json();
          if(!isCurrent())return;
          const response=await fetch('/api/chart-history/'+key,{method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Token':token},body:JSON.stringify({retry:collect==='retry'})});
          if(!response.ok)throw Error();
          pending=(await response.json()).history.state==='pending';
        }
        if(!isCurrent())return;
        const response=await fetch('/api/chart/'+key+'?timeframe='+frame);if(!response.ok)throw Error();
        const value=await response.json();
        if(pending&&value.history.state==='not_requested')value.history.state='pending';
        await render(value);
      }catch {
        if(isCurrent()){status.textContent='장기 차트를 열지 못했습니다. 재시도할 수 있습니다.';retry.hidden=false;}
      }finally{busy=false;if(isCurrent())controls(false);}
    }
    frames.forEach(b=>b.onclick=()=>change(b.dataset.frame));
    ranges.forEach(b=>b.onclick=()=>{range=b.dataset.range;workspace?.range(range);ranges.forEach(x=>x.classList.toggle('selected',x===b));});
    retry.onclick=()=>change(frame,'retry');
    await change('1d',true);
  }
  return {mount};
})();
