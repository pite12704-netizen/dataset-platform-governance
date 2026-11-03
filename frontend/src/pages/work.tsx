/** 【学习标注：企业工作台页面】审批、任务、组织、审计、开发者接入、安全和治理后台。 */
import { useCallback, useContext, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Alert, App, Button, Card, Checkbox, Descriptions, Empty, Form, Input, InputNumber, Modal, Progress, Select, Space, Switch, Table, Tabs, Tag, Typography } from 'antd';
import { ArrowRightOutlined, CheckOutlined, CopyOutlined, KeyOutlined, PlusOutlined, ReloadOutlined, RobotOutlined, SendOutlined, TeamOutlined } from '@ant-design/icons';
import { api, bytes, date, labels, post, put, remove, Row } from '../api';
import { Session } from '../main';
import { PageTitle, Status } from './data';
import {AccountRecovery,RecoveryCodes} from './account-recovery';
import {SpaceQualityPolicy,SpaceResponsibilities} from './delivery';
import {OrganizationOwnership} from './organization-ownership';
import {useJobRows} from './job-rows';

function useRows(path:string){
  // 通用列表 Hook：加载接口、保存 loading 状态，并统一显示错误消息。
  const [rows,setRows]=useState<Row[]>([]),[loading,setLoading]=useState(false),[hasMore,setHasMore]=useState(false),[offset,setOffset]=useState(0),{message}=App.useApp();
  const refresh=useCallback(async()=>{setLoading(true);try{const data=await api(path);setRows(Array.isArray(data)?data:data.items);setHasMore(Boolean(data.has_more));setOffset(data.next_offset||0);}catch(e:any){message.error(e.message);}finally{setLoading(false);}},[path,message]);
  const loadMore=async()=>{if(loading||!hasMore)return;setLoading(true);try{const data=await api(path+(path.includes('?')?'&':'?')+'offset='+offset);setRows(old=>[...old,...data.items.filter((item:Row)=>!old.some(row=>row.id===item.id))]);setHasMore(Boolean(data.has_more));setOffset(data.next_offset||0);}catch(e:any){message.error(e.message);}finally{setLoading(false);}};
  useEffect(()=>{refresh();},[refresh]);return {rows,loading,refresh,hasMore,loadMore};
}

