import test from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
const bundle=await build({entryPoints:['src/agent-progress.ts'],bundle:true,write:false,format:'esm',platform:'node'});
const {watchAction}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
const tick=()=>new Promise(r=>setImmediate(r));
const snap=(status='running',progress=30)=>({action_id:'action-a',job_id:'job-a',status,progress});
class Events{
  static last;
  readyState=1;handlers={};onerror;
  constructor(url){this.url=url;Events.last=this;}
  addEventListener(name,callback){this.handlers[name]=callback;}
  close(){this.readyState=2;}
  emit(name,value){this.handlers[name]?.({data:JSON.stringify(value)});}
}
function source(t,value){const previous=Object.getOwnPropertyDescriptor(globalThis,'EventSource');Object.defineProperty(globalThis,'EventSource',{configurable:true,value});t.after(()=>{if(previous)Object.defineProperty(globalThis,'EventSource',previous);else delete globalThis.EventSource;});}
test('snapshot stream recovers via read-only status, hides content on revocation and closes',async t=>{
  source(t,Events);const requests=[],values=[],states=[];
  t.mock.method(globalThis,'fetch',async(url,init)=>{requests.push({url,method:init.method||'GET'});return new Response(JSON.stringify(snap('running',50)),{status:200});});
  const stop=watchAction('action-a',v=>values.push(v),v=>states.push(v));t.after(stop);
  const stream=Events.last;assert.ok(!stream.url.includes('token'));
  stream.emit('snapshot',snap());assert.equal(values.at(-1).progress,30);
  stream.onerror();await tick();await tick();assert.equal(values.at(-1).progress,50);assert.equal(requests[0].method,'GET');
  stream.emit('access_lost',{});assert.equal(values.at(-1),undefined);assert.equal(states.at(-1),'forbidden');assert.equal(stream.readyState,2);
  stream.emit('snapshot',snap('completed',100));assert.equal(values.at(-1),undefined);
});
test('terminal snapshot closes once, ignores a different action and releases on unmount',t=>{
  source(t,Events);const values=[];const stop=watchAction('action-a',v=>values.push(v),()=>{});t.after(stop);
  Events.last.emit('snapshot',{...snap(),action_id:'other'});assert.equal(values.length,0);
  Events.last.emit('snapshot',snap('completed',100));assert.equal(values.length,1);assert.equal(Events.last.readyState,2);
  stop();Events.last.emit('snapshot',snap());assert.equal(values.length,1);
});
test('REST fallback can restore a completed task without creating new work',async t=>{
  source(t,undefined);const values=[];let calls=0;
  t.mock.method(globalThis,'fetch',async()=>{calls++;return new Response(JSON.stringify(snap('completed',100)),{status:200});});
  const stop=watchAction('action-a',v=>values.push(v),()=>{});t.after(stop);await tick();await tick();
  assert.equal(values[0].status,'completed');assert.equal(calls,1);
});
test('REST fallback stops and clears stale progress when identity expires',async t=>{
  source(t,undefined);const values=[],states=[];
  t.mock.method(globalThis,'fetch',async()=>new Response(JSON.stringify({message:'expired'}),{status:401}));
  const stop=watchAction('action-a',v=>values.push(v),v=>states.push(v));t.after(stop);await tick();await tick();
  assert.equal(values.at(-1),undefined);assert.equal(states.at(-1),'forbidden');
});
