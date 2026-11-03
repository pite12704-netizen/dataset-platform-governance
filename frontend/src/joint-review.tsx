import {useEffect,useState} from 'react';
import {Alert,App,Button,Card,Checkbox,Form,Pagination,Select,Space,Tag} from 'antd';
import {Link} from 'react-router-dom';
import {api,put,Row} from './api';
import {AssetImage} from './pages/image-pilot';

export function JointReviewSetup({stats,refresh}:{stats:Row;refresh:()=>Promise<void>}){
  const {message}=App.useApp();const [members,setMembers]=useState<Row[]>([]),[reports,setReports]=useState<Row[]>([]),[busy,setBusy]=useState(false);const [form]=Form.useForm();
  useEffect(()=>{Promise.all([api(`/spaces/${stats.space_id}/members`),api(`/joint-review/versions/${stats.version_id}/reports`)]).then(([m,r])=>{setMembers(m);setReports(r.items);form.setFieldsValue(stats.assignment||{annotators:[],reviewers:[]});}).catch(e=>message.error(e.message));},[stats.version_id,stats.space_id,form,message]);
  const options=(review=false)=>members.filter(m=>m.active&&m.user_id!==stats.owner_id&&(!review||['admin','reviewer'].includes(m.role))).map(m=>({value:m.user_id,label:m.username||m.name||m.email}));
  return <Card title="标注与审核分工 / 去重联审" style={{marginBottom:16}}>
    <Alert type="info" showIcon message="标注账号与审核账号必须不同。标注人只提交，审核人确认或退回标注；发布另由管理员审批。原有记录不自动变更操作者。" description="先后台生成去重候选报告，再关联；无需先逐对审批。启用后固定图片范围，更新分工会释放当前领取，请成员先保存。未配置的旧版本仍兼容原流程。"/>
    <Form form={form} layout="vertical" onFinish={async v=>{setBusy(true);try{await put(`/joint-review/versions/${stats.version_id}`,{...v,report_id:v.report_id||null});await refresh();message.success('岗位与联合作业已配置，请分别登录不同账号');}catch(e:any){message.error(e.message);}finally{setBusy(false);}}}>
      <Form.Item name="annotators" label="标注账号" rules={[{required:true}]}><Select mode="multiple" options={options()}/></Form.Item>
      <Form.Item name="reviewers" label="审核账号（须具备组织审核权限）" rules={[{required:true}]}><Select mode="multiple" options={options(true)}/></Form.Item>
      <Form.Item name="report_id" label="关联本版去重候选（留空只启用岗位分离）"><Select allowClear options={reports.map(r=>({value:r.id,label:`${r.id.slice(0,8)} · ${r.images}张 · ${r.pairs}对 · ${r.status==='approved'?'已审核':'待联审'}`}))}/></Form.Item>
      <Space><Button htmlType="submit" type="primary" loading={busy}>保存分工与作业范围</Button><Link to={`/data-governance/${stats.version_id}`}>生成 / 查看重复候选</Link></Space>
    </Form>
  </Card>;
}

export function DuplicateAnnotationPanel({image,editable,onChange}:{image:Row;editable:boolean;onChange:(v:Row)=>void}){
  const [info,setInfo]=useState<Row>(),[choices,setChoices]=useState<Record<string,boolean>>({}),[excluded,setExcluded]=useState(!!image.snapshot?.dedup_excluded),[page,setPage]=useState(1),[error,setError]=useState('');
  const publish=(data:Row,c:Record<string,boolean>,e:boolean,edited=false)=>{const owned=data.pairs.filter((p:Row)=>p.owned);onChange({edited,duplicate_decisions:owned.filter((p:Row)=>c[p.id]!==undefined).map((p:Row)=>({pair_id:p.id,duplicate:c[p.id]})),exclude_duplicate:e,
    ready:!data.requires_resubmit&&owned.every((p:Row)=>editable?c[p.id]!==undefined:p.confirmed||p.proposed!==null)});};
  useEffect(()=>{onChange({ready:false});api(`/joint-review/images/${image.id}/similar`).then(data=>{const c=Object.fromEntries(data.pairs.filter((p:Row)=>p.owned).flatMap((p:Row)=>{const value=p.confirmed?p.approved:p.kind==='exact'?true:p.proposed;return value===null||value===undefined?[]:[[p.id,value]];}));setInfo(data);setChoices(c);publish(data,c,excluded);}).catch(e=>setError(e.message));},[image.id]);
  if(error)return <Alert type="error" message={'相似候选加载失败：'+error}/>;
  if(!info)return <Alert type="info" message="加载相似组…"/>;
  if(!info.report_id)return <Alert type="info" message="当前仅启用岗位分离，未关联重复候选报告。"/>;
  return <Card size="small" title="同屏去重初判与标注" style={{margin:'12px 0'}}>
    <p>算法报告覆盖 {info.images} 张图，{info.pair_count} 对候选，方法：{info.method}。通过本图同时确认其去重判定；全部候选终审后自动汇总报告，无候选仅表示算法未检出。</p>
    <p>参考图不等于重复。SHA 完全相同由哈希确定；近似图须判断。重复组按稳定资产 ID 保留代表，不复用近似图的框和属性。</p>
    {info.requires_resubmit&&<Alert type="warning" message="旧提交尚无联合去重初判，请退回标注补充；原标注保留。"/>}
    {!info.pairs.length&&<p>本图没有关联候选，正常标注并提交。</p>}
    <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(220px,1fr))',gap:12}}>{info.pairs.slice((page-1)*8,page*8).map((p:Row)=><Card size="small" key={p.id}>
      <AssetImage id={p.reference_asset_id} alt="可能重复的参考原图" path={`/joint-review/images/${image.id}/reference/${p.reference_asset_id}/preview`}/>
      <p>{p.kind==='exact'?'SHA256 完全相同':`近似候选 · 距离 ${p.distance}`}</p>
      {p.owned?<Select style={{width:'100%'}} disabled={!editable||p.kind==='exact'||p.confirmed} placeholder="请选择重复或不同" value={choices[p.id]===undefined?undefined:choices[p.id]?'duplicate':'different'} options={[{value:'duplicate',label:'建议重复'},{value:'different',label:'相似但不同，保留'}]} onChange={v=>{const c={...choices,[p.id]:v==='duplicate'};setChoices(c);const e=excluded&&Object.values(c).some(Boolean);setExcluded(e);publish(info,c,e,true);}}/>:<Tag>关联参考；判定随对应图片提交</Tag>}
      {!editable&&p.owned&&<p>标注初判：{p.proposed===true?'重复':p.proposed===false?'不同':p.confirmed?'沿用已独立确认报告':'未提交'}</p>}
    </Card>)}</div>
    <Pagination style={{marginTop:12}} pageSize={8} current={page} total={info.pairs.length} onChange={setPage} showSizeChanger={false}/>
    <Checkbox style={{marginTop:12}} disabled={!editable||!excluded&&!Object.values(choices).some(Boolean)} checked={excluded} onChange={e=>{setExcluded(e.target.checked);publish(info,choices,e.target.checked,true);}}>本图建议作为重复排除，不再重复画框；终审确认后仅从正式训练交付排除，原图保留</Checkbox>
    {excluded&&<Alert type="warning" message="这是重复排除，不是无目标负样本。审核人将同时核对重复关系。"/>}
  </Card>;
}