export function RequestsPage(){
  // 【功能：申请与审批】三个标签分别对应 Application、Publication 和 Grant。
  const applications=useRows('/applications?paged=true'),publications=useRows('/publications?paged=true'),grants=useRows('/grants?paged=true'),{message}=App.useApp();
  const [decision,setDecision]=useState<Row>(),[review,setReview]=useState<Row>();
  const refresh=async()=>{await Promise.all([applications.refresh(),publications.refresh(),grants.refresh()]);};
  const actions=(record:Row,kind:string)=><Space>{record.can_review&&record.status==='pending'?<><Button type="link" onClick={()=>setDecision({id:record.id,kind,approve:true})}>通过</Button><Button type="link" danger onClick={()=>setDecision({id:record.id,kind,approve:false})}>退回</Button></>:<span className="muted">—</span>}{kind==='publications'&&<Button type="link" onClick={()=>setReview(record)}>审核材料</Button>}</Space>;
  return <><PageTitle title="申请与审批" subtitle="发布审核与使用授权各有边界，每一次决定都有记录。" extra={<Button icon={<ReloadOutlined/>} onClick={refresh}>刷新</Button>}/><Space wrap>
    {applications.hasMore&&<Button loading={applications.loading} onClick={applications.loadMore}>加载更多使用申请</Button>}
    {publications.hasMore&&<Button loading={publications.loading} onClick={publications.loadMore}>加载更多发布审核</Button>}
    {grants.hasMore&&<Button loading={grants.loading} onClick={grants.loadMore}>加载更多授权</Button>}
    </Space><Tabs items={[
    {key:'applications',label:`使用申请 (${applications.rows.filter(x=>x.status==='pending').length})`,children:<Card><Table rowKey="id" dataSource={applications.rows} loading={applications.loading} columns={[{title:'数据集',dataIndex:'dataset_name'},{title:'申请人',dataIndex:'applicant_name'},{title:'使用目的',dataIndex:'purpose',ellipsis:true},{title:'期限',dataIndex:'duration_days',render:n=>`${n} 天`},{title:'状态',dataIndex:'status',render:v=><Status value={v}/>},{title:'操作',render:(_:any,r:Row)=>actions(r,'applications')}]} expandable={{expandedRowRender:r=><Descriptions column={1} items={[{key:'purpose',label:'使用目的',children:r.purpose},{key:'comment',label:'审批意见',children:r.comment||'—'},{key:'date',label:'提交时间',children:date(r.created_at)}]}/>}}/></Card>},
    {key:'publications',label:`发布审核 (${publications.rows.filter(x=>x.status==='pending').length})`,children:<Card><Alert type="info" showIcon style={{marginBottom:16}} message="组织内发布由组织管理员审批，公开上架由平台管理员审批；负责人和提交者不能自审。标注审核名单不授予发布权限。"/><Table rowKey="id" dataSource={publications.rows} loading={publications.loading} columns={[{title:'数据集',dataIndex:'dataset_name'},{title:'版本',dataIndex:'version_number',render:n=>`v${n}`},{title:'状态',dataIndex:'status',render:v=><Status value={v}/>},{title:'提交时间',dataIndex:'created_at',render:date},{title:'操作',render:(_:any,r:Row)=>actions(r,'publications')}]}/></Card>},
    {key:'grants',label:'访问授权',children:<Card><Table rowKey="id" dataSource={grants.rows} columns={[{title:'数据集',dataIndex:'dataset_name'},{title:'授权主体',render:(_:any,r:Row)=>r.project_id?'项目成员':'个人'},{title:'到期时间',dataIndex:'expires_at',render:date},{title:'状态',render:(_:any,r:Row)=><Tag color={r.effective?'green':'default'}>{r.revoked_at?'已撤销':r.effective?'生效中':'已到期'}</Tag>},{title:'操作',render:(_:any,r:Row)=>r.can_revoke&&r.effective?<Button danger type="link" onClick={()=>setDecision({id:r.id,kind:'grants'})}>撤销授权</Button>:'—'}]}/></Card>}
  ]}/><Modal title={decision?.kind==='grants'?'撤销授权':decision?.approve?'确认通过':'退回申请'} open={!!decision} onCancel={()=>setDecision(undefined)} footer={null} destroyOnClose><Form layout="vertical" onFinish={async(v)=>{try{if(decision?.kind==='grants')await post(`/grants/${decision.id}/revoke`,{reason:v.comment});else await post(`/${decision?.kind}/${decision?.id}/decision`,{approve:decision?.approve,comment:v.comment||''});message.success('处理完成');setDecision(undefined);await refresh();}catch(e:any){message.error(e.message);}}}><Form.Item name="comment" label="处理意见" rules={[{required:!decision?.approve,min:decision?.kind==='grants'?5:1}]}><Input.TextArea rows={4}/></Form.Item><Button type="primary" htmlType="submit">确认提交</Button></Form></Modal>
  <Modal title="发布审核材料" open={!!review} onCancel={()=>setReview(undefined)} footer={null} width={850}><Descriptions column={1} items={[{key:'license',label:'许可',children:review?.license},{key:'source',label:'来源',children:review?.source},{key:'card',label:'数据说明',children:<pre className="preview-text">{JSON.stringify(review?.card,null,2)}</pre>}]}/>{review?.assets_truncated&&<Alert type="info" message={`当前展示前 20 个文件，共 ${review.asset_count} 个；请到版本治理与测评工作台查看完整证据。`}/>}<Link to={`/data-governance/${review?.version_id}`}>治理与测评证据</Link><Table rowKey="id" dataSource={review?.assets} columns={[{title:'文件',dataIndex:'filename'},{title:'状态',dataIndex:'status',render:v=><Status value={v}/>},{title:'检查结果',dataIndex:'checks',render:v=><pre className="compact-json">{JSON.stringify(v,null,2)}</pre>}]}/></Modal></>;
}

