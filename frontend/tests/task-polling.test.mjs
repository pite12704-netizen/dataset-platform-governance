import test from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';

const compiled=await build({stdin:{contents:"export * from './src/task-polling'; export {ApiError} from './src/api';",resolveDir:process.cwd()},
  bundle:true,write:false,format:'esm',platform:'node'});
const {taskPolling,ApiError}=await import('data:text/javascript;base64,'+Buffer.from(compiled.outputFiles[0].text).toString('base64'));
const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};

test('slow refresh requests are coalesced and never overlap',async()=>{
  let finish,calls=0,active=0,maximum=0;const values=[];
  const poll=taskPolling({load:()=>{calls++;maximum=Math.max(maximum,++active);return new Promise(resolve=>{finish=value=>{active--;resolve(value);};});},
    onValue:value=>values.push(value),onError:()=>{},shouldPoll:()=>false});
  const first=poll.refresh();await flush();const second=poll.refresh();
  assert.equal(calls,1);assert.equal(first,second);finish({status:'completed'});await first;
  assert.equal(maximum,1);assert.equal(values.length,1);poll.stop();
});

test('503 preserves last success and blocks manual refresh until Retry-After expires',async t=>{
  t.mock.timers.enable({apis:['setTimeout','Date']});let calls=0;const rows=[],failures=[];
  const poll=taskPolling({load:async()=>{if(++calls===1)return {items:[{id:'known'}]};throw new ApiError('unavailable',503,20);},
    onValue:value=>rows.push(value),onError:(error,delay)=>failures.push(delay),shouldPoll:()=>false});
  await poll.refresh();await poll.refresh();
  assert.equal(failures[0],20000);assert.equal(rows.at(-1).items[0].id,'known');
  await poll.refresh();assert.equal(calls,2);
  t.mock.timers.tick(19999);await flush();assert.equal(calls,2);
  t.mock.timers.tick(1);await flush();assert.equal(calls,3);assert.equal(rows.length,1);poll.stop();
});

test('authentication errors stop automatic polling; late results after stop do not update UI',async t=>{
  t.mock.timers.enable({apis:['setTimeout','Date']});let calls=0,finish;const values=[];
  const denied=taskPolling({load:async()=>{calls++;throw new ApiError('login expired',401);},onValue:()=>{},onError:()=>{},shouldPoll:()=>true});
  await denied.refresh();t.mock.timers.tick(60000);await flush();assert.equal(calls,1);denied.stop();
  const stopped=taskPolling({load:()=>new Promise(resolve=>{finish=resolve;}),onValue:value=>values.push(value),onError:()=>{},shouldPoll:()=>true});
  const request=stopped.refresh();await flush();stopped.stop();finish({status:'old'});await request;assert.equal(values.length,0);
});

test('successful running tasks schedule one next poll and completion stops it',async t=>{
  t.mock.timers.enable({apis:['setTimeout','Date']});let calls=0;
  const poll=taskPolling({load:async()=>({active:++calls<2}),onValue:()=>{},onError:()=>{},shouldPoll:value=>value.active});
  await poll.refresh();t.mock.timers.tick(3999);await flush();assert.equal(calls,1);
  t.mock.timers.tick(1);await flush();assert.equal(calls,2);
  t.mock.timers.tick(60000);await flush();assert.equal(calls,2);poll.stop();
});
