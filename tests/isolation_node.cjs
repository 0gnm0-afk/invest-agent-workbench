'use strict';
// C8 preload: install before the selected Node fixture is loaded.
const fs = require('node:fs');
const path = require('node:path');
const {fileURLToPath} = require('node:url');
const net = require('node:net');
const dns = require('node:dns');
const dgram = require('node:dgram');
const child = require('node:child_process');
const worker = require('node:worker_threads');
const Module = require('node:module');
const original = {read: fs.readFileSync, write: fs.writeFileSync, append: fs.appendFileSync,
  exists: fs.existsSync, realpath: fs.realpathSync.native, stat: fs.lstatSync};
const config = JSON.parse(original.read(process.env.M00_CONFIG, 'utf8'));
const events = [];
let internal = false;
function deny(category, operation) {
  const event = {node: config.node, category, operation};
  events.push(event);
  original.append(config.events, JSON.stringify(event) + '\n');
  const error = new Error('isolation_denied:' + category + ':' + operation);
  error.code = 'EACCES';
  throw error;
}
function normalize(value) {
  if (value instanceof URL) value = fileURLToPath(value);
  if (Buffer.isBuffer(value)) value = value.toString();
  if (typeof value !== 'string') return deny('filesystem', 'unknown_path');
  let full = path.resolve(value), suffix = [];
  while (!original.exists(full)) {
    const parent = path.dirname(full);
    if (parent === full) break;
    suffix.unshift(path.basename(full)); full = parent;
  }
  if (original.exists(full)) full = original.realpath(full);
  return path.join(full, ...suffix).toLowerCase();
}
function inside(value, root) { return value === root || value.startsWith(root + path.sep); }
const workspace = normalize(config.workspace);
const protectedPaths = config.protected.map(normalize);
const fds = new Set([0, 1, 2]);
function check(value, write = false) {
  if (internal) return;
  if (value instanceof URL) value = fileURLToPath(value);
  if (typeof value === 'number') {
    if (fds.has(value)) return;
    return deny('filesystem', 'unknown_fd');
  }
  const raw = String(value);
  if (/^(\\\\|\/\/)/.test(raw) || raw.slice(2).includes(':')) deny('filesystem', 'device_or_stream');
  if(!inside(path.resolve(raw).toLowerCase(),workspace))deny('filesystem','outside_workspace');
  const full = normalize(value);
  if (protectedPaths.some(p => inside(full, p) || (write && inside(p, full))) || !inside(full, workspace)) deny('filesystem', write ? 'write' : 'read');
}
// Cover sync, callback and promises APIs, including enumeration and metadata.
const readers = ['readFile','readdir','stat','statfs','lstat','realpath','access','readlink','opendir','openAsBlob','exists'];
const writers = ['writeFile','appendFile','truncate','unlink','rm','rmdir','mkdir','mkdtemp','mkdtempDisposable','chmod','chown','utimes','lchmod','lchown','lutimes'];
for (const [names, writing] of [[readers, false], [writers, true]]) {
  for (const name of names) {
    for (const suffix of ['', 'Sync']) {
      const key = name + suffix;
      if (fs[key]) { const fn = fs[key]; fs[key] = function(value, ...args) {check(value, writing);if(['rm','rmdir','readdir','opendir'].includes(name) && args[0]?.recursive)deny('filesystem','recursive'); return fn.call(this, value, ...args);}; }
    }
    if (fs.promises[name]) {const fn = fs.promises[name]; fs.promises[name] = async function(value, ...args) {check(value, writing);if(['rm','rmdir','readdir','opendir'].includes(name) && args[0]?.recursive)deny('filesystem','recursive'); return fn.call(this, value, ...args);};}
  }
}
for(const key of ['glob','globSync'])if(fs[key])fs[key]=()=>deny('filesystem','glob');
if(fs.promises.glob)fs.promises.glob=()=>deny('filesystem','glob');
if(fs.promises.watch)fs.promises.watch=()=>deny('filesystem','watch');
fs.realpathSync.native=value=>{check(value);return original.realpath(value);};
for (const name of ['rename', 'copyFile', 'cp']) {
  for (const suffix of ['', 'Sync']) {
    const fn = fs[name + suffix];
    if (fn) fs[name + suffix] = function(a,b,...args) {check(a,true);check(b,true);if(name==='cp')deny('filesystem','recursive_copy');return fn.call(this,a,b,...args);};
  }
  const fn = fs.promises[name];
  if (fn) fs.promises[name] = async function(a,b,...args) {check(a,true);check(b,true);if(name==='cp')deny('filesystem','recursive_copy');return fn.call(this,a,b,...args);};
}
for (const name of ['link','symlink']) {
  for (const suffix of ['', 'Sync']) if (fs[name + suffix]) fs[name + suffix] = () => deny('filesystem','link');
  fs.promises[name] = async () => deny('filesystem','link');
}
const openSync = fs.openSync, open = fs.open;
fs.openSync = function(value, flags, ...args) {check(value, flags !== 'r');const fd=openSync.call(this,value,flags,...args);fds.add(fd);return fd;};
fs.open = function(value, flags, ...args) {
  check(value, flags !== 'r'); const cb=args.pop();
  return open.call(this,value,flags,...args,(error,fd)=>{if (!error) fds.add(fd);cb(error,fd);});
};
const promiseOpen = fs.promises.open;
fs.promises.open = async function(value, flags, ...args) {check(value,flags !== 'r');return promiseOpen.call(this,value,flags,...args);};
for (const name of ['createReadStream','createWriteStream','watch','watchFile','existsSync']) {
  const fn=fs[name];fs[name]=function(value,...args) {check(value,name === 'createWriteStream');return fn.call(this,value,...args);};
}
const ports = new Set();
const listen = net.Server.prototype.listen;
net.Server.prototype.listen = function(...args) {
  const options = typeof args[0] === 'object' ? args[0] : {port:args[0], host:args[1]};
  if (options.host !== '127.0.0.1' || options.port !== 0 || options.path || options.fd) deny('network','bind');
  this.once('listening',()=>{const port=this.address().port;ports.add(port);this.once('close',()=>ports.delete(port));});
  return listen.apply(this,args);
};
const connect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function(...args) {
  let first=args[0];if(Array.isArray(first))first=first[0];
  const options=typeof first === 'object' ? first : {port:first,host:args[1]};
  if (options.host !== '127.0.0.1' || !ports.has(Number(options.port)) || options.path || options.fd) deny('network','connect');
  return connect.apply(this,args);
};
for (const name of Object.keys(dns)) if (/^(lookup|resolve|reverse)/.test(name) && typeof dns[name] === 'function') {
  const fn=dns[name];dns[name]=function(host,...args){if(name !== 'lookup' || host !== '127.0.0.1')deny('network','dns');return fn.call(this,host,...args);};
}
for (const name of Object.keys(dns.promises)) if (/^(lookup|resolve|reverse)/.test(name) && typeof dns.promises[name] === 'function') {
  const fn=dns.promises[name];dns.promises[name]=async function(host,...args){if(name !== 'lookup' || host !== '127.0.0.1')deny('network','dns');return fn.call(this,host,...args);};
}
for(const owner of [dns,dns.promises])owner.Resolver=class {constructor(){deny('network','resolver');}};
dgram.createSocket=()=>deny('network','datagram');
for (const name of ['spawn','spawnSync','exec','execSync','execFile','execFileSync','fork']) child[name]=()=>deny('process',name);
worker.Worker=class {constructor(){deny('process','worker');}};
process.dlopen=()=>deny('native','dlopen');
process.binding=()=>deny('native','binding');
process._linkedBinding=()=>deny('native','linked_binding');
process.kill=()=>deny('process','kill');
const load=Module._load;
const forbiddenBuiltin=name=>['sqlite','inspector','inspector/promises','wasi','v8'].includes(name.replace(/^node:/,''));
Module._load=function(name,...args){if(forbiddenBuiltin(name))deny('native','unverified_builtin');return load.call(this,name,...args);};
if(typeof Module.registerHooks !== 'function')throw new Error('esm_guard_unavailable');
Module.registerHooks({resolve(name,context,next){
  if(forbiddenBuiltin(name))deny('native','unverified_esm_builtin');
  const result=next(name,context);
  if(result.url.startsWith('file:'))check(new URL(result.url));
  return result;
}});
if(process.getBuiltinModule)process.getBuiltinModule=(name)=>Module._load(name.startsWith('node:')?name:'node:'+name);
const chdir=process.chdir;process.chdir=function(value){check(value);return chdir.call(this,value);};
const extension=Module._extensions['.js'];
Module._extensions['.js']=function(module,filename){check(filename);return extension(module,filename);};
Module._extensions['.node']=()=>deny('native','addon');
Module.syncBuiltinESMExports();
let finalResult;
const writeResult = (status, reason='') => {
  finalResult={node:config.node,language:'node',status,reason,executed:true,version:process.version};
  original.write(config.result, JSON.stringify(finalResult));
};
globalThis.M00 = {config, events, deny, writeResult};
if(config.probe === 'not_ready') process.exit(2);
original.write(config.ready, JSON.stringify({token:config.token,guard:'ready'}));
const deadline=Date.now()+15000;
while(!original.exists(config.go)) {
  if(Date.now()>deadline)process.exit(2);
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)),0,0,10);
}
if(original.read(config.go,'utf8') !== config.token) process.exit(2);
process.on('exit',code=>{
  if(events.length)writeResult('blocked','guard_violation');
  else if(code !== 0)writeResult('failed','nonzero_exit');
  else if(!finalResult)writeResult('passed');
});
process.on('uncaughtException',()=>{writeResult(events.length?'blocked':'failed','uncaught_exception');process.exitCode=1;});
process.on('unhandledRejection',()=>{writeResult(events.length?'blocked':'failed','unhandled_rejection');process.exitCode=1;});
