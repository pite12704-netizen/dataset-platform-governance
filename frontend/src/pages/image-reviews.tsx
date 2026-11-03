import {TemplateFormFields} from '../review-template';
/** 【学习标注：图像复核页面】按目标连续纠错；保存后自动送审并打开下一项，服务端检查授权与 revision。 */
import { useCallback, useContext, useEffect, useRef, useState } from 'react';
import { Alert, App, Button, Card, Empty, Form, Input, InputNumber, Modal, Pagination, Popconfirm, Radio, Select, Space, Spin, Tag } from 'antd';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { api, date, key, post, put, Row } from '../api';
import { Session } from '../main';
import { AssetImage } from './image-pilot';
import { WholeImageReview } from './image-completeness';
import '../image-reviews.css';

const statusWords: Record<string,string> = {pending:'待修改后提交',submitted:'待人工审核',approved:'审核通过',rejected:'已退回',excluded:'已排除误检'};
const batchWords: Record<string,string> = {draft:'负责人草稿',submitted:'已送人工审核',in_review:'审核处理中',completed:'人工审核完成',discarded:'已弃用的识别草稿'};
const words: Record<string,string> = {hat:'戴帽子',no_hat:'未戴帽子',unclear:'不确定',short_sleeves:'短袖',long_sleeves:'长袖',sleeveless:'无袖',
  white:'白色',black:'黑色',red:'红色',blue:'蓝色',green:'绿色',yellow:'黄色',other_or_unclear:'其他 / 不确定'};
const options = {hat:['hat','no_hat','unclear'],sleeve:['short_sleeves','long_sleeves','sleeveless','unclear'],
  upper_color:['white','black','red','blue','green','yellow','other_or_unclear']};

function download(value: Row, name: string) {
  const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));
  const link=document.createElement('a');link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}

