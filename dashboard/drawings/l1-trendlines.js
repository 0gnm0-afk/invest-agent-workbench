/* M06 read-only L1 overlay. M10 supplies its HTTP reader and viewport events.
 * createController({request, renderer, isCurrent?, setTimer?, clearTimer?, delay?})
 *   -> {update({stock_key, start?, end?, timeframe?}), dispose()}
 * request receives only this calculation window and returns the L1 packet.
 * attach({chart, lineSeries, lineStyles?, legend?, request, ...controllerOptions})
 *   uses a LightweightCharts chart; no workspace state or save callback is read.
 */
window.WorkbenchL1Trendlines = (() => {
  const frames = {'1d':'일봉', '1w':'주봉', '1mo':'월봉'};
  const unavailable = {
    insufficient_data:'완료봉이 부족합니다.', invalid_data:'가격 자료를 확인할 수 없습니다.',
    unavailable:'저장 가격이 없습니다.', solver_failed:'추세선 계산을 완료하지 못했습니다.'
  };
  function calculationWindow(value) {
    if (!value || typeof value.stock_key !== 'string' || !value.stock_key.trim()) throw Error('stock_key_required');
    const timeframe = value.timeframe || '1d';
    if (!Object.hasOwn(frames, timeframe)) throw Error('unsupported_timeframe');
    const result = {stock_key:value.stock_key, timeframe};
    for (const key of ['start', 'end']) {
      if (value[key] != null) {
        if (typeof value[key] !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value[key])) throw Error('invalid_window');
        result[key] = value[key];
      }
    }
    if (result.start && result.end && result.start > result.end) throw Error('invalid_window');
    return result;
  }
  function createController({request, renderer, isCurrent=()=>true,
    setTimer=(fn,ms)=>setTimeout(fn,ms), clearTimer=id=>clearTimeout(id), delay=500}) {
    let generation=0, timer=null, dead=false;
    const current = version => !dead && version===generation && isCurrent();
    function update(value) {
      if (dead) return;
      const input=calculationWindow(value), version=++generation;
      if (timer!==null) clearTimer(timer);
      renderer.clear();
      renderer.status?.('pending', input);
      timer=setTimer(async () => {
        timer=null;
        if (!current(version)) return;
        try {
          const packet=await request(input);
          if (!current(version)) return;
          renderer.render(packet);
        } catch {
          if (!current(version)) return;
          renderer.clear();
          renderer.status?.('failed', input);
        }
      }, delay);
    }
    function dispose() {
      if (dead) return;
      dead=true;generation++;
      if (timer!==null) clearTimer(timer);
      timer=null;renderer.clear();
    }
    return {update, dispose};
  }
  function createChartOverlay({chart, lineSeries, lineStyles={Solid:0, Dashed:2}, legend}) {
    let refs=[];
    function clear() {
      refs.forEach(ref=>chart.removeSeries(ref));refs=[];
      if (legend) legend.textContent='';
    }
    function status(state) {
      if (legend) legend.textContent=state==='pending'?'자동 L1 추세선 · 화면 구간 계산 대기':
        '자동 L1 추세선 · 계산 자료를 읽지 못했습니다.';
    }
    function render(packet) {
      clear();
      if (!packet || !Object.hasOwn(frames, packet.window?.timeframe)) throw Error('invalid_l1_packet');
      const window=packet.calculated_window || packet.window;
      const range=`${window.start || '시작 미확인'} ~ ${window.end || '끝 미확인'} · ${frames[packet.window.timeframe]}`;
      if (packet.status!=='ok') {
        if (legend) legend.textContent=`자동 L1 추세선 · ${range} · ${unavailable[packet.status] || '계산 결과를 확인할 수 없습니다.'}`;
        return;
      }
      if (!Array.isArray(packet.lines)) throw Error('invalid_l1_lines');
      // Validate the entire response before adding any series.
      const lines=packet.lines.map(line=>{
        if (!['up','down'].includes(line.direction) || typeof line.in_progress!=='boolean' ||
          !Array.isArray(line.points) || line.points.length!==2 ||
          line.points.some(p=>typeof p.time!=='string' || !/^\d{4}-\d{2}-\d{2}$/.test(p.time) || !Number.isFinite(p.price)) ||
          line.points[0].time>=line.points[1].time) throw Error('invalid_l1_line');
        return {line, data:line.points.map(p=>({time:p.time, value:p.price}))};
      });
      for (const {line,data} of lines) {
        const title=`자동 L1 ${line.direction==='up'?'상승':'하락'}${line.in_progress?' · 진행 중':''}`;
        const ref=chart.addSeries(lineSeries, {color:line.direction==='up'?'#298463':'#994653',
          lineWidth:2, lineStyle:line.in_progress?lineStyles.Dashed:lineStyles.Solid, title,
          priceLineVisible:false, lastValueVisible:false, crosshairMarkerVisible:false,
          autoscaleInfoProvider:()=>null});
        refs.push(ref);ref.setData(data);
      }
      if (legend) legend.textContent=`자동 L1 추세선 · ${range} · ${lines.length}개`+
        (lines.some(({line})=>line.in_progress)?' · 마지막 구간 진행 중 (점선)':'')+
        ' · 화면 구간에 따라 바뀌는 참고선';
    }
    return {render, clear, status};
  }
  function attach({chart, lineSeries, lineStyles, legend, ...options}) {
    return createController({...options, renderer:createChartOverlay({chart,lineSeries,lineStyles,legend})});
  }
  return {createController, createChartOverlay, attach};
})();
