'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const net=require('node:net');
const {config, events, writeResult}=globalThis.M00;
async function run() {
  const file=path.join(config.workspace,'protected','sentinel.txt');
  if(config.probe) {
    switch(config.probe) {
      case 'read':fs.readFileSync(file);break;
      case 'write':fs.writeFileSync(file,'changed');break;
      case 'truncate':fs.truncateSync(file,0);break;
      case 'recursive':fs.rmSync(config.workspace,{recursive:true});break;
      case 'recursive_read':fs.readdirSync(config.workspace,{recursive:true});break;
      case 'glob':fs.globSync('**/*',{cwd:config.workspace});break;
      case 'mkdtemp':fs.mkdtempSync(path.join(path.dirname(file),'new-'));break;
      case 'enumerate':fs.readdirSync(path.dirname(file));break;
      case 'delete':fs.unlinkSync(file);break;
      case 'rename':fs.renameSync(file,path.join(config.temp,'moved'));break;
      case 'escape':fs.readFileSync(path.join(config.temp,'..','protected','sentinel.txt'));break;
      case 'link':fs.readFileSync(path.join(config.workspace,'protected-link','sentinel.txt'));break;
      case 'drive':fs.readFileSync('Z:/M00-synthetic-nonexistent/sentinel.txt');break;
      case 'dns':require('node:dns').lookup('m00.invalid',()=>{});break;
      case 'resolver':new (require('node:dns').Resolver)().resolve4('m00.invalid',()=>{});break;
      case 'reverse_dns':require('node:dns').reverse('127.0.0.1',()=>{});break;
      case 'esm_native':await import('node:sqlite');break;
      case 'connect':net.connect({host:'192.0.2.1',port:443});break;
      case 'unowned':net.connect({host:'127.0.0.1',port:9});break;
      case 'bind':net.createServer().listen({host:'0.0.0.0',port:0});break;
      case 'credentials':process.binding('fs');break;
      case 'registry':require('node:child_process').execFileSync('reg',['query','HKCU\\Environment']);break;
      case 'process':require('node:child_process').spawn('powershell',[]);break;
      case 'swallow':try{fs.readFileSync(file);}catch(error){assert.equal(error.code,'EACCES');}break;
      case 'ready':break;
      default:throw new Error('unknown_fixed_probe');
    }
  } else {
    const fixture=path.join(config.temp,'node-fixture.txt');
    await fs.promises.writeFile(fixture,'synthetic');
    assert.equal(await fs.promises.readFile(fixture,'utf8'),'synthetic');
    const server=net.createServer(socket=>socket.end('fixture'));
    await new Promise((resolve,reject)=>{server.once('error',reject);server.listen({host:'127.0.0.1',port:0},resolve);});
    try {
      const value=await new Promise((resolve,reject)=>{
        let data='';const client=net.connect({host:'127.0.0.1',port:server.address().port});
        client.on('data',chunk=>data+=chunk);client.on('end',()=>{client.destroy();resolve(data);});client.on('error',reject);
      });
      assert.equal(value,'fixture');
    } finally {await new Promise(resolve=>server.close(resolve));}
    assert.equal(events.length,0);
  }
  // A normal registered Node script needs no custom reporting protocol: the
  // preload owns exit accounting. Negative subruns also emit an explicit state.
  if(config.probe)writeResult(events.length?'blocked':'passed',events.length?'guard_violation':'');
  process.exitCode=events.length?1:0;
}
run().catch(()=>{writeResult(events.length?'blocked':'failed','node_fixture_failure');process.exitCode=1;});
