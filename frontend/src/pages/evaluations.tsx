/** 【学习标注：独立测评】负责人提交真值评测；另一位组织审核人核验真值和质量门槛。 */
import {useCallback,useEffect,useState} from 'react';
import {Link,useParams} from 'react-router-dom';
import {Alert,App,Button,Card,Checkbox,Descriptions,Form,Modal,Select,Space,Table,Tag} from 'antd';
import {api,post,put,key,Row,labels,date} from '../api';

export function EvaluationsPage(){
  const {versionId}=useParams(),{message}=App.useApp();
  const [items,setItems]=useState<Row[]>([]),[hasMore,setHasMore]=useState(false),[loading,setLoading]=useState(false),[error,setError]=useState('');
  const [context,setContext]=useState<Row>(),[reports,setReports]=useState<Row[]>([]),[jobs,setJobs]=useState<Row[]>([]);
  const [active,setActive]=useState<Row>(),[truth,setTruth]=useState<Row>(),[verified,setVerified]=useState(false),[busy,setBusy]=useState(false);
  const [form]=Form.useForm();
  const loadInbox=useCallback(async(offset=0)=>{
    setLoading(true);
    try{
      const page=await api(`/governance/evaluations?limit=50&offset=${offset}`);
      setItems(previous=>offset?[...previous,...page.items]:page.items);
      setHasMore(page.has_more);setError('');
    }catch(e:any){setError(e.message);}finally{setLoading(false);}
  },[]);
  const loadVersion=useCallback(async()=>{
    try{
      const [c,r,j]=await Promise.all([api(`/governance/versions/${versionId}/context`),api(`/governance/versions/${versionId}`),api('/jobs')]);
      setContext(c);setReports(r.filter((x:Row)=>x.kind==='evaluate'));
      setJobs(j.filter((x:Row)=>x.version_id===versionId&&x.kind==='evaluate'));setError('');
    }catch(e:any){setError(e.message);}
  },[versionId]);
  useEffect(()=>{if(versionId)loadVersion();else loadInbox();},[versionId,loadInbox,loadVersion]);
  useEffect(()=>{if(!versionId||!jobs.some(j=>['queued','running'].includes(j.status)))return;
    const timer=setInterval(loadVersion,5000);return()=>clearInterval(timer);
  },[versionId,jobs,loadVersion]);
  const submit=async(values:Row)=>{setBusy(true);try{
    await post('/jobs',{version_id:versionId,kind:'evaluate',idempotency_key:key(),...values});
    message.success('评测任务已提交，完成后由另一位审核人核验');await loadVersion();
  }catch(e:any){message.error(e.message);}finally{setBusy(false);}};
  const open=(report:Row)=>{setActive(report);setTruth(undefined);setVerified(false);};
  const previewTruth=async()=>{if(!active)return;try{
    setTruth(await api(`/governance/reports/${active.id}/truth`));
  }catch(e:any){message.error(e.message);}};
  const decide=async(approve:boolean)=>{if(!active)return;setBusy(true);try{
    await post(`/governance/reports/${active.id}/decision`,{approve,truth_verified:approve&&verified});
    message.success(approve?'测评已独立审核':'测评已退回');setActive(undefined);await loadVersion();
  }catch(e:any){message.error(e.message);}finally{setBusy(false);}};
  if(error)return <Alert type="error" message={error} action={<Button onClick={()=>versionId?loadVersion():loadInbox()}>重试</Button>}/>;
  if(!versionId)return <>
    <div className="page-title"><div><div className="eyebrow">QUALITY EVALUATION</div><h1>独立测评工作台</h1>
      <p>负责人提交模型候选或最终标注的真值评测；组织审核人集中处理待核验报告。</p></div><Button onClick={()=>loadInbox()} loading={loading}>刷新</Button></div>
    <Alert showIcon type="info" message="审核人不能审核本人提交的报告；报告批准也不等于达到发布质量门槛。发布时仍按版本的质量策略校验。"/>
    <Card title="测评报告" style={{marginTop:20}}><Table rowKey="id" loading={loading} dataSource={items} pagination={{pageSize:20}} columns={[
      {title:'数据集',dataIndex:'dataset_name',render:(value:string,row:Row)=><Link to={`/evaluations/${row.version_id}`}>{value} · v{row.version_number}</Link>},
      {title:'评测对象',dataIndex:'evaluation_target',render:(value:string)=>value==='approved_annotations'?'最终人工标注':'模型候选'},
      {title:'状态',dataIndex:'status',render:(value:string)=><Tag>{labels[value]||value}</Tag>},
      {title:'质量门槛',dataIndex:'quality_gate',render:(gate:Row)=>gate?gate.passed?<Tag color="green">达标</Tag>:<Tag color="red">未达标</Tag>:'待核验'},
      {title:'创建时间',dataIndex:'created_at',render:date},
      {title:'操作',render:(_:unknown,row:Row)=><Link to={`/evaluations/${row.version_id}`}>{row.can_review?'去审核':'查看报告'}</Link>}
    ]}/>{hasMore&&<Button loading={loading} onClick={()=>loadInbox(items.length)}>加载更多</Button>}</Card>
  </>;
  if(!context)return <Card>加载测评信息…</Card>;
  const editable=context.can_manage&&context.mutable;
  const pending=active?.status==='pending'&&context.can_review&&context.mutable;
  const gate=active?.summary?.quality_gate;
  return <><Link to="/evaluations">← 返回测评待办</Link><h1>{context.dataset_name} · v{context.number} 独立测评</h1>
    <p><Link to={`/blind-evaluations/${versionId}`}>独立盲测：抽样、私有真值与专人审批 →</Link></p><Space wrap><Link to={`/datasets/${context.dataset_id}`}>数据集详情</Link><Link to={`/data-governance/${versionId}`}>生产与治理</Link><Link to={`/image-reviews?version=${versionId}`}>标注复核</Link></Space>
    <Alert showIcon type={context.quality_gate_required?'info':'warning'} style={{marginTop:18}}
      message={context.quality_gate_required?'此版本发布时必须有有效且达标的最终标注评测报告':'此版本尚未启用发布质量门槛；模型候选评测仅用于诊断，不能代表最终标注质量'}/>
    {editable&&<Card title="提交评测任务" style={{marginTop:20}}>
      <Checkbox disabled={context.quality_tier==='certified'} checked={context.quality_gate_required} onChange={async e=>{try{
        await put(`/governance/versions/${versionId}/quality-policy`,{required:e.target.checked});await loadVersion();
      }catch(err:any){message.error(err.message);}}}>发布时要求最终人工标注通过质量门槛</Checkbox>
      <p className="muted">请将独立真值 JSON 上传并通过检查；参与评测后真值不会随普通训练授权开放。最终标注评测绑定整版快照，样本范围在报告中明确记录。真值不能由待测模型输出直接充当。</p>
      <Form form={form} layout="vertical" initialValues={{evaluation_target:'approved_annotations'}} onFinish={submit}>
        <Form.Item name="evaluation_target" label="评测对象" rules={[{required:true}]}><Select options={[
          {value:'approved_annotations',label:'最终人工标注：用于发布质量门槛'},
          {value:'model_candidate',label:'模型候选：诊断识别效果'}]}/></Form.Item>
        <Form.Item name="truth_asset_id" label="独立真值 JSON 文件" rules={[{required:true}]}><Select options={context.assets.filter((a:Row)=>!a.image&&a.status==='clean').map((a:Row)=>({value:a.id,label:a.filename}))}/></Form.Item>
        <Form.Item name="batch_id" label="识别与标注批次" rules={[{required:true}]}><Select options={context.batches.map((b:Row)=>({value:b.id,label:`${b.id} · ${labels[b.status]||b.status}`}))}/></Form.Item>
        <Alert type="info" message={'真值格式：{"schema_version":1,"images":[{"asset_id":"图像ID","persons":[{"bbox":[x,y,w,h],"labels":{"hat":"hat","sleeve":"short_sleeves","upper_color":"red"}}]}]}。需覆盖有/无人图和正负样本。'}/>
        <Button type="primary" htmlType="submit" loading={busy} style={{marginTop:16}}>提交评测任务</Button>
      </Form>
    </Card>}
    <Card title="本版本评测报告" extra={<Button onClick={loadVersion}>刷新</Button>} style={{marginTop:20}}><Table rowKey="id" dataSource={reports} columns={[
      {title:'对象',render:(_:unknown,r:Row)=>r.summary.evaluation_target==='approved_annotations'?'最终人工标注':'模型候选'},
      {title:'图片数',render:(_:unknown,r:Row)=>r.summary.unique_images},
      {title:'状态',dataIndex:'status',render:(v:string)=><Tag>{labels[v]||v}</Tag>},
      {title:'门槛',render:(_:unknown,r:Row)=>r.summary.quality_gate?.passed?'达标':r.summary.quality_gate?'未达标':'待核验'},
      {title:'创建时间',dataIndex:'created_at',render:date},
      {title:'操作',render:(_:unknown,r:Row)=><Button onClick={()=>open(r)}>查看与审核</Button>}
    ]}/></Card>
    <Card title="本版本评测任务" style={{marginTop:20}}><Table rowKey="id" dataSource={jobs} columns={[
      {title:'状态',dataIndex:'status',render:(v:string)=>labels[v]||v},{title:'进度',dataIndex:'progress'},
      {title:'失败原因',dataIndex:'error'}]}/><Link to="/jobs">查看全部任务 →</Link></Card>
    <Modal title="独立真值评测报告" open={!!active} onCancel={()=>setActive(undefined)} footer={null} width={950} destroyOnClose>
      {active&&<><Descriptions bordered size="small" column={2} items={[
        {key:'target',label:'评测对象',children:active.summary.evaluation_target==='approved_annotations'?'最终人工标注':'模型候选'},
        {key:'images',label:'独立图片',children:active.summary.unique_images},
        {key:'recall',label:'人物召回',children:active.summary.detection?.recall?.toFixed(3)??'无定义'},
        {key:'precision',label:'人物精确率',children:active.summary.detection?.precision?.toFixed(3)??'无定义'}]}/>
        {gate&&<Alert style={{marginTop:16}} type={gate.passed?'success':'warning'} message={gate.passed?'达到当前质量门槛':'未达到当前质量门槛'} description={gate.failures?.join('；')}/>}
        <details style={{marginTop:16}}><summary>查看完整指标、覆盖范围和限制</summary><pre className="preview-text">{JSON.stringify(active.summary,null,2)}</pre></details>
        <Button style={{marginTop:18}} onClick={previewTruth}>查看独立真值</Button>
        {truth&&<><pre className="preview-text">{JSON.stringify(truth,null,2)}</pre>
          {pending&&<Checkbox checked={verified} onChange={e=>setVerified(e.target.checked)}>已独立核对真值来源、标注及采样分布</Checkbox>}</>}
        {pending&&<Space style={{display:'flex',marginTop:18}}><Button type="primary" loading={busy} disabled={!verified||!truth} onClick={()=>decide(true)}>审核通过</Button>
          <Button danger loading={busy} onClick={()=>decide(false)}>退回报告</Button></Space>}
      </>}
    </Modal>
  </>;
}
