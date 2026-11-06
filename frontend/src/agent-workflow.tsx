import {useEffect,useRef,useState} from 'react';
import {Alert,App,Button,Card,Descriptions,Progress,Space,Steps,Tag} from 'antd';
import {Link} from 'react-router-dom';
import {post,Row,labels} from './api';
import {watchAction} from './agent-progress';

const workflowLabels:Record<string,string>={created:'正在保存计划',awaiting_confirmation:'等待你确认',confirmed:'已确认',
  submitted:'已提交任务',awaiting_task:'等待后台任务',completed:'流程已完成',failed:'后台任务失败',cancelled:'已取消',
  invalidated:'恢复后已失效',redacted:'内容已清除',expired:'确认已过期',interrupted:'流程中断，可查询恢复'};
workflowLabels.task_needs_attention='任务需要处理，请在任务中心重试或查看原因';

export function RecoverableWorkflow({action,onRecovered}:{action:Row,onRecovered:(value:Row)=>void}){
  const {message}=App.useApp();const [busy,setBusy]=useState(false);const flow=action.orchestration;
  if(!flow)return null;
  const current=['completed','failed','cancelled'].includes(flow.status)?3:action.status==='completed'?2:flow.status==='awaiting_confirmation'?1:0;
  return <Card size="small" title="可恢复的处理流程" style={{margin:'12px 0'}}>
    <Steps size="small" current={current} items={[{title:'生成预览'},{title:'人工确认'},{title:'后台处理'},{title:'汇总结果'}]}/>
    <p><Tag>{workflowLabels[flow.status]||flow.status}</Tag>{flow.busy?'另一请求正在处理此流程':''}</p>
    <p className="muted">流程已保存。刷新页面后可继续查看；后台处理仍由任务中心执行，恢复流程不会代替人工确认。</p>
    {flow.recoverable&&flow.status!=='awaiting_confirmation'&&<Button loading={busy} disabled={flow.busy} onClick={async()=>{
      setBusy(true);try{const next=await post(`/agent/actions/${action.id}/resume`,{mode:action.status==='completed'?'refresh':'recover'});
        onRecovered(next);message.success(next.status==='pending'?'流程已恢复，请查看预览并确认':'已读取流程与任务结果');
      }catch(e:any){message.error(e.message);}finally{setBusy(false);}
    }}>{action.status==='completed'?'更新流程结果':'恢复流程'}</Button>}
  </Card>;
}

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

export function AgentTaskProgress({actionId,workflow=false,onWorkflowUpdate}:{actionId:string,workflow?:boolean,onWorkflowUpdate?:()=>void}){
  const {message}=App.useApp();const [snapshot,setSnapshot]=useState<Row>(),[state,setState]=useState('connecting'),[busy,setBusy]=useState(false),[epoch,setEpoch]=useState(0);
  const reconciled=useRef('');
  useEffect(()=>{setSnapshot(undefined);return watchAction(actionId,setSnapshot,setState);},[actionId,epoch]);
  useEffect(()=>{
    if(!workflow||!snapshot||!['completed','failed','cancelled'].includes(snapshot.status))return;
    const key=`${actionId}:${snapshot.status}:${snapshot.attempts}`;if(reconciled.current===key)return;reconciled.current=key;
    // Only reconcile an already-submitted task. This endpoint cannot approve a plan.
    void post(`/agent/actions/${actionId}/resume`,{mode:'refresh'}).then(()=>onWorkflowUpdate?.()).catch(()=>{});
  },[actionId,snapshot,workflow,onWorkflowUpdate]);
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
