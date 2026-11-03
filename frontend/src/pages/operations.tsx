import {useEffect,useState} from 'react';
import {Alert,App,Button,Card,Form,Input,Space,Statistic} from 'antd';
import {api,post,Row} from '../api';

export function OperationsPage(){
  const {message}=App.useApp();const [status,setStatus]=useState<Row>(),[error,setError]=useState('');
  const [probe,setProbe]=useState<Row>();
  const load=async()=>{try{setStatus(await api('/admin/health-summary'));setProbe(await api('/admin/mail/probe'));setError('');}catch(e:any){setError(e.message);}};
  useEffect(()=>{load();const timer=setInterval(load,15000);return()=>clearInterval(timer);},[]);
  return <><h1>运行监控</h1><Button onClick={load}>刷新</Button>{error&&<Alert type="error" message={error}/>}
    {status&&<><Space wrap style={{marginTop:20}}>{[['queued','排队任务'],['oldest_queue_seconds','最长排队秒数'],['failed_last_hour','近一小时失败'],['mail_pending','待处理邮件']].map(([field,label])=><Card key={field}><Statistic title={label} value={status[field]}/></Card>)}</Space>
      {status.alerts.map((alert:Row,index:number)=><Alert key={index} style={{marginTop:16}} type={alert.severity==='info'?'info':'warning'} message={alert.message} description={alert.action}/>)}
      <p>计数来自真实任务与发件队列。HTTP 指标范围：{status.request_metrics_scope}。邮件排队不等于收件。</p></>}
    <Card title="真实邮件收件验证" style={{marginTop:20}}><p>SMTP 接受：{probe?.smtp_accepted_at||'未确认'} · 收件链接确认：{probe?.received_confirmed_at||'未确认'}。测试文件投递不会记成 SMTP 成功。</p>
      <Form layout="inline" onFinish={async values=>{try{const result=await post('/admin/mail/probe',values);message.info(result.message);await load();}catch(e:any){message.error(e.message);}}}>
        <Form.Item name="email" label="你可接收测试邮件的邮箱" rules={[{required:true,type:'email'}]}><Input/></Form.Item><Button htmlType="submit">发送收件探针</Button>
      </Form></Card>
    <Card title="补建历史图片持久化缩略图" style={{marginTop:20}}><p>仅管理员可发起，按版本每次最多安排 100 张；不开放图片下载权。原图保留，衍生文件计入空间配额。</p>
      <Form layout="inline" onFinish={async values=>{try{const result=await post('/admin/thumbnails/prewarm',{...values,limit:100});message.success(`安排 ${result.job_ids.length} 个任务${result.has_more?'，可继续安排剩余图片':''}`);}catch(e:any){message.error(e.message);}}}>
        <Form.Item name="version_id" label="数据版本 ID" rules={[{required:true,len:32}]}><Input/></Form.Item><Button htmlType="submit">后台补建</Button>
      </Form></Card>
  </>;
}