export function JobsPage(){
  // 【功能：任务中心】运行中任务定时刷新；失败任务可重试，排队/运行任务可取消。
  const [page,setPage]=useState(0),data=useJobRows(page),{message}=App.useApp();
  return <><PageTitle title="处理任务" subtitle="检查、转换和打包在后台运行，进度与结果始终可追溯。"
    extra={<Button icon={<ReloadOutlined/>} loading={data.refreshing} onClick={data.refresh}>刷新</Button>}/>
    {data.error&&<Alert type="warning" showIcon message={data.error} style={{marginBottom:16}}/>}
    <p className="muted">后台自动更新状态；查询失败时保留上次结果。{data.updated&&` 最近成功查询：${new Date(data.updated).toLocaleTimeString('zh-CN')}`}</p>
    <Card><Table rowKey="id" dataSource={data.rows} loading={data.loading} pagination={false}
      locale={{emptyText:data.error?'任务查询暂不可用，当前无法确认任务列表；这不表示任务已删除。':'暂无任务'}} columns={[
      {title:'任务类型',dataIndex:'kind',render:v=><strong>{v==='thumbnail'?'缩略图生成':labels[v]||v}</strong>},
      {title:'状态',dataIndex:'status',render:v=><Status value={v}/>},
      {title:'进度',dataIndex:'progress',width:190,render:(n,r)=><><Progress percent={n} size="small"
        format={percent=>r.status==='queued'?'等待执行':r.status==='running'&&n<=1?'准备输入':`${percent}%`}/>
        {r.result?.total_files!=null&&<small>{r.result.processed_files??0} / {r.result.total_files} 个文件</small>}</>},
      {title:'尝试次数',dataIndex:'attempts'},{title:'创建时间',dataIndex:'created_at',render:date},
      {title:'操作',render:(_:any,r:Row)=><Space>
        {['queued','running'].includes(r.status)&&<Button type="link" danger onClick={async()=>{try{await post(`/jobs/${r.id}/cancel`);await data.refresh();}catch(e:any){message.error(e.message);}}}>取消</Button>}
        {['failed','cancelled'].includes(r.status)&&r.attempts<3&&<Button type="link" onClick={async()=>{try{await post(`/jobs/${r.id}/retry`);await data.refresh();}catch(e:any){message.error(e.message);}}}>重试</Button>}
        {r.kind==='vision'&&r.result?.batch_id&&<Link to={`/image-reviews/${r.result.batch_id}`}>查看复核队列</Link>}
        {r.kind==='package'&&r.status==='completed'&&<a href={`/api/v1/jobs/${r.id}/download`}>下载包</a>}
      </Space>}
    ]} expandable={{expandedRowRender:r=><><p>任务 ID：<code>{r.id}</code></p>{r.error&&<Alert type="error" message={r.error}/>}
      <pre className="preview-text">{JSON.stringify(r.result,null,2)}</pre></>}}/>
      <Space style={{marginTop:16}}><Button disabled={!page||data.loading} onClick={()=>setPage(value=>value-1)}>上一页</Button>
        <span>第 {page+1} 页，每页最多 50 项</span><Button disabled={!data.hasMore||data.loading} onClick={()=>setPage(value=>value+1)}>下一页</Button></Space>
    </Card></>;
}

