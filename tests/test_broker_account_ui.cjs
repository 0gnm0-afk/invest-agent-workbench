// Exercise the actual account-card renderer with synthetic saved-provider data.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('dashboard/shell/app.js','utf8');
const start=source.indexOf('function accountCards() {'),end=source.indexOf('\nfunction ',start+1);
assert(start>=0&&end>start);
const context={DATA:{accounts:{as_of:'combined-old',query_state:'partial',accounts:[
 {alias:'nh-synthetic',provider:'nh',as_of:'nh-time',query_state:'received',stale:false,positions:[],holdings_complete:true,cash_complete:false},
 {alias:'meritz-synthetic',provider:'meritz',as_of:'meritz-old',query_state:'failed',stale:true,positions:[],holdings_complete:false,cash_complete:false}
]}},state:{partial:'일부 확인',received:'조회 완료',failed:'조회 실패'},
esc:x=>String(x??''),tag:x=>x,stamp:x=>x,WorkbenchRisk:{isEquity:()=>true}};
vm.createContext(context);vm.runInContext(source.slice(start,end),context);
const html=context.accountCards();
assert(html.includes('NH 계좌 1'));assert(html.includes('메리츠 계좌 2'));
assert(html.includes('조회 실패'));assert(html.includes('24시간 경과'));
assert(html.includes('잔고 기준 nh-time'));assert(html.includes('잔고 기준 meritz-old'));
assert(html.includes('잔고 일부 미확인'));
const quantityStart=source.indexOf('function accountQuantity('),quantityEnd=source.indexOf('\nfunction ',quantityStart+1);
vm.runInContext(source.slice(quantityStart,quantityEnd),context);
assert.equal(context.accountQuantity('0.SYNTH_ACCOUNT_c775e7b757ed'),'0.123456789');
assert.equal(context.accountQuantity('999999999999999999'),'999,999,999,999,999,999');
assert.equal(context.accountQuantity(null),'미확인');
const detailStart=source.indexOf('function accountDetail('),detailEnd=source.indexOf('\nasync function route',detailStart);
let detailHTML='';Object.assign(context,{$:()=>({set innerHTML(value){detailHTML=value;}}),
 heading:()=>'',accountWorkspace:()=>'',block:(title,body)=>body,stockLink:()=>'',money:()=>''});
context.DATA.accounts.accounts[1].positions=[{market:'US',symbol:'SYN',currency:'USD',quantity:'0.SYNTH_ACCOUNT_c775e7b757ed'}];
vm.runInContext(source.slice(detailStart,detailEnd),context);context.accountDetail('meritz-synthetic');
assert(detailHTML.includes('0.123456789'));assert(detailHTML.includes('정수 수량만 지원'));
console.log('Saved provider account cards: provider, own timestamp, failure and stale state PASS');
