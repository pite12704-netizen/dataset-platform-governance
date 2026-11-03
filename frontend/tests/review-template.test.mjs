// Isolated compiled-app test. Never attaches to the owner's browser or live API.
import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {spawn} from 'node:child_process';
import {readFile,mkdir,access,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
const pause=ms=>new Promise(r=>setTimeout(r,ms));
test('owner adds a custom review field, saves version template, reopens it without creating a task',{timeout:90000},async()=>{
  const v='a'.repeat(32),ds='b'.repeat(32),user='c'.repeat(32),bid='d'.repeat(32),item='e'.repeat(32);let saved,jobs=0,nextIndex=0,previewData,ownerMode=false,jointMode=false,assignment,modality='image',modalityChange;const saves=[];
  const object=()=>({id:item,asset_id:'f'.repeat(32),person_key:'human-added',filename:'synthetic.png',width:64,height:80,
    status:'approved',revision:1,predicted:{},annotation:{category:'truck',bbox:[1,2,20,30],labels:{color:'红',damaged:false,custom_size:'small'}}});
  const version=()=>({id:v,dataset_id:ds,number:1,status:'draft',can_manage:true,can_download:true,quality_tier:'raw',
    card:{},assets:[],asset_count:0,total_bytes:0,dataset:{name:'Synthetic template fixture',modality},source:'synthetic',license:'test'});
  const dist=path.resolve('dist');const server=createServer(async(req,res)=>{try{
    const route=new URL(req.url,'http://localhost').pathname;
    const send=(x,type='application/json')=>{res.writeHead(200,{'Content-Type':type});res.end(type==='application/json'?JSON.stringify(x):x);};
    if(route.startsWith('/api/v1')){
      const p=route.slice(7);
      if(p==='/config')return send({deployment:'private',environment:'test',supported_formats:['txt','json','png'],modality_formats:{text:['txt','json'],image:['txt','json','png']}});
      if(p==='/auth/me')return send({id:user,name:'Template tester'});
      if(p==='/spaces'||p==='/notifications')return send([]);
      if(p==='/spaces/'+'7'.repeat(32)+'/members')return send([{user_id:'5'.repeat(32),username:'annotator.a',role:'member',active:true},{user_id:'6'.repeat(32),username:'reviewer.b',role:'reviewer',active:true}]);
      if(p==='/joint-review/versions/'+v+'/reports')return send({items:[{id:'8'.repeat(32),images:2,pairs:1,status:'pending'}]});
      if(p==='/joint-review/versions/'+v&&req.method==='PUT'){
        const chunks=[];for await(const x of req)chunks.push(x);assignment=JSON.parse(Buffer.concat(chunks).toString());return send({assignment});
      }
      if(p.startsWith('/joint-review/images/')&&p.endsWith('/similar'))return send({report_id:'8'.repeat(32),images:2,pair_count:1,method:'synthetic',pairs:[{id:'a'.repeat(24),left:'0'.repeat(32),right:'f'.repeat(32),reference_asset_id:'0'.repeat(32),owned:true,kind:'near_candidate',distance:2,proposed:null,approved:null,confirmed:false}]});
      if(p==='/datasets/'+ds+'/modality'){
        const chunks=[];for await(const x of req)chunks.push(x);modalityChange=JSON.parse(Buffer.concat(chunks).toString());modality=modalityChange.modality;return send({modality});
      }
      if(p==='/datasets/'+ds)return send({id:ds,name:'Synthetic template fixture',modality,visibility:'private',access_mode:'application',can_manage:true,versions:[version()]});
      if(p==='/versions/'+v)return send(version());
      if(p==='/review-workbench/'+v)return send({version_id:v,dataset_id:ds,dataset_name:'Synthetic version queue',number:1,
        counts:{pending:2-saves.length,submitted:saves.length,approved:0},draft_images:0,total_images:2,available:Math.max(0,2-nextIndex),can_review:!ownerMode,can_manage:ownerMode,space_id:'7'.repeat(32),owner_id:user,assignment,can_annotate:jointMode,can_approve:false});
      if(p==='/review-workbench/'+v+'/next'){
        const index=nextIndex++,id=String(index+1).repeat(32),batchId=String(index+8).repeat(32);
        if(index>1)return send({image:null,message:'waiting'});
        const obj={...object(),id:String(index+4).repeat(32),status:'submitted'};
        const frame={id,batch_id:batchId,asset_id:obj.asset_id,width:64,height:80,status:'pending',revision:0,claim_token:String(index+6).repeat(32),items:[obj]};
        return send({image:frame,batch:{id:batchId,provenance:{review_template:saved},can_review:!jointMode,can_edit:true,joint_mode:jointMode,items:[obj],images:[{...frame,filename:index?'frame-two.png':'frame-one.png'}]}});
      }
      if(p.startsWith('/image-reviews/images/')&&req.method==='PUT'){
        const chunks=[];for await(const x of req)chunks.push(x);saves.push(JSON.parse(Buffer.concat(chunks).toString()));return send({});
      }
      if(p==='/image-reviews/'+bid)return send({id:bid,version_id:v,dataset_name:'Synthetic object review',created_at:'2026-11-01T00:00:00',
        status:'completed',can_review:true,can_edit:true,total:1,image_total:1,image_counts:{approved:1},counts:{approved:1},
        provenance:{origin:'local_vision_worker',review_template:saved},images:[],items:[object()]});
      if(p==='/image-reviews/items/'+item+'/history')return send([{id:'history',revision:1,actor_id:user,action:'edit',
        created_at:'2026-11-01T00:00:00',before:{},after:{annotation:object().annotation}}]);
      if(p.includes('/preview'))return send(previewData?{content:previewData}:{unavailable:true});
      if(p==='/review-templates/versions/'+v){
        if(req.method==='PUT'){const chunks=[];for await(const x of req)chunks.push(x);saved=JSON.parse(Buffer.concat(chunks).toString());}
        return send({template:saved||null,digest:saved?'fixture-digest':null});
      }
      if(p.startsWith('/jobs')&&req.method==='POST')jobs++;
      return send({items:[]});
    }
    if(route.startsWith('/assets/')){const file=path.resolve(dist,'.'+route);assert.ok(file.startsWith(dist+path.sep));return send(await readFile(file),file.endsWith('.js')?'application/javascript':'text/css');}
    return send(await readFile(path.join(dist,'index.html')),'text/html');
  }catch(e){res.writeHead(500);res.end(String(e));}});
  await new Promise(r=>server.listen(0,'127.0.0.1',r));const origin='http://127.0.0.1:'+server.address().port;
  const profiles=path.join(tmpdir(),'dataset-template-ui-'+randomUUID());await mkdir(profiles,{recursive:true});
  let exe;for(const p of [process.env.DP_TEST_BROWSER_EXE,'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe','C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe','/usr/bin/chromium'].filter(Boolean)){try{await access(p);exe=p;break;}catch{}}
  assert.ok(exe,'A test browser is required');const browser=spawn(exe,['--headless=new','--disable-gpu','--no-sandbox','--remote-debugging-port=0','--no-first-run','--disable-extensions','--window-size=1440,1200','--user-data-dir='+profiles,'about:blank'],{windowsHide:true,stdio:'ignore'});
  let socket;try{
    let port;for(let i=0;i<100;i++){try{port=Number((await readFile(path.join(profiles,'DevToolsActivePort'),'utf8')).split('\n')[0]);break;}catch{await pause(100);}}
    assert.ok(port);const targets=await(await fetch(`http://127.0.0.1:${port}/json/list`)).json();socket=new WebSocket(targets.find(t=>t.type==='page').webSocketDebuggerUrl);
    await new Promise((r,j)=>{socket.onopen=r;socket.onerror=j;});let seq=0;const pending=new Map();
    socket.onmessage=e=>{const x=JSON.parse(e.data);if(x.id){const q=pending.get(x.id);if(q){clearTimeout(q.timer);pending.delete(x.id);x.error?q.reject(x.error):q.resolve(x.result);}}};
    const cmd=(method,params={})=>new Promise((resolve,reject)=>{const id=++seq;pending.set(id,{resolve,reject,timer:setTimeout(()=>reject(Error('CDP timeout')),10000)});socket.send(JSON.stringify({id,method,params}));});
    const evaluate=async(expression)=>(await cmd('Runtime.evaluate',{expression,returnByValue:true,userGesture:true})).result.value;
    const until=async expression=>{for(let i=0;i<150;i++){if(await evaluate(expression))return;await pause(50);}throw Error('UI condition timed out: '+expression);};
    const click=async text=>{const selector=`Array.from(document.querySelectorAll('button')).find(e=>e.textContent.trim()===${JSON.stringify(text)})`;await until(`!!(${selector})`);await evaluate(`(${selector}).click()`);};
    await cmd('Page.navigate',{url:origin+'/datasets/'+ds});await click('目标类别与审核模板');await click('使用车辆示例');await click('添加属性（最多20项）');
    for(const [id,value] of [['fields_2_key','custom_size'],['fields_2_label','自定义尺寸'],['fields_2_optionsText','small\nlarge']]){
      await until(`!!document.getElementById('${id}')`);await evaluate(`document.getElementById('${id}').focus()`);await cmd('Input.insertText',{text:value});
    }
    await click('保存并绑定此版本');for(let i=0;i<100&&!saved;i++)await pause(50);
    assert.equal(saved.name,'车辆审核');assert.equal(saved.classes[1].key,'truck');
    assert.deepEqual(saved.fields[2],{key:'custom_size',label:'自定义尺寸',type:'enum',description:'',options:['small','large']});
    await until("!document.querySelector('.ant-modal-wrap:not([style*=\"display: none\"])')");
    await click('目标类别与审核模板');await until("document.getElementById('fields_2_key')?.value==='custom_size'");
    assert.equal(jobs,0,'Template configuration must not run inference');
    await cmd('Page.navigate',{url:origin+'/image-reviews/'+bid+'?history=1'});await click('查看记录');
    await until("document.body.textContent.includes('此目标为人工补标，没有模型原始预测。')");
    await until("!!document.querySelector('.ant-modal-body .ant-form')");
    previewData=await evaluate("(()=>{const c=document.createElement('canvas');c.width=64;c.height=80;const g=c.getContext('2d');g.fillStyle='#dddddd';g.fillRect(0,0,64,80);return c.toDataURL('image/jpeg');})()");
    await cmd('Page.navigate',{url:origin+'/image-reviews/workbench/'+v});await click('开始 / 继续审核');
    await until("document.body.textContent.includes('frame-one.png')&&!!document.querySelector('svg[aria-label=\"整图标注画布\"]')");
    await click('保存修改并查看下一张');
    await until("document.body.textContent.includes('frame-two.png')&&!!document.querySelector('svg[aria-label=\"整图标注画布\"]')");
    assert.equal(saves.length,1);assert.equal(saves[0].persons[0].annotation.category,'truck');
    assert.equal(await evaluate('location.pathname'),'/image-reviews/workbench/'+v);
    ownerMode=true;await cmd('Page.navigate',{url:origin+'/image-reviews/workbench/'+v});
    const mouseClick=async expression=>{const rect=await evaluate(`(()=>{const e=${expression};if(!e)return null;e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);assert.ok(rect);await cmd('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...rect});await cmd('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...rect});};
    const choose=async(id,label)=>{
      await until(`!!document.getElementById('${id}')`);await mouseClick(`document.getElementById('${id}').closest('.ant-select')`);
      await until(`document.getElementById('${id}').getAttribute('aria-expanded')==='true'`);await pause(500);
      await until(`Array.from(document.querySelectorAll('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option-content')).some(e=>e.textContent.includes(${JSON.stringify(label)}))`);
      await mouseClick(`(()=>{const rows=Array.from(document.querySelectorAll('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option-content'));const e=rows.find(e=>e.textContent.trim()===${JSON.stringify(label)})||rows.find(e=>e.textContent.includes(${JSON.stringify(label)}));return e.closest('.ant-select-item-option');})()`);
      await until(`document.getElementById('${id}').closest('.ant-select').textContent.includes(${JSON.stringify(label)})`);
      if(await evaluate(`document.getElementById('${id}').getAttribute('aria-expanded')==='true'`)){await cmd('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});await cmd('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});}await pause(300);
    };
    await choose('annotators','annotator.a');await choose('reviewers','reviewer.b');await choose('report_id','88888888');
    await click('保存分工与作业范围');for(let i=0;i<100&&!assignment;i++)await pause(50);
    assert.ok(assignment,await evaluate("JSON.stringify({errors:Array.from(document.querySelectorAll('.ant-form-item-explain-error')).map(e=>e.textContent),selected:Array.from(document.querySelectorAll('.ant-select-selection-item')).map(e=>e.textContent)})"));assert.deepEqual(assignment.annotators,['5'.repeat(32)]);assert.deepEqual(assignment.reviewers,['6'.repeat(32)]);assert.equal(assignment.report_id,'8'.repeat(32));
    ownerMode=false;jointMode=true;nextIndex=0;saves.length=0;
    await cmd('Page.navigate',{url:origin+'/image-reviews/workbench/'+v});await click('开始 / 继续审核');
    await until("document.body.textContent.includes('同屏去重初判与标注')");
    await until("Array.from(document.querySelectorAll('button')).find(e=>e.textContent==='保存修改并查看下一张')?.disabled===true");
    await mouseClick("Array.from(document.querySelectorAll('.ant-select-selection-placeholder')).find(e=>e.textContent.includes('请选择重复或不同')).closest('.ant-select')");
    await until("Array.from(document.querySelectorAll('.ant-select-item-option-content')).some(e=>e.textContent==='建议重复')");
    await mouseClick("Array.from(document.querySelectorAll('.ant-select-item-option-content')).find(e=>e.textContent==='建议重复')");
    await mouseClick("Array.from(document.querySelectorAll('.ant-checkbox-wrapper')).find(e=>e.textContent.includes('本图建议作为重复排除'))");
    await until("Array.from(document.querySelectorAll('button')).find(e=>e.textContent==='保存修改并查看下一张')?.disabled===false");
    await click('保存修改并查看下一张');await until("document.body.textContent.includes('frame-two.png')");
    assert.equal(saves[0].exclude_duplicate,true);assert.equal(saves[0].duplicate_decisions[0].duplicate,true);assert.deepEqual(saves[0].persons,[]);
    await cmd('Page.navigate',{url:origin+'/datasets/'+ds});await click('修改数据类型');await choose('modality','文本');await click('校验并保存类型');
    for(let i=0;i<100&&!modalityChange;i++)await pause(50);assert.deepEqual(modalityChange,{modality:'text'});
    await cmd('Page.navigate',{url:origin+'/upload?version='+v});await until("document.querySelector('input[aria-label=\"选择数据文件\"]')?.disabled===false");
    assert.equal(await evaluate("document.querySelector('input[aria-label=\"选择数据文件\"]').accept"),'.txt,.json');
    const blocked=path.join(profiles,'blocked.png');await writeFile(blocked,'synthetic not uploaded');
    const documentNode=await cmd('DOM.getDocument');const fileInput=await cmd('DOM.querySelector',{nodeId:documentNode.root.nodeId,selector:'input[aria-label="选择数据文件"]'});
    await cmd('DOM.setFileInputFiles',{nodeId:fileInput.nodeId,files:[blocked]});await until("document.body.textContent.includes('类型不符')");
    assert.equal(await evaluate("Array.from(document.querySelectorAll('button')).find(e=>e.textContent==='开始上传').disabled"),true);
    await cmd('Browser.close');
  }finally{socket?.close();if(browser.exitCode===null)browser.kill();await new Promise(r=>server.close(r));}
});
