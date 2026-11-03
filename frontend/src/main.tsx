/** 【学习标注：前端入口】全局会话、侧边栏、登录弹窗和页面路由都从这里装配。 */
import React, { useCallback, useEffect, useState } from 'react';
import ReactDOM from 'react-dom/client';
import { createBrowserRouter, RouterProvider, Link, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { App as AntApp, Avatar, Badge, Button, ConfigProvider, Dropdown, Empty, Form, Input, Modal, Select, Space, Spin, Tag } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { AppstoreOutlined, BellOutlined, CheckSquareOutlined, DatabaseOutlined, FolderOpenOutlined, LogoutOutlined, PlusOutlined, RobotOutlined, SafetyCertificateOutlined, SettingOutlined, TeamOutlined, ThunderboltOutlined, UserOutlined } from '@ant-design/icons';
import { api, post, Row } from './api';
import { Catalog, DatasetPage, Overview, UploadPage } from './pages/data';
import { AuditPage, CredentialsPage, GovernancePage, JobsPage, OrganizationPage, RequestsPage, SecurityPage } from './pages/work';
import './style.css';

const AgentPage=React.lazy(()=>import('./pages/agent').then(m=>({default:m.AgentPage}))); 
const ImagePilotPage=React.lazy(()=>import('./pages/image-pilot').then(m=>({default:m.ImagePilotPage}))); 
const ReviewInbox=React.lazy(()=>import('./pages/review-workbench').then(m=>({default:m.ReviewInbox})));
const ReviewWorkspaceRoute=React.lazy(()=>import('./pages/review-workbench').then(m=>({default:m.ReviewWorkspaceRoute})));
const ReviewBatchEntry=React.lazy(()=>import('./pages/review-workbench').then(m=>({default:m.ReviewBatchEntry})));
const DataGovernancePage=React.lazy(()=>import('./pages/data-governance').then(m=>({default:m.DataGovernancePage}))); 
const DeliveryPage=React.lazy(()=>import('./pages/delivery').then(m=>({default:m.DeliveryPage})));
const EvaluationsPage=React.lazy(()=>import('./pages/evaluations').then(m=>({default:m.EvaluationsPage})));
const BlindEvaluationPage=React.lazy(()=>import('./pages/blind-evaluation').then(m=>({default:m.BlindEvaluationPage})));
const OperationsPage=React.lazy(()=>import('./pages/operations').then(m=>({default:m.OperationsPage})));
const ImportsPage=React.lazy(()=>import('./pages/imports').then(m=>({default:m.ImportsPage})));
const DirectoryImportPage=React.lazy(()=>import('./pages/directory-import').then(m=>({default:m.DirectoryImportPage})));

export const Session = React.createContext<{user: Row | null; config: Row; spaces: Row[]; refresh: () => Promise<void>; login: () => void}>({user: null, config: {}, spaces: [], refresh: async () => {}, login: () => {}});

function Shell() {
  // Shell 启动时读取平台配置和当前用户；受保护页面由 protectedPage 统一拦截未登录状态。
  const [user, setUser] = useState<Row | null>(null), [config, setConfig] = useState<Row>({}), [spaces, setSpaces] = useState<Row[]>([]);
  const [loading, setLoading] = useState(true), [auth, setAuth] = useState(false), [register, setRegister] = useState(false), [notes, setNotes] = useState<Row[]>([]);
  const [forgot,setForgot]=useState(false),[resend,setResend]=useState(false);
  const [form] = Form.useForm(); const { message } = AntApp.useApp();
  const location = useLocation(), navigate = useNavigate();
  const refresh = useCallback(async () => {
    try { const me = await api('/auth/me'); setUser(me);
      if(me.must_change_password||me.recovery_only){setSpaces([]);setNotes([]);return;}
      setSpaces(await api('/spaces')); setNotes(await api('/notifications')); }
    catch { setUser(null); setSpaces([]); }
  }, []);
  useEffect(() => { Promise.all([api('/config').then(setConfig), refresh()]).catch(e => message.error(e.message)).finally(() => setLoading(false)); }, [refresh, message]);
  useEffect(()=>{if((user?.must_change_password||user?.recovery_only)&&location.pathname!=='/security')navigate('/security');},[user?.must_change_password,user?.recovery_only,location.pathname,navigate]);
  const login = () => { setRegister(false); setAuth(true); };
  const authSubmit = async (values: Row) => {
    const payload=Object.fromEntries(Object.entries(values).filter(([name,value])=>name!=='confirm_password'&&value!==''&&value!==undefined));
    try { if (register) { const result=await post('/auth/register', payload); message.success(result.message); setRegister(false);form.resetFields(); }
      else { const result = await post('/auth/login', payload); setAuth(false); await refresh(); if (result.mfa_setup_required||result.password_change_required||result.mfa_recovery_required) navigate('/security'); } }
    catch(e: any) { message.error(e.message); }
  };
  const menuGroups = [
    {title:'数据集门户',items:[{path:'/',label:'数据集广场',icon:<AppstoreOutlined/>},{path:'/overview',label:'工作台',icon:<DatabaseOutlined/>},{path:'/mine',label:'我的数据集',icon:<FolderOpenOutlined/>},{path:'/requests',label:'申请与审批',icon:<CheckSquareOutlined/>}]},
    {title:'数据生产与治理',items:[{path:'/upload',label:'上传数据',icon:<PlusOutlined/>},{path:'/imports',label:'批量导入',icon:<FolderOpenOutlined/>},{path:'/image-pilot',label:'图像识别',icon:<AppstoreOutlined/>},{path:'/jobs',label:'处理任务',icon:<ThunderboltOutlined/>}]},
    {title:'标注与复核',items:[{path:'/image-reviews',label:'图像标注与复核',icon:<CheckSquareOutlined/>}]},
    {title:'独立测评',items:[{path:'/evaluations',label:'测评工作台',icon:<SafetyCertificateOutlined/>}]},
    {title:'协作与接入',items:[{path:'/organizations',label:'组织与项目',icon:<TeamOutlined/>},{path:'/agent',label:'数据工作助手',icon:<RobotOutlined/>},{path:'/credentials',label:'开发者接入',icon:<SettingOutlined/>},{path:'/audit',label:'操作记录',icon:<UserOutlined/>}]}
  ];
  const menus=menuGroups.flatMap(group=>group.items);
  const selected=(path:string)=>location.pathname===path||(path!=='/'&&location.pathname.startsWith(path+'/'));
  const protectedPage = (page: React.ReactNode) => user ? page : <div className="login-empty"><SafetyCertificateOutlined/><h2>登录后管理你的数据资产</h2><p>上传数据、协作审批与访问授权都在这里。</p><Button type="primary" onClick={login}>登录平台</Button></div>;
  if (loading) return <div className="loading"><Spin size="large"/></div>;
  return <Session.Provider value={{user,config,spaces,refresh,login}}><div className="shell">
    <aside className="sidebar"><Link className="brand" to="/"><span className="brand-symbol"><DatabaseOutlined/></span><div>Data Commons<small>企业数据集平台</small></div></Link>
      <div className="workspace-pill"><span className="live-dot"/>{config.deployment === 'private' ? '企业私有空间' : '开放数据社区'}<Tag bordered={false}>{config.environment === 'production' ? '正式' : '试运营'}</Tag></div>
      <div className="sidebar-nav">{menuGroups.map(group=><React.Fragment key={group.title}><div className="nav-label">{group.title}</div><nav>{group.items.map(m=><Link key={m.path} className={selected(m.path)?'selected':''} to={m.path}>{m.icon}<span>{m.label}</span>{m.path==='/agent'&&<small>AI</small>}</Link>)}</nav></React.Fragment>)}
      {user?.platform_admin&&<><div className="nav-label">平台管理</div><nav><Link className={selected('/governance')?'selected':''} to="/governance"><SafetyCertificateOutlined/>治理后台</Link></nav></>}</div>
      <div className="sidebar-bottom"><div className="shield"><SafetyCertificateOutlined/></div><strong>数据有序流动</strong><p>每一份数据都有来源<br/>每一次访问都有依据</p><span>版本管理 · 授权访问 · 全程审计</span></div>
    </aside>
    <div className="main"><header className="topbar"><span className="breadcrumb">工作空间 <span>/</span> {menus.find(m=>m.path===location.pathname)?.label || '数据资产'}</span><div className="topbar-right">
      <Tag className="environment-tag">{config.deployment === 'private' ? '内网模式' : '公开平台'}</Tag>
      {user ? <><Dropdown menu={{items: notes.length ? notes.slice(0,8).map(n=>({key:n.id,label:<div style={{maxWidth:280}}><strong>{n.title}</strong><div>{n.body}</div></div>,onClick:async()=>{await post(`/notifications/${n.id}/read`);await refresh();}})) : [{key:'empty',label:'暂无通知'}]}} trigger={['click']}><Button aria-label="通知" type="text" icon={<Badge dot={notes.some(n=>!n.read)}><BellOutlined/></Badge>}/></Dropdown>
      <Dropdown menu={{items:[{key:'security',label:'账号安全',onClick:()=>navigate('/security')},{key:'logout',label:'退出登录',icon:<LogoutOutlined/>,onClick:async()=>{await post('/auth/logout');await refresh();navigate('/');}}]}}><Button type="text"><Avatar size="small" style={{background:'#dbe9df',color:'#244b3d'}}>{user.name?.[0]}</Avatar>{user.name}</Button></Dropdown></> : <Button onClick={login}>登录 / 注册</Button>}
    </div></header><main className="content"><React.Suspense fallback={<Spin/>}><Routes>
      {/* 【功能路由】URL 在这里映射到各业务页面；学习某功能时可从对应组件反向追 API。 */}
      <Route path="/" element={<Catalog/>}/><Route path="/datasets/:id" element={<DatasetPage/>}/>
      <Route path="/overview" element={protectedPage(<Overview/>)}/><Route path="/mine" element={protectedPage(<Catalog mine/>)}/>
      <Route path="/upload" element={protectedPage(<UploadPage/>)}/><Route path="/requests" element={protectedPage(<RequestsPage/>)}/>
      <Route path="/jobs" element={protectedPage(<JobsPage/>)}/><Route path="/organizations" element={protectedPage(<OrganizationPage/>)}/>
      <Route path="/agent" element={protectedPage(<AgentPage/>)}/><Route path="/credentials" element={protectedPage(<CredentialsPage/>)}/>
      <Route path="/image-pilot" element={protectedPage(<ImagePilotPage/>)}/><Route path="/image-pilot/:versionId" element={protectedPage(<ImagePilotPage/>)}/>
      <Route path="/image-reviews" element={protectedPage(<ReviewInbox/>)}/><Route path="/image-reviews/workbench/:versionId" element={protectedPage(<ReviewWorkspaceRoute/>)}/><Route path="/image-reviews/:batchId" element={protectedPage(<ReviewBatchEntry/>)}/>
      <Route path="/deliveries/:versionId" element={protectedPage(<DeliveryPage/>)}/>
      <Route path="/data-governance/:versionId" element={protectedPage(<DataGovernancePage/>)}/>
      <Route path="/blind-evaluations" element={protectedPage(<BlindEvaluationPage/>)}/><Route path="/blind-evaluations/:versionId" element={protectedPage(<BlindEvaluationPage/>)}/><Route path="/operations" element={protectedPage(<OperationsPage/>)}/>
      <Route path="/evaluations" element={protectedPage(<EvaluationsPage/>)}/><Route path="/evaluations/:versionId" element={protectedPage(<EvaluationsPage/>)}/>
      <Route path="/imports" element={protectedPage(<ImportsPage/>)}/><Route path="/imports/local" element={protectedPage(<DirectoryImportPage/>)}/><Route path="/imports/:batchId" element={protectedPage(<ImportsPage/>)}/>
      <Route path="/audit" element={protectedPage(<AuditPage/>)}/><Route path="/governance" element={protectedPage(<GovernancePage/>)}/>
      <Route path="/security" element={protectedPage(<SecurityPage/>)}/><Route path="*" element={<Navigate to="/"/>}/>
    </Routes></React.Suspense></main><footer>DATA COMMONS <span>让数据被发现，让协作有依据。</span><a href="/api/v1/config" target="_blank">平台状态 ↗</a></footer></div>
    <Modal title={register?'创建账号':'欢迎回来'} open={auth} onCancel={()=>setAuth(false)} footer={null} destroyOnClose><p className="muted">连接数据与每一种可能。</p><Form form={form} layout="vertical" onFinish={authSubmit} preserve={false}>
      {register?<><Form.Item name="name" label="姓名" rules={[{required:true}]}><Input/></Form.Item>
        <Form.Item name="username" label={config.registration_email_required?'登录账号（选填，也可直接用邮箱登录）':'登录账号'} rules={[{required:!config.registration_email_required},{pattern:/^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$/,message:'使用 3–32 位字母、数字、点、横线或下划线'}]}><Input autoComplete="username"/></Form.Item>
        <Form.Item name="email" label={config.registration_email_required?'邮箱':'邮箱（选填）'} rules={[{required:config.registration_email_required,type:'email'}]}><Input autoComplete="email"/></Form.Item></>:
        <Form.Item name="account" label="账号或邮箱" rules={[{required:true}]}><Input autoComplete="username"/></Form.Item>}
      <Form.Item name="password" label="密码" rules={[{required:true,min:register?12:1}]}><Input.Password autoComplete={register?'new-password':'current-password'}/></Form.Item>
      {register&&<Form.Item name="confirm_password" label="确认密码" dependencies={['password']} rules={[{required:true},({getFieldValue})=>({validator(_,value){return !value||value===getFieldValue('password')?Promise.resolve():Promise.reject(new Error('两次密码不一致'));}})]}><Input.Password autoComplete="new-password"/></Form.Item>}
      {!register && <Form.Item name="code" label="认证器动态验证码（已绑定 MFA 时填写）" extra="来自 TOTP 认证器，不是邮箱或短信验证码。"><Input maxLength={6} inputMode="numeric"/></Form.Item>}
      {!register&&<Form.Item name="recovery_code" label="丢失认证器时填写恢复码（与动态码二选一）"><Input.Password autoComplete="off"/></Form.Item>}<Button type="primary" htmlType="submit" block>{register?'创建账号':'登录'}</Button>
    </Form><Space style={{marginTop:16}}>{config.registration_enabled && <Button type="link" onClick={()=>{setRegister(!register);form.resetFields();}}>{register?'已有账号？登录':'注册新账号'}</Button>}
      {config.verification_required&&<Button type="link" onClick={()=>{setResend(true);setForgot(true);}}>重新发送账号验证邮件</Button>}<Button type="link" disabled={!config.password_reset_available} onClick={()=>{setResend(false);setForgot(true);}}>邮箱找回密码</Button>
      {config.oidc_enabled && <a href="/api/v1/auth/oidc/login">企业身份登录</a>}</Space>
      {!config.registration_enabled&&<p className="muted">当前采用管理员邀请或创建账号，自行注册未开放。</p>}
      {!config.password_reset_available&&<p className="muted">{config.mail_delivery_mode==='spool'?'当前邮件仅写入测试文件，不发送真实邮件。':'邮件找回尚未开通。'}忘记密码请联系平台管理员重置；无邮箱的本地账号也使用此方式。</p>}
    </Modal>
    <Modal title={resend?'重新发送验证邮件':'邮箱重置密码'} open={forgot} onCancel={()=>setForgot(false)} footer={null} destroyOnClose><p>输入账号绑定的邮箱。重置链接有效期一小时，提交请求不代表邮件已送达。</p><Form layout="vertical" onFinish={async values=>{try{const result=await post(resend?'/auth/resend-verification':'/auth/forgot-password',values);message.info(result.message);setForgot(false);}catch(e:any){message.error(e.message);}}}><Form.Item name="email" label="绑定邮箱" rules={[{required:true,type:'email'}]}><Input autoComplete="email"/></Form.Item><Button htmlType="submit" type="primary">提交重置请求</Button></Form></Modal>
    <AccountLink/>
  </div></Session.Provider>;
}

function AccountLink(){
  const params=new URLSearchParams(window.location.search),verify=params.get('verify'),reset=params.get('reset'),bindEmail=params.get('bind_email'),probe=params.get('mail_probe');
  const [open,setOpen]=useState(Boolean(verify||reset||bindEmail||probe));const {message}=AntApp.useApp();
  const {refresh,login}=React.useContext(Session);
  return <Modal title={probe?'确认测试邮件已收到':bindEmail?'确认绑定新邮箱':verify?'验证账号':'重置密码'} open={open} footer={null} closable={false}>
    <Form layout="vertical" onFinish={async values=>{try{
      await post(probe?'/auth/mail/confirm-probe':bindEmail?'/auth/email/confirm':verify?'/auth/verify':'/auth/reset-password',{token:probe||bindEmail||verify||reset,...(reset?{password:values.password}:bindEmail?values:{})});
      message.success('操作成功，请重新登录');setOpen(false);window.history.replaceState({},'',window.location.pathname);
      await refresh();login();
    }catch(e:any){message.error(e.message);}}}>
      {probe&&<><p>请先登录发送本次探针的管理员账号，点击确认后才记为真实收件。</p><Button onClick={login}>登录发起账号</Button></>}
      {bindEmail&&<><p>请先登录申请变更的原账号，再输入密码与认证器验证码完成确认。</p><Button onClick={login}>登录原账号</Button><Form.Item name="current_password" label="当前密码" rules={[{required:true}]}><Input.Password autoComplete="current-password"/></Form.Item><Form.Item name="code" label="MFA 已启用时填写验证码"><Input maxLength={6}/></Form.Item></>}
      {reset&&<><Form.Item name="password" label="新密码" rules={[{required:true,min:12,max:128}]}><Input.Password autoComplete="new-password"/></Form.Item>
        <Form.Item name="confirm_password" label="确认新密码" dependencies={['password']} rules={[{required:true},({getFieldValue})=>({validator(_,value){return !value||value===getFieldValue('password')?Promise.resolve():Promise.reject(new Error('两次密码不一致'));}})]}><Input.Password autoComplete="new-password"/></Form.Item></>}
      <Button htmlType="submit" type="primary">{probe?'确认已收到测试邮件':bindEmail?'确认绑定邮箱':verify?'确认验证':'更新密码'}</Button>
    </Form>
  </Modal>;
}

// Data router 支持 useBlocker：在站内导航、前进/后退真正发生前保护活动上传。
const router=createBrowserRouter([{path:'*',element:<Shell/>}]);
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><ConfigProvider locale={zhCN} theme={{token:{colorPrimary:'#2e6250',borderRadius:8,fontFamily:'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif',colorBgLayout:'#f6f7f4'},components:{Button:{controlHeight:36},Table:{headerBg:'#f7f8f5'}}}}><AntApp><RouterProvider router={router}/></AntApp></ConfigProvider></React.StrictMode>);
