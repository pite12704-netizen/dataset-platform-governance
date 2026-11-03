import {useContext,useState} from 'react';
import {Alert,App,Button,Card,Form,Input,Modal,Typography} from 'antd';
import {post,Row} from '../api';
import {Session} from '../main';

export function RecoveryCodes({codes,onClose}:{codes:string[];onClose:()=>void}){
  return <Modal title="一次性恢复码" open={codes.length>0} onCancel={onClose} onOk={onClose} destroyOnClose>
    <Alert type="warning" message="只展示这一次。离线保管，不要放入聊天、代码或截图；每个码只能使用一次。重新生成会撤销旧码。"/>
    <Typography.Paragraph code copyable style={{whiteSpace:'pre',marginTop:16}}>{codes.join('\n')}</Typography.Paragraph>
  </Modal>;
}

export function AccountRecovery(){
  const {user,refresh,login}=useContext(Session),{message}=App.useApp();const [codes,setCodes]=useState<string[]>([]);
  if(user?.recovery_only)return <Card title="恢复丢失的认证器"><Alert type="warning" message="此会话仅用于 MFA 恢复，无法操作业务。重置后旧认证器、会话及服务凭据均失效。"/>
    <Form layout="vertical" onFinish={async values=>{try{const result=await post('/auth/mfa/recover',values);message.success(result.message);await refresh();login();}catch(e:any){message.error(e.message);}}}>
      <Form.Item name="current_password" label="再次确认当前密码" rules={[{required:true}]}><Input.Password autoComplete="current-password"/></Form.Item><Button htmlType="submit" type="primary">重置丢失的 MFA 并退出</Button>
    </Form></Card>;
  return <><Card title="绑定或变更邮箱" style={{maxWidth:700,marginBottom:20}}><p>新邮箱必须验证；启用 MFA 的账号还需填写认证器验证码。确认成功后重新登录。</p>
    <Form layout="vertical" onFinish={async values=>{try{const result=await post('/auth/email/change',values);message.info(result.message);}catch(e:any){message.error(e.message);}}}>
      <Form.Item name="email" label="新邮箱" rules={[{required:true,type:'email'}]}><Input autoComplete="email"/></Form.Item>
      <Form.Item name="current_password" label="当前密码" rules={[{required:true}]}><Input.Password autoComplete="current-password"/></Form.Item>
      {user?.mfa_enabled&&<Form.Item name="code" label="认证器验证码" rules={[{required:true,len:6}]}><Input maxLength={6}/></Form.Item>}
      <Button htmlType="submit">发送新邮箱验证请求</Button>
    </Form></Card>{user?.mfa_enabled&&<Card title="重新生成 MFA 恢复码" style={{maxWidth:700,marginBottom:20}}>
      <Form layout="vertical" onFinish={async values=>{try{const result=await post('/auth/mfa/recovery-codes',values);setCodes(result.recovery_codes);}catch(e:any){message.error(e.message);}}}>
        <Form.Item name="current_password" label="当前密码" rules={[{required:true}]}><Input.Password autoComplete="current-password"/></Form.Item>
        <Form.Item name="code" label="新的认证器验证码" rules={[{required:true,len:6}]}><Input maxLength={6}/></Form.Item><Button htmlType="submit">撤销旧恢复码并生成新码</Button>
      </Form></Card>}<RecoveryCodes codes={codes} onClose={()=>setCodes([])}/></>;
}
