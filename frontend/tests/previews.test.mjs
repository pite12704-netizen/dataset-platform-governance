import test from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
const bundle=await build({stdin:{contents:"export * from './src/preview-queue'; export * from './src/preview-bootstrap';",resolveDir:process.cwd()},bundle:true,write:false,format:'esm',platform:'node'});
const {PreviewQueue,installPreviewBackpressure}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
const tick=()=>new Promise(resolve=>setImmediate(resolve));

test('24 thumbnails complete with at most two active preview requests',async()=>{
  const queue=new PreviewQueue(2);let active=0,maximum=0;
  const result=await Promise.all(Array.from({length:24},(_,i)=>queue.load('image-'+i,async()=>{
    maximum=Math.max(maximum,++active);await tick();active--;return i;
  })));
  assert.equal(maximum,2);assert.equal(result.length,24);assert.equal(active,0);
});

test('only pending requests for the same identity are shared; completion is not cached',async()=>{
  const queue=new PreviewQueue(2);let calls=0,finish;
  const firstController=new AbortController();
  const run=async()=>{calls++;return new Promise(resolve=>{finish=resolve;});};
  const first=queue.load('owner:image',run,firstController.signal);
  const second=queue.load('owner:image',run);await tick();
  firstController.abort();await assert.rejects(first,{name:'AbortError'});
  finish('image-data');assert.equal(await second,'image-data');assert.equal(calls,1);
  assert.equal(await queue.load('owner:image',async()=>{calls++;return 'new-authorized-response';}),'new-authorized-response');
  assert.equal(calls,2);
});

test('an unmounted queued image never consumes a request slot',async()=>{
  const queue=new PreviewQueue(1);let release,calls=0;
  const first=queue.load('first',()=>new Promise(resolve=>{release=resolve;}));await tick();
  const controller=new AbortController();
  const queued=queue.load('offscreen',async()=>{calls++;},controller.signal);
  controller.abort();await assert.rejects(queued,{name:'AbortError'});
  release('done');await first;await tick();assert.equal(calls,0);
  assert.equal(await queue.load('next',async()=>true),true);
});

test('503 and 429 respect Retry-After; retries are bounded and forbidden requests are not retried',async()=>{
  const delays=[];const queue=new PreviewQueue(2,3,45000,async ms=>{delays.push(ms);},()=>0);let calls=0;
  const value=await queue.load('busy',async()=>{
    calls++;if(calls<3)throw Object.assign(new Error('busy'),{status:calls===1?503:429,retryAfter:calls});return 'loaded';
  });
  assert.equal(value,'loaded');assert.deepEqual(delays,[1000,2000]);
  let forbidden=0;
  await assert.rejects(queue.load('private',async()=>{forbidden++;throw Object.assign(new Error('denied'),{status:403});}));
  assert.equal(forbidden,1);
  let attempts=0;
  await assert.rejects(queue.load('unavailable',async()=>{attempts++;throw Object.assign(new Error('busy'),{status:503});}));
  assert.equal(attempts,3);
});

test('deadline abort releases running work and does not create an endless retry loop',async()=>{
  const queue=new PreviewQueue(1,5,20);
  await assert.rejects(queue.load('slow',signal=>new Promise((_,reject)=>{
    signal.addEventListener('abort',()=>reject(signal.reason),{once:true});
  })),{name:'TimeoutError'});
  assert.equal(await queue.load('after',async()=>42),42);
});

test('legacy gallery hotfix preserves identity and body while retrying preview-only requests',async()=>{
  const delays=[],calls=[];
  const target={location:{href:'https://platform.test/image-pilot/v',origin:'https://platform.test'},fetch:async(input,init)=>{
    calls.push({input,init});
    if(String(input).endsWith('/preview')&&calls.filter(c=>String(c.input).endsWith('/preview')).length===1)
      return new Response(JSON.stringify({message:'busy'}),{status:503,headers:{'Retry-After':'1','Content-Type':'application/json'}});
    return new Response(JSON.stringify({kind:'image',content:'synthetic'}),{headers:{'Content-Type':'application/json','Content-Encoding':'gzip'}});
  }};
  installPreviewBackpressure(target,new PreviewQueue(2,3,45000,async ms=>{delays.push(ms);},()=>0));
  const response=await target.fetch('/api/v1/assets/a/preview',{credentials:'include',headers:{'X-Requested-With':'dataset-platform'}});
  assert.equal((await response.json()).content,'synthetic');assert.equal(response.headers.get('Content-Encoding'),null);
  assert.deepEqual(delays,[1000]);assert.ok(calls.every(c=>c.init.credentials==='include'));
  const externalInit={method:'POST',credentials:'omit'};
  await target.fetch('/api/v1/auth/login',externalInit);assert.equal(calls.at(-1).init,externalInit);
  await target.fetch('https://other.test/api/v1/assets/a/preview',externalInit);assert.equal(calls.at(-1).init,externalInit);
});

test('legacy hotfix returns permission failures without masking them or retrying',async()=>{
  let calls=0;
  const target={location:{href:'https://platform.test/',origin:'https://platform.test'},fetch:async()=>{
    calls++;return new Response(JSON.stringify({message:'no permission'}),{status:403});
  }};
  installPreviewBackpressure(target);
  const response=await target.fetch('/api/v1/assets/private/preview');
  assert.equal(response.status,403);assert.equal((await response.json()).message,'no permission');assert.equal(calls,1);
});