export function OrganizationPage(){
  // 【功能：组织项目】维护 Membership、Project 和 ProjectMember.can_apply。
  const {spaces,refresh:refreshSession}=useContext(Session),{message}=App.useApp();
  const [selected,setSelected]=useState(''),[members,setMembers]=useState<Row[]>([]),[projects,setProjects]=useState<Row[]>([]),[modal,setModal]=useState(''),[project,setProject]=useState<Row>();
  const space=spaces.find(x=>x.id===selected),canAdmin=space?.my_role==='admin';
  const load=async()=>{if(!selected)return;try{setMembers(await api(`/spaces/${selected}/members`));setProjects(await api(`/spaces/${selected}/projects`));}catch(e:any){message.error(e.message);}};
  useEffect(()=>{if(!selected&&spaces.length)setSelected(spaces[0].id);},[spaces,selected]);useEffect(()=>{load();},[selected]);
  return <><PageTitle title="组织与项目" subtitle="以组织归属数据，以项目协作使用。" extra={<Button type="primary" icon={<PlusOutlined/>} onClick={()=>setModal('space')}>创建组织</Button>}/><Select value={selected} style={{width:300,marginBottom:24}} onChange={setSelected} options={spaces.map(x=>({value:x.id,label:`${x.name} · ${labels[x.my_role]}`}))}/>{space&&<>{canAdmin&&<SpaceQualityPolicy space={space} refresh={refreshSession}/>}<SpaceResponsibilities space={space}/>{canAdmin&&space.kind==='organization'&&<OrganizationOwnership space={space} members={members}/>}<div className="stat-grid"><Card><StatisticLike label="空间类型" value={space.kind==='personal'?'个人空间':'组织空间'}/></Card><Card><StatisticLike label="成员数量" value={members.filter(x=>x.active).length}/></Card><Card><StatisticLike label="已使用 / 配额" value={`${bytes(space.used_bytes)} / ${bytes(space.quota_bytes)}`}/></Card><Card><StatisticLike label="项目数量" value={projects.length}/></Card></div><Card title="空间成员" extra={canAdmin&&space.kind==='organization'&&<Button onClick={()=>setModal('member')} icon={<PlusOutlined/>}>添加 / 更新成员</Button>}><Table rowKey="id" dataSource={members} columns={[{title:'成员',dataIndex:'name'},{title:'账号',dataIndex:'username'},{title:'邮箱',dataIndex:'email'},{title:'角色',dataIndex:'role',render:x=>labels[x]},{title:'状态',dataIndex:'active',render:x=><Tag color={x?'green':'default'}>{x?'有效':'已移除'}</Tag>}]}/></Card><Card style={{marginTop:24}} title="项目空间" extra={canAdmin&&<Button onClick={()=>setModal('project')}>新建项目</Button>}><Table rowKey="id" dataSource={projects} columns={[{title:'项目名称',dataIndex:'name'},{title:'创建时间',dataIndex:'created_at',render:date},{title:'成员管理',render:(_:any,r:Row)=>canAdmin?<Button type="link" onClick={()=>{setProject(r);setModal('project-member');}}>设置项目成员</Button>:'—'}]}/></Card></>}
    <Modal title={{space:'创建组织',member:'添加 / 更新组织成员',project:'创建项目','project-member':'设置项目成员'}[modal]||''} open={!!modal} onCancel={()=>setModal('')} footer={null} destroyOnClose><Form layout="vertical" initialValues={{role:'member',active:true,can_apply:false}} onFinish={async(values)=>{try{if(modal==='space'){const sp=await post('/spaces',values);await refreshSession();setSelected(sp.id);}else if(modal==='member')await post(`/spaces/${selected}/members`,values);else if(modal==='project')await post(`/spaces/${selected}/projects`,values);else await post(`/spaces/${selected}/projects/${project?.id}/members`,values);setModal('');await load();await refreshSession();message.success('已保存');}catch(e:any){message.error(e.message);}}}>
      {['space','project'].includes(modal)&&<Form.Item name="name" label="名称" rules={[{required:true}]}><Input/></Form.Item>}
      {modal==='member'&&<><Form.Item name="email" label="现有成员账号或邮箱" rules={[{required:true}]}><Input/></Form.Item><Form.Item name="role" label="组织角色"><Select options={['admin','reviewer','evaluator','contributor','member'].map(x=>({value:x,label:labels[x]}))}/></Form.Item><Form.Item name="active" label="成员资格" valuePropName="checked"><Switch checkedChildren="有效" unCheckedChildren="移除"/></Form.Item></>}
      {modal==='project-member'&&<><Form.Item name="user_id" label="选择组织成员" rules={[{required:true}]}><Select options={members.filter(x=>x.active).map(x=>({value:x.user_id,label:x.name}))}/></Form.Item><Form.Item name="can_apply" valuePropName="checked"><Checkbox>允许代表项目申请数据</Checkbox></Form.Item><Form.Item name="active" valuePropName="checked"><Checkbox>项目成员资格有效</Checkbox></Form.Item></>}
      <Button htmlType="submit" type="primary">保存</Button>
    </Form></Modal></>;
}
function StatisticLike({label,value}:{label:string;value:React.ReactNode}){return <div className="statistic-like"><span>{label}</span><strong>{value}</strong></div>;}

