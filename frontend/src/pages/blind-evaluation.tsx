import {useCallback,useContext,useEffect,useState} from 'react';
import {Link,useParams} from 'react-router-dom';
import {Alert,App,Button,Card,Form,Input,InputNumber,Modal,Popconfirm,Select,Space,Table,Tag} from 'antd';
import {api,post,Row} from '../api';
import {Session} from '../main';
import {AssetImage} from './image-pilot';

export function BlindEvaluationPage(){
  const {versionId}=useParams(),{user}=useContext(Session),{message}=App.useApp();
  const [rows,setRows]=useState<Row[]>([]),[context,setContext]=useState<Row>(),[members,setMembers]=useState<Row[]>([]),[active,setActive]=useState<Row>();
  const [error,setError]=useState(''),[busy,setBusy]=useState(false);
  const load=useCallback(async()=>{try{const result=await api('/evaluation-sets');setRows(result.items);if(versionId){
    const c=await api(`/governance/versions/${versionId}/context`);setContext(c);setMembers(await api(`/spaces/${c.space_id}/members`));}
    setError('');}catch(e:any){setError(e.message);}},[versionId]);
  useEffect(()=>{load();},[load]);
  const detail=async(id:string)=>{try{setActive(await api(`/evaluation-sets/${id}`));}catch(e:any){message.error(e.message);}};
  return <><h1>独立盲测</h1><Alert type="info" showIcon message="系统在冻结生产标注后独立抽样。保管人建立私有真值，另一位指定审核人批准；生产负责人不能读取样本清单和答案，修改生产标注须重新抽样。"/>
    {error&&<Alert type="error" message={error}/>}
    {context?.my_role==='evaluator'&&<Card title="为当前版本建立盲测" style={{marginTop:20}}><Form layout="inline" onFinish={async values=>{setBusy(true);try{const result=await post('/evaluation-sets',{...values,version_id:versionId});await load();await detail(result.id);}catch(e:any){message.error(e.message);}finally{setBusy(false);}}}>
      <Form.Item name="sample_count" label="抽样图片数" initialValue={100} rules={[{required:true}]}><InputNumber min={1} max={2000}/></Form.Item>
      <Form.Item name="reviewer_id" label="独立批准人" rules={[{required:true}]}><Select style={{width:240}} options={members.filter(m=>m.active&&m.role==='evaluator'&&m.user_id!==user?.id&&m.user_id!==context.owner_id).map(m=>({value:m.user_id,label:m.name}))}/></Form.Item>
      <Button type="primary" htmlType="submit" loading={busy}>冻结并独立抽样</Button></Form></Card>}
    <Card title="我的盲测记录" extra={<Button onClick={load}>刷新</Button>} style={{marginTop:20}}><Table rowKey="id" dataSource={rows} columns={[{title:'编号',dataIndex:'id'},{title:'状态',dataIndex:'status',render:v=><Tag>{v}</Tag>},
      {title:'样本数',render:(_:unknown,r:Row)=>r.sample_ids.length},{title:'操作',render:(_:unknown,r:Row)=><Button onClick={()=>detail(r.id)}>管理样本与真值</Button>}]}/></Card>
    <Modal title="私有盲测样本" open={!!active} onCancel={()=>setActive(undefined)} footer={null} width={1000} destroyOnClose>
      <p>不显示生产标签。按原图尺寸独立填写 bbox 和属性，不能复制生产标签作为真值。</p>
      <Button onClick={()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(active?.template,null,2)],{type:'application/json'}));const link=document.createElement('a');link.href=url;link.download='independent-truth-template.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}}>下载空白真值模板</Button>
      <div className="review-grid" style={{marginTop:20}}>{active?.samples?.map((sample:Row)=><Card key={sample.asset_id} title={sample.filename}><AssetImage id={sample.asset_id} binary alt={sample.filename} path={`/evaluation-sets/${active.id}/assets/${sample.asset_id}/preview`}/><p>{sample.asset_id} · {sample.width} × {sample.height}</p></Card>)}</div>
      {active?.creator_id===user?.id&&active?.status==='draft'&&<Form layout="vertical" style={{marginTop:20}} onFinish={async values=>{const id=active?.id;if(!id)return;try{await post(`/evaluation-sets/${id}/truth`,values);await detail(id);await load();}catch(e:any){message.error(e.message);}}}>
        <p>将完成的 JSON 上传到你个人空间的独立私有版本，通过检查后复制资产 ID。真值必须在生产版本之外。</p>
        <Link to="/upload">打开数据上传</Link><Form.Item name="truth_asset_id" label="私有真值文件资产 ID" rules={[{required:true,len:32}]}><Input/></Form.Item><Button htmlType="submit">检查并绑定真值</Button>
      </Form>}
      {active&&active.creator_id===user?.id&&['ready','used'].includes(active.status)&&<Button type="primary" style={{marginTop:20}} onClick={async()=>{const id=active.id;try{await post(`/evaluation-sets/${id}/run`);message.success('盲测任务已登记，完成后由指定审核人批准');await load();await detail(id);}catch(e:any){message.error(e.message);}}}>执行冻结快照盲测</Button>}
      {active&&<p><Link to={`/evaluations/${active.target_version_id}`}>前往独立测评工作台审核报告</Link></p>}
      {active&&active.status!=='revoked'&&<Popconfirm title="撤销此盲测？" description="评测将失效；若用于已发布认证版本，会撤回版本并撤销授权，原图保留。" onConfirm={async()=>{try{await post(`/evaluation-sets/${active.id}/revoke`);await detail(active.id);await load();}catch(e:any){message.error(e.message);}}}><Button danger>撤销此盲测证据</Button></Popconfirm>}
    </Modal></>;
}