export function ImageReviewsPage(){
  const {batchId}=useParams(),[search]=useSearchParams(),navigate=useNavigate();
  const {user}=useContext(Session),{message}=App.useApp();
  const [batches,setBatches]=useState<Row[]>([]),[batch,setBatch]=useState<Row|null>(null),[version,setVersion]=useState<Row|null>(null);
  const [loading,setLoading]=useState(true),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  const [status,setStatus]=useState('all'),[query,setQuery]=useState(''),[page,setPage]=useState(1);
  const [selected,setSelected]=useState<Row|null>(null),[history,setHistory]=useState<Row[]>([]),[note,setNote]=useState('');
  const [rejectOpen,setRejectOpen]=useState(false),[dirty,setDirty]=useState(false),[form]=Form.useForm();
  const [wholeOpen,setWholeOpen]=useState(false);
  const [leaseLost,setLeaseLost]=useState(false),[conflictDraft,setConflictDraft]=useState<Row|null>(null);
  const activeLease=useRef<{id:string;token:string}|null>(null),viewId=useRef<string|null>(null);
  const batchScope=useRef(batchId);batchScope.current=batchId;
  const release=useCallback(async()=>{
    const lease=activeLease.current;activeLease.current=null;
    if(lease)try{await api(`/image-reviews/items/${lease.id}/release`,{method:'POST',
      body:JSON.stringify({claim_token:lease.token}),keepalive:true});}catch{/* 到期自动回收；旧凭证不会释放新领取。 */}
  },[]);
  useEffect(()=>{const leaving=()=>{void release();};window.addEventListener('pagehide',leaving);
    return()=>{window.removeEventListener('pagehide',leaving);void release();};},[batchId,release]);
  const sourceVersion=search.get('version');
  const draftBatches=batches.filter(b=>b.status==='draft').length;
  const overlappingBatches=batches.some(b=>b.overlaps_other_batch);
  const refresh=useCallback(async()=>{
    setError('');
    try {
      if(batchId){const value=await api(`/image-reviews/${batchId}`);setBatch(value);return value;}
      else {
        setBatches(await api('/image-reviews'+(sourceVersion?`?version_id=${encodeURIComponent(sourceVersion)}`:'')));
        setVersion(sourceVersion?await api(`/versions/${encodeURIComponent(sourceVersion)}`):null);
      }
    } catch(e:any){setError(e.message);setBatch(null);setBatches([]);return null;}finally{setLoading(false);}
  },[batchId,sourceVersion]);
  useEffect(()=>{setLoading(true);setSelected(null);void refresh();},[refresh]);
  const display=async(item:Row)=>{
    viewId.current=item.id;setSelected(item);setNote('');setRejectOpen(false);setDirty(false);setHistory([]);setLeaseLost(false);
    form.resetFields();form.setFieldsValue({category:item.annotation.category,labels:item.annotation.labels,x:item.annotation.bbox[0],y:item.annotation.bbox[1],
      width:item.annotation.bbox[2],height:item.annotation.bbox[3]});
    try{const events=await api(`/image-reviews/items/${item.id}/history`);if(viewId.current===item.id)setHistory(events);}
    catch(e:any){message.error(e.message);}
  };
  const acceptClaim=async(result:Row,scope= batchId)=>{
    if(scope!==batchScope.current){if(result.item)await api(`/image-reviews/items/${result.item.id}/release`,
      {method:'POST',body:JSON.stringify({claim_token:result.claim_token}),keepalive:true});return false;}
    if(!result.item){viewId.current=null;setSelected(null);message.info(result.message);return false;}
    activeLease.current={id:result.item.id,token:result.claim_token};
    await display({...result.item,claim_token:result.claim_token});return true;
  };
  const open=async(item:Row)=>{
    setBusy(true);const scope=batchId;
    try{
      await release();
      if(!batch?.can_review||item.status==='approved'||item.status==='submitted'&&item.editor_id===user?.id)await display({...item,claim_token:undefined});
      else await acceptClaim(await post(`/image-reviews/items/${item.id}/claim`),scope);
    }catch(e:any){message.error(e.message);await refresh();}finally{setBusy(false);}
  };
  const claimNext=async(after_item_id?:string)=>{
    const scope=batchId;
    return acceptClaim(await post(`/image-reviews/${scope}/claim-next`,{after_item_id}),scope);
  };
  const close=async()=>{viewId.current=null;setSelected(null);setRejectOpen(false);setConflictDraft(null);await release();await refresh();};
  const loseLease=()=>{
    void release();setLeaseLost(true);
    if(dirty)setConflictDraft(form.getFieldsValue());
  };
  useEffect(()=>{
    if(!selected?.claim_token||leaseLost)return;
    const lease={id:selected.id,token:selected.claim_token};let renewing=false,stopped=false;
    const timer=setInterval(async()=>{
      if(renewing||activeLease.current?.token!==lease.token)return;
      renewing=true;
      try{await post(`/image-reviews/items/${lease.id}/renew`,{claim_token:lease.token});}
      catch(e:any){if(!stopped&&activeLease.current?.token===lease.token){
        if([401,403,409].includes(e.status)){loseLease();message.warning('领取已失效，未保存的修改已保留，请重新领取后核对。');}
        else message.warning('暂时无法续期，请检查连接；提交时仍会校验领取有效期。');
      }}finally{renewing=false;}
    },Math.min(60000,Math.max(10000,(batch?.lease_seconds||300)*1000/3)));
    return()=>{stopped=true;clearInterval(timer);};
  },[selected?.id,selected?.claim_token,leaseLost,dirty,form,batch?.lease_seconds,message]);
  const apply=async(action:string)=>{
    if(!selected?.claim_token||leaseLost)return message.warning('请先领取当前审核任务');
    if(action==='reject'&&!note.trim())return message.warning('退回异常数据时请填写原因');
    setBusy(true);
    try{
      if(action==='edit'){
        const values=await form.validateFields();
        await put(`/image-reviews/items/${selected.id}`,{revision:selected.revision,claim_token:selected.claim_token,annotation:{
          labels:values.labels,...(values.category?{category:values.category}:{}),bbox:[values.x,values.y,values.width,values.height]}});
      }else await post(`/image-reviews/items/${selected.id}/transition`,{
        revision:selected.revision,claim_token:selected.claim_token,action,note});
      const oldId=selected.id;
      activeLease.current=null;setConflictDraft(null);viewId.current=null;setSelected(null);
      await refresh();
      setRejectOpen(false);
      message.success(action==='edit'?'修改已保存，等待另一位审核人确认':'审核已保存');
      await claimNext(oldId);
    }catch(e:any){message.error(e.message||'请检查表单');if([401,403,409].includes(e.status)){loseLease();await refresh();}}
    finally{setBusy(false);}
  };
  if(loading)return <Spin><div style={{height:200}}/></Spin>;
  if(error)return <Alert type="error" message={error} action={<Button onClick={refresh}>重试</Button>}/>;
  if(!batchId)return <div className="image-reviews"><h1>图像标注与复核</h1>
    <p className="muted">数据负责人整批送审；组织审核人直接核对模型结果。修改过的标注交给另一位审核人二次确认。</p>
    {version&&<Card title={`${version.dataset.name} · v${version.number}`}>
      <p>{version?.review_template?`模型按「${version.review_template.name}」定位 ${version.review_template.classes.map((c:Row)=>c.label).join('、')}，填写模板属性。`:'未绑定自定义模板，使用默认人物框、帽子、袖长和上衣颜色。'}每片最多100张，按计划有界调度，结果均待独立人工复核。</p>
      <Space><Link to={`/datasets/${version.dataset.id}`}>返回版本详情</Link>
      {version.can_manage&&draftBatches>0&&!overlappingBatches&&<Button loading={busy} type="primary" onClick={async()=>{
        setBusy(true);try{const result=await post(`/image-reviews/versions/${version.id}/submit`);await refresh();
          message.success(`已将 ${result.submitted_batches} 个批次、${result.persons} 个目标候选提交给组织审核人`);
        }catch(e:any){message.error(e.message);}finally{setBusy(false);}
      }}>整套提交人工审核</Button>}
      {version.can_manage&&<Button loading={busy} type="primary" onClick={async()=>{
        setBusy(true);try{const result=await post('/jobs/vision-batches',{version_id:version.id,kind:'vision',idempotency_key:key()});
          message.info(result.plan_id?`已登记自动计划，先安排 ${result.job_ids.length} 个分片；剩余 ${result.remaining_images} 张将由后台继续调度，可在任务中心跟踪`:'图片已有识别任务；失败任务请在任务页重试');
          navigate('/jobs');
        }catch(e:any){message.error(e.message);}finally{setBusy(false);}
      }}>运行本地目标识别</Button>}</Space>
      {overlappingBatches&&<Alert style={{marginTop:16}} type="warning" showIcon message="存在覆盖相同图片的多个试验批次"
        description="选择采用的批次送审，并弃用其余重复草稿。未弃用的草稿仍需审核完成才能发布。"/>}
    </Card>}
    {!version&&<Alert type="info" message="从数据集详情的“图像识别与复核”进入，或从图像清洗试验导入已有结果。"/>}
    <div className="review-batches">{batches.map(b=><Card key={b.id} title={b.provenance.origin==='local_vision_worker'?'自动目标识别':'导入图像试验'}
      extra={<Link to={`/image-reviews/${b.id}`}>打开复核队列 →</Link>}>
      <p>{b.provenance.model} · {b.provenance.images} 张图片 · {b.total} 个目标 · {batchWords[b.status]||b.status}</p>
      <Space wrap>{Object.entries(b.counts).map(([s,n])=><Tag key={s}>{statusWords[s]} {String(n)}</Tag>)}</Space>
      <p className="muted">创建于 {date(b.created_at)} · 批次 {b.id.slice(0,8)}</p>
    </Card>)}</div>{!batches.length&&<Empty description="尚无可访问的复核批次"/>}
  </div>;
  if(!batch)return null;
  const filtered=batch.items.filter((item:Row)=>(status==='all'||item.status===status)&&item.filename.includes(query));
  const actionable=batch.items.filter((item:Row)=>(item.status==='submitted'&&item.editor_id!==user?.id)||['pending','rejected'].includes(item.status));
  const box=selected?.annotation.bbox;
  return <div className="image-reviews">
    <div className="review-heading"><div><Link to="/image-reviews">图像复核 /</Link><h1>{batch.dataset_name}</h1>
      <p className="muted"><Link to={`/data-governance/${batch.version_id}`}>去重与划分</Link> · <Link to={`/evaluations/${batch.version_id}`}>独立测评</Link> · v{batch.version_number} · {batch.provenance.model} · {batch.total} 个目标候选 · {batchWords[batch.status]||batch.status}</p></div>
      <Space>{batch.can_review&&actionable.length>0&&<Button type="primary" loading={busy} onClick={async()=>{
        setBusy(true);try{await claimNext();await refresh();}catch(e:any){message.error(e.message);}finally{setBusy(false);}
      }}>领取下一条并连续审核（{actionable.length}）</Button>}
      {batch.can_submit_batch&&<Button type="primary" loading={busy} onClick={async()=>{
        setBusy(true);try{await post(`/image-reviews/${batch.id}/submit`);await refresh();message.success('整批数据已提交给组织审核人');}
        catch(e:any){message.error(e.message);}finally{setBusy(false);}
      }}>整批提交人工审核</Button>}
      {batch.can_discard_batch&&<Popconfirm title="弃用这个重复识别草稿？" description="须保留覆盖这些图片的另一套候选。保留实验记录和原图；已送审批次不能弃用。"
        okText="确认弃用" cancelText="取消" onConfirm={async()=>{setBusy(true);try{
          await post(`/image-reviews/${batch.id}/discard`);await refresh();message.success('草稿已弃用，实验记录保留');
        }catch(e:any){message.error(e.message);}finally{setBusy(false);}}}>
        <Button danger disabled={busy}>弃用重复识别草稿</Button></Popconfirm>}
      <Button onClick={refresh}>刷新</Button><Button disabled={!batch.counts.approved&&!(batch.image_total>0&&batch.image_counts?.approved===batch.image_total)} onClick={async()=>{
        try{download(await api(`/image-reviews/${batch.id}/export`),`approved-annotations-${batch.id}.json`);}
        catch(e:any){message.error(e.message);}
      }}>导出已审核标注与负样本</Button></Space></div>
    <Alert type="warning" showIcon message={batch.status==='draft'?'这是模型候选草稿，请由数据负责人整批送审。':'候选标签不等于高质量训练集。'}
      description={batch.status==='draft'?'数据负责人不需要逐条标注或填写原因；提交后，组织审核人会在受控工作台查看整批图片。':'正常结果直接通过；错误属性从固定选项中重新选择后保存。两种操作都会自动进入下一张，修改结果由另一位审核人确认。'}/>
    {batch.status==='discarded'&&<Alert style={{marginTop:16}} type="info" message="此草稿已弃用，保留记录，不参与审核与发布。"/>}
    <Card style={{marginTop:16}} title={`整图完整性：${batch.image_counts?.approved||0} / ${batch.image_total||0} 张已确认`}
      extra={batch.can_review&&<Button onClick={async()=>{await release();setSelected(null);setWholeOpen(!wholeOpen);}}> {wholeOpen?'关闭整图工作台':'打开整图补标与复核'} </Button>}>
      {wholeOpen&&<WholeImageReview batch={batch} refresh={refresh}/>}
      {!wholeOpen&&<p>逐人标签审核后，还需核对漏检、误检和无目标图片；整图确认完成后才能正式发布。</p>}
    </Card>
    {batch.provenance.scans&&<p className="muted">整图定位：{batch.provenance.scans.filter((s:Row)=>s.persons===0).length} 张未检出目标，
      {batch.provenance.scans.filter((s:Row)=>s.truncated).length} 张提示目标数量超限。这些图片尚未被确认为负样本或完整标注。</p>}
    <div className="review-counts">{Object.entries(batch.counts).map(([s,n])=><button key={s} onClick={()=>{setStatus(s);setPage(1);}}>
      <span>{statusWords[s]}</span><strong>{String(n)}</strong></button>)}</div>
    <Space wrap style={{marginBottom:20}}><Select aria-label="复核状态" value={status} style={{width:175}} onChange={v=>{setStatus(v);setPage(1);}}
      options={[{value:'all',label:'全部状态'},...Object.entries(statusWords).map(([value,label])=>({value,label}))]}/>
      <Input aria-label="搜索文件名" placeholder="按文件名查找" value={query} onChange={e=>{setQuery(e.target.value);setPage(1);}}/>
      <span>共 {filtered.length} 条</span></Space>
    <div className="review-grid">{filtered.slice((page-1)*12,page*12).map((item:Row)=><Card key={item.id} size="small"
      cover={<div className="review-thumbnail"><AssetImage id={item.asset_id} alt={item.filename} path={`/image-reviews/${batch.id}/assets/${item.asset_id}/preview`}/></div>}>
      <strong>{item.filename} · 目标 {item.person_key}</strong>{item.annotation.category&&<Tag>{batch.provenance?.review_template?.classes.find((c:Row)=>c.key===item.annotation.category)?.label||item.annotation.category}</Tag>}<p><Tag color={item.status==='approved'?'green':'default'}>{statusWords[item.status]}</Tag>修订 {item.revision}</p>
      <Space wrap>{Object.values(item.annotation.labels).map((v:any,i)=><Tag key={i}>{(words[String(v)]??String(v??'不确定'))}</Tag>)}</Space>
      {item.claimed_by_id&&item.claim_expires_at&&new Date(item.claim_expires_at+'Z')>new Date()&&
        <p><Tag color={item.claimed_by_id===user?.id?'blue':'orange'}>{item.claimed_by_id===user?.id?'我正在审核':'其他审核人处理中'}</Tag></p>}
      <Button block style={{marginTop:14}} disabled={busy||!!(item.claimed_by_id&&item.claimed_by_id!==user?.id&&
        item.claim_expires_at&&new Date(item.claim_expires_at+'Z')>new Date())} onClick={()=>open(item)}>
        {item.status==='approved'||!batch.can_review||item.status==='submitted'&&item.editor_id===user?.id?'查看记录':'领取此条并连续审核'}</Button>
    </Card>)}</div>{!filtered.length&&<Empty description="此条件下没有标注"/>}
    <Pagination style={{marginTop:24}} current={page} pageSize={12} total={filtered.length} onChange={setPage} showSizeChanger={false}/>
    <Modal title={selected?`${selected.filename} · 目标 ${selected.person_key}`:''} width={1060} open={!!selected}
      onCancel={()=>!busy&&void close()} footer={null} destroyOnClose>
      {selected&&<><div className="review-editor"><div>
        <div className="review-image"><AssetImage id={selected.asset_id} alt="待核对的原图与当前保存的目标框" path={`/image-reviews/${batch.id}/assets/${selected.asset_id}/preview`}/>
          <div className="review-box" style={{left:`${box[0]/selected.width*100}%`,top:`${box[1]/selected.height*100}%`,
            width:`${box[2]/selected.width*100}%`,height:`${box[3]/selected.height*100}%`}}/></div>
        <p className="muted">蓝框为已保存目标框。原图 {selected.width} × {selected.height} 像素；预览经过缩放。</p>
        <Card size="small" title="模型原始预测（保留不覆盖）"><Space wrap>{Object.values(selected.predicted?.labels||{}).map((v:any,i)=><Tag key={i}>{(words[String(v)]??String(v??'不确定'))}</Tag>)}</Space>
          {selected.predicted?.category&&<Tag>模型原始类别：{selected.predicted.category}</Tag>}{!selected.predicted?.bbox&&<p className="muted">此目标为人工补标，没有模型原始预测。</p>}<p className="muted">原始框：{(selected.predicted?.bbox||[]).map((v:number)=>v.toFixed(1)).join(', ')}</p></Card>
      </div><div><Tag>{statusWords[selected.status]}</Tag><span>修订 {selected.revision}</span>
        {selected.claim_token&&!leaseLost&&<p><Tag color="blue">已领取 · 自动续期</Tag>关闭窗口将释放任务</p>}
        {leaseLost&&<Alert type="warning" showIcon message="领取或页面版本已失效，提交已暂停"
          description="重新领取会加载最新保存的标注，请对照下面保留的修改重新核对。"
          action={<Button disabled={busy} onClick={()=>open(selected)}>重新领取</Button>}/>}
        {conflictDraft&&<Alert style={{marginTop:12}} type="info" message="上次未保存的修改（供对照，不会自动提交）"
          description={`${Object.values(conflictDraft.labels||{}).map((v:any)=>(words[String(v)]??String(v??'不确定'))).join(' / ')}；框：${[conflictDraft.x,conflictDraft.y,conflictDraft.width,conflictDraft.height].join(', ')}`}/>}
        <Form form={form} layout="vertical" disabled={!batch.can_edit||!selected.claim_token||leaseLost||busy} onValuesChange={()=>setDirty(true)}>
          {batch.provenance?.review_template?<TemplateFormFields template={batch.provenance.review_template}/>:<>
          {Object.entries(options).map(([field,values])=><Form.Item key={field} name={['labels',field]}
            label={{hat:'帽子',sleeve:'袖长',upper_color:'上衣颜色'}[field]} rules={[{required:true}]}>
            <Radio.Group className="review-options" optionType="button" buttonStyle="solid"
              options={values.map(value=>({value,label:(words[String(value)]??String(value??'不确定'))}))}/></Form.Item>)}
          </>}
          <div className="review-coordinates">{[['x','左侧 X'],['y','顶部 Y'],['width','宽度'],['height','高度']].map(([name,label])=>
            <Form.Item key={name} name={name} label={label} rules={[{required:true}]}><InputNumber min={name==='x'||name==='y'?0:0.01} precision={2}/></Form.Item>)}</div>
        </Form>
        <Space wrap>{selected.claim_token&&!leaseLost&&batch.can_edit&&(dirty||['pending','rejected'].includes(selected.status))&&
          <Button type="primary" loading={busy} onClick={()=>apply('edit')}>保存修改并查看下一张</Button>}
          {selected.claim_token&&!leaseLost&&batch.can_review&&!dirty&&selected.status==='submitted'&&selected.editor_id!==user?.id&&<>
            <Button type="primary" loading={busy} onClick={()=>apply('approve')}>审核通过并查看下一张</Button>
            <Button danger type="text" disabled={busy} onClick={()=>{setNote('');setRejectOpen(true);}}>无法确认，退回复查</Button></>}
        </Space>
        <p className="muted">无需填写常规审核说明。系统会保留修改前后值、操作者和修订时间。</p>
        {selected.editor_id===user?.id&&selected.status==='submitted'&&<p className="muted">这条修改已送交另一位审核人确认。</p>}
      </div></div><h3>修订与复核记录</h3>{history.length?history.map(h=><div className="review-history" key={h.id}>
        <strong>r{h.revision} · {{batch_submit:'整批送审',edit:'保存修改',submit:'提交二次确认',approve:'审核通过',reject:'退回修改'}[h.action as string]}</strong>
        <span>{date(h.created_at)} · 操作者 {h.actor_id.slice(0,8)}</span>{h.note&&<p>{h.note}</p>}
        <p className="muted">{h.before?.annotation?.category&&`${h.before.annotation.category} → ${h.after?.annotation?.category}；`}{Object.values(h.before?.annotation?.labels||{}).map((v:any)=>(words[String(v)]??String(v??'不确定'))).join(' / ')} → {Object.values(h.after?.annotation?.labels||{}).map((v:any)=>(words[String(v)]??String(v??'不确定'))).join(' / ')}</p>
      </div>):<p className="muted">尚无目标工修订或复核记录。</p>}</>}
    </Modal>
    <Modal title="退回异常数据" open={rejectOpen} okText="确认退回" cancelText="取消" confirmLoading={busy}
      okButtonProps={{danger:true,disabled:!note.trim()}} onCancel={()=>!busy&&setRejectOpen(false)} onOk={()=>apply('reject')}>
      <p>只有图片无法判断、目标框严重错误等不能直接纠正的情况才需要退回。</p>
      <Input.TextArea autoFocus aria-label="退回原因" placeholder="请选择退回时说明具体问题" maxLength={1000}
        value={note} onChange={e=>setNote(e.target.value)}/>
    </Modal>
  </div>;
}
