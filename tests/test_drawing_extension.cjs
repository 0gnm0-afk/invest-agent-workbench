const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
global.window=global;
for(const name of ['format.js','drawing-tools.js'])vm.runInThisContext(fs.readFileSync(path.join(__dirname,'../dashboard',name),'utf8'));
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.attrs={};this.events={};this.style={};this.classList={add(){}};}
  append(child){this.children.push(child);}
  setAttribute(k,v){this.attrs[k]=String(v);}getAttribute(k){return this.attrs[k];}
  replaceChildren(){this.children=[];}addEventListener(k,fn){this.events[k]=fn;}remove(){}
  querySelectorAll(){return this.children.flatMap(c=>[c,...c.querySelectorAll()]);}
  getBoundingClientRect(){return {left:0,top:0,width:100,height:200};}
}
let pending;
global.document={createElementNS:(_,tag)=>new Element(tag)};
global.requestAnimationFrame=fn=>{pending=fn;return 1;};global.cancelAnimationFrame=()=>{};
const color='#218775';
function fixture(drawings,frame='1d',shift=0){
  const bars=Array.from({length:6},(_,i)=>({date:frame==='1mo'?`2026-${String(i+1).padStart(2,'0')}-01`:
    frame==='1w'?new Date(Date.UTC(2026,8,1+i*7)).toISOString().slice(0,10):`2026-09-${String(i+1).padStart(2,'0')}`}));
  let shapes=drawings(bars),changes=0;
  const original=structuredClone(shapes),el=new Element('div'),scale={width:()=>100,logicalToCoordinate:i=>i*10+shift,coordinateToLogical:x=>(x-shift)/10};
  const tool=DrawingTools.attach({element:el,chart:{timeScale:()=>scale,panes:()=>[{getHeight:()=>200}]},
    series:{priceToCoordinate:p=>200-p,coordinateToPrice:y=>200-y},bars,timeframe:frame,currency:'USD',getDrawings:()=>shapes,
    onChange:next=>{shapes=next;changes++;},onSelect(){},onHint(){}});
  const svg=el.children[0];
  return {tool,svg,bars,original,get shapes(){return shapes;},get changes(){return changes;},
    render(){pending();return svg.querySelectorAll().filter(n=>n.tag==='line'&&n.attrs.stroke===color).map(n=>({
      x1:+n.attrs.x1,y1:+n.attrs.y1,x2:+n.attrs.x2,y2:+n.attrs.y2,dash:n.attrs['stroke-dasharray']}));}};
}
function drawing(bars,kind='trend',extend=true,reverse=false){
  const points=[{time:bars[1].date,price:80},{time:bars[3].date,price:120}];
  if(reverse)points.reverse();if(kind==='channel')points.push({time:bars[2].date,price:130});
  return {id:'test',kind,points,color,width:2,extend};
}
// The saved anchors stay solid, while both projections share exactly their slope.
for(const frame of ['1d','1w','1mo'])for(const reverse of [false,true]){
  const f=fixture(bars=>[drawing(bars,'trend',true,reverse)],frame),lines=f.render(),left=frame==='1d'?10:6,right=frame==='1d'?30:26;
  assert.equal(lines.length,3);
  assert.deepEqual(lines.slice(0,2),[{x1:0,y1:120+left*2,x2:left,y2:120,dash:'5 4'},
    {x1:right,y1:80,x2:100,y2:80-(100-right)*2,dash:'5 4'}]);
  assert.equal(lines[2].dash,'');assert.deepEqual([lines[2].x1,lines[2].x2],reverse?[right,left]:[left,right]);
  assert.deepEqual(f.shapes,f.original);assert.equal(f.changes,0);f.tool.destroy();
}
// Channel width is constant; its fill remains in the anchored span.
for(const reverse of [false,true]){
  const f=fixture(bars=>[drawing(bars,'channel',true,reverse)]),lines=f.render();assert.equal(lines.length,9);
  for(const i of [0,1,2]){assert.equal(lines[i+3].y1,lines[i].y1-30);assert.equal(lines[i+3].y2,lines[i].y2-30);
    assert.equal(lines[i+6].y1,lines[i].y1-15);assert.equal(lines[i+6].y2,lines[i].y2-15);}
  assert.equal(lines[2].dash,'');assert.equal(lines[5].dash,'');assert.equal(lines[8].dash,'5 4');
  const polygon=f.svg.querySelectorAll().find(n=>n.tag==='polygon');
  assert.equal(polygon.attrs.points,reverse?'30,80 10,120 10,90 30,50':'10,120 30,80 30,50 10,90');
  f.tool.destroy();
}
// Opting out preserves old finite drawings, including channel boundaries.
for(const kind of ['trend','channel']){
  const f=fixture(bars=>[drawing(bars,kind,false)]),lines=f.render();assert.equal(lines.length,kind==='trend'?1:3);
  assert.ok(lines.every(l=>l.x1===10&&l.x2===30));f.tool.destroy();
}
// Pan/zoom may hide both anchors: the visible continuation still has the same slope.
for(const shift of [-100,100]){
  const f=fixture(bars=>[drawing(bars)],'1d',shift),lines=f.render();assert.equal(lines.length,2);
  assert.equal(lines.filter(l=>l.dash==='5 4').length,1);
  for(const l of lines)assert.equal((l.y2-l.y1)/(l.x2-l.x1),-2);f.tool.destroy();
}
// Horizontal/flat trends and endpoints on the viewport edge produce no duplicate tail.
const flat=fixture(bars=>[{...drawing(bars),points:[{time:bars[0].date,price:100},{time:bars[3].date,price:100}]}]);
assert.deepEqual(flat.render(),[{x1:30,y1:100,x2:100,y2:100,dash:'5 4'},{x1:0,y1:100,x2:30,y2:100,dash:''}]);flat.tool.destroy();
// Completing a drawing enables projections only for trend/channel, after the last click.
for(const kind of Object.keys(DrawingTools.count)){
  const f=fixture(()=>[]);f.tool.setMode(kind);f.render();
  const positions=kind==='riskreward'?[[10,100],[30,120],[20,60]]:[[10,120],[30,80],[20,70]];
  for(let i=0;i<DrawingTools.count[kind];i++){
    const [x,y]=positions[i];f.svg.events.pointerdown({button:0,clientX:x,clientY:y,preventDefault(){},stopPropagation(){}});
    if(i+1<DrawingTools.count[kind])assert.equal(f.shapes.length,0);
  }
  assert.equal(f.shapes.length,1);assert.equal(f.shapes[0].extend,['trend','channel'].includes(kind));
  assert.deepEqual(Object.keys(f.shapes[0]).sort(),['color','extend','id','kind','points','width']);f.tool.destroy();
}
console.log('Dotted extensions: solid anchors, both directions, channel width, reverse order, pan, timeframes, opt-out and completion defaults: PASS');
