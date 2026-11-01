// Validate the prepared official NH source patch with an in-memory transport.
// Usage: node tests/test_nh_mcp_contract.cjs <prepared-src> <installed-node_modules>
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const [source,modules]=process.argv.slice(2);
assert(source&&modules,'Pass prepared sources and the official MCP node_modules directory');
const ts=require(path.resolve(modules,'typescript'));
const compile=(name,replace)=>{
 let code=fs.readFileSync(path.resolve(source,name),'utf8');
 code=replace(code);
 const result=ts.transpileModule(code,{compilerOptions:{module:ts.ModuleKind.ES2022,target:ts.ScriptTarget.ES2022},reportDiagnostics:true});
 assert.equal((result.diagnostics||[]).filter(d=>d.category===ts.DiagnosticCategory.Error).length,0);
 return import('data:text/javascript;base64,'+Buffer.from(result.outputText).toString('base64'));
};
(async()=>{
 const client=await compile('client.ts',s=>s.replace('import { getAccessToken } from "./auth.js";','const getAccessToken = async () => "synthetic-token";'));
 const originalFetch=global.fetch;
 const raw='{"Output_0":{"dca":999999999999999999,"fc_dca":0.1234567890123456789},"Output_1":[],"rsp_msg":"조회가 완료되었습니다."}';
 let request;
 global.fetch=async(url,options)=>{
  request={url,options};
  return new Response(raw,{status:200,headers:{cts_flag:options.headers.cts?'N':'Y',cts:'next-cursor'}});
 };
 try{
  const config={baseUrl:'https://synthetic.invalid',appKey:'synthetic-key',appSecret:'synthetic-secret'};
  const first=await client.callRest(config,'/krstock/inquiry/v1/balance',{act_no:'SYNTH_ACCOUNT_254aa248acb4'});
  assert.equal(first._nhplug_response.raw_json,raw);
  assert.equal(first._nhplug_response.headers.cts_flag,'Y');
  assert.equal(first._nhplug_response.headers.cts,'next-cursor');
  assert.equal(first._nhplug_response.request_cts,null);
  assert(!JSON.stringify(first._nhplug_response).includes('synthetic-secret'));
  const last=await client.callRest(config,'/krstock/inquiry/v1/balance',{},'next-cursor');
  assert.equal(last._nhplug_response.request_cts,'next-cursor');
  assert.equal(last._nhplug_response.headers.cts_flag,'N');
  assert.equal(request.options.headers.cts,'next-cursor');
  const quote=await client.callRest(config,'/krstock/quote/v1/currentPrice',{});
  assert.equal(quote._nhplug_response,undefined);
 }finally{global.fetch=originalFetch;}
 const callbacks={};let called;
 global.__nhTestZod=require(path.resolve(modules,'zod')).z;
 global.__nhTestRead=async(config,path,input,cts)=>{called={path,input,cts};return {rsp_msg:'synthetic'};};
 const tools=await compile('tools.ts',s=>s
  .replace('import { z } from "zod";','const z = globalThis.__nhTestZod;')
  .replace('import { callRest } from "./client.js";','const callRest = globalThis.__nhTestRead;')
  .replace('import { loadOperations, findOperation, listDomains } from "./spec.js";',
           'const loadOperations=()=>[], findOperation=()=>undefined, listDomains=()=>[];'));
 tools.registerTools({tool:(name,description,schema,callback)=>{callbacks[name]=callback;}},{enableTrading:false});
 await callbacks.get_stock_balance({account_no:'SYNTH_ACCOUNT_254aa248acb4'});
 assert.equal(called.path,'/krstock/inquiry/v1/balance');
 assert.deepEqual(called.input,{act_no:'SYNTH_ACCOUNT_254aa248acb4',bnc_bse_cd:'1',ltg_aot_dit_cd:'9',aet_bse:'1',qut_dit_cd:'UNT',aly_qut_cd:'1'});
 delete global.__nhTestZod;delete global.__nhTestRead;
 console.log('NH MCP synthetic transport: response headers, exact raw JSON, cursor, aligned shortcut, unchanged quote PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
