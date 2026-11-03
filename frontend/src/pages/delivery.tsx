/** Approved attributes → frozen selection → reproducible training delivery. */
import {useCallback,useEffect,useState} from 'react';
import {Link,useParams} from 'react-router-dom';
import {Alert,App,Button,Card,Checkbox,Form,Input,InputNumber,Modal,Select,Space,Table,Tag} from 'antd';
import {api,post,put,key,Row,labels} from '../api';

export const tierOptions=[{value:'raw',label:'原始 / 普通数据'},{value:'curated',label:'已治理交付'},{value:'certified',label:'高质量认证交付'}];
const values:Record<string,{value:string;label:string}[]>={
  hat:[{value:'hat',label:'戴帽子'},{value:'no_hat',label:'未戴帽子'},{value:'unclear',label:'不确定'}],
  sleeve:[{value:'short_sleeves',label:'短袖'},{value:'long_sleeves',label:'长袖'},{value:'sleeveless',label:'无袖'},{value:'unclear',label:'不确定'}],
  upper_color:['white','black','red','blue','green','yellow','other_or_unclear'].map((value,index)=>({value,label:['白','黑','红','蓝','绿','黄','其他 / 不确定'][index]}))
};

export function DeliveryPage(){
  const {versionId}=useParams(),{message}=App.useApp();
  const [version,setVersion]=useState<Row>(),[rows,setRows]=useState<Row[]>([]),[total,setTotal]=useState(0);
  const [delivery,setDelivery]=useState<Row>();
  const [selection,setSelection]=useState<Row>({}),[page,setPage]=useState(1),[error,setError]=useState('');
  const [busy,setBusy]=useState(false),[format,setFormat]=useState('coco'),[build,setBuild]=useState<Row>(),[tier,setTier]=useState('raw');
  const load=useCallback(async()=>{try{
    const params=new URLSearchParams({limit:'24',offset:String((page-1)*24)});
    Object.entries(selection).forEach(([name,value])=>{if(value!==undefined)params.set(name,String(value));});
    const [v,result,ready]=await Promise.all([api(`/versions/${versionId}`),api(`/deliveries/versions/${versionId}/attributes?${params}`),api(`/deliveries/versions/${versionId}`)]);
    setDelivery(ready);
    setVersion(v);setTier(v.quality_tier);setRows(result.items);setTotal(result.total);setError('');
  }catch(e:any){setError(e.message);}},[versionId,page,selection]);
  useEffect(()=>{load();},[load]);
  useEffect(()=>{if(build?.status==='completed')void load();},[build?.status,load]);
  useEffect(()=>{if(!build||!['queued','running'].includes(build.status))return;
    const timer=setInterval(async()=>{try{setBuild(await api(`/jobs/${build.id}`));}catch(e:any){setError(e.message);}},3000);
    return()=>clearInterval(timer);
  },[build]);
  const mutable=version?.can_manage&&['draft','rejected'].includes(version?.status);
  return <><Link to={version?`/datasets/${version.dataset_id}`:'/mine'}>← 数据集</Link><h1>属性筛选与训练交付</h1>
    <Alert type="info" showIcon message="仅使用已独立审核的图片。组合属性须由同一目标满足；导出整图及其中全部已审核目标，避免误当负样本。自定义模板当前支持整版与有/无目标导出，不提供服饰属性筛选或认证评测。"/>
    {error&&<Alert type="error" message={error} style={{marginTop:16}}/>}
    {version&&<Card title={`v${version.number} · ${tierOptions.find(t=>t.value===version.quality_tier)?.label}`} style={{marginTop:20}}>
      <Space wrap><Link to={`/data-governance/${versionId}`}>去重与划分</Link><Link to={`/evaluations/${versionId}`}>独立测评</Link>
        {mutable&&<><Select style={{width:200}} value={tier} onChange={setTier} options={tierOptions.map((option,index)=>({...option,disabled:index<tierOptions.findIndex(t=>t.value===version.quality_tier)||!!version.review_template&&option.value==='certified'}))}/>
          <Button onClick={async()=>{try{await put(`/versions/${versionId}`,{license:version.license,source:version.source,changelog:version.changelog,
            quality_tier:tier,card:Object.fromEntries(Object.entries(version.card).filter(([name])=>!name.startsWith('_')))});await load();message.success('等级已更新；认证规则由服务端强制执行');}catch(e:any){message.error(e.message);}}}>保存交付等级</Button></>}
        {version.can_download&&delivery?.available?<a href={`/api/v1/deliveries/versions/${versionId}/download`}><Button>下载整版正式交付</Button></a>:<span>尚无有效整版构建：{delivery?.reason||'请完成审核并构建整版交付包'}</span>}
      </Space><p>原始数据发布不代表语义质量通过。已治理等级要求去重、完整复核和正式构建；认证等级另要求来源许可审核、分组划分和达标的整版快照评测。</p>
    </Card>}
    <Card title="筛选已审核图片" style={{marginTop:20}}><Space wrap>
      {!version?.review_template&&Object.entries(values).map(([name,options])=><Select key={name} style={{width:160}} allowClear value={selection[name]}
        placeholder={{hat:'所有帽子属性',sleeve:'所有袖长',upper_color:'所有上衣颜色'}[name]} options={options}
        onChange={value=>{setPage(1);setSelection(old=>({...old,[name]:value,has_person:undefined}));}}/>)}
      <Select style={{width:160}} allowClear value={selection.has_person} placeholder="全部样本" options={[{value:true,label:version?.review_template?'有目标样本':'有人样本'},{value:false,label:version?.review_template?'已确认无目标':'已确认无人'}]}
        onChange={value=>{setPage(1);setSelection(value===false?{has_person:false}:{...selection,has_person:value});}}/>
      <Button onClick={()=>{setSelection({});setPage(1);}}>清空条件</Button><span>命中 {total} 张已审核图片</span>
    </Space><Table rowKey="id" dataSource={rows} style={{marginTop:18}} pagination={{current:page,pageSize:24,total,showSizeChanger:false,onChange:setPage}}
      columns={[{title:'文件',dataIndex:'filename'},{title:'尺寸',render:(_:unknown,r:Row)=>`${r.width} × ${r.height}`},
      {title:'样本',render:(_:unknown,r:Row)=><Tag>{r.no_person?'已审核负样本':'已审核人物'}</Tag>},
      {title:'图片',render:(_:unknown,r:Row)=><Link to={`/datasets/${version?.dataset_id}?version=${versionId}`}>在受控版本查看</Link>}]}/></Card>
    {mutable&&<Card title="冻结条件并生成训练包" style={{marginTop:20}}><p>清空筛选条件生成整版正式交付；设置条件生成可追溯子集。构建期间标注变化会使任务失败，完成后变化会使下载失效，需重新构建。</p>
      <Space><Select value={format} onChange={setFormat} style={{width:260}} options={[{value:'coco',label:'COCO 目标检测 + 审核属性'},{value:'reviewed-json',label:'平台审核 JSON + 标准化图片'},{value:'yolo',label:'YOLO 目标检测（需训练/验证划分）'},{value:'vlm-jsonl',label:'VLM 结构化指令 JSONL（人工标签）'}]}/>
        <Button type="primary" disabled={!total} loading={busy} onClick={async()=>{setBusy(true);try{const clean=Object.fromEntries(Object.entries(selection).filter(([,v])=>v!==undefined));
          setBuild(await post('/jobs',{version_id:versionId,kind:'release',delivery_format:format,selection:clean,idempotency_key:key()}));message.success('构建已登记，可到任务中心持续查看');
        }catch(e:any){message.error(e.message);}finally{setBusy(false);}}}>构建当前条件训练包</Button></Space>
      {build&&<p>构建 {build.id}：{build.status} {build.error||''} {build.status==='completed'&&<a href={`/api/v1/jobs/${build.id}/download`}>下载此构建</a>} · <Link to="/jobs">任务中心</Link></p>}
    </Card>}
  </>;
}

