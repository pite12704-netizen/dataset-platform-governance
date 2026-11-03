import {Button,Form,Input,InputNumber,Modal,Select,Space,Alert,App,Card} from 'antd';
import {useState} from 'react';
import {api,put,Row} from './api';

export const unknownAnnotation=(t:Row)=>({category:t.classes[0].key,labels:Object.fromEntries(t.fields.map((f:Row)=>[f.key,null]))});
export function AttributeControl({field,value,onChange,disabled}:{field:Row;value?:any;onChange?:(value:any)=>void;disabled?:boolean}){
  if(field.type==='enum')return <Select allowClear placeholder="不确定 / 看不清" disabled={disabled} value={value??undefined} options={field.options.map((x:string)=>({value:x,label:x}))} onChange={v=>onChange?.(v??null)} style={{minWidth:140}}/>;
  if(field.type==='boolean')return <Select allowClear placeholder="不确定" disabled={disabled} value={value===true?'yes':value===false?'no':undefined} options={[{value:'yes',label:'是'},{value:'no',label:'否'}]} onChange={v=>onChange?.(v==null?null:v==='yes')} style={{minWidth:100}}/>;
  if(field.type==='number')return <InputNumber disabled={disabled} value={value} placeholder="未知可留空" onChange={v=>onChange?.(v??null)}/>;
  return <Input disabled={disabled} value={value??''} maxLength={500} placeholder="未知可留空" onChange={e=>onChange?.(e.target.value||null)}/>;
}
export function TemplateFormFields({template}:{template:Row}){
  return <><Form.Item name="category" label="目标类别" rules={[{required:true}]}><Select options={template.classes.map((c:Row)=>({value:c.key,label:c.label}))}/></Form.Item>
    {template.fields.map((f:Row)=><Form.Item key={f.key} name={['labels',f.key]} label={f.label} help={f.description}><AttributeControl field={f}/></Form.Item>)}</>;
}
export function TemplateControls({template,annotation,onChange,disabled}:{template:Row;annotation:Row;onChange:(v:Row)=>void;disabled:boolean}){
  return <><Select aria-label="目标类别" disabled={disabled} value={annotation.category} options={template.classes.map((c:Row)=>({value:c.key,label:c.label}))} onChange={category=>onChange({category})}/>
    {template.fields.map((f:Row)=><Space key={f.key}><span>{f.label}</span><AttributeControl disabled={disabled} field={f} value={annotation.labels[f.key]} onChange={value=>onChange({labels:{...annotation.labels,[f.key]:value}})}/></Space>)}</>;
}
const examples:Record<string,Row>={
  '通用目标':{name:'通用目标检测',classes:[{key:'object',label:'目标'}],fields:[]},
  '车辆':{name:'车辆审核',classes:[{key:'car',label:'轿车'},{key:'truck',label:'货车'},{key:'bicycle',label:'自行车'}],fields:[{key:'color',label:'颜色',type:'enum',options:['红','蓝','白','黑','其他']},{key:'damaged',label:'是否可见破损',type:'boolean',options:[]}]},
  '动物':{name:'动物审核',classes:[{key:'cat',label:'猫'},{key:'dog',label:'狗'},{key:'bird',label:'鸟'}],fields:[{key:'occluded',label:'是否遮挡',type:'boolean',options:[]}]},
  '商品':{name:'商品审核',classes:[{key:'product',label:'商品'}],fields:[{key:'packaging',label:'包装状态',type:'enum',options:['完好','破损','无包装']}]},
};
export function ReviewTemplateEditor({version,onSaved}:{version:Row;onSaved:()=>Promise<void>}){
  const {message}=App.useApp(),[open,setOpen]=useState(false),[busy,setBusy]=useState(false),[digest,setDigest]=useState('');const [form]=Form.useForm();
  const fill=(t:Row)=>{form.resetFields();form.setFieldsValue({...t,fields:t.fields.map((f:Row)=>({...f,optionsText:f.options?.join('\n')||''}))});};
  const definition=()=>{const v=form.getFieldsValue();return {schema_version:1,name:v.name,classes:(v.classes||[]).map((c:Row)=>({key:c.key,label:c.label,description:c.description||''})),fields:(v.fields||[]).map((f:Row)=>({key:f.key,label:f.label,type:f.type,description:f.description||'',options:f.type==='enum'?(f.optionsText||'').split(/[,，\n]/).map((x:string)=>x.trim()).filter(Boolean):[]}))};};
  const show=async()=>{try{const result=await api(`/review-templates/versions/${version.id}`);fill(result.template||examples['通用目标']);setDigest(result.digest||'');setOpen(true);}catch(e:any){message.error(e.message);}};
  return <><Button onClick={show}>目标类别与审核模板</Button><Modal title="配置自定义目标审核模板" width={950} open={open} onCancel={()=>!busy&&setOpen(false)} footer={null} destroyOnClose>
    <Alert showIcon type="info" message="模板按数据版本绑定。开始识别或产生审核记录后不能修改，变更须创建新版本；未绑定的历史版本继续使用人物服饰模板。" description="支持目标框、类别和可见属性；未知值可留空。不支持用此模板替代 OCR、分割或独立质量认证。"/>
    {digest&&<p>已绑定摘要：{digest.slice(0,16)}…</p>}
    <Space wrap style={{margin:'12px 0'}}>{Object.entries(examples).map(([name,t])=><Button key={name} onClick={()=>fill(t)}>使用{name}示例</Button>)}
      <label><input aria-label="导入审核模板JSON" type="file" accept="application/json,.json" onChange={async e=>{try{const f=e.target.files?.[0];if(!f)return;if(f.size>32768)throw Error('模板文件最多32KiB');fill(JSON.parse(await f.text()));}catch(err:any){message.error(err.message);}}}/></label></Space>
    <Form form={form} layout="vertical" onFinish={async()=>{setBusy(true);try{await put(`/review-templates/versions/${version.id}`,definition());await onSaved();message.success('模板已绑定此版本');setOpen(false);}catch(e:any){message.error(e.message);}finally{setBusy(false);}}}>
      <Form.Item name="name" label="模板名称" rules={[{required:true,max:80}]}><Input/></Form.Item>
      <Card size="small" title="目标类别"><Form.List name="classes">{(rows,{add,remove})=><>{rows.map(r=><Space key={r.key} align="start"><Form.Item name={[r.name,'key']} label="稳定标识（英文）" rules={[{required:true,pattern:/^[a-z][a-z0-9_]{0,39}$/}]}><Input placeholder="car"/></Form.Item><Form.Item name={[r.name,'label']} label="显示名称" rules={[{required:true,max:60}]}><Input placeholder="轿车"/></Form.Item><Form.Item name={[r.name,'description']} label="辨认说明"><Input maxLength={300}/></Form.Item><Button danger onClick={()=>remove(r.name)}>删除</Button></Space>)}<Button disabled={rows.length>=30} onClick={()=>add({key:'',label:''})}>添加类别（最多30个）</Button></>}</Form.List></Card>
      <Card size="small" title="自定义属性" style={{marginTop:12}}><Form.List name="fields">{(rows,{add,remove})=><>{rows.map(r=><Card size="small" key={r.key} style={{marginBottom:12}} extra={<Button danger onClick={()=>remove(r.name)}>删除属性</Button>}><Space align="start"><Form.Item name={[r.name,'key']} label="字段标识" rules={[{required:true,pattern:/^[a-z][a-z0-9_]{0,39}$/}]}><Input placeholder="color"/></Form.Item><Form.Item name={[r.name,'label']} label="显示名称" rules={[{required:true,max:60}]}><Input placeholder="颜色"/></Form.Item><Form.Item name={[r.name,'type']} label="类型" rules={[{required:true}]}><Select style={{width:130}} options={[{value:'enum',label:'单选'},{value:'boolean',label:'是/否'},{value:'number',label:'数值'},{value:'text',label:'短文本'}]}/></Form.Item></Space><Form.Item name={[r.name,'optionsText']} label="单选选项（每行一个；其他类型忽略）"><Input.TextArea rows={2}/></Form.Item><Form.Item name={[r.name,'description']} label="审核说明"><Input maxLength={300}/></Form.Item></Card>)}<Button disabled={rows.length>=20} onClick={()=>add({key:'',label:'',type:'enum',optionsText:''})}>添加属性（最多20项）</Button></>}</Form.List></Card>
      <Space style={{marginTop:16}}><Button type="primary" htmlType="submit" loading={busy} disabled={!version.can_manage||!['draft','rejected'].includes(version.status)}>保存并绑定此版本</Button><Button onClick={()=>{const blob=new Blob([JSON.stringify(definition(),null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='review-template.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}}>导出模板 JSON</Button></Space>
    </Form>
  </Modal></>;
}