export function AuditPage(){
  // 【功能：审计】普通用户查看本人记录；空间管理员可按空间查看。
  const {spaces}=useContext(Session);const [space,setSpace]=useState(''),data=useRows(`/audits${space?'?space_id='+space:''}`);
  return <><PageTitle title="操作记录" subtitle="查看上传、审批、授权和数据访问的完整操作轨迹。"/><Select value={space} style={{width:280,marginBottom:20}} onChange={setSpace} options={[{value:'',label:'我的操作记录'},...spaces.filter(x=>x.my_role==='admin').map(x=>({value:x.id,label:x.name}))]}/><Card><Table rowKey="id" dataSource={data.rows} loading={data.loading} columns={[{title:'操作',dataIndex:'action',render:x=><code>{x}</code>},{title:'资源',dataIndex:'resource_id',ellipsis:true},{title:'时间',dataIndex:'created_at',render:date},{title:'详情',dataIndex:'details',render:x=><span className="muted">{JSON.stringify(x)}</span>}]}/></Card></>;
}

export function CredentialsPage(){
  // 【功能：外部接入】服务凭据只显示一次；MCP 区域展示只读工具和配置状态。
  const data=useRows('/credentials'),{config,user}=useContext(Session),{message}=App.useApp();const [modal,setModal]=useState(false),[token,setToken]=useState('');
  return <><PageTitle title="开发者接入" subtitle="让外部程序与 Agent 通过受控接口使用数据。" extra={<Button type="primary" icon={<KeyOutlined/>} onClick={()=>setModal(true)}>创建服务凭据</Button>}/><div className="two-column"><Card title="平台 API"><p>服务凭据继承创建者的数据权限，并进一步限制可调用的接口范围。</p><code>/api/v1/datasets</code><p className="muted">通过 Authorization: Bearer 传递凭据。凭据仅展示一次，请妥善保管。</p></Card><Card title="MCP 接入"><Status value={config.mcp_enabled?'active':'未配置'}/><p>使用企业 OAuth 身份访问只读 MCP 工具。</p><code>/mcp</code><p className="muted">元数据发现：/.well-known/oauth-protected-resource</p><Space wrap>{["search_datasets","get_version","compare_versions","get_quality","get_sample","get_job"].map(tool=><Tag key={tool}>{tool}</Tag>)}</Space><p className="muted">按令牌范围提供只读工具。样例读取限制为 16 KiB / 20 行，仍需样例展示许可或版本授权。</p></Card></div><Card title="我的服务凭据"><Table rowKey="id" dataSource={data.rows} columns={[{title:'名称',dataIndex:'name'},{title:'权限范围',dataIndex:'scopes',render:x=>x.map((s:string)=><Tag key={s}>{s}</Tag>)},{title:'到期时间',dataIndex:'expires_at',render:date},{title:'状态',dataIndex:'revoked',render:v=>v?'已撤销':'有效'},{title:'操作',render:(_:any,r:Row)=>!r.revoked&&<Button type="link" danger onClick={async()=>{try{await remove(`/credentials/${r.id}`);await data.refresh();}catch(e:any){message.error(e.message);}}}>撤销</Button>}]}/></Card>
  <Modal title="创建服务凭据" open={modal} onCancel={()=>setModal(false)} footer={null} destroyOnClose><Form layout="vertical" initialValues={{scopes:['catalog:read'],duration_days:30}} onFinish={async(values)=>{try{const result=await post('/credentials',values);setToken(result.token);setModal(false);await data.refresh();}catch(e:any){message.error(e.message);}}}><Form.Item name="name" label="用途名称" rules={[{required:true}]}><Input/></Form.Item><Form.Item name="scopes" label="权限范围" rules={[{required:true}]}><Checkbox.Group options={['catalog:read','content:read','jobs:read','agent:read',...(user?.platform_admin?['metrics:read']:[])]}/></Form.Item><Form.Item name="duration_days" label="有效期（天）"><InputNumber min={1} max={90}/></Form.Item><Button type="primary" htmlType="submit">创建</Button></Form></Modal><Modal title="保存服务凭据" open={!!token} onCancel={()=>setToken('')} onOk={()=>setToken('')}><Alert type="warning" message="此凭据只展示一次，关闭后无法再次查看。"/><Typography.Paragraph copyable style={{marginTop:20}} code>{token}</Typography.Paragraph></Modal></>;
}

