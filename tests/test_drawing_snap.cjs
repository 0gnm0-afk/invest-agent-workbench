const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const root=require('node:path').resolve(__dirname,'..');global.window=global;
for(const f of ['format.js','drawing-tools.js'])vm.runInThisContext(fs.readFileSync(root+'/dashboard/'+f,'utf8'));
const D=DrawingTools;
const bars=[{date:'2026-09-28',close:100.123456},{date:'2026-09-29',close:120},{date:'2026-09-30',close:125},{date:'2026-10-01',projected:true},{date:'2026-10-02',projected:true}];
const original=JSON.stringify(bars);
const scale={width:()=>200,logicalToCoordinate:i=>i*40,coordinateToLogical:x=>x/40,getVisibleLogicalRange:()=>({from:0,to:4})};
const series={priceToCoordinate:p=>200-p,coordinateToPrice:y=>200-y};
assert.equal(D.closeSnap(bars,scale,series,4,96).price,100.123456,'full source precision');
assert.equal(D.closeSnap(bars,scale,series,0,80),null,'never snap to a candle on the right');
let held=D.closeSnap(bars,scale,series,4,79);
assert.equal(held.price,120);
assert.equal(D.closeSnap(bars,scale,series,4,73,held).price,120,'hold current close across nearby competing prices');
assert.equal(D.closeSnap(bars,scale,series,4,69,held).price,125,'release old close and acquire another');
assert.equal(D.closeSnap(bars,scale,series,4,30,held),null,'free beyond threshold');
assert.equal(D.closeSnap(bars,{...scale,getVisibleLogicalRange:()=>({from:1,to:4})},series,4,100),null,'offscreen closes excluded');
assert.equal(D.closeSnap([{date:'2026-10-01',close:100,projected:true}],scale,series,4,100),null,'future whitespace excluded');
assert.equal(D.closeSnap([{date:'2026-10-01',close:null}],scale,series,4,200),null,'missing is not zero');
assert.equal(D.closeSnap([{date:'2026-09-28',close:120},bars[1]],scale,series,4,80).i,1,'equal prices prefer closest candle to cursor');
assert.equal(JSON.stringify(bars),original);
class Element {
 constructor(){this.events={};this.attrs={};this.style={};this.classList={add(){}};this.children=[];}
 append(c){this.children.push(c);}setAttribute(k,v){this.attrs[k]=v;}getAttribute(k){return this.attrs[k];}replaceChildren(){this.children=[];}
 addEventListener(k,f){this.events[k]=f;}remove(){}getBoundingClientRect(){return {left:0,top:0,width:200,height:200};}
 setPointerCapture(){}hasPointerCapture(){return true;}releasePointerCapture(){}
}
let render;global.document={createElementNS:()=>new Element()};global.requestAnimationFrame=f=>(render=f,1);global.cancelAnimationFrame=()=>{};
const el=new Element();let shapes=[];
const tool=D.attach({element:el,chart:{timeScale:()=>scale,panes:()=>[{getHeight:()=>200}]},series,bars,currency:'USD',getDrawings:()=>shapes,onChange:n=>{shapes=n;},onSelect(){},onHint(){}});
const svg=el.children[0],event=(x,y,extra={})=>({clientX:x,clientY:y,button:0,pointerId:1,target:{closest:()=>null},preventDefault(){},stopPropagation(){},...extra});
tool.setMode('trend');svg.events.pointermove(event(120,96));render();
let guide=svg.children.find(e=>e.attrs.class==='close-snap-guide');
assert.ok(guide,'dotted guide rendered before placing first point');
assert.equal(guide.attrs['data-source-date'],'2026-09-28');
assert.equal(guide.children[0].attrs['stroke-dasharray'],'3 4');
assert.equal(guide.children[0].attrs.x1,0);assert.equal(guide.children[0].attrs.x2,120);
assert.match(guide.children.at(-1).textContent,/100.12 USD/);
svg.events.pointerdown(event(120,96));svg.events.pointerdown(event(160,82));
assert.deepEqual(shapes[0].points,[{time:'2026-10-01',price:100.123456},{time:'2026-10-02',price:120}],'only price snaps; future dates remain');
tool.setMode('horizontal');svg.events.pointerdown(event(160,82,{altKey:true}));assert.equal(shapes.at(-1).points[0].price,118,'Alt bypass');
tool.setSnapEnabled(false);tool.setMode('horizontal');svg.events.pointerdown(event(160,82));assert.equal(shapes.at(-1).points[0].price,118,'toggle off');
tool.setSnapEnabled(true);tool.setMode('horizontal');svg.events.pointermove(event(160,82));render();assert.ok(svg.children.some(e=>e.attrs.class==='close-snap-guide'));
svg.events.pointerleave();render();assert.ok(!svg.children.some(e=>e.attrs.class==='close-snap-guide'),'leave clears transient guide');
tool.setMode('select');render();assert.ok(!svg.children.some(e=>e.attrs.class==='close-snap-guide'));
const id=shapes[0].id,handle={closest:()=>({getAttribute:()=>id}),hasAttribute:()=>true,getAttribute:()=> '1'};
tool.select(id);svg.events.pointerdown(event(160,80,{target:handle}));svg.events.pointermove(event(160,73));svg.events.pointerup(event(160,73));
assert.equal(shapes[0].points[1].price,125,'handle snaps to another close');
const before=structuredClone(shapes),body={closest:()=>({getAttribute:()=>id}),hasAttribute:()=>false};
svg.events.pointerdown(event(160,73,{target:body}));svg.events.pointermove(event(160,70));svg.events.pointerup(event(160,70));
assert.equal(shapes[0].points[0].price,before[0].points[0].price+3,'whole-object movement keeps geometry without snapping');
const translated=structuredClone(shapes);svg.events.pointerdown(event(160,70,{target:handle}));svg.events.pointermove(event(160,80));tool.cancel();assert.deepEqual(shapes,translated,'cancel restores exact points');
tool.destroy();console.log('Close snap: selection, hysteresis, exclusions, dotted preview, future date, exact price, Alt/toggle, handle, whole move and cancel PASS');
