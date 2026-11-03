// 仅合成本地文件与内存 API，不连接业务服务或模型。
import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {build} from 'esbuild';

const bundle=await build({entryPoints:['src/directory-import.ts'],bundle:true,write:false,format:'esm',platform:'node'});
const {selectDirectory,prepareDirectory,transferDirectory}=await import('data:text/javascript;base64,'+
  Buffer.from(bundle.outputFiles[0].text).toString('base64'));
const config={max_upload_bytes:20*1024**2};
const sha=bytes=>createHash('sha256').update(bytes).digest('hex');
const digest=async file=>sha(Buffer.from(await file.arrayBuffer()));
const identity={ownerId:'owner',versionId:'version',createKey:'stable-create-key',sourceId:'browser-test',source:'合成测试',license:'测试许可'};
const signal=()=>new AbortController().signal;
const file=(path,content='synthetic')=>{
  const value=new File([content],path.split('/').pop());
  Object.defineProperty(value,'webkitRelativePath',{value:path});return value;
};
const prepare=(selection,previous)=>prepareDirectory(selection,previous??identity,digest,signal(),()=>{},previous);

function backend(fault=()=>{}) {
  const records=new Map(),uploads=new Map(),parts=[],pages=[],creates=[],state={saved:undefined,finalized:0};
  let batch;
  const call=async(path,init={})=>{
    const body=typeof init.body==='string'?JSON.parse(init.body):init.body;
    const failure=fault({path,body,init,state});if(failure)throw failure;
    if(path==='/imports') {
      assert.ok(state.saved,'必须先持久化幂等身份');creates.push(body.idempotency_key);
      batch??={...body,id:'batch',status:'uploading',sealed:false};
      return {...batch,token:'must-not-be-persisted'};
    }
    if(path==='/imports/batch')return {...batch};
    if(path==='/imports/batch/items') {
      pages.push(body.items.length);
      for(const entry of body.items) {
        assert.equal('path' in entry,false,'本地定位与服务器清单分离');
        const old=records.get(entry.external_id);
        if(old)assert.deepEqual({...old,id:undefined},{...entry,id:undefined});
        else records.set(entry.external_id,{...entry,id:'item-'+records.size});
      }
      return {items:body.items.map(entry=>records.get(entry.external_id))};
    }
    if(path.endsWith('/upload')) {
      const itemId=path.split('/')[4];
      if(!uploads.has(itemId))uploads.set(itemId,{id:itemId,status:'uploading',part_size:1024,parts:{}});
      return structuredClone(uploads.get(itemId));
    }
    if(init.method==='PUT') {
      const [, ,uploadId,,number]=path.split('/');
      const block=Buffer.from(body);
      uploads.get(uploadId).parts[number]={size:block.length,sha256:sha(block)};
      parts.push({uploadId,number});return {};
    }
    if(path.endsWith('/complete')) {uploads.get(path.split('/')[2]).status='completed';return {asset_id:'asset'};}
    if(path.endsWith('/finalize')) {state.finalized++;batch.sealed=true;batch.status='completed';return {...batch};}
    throw new Error('unexpected '+path);
  };
  return {call,records,uploads,parts,pages,creates,state,save:async value=>{state.saved=structuredClone(value);}};
}

test('folder selection preserves Unicode paths and reports invalid and excluded files',()=>{
  const selection=selectDirectory([file('原图/a/same.JPG'),file('原图/b/same.JPG'),file('原图/readme.txt'),
    file('原图/empty.png',''),file('原图/../escape.png')],config);
  assert.deepEqual(selection.files.map(item=>item.path),['a/same.JPG','b/same.JPG']);
  assert.equal(selection.skipped.length,1);assert.equal(selection.blocked.length,2);
});

test('60000 metadata records can be selected without reading all image contents',()=>{
  const files=Array.from({length:60000},(_,i)=>({name:'image.jpg',size:1024,webkitRelativePath:`root/${i}/image.jpg`,
    arrayBuffer(){throw new Error('must not read image during selection');}}));
  const selection=selectDirectory(files,config);
  assert.equal(selection.files.length,60000);assert.equal(selection.bytes,60000*1024);
});

