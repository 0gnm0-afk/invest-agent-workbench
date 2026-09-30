// Validate the generated phase/search handler against a minimal DOM.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const q={value:'',addEventListener(k,f){this.change=f}},p={value:'candidates',addEventListener(k,f){this.change=f}};
const rows=[{textContent:'KR 한미약품',dataset:{phase:'watch'}},{textContent:'US ROKU',dataset:{phase:'ready'}},{textContent:'US ENTRY',dataset:{phase:'entry'}},{textContent:'US EXCLUDED',dataset:{phase:'excluded'}}];
vm.runInNewContext(script,{document:{getElementById(id){return id==='q'?q:p},querySelectorAll(){return rows}}});
let checks=0;
for(const [term,phase,want] of [['','candidates',[false,false,false,true]],['','ready',[true,false,true,true]],['','watch',[false,true,true,true]],['','entry',[true,true,false,true]],['','excluded',[true,true,true,false]],['','all',[false,false,false,false]],['한미','candidates',[false,true,true,true]],['roku','candidates',[true,false,true,true]],['ZZZZ','all',[true,true,true,true]]]){q.value=term;p.value=phase;q.change();assert.deepStrictEqual(rows.map(x=>x.hidden),want);checks++;}
console.log(JSON.stringify({phase_filter_checks:checks,passed:true,browser_visual_validation:false}));
