import {TemplateControls,unknownAnnotation} from '../review-template';
import {DuplicateAnnotationPanel} from '../joint-review';
/** 整图复核：画框补漏、排除误检、独立确认；领取和修订均由服务端管理。 */
import { useContext, useEffect, useRef, useState } from 'react';
import {useBlocker} from 'react-router-dom';
import { Alert, App, Button, InputNumber, Select, Space, Tag } from 'antd';
import { api, post, put, Row } from '../api';
import { Session } from '../main';
import {previewQueue} from '../preview-queue';

const choices: Record<string, string[]> = {hat:['hat','no_hat','unclear'],
  sleeve:['short_sleeves','long_sleeves','sleeveless','unclear'],
  upper_color:['white','black','red','blue','green','yellow','other_or_unclear']};
const labels: Record<string,string> = {hat:'戴帽子',no_hat:'无帽子',unclear:'不确定',short_sleeves:'短袖',
  long_sleeves:'长袖',sleeveless:'无袖',white:'白',black:'黑',red:'红',blue:'蓝',green:'绿',yellow:'黄',other_or_unclear:'其他/不确定'};
const states: Record<string,string> = {pending:'待整图核对',submitted:'待第二人确认',approved:'整图已确认'};

export function WholeImageReview({batch, refresh,initialClaim,onNext,onPause}:{batch:Row;refresh:()=>Promise<void>;initialClaim?:Row;onNext?:()=>Promise<void>;onPause?:()=>void}) {
  const {message}=App.useApp(), {user}=useContext(Session);const template=batch.provenance?.review_template;
  const [selected,setSelected]=useState(batch.images?.[0]?.id||''),[page,setPage]=useState<Row|null>(initialClaim||null);
  const [persons,setPersons]=useState<Row[]>(initialClaim?initialClaim.items.filter((i:Row)=>i.status!=='excluded').map((i:Row)=>({item_id:i.id,annotation:i.annotation})):[]),[source,setSource]=useState(''),[busy,setBusy]=useState(false);
  const [drawing,setDrawing]=useState<[number,number]|null>(null),[lost,setLost]=useState(false);
  const [dirty,setDirty]=useState(false);
  const joint=useRef<Row>({ready:!batch.joint_mode});const [jointReady,setJointReady]=useState(!batch.joint_mode);
  const blocker=useBlocker(!!onNext&&dirty&&!!page);
  useEffect(()=>{if(blocker.state==='blocked'){if(window.confirm('有未保存的整图修改，确定离开？'))blocker.proceed();else blocker.reset();}},[blocker]);
  const lease=useRef<{id:string;token:string}|null>(initialClaim?{id:initialClaim.id,token:initialClaim.claim_token}:null);
  const current=page||batch.images?.find((i:Row)=>i.id===selected);
  useEffect(()=>{
    let active=true;setSource('');const controller=new AbortController();
    const img=batch.images?.find((i:Row)=>i.id===selected);
    const endpoint=img?`/image-reviews/${batch.id}/assets/${img.asset_id}/preview`:'';
    if(img)previewQueue.load<Row>(`${user?.id||'anonymous'}:${endpoint}:false`,signal=>api(endpoint,{signal}),controller.signal).then(r=>{
      if(active&&r.content?.startsWith('data:image/jpeg;base64,'))setSource(r.content);
    }).catch(e=>active&&message.error(e.message));
    return()=>{active=false;controller.abort();};
  },[selected,batch.id,user?.id,message]);
  useEffect(()=>{
    const timer=setInterval(()=>{const held=lease.current;if(held)post(`/image-reviews/images/${held.id}/renew`,
      {claim_token:held.token}).catch(()=>setLost(true));},60000);
    return()=>{clearInterval(timer);const held=lease.current;lease.current=null;
      if(held)api(`/image-reviews/images/${held.id}/release`,{method:'POST',body:JSON.stringify({claim_token:held.token}),keepalive:true}).catch(()=>{});};
  },[]);
  const release=async()=>{const held=lease.current;if(held)await post(`/image-reviews/images/${held.id}/release`,{claim_token:held.token});
    lease.current=null;setPage(null);setLost(false);};
  const claim=async()=>{setBusy(true);try{const r=await post(`/image-reviews/images/${selected}/claim`);
    lease.current={id:r.id,token:r.claim_token};setPage(r);setLost(false);
    setPersons(r.items.filter((i:Row)=>i.status!=='excluded').map((i:Row)=>({item_id:i.id,annotation:i.annotation})));
  }catch(e:any){message.error(e.message);}finally{setBusy(false);}};
  const done=async(approve:boolean)=>{if(!page||!lease.current)return;setBusy(true);try{
    const body={revision:page.revision,claim_token:lease.current.token};
    if(approve)await post(`/image-reviews/images/${page.id}/approve`,body);
    else await put(`/image-reviews/images/${page.id}`,{...body,persons:joint.current.exclude_duplicate?[]:persons,
      ...(batch.joint_mode?{duplicate_decisions:joint.current.duplicate_decisions||[],exclude_duplicate:!!joint.current.exclude_duplicate}:{})});
    lease.current=null;setPage(null);setDirty(false);setLost(false);await refresh();
    message.success(approve?'整图已由第二人确认':'已保存补标，等待另一位审核人确认');
    if(onNext){await onNext();return;}
    const next=batch.images.find((i:Row)=>i.id!==selected&&i.status!=='approved'&&(i.status!=='submitted'||i.editor_id!==user?.id));if(next)setSelected(next.id);
  }catch(e:any){message.error(e.message);if(e.status===409)setLost(true);}finally{setBusy(false);}};
  const coord=(event:any):[number,number]=>{const rect=event.currentTarget.getBoundingClientRect();return [
    Math.max(0,Math.min(current.width,(event.clientX-rect.left)*current.width/rect.width)),
    Math.max(0,Math.min(current.height,(event.clientY-rect.top)*current.height/rect.height))];};
  const change=(index:number,update:Row)=>{setDirty(true);setPersons(old=>old.map((p,i)=>i===index?{...p,annotation:{...p.annotation,...update}}:p));};
  useEffect(()=>{const warn=(e:BeforeUnloadEvent)=>{if(dirty&&lease.current){e.preventDefault();e.returnValue='';}};window.addEventListener('beforeunload',warn);return()=>window.removeEventListener('beforeunload',warn);},[dirty]);
  return <div>
    <Alert type="info" showIcon message="整图核对包含漏检和空结果；保存后需要另一位审核人确认。"
      description="领取后在原图上拖动补充目标框；误检可排除。确认没有目标时排除所有框后保存。关闭会释放领取，断网后到期回收。"/>
    <Space wrap style={{margin:'16px 0'}}>
      {!onNext&&<Select aria-label="整图复核图片" style={{width:320}} value={selected} disabled={!!page||busy}
        options={batch.images?.map((i:Row)=>({value:i.id,label:`${i.filename} · ${states[i.status]}`}))}
        onChange={value=>{setSelected(value);setPage(null);}}/>}
      {!onNext&&<Button loading={busy} disabled={!selected||!!page||current?.status==='approved'||current?.status==='submitted'&&current?.editor_id===user?.id} onClick={claim}>领取整图</Button>}
      {onNext&&<Button disabled={busy} onClick={async()=>{if(dirty&&!window.confirm('这张图片有未保存修改，确定放弃修改并跳到下一张吗？'))return;setBusy(true);try{await release();setDirty(false);await onNext();}catch(e:any){message.error(e.message);}finally{setBusy(false);}}}>跳过，下一张</Button>}
      {page&&<Button disabled={busy} onClick={async()=>{if(dirty&&!window.confirm('有未保存修改，确定放弃并暂停？'))return;try{await release();setDirty(false);onPause?.();}catch(e:any){message.error(e.message);}}}>{onNext?'暂停并释放':'释放领取'}</Button>}
      {onNext&&page?.status==='submitted'&&<Button disabled={busy||lost} onClick={async()=>{const id=page.id,revision=page.revision;setBusy(true);try{await release();await post(`/image-reviews/images/${id}/reopen`,{revision});if(batch.joint_mode){await refresh();await onNext();}else await claim();}catch(e:any){message.error(e.message);}finally{setBusy(false);}}}>{batch.joint_mode?'退回标注并查看下一张':'发现问题，返回修改'}</Button>}
      {onNext&&lost&&<Button disabled={busy} onClick={()=>{if(!dirty||window.confirm('重新领取将加载服务器最新版本，放弃本地未保存修改？')){setDirty(false);void claim();}}}>重新领取本张</Button>}
      {batch.can_review&&current?.status!=='pending'&&!page&&<Button disabled={busy} onClick={async()=>{try{
        await post(`/image-reviews/images/${selected}/reopen`,{revision:current.revision});await refresh();
      }catch(e:any){message.error(e.message);}}}>返回修改</Button>}
    </Space>
    {lost&&<Alert type="warning" message="领取或修订已变化。本地修改保留，请释放后重新领取并核对。"/>}
    {batch.joint_mode&&page&&<DuplicateAnnotationPanel key={page.id+':'+page.revision} image={page} editable={!!batch.can_edit&&page.status==='pending'} onChange={value=>{joint.current=value;setJointReady(value.ready);if(value.edited)setDirty(true);}}/>}
    {page&&!source&&<Alert type="info" message="原图尚未加载成功，暂不能保存或确认；可等待加载，或跳过后重试。"/>}
    {source&&current&&<svg aria-label="整图标注画布" viewBox={`0 0 ${current.width} ${current.height}`}
      style={{display:'block',width:'100%',maxWidth:900,aspectRatio:`${current.width}/${current.height}`,touchAction:'none'}}
      onPointerDown={e=>{if(page?.status==='pending'&&!lost){e.currentTarget.setPointerCapture(e.pointerId);setDrawing(coord(e));}}}
      onPointerCancel={()=>setDrawing(null)} onPointerUp={e=>{if(!drawing)return;const end=coord(e);setDrawing(null);
        const box=[Math.min(drawing[0],end[0]),Math.min(drawing[1],end[1]),Math.abs(end[0]-drawing[0]),Math.abs(end[1]-drawing[1])];
        if(box[2]>=2&&box[3]>=2){setDirty(true);setPersons(old=>[...old,{annotation:{bbox:box,...(template?unknownAnnotation(template):{labels:{hat:'unclear',sleeve:'unclear',upper_color:'other_or_unclear'}})}}]);}}}>
      <image href={source} width={current.width} height={current.height}/>
      {(page?persons:batch.items.filter((i:Row)=>i.asset_id===current.asset_id&&i.status!=='excluded')
        .map((i:Row)=>({annotation:i.annotation}))).map((p:Row,i:number)=><g key={i} pointerEvents="none">
          <rect x={p.annotation.bbox[0]} y={p.annotation.bbox[1]} width={p.annotation.bbox[2]} height={p.annotation.bbox[3]}
            fill="none" stroke="red" strokeWidth={Math.max(1,current.width/400)}/>
          <text x={p.annotation.bbox[0]} y={Math.max(12,p.annotation.bbox[1])} fill="red" fontSize={Math.max(12,current.width/45)}>{i+1}</text>
        </g>)}
    </svg>}
    {page&&persons.map((p,i)=><div key={i} style={{padding:'12px 0',borderBottom:'1px solid #ddd'}}>
      <Space wrap><Tag>目标 {i+1}</Tag>{template?<TemplateControls template={template} annotation={p.annotation} disabled={page.status!=='pending'||lost} onChange={v=>change(i,v)}/>:<>{Object.entries(choices).map(([field,values])=><Select key={field} aria-label={`${field}-${i}`}
        value={p.annotation.labels[field]} disabled={page.status!=='pending'||lost} style={{width:140}}
        options={values.map(v=>({value:v,label:labels[v]}))} onChange={v=>change(i,{labels:{...p.annotation.labels,[field]:v}})}/>)}
        </>}
        {p.annotation.bbox.map((v:number,j:number)=><InputNumber key={j} aria-label={`框-${i}-${j}`} value={v}
          disabled={page.status!=='pending'||lost} min={0} style={{width:80}} onChange={n=>{const b=[...p.annotation.bbox];b[j]=n||0;change(i,{bbox:b});}}/>)}
        <Button danger disabled={page.status!=='pending'||lost} onClick={()=>{setDirty(true);setPersons(old=>old.filter((_,j)=>j!==i));}}>排除此框</Button>
      </Space>
    </div>)}
    {page&&<Space style={{marginTop:16}}><Button type="primary" loading={busy} disabled={lost||!source||!jointReady} onClick={()=>done(page.status==='submitted')}>
      {page.status==='submitted'?(onNext?'审核通过并查看下一张':'独立确认整图完整'):(onNext?'保存修改并查看下一张':'保存整图并送独立确认')}</Button>
      {!persons.length&&<Tag>确认无目标的整图</Tag>}</Space>}
  </div>;
}