test('manifest has stable distinct identities for same filenames; changed content cannot resume',async()=>{
  const files=[file('root/a/same.png'),file('root/b/same.png')];
  const selection=selectDirectory(files,config),draft=await prepare(selection);
  assert.notEqual(draft.entries[0].external_id,draft.entries[1].external_id);
  assert.equal((await prepare(selection,draft)).fingerprint,draft.fingerprint);
  const renamedRoot=selectDirectory([file('other/a/same.png'),file('other/b/same.png')],config);
  assert.equal((await prepare(renamedRoot,draft)).fingerprint,draft.fingerprint);
  await assert.rejects(prepare(selectDirectory([file('root/a/same.png','changed'),files[1]],config),draft),/原批次不一致/);
  await assert.rejects(prepareDirectory(selection,{...draft,ownerId:'other'},digest,signal(),()=>{},draft),/用户、版本/);
});

test('103 images paginate at 100 and resume after acknowledged part without duplicate assets',async()=>{
  const selection=selectDirectory(Array.from({length:103},(_,i)=>file(`root/${i}/same.png`)),config);
  const draft=await prepare(selection),server=backend();let interrupted=false;
  const call=async(path,init)=>{
    const response=await server.call(path,init);
    if(init?.method==='PUT'&&!interrupted){interrupted=true;throw new Error('process closed after server accepted part');}
    return response;
  };
  await assert.rejects(transferDirectory(draft,selection,call,server.save,signal(),()=>{}),/process closed/);
  const resumed=await prepare(selection,server.state.saved);
  assert.equal((await transferDirectory(resumed,selection,call,server.save,signal(),()=>{})).status,'completed');
  assert.deepEqual(server.pages,[100,100,3]);assert.equal(server.parts.length,103);
  assert.equal(server.records.size,103);assert.equal(server.uploads.size,103);assert.equal(server.creates.length,1);
  assert.equal(JSON.stringify(server.state.saved).includes('must-not-be-persisted'),false);
});

test('storage failure prevents network side effects and pause prevents further files',async()=>{
  const selection=selectDirectory([file('root/a.png'),file('root/b.png')],config),draft=await prepare(selection),server=backend();
  await assert.rejects(transferDirectory(draft,selection,server.call,async()=>{throw new Error('quota exceeded');},signal(),()=>{}),/quota/);
  assert.equal(server.creates.length,0);
  const controller=new AbortController();
  await assert.rejects(transferDirectory(draft,selection,server.call,server.save,controller.signal,count=>{
    if(count===1)controller.abort();
  }),{name:'AbortError'});
  assert.equal(server.uploads.size,1);assert.equal(server.state.finalized,0);
});

test('lost creation response reuses the durable key and permission failure does not finalize',async()=>{
  const selection=selectDirectory([file('root/a.png')],config),draft=await prepare(selection),server=backend();let lost=false;
  const call=async(path,init)=>{
    const response=await server.call(path,init);
    if(path==='/imports'&&!lost){lost=true;throw new Error('response lost');}return response;
  };
  await assert.rejects(transferDirectory(draft,selection,call,server.save,signal(),()=>{}),/response lost/);
  assert.equal(server.state.saved.batchId,undefined);
  await transferDirectory(server.state.saved,selection,call,server.save,signal(),()=>{});
  assert.deepEqual(server.creates,['stable-create-key','stable-create-key']);
  const denied=backend(({path})=>path.endsWith('/upload')?new Error('403 revoked'):undefined);
  await assert.rejects(transferDirectory(draft,selection,denied.call,denied.save,signal(),()=>{}),/403/);
  assert.equal(denied.state.finalized,0);
});

test('unknown legacy part hashes are retransmitted and completed files are skipped',async()=>{
  const selection=selectDirectory([file('root/a.png')],config),draft=await prepare(selection),server=backend();let once=true;
  const call=async(path,init)=>{
    const response=await server.call(path,init);
    if(init?.method==='PUT'&&once){once=false;delete server.uploads.get('item-0').parts['1'].sha256;throw new Error('disconnect');}
    return response;
  };
  await assert.rejects(transferDirectory(draft,selection,call,server.save,signal(),()=>{}));
  await transferDirectory(server.state.saved,selection,call,server.save,signal(),()=>{});
  assert.equal(server.parts.length,2);
  await transferDirectory(server.state.saved,selection,call,server.save,signal(),()=>{});
  assert.equal(server.parts.length,2);
});