export function SecurityPage(){
  const {user,refresh,login}=useContext(Session),{message}=App.useApp();
  const [setup,setSetup]=useState<Row>();const [recoveryCodes,setRecoveryCodes]=useState<string[]>([]);
  if(user?.recovery_only)return <AccountRecovery/>;
  return <><PageTitle title="账号安全" subtitle="管理登录账号、密码和认证器动态验证码。"/>
    {user?.must_change_password&&<Alert style={{marginBottom:20}} type="warning" showIcon message="请先修改初始或重置密码" description="完成修改后重新登录，才能访问数据、任务及其他业务功能。"/>}
    <Card title="修改密码" style={{maxWidth:700,marginBottom:20}}><Form layout="vertical" onFinish={async values=>{try{
      const result=await post('/auth/change-password',{current_password:values.current_password,password:values.password});
      message.success(result.message);await refresh();login();
    }catch(e:any){message.error(e.message);}}}>
      <Form.Item name="current_password" label="当前密码" rules={[{required:true}]}><Input.Password autoComplete="current-password"/></Form.Item>
      <Form.Item name="password" label="新密码" rules={[{required:true,min:12,max:128}]}><Input.Password autoComplete="new-password"/></Form.Item>
      <Form.Item name="confirm_password" label="确认新密码" dependencies={['password']} rules={[{required:true},({getFieldValue})=>({validator(_,value){return !value||value===getFieldValue('password')?Promise.resolve():Promise.reject(new Error('两次密码不一致'));}})]}><Input.Password autoComplete="new-password"/></Form.Item>
      <Button htmlType="submit" type="primary">修改密码并退出旧会话</Button>
    </Form></Card>
    {!user?.must_change_password&&<Card title="登录账号" style={{maxWidth:700,marginBottom:20}}><p>当前账号：{user?.username||'尚未设置，可使用邮箱登录'} · 邮箱：{user?.email||'未绑定'}</p>
      <Form key={user?.username||'unset'} layout="vertical" initialValues={{username:user?.username||''}} onFinish={async values=>{try{await put('/auth/username',values);await refresh();message.success('登录账号已更新');}catch(e:any){message.error(e.message);}}}>
        <Form.Item name="username" label="登录账号" rules={[{required:true,pattern:/^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$/,message:'使用 3–32 位字母、数字、点、横线或下划线'}]}><Input autoComplete="username"/></Form.Item>
        <Form.Item name="current_password" label="输入当前密码确认" rules={[{required:true}]}><Input.Password autoComplete="current-password"/></Form.Item><Button htmlType="submit">保存登录账号</Button>
      </Form></Card>}
    {!user?.must_change_password&&<AccountRecovery/>}<RecoveryCodes codes={recoveryCodes} onClose={()=>setRecoveryCodes([])}/>
    <Card title="双因素认证 MFA" style={{maxWidth:700}}><Tag color={user?.mfa_enabled?'green':'gold'}>{user?.mfa_enabled?'已启用':'尚未启用'}</Tag><p>在 TOTP 认证器中绑定后生成六位动态验证码，不通过邮件或短信发送。管理员操作需要 MFA；重置密码不会解除 MFA。</p>
      {!user?.mfa_enabled&&!setup&&<Button type="primary" onClick={async()=>{try{setSetup(await post('/auth/mfa/setup'));}catch(e:any){message.error(e.message);}}}>开始绑定</Button>}
      {setup&&<><Typography.Paragraph copyable code>{setup.secret}</Typography.Paragraph><Form onFinish={async values=>{try{const result=await post('/auth/mfa/confirm',values);setRecoveryCodes(result.recovery_codes||[]);setSetup(undefined);await refresh();message.success('双因素认证已启用');}catch(e:any){message.error(e.message);}}}>
        <Form.Item name="code" rules={[{required:true,len:6}]}><Input placeholder="输入认证器中的 6 位验证码" maxLength={6}/></Form.Item><Button type="primary" htmlType="submit">确认绑定</Button></Form></>}
    </Card></>;
}