export function SpaceQualityPolicy({space,refresh}:{space:Row;refresh:()=>Promise<void>}){
  const {message}=App.useApp();
  const policy=space.quality_policy||{};
  return <Card title="组织发布质量策略" style={{marginBottom:20}}><Form key={`${space.id}-${policy.revision||0}`} layout="inline"
    initialValues={{minimum_tier:'raw',min_truth_images:100,min_positives:20,min_negatives:20,min_precision:.9,min_recall:.8,min_attribute_coverage:.95,...policy}}
    onFinish={async values=>{try{await put(`/spaces/${space.id}/quality-policy`,{...values,revision:policy.revision||0});await refresh();message.success('策略已保存；旧评测不能用于新策略');}catch(e:any){message.error(e.message);}}}>
    <Form.Item name="minimum_tier" label="最低发布等级"><Select options={tierOptions} style={{width:200}}/></Form.Item>
    <Form.Item name="require_blind_evaluation" valuePropName="checked"><Checkbox>质量验收必须使用独立盲测</Checkbox></Form.Item>
    <Form.Item name="min_truth_images" label="最少真值图"><InputNumber min={1} max={10000}/></Form.Item>
    <Form.Item name="min_positives" label="最少正样本"><InputNumber min={1} max={10000}/></Form.Item>
    <Form.Item name="min_negatives" label="最少负样本"><InputNumber min={0} max={10000}/></Form.Item>
    {['min_precision','min_recall','min_attribute_coverage'].map(name=><Form.Item key={name} name={name} label={{min_precision:'精确率',min_recall:'召回率',min_attribute_coverage:'覆盖率'}[name]}><InputNumber min={0} max={1} step={.01}/></Form.Item>)}
    <Button htmlType="submit">保存组织策略</Button></Form><p>服务端取组织阈值与平台最低阈值的较严格值。此策略影响后续验收和待发布版本，不自动重认证历史版本。</p></Card>;
}


