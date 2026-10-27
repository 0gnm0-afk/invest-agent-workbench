// Display-only grouping must not invent missing amounts or double-count deposits.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const context={window:{},WorkbenchFormat:{money:()=>''}};
vm.runInNewContext(fs.readFileSync('dashboard/accounts/risk-workbench.js','utf8'),context);
const {composition,isEquity}=context.window.WorkbenchRisk;
const sample=()=>({valuation:{base_currency:'KRW',fx_to_base:{USD:1000}},accounts:[{alias:'a',holdings_complete:true,cash_complete:false,cash:{},positions:[{symbol:'A',currency:'USD',quantity:2,price:10},{symbol:'CMA',currency:'KRW',instrument_class_hint:'CMA_note_from_broker_label',observed_evaluation_krw:500}],observed_deposits:{KR:{dca:10,nxt_dd_dca:200,nxt2_dd_dca:300},US:{fc_dca:3,krw_dca:3000}}}]});
let b=sample(),before=JSON.stringify(b),c=composition(b);
assert.equal(c.groups[0].subtotal,20000);assert.equal(c.groups[1].subtotal,3510);assert.equal(c.total,23510);
assert.equal(c.partial,true);assert.equal(c.canChart,true);assert.equal(JSON.stringify(b),before);
b.accounts[0].cash={USD:7,KRW:8};c=composition(b);assert.equal(c.groups[1].subtotal,7508);
b=sample();delete b.valuation.fx_to_base.USD;c=composition(b);assert.equal(c.groups[0].subtotal,null);assert.equal(c.canChart,false);assert.equal(c.total,null);
b=sample();b.accounts[0].positions[0].price=null;c=composition(b);assert.equal(c.groups[0].missing,1);assert.equal(c.canChart,false);
b=sample();b.accounts.push({alias:'empty',holdings_complete:true,cash_complete:false,positions:[],observed_deposits:{KR:{dca:0},US:{fc_dca:0}}});c=composition(b,'empty');assert.equal(c.total,0);assert.equal(c.canChart,false);assert.equal(c.groups[0].subtotal,0);
assert.equal(composition(b,'missing').total,null);assert.equal(composition(null).total,null);
assert.equal(isEquity({asset_type:'bond'}),false);assert.equal(isEquity({asset_type:'adr'}),true);
b=sample();b.accounts[0].positions.push({asset_type:'bond',symbol:'BOND',currency:'KRW',market_value:100});assert.equal(composition(b).groups[1].subtotal,3610);
b=sample();b.accounts[0].observed_deposits.US.fc_dca=-5;assert.equal(composition(b).canChart,false);
console.log('account composition: classification, missing values, FX, deposit duplicates, scope, zero, negative, immutability PASS');
