const assert=require('node:assert/strict');
global.window={};require('../dashboard/chart-workspace.js');
const host={innerHTML:''},status={textContent:''};
global.document={querySelector:s=>s==='#history-status'?status:null};
let calls=0;global.fetch=async()=>{calls++;return {ok:true,json:async()=>({revision:1,state:{drawings:[]}})}};
(async()=>{
 await window.WorkbenchWorkspace.mount({host,plot:{},stock:{bars:[{date:'2026-09-30'}]},market:'KR',symbol:'105560.KS',currency:'KRW',isCurrent:()=>true});
 assert.match(host.innerHTML,/이전 버전/);assert.match(status.textContent,/공유 미적용/);assert.equal(calls,1);
 global.fetch=async()=>{calls++;return {ok:true,json:async()=>({drawing_limit:300,revision:1,state:{drawings:[]}})}};
 await window.WorkbenchWorkspace.mount({host,plot:{},stock:{bars:[{date:'2026-09-30'}]},market:'KR',symbol:'105560.KS',currency:'KRW',isCurrent:()=>true});
 assert.match(host.innerHTML,/점선 연장 손잡이.*다시 실행/);assert.equal(calls,2);
 console.log('legacy server guard: blocked before chart editing or save');
})().catch(e=>{console.error(e);process.exit(1)});
