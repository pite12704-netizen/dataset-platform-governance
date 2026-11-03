import {useEffect,useState} from 'react';
import {Alert,App,Button,Card,Descriptions,Progress,Space,Tag} from 'antd';
import {Link} from 'react-router-dom';
import {post,Row,labels} from './api';
import {watchAction} from './agent-progress';

export function WorkflowPlan({value}:{value:Row}){
  return <Card size="small" title="待确认的处理计划" style={{marginBottom:16}}>
    <Descriptions size="small" column={1} items={[
      {key:'hash',label:'计划指纹',children:value.plan_hash},
      {key:'images',label:'处理图片',children:`${value.images} 张${value.total_images?` / 本版 ${value.total_images} 张`:''}`},
      ...(value.model?[{key:'model',label:'模型',children:value.model},{key:'template',label:'审核模板',children:`${value.template_name} · ${value.attributes.join('、')}`},
        {key:'shard',label:'分片规模',children:`每片最多 ${value.shard_size} 张`},{key:'selection',label:'选择方式',children:value.selection_method}]:
        [{key:'distance',label:'近重复距离',children:value.near_distance},{key:'budget',label:'计算预算',children:`最多 ${value.max_candidates} 个候选对 / ${value.max_comparisons} 次比较`}])
    ]}/><p>确认后才创建任务。输入、模板或模型配置变化时，本预览会失效；结果仍需要人工处理。</p>
  </Card>;
}

export function AgentTaskProgress({actionId}:{actionId:string}){
  const {message}=App.useApp();const [snapshot,setSnapshot]=useState<Row>(),[state,setState]=useState('connecting'),[busy,setBusy]=useState(false),[epoch,setEpoch]=useState(0);
  useEffect(()=>{setSnapshot(undefined);return watchAction(actionId,setSnapshot,setState);},[actionId,epoch]);
  return <Card size="small" title="后台任务进度" style={{margin:'12px 0'}}>
    {state==='forbidden'?<Alert type="warning" message="身份或数据权限已变化，进度内容已隐藏。"/>:<>
      <Space wrap><Tag>{snapshot?labels[snapshot.status]||snapshot.status:'读取中'}</Tag><span>{state==='reconnecting'?'连接暂时中断，正在恢复最新状态':state==='connecting'?'连接任务状态…':state==='finished'?'本次任务已结束':'正在接收状态快照'}</span></Space>
      {snapshot&&<><Progress percent={Math.max(0,Math.min(100,snapshot.progress))}/><p>任务 {snapshot.job_id} · 尝试 {snapshot.attempts} 次</p>
        {snapshot.progress_basis&&<p className="muted">{snapshot.progress_basis}</p>}
        {snapshot.recovery_hint&&<Alert type="info" message={snapshot.recovery_hint}/>}
        {Object.entries(snapshot.counts||{}).map(([key,value])=><Tag key={key}>{({completed_images:'已完成图片',total_images:'计划图片',scheduled_images:'已安排图片',processed_files:'已处理文件',total_files:'文件总数',checked:'已检查',failed:'失败'} as Row)[key]||key}：{String(value)}</Tag>)}
        <Space style={{marginTop:12}}><Link to="/jobs">任务中心</Link>
          {['queued','running'].includes(snapshot.status)&&<Button danger loading={busy} onClick={async()=>{setBusy(true);try{await post(`/jobs/${snapshot.job_id}/cancel`);message.success('取消请求已提交，已完成结果保留');setEpoch(v=>v+1);}catch(e:any){message.error(e.message);}finally{setBusy(false);}}}>取消任务</Button>}
          {['failed','cancelled'].includes(snapshot.status)&&!snapshot.recovery_hint&&<Button loading={busy} onClick={async()=>{setBusy(true);try{await post(`/jobs/${snapshot.job_id}/retry`);setEpoch(v=>v+1);message.success('已提交重试');}catch(e:any){message.error(e.message);}finally{setBusy(false);}}}>重试任务</Button>}
        </Space></>}
      <p className="muted">进度来自持久化任务状态。重连恢复当前快照，不重放全部历史事件；离开页面不会取消后台任务。</p>
    </>}
  </Card>;
}
