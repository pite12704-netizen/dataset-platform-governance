import {JointReviewSetup} from '../joint-review';
import {useEffect,useState} from 'react';
import {Link,useParams,useSearchParams,useNavigate} from 'react-router-dom';
import {Alert,App,Button,Card,Progress,Space,Spin,Tag} from 'antd';
import {api,post,key,Row} from '../api';
import {WholeImageReview} from './image-completeness';
import {ImageReviewsPage} from './image-reviews';

export function ReviewBatchEntry(){
  const {batchId}=useParams(),[search]=useSearchParams(),navigate=useNavigate();const [error,setError]=useState('');
  useEffect(()=>{if(search.get('history')==='1')return;let active=true;api(`/review-workbench/batches/${batchId}`).then(r=>{if(active)navigate(`/image-reviews/workbench/${r.version_id}`,{replace:true});}).catch(e=>setError(e.message));return()=>{active=false;};},[batchId,search,navigate]);
  if(search.get('history')==='1')return <ImageReviewsPage/>;
  return error?<Alert type="error" message={error}/>:<Spin/>;
}

export function ReviewInbox(){
  const [search]=useSearchParams();const v=search.get('version');
  if(search.get('history')==='1')return <ImageReviewsPage/>;
  if(v)return <ReviewWorkspace versionId={v}/>;
  return <Inbox/>;
}
function Inbox(){
  const [rows,setRows]=useState<Row[]>([]),[error,setError]=useState(''),[more,setMore]=useState(false);
  const load=async(offset=0)=>{try{const r=await api(`/review-workbench?offset=${offset}`);setRows(old=>offset?[...old,...r.items]:r.items);setMore(r.has_more);setError('');}catch(e:any){setError(e.message);}};
  useEffect(()=>{void load();},[]);
  return <><h1>图像标注与复核</h1><p>按数据集版本进入连续工作台，一张图片核对全部目标；保存或确认后自动进入下一张。</p>
    {error&&<Alert type="error" message={error}/>}<Button onClick={()=>load()}>刷新审核队列</Button>
    {!rows.length&&!error&&<Alert style={{marginTop:16}} type="info" message="暂无可见审核图片，请负责人先完成识别并整版送审。"/>}
    {rows.map(r=><Card key={r.version_id} title={`${r.name} · v${r.number}`} style={{marginTop:16}}
      extra={<Link to={`/image-reviews/workbench/${r.version_id}`}><Button type="primary">{r.can_review?'进入连续审核':'查看进度与送审'}</Button></Link>}>
      <Space wrap><Tag>图片 {r.images}</Tag><Tag>待整图核对 {r.pending}</Tag><Tag>待独立确认 {r.submitted}</Tag><Tag color="green">已完成 {r.approved}</Tag>{r.draft>0&&<Tag>待负责人送审 {r.draft}</Tag>}</Space>
    </Card>)}{more&&<Button onClick={()=>load(rows.length)}>加载更多版本</Button>}
  </>;
}

export function ReviewWorkspaceRoute(){const {versionId}=useParams();return <ReviewWorkspace versionId={versionId!}/>;}
export function ReviewWorkspace({versionId}:{versionId:string}){
  const {message}=App.useApp();const [stats,setStats]=useState<Row>(),[entry,setEntry]=useState<Row>(),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const refresh=async()=>{const r=await api(`/review-workbench/${versionId}`);setStats(r);};
  useEffect(()=>{void refresh().catch(e=>setError(e.message));},[versionId]);
  const next=async()=>{setBusy(true);const previous=entry?.image.id;try{const r=await post(`/review-workbench/${versionId}/next`,{after_image_id:previous||null});setEntry(r.image?r:undefined);setError('');if(!r.image)message.info(r.message);await refresh();}catch(e:any){setError('领取下一张失败：'+e.message);setEntry(undefined);}finally{setBusy(false);}};
  if(!stats)return error?<Alert type="error" message={error} action={<Button onClick={()=>refresh().catch(e=>setError(e.message))}>重试</Button>}/>:<Spin/>;
  return <><Space><Link to="/image-reviews">← 审核队列</Link>{stats.can_manage&&<Link to={`/datasets/${stats.dataset_id}?version=${versionId}`}>数据集详情</Link>}</Space>
    <h1>{stats.dataset_name} · v{stats.number} 连续审核</h1>{stats.assignment&&<Tag color="blue">{stats.can_annotate?'标注岗位：去重初判＋标注':stats.can_approve?'审核岗位：通过或退回':'负责人'}</Tag>}{stats.can_manage&&<JointReviewSetup stats={stats} refresh={refresh}/>}
    {!stats.assignment&&<Alert type="info" message="当前为旧版兼容流程；请负责人配置不同的标注和审核账号，启用严格岗位分离与去重联审。"/>}
    <Space wrap><Tag>已送审图片 {stats.total_images}</Tag><Tag>待核对 {stats.counts.pending||0}</Tag><Tag>待独立确认 {stats.counts.submitted||0}</Tag><Tag color="green">已完成 {stats.counts.approved||0}</Tag><Tag>我可领取 {stats.available}</Tag></Space>
    <Progress percent={Math.floor((stats.counts.approved||0)*100/Math.max(1,stats.total_images))}/>
    {error&&<Alert type="error" message={error} style={{marginBottom:12}}/>}
    {stats.can_manage&&<Card title="负责人操作" style={{marginBottom:16}}><Alert type="info" message="负责人不能自审。整版送审后，请独立审核人进入同一个版本工作台。"/>
      <Space style={{marginTop:12}}><Button disabled={busy} onClick={async()=>{setBusy(true);try{await post('/jobs/vision-batches',{version_id:versionId,kind:'vision',idempotency_key:key()});message.success('视觉计划已登记，可到任务中心跟踪');await refresh();}catch(e:any){setError(e.message);}finally{setBusy(false);}}}>运行本地目标识别</Button>
        <Button disabled={busy||!stats.draft_images} onClick={async()=>{setBusy(true);try{await post(`/image-reviews/versions/${versionId}/submit`);await refresh();message.success('已整版送审');}catch(e:any){setError(e.message);}finally{setBusy(false);}}}>整版提交人工审核</Button><Link to="/jobs">处理任务</Link><Link to={`/image-reviews?version=${versionId}&history=1`}>高级候选管理</Link></Space></Card>}
    {stats.can_review&&!entry&&<Button type="primary" size="large" loading={busy} onClick={next}>开始 / 继续审核</Button>}
    {entry&&<Card title={`${entry.batch.images[0].filename} · 整图目标审核`}>
      <WholeImageReview key={entry.image.id+':'+entry.image.claim_token} batch={entry.batch} initialClaim={entry.image} refresh={refresh} onNext={next} onPause={()=>setEntry(undefined)}/>
      <p><Link to={`/image-reviews/${entry.batch.id}?history=1`}>查看本图来源记录（高级）</Link></p>
    </Card>}
    {!stats.can_manage&&!stats.can_review&&<Alert type="info" message="此版本当前不可编辑，或需要其他独立审核人处理。"/>}
  </>;
}
