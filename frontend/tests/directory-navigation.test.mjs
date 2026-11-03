// Actual compiled React/Router/Worker/IndexedDB regression in an isolated browser.
// Only synthetic in-memory API and images; never attach to the owner's Chrome or services.
import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {spawn} from 'node:child_process';
import {access,readFile,writeFile,mkdir} from 'node:fs/promises';
import {createHash,randomUUID} from 'node:crypto';
import path from 'node:path';

const version='a'.repeat(32),owner='b'.repeat(32),dataset='c'.repeat(32),batchId='d'.repeat(32);
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const hash=bytes=>createHash('sha256').update(bytes).digest('hex');

test('progress opens separately; Back/menu ask; staying continues; explicit leave preserves resumable upload',
  {timeout:90000},async()=>{
    const guesses=[process.env.DP_TEST_BROWSER_EXE,...(process.platform==='win32'?
      ['C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe','C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe']:
      ['/usr/bin/google-chrome','/usr/bin/chromium','/usr/bin/chromium-browser'])].filter(Boolean);
    let executable;
    for(const value of guesses){try{await access(value);executable=value;break;}catch{}}
    assert.ok(executable,'Install Chrome/Chromium or set DP_TEST_BROWSER_EXE to the test browser executable');
    const dist=path.resolve('dist'),output=path.resolve('../var/navigation-tests',randomUUID());
    await access(path.join(dist,'index.html'));await mkdir(output,{recursive:true});
    let batch,releaseGate;
    const gate=new Promise(resolve=>{releaseGate=resolve;});
    const items=new Map(),uploads=new Map(),statistics={creates:0,puts:0,completes:0,finalizes:0};
    const summary=()=>({...batch,counts:{registered:items.size,uploaded:statistics.completes,clean:statistics.completes,checking:0,failed:0}});
    const server=createServer(async(req,res)=>{
      try{
        const pathname=new URL(req.url,'http://fixture').pathname;
        const send=(value,status=200,type='application/json')=>{
          if(res.destroyed)return;
          const bytes=type==='application/json'?Buffer.from(JSON.stringify(value)):Buffer.from(value);
          res.writeHead(status,{'Content-Type':type,'Content-Length':bytes.length});res.end(bytes);
        };
        if(pathname.startsWith('/api/v1')){
          const route=pathname.slice('/api/v1'.length);
          if(req.method==='GET'){
            if(route==='/config')return send({deployment:'private',environment:'test',max_upload_bytes:20*1024**2});
            if(route==='/auth/me')return send({id:owner,name:'目录导航测试用户'});
            if(route==='/spaces'||route==='/notifications')return send([]);
            if(route==='/jobs')return send({items:[],has_more:false,total:0});
            if(route==='/versions/'+version)return send({id:version,dataset_id:dataset,dataset:{name:'合成图片目录'},number:1,
              status:'draft',can_manage:true,source:'本地合成测试',license:'仅用于测试'});
            if(route==='/imports')return send({items:batch?[summary()]:[],has_more:false});
            if(route==='/imports/'+batchId)return send(summary());
            if(route==='/imports/'+batchId+'/items')return send({items:[...items.values()].map(item=>({...item,status:'pending'})),has_more:false});
          }
          const body=Buffer.concat(await Array.fromAsync(req));
          const json=()=>JSON.parse(body.toString()||'{}');
          if(route==='/imports'&&req.method==='POST'){
            statistics.creates++;batch={...json(),id:batchId,status:'uploading',sealed:false};
            return send({...summary(),token:'synthetic-unused'});
          }
          if(route==='/imports/'+batchId+'/items'&&req.method==='POST'){
            const page=json().items;
            for(const entry of page)items.set(entry.external_id,items.get(entry.external_id)??{...entry,id:randomUUID().replaceAll('-','')});
            return send({items:page.map(entry=>items.get(entry.external_id))});
          }
          if(route.endsWith('/upload')&&req.method==='POST'){
            const id=route.split('/')[4];uploads.set(id,uploads.get(id)??{id,status:'uploading',part_size:8*1024**2,parts:{}});
            return send(uploads.get(id));
          }
          if(req.method==='PUT'){
            const [, ,id,,number]=route.split('/');
            uploads.get(id).parts[number]={size:body.length,sha256:hash(body)};statistics.puts++;
            if(statistics.puts===1)await gate;
            return send({etag:'synthetic'});
          }
          if(route.endsWith('/complete')){uploads.get(route.split('/')[2]).status='completed';statistics.completes++;return send({asset_id:randomUUID()});}
          if(route.endsWith('/finalize')){statistics.finalizes++;batch.status='completed';batch.sealed=true;return send(summary());}
          return send({message:'No fixture route'},404);
        }
        const file=path.resolve(dist,'.'+pathname);
        if(pathname.startsWith('/assets/')&&file.startsWith(dist+path.sep)){
          return send(await readFile(file),200,file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'application/octet-stream');
        }
        return send(await readFile(path.join(dist,'index.html')),200,'text/html; charset=utf-8');
      }catch(error){if(!res.destroyed){res.writeHead(500);res.end('Fixture failure: '+error.message);}}
    });
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
    const origin='http://127.0.0.1:'+server.address().port;
    const profile=path.join(output,'profile');await mkdir(profile);
    const browser=spawn(executable,['--headless=new','--remote-debugging-port=0','--disable-background-networking',
      '--disable-sync','--disable-extensions','--no-first-run','--no-default-browser-check','--window-size=1440,1200',
      '--user-data-dir='+profile,'about:blank'],{windowsHide:true,stdio:'ignore'});
    let socket,send;const errors=[];
    try{
      let port;
      for(let i=0;i<200;i++){try{port=Number((await readFile(path.join(profile,'DevToolsActivePort'),'utf8')).split('\n')[0]);break;}catch{await pause(100);}}
      assert.ok(port,'Test browser did not start');
      const targets=await(await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      socket=new WebSocket(targets.find(value=>value.type==='page').webSocketDebuggerUrl);
      await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject;});
      let sequence=0;const waiting=new Map();
      socket.onmessage=event=>{
        const value=JSON.parse(event.data);
        if(value.id){const entry=waiting.get(value.id);if(entry){clearTimeout(entry.timer);waiting.delete(value.id);value.error?entry.reject(value.error):entry.resolve(value.result);}}
        else if(value.method==='Runtime.exceptionThrown')errors.push(value.params.exceptionDetails);
      };
      send=(method,params={})=>new Promise((resolve,reject)=>{
        const id=++sequence;waiting.set(id,{resolve,reject,timer:setTimeout(()=>{waiting.delete(id);reject(new Error('Browser timeout: '+method));},10000)});
        socket.send(JSON.stringify({id,method,params}));
      });
      const evaluate=async(expression)=>{
        const result=await send('Runtime.evaluate',{expression,returnByValue:true,userGesture:true});
        if(result.exceptionDetails)throw new Error(result.exceptionDetails.text);
        return result.result.value;
      };
      const until=async(expression,label)=>{for(let i=0;i<150;i++){if(await evaluate(expression))return;await pause(50);}throw new Error('UI timeout: '+label);};
      const click=async text=>{await until(`Array.from(document.querySelectorAll('button,a')).some(el=>el.textContent.trim()===${JSON.stringify(text)})`,text);
        await evaluate(`Array.from(document.querySelectorAll('button,a')).find(el=>el.textContent.trim()===${JSON.stringify(text)}).click()`);};
      const select=async()=>{
        await until(`document.querySelector('input[aria-label="选择本地图片文件夹"]')?.disabled===false`,'folder selector');
        await evaluate(`(()=>{const data=new DataTransfer();for(const pathname of ['root/a/same.png','root/b/same.png']){
          const file=new File(['synthetic-image'],pathname.split('/').pop());Object.defineProperty(file,'webkitRelativePath',{value:pathname});data.items.add(file);}
          const input=document.querySelector('input[aria-label="选择本地图片文件夹"]');input.files=data.files;input.dispatchEvent(new Event('change',{bubbles:true}));})()`);
      };
      await send('Runtime.enable');await send('Page.enable');
      await send('Page.navigate',{url:origin+'/imports/local?version='+version});
      await until(`document.querySelector('input[aria-label="选择本地图片文件夹"]')?.disabled===false`,'initial route');
      await click('← 导入批次');await until(`location.pathname==='/imports'`,'import list');
      await click('网页选择文件夹 / 续传');await until(`location.pathname==='/imports/local'`,'return to folder page');
      await select();await click('校验并开始上传');
      for(let i=0;i<100&&statistics.puts===0;i++)await pause(50);
      assert.equal(statistics.puts,1,'first synthetic part should be waiting');
      await evaluate('window.history.back()');
      const visiblePrompt=`Array.from(document.querySelectorAll('.ant-modal-wrap')).some(el=>el.getClientRects().length>0 && el.textContent.includes('正在校验或上传，是否暂停并离开？'))`;
      await until(visiblePrompt,'Back confirmation');
      assert.equal(await evaluate('location.pathname'),'/imports/local');
      await click('留在此页继续上传');
      await until(`!(${visiblePrompt})`,'stay on folder page');
      await evaluate(`document.querySelector('a[href="/jobs"]').click()`);
      await until(visiblePrompt,'sidebar confirmation');
      await click('留在此页继续上传');
      await until(`!(${visiblePrompt})`,'stay after sidebar navigation');
      const progressSelector=`a[href="/imports/${batchId}"]`;
      assert.equal(await evaluate(`document.querySelector('${progressSelector}').target`),'_blank');
      assert.equal(await evaluate(`document.querySelector('${progressSelector}').rel`),'noopener noreferrer');
      await evaluate(`document.querySelector('${progressSelector}').click()`);
      for(let i=0;i<100;i++){
        const pages=await(await fetch(`http://127.0.0.1:${port}/json/list`)).json();
        if(pages.some(page=>page.url===origin+'/imports/'+batchId))break;
        if(i===99)throw new Error('Progress tab did not open');await pause(50);
      }
      assert.equal(await evaluate('location.pathname'),'/imports/local');
      assert.equal(statistics.creates,1);assert.equal(statistics.completes,0);
      // Explicit leave is the only navigation that intentionally pauses this transfer.
      await evaluate(`document.querySelector('a[href="/jobs"]').click()`);
      await until(visiblePrompt,'leave confirmation');
      await click('暂停并离开');await until(`location.pathname==='/jobs'`,'accepted navigation');
      releaseGate();
      await evaluate('window.history.back()');await until(`location.pathname==='/imports/local'`,'resume route');
      await select();await click('校验并继续上传');
      await until(`document.body.textContent.includes('整批文件检查通过')`,'resumed completion');
      assert.deepEqual(statistics,{creates:1,puts:2,completes:2,finalizes:1});assert.equal(errors.length,0,JSON.stringify(errors));
      const screenshot=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});
      await writeFile(path.join(output,'page.png'),Buffer.from(screenshot.data,'base64'));
      await writeFile(path.join(output,'result.json'),JSON.stringify({passed:true,statistics,errors,new_tab:true,back_blocked:true,
        sidebar_blocked:true,stayed:true,explicit_pause:true,resumed:true},null,2));
      console.log('Isolated browser verification:',output);
    }finally{
      releaseGate();if(send)await Promise.race([send('Browser.close').catch(()=>{}),pause(500)]);
      socket?.close();browser.kill();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));
    }
  });
