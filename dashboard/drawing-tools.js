/* Date/price anchors; SVG projection follows the Lightweight Charts public scales. */
window.DrawingTools = (() => {
  const count = {trend:2, channel:3, horizontal:1, box:2, measure:2, riskreward:3};
  const labels = {trend:'추세선',channel:'평행 채널',horizontal:'수평선',box:'박스',measure:'구간 측정',riskreward:'손익비'};
  const clone = x => JSON.parse(JSON.stringify(x));
  function channel(points) {
    const [a,b,c] = points, slope = (b.y-a.y)/(b.x-a.x);
    const offset = c.y - (a.y + slope*(c.x-a.x));
    return [a,b,{x:b.x,y:b.y+offset},{x:a.x,y:a.y+offset}];
  }
  function boundary(bars, i, frame) {
    if(i+1<bars.length)return Date.parse(bars[i+1].date);
    const d=new Date(bars[i].date+'T00:00:00Z');
    if(frame==='1mo')d.setUTCMonth(d.getUTCMonth()+1);else d.setUTCDate(d.getUTCDate()+(frame==='1w'?7:1));
    return d.getTime();
  }
  // Calendar-only slots: no prices, volumes or indicator values are projected.
  function timeline(bars,frame='1d') {
    if(!bars.length)return [];
    const result=bars.map(b=>({...b})), last=bars.at(-1);
    const end=new Date((last.last_session||last.date)+'T00:00:00Z');end.setUTCFullYear(end.getUTCFullYear()+5);
    const cursor=new Date(last.date+'T00:00:00Z');
    const next=()=>{if(frame==='1mo')cursor.setUTCMonth(cursor.getUTCMonth()+1);else cursor.setUTCDate(cursor.getUTCDate()+(frame==='1w'?7:1));};
    next();
    while(cursor<=end){
      if(frame!=='1d'||![0,6].includes(cursor.getUTCDay()))result.push({date:cursor.toISOString().slice(0,10),projected:true});
      next();
    }
    if(frame!=='1d')for(let i=bars.length-1;i<result.length;i++){
      // A partial current week/month can also contain future drawing anchors.
      result[i].last_session=new Date(boundary(result,i,frame)-86400000).toISOString().slice(0,10);
      result[i].first_session ||= result[i].date;
    }
    result.futureAfter=last.last_session||last.date;
    return result;
  }
  function index(time,bars,frame='1d') {
    if(!Number.isFinite(Date.parse(time))||!bars.length||time<bars[0].date||time>(bars.at(-1).last_session||bars.at(-1).date))return null;
    let lo=0,hi=bars.length;
    while(lo<hi){const m=(lo+hi)>>1;if(bars[m].date<=time)lo=m+1;else hi=m;}
    const i=lo-1,start=Date.parse(bars[i].date),fraction=(Date.parse(time)-start)/(boundary(bars,i,frame)-start);
    // Preserve distinct dates even when both anchors fall inside one week/month.
    return frame==='1d'?i+fraction:i-.4+.8*fraction;
  }
  function coordinate(scale, logical) {
    const base=Math.floor(logical),left=scale.logicalToCoordinate(base),right=scale.logicalToCoordinate(base+1);
    return left==null||right==null?null:left+(logical-base)*(right-left);
  }
  function timeAt(logical,bars,frame='1d') {
    const i=Math.max(0,Math.min(bars.length-1,Math.round(logical)));
    if(frame==='1d')return bars[i].date;
    const start=Date.parse(bars[i].date),fraction=Math.max(0,Math.min(1,(logical-i+.4)/.8));
    const ms=Math.round((start+fraction*(boundary(bars,i,frame)-start))/86400000)*86400000;
    const time=new Date(ms).toISOString().slice(0,10);
    return time<(bars[i].first_session||bars[i].date)?(bars[i].first_session||bars[i].date):time>(bars[i].last_session||bars[i].date)?(bars[i].last_session||bars[i].date):time;
  }
  function measure(points, bars, currency, frame='1d') {
    const [a,b] = points, delta = b.price-a.price;
    const ia=index(a.time,bars,frame), ib=index(b.time,bars,frame);
    const days=Math.round((Date.parse(b.time)-Date.parse(a.time))/86400000);
    return `${delta>=0?'+':''}${WorkbenchFormat.money(delta,currency)} ${currency} · ${delta>=0?'+':''}${(delta/a.price*100).toFixed(2)}% · ${ia==null||ib==null?'미확인':Math.abs(Math.round(ib)-Math.round(ia))}${bars.futureAfter&&[a.time,b.time].some(t=>t>bars.futureAfter)?'칸 간격 (미래 달력)':'봉 간격'} · ${Math.abs(days)}일`;
  }
  function riskReward(points,currency) {
    if(points.length!==3)return null;
    const [entry,stop,target]=points.map(p=>p.price);
    if(!points.every(p=>Number.isFinite(p.price)&&p.price>0&&p.price<=1e15)||!(stop<entry&&entry<target||target<entry&&entry<stop)||new Set(points.map(p=>p.time)).size<2)return null;
    const risk=Math.abs(entry-stop),reward=Math.abs(target-entry),ratio=reward/risk;
    return {entry,stop,target,risk,reward,ratio,direction:stop<entry?'매수':'매도',
      label:'손익비 1 : '+ratio.toFixed(2),
      loss:'손실 '+WorkbenchFormat.money(risk,currency)+' '+currency+' ('+(risk/entry*100).toFixed(2)+'%)',
      profit:'수익 '+WorkbenchFormat.money(reward,currency)+' '+currency+' ('+(reward/entry*100).toFixed(2)+'%)'};
  }
  // Screen-pixel thresholds keep the same feel at every price/zoom level.
  function closeSnap(bars,scale,series,logical,y,previous=null) {
    const visible=scale.getVisibleLogicalRange?.();
    const first=Math.max(0,Math.ceil(visible?.from??0));
    const last=Math.min(bars.length-1,Math.round(logical),Math.floor(visible?.to??bars.length-1));
    function candidate(i) {
      const b=bars[i];
      if(i<first||i>last||!b||b.projected||!Number.isFinite(b.close)||b.close<=0)return null;
      const x=scale.logicalToCoordinate(i),cy=series.priceToCoordinate(b.close);
      if(x==null||cy==null||x<0||x>scale.width()||!Number.isFinite(cy))return null;
      return {i,time:b.date,price:b.close,distance:Math.abs(y-cy)};
    }
    const held=previous&&candidate(previous.i);
    if(held&&held.distance<=10)return held;
    let best=null;
    for(let i=last;i>=first;i--){const c=candidate(i);if(c&&c.distance<=6&&(!best||c.distance<best.distance))best=c;}
    return best;
  }
  function attach({element, chart, series, bars, currency, timeframe='1d', drawingLimit=300, snapEnabled=true, getDrawings, getOverlays=()=>[], onChange, onSelect, onHint}) {
    const NS='http://www.w3.org/2000/svg';
    const svg=document.createElementNS(NS,'svg');
    svg.classList.add('drawing-layer');svg.setAttribute('aria-label','가격 차트 작도 영역');
    element.append(svg);
    let mode='select', selected=null, draft=[], hover=null, drag=null, frame=0, removed=false, signature='', snapped=null;
    function node(tag,attrs,parent=svg) {
      const n=document.createElementNS(NS,tag);for(const [k,v] of Object.entries(attrs))n.setAttribute(k,v);parent.append(n);return n;
    }
    function xy(p) {const i=index(p.time,bars,timeframe);return i==null?null:{x:coordinate(chart.timeScale(),i),y:series.priceToCoordinate(p.price)};}
    function point(event) {
      const r=svg.getBoundingClientRect(),x=event.clientX-r.left,y=event.clientY-r.top;
      const logical=chart.timeScale().coordinateToLogical(x), price=series.coordinateToPrice(y);
      if(logical==null||price==null||price<=0||x<0||y<0||x>r.width||y>r.height){snapped=null;return null;}
      snapped=snapEnabled&&!event.altKey&&(count[mode]||drag?.handle!=null)?closeSnap(bars,chart.timeScale(),series,logical,y,snapped):null;
      return {time:timeAt(logical,bars,timeframe),price:snapped?snapped.price:price};
    }
    function render() {
      if(removed)return;
      const w=chart.timeScale().width(),h=chart.panes()[0].getHeight();
      const all=getDrawings().concat(getOverlays()).map(d=>({d,p:d.points.map(xy)})).sort((a,b)=>Number(a.d.id===selected)-Number(b.d.id===selected));
      if(draft.length) {
        const ps=[...draft];if(hover&&ps.length<count[mode])ps.push(hover);
        if(ps.length===count[mode])all.push({d:{kind:mode,points:ps,color:'#218775',width:2,extend:false},p:ps.map(xy)});
      }
      const sig=JSON.stringify([w,h,all,mode,selected,draft,hover,snapped]);
      if(signature!==sig) {
        signature=sig;svg.replaceChildren();svg.setAttribute('width',w);svg.setAttribute('height',h);
        svg.style.pointerEvents=count[mode]?'auto':'none';
        svg.style.cursor=count[mode]?'crosshair':'default';
        if(count[mode])node('rect',{width:w,height:h,fill:'transparent','pointer-events':'all'});
        for(const {d,p} of all) {
          if(p.some(v=>!v||v.x==null||v.y==null||!Number.isFinite(v.x+v.y)))continue;
          const g=node('g',{'data-id':d.id||'',opacity:d.id?1:.65,'aria-label':(d.ai?'AI 제안 · ':'')+labels[d.kind],class:d.ai?'ai-proposal':'user-drawing'});
          const [a,b]=p, stroke=d.color;
          function line(v1,v2,dash='') {
            node('line',{x1:v1.x,y1:v1.y,x2:v2.x,y2:v2.y,stroke:'transparent','stroke-width':14,'pointer-events':mode==='select'?'stroke':'none'},g);
            node('line',{x1:v1.x,y1:v1.y,x2:v2.x,y2:v2.y,stroke,'stroke-width':d.width,'stroke-dasharray':d.ai?'7 5':dash,'pointer-events':'none'},g);
          }
          if(d.kind==='horizontal') {
            line({x:0,y:a.y},{x:w,y:a.y});
            const text=node('text',{x:8,y:a.y-7,fill:stroke,'font-size':12,'pointer-events':'none'},g);
            text.textContent=WorkbenchFormat.money(d.points[0].price,currency)+' '+currency;
          } else if(d.kind==='riskreward') {
            const result=riskReward(d.points,currency);if(!result)continue;
            const left=Math.min(...p.map(v=>v.x)),right=Math.max(...p.map(v=>v.x)),entry=p[0],stop=p[1],target=p[2];
            for(const [edge,color] of [[stop,'#ee6677'],[target,'#36c595']]){
              node('rect',{x:left,y:Math.min(entry.y,edge.y),width:Math.max(1,right-left),height:Math.abs(edge.y-entry.y),fill:color,'fill-opacity':.18,stroke:color,'stroke-width':d.width,'pointer-events':mode==='select'?'all':'none'},g);
            }
            line({x:left,y:entry.y},{x:right,y:entry.y},'5 3');
            const texts=[result.direction+' · '+result.label,result.profit,result.loss];
            const tx=Math.max(6,Math.min(left+6,w-235)),ty=Math.max(16,Math.min(Math.min(stop.y,target.y)-42,h-48));
            const text=node('text',{x:tx,y:ty,fill:'#e9f2ff','font-size':11,'paint-order':'stroke',stroke:'#202630','stroke-width':3,'pointer-events':'none'},g);
            texts.forEach((t,i)=>node('tspan',{x:tx,dy:i?15:0},text).textContent=t);
          } else if(d.kind==='box'||d.kind==='measure') {
            node('rect',{x:Math.min(a.x,b.x),y:Math.min(a.y,b.y),width:Math.max(1,Math.abs(b.x-a.x)),height:Math.max(1,Math.abs(b.y-a.y)),fill:stroke,'fill-opacity':.10,stroke,'stroke-width':d.width,'pointer-events':mode==='select'?'all':'none'},g);
            if(d.kind==='measure') {
              line(a,b,'4 3');
              const parts=measure(d.points,bars,currency,timeframe).split(' · '), lines=[parts.slice(0,2).join(' · '),parts.slice(2).join(' · ')];
              const labelWidth=Math.min(w-10,Math.max(...lines.map(t=>t.length))*7);
              const x=Math.max(5,Math.min(Math.min(a.x,b.x),w-labelWidth-5)),y=Math.max(17,Math.min(a.y,b.y)-23);
              const text=node('text',{x,y,fill:stroke,'font-size':12,'paint-order':'stroke',stroke:'#fff','stroke-width':3,'pointer-events':'none'},g);
              lines.forEach((value,i)=>{node('tspan',{x,dy:i?15:0},text).textContent=value;});
            }
          } else if(a.x!==b.x) {
            const slope=(b.y-a.y)/(b.x-a.x), start=d.extend&&b.x<a.x?b:a, end=d.extend?{x:w,y:a.y+slope*(w-a.x)}:b;
            line(start,end);
            if(d.kind==='channel') {
              const q=channel(p), offset=q[3].y-a.y;
              const upperStart={x:start.x,y:start.y+offset},upperEnd={x:end.x,y:end.y+offset};
              node('polygon',{points:[start,end,upperEnd,upperStart].map(v=>`${v.x},${v.y}`).join(' '),fill:stroke,'fill-opacity':.10,'pointer-events':mode==='select'?'all':'none'},g);
              line(upperStart,upperEnd);line({x:start.x,y:start.y+offset/2},{x:end.x,y:end.y+offset/2},'5 4');
            }
          }
          if(d.ai){g.setAttribute('opacity','.7');g.querySelectorAll('*').forEach(n=>n.setAttribute('pointer-events','none'));}
          if(d.id===selected&&mode==='select')p.forEach((v,i)=>node('circle',{cx:v.x,cy:v.y,r:6,fill:'#fff',stroke,'stroke-width':2,'data-point':i,'pointer-events':'all',cursor:'move'},g));
        }
        for(const p of draft){const v=xy(p);if(v)node('circle',{cx:v.x,cy:v.y,r:4,fill:'#218775','pointer-events':'none'});}
        if(hover&&(count[mode]||drag?.handle!=null)){
          const v=xy(hover);
          if(v&&v.x!=null&&v.y!=null){
            if(snapped){
              const sx=chart.timeScale().logicalToCoordinate(snapped.i);
              const g=node('g',{class:'close-snap-guide','data-source-date':snapped.time,'data-price':snapped.price,'pointer-events':'none'});
              node('line',{x1:sx,y1:v.y,x2:v.x,y2:v.y,stroke:'#83d8ff','stroke-width':1.5,'stroke-dasharray':'3 4'},g);
              node('circle',{cx:sx,cy:v.y,r:4,fill:'#202630',stroke:'#83d8ff','stroke-width':2},g);
              const label='종가 '+WorkbenchFormat.money(snapped.price,currency)+' '+currency+' · '+snapped.time;
              const width=Math.min(w-8,label.length*7+12),x=Math.max(4,Math.min(v.x-width/2,w-width-4));
              const top=v.y<35?v.y+12:v.y-30;
              node('rect',{x,y:top,width,height:22,rx:3,fill:'#172b3a',stroke:'#83d8ff','stroke-width':.5},g);
              node('text',{x:x+6,y:top+15,fill:'#b9eaff','font-size':11},g).textContent=label;
            }
            node('circle',{cx:v.x,cy:v.y,r:5,fill:snapped?'#83d8ff':'#202630',stroke:snapped?'#e4f7ff':'#83d8ff','stroke-width':2,'pointer-events':'none'});
          }
        }

      }
      frame=requestAnimationFrame(render);
    }
    function select(id) {mode='select';draft=[];hover=null;snapped=null;selected=id;onSelect(id);signature='';}
    function setMode(next) {
      mode=next;draft=[];hover=null;snapped=null;drag=null;signature='';
      onHint(mode==='riskreward'?'손익비: 진입가 → 손절가 → 목표가 순서로 클릭하세요. 가로 폭은 세 점의 날짜 범위입니다.':count[mode]?`${labels[mode]}: ${count[mode]}개 지점을 차례로 클릭하세요. Esc로 취소.`:'선택한 도형의 원형 점은 끝점 수정, 선·영역은 전체 이동입니다. 빈 영역은 차트 이동·확대에 사용합니다.');
    }
    function down(e) {
      if(e.button!==0)return;
      const p=point(e);if(!p)return;
      if(count[mode]) {
        hover=p;
        e.preventDefault();e.stopPropagation();
        if((mode==='trend'||mode==='channel')&&draft.length===1&&draft[0].time===p.time){onHint('두 번째 점은 다른 날짜를 선택하세요.');return;}
        if(mode==='riskreward'&&draft.length===1&&draft[0].price===p.price){onHint('손절가는 진입가와 달라야 합니다.');return;}
        if(mode==='riskreward'&&draft.length===2&&!riskReward([...draft,p],currency)){onHint('목표가는 진입가를 기준으로 손절가의 반대편에 두고, 날짜 범위를 넓혀 주세요.');return;}
        draft.push(p);
        if(draft.length===count[mode]) {
          if(getDrawings().length>=drawingLimit){draft=[];onHint(`종목당 최대 ${drawingLimit}개입니다. 필요 없는 도형을 삭제하세요.`);return;}
          const drawing={id:crypto.randomUUID(),kind:mode,points:clone(draft),color:'#218775',width:2,extend:false};
          onChange([...getDrawings(),drawing]);setMode('select');select(drawing.id);
        }else if(mode==='riskreward')onHint(draft.length===1?'손익비: 손절가를 클릭하세요.':'손익비: 진입가 반대편의 목표가를 클릭하세요.');else onHint(`${labels[mode]}: ${count[mode]-draft.length}개 지점을 더 클릭하세요.`);
      } else {
        const group=e.target.closest('[data-id]'),id=group?.getAttribute('data-id');
        if(!id||group.classList.contains('ai-proposal'))return;
        e.preventDefault();e.stopPropagation();select(id);
        const d=getDrawings().find(v=>v.id===id);
        drag={id,start:p,original:clone(d),all:clone(getDrawings()),handle:e.target.hasAttribute('data-point')?Number(e.target.getAttribute('data-point')):null,moved:false};
        svg.setPointerCapture(e.pointerId);
      }
    }
    function move(e) {
      const p=point(e);if(!p){hover=null;return;}hover=p;
      if(!drag)return;
      const d=clone(drag.original);
      if(drag.handle!=null)d.points[drag.handle]=p;
      else {
        let delta=Math.round(index(p.time,bars,timeframe)-index(drag.start.time,bars,timeframe));
        const ids=d.points.map(v=>index(v.time,bars,timeframe));
        if(ids.some(i=>i==null))return;
        delta=Math.max(-Math.floor(Math.min(...ids)+.4),Math.min(bars.length-1-Math.floor(Math.max(...ids)+.4),delta));
        const priceDelta=p.price-drag.start.price;
        d.points=d.points.map((v,i)=>({time:delta===0?v.time:timeAt(ids[i]+delta,bars,timeframe),price:v.price+priceDelta}));
      }
      if(d.points.some(v=>v.price<=0)||((d.kind==='trend'||d.kind==='channel')&&d.points[0].time===d.points[1].time))return;
      if(d.kind==='riskreward'&&!riskReward(d.points,currency))return;
      drag.moved=true;
      onChange(drag.all.map(v=>v.id===d.id?d:v),{preview:true});
    }
    function up(e) {
      if(!drag)return;
      if(svg.hasPointerCapture(e.pointerId))svg.releasePointerCapture(e.pointerId);
      const before=drag.all,moved=drag.moved;drag=null;snapped=null;hover=null;
      if(moved)onChange(getDrawings(),{before});
    }
    function cancel() {
      if(drag){const old=drag.all;drag=null;onChange(old,{preview:true});}
      draft=[];hover=null;snapped=null;signature='';
    }
    svg.addEventListener('pointerdown',down);svg.addEventListener('pointermove',move);svg.addEventListener('pointerup',up);
    svg.addEventListener('pointercancel',cancel);
    svg.addEventListener('pointerleave',()=>{if(!drag){hover=null;snapped=null;}});
    setMode('select');frame=requestAnimationFrame(render);
    return {setMode,select,cancel,setSnapEnabled(value){snapEnabled=!!value;snapped=null;hover=null;signature='';},destroy(){removed=true;cancelAnimationFrame(frame);svg.remove();}};
  }
  return {attach,measure,riskReward,channel,labels,count,index,timeAt,coordinate,timeline,closeSnap};
})();