export function GovernancePage(){
  const {user,config}=useContext(Session),{message}=App.useApp();
  const [tab,setTab]=useState('datasets'),[rows,setRows]=useState<Row[]>([]),[loading,setLoading]=useState(false),[edit,setEdit]=useState<Row>();
  const [mail,setMail]=useState<Row>(),[busy,setBusy]=useState(false);
  const load=async()=>{if(!user?.platform_admin)return;setLoading(true);try{
    setRows(await api('/admin/'+tab));if(tab==='users')setMail(await api('/admin/mail/status'));
  }catch(e:any){message.error(e.message);}finally{setLoading(false);}};
  useEffect(()=>{load();},[tab]);
  if(!user?.platform_admin)return <Alert type="warning" message="此页面需要平台管理员权限。"/>;
  const columns:Record<string,any[]>={
    datasets:[{title:'数据集',dataIndex:'name'},{title:'可见范围',dataIndex:'visibility',render:(v:string)=>labels[v]},{title:'状态',dataIndex:'status',render:(v:string)=>labels[v]},{title:'治理操作',render:(_:any,r:Row)=><Button type="link" onClick={()=>setEdit({...r,kind:'status'})}>变更状态</Button>}],
    users:[{title:'用户',dataIndex:'name'},{title:'登录账号',dataIndex:'username',render:(v:string)=>v||'未设置'},{title:'邮箱',dataIndex:'email',render:(v:string)=>v||'未绑定'},
      {title:'身份',dataIndex:'platform_admin',render:(v:boolean)=>v?'平台管理员':'普通用户'},{title:'MFA',dataIndex:'mfa_enabled',render:(v:boolean)=>v?'已启用':'未启用'},
      {title:'改密要求',dataIndex:'must_change_password',render:(v:boolean)=>v?'下次登录须改密':'—'},
      {title:'状态',dataIndex:'active',render:(v:boolean,r:Row)=><Switch checked={v} disabled={r.id===user.id} onChange={async(active)=>{try{await put(`/admin/users/${r.id}`,{active});await load();}catch(e:any){message.error(e.message);}}}/>},
      {title:'恢复访问',render:(_:any,r:Row)=>r.id!==user.id&&<Button type="link" disabled={!r.active} onClick={()=>setEdit({...r,kind:'password'})}>重置密码</Button>}],
    reports:[{title:'数据集 ID',dataIndex:'dataset_id'},{title:'举报说明',dataIndex:'reason'},{title:'状态',dataIndex:'status'},{title:'处理',render:(_:any,r:Row)=>r.status==='pending'?<Button type="link" onClick={()=>setEdit({...r,kind:'report'})}>处理举报</Button>:r.resolution}],
    spaces:[{title:'空间',dataIndex:'name'},{title:'已使用',dataIndex:'used_bytes',render:bytes},{title:'上传预留',dataIndex:'reserved_bytes',render:bytes},{title:'配额',dataIndex:'quota_bytes',render:bytes},{title:'操作',render:(_:any,r:Row)=><Button type="link" onClick={()=>setEdit({...r,kind:'quota'})}>调整配额</Button>}]
  };
  return <><p><Link to="/operations">运行监控与持久化预览管理 →</Link></p><PageTitle title="治理后台" subtitle="管理账号、内容与资源配额，每次管理操作留下审计记录。" extra={<Space wrap><Button onClick={load}>刷新</Button>
    {config.deployment==='private'&&<Button type="primary" onClick={()=>setEdit({kind:'local'})}>创建本地账号</Button>}
    <Button disabled={config.mail_delivery_mode!=='smtp'} onClick={()=>setEdit({kind:'invite'})}>邮件邀请</Button></Space>}/>
    {!user.mfa_enabled&&<Alert style={{marginBottom:20}} type="warning" message={<span>管理员应启用双因素认证。<Link to="/security">前往账号安全</Link></span>}/>}
    <Tabs activeKey={tab} onChange={setTab} items={[
      ['datasets','数据集治理'],['users','账号管理'],['reports','举报处置'],['spaces','空间配额']].map(([key,label])=>({key,label}))}/>
    {tab==='users'&&<Card title="注册与邮件状态" style={{marginBottom:20}}><p>自行注册：{config.registration_enabled?'开启':'关闭'} · {config.registration_email_required?'注册字段：邮箱必填':'注册字段：邮箱可选'}</p>
      {mail&&<><Alert type={mail.delivery_mode==='smtp'?'info':'warning'} message={mail.delivery_mode==='smtp'?'SMTP 已配置，接受请求不等于收件箱送达':mail.delivery_mode==='spool'?'测试模式：只写本地邮件文件，不发真实邮件':'邮件服务未配置：请使用本地账号创建或管理员重置'} description={`待处理 ${mail.pending} 封，重试达到上限 ${mail.retry_exhausted} 封，SMTP 已接受 ${mail.submitted_to_smtp} 封，测试文件 ${mail.spooled} 封。`}/><p className="muted">不展示收件地址、邮件正文或重置链接。修改配置后需要统一重启 API 与 Worker。</p></>}
    </Card>}
    <Card><Table rowKey="id" loading={loading} dataSource={rows} columns={columns[tab]}/></Card>
    <Modal title={edit?.kind==='password'?'管理员重置密码':edit?.kind==='local'?'创建本地账号':'管理操作'} open={!!edit} onCancel={()=>!busy&&setEdit(undefined)} footer={null} destroyOnClose>
      <Form layout="vertical" preserve={false} onFinish={async values=>{setBusy(true);try{
        const payload=Object.fromEntries(Object.entries(values).filter(([name,value])=>name!=='confirm_password'&&value!==''&&value!==undefined));
        if(edit?.kind==='invite')await post('/admin/users/invite',payload);
        if(edit?.kind==='local')await post('/admin/users/local',payload);
        if(edit?.kind==='password'){const result=await post(`/admin/users/${edit.id}/reset-password`,payload);message.info(result.message);}
        if(edit?.kind==='status')await post(`/admin/datasets/${edit.id}/status`,payload);
        if(edit?.kind==='report')await post(`/admin/reports/${edit.id}/resolve`,payload);
        if(edit?.kind==='quota')await put(`/admin/spaces/${edit.id}/quota`,{quota_bytes:Math.round(values.gb*1024**3)});
        setEdit(undefined);message.success('操作已保存');await load();
      }catch(e:any){message.error(e.message);}finally{setBusy(false);}}}>
        {['invite','local'].includes(edit?.kind)&&<><Form.Item name="name" label="姓名" rules={[{required:true}]}><Input/></Form.Item>
          {edit?.kind==='local'&&<Form.Item name="username" label="登录账号" rules={[{required:true,pattern:/^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$/,message:'使用 3–32 位字母、数字、点、横线或下划线'}]}><Input autoComplete="off"/></Form.Item>}
          <Form.Item name="email" label={edit?.kind==='invite'?'邮箱':'邮箱（选填）'} rules={[{required:edit?.kind==='invite',type:'email'}]}><Input/></Form.Item>
        </>}
        {['local','password'].includes(edit?.kind)&&<><Alert style={{marginBottom:16}} type="info" message="请通过已确认的渠道交付初始密码；不通过邮件发送明文密码。用户下次登录必须改密。重置会撤销旧会话、服务和导入凭据，并保留 MFA。"/>
          <Form.Item name="password" label="初始密码" rules={[{required:true,min:12,max:128}]}><Input.Password autoComplete="new-password"/></Form.Item>
          <Form.Item name="confirm_password" label="确认初始密码" dependencies={['password']} rules={[{required:true},({getFieldValue})=>({validator(_,value){return !value||value===getFieldValue('password')?Promise.resolve():Promise.reject(new Error('两次密码不一致'));}})]}><Input.Password autoComplete="new-password"/></Form.Item>
        </>}
        {edit?.kind==='password'&&<Form.Item name="reason" label="重置原因（不要填写密码）" rules={[{required:true,min:5,max:500}]}><Input.TextArea rows={3}/></Form.Item>}
        {edit?.kind==='invite'&&<Alert style={{marginBottom:16}} type="info" message="将提交一次性密码设置链接到配置的 SMTP 邮件服务器；成功提交不代表收件箱送达。"/>}
        {edit?.kind==='status'&&<Form.Item name="status" label="目标状态" rules={[{required:true}]}><Select options={['active','unlisted','blocked'].map(x=>({value:x,label:labels[x]}))}/></Form.Item>}
        {['status','report'].includes(edit?.kind)&&<Form.Item name="reason" label="处理说明" rules={[{required:true,min:5}]}><Input.TextArea rows={4}/></Form.Item>}
        {edit?.kind==='quota'&&<Form.Item name="gb" label="存储配额（GB）" rules={[{required:true}]}><InputNumber min={0.1}/></Form.Item>}
        <Button htmlType="submit" type="primary" loading={busy}>确认保存</Button>
      </Form>
    </Modal></>;
}
