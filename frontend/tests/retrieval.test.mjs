// Compiled UI + synthetic HTTP service + independent browser; no owner session or business service.
import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {spawn} from 'node:child_process';
import {readFile,writeFile,mkdir,access} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
const pause=ms=>new Promise(r=>setTimeout(r,ms));

test('retrieval workbench shows sources, real version links, temporary query and clears failed results',{timeout:60000},async()=>{
  const version='a'.repeat(32),document='b'.repeat(32);let fail=false,knowledge=0,catalog=0,queries=0,indexed=0;
  const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6AboAAAAASUVORK5CYII=','base64');
  const dist=path.resolve('dist');const server=createServer(async(req,res)=>{try{
    const route=new URL(req.url,'http://localhost').pathname;
    const send=(value,type='application/json',status=200)=>{res.writeHead(status,{'Content-Type':type});res.end(type==='application/json'?JSON.stringify(value):value);};
    if(route.startsWith('/api/v1')){
      const p=route.slice(7);const chunks=[];for await(const chunk of req)chunks.push(chunk);const body=Buffer.concat(chunks);
      if(p==='/config')return send({deployment:'private',environment:'test'});
      if(p==='/auth/me')return send({id:'fixture',name:'Synthetic owner',platform_admin:true});
      if(p==='/datasets')return send({items:[{id:'fixture-dataset',name:'Synthetic catalog'}]});
      if(p==='/versions/'+version)return send({id:version,number:1,status:'draft'});
      if(p==='/retrieval/capabilities')return send({enabled:true,image_enabled:true,max_image_bytes:1024*1024});
      if(p==='/retrieval/documents')return send({items:[{id:document,title:'Synthetic policy',revision:1,status:'published'}]});
      if(p==='/retrieval/documents/'+document)return send({id:document,title:'Synthetic policy',body:'Independent review requires another account.',revision:1,status:'published',can_manage:true});
      if(p==='/retrieval/knowledge/ask'){
        knowledge++;assert.equal(JSON.parse(body).query,'独立复核是什么');
        return send({answer:'Another account must review.',mode:'knowledge-extractive',knowledge_citations:[{entry_id:'c',document_id:document,name:'Synthetic policy',section:'Review',revision:1,excerpt:'Independent review requires another account.'}]});
      }
      if(p==='/retrieval/catalog/search'){
        catalog++;assert.equal(JSON.parse(body).query,'夜间行人');
        return send({mode:'hybrid',items:[{version_id:version,name:'Night dataset',number:1,url:'/datasets/fixture-dataset?version='+version,description:'夜间道路',license:'test-license',match_basis:'资料声明'}]});
      }
      if(p==='/retrieval/versions/'+version+'/images')return send({items:[],next_cursor:null});
      if(p==='/retrieval/versions/'+version+'/index'){
        if(req.method==='POST'){indexed++;return send({status:'queued'});}
        return send({counts:{queued:2},errors:[]});
      }
      if(p==='/retrieval/images/query'){
        queries++;assert.match(req.headers['content-type'],/^multipart\/form-data; boundary=/);assert.ok(body.includes(Buffer.from(version)));
        if(fail)return send({message:'Synthetic vector outage'},'application/json',503);
        return send({mode:'hybrid',items:[{asset_id:'test-image',name:'Similar synthetic.png',preview_url:'/api/v1/retrieval/images/test-image/preview'}]});
      }
      if(p==='/retrieval/images/test-image/preview')return send(png,'image/png');
      return send([]);
    }
    if(route.startsWith('/assets/')){const file=path.resolve(dist,'.'+route);assert.ok(file.startsWith(dist+path.sep));return send(await readFile(file),file.endsWith('.js')?'application/javascript':'text/css');}
    return send(await readFile(path.join(dist,'index.html')),'text/html');
  }catch(e){res.writeHead(500);res.end(String(e));}});
  await new Promise(r=>server.listen(0,'127.0.0.1',r));const origin='http://127.0.0.1:'+server.address().port;
  const profile=path.join(tmpdir(),'retrieval-ui-'+randomUUID());await mkdir(profile,{recursive:true});const queryFile=path.join(profile,'query.png');await writeFile(queryFile,png);
  let exe;for(const p of [process.env.DP_TEST_BROWSER_EXE,'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe','C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe','/usr/bin/chromium'].filter(Boolean)){try{await access(p);exe=p;break;}catch{}}
  assert.ok(exe,'Independent browser required');const browser=spawn(exe,['--headless=new','--no-sandbox','--disable-gpu','--remote-debugging-port=0','--no-first-run','--disable-extensions','--window-size=1440,1200','--user-data-dir='+profile,'about:blank'],{windowsHide:true,stdio:'ignore'});
  let socket;try{
    let port;for(let i=0;i<100;i++){try{port=Number((await readFile(path.join(profile,'DevToolsActivePort'),'utf8')).split('\n')[0]);break;}catch{await pause(100);}}
    assert.ok(port);const targets=await(await fetch(`http://127.0.0.1:${port}/json/list`)).json();socket=new WebSocket(targets.find(t=>t.type==='page').webSocketDebuggerUrl);
    await new Promise((r,j)=>{socket.onopen=r;socket.onerror=j;});let seq=0;const pending=new Map();
    socket.onmessage=e=>{const x=JSON.parse(e.data);if(x.id){const q=pending.get(x.id);if(q){clearTimeout(q.timer);pending.delete(x.id);x.error?q.reject(x.error):q.resolve(x.result);}}};
    const cmd=(method,params={})=>new Promise((resolve,reject)=>{const id=++seq;pending.set(id,{resolve,reject,timer:setTimeout(()=>reject(Error('CDP timeout')),10000)});socket.send(JSON.stringify({id,method,params}));});
    const evaluate=async expression=>(await cmd('Runtime.evaluate',{expression,returnByValue:true,userGesture:true})).result.value;
    const until=async expression=>{for(let i=0;i<180;i++){if(await evaluate(expression))return;await pause(50);}throw Error('UI timeout: '+expression);};
    const click=async text=>{const e=`Array.from(document.querySelectorAll('button')).find(e=>e.textContent.trim()===${JSON.stringify(text)})`;await until(`!!(${e})&&!(${e}).disabled`);await evaluate(`(${e}).click()`);};
    const input=async(label,value)=>{const e=`document.querySelector('[aria-label="${label}"]')`;await until(`!!(${e})`);await evaluate(`(()=>{const e=${e};Object.getOwnPropertyDescriptor(e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype,'value').set.call(e,${JSON.stringify(value)});e.dispatchEvent(new Event('input',{bubbles:true}));})()`);};
    await cmd('Page.navigate',{url:origin+'/retrieval'});await until("document.body.textContent.includes('Synthetic policy')");
    await input('知识问题','独立复核是什么');await click('检索并回答');await until("document.body.textContent.includes('Another account must review.')");
    await click('查看授权原文');await until("document.querySelector('.ant-modal-body')?.textContent.includes('Independent review')");
    await evaluate("document.querySelector('.ant-modal-close').click()");await pause(300);
    await evaluate("Array.from(document.querySelectorAll('[role=tab]')).find(e=>e.textContent==='自然语言找数据集').click()");
    await input('数据集需求','夜间行人');await click('搜索版本');await until("document.body.textContent.includes('Night dataset')");
    assert.ok(await evaluate(`!!document.querySelector('a[href="/datasets/fixture-dataset?version=${version}"]')`));
    await cmd('Page.navigate',{url:origin+'/retrieval?version='+version});await until("!!document.querySelector('input[type=file]')");
    const root=await cmd('DOM.getDocument');const node=await cmd('DOM.querySelector',{nodeId:root.root.nodeId,selector:'input[type=file]'});
    await cmd('DOM.setFileInputFiles',{nodeId:node.nodeId,files:[queryFile]});await click('查找相似图片');await until("document.body.textContent.includes('Similar synthetic.png')");
    fail=true;await click('查找相似图片');await until("document.body.textContent.includes('Synthetic vector outage')");
    assert.equal(await evaluate("document.body.textContent.includes('Similar synthetic.png')"),false);
    await click('负责人：建立/重建版本索引');await until("document.body.textContent.includes('已登记索引事件')");assert.equal(indexed,1);
    assert.equal(knowledge,1);assert.equal(catalog,1);assert.equal(queries,2);
    await cmd('Browser.close');
  }finally{socket?.close();if(browser.exitCode===null)browser.kill();await new Promise(r=>server.close(r));}
});
