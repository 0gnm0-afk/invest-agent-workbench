const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
global.window=global;
for(const f of ['format.js','drawing-tools.js'])vm.runInThisContext(fs.readFileSync(path.join(__dirname,'../dashboard',f),'utf8'));
const bars=Array.from({length:10},(_,i)=>({date:`2026-09-${String(i+1).padStart(2,'0')}`}));
assert.deepEqual(DrawingTools.channel([{x:0,y:100},{x:20,y:80},{x:10,y:70}]),[{x:0,y:100},{x:20,y:80},{x:20,y:60},{x:0,y:80}]);
assert.match(DrawingTools.measure([{time:bars[0].date,price:1000},{time:bars[5].date,price:1200}],bars,'USD'),/\+200.00 USD · \+20.00% · 5봉 간격 · 5일/);
assert.match(DrawingTools.measure([{time:bars[2].date,price:1200},{time:bars[0].date,price:1000}],bars,'KRW'),/-200 KRW · -16.67% · 2봉 간격 · 2일/);
class Element {
  constructor(){this.events={};this.attrs={};this.style={};this.classList={add(){}};}
  append(child){this.child=child;}setAttribute(k,v){this.attrs[k]=v;}getAttribute(k){return this.attrs[k];}
  replaceChildren(){}addEventListener(name,fn){this.events[name]=fn;}remove(){}
  getBoundingClientRect(){return {left:0,top:0,width:100,height:200};}
  setPointerCapture(){}hasPointerCapture(){return true;}releasePointerCapture(){}
}
global.document={createElementNS:()=>new Element()};global.requestAnimationFrame=()=>1;global.cancelAnimationFrame=()=>{};
let shapes=[],commits=0,lastBefore;
const el=new Element(),chart={timeScale:()=>({width:()=>100,coordinateToLogical:x=>x/10,logicalToCoordinate:i=>i*10}),panes:()=>[{getHeight:()=>200}]};
const drawing=DrawingTools.attach({element:el,chart,series:{coordinateToPrice:y=>200-y,priceToCoordinate:p=>200-p},bars,currency:'KRW',getDrawings:()=>shapes,
 onChange:(next,options={})=>{shapes=next;if(!options.preview){commits++;lastBefore=options.before;}},onSelect(){},onHint(){}});
const svg=el.child;
function event(x,y,target={closest:()=>null}){return {clientX:x,clientY:y,button:0,pointerId:1,target,preventDefault(){},stopPropagation(){}};}
for(const kind of ['trend','channel','horizontal','box','measure']) {
 drawing.setMode(kind);
 [[10,100],[30,80],[20,60]].slice(0,DrawingTools.count[kind]).forEach(([x,y])=>svg.events.pointerdown(event(x,y)));
 assert.equal(shapes.at(-1).kind,kind);
}
assert.equal(commits,5);
const id=shapes[0].id,original=JSON.parse(JSON.stringify(shapes[0]));drawing.select(id);
const handle={closest:()=>({getAttribute:()=>id}),hasAttribute:()=>true,getAttribute:()=> '1'};
svg.events.pointerdown(event(30,80,handle));svg.events.pointermove(event(40,70));svg.events.pointerup(event(40,70));
assert.deepEqual(shapes[0].points,[original.points[0],{time:bars[4].date,price:130}]);assert.deepEqual(lastBefore[0],original);
// Whole-object translation keeps both anchors' distances; cancellation restores it.
const body={closest:()=>({getAttribute:()=>id}),hasAttribute:()=>false};
const before=JSON.parse(JSON.stringify(shapes));
svg.events.pointerdown(event(20,90,body));svg.events.pointermove(event(30,80));svg.events.pointerup(event(30,80));
assert.equal(shapes[0].points[0].price,before[0].points[0].price+10);
assert.equal(shapes[0].points[1].price,before[0].points[1].price+10);
const translated=JSON.parse(JSON.stringify(shapes));
svg.events.pointerdown(event(20,90,body));svg.events.pointermove(event(30,80));drawing.cancel();assert.deepEqual(shapes,translated);
drawing.setMode('box');svg.events.pointerdown(event(20,100));drawing.cancel();assert.equal(shapes.length,5);
drawing.destroy();console.log('All five tools, channel geometry, KRW/USD measurement, handle/whole drag and cancellation: PASS');
