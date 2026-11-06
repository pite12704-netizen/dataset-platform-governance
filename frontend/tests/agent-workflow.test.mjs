// Real compiled UI, synthetic API, isolated browser. Never touches the running platform.
import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {spawn} from 'node:child_process';
import {readFile,mkdir,access} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
const pause=ms=>new Promise(r=>setTimeout(r,ms));
for(const engine of ['legacy','langgraph'])test(`visual draft requires preview and confirmation, then shows streamed task result (${engine})`,{timeout:60000},async()=>{
  const v='a'.repeat(32),actionId='b'.repeat(32),job='c'.repeat(32);let action,confirmations=0,previews=0,streams=0,recoveries=0,reconciliations=0;
  const dist=path.resolve('dist');const server=createServer(async(req,res)=>{try{
    const route=new URL(req.url,'http://localhost').pathname;
    const send=(value,type='application/json')=>{res.writeHead(200,{'Content-Type':type});res.end(type==='application/json'?JSON.stringify(value):value);};
    if(route.startsWith('/api/v1')){
      const p=route.slice(7);
      if(p==='/config')return send({deployment:'private',environment:'test'});
      if(p==='/auth/me')return send({id:'d'.repeat(32),name:'Synthetic owner'});
      if(p==='/agent/capabilities')return send({model_enabled:false,workflow_engine:engine});
      if(p==='/datasets')return send({items:[]});
      if(p==='/versions/'+v)return send({id:v,number:1,dataset:{name:'Synthetic visual version'}});
      if(p==='/agent/conversations/'+'f'.repeat(32))return send({messages:[]});
      if(p==='/agent/actions'&&req.method!=='POST')return send(action?[action]:[]);
      if(p==='/agent/chat'){
        const chunks=[];for await(const x of req)chunks.push(x);const body=JSON.parse(Buffer.concat(chunks).toString());
        assert.equal(body.intent,'vision');assert.deepEqual(body.version_ids,[v]);
        return send({conversation_id:'f'.repeat(32),run_id:'e'.repeat(32),tool:'vision',status:'completed',mode:'offline-tools',answer:'识别草稿，尚未执行',
          citations:[],draft:{kind:'vision',version_id:v,image_limit:2}});
      }
      if(p==='/agent/actions'&&req.method==='POST'){
        const chunks=[];for await(const x of req)chunks.push(x);const body=JSON.parse(Buffer.concat(chunks).toString());
        assert.equal(body.image_limit,2);previews++;
        action={id:actionId,kind:'vision',status:'pending',payload:body,expires_at:'2099-01-01T00:00:00',summary:{dataset_name:'Synthetic visual version',version_number:1,license:'test',input_files:2,input_bytes:100,output:'候选标注',resource_estimate:'synthetic',workflow:{plan_hash:'9'.repeat(64),images:2,total_images:20,model:'fixture-vl',template_name:'人物服饰',attributes:['袖长','裤长','帽子'],shard_size:1,selection_method:'前N张'}}};
        if(engine==='langgraph')action.orchestration={engine,status:'interrupted',recoverable:true};return send(action);
      }
      if(p==='/agent/actions/'+actionId+'/confirm'){
        confirmations++;action={...action,status:'completed',result:{job_id:job,plan_id:job,url:'/jobs'}};
        if(engine==='langgraph')action.orchestration={engine,status:'awaiting_task',recoverable:true};return send(action);
      }
      if(p==='/agent/actions/'+actionId+'/resume'){
        assert.equal(engine,'langgraph');const chunks=[];for await(const x of req)chunks.push(x);const body=JSON.parse(Buffer.concat(chunks).toString());
        if(action.status==='pending'){
          assert.equal(body.mode,'recover');assert.equal(confirmations,0);recoveries++;
          action.orchestration={engine,status:'awaiting_confirmation',recoverable:true};
        }else{
          assert.equal(body.mode,'refresh');reconciliations++;
          action.orchestration={engine,status:'completed',recoverable:false};
        }return send(action);
      }
      if(p==='/agent/actions/'+actionId+'/events'){
        streams++;return send('id: '+ '8'.repeat(64)+'\nevent: snapshot\ndata: '+JSON.stringify({action_id:actionId,job_id:job,status:'completed',kind:'vision_plan',progress:100,attempts:1,counts:{completed_images:2,total_images:2}})+'\n\n','text/event-stream');
      }
      return send([]);
    }
    if(route.startsWith('/assets/')){const file=path.resolve(dist,'.'+route);assert.ok(file.startsWith(dist+path.sep));return send(await readFile(file),file.endsWith('.js')?'application/javascript':'text/css');}
    return send(await readFile(path.join(dist,'index.html')),'text/html');
  }catch(e){res.writeHead(500);res.end(String(e));}});
  await new Promise(r=>server.listen(0,'127.0.0.1',r));const origin='http://127.0.0.1:'+server.address().port;
  const profile=path.join(tmpdir(),'dataset-workflow-ui-'+randomUUID());await mkdir(profile,{recursive:true});
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
    await cmd('Page.navigate',{url:origin+'/agent?version='+v});await until("document.querySelector('.agent-context').textContent.includes('Synthetic visual version')");
    await click('为所选版本安排图像识别');await until("!!document.getElementById('image_limit')");assert.equal(previews,0);assert.equal(confirmations,0);
    await click('查看执行预览');await until("document.body.textContent.includes('待确认的处理计划')");
    assert.equal(previews,1);assert.equal(confirmations,0);assert.ok(await evaluate("document.body.textContent.includes('裤长')"));
    if(engine==='langgraph'){
      await click('恢复流程');await until("document.body.textContent.includes('等待你确认')");
      assert.equal(recoveries,1);assert.equal(confirmations,0);
    }
    await click('确认提交');await until("document.body.textContent.includes('本次任务已结束')");
    assert.equal(confirmations,1);assert.equal(streams,1);assert.ok(await evaluate(`document.body.textContent.includes('${job}')`));
    if(engine==='langgraph'){
      for(let i=0;i<100&&reconciliations===0;i++)await pause(50);
      assert.equal(reconciliations,1);
    }else assert.equal(reconciliations,0);
    await cmd('Browser.close');
  }finally{socket?.close();if(browser.exitCode===null)browser.kill();await new Promise(r=>server.close(r));}
});
