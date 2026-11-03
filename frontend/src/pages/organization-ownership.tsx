import {useCallback,useContext,useEffect,useState} from 'react';
import {Alert,App,Button,Card,Form,Input,Modal,Select,Space,Table,Tag} from 'antd';
import {api,date,post,Row} from '../api';
import {Session} from '../main';

export function OrganizationOwnership({space,members}:{space:Row;members:Row[]}){
  const {user,refresh}=useContext(Session),{message}=App.useApp();
  const [rows,setRows]=useState<Row[]>([]),[selected,setSelected]=useState<Row>(),[open,setOpen]=useState(false),[busy,setBusy]=useState(false);
  const root=`/spaces/${space.id}/ownership-transfers`;
  const load=useCallback(async()=>{try{setRows(await api(root));}catch(e:any){message.error(e.message);}},[root,message]);
  useEffect(()=>{void load();},[load]);
  return <Card style={{marginTop:24}} title="组织所有权交接" extra={space.owner_id===user?.id&&
    <Button onClick={()=>{setSelected(undefined);setOpen(true);}}>交接组织所有权</Button>}>
    <Alert type="info" showIcon message="由现任所有者发起，继任管理员在24小时内独立确认。数据集负责人和下载授权仍分别管理。"/>
    <Table rowKey="id" dataSource={rows} pagination={{pageSize:5}} columns={[
      {title:'继任管理员',dataIndex:'target_user_id',render:id=>members.find(m=>m.user_id===id)?.name||id},
      {title:'原因',dataIndex:'reason'},{title:'状态',dataIndex:'status',render:value=><Tag>{({pending:'待接收',accepted:'已交接',cancelled:'已取消'} as Record<string,string>)[value]||value}</Tag>},
      {title:'截止时间',dataIndex:'expires_at',render:date},{title:'操作',render:(_:any,row:Row)=>row.status==='pending'&&<Space>
        {row.target_user_id===user?.id&&<Button type="link" onClick={()=>{setSelected(row);setOpen(true);}}>确认接收</Button>}
        {[space.owner_id,row.target_user_id].includes(user?.id)&&<Button type="link" onClick={async()=>{try{await post(`${root}/${row.id}/cancel`,{});await load();}catch(e:any){message.error(e.message);}}}>取消</Button>}
      </Space>}]}/>
    <Modal title={selected?'确认接收组织所有权':'发起组织所有权交接'} open={open} onCancel={()=>setOpen(false)} footer={null} destroyOnClose>
      <Form layout="vertical" onFinish={async(values)=>{setBusy(true);try{
        await post(selected?`${root}/${selected.id}/accept`:root,values);setOpen(false);await refresh();await load();message.success(selected?'组织所有权交接完成':'已通知继任管理员确认');
      }catch(e:any){message.error(e.message);}finally{setBusy(false);}}}>
        {!selected&&<><Form.Item name="target_user_id" label="继任管理员" rules={[{required:true}]}><Select options={members.filter(m=>m.active&&m.role==='admin'&&m.user_id!==user?.id).map(m=>({value:m.user_id,label:m.name}))}/></Form.Item>
          <Form.Item name="reason" label="交接原因" rules={[{required:true,min:5,max:2000}]}><Input.TextArea rows={3}/></Form.Item></>}
        <Form.Item name="current_password" label="你的当前密码" rules={[{required:true}]}><Input.Password autoComplete="current-password"/></Form.Item>
        {user?.mfa_enabled&&<Form.Item name="code" label="新的动态验证码" rules={[{required:true,pattern:/^\d{6}$/}]}><Input inputMode="numeric" autoComplete="one-time-code"/></Form.Item>}
        <Button type="primary" htmlType="submit" loading={busy}>{selected?'确认接收':'提交交接请求'}</Button>
      </Form>
    </Modal>
  </Card>;
}