export function SpaceResponsibilities({space}:{space:Row}){
  const {message}=App.useApp();const [rows,setRows]=useState<Row[]>([]),[members,setMembers]=useState<Row[]>([]),[target,setTarget]=useState<Row>();
  const [more,setMore]=useState(false);
  const load=useCallback(async()=>{try{const [result,people]=await Promise.all([api(`/spaces/${space.id}/responsibilities`),api(`/spaces/${space.id}/members`)]);
    setRows(result.items);setMembers(people);setMore(result.has_more);
  }catch(e:any){message.error(e.message);}},[space.id,message]);
  useEffect(()=>{load();},[load]);
  return <Card title="数据负责人交接" style={{marginTop:20}}><p>负责人正常转交；离职接管需要另一位同时具备平台与组织管理员身份的 MFA 用户。组织成员管理权限本身不提供文件访问权。</p>
    <Table rowKey="id" dataSource={rows} columns={[{title:'数据集',dataIndex:'name'},{title:'负责人',dataIndex:'owner_name'},
      {title:'状态',render:(_:unknown,r:Row)=><Tag color={r.owner_available?'green':'orange'}>{r.owner_available?'有效':'需要交接'}</Tag>},
      {title:'操作',render:(_:unknown,r:Row)=>r.can_transfer||r.can_recover?<Button onClick={()=>setTarget(r)}>转交负责人</Button>:'由当前负责人或独立平台管理员处理'}]}/>
    {more&&<Button onClick={async()=>{try{const result=await api(`/spaces/${space.id}/responsibilities?offset=${rows.length}`);setRows(old=>[...old,...result.items]);setMore(result.has_more);}catch(e:any){message.error(e.message);}}}>加载更多负责数据集</Button>}
    <Modal title="转交数据负责人" open={!!target} onCancel={()=>setTarget(undefined)} footer={null} destroyOnClose><Form layout="vertical" onFinish={async values=>{try{
      await post(`/datasets/${target?.id}/transfer-owner`,values);setTarget(undefined);await load();message.success('负责人已转交，原负责人不再拥有管理权限');
    }catch(e:any){message.error(e.message);}}}>
      <Form.Item name="target_user_id" label="新负责人" rules={[{required:true}]}><Select options={members.filter(m=>m.active&&['admin','contributor'].includes(m.role)&&m.user_id!==target?.owner_id).map(m=>({value:m.user_id,label:m.name}))}/></Form.Item>
      <Form.Item name="reason" label="交接说明" rules={[{required:true,min:5}]}><Input.TextArea/></Form.Item>
      <Form.Item name="current_password" label="输入你的当前密码确认" rules={[{required:true}]}><Input.Password autoComplete="current-password"/></Form.Item>
      <Button htmlType="submit" type="primary">确认转交</Button>
    </Form></Modal></Card>;
}


export function DatasetDescriptionEditor({dataset,refresh}:{dataset:Row;refresh:()=>Promise<void>}){
  const [open,setOpen]=useState(false),{message}=App.useApp();
  const [typeOpen,setTypeOpen]=useState(false),[changing,setChanging]=useState(false);
  return <><Button onClick={()=>setOpen(true)}>修改目录说明</Button><Button disabled={dataset.versions?.some((v:Row)=>['published','pending_review'].includes(v.status))} onClick={()=>setTypeOpen(true)}>修改数据类型</Button>
    <Modal title="修改数据类型" open={typeOpen} onCancel={()=>!changing&&setTypeOpen(false)} footer={null} destroyOnClose>
      <p>系统会校验已有文件和进行中的上传。类型变更不会转换、删除或重新识别文件；已发布或待发布审核的数据集不能直接修改。</p>
      <Form layout="vertical" initialValues={{modality:dataset.modality}} onFinish={async values=>{setChanging(true);try{await put(`/datasets/${dataset.id}/modality`,values);await refresh();setTypeOpen(false);message.success('数据类型已更新');}catch(e:any){message.error(e.message);}finally{setChanging(false);}}}>
        <Form.Item name="modality" label="数据类型" rules={[{required:true}]}><Select options={['text','image','audio','video','tabular','multimodal'].map(value=>({value,label:labels[value]}))}/></Form.Item>
        <Button type="primary" htmlType="submit" loading={changing}>校验并保存类型</Button>
      </Form>
    </Modal><Modal title="修改目录展示说明" open={open} onCancel={()=>setOpen(false)} footer={null} destroyOnClose>
    <p>仅修改目录介绍，不修改已发布版本的来源、许可、文件或授权范围。</p><Form layout="vertical" initialValues={{name:dataset.name,description:dataset.description,category:dataset.category,tags:dataset.tags}} onFinish={async values=>{try{
      await put(`/datasets/${dataset.id}/description`,values);await refresh();setOpen(false);message.success('说明已更新并记录审计');
    }catch(e:any){message.error(e.message);}}}>
      <Form.Item name="name" label="名称" rules={[{required:true}]}><Input maxLength={200}/></Form.Item>
      <Form.Item name="description" label="介绍"><Input.TextArea rows={4}/></Form.Item>
      <Form.Item name="category" label="领域"><Input maxLength={80}/></Form.Item>
      <Form.Item name="tags" label="标签"><Select mode="tags"/></Form.Item>
      <Button htmlType="submit">保存说明</Button>
    </Form></Modal></>;
}
