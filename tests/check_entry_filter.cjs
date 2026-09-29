// Generated handler test with a minimal DOM; no browser claim.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const q={value:'',addEventListener(k,f){this.change=f}},a={checked:false,addEventListener(k,f){this.change=f}};
const rows=[{textContent:'KR 한글종목 005930',dataset:{candidate:'true'}},{textContent:'US BEST',dataset:{candidate:'false'}}];
vm.runInNewContext(script,{document:{getElementById(id){return id==='q'?q:a},querySelectorAll(){return rows}}});
let count=0;
for(const [term,active,want] of [['',false,[false,false]],['한글',false,[false,true]],['best',false,[true,false]],['',true,[false,true]],['없는종목',false,[true,true]],['BEST',true,[true,true]]]){
  q.value=term;a.checked=active;q.change();assert.deepStrictEqual(rows.map(r=>r.hidden),want);count++;
}
console.log(JSON.stringify({entry_filter_checks:count,passed:true,browser_visual_validation:false}));
