'use strict';
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');global.window={};
vm.runInThisContext(fs.readFileSync('dashboard/trailing.js','utf8'));
const p={currency:'USD',prices:[{date:'2026-10-01',close:100},{date:'2026-10-02',close:95}],history:[{date:'2026-09-30',line:92},{date:'2026-10-01',line:95},{date:'2026-10-02',line:96}]};
const s=window.WorkbenchTrailing.series(p);assert.equal(s.length,2);assert.deepEqual(s.map(x=>x.label),['종가','최종 트레일링']);assert.equal(s[1].data.length,2);assert.equal(s[0].currency,'USD');assert.ok(!s.some(x=>/평단|MA5/.test(x.label)));
const app=fs.readFileSync('dashboard/app.js','utf8');assert.ok(app.includes("page==='risk'"));assert.ok(!app.includes('기존 위험관리'));assert.ok(!fs.readFileSync('dashboard/risk-workbench.js','utf8').includes('data-calculate'));
console.log('trailing UI: exactly two series, visible range, currency, route and retired editor PASS');
