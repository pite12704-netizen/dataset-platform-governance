/** 【学习标注：治理闭环】后台任务生成报告 → 独立人工判断 → 保留原图生成派生版本。 */
import {useCallback,useEffect,useState} from 'react';
import {Link,useParams} from 'react-router-dom';
import {Alert,App,Button,Card,Form,Input,InputNumber,Modal,Select,Space,Table,Tag} from 'antd';
import {api,post,key,Row,labels,date} from '../api';
import {AssetImage} from './image-pilot';

function ReportImage({report,asset}:{report:string;asset:string}){
  return <AssetImage id={asset} alt="重复候选对原图" binary className="governance-preview" path={`/governance/reports/${report}/assets/${asset}/preview`}/>;
}

export function DataGovernancePage(){
  const {versionId}=useParams(),{message}=App.useApp();
  const [context,setContext]=useState<Row>(),[reports,setReports]=useState<Row[]>([]),[jobs,setJobs]=useState<Row[]>([]);
  const [error,setError]=useState(''),[busy,setBusy]=useState(false),[kind,setKind]=useState('dedup'),[active,setActive]=useState<Row>();
  const [decisions,setDecisions]=useState<Record<string,boolean>>({}),[pair,setPair]=useState<Row>();
  const [groups,setGroups]=useState('');const [sourceEvidence,setSourceEvidence]=useState('');const [form]=Form.useForm();
  const refresh=useCallback(async()=>{try{
    const [c,r,j]=await Promise.all([api(`/governance/versions/${versionId}/context`),api(`/governance/versions/${versionId}`),api(`/jobs?compact=true&limit=100&version_id=${versionId}&governance_only=true`)]);
    setContext(c);setReports(r.filter((x:Row)=>x.kind!=='evaluate'));setJobs(j.items.filter((x:Row)=>x.kind!=='evaluate'));setError('');
  }catch(e:any){setError(e.message);}},[versionId]);
  useEffect(()=>{refresh();},[refresh]);
  useEffect(()=>{if(!jobs.some(j=>['queued','running'].includes(j.status)))return;const timer=setInterval(refresh,5000);return()=>clearInterval(timer);},[jobs,refresh]);
  const execute=async(values:Row)=>{setBusy(true);try{
    const groupMap=kind==='split'&&groups.trim()?JSON.parse(groups):{};
    await post('/jobs',{version_id:versionId,kind,idempotency_key:key(),...values,groups:groupMap});
    message.success('已创建治理任务，可在处理任务页跟踪进度');await refresh();
  }catch(e:any){message.error(e.message);}finally{setBusy(false);}};
  const open=(r:Row)=>{setActive(r);setDecisions(Object.fromEntries((r.summary.pairs||[]).filter((p:Row)=>p.kind==='exact').map((p:Row)=>[p.id,true])));};
  const decide=async(approve:boolean)=>{if(!active)return;setBusy(true);try{
    await post(`/governance/reports/${active.id}/decision`,{approve,duplicate_pairs:Object.keys(decisions).filter(id=>decisions[id]),reviewed_pairs:Object.keys(decisions)});
    message.success(approve?'独立审查通过':'已退回此报告');setActive(undefined);await refresh();
  }catch(e:any){message.error(e.message);}finally{setBusy(false);}};
  if(error)return <Alert type="error" message={error} action={<Button onClick={refresh}>重试</Button>}/>;
  if(!context)return <Card>加载治理信息…</Card>;
  const editable=context.can_manage&&context.mutable;
  const approved=reports.filter(r=>r.kind==='dedup'&&r.status==='approved');
  const pairs=active?.summary.pairs||[];
  return <><Link to={`/datasets/${context.dataset_id}`}>← 返回数据集</Link><h1>{context.dataset_name} · v{context.number} 数据生产与治理</h1>
    <Alert showIcon type="info" message="原图始终保留。去重使用 SHA256 与 dHash 候选；近重复需要人工判断。分组划分依赖真实来源组。标注完成后进入独立测评工作台。"/>
    {context.can_review&&context.my_role!=='evaluator'&&context.mutable&&<Card title="独立来源与许可审核" style={{marginTop:20}}>
      <p>认证交付需要核对数据来源及训练、再分发许可；审批记录绑定当前来源与许可，修改后需重新审核。</p>
      <p>当前来源：{context.source} · 当前许可：{context.license}</p>
      <Input.TextArea value={sourceEvidence} onChange={e=>setSourceEvidence(e.target.value)} placeholder="填写核验凭证引用或归档编号（不填密码）"/>
      <Button style={{marginTop:12}} disabled={sourceEvidence.length<5} onClick={async()=>{try{await post(`/governance/versions/${versionId}/source-decision`,{approved:true,evidence_reference:sourceEvidence,reason:'已独立核验来源及本次交付许可范围'});await refresh();message.success('来源许可审核已记录');}catch(e:any){message.error(e.message);}}}>确认已核验来源与许可</Button>
    </Card>}
    <p><Link to={`/deliveries/${versionId}`}>属性筛选与训练交付 →</Link></p>
    <p><Link to={`/evaluations/${versionId}`}>进入此版本的独立测评 →</Link></p>
    {editable&&<Card title="创建治理任务" style={{marginTop:20}}>
      <Alert showIcon type={context.image_count>context.governance_max_images?'warning':'info'} message={`当前版本 ${context.image_count} 张图片，治理上限 ${context.governance_max_images} 张；候选对上限 ${context.dedup_max_candidates}，超出计算或候选预算将明确失败，不截断为成功。`}/>
      <Form form={form} layout="vertical" initialValues={{near_distance:6,seed:20261007}} onFinish={execute} style={{marginTop:20}}>
        <Form.Item label="操作"><Select value={kind} onChange={setKind} options={['dedup','split','curate'].map(value=>({value,label:labels[value]}))}/></Form.Item>
        {kind==='dedup'&&<Form.Item name="near_distance" label="dHash 距离阈值（越大候选越多，不是相似度百分比）"><InputNumber min={0} max={16}/></Form.Item>}
        {['split','curate'].includes(kind)&&<Form.Item name="dedup_report_id" label="已独立审核的去重报告" rules={[{required:true}]}><Select options={approved.map(r=>({value:r.id,label:`${r.id} · ${r.summary.images} 张图`}))}/></Form.Item>}
        {kind==='curate'&&<Alert type="info" message="对确认重复组保留稳定资产 ID 对应的代表图，复制成新版本；新文件重新检查，标注需在新版本继续完成复核。"/>}
        {kind==='split'&&<><Form.Item label="来源组映射 JSON（本地上传须填写；批量导入可留空读取清单来源组）"><Input.TextArea value={groups} onChange={e=>setGroups(e.target.value)} rows={6} placeholder='{"资产ID":"视频或连拍组001"}'/></Form.Item><Form.Item name="seed" label="固定随机种子"><InputNumber/></Form.Item><Button onClick={()=>setGroups(JSON.stringify(Object.fromEntries(context.assets.filter((a:Row)=>a.image).map((a:Row)=>[a.id,'请填写真实来源组'])),null,2))}>生成待填写模板</Button></>}
        <Button type="primary" htmlType="submit" loading={busy} style={{marginTop:16}}>创建{labels[kind]}任务</Button>
      </Form>
    </Card>}
    <Card title="治理报告与独立审查" extra={<Button onClick={refresh}>刷新</Button>} style={{marginTop:20}}><Table rowKey="id" dataSource={reports} columns={[
      {title:'类型',dataIndex:'kind',render:v=>labels[v]||v},{title:'状态',dataIndex:'status',render:v=><Tag>{labels[v]||v}</Tag>},
      {title:'创建时间',dataIndex:'created_at',render:date},{title:'操作',render:(_:any,r:Row)=><Button onClick={()=>open(r)}>查看与审查</Button>}]} expandable={{expandedRowRender:r=><pre className="preview-text">{JSON.stringify(r.summary,null,2)}</pre>}}/></Card>
    <Card title="当前版本的后台任务" style={{marginTop:20}}><Table rowKey="id" dataSource={jobs} columns={[{title:'操作',dataIndex:'kind',render:v=>labels[v]||v},{title:'状态',dataIndex:'status',render:v=>labels[v]||v},{title:'进度',dataIndex:'progress'},{title:'失败原因',dataIndex:'error'}]}/><Link to="/jobs">进入任务管理 →</Link></Card>
    <Modal title={`${labels[active?.kind||'']}报告审查`} open={!!active} onCancel={()=>setActive(undefined)} width={1000} footer={null} destroyOnClose>
      {active?.kind==='dedup'?<><Alert type="info" message="完全相同由内容哈希确认；每对近重复必须明确判断。先对比原图，再选择重复或不同。"/><Table rowKey="id" dataSource={pairs} pagination={{pageSize:8}} columns={[
        {title:'候选',render:(_:any,p:Row)=><span>{p.left.slice(0,8)} / {p.right.slice(0,8)}</span>},
        {title:'依据',render:(_:any,p:Row)=>p.kind==='exact'?'SHA256相同':`dHash距离 ${p.distance}`},
        {title:'判定',render:(_:any,p:Row)=><Space><Button onClick={()=>setPair(p)}>对比原图</Button><Select disabled={p.kind==='exact'||active.status!=='pending'} value={decisions[p.id]===undefined?undefined:decisions[p.id]?'duplicate':'distinct'} placeholder="请选择" style={{width:110}} onChange={v=>setDecisions(d=>({...d,[p.id]:v==='duplicate'}))} options={[{value:'duplicate',label:'重复'},{value:'distinct',label:'不同'}]}/></Space>}]}/></>:<pre className="preview-text">{JSON.stringify(active?.summary,null,2)}</pre>}
      {active&&context.joint_report_id===active.id&&<Alert type="info" message="此报告已关联去重标注联审，无需在此重复审批；请进入连续工作台，由审核岗位逐图终审后汇总。" action={<Link to={`/image-reviews/workbench/${versionId}`}>进入联合作业</Link>}/>}
      {context.can_review&&context.mutable&&active?.status==='pending'&&context.joint_report_id!==active.id&&<Space style={{marginTop:20}}><Button type="primary" loading={busy} disabled={active.kind==='dedup'&&Object.keys(decisions).length!==pairs.length} onClick={()=>decide(true)}>独立审查通过</Button><Button danger loading={busy} onClick={()=>decide(false)}>退回报告</Button></Space>}
    </Modal>
    <Modal title="原图对比" open={!!pair} onCancel={()=>setPair(undefined)} footer={null} width={1100} destroyOnClose>{pair&&active&&<div className="two-column"><ReportImage report={active.id} asset={pair.left}/><ReportImage report={active.id} asset={pair.right}/></div>}</Modal>
  </>;
}
