// Synthetic API and a private headless profile; never uses business accounts or services.
import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {spawn} from 'node:child_process';
import {readFile,mkdir,access} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
const pause=ms=>new Promise(r=>setTimeout(r,ms));
test('adding citations is explicit; quality shortcut submits exactly the clicked version, once',{timeout:60000},async()=>{
  const a='a'.repeat(32),b='b'.repeat(32),requests=[];
  const citations=[a,b].map((id,i)=>({version_id:id,dataset_id:'d'.repeat(32),name:'Synthetic '+(i?'B':'A'),number:1,modality:'image',access_mode:'open'}));
  const dist=path.resolve('dist');const server=createServer(async(req,res)=>{try{
    const route=new URL(req.url,'http://localhost').pathname;
    const send=(value,type='application/json')=>{res.writeHead(200,{'Content-Type':type});res.end(type==='application/json'?JSON.stringify(value):value);};
    if(route.startsWith('/api/v1')){
      const p=route.slice(7);
      if(p==='/config')return send({deployment:'private',environment:'test'});
      if(p==='/auth/me')return send({id:'c'.repeat(32),name:'Synthetic user'});
      if(p==='/agent/capabilities')return send({model_enabled:false});
      if(p==='/datasets')return send({items:[]});
      if(p==='/agent/chat'){
        const chunks=[];for await(const x of req)chunks.push(x);const body=JSON.parse(Buffer.concat(chunks).toString());requests.push(body);
        return send({conversation_id:'f'.repeat(32),run_id:String(requests.length).repeat(32),tool:'quality',status:'completed',mode:'platform',
          answer:body.version_ids.length?'质量结果 '+body.version_ids[0]:'请选择具体版本',citations:body.version_ids.length?[]:citations});
      }
      return send([]);
    }
    if(route.startsWith('/assets/')){const file=path.resolve(dist,'.'+route);assert.ok(file.startsWith(dist+path.sep));return send(await readFile(file),file.endsWith('.js')?'application/javascript':'text/css');}
    return send(await readFile(path.join(dist,'index.html')),'text/html');
  }catch(e){res.writeHead(500);res.end(String(e));}});
  await new Promise(r=>server.listen(0,'127.0.0.1',r));const origin='http://127.0.0.1:'+server.address().port;
  const profile=path.join(tmpdir(),'dataset-agent-context-'+randomUUID());await mkdir(profile,{recursive:true});
  let exe;for(const candidate of [process.env.DP_TEST_BROWSER_EXE,'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe','C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe','/usr/bin/chromium'].filter(Boolean)){try{await access(candidate);exe=candidate;break;}catch{}}
  assert.ok(exe,'Independent test browser required');
  const browser=spawn(exe,['--headless=new','--no-sandbox','--disable-gpu','--remote-debugging-port=0','--no-first-run','--disable-extensions','--user-data-dir='+profile,'about:blank'],{windowsHide:true,stdio:'ignore'});
  let socket;try{
    let port;for(let i=0;i<100;i++){try{port=Number((await readFile(path.join(profile,'DevToolsActivePort'),'utf8')).split('\n')[0]);break;}catch{await pause(100);}}
    assert.ok(port);const targets=await(await fetch(`http://127.0.0.1:${port}/json/list`)).json();socket=new WebSocket(targets.find(t=>t.type==='page').webSocketDebuggerUrl);
    await new Promise((r,j)=>{socket.onopen=r;socket.onerror=j;});let seq=0;const pending=new Map();
    socket.onmessage=e=>{const x=JSON.parse(e.data);if(x.id){const q=pending.get(x.id);if(q){clearTimeout(q.timer);pending.delete(x.id);x.error?q.reject(x.error):q.resolve(x.result);}}};
    const cmd=(method,params={})=>new Promise((resolve,reject)=>{const id=++seq;pending.set(id,{resolve,reject,timer:setTimeout(()=>reject(Error('CDP timeout')),10000)});socket.send(JSON.stringify({id,method,params}));});
    const evaluate=async expression=>(await cmd('Runtime.evaluate',{expression,returnByValue:true,userGesture:true})).result.value;
    const until=async expression=>{for(let i=0;i<150;i++){if(await evaluate(expression))return;await pause(50);}throw Error('UI timeout: '+expression);};
    const button=(text,index=0)=>`Array.from(document.querySelectorAll('button')).filter(e=>e.textContent.trim()===${JSON.stringify(text)})[${index}]`;
    const click=async(text,index=0)=>{const e=button(text,index);await until(`!!(${e})&&!(${e}).disabled`);await evaluate(`(${e}).click()`);};
    await cmd('Page.navigate',{url:origin+'/agent'});await click('解读所选版本的质量报告');await until("document.body.textContent.includes('请选择具体版本')");
    await click('加入所选版本');await until(`(${button('已加入所选版本')})?.disabled===true`);assert.equal(requests.length,1);
    await click('加入所选版本');await pause(50);assert.equal(requests.length,1);
    await until("document.querySelectorAll('.agent-context .ant-select-selection-item').length===2");
    const quality=button('解读此版本质量');await evaluate(`(${quality}).click();(${quality}).click();`);
    await until(`document.body.textContent.includes('质量结果 ${a}')`);assert.equal(requests.length,2);assert.deepEqual(requests[1].version_ids,[a]);assert.equal(requests[1].intent,'quality');
    await click('解读此版本质量',1);await until(`document.body.textContent.includes('质量结果 ${b}')`);assert.equal(requests.length,3);assert.deepEqual(requests[2].version_ids,[b]);
    await cmd('Browser.close');
  }finally{socket?.close();if(browser.exitCode===null)browser.kill();await new Promise(r=>server.close(r));}
});
