/** 本地目录 / MySQL + MinIO 接入；页面管理批次，原图由客户工具分片上传。 */
import {useCallback,useEffect,useRef,useState} from 'react';
import {Link,useParams,useSearchParams,useNavigate} from 'react-router-dom';
import {Alert,App,Button,Card,Descriptions,Form,Input,InputNumber,Modal,Pagination,Popconfirm,Select,Space,Table,Tag,Typography} from 'antd';
import {api,post,key,Row,date,bytes,ApiError} from '../api';

const words:Record<string,string>={uploading:'接收中',checking:'检查中',completed:'完整导入',partial_failed:'存在失败',cancelled:'已取消',
  pending:'待上传',quarantine:'检查中',clean:'检查通过',rejected:'文件不合格',failed:'检查任务失败',aborted:'上传已终止'};

export function ImportsPage(){
  const {batchId}=useParams(),[params]=useSearchParams(),navigate=useNavigate(),{message}=App.useApp();
  const [rows,setRows]=useState<Row[]>([]),[batch,setBatch]=useState<Row>(),[items,setItems]=useState<Row[]>([]);
  const [hasMore,setHasMore]=useState(false),[page,setPage]=useState(1),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  const [modal,setModal]=useState(false),[token,setToken]=useState(''),[tokenBatch,setTokenBatch]=useState('');
  const [createKey,setCreateKey]=useState(()=>key());
  const [form]=Form.useForm();const [partialReason,setPartialReason]=useState('');
  const pollingDelay=useRef(5000),pollAfter=useRef(0),loading=useRef(false);
  const load=useCallback(async()=>{if(loading.current||Date.now()<pollAfter.current)return;loading.current=true;try{
    if(batchId){
      const [b,p]=await Promise.all([api(`/imports/${batchId}`),api(`/imports/${batchId}/items?limit=50&offset=${(page-1)*50}`)]);
      setBatch(b);setItems(p.items);
    }else{
      const result=await api('/imports?limit=50');setRows(result.items);setHasMore(result.has_more);
    }
    pollingDelay.current=5000;pollAfter.current=0;setError('');
  }catch(e:any){pollingDelay.current=e instanceof ApiError&&e.status===429?Math.max(5000,e.retryAfter*1000):15000;
    pollAfter.current=Date.now()+pollingDelay.current;
    setError(e instanceof ApiError&&e.status===429?`查询受到限流，${Math.ceil(pollingDelay.current/1000)} 秒后自动刷新；已上传文件保留。`:e.message);
  }finally{loading.current=false;}},[batchId,page]);
  useEffect(()=>{setBatch(undefined);setPage(1);},[batchId]);
  useEffect(()=>{load();},[load]);
  useEffect(()=>{if(!batchId||['completed','cancelled'].includes(batch?.status))return;
    let cancelled=false,timer:ReturnType<typeof setTimeout>;
    const tick=async()=>{await load();if(!cancelled)timer=setTimeout(tick,pollingDelay.current);};
    timer=setTimeout(tick,pollingDelay.current);return()=>{cancelled=true;clearTimeout(timer);};
  },[batchId,batch?.status,load]);
  const act=async(path:string,data:Row={})=>{setBusy(true);try{const result=await post(path,data);
    if(result.token){setToken(result.token);setTokenBatch(batchId||result.id);}
    await load();message.success('操作已保存');
  }catch(e:any){message.error(e.message);}finally{setBusy(false);}};
  const report=()=>{const failures=items.filter(i=>['failed','rejected'].includes(i.status));
    const url=URL.createObjectURL(new Blob([JSON.stringify(failures,null,2)],{type:'application/json'}));
    const link=document.createElement('a');link.href=url;link.download=`import-${batchId}-page-${page}-failures.json`;link.click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  };
  return <><div className="page-title"><div><div className="eyebrow">CONTROLLED INGESTION</div><h1>批量导入</h1>
    <p>本地图片直接在网页选择文件夹；客户 MySQL + MinIO 来源使用独立接入工具。</p></div><Space wrap>
      <Link to={`/imports/local${batchId&&batch?`?version=${batch.version_id}&batch=${batchId}`:params.get('version')?`?version=${params.get('version')}`:''}`}><Button type="primary">网页选择文件夹 / 续传</Button></Link>
      <a href="/api/v1/imports/client-kit"><Button>下载技术接入工具</Button></a><Button onClick={load}>刷新</Button>
      {!batchId&&<Button onClick={()=>{setCreateKey(key());form.setFieldsValue({version_id:params.get('version')||''});setModal(true);}}>从工具清单创建批次</Button>}</Space></div>
    {error&&<Alert type="error" message={error} style={{marginBottom:18}}/>}
    {batchId&&<Alert type="info" showIcon style={{marginBottom:16}} message="此页仅查询服务器接收进度，不负责传输本地图片"
      description="上传在原来的文件夹上传页执行，请保留原页。若数量不再增长，请回原页查看提示并继续原批次，不要取消或重建批次。"/>}
    <Alert showIcon type="info" message="本地图片点击“网页选择文件夹 / 续传”，不需要源码、Python 或手工生成清单。上传期间保持页面打开；暂停或刷新后重新选择原文件夹继续。"/>
    {!batchId?<Card title="我的导入批次" style={{marginTop:20}}><Table rowKey="id" dataSource={rows} pagination={{pageSize:10}} columns={[
      {title:'来源',dataIndex:'source_id'},{title:'状态',dataIndex:'status',render:(v:string)=><Tag>{words[v]||v}</Tag>},
      {title:'检查通过 / 预期',render:(_:unknown,r:Row)=>`${r.counts.clean} / ${r.expected_files}`},
      {title:'失败',render:(_:unknown,r:Row)=>r.counts.failed},{title:'创建时间',dataIndex:'created_at',render:date},
      {title:'操作',render:(_:unknown,r:Row)=><Link to={`/imports/${r.id}`}>进度与续传</Link>}
    ]}/>{hasMore&&<Button onClick={async()=>{try{const result=await api(`/imports?limit=50&offset=${rows.length}`);setRows(p=>[...p,...result.items]);setHasMore(result.has_more);}catch(e:any){message.error(e.message);}}}>加载更多</Button>}</Card>:batch&&<>
      <p><Link to="/imports">← 导入列表</Link> · <Link to={`/data-governance/${batch.version_id}`}>进入此版本治理</Link> · <Link to={`/image-reviews?version=${batch.version_id}`}>图像识别与复核</Link></p>
      <Card title={<Space>来源 {batch.source_id}<Tag>{words[batch.status]||batch.status}</Tag></Space>} style={{marginTop:20}}>
        <Descriptions column={2} items={[
          {key:'id',label:'批次 ID',children:<Typography.Text copyable>{batch.id}</Typography.Text>},
          {key:'version',label:'目标版本',children:<Typography.Text copyable>{batch.version_id}</Typography.Text>},
          {key:'files',label:'已登记 / 预期文件',children:`${batch.counts.registered} / ${batch.expected_files}`},
          {key:'size',label:'声明数据大小',children:bytes(batch.expected_bytes)},
          {key:'upload',label:'已完整传输',children:batch.counts.uploaded},
          {key:'clean',label:'检查通过 / 失败 / 检查中',children:`${batch.counts.clean} / ${batch.counts.failed} / ${batch.counts.checking}`},
          {key:'credential',label:'凭据',children:batch.revoked?'已撤销':`到期 ${date(batch.expires_at)}`},
          {key:'sealed',label:'最终清单',children:batch.sealed?'已封存':'待完成'}]}/>
        {!['completed','cancelled'].includes(batch.status)&&<Space wrap style={{marginTop:18}}>
          <Button type="primary" loading={busy} onClick={()=>act(`/imports/${batch.id}/finalize`)}>核对并完成导入</Button>
          <Button loading={busy} onClick={()=>act(`/imports/${batch.id}/credential`)}>生成新凭据 / 使旧凭据失效</Button>
          <Popconfirm title="撤销此批次的上传凭据？" onConfirm={()=>act(`/imports/${batch.id}/credential/revoke`)}><Button disabled={busy}>撤销凭据</Button></Popconfirm>
          <Popconfirm title="停止接收此批次剩余文件？" description="已接收资产保留；未完成分片由后台回收。取消会记录排除数量，不代表整批成功。" onConfirm={()=>act(`/imports/${batch.id}/cancel`)}><Button danger disabled={busy}>取消导入</Button></Popconfirm>
        </Space>}
        {batch.status==='cancelled'&&batch.counts.uploaded>0&&<Alert style={{marginTop:18}} type={batch.resolution?.kind==='accepted_partial'?'success':'warning'}
          message={batch.resolution?.kind==='accepted_partial'?`已明确接收 ${batch.resolution.accepted_files} 张，排除 ${batch.resolution.omitted_files} 张`:'取消不等于完整接收：请核对保留文件后明确接受此子集'}
          description={batch.resolution?.kind!=='accepted_partial'&&<Space wrap><Select style={{width:300}} value={partialReason||undefined} placeholder="选择接收此子集的原因"
            options={[{value:'仅接收当前可用来源子集，其余后续另批提供',label:'其余文件后续另批提供'},{value:'按业务需求仅保留当前已接收数据子集',label:'业务仅需要当前子集'}]} onChange={setPartialReason}/>
            <Popconfirm title={`明确接受 ${batch.counts.clean} / ${batch.expected_files} 个文件？`} onConfirm={()=>act(`/imports/${batch.id}/accept-partial`,{confirmed:true,reason:partialReason,expected_clean_files:batch.counts.clean})}>
              <Button disabled={!partialReason||batch.counts.checking>0||batch.counts.clean!==batch.counts.uploaded}>确认接受当前子集</Button></Popconfirm></Space>}/>}
        <p className="muted">同一批次重新运行客户端会核对服务器进度并续传。凭据到期后在此生成新凭据。文件检查不合格需修正来源并使用新清单；检查任务的临时故障可重试。</p>
      </Card>
      <Card title="逐文件进度" extra={<Button onClick={report}>导出本页失败清单</Button>} style={{marginTop:20}}><Table rowKey="id" pagination={false} dataSource={items} columns={[
        {title:'来源记录',dataIndex:'external_id'},{title:'文件',dataIndex:'filename',ellipsis:true},
        {title:'来源组',dataIndex:'source_group'},{title:'状态',dataIndex:'status',render:(v:string)=><Tag>{words[v]||v}</Tag>},
        {title:'失败原因',dataIndex:'error'},{title:'操作',render:(_:unknown,r:Row)=>r.status==='failed'&&batch.status!=='cancelled'?<Button loading={busy} onClick={()=>act(`/imports/${batch.id}/items/${r.id}/retry-scan`)}>重试检查</Button>:'—'}
      ]}/><Pagination current={page} pageSize={50} total={batch.counts.registered} showSizeChanger={false} onChange={setPage} style={{marginTop:18}}/></Card>
    </>}
    <Modal title="创建导入批次" open={modal} onCancel={()=>setModal(false)} footer={null} destroyOnClose>
      <p>先从数据集草稿版本点击“批量导入”，再选择客户工具生成的 summary.json 自动填写清单数量。</p>
      <input type="file" accept=".json" aria-label="选择导出摘要" onChange={async e=>{const file=e.target.files?.[0];if(!file)return;try{
        if(file.size>16384)throw new Error('请选择小于 16KiB 的导出摘要');const summary=JSON.parse(await file.text());
        if(!summary.complete)throw new Error('客户导出存在失败，请先修复来源');
        form.setFieldsValue({source_id:summary.source_id,expected_files:summary.files,expected_bytes:summary.bytes});
      }catch(err:any){message.error(err.message);}}}/>
      <Form form={form} layout="vertical" style={{marginTop:18}} onFinish={async values=>{setBusy(true);try{
        const result=await post('/imports',{...values,idempotency_key:createKey});setModal(false);setToken(result.token||'');setTokenBatch(result.id);navigate(`/imports/${result.id}`);
      }catch(e:any){message.error(e.message);}finally{setBusy(false);}}}>
        <Form.Item name="version_id" label="目标草稿版本 ID" rules={[{required:true,pattern:/^[a-f0-9]{32}$/}]}><Input/></Form.Item>
        <Form.Item name="source_id" label="来源标识" rules={[{required:true,pattern:/^[A-Za-z0-9_.-]+$/}]}><Input maxLength={100}/></Form.Item>
        <Form.Item name="expected_files" label="预期文件数" rules={[{required:true}]}><InputNumber min={1} max={100000} precision={0}/></Form.Item>
        <Form.Item name="expected_bytes" label="预期总字节数" rules={[{required:true}]}><InputNumber min={1} precision={0}/></Form.Item>
        <Button type="primary" loading={busy} htmlType="submit">创建批次并签发凭据</Button>
      </Form>
    </Modal>
    <Modal title="保存批次专用凭据" open={!!token} onCancel={()=>setToken('')} onOk={()=>setToken('')} destroyOnClose>
      <Alert type="warning" message="凭据仅展示一次，有效期 24 小时，只能操作本批次；不要写入配置文件或命令行。关闭后可在批次页轮换。"/>
      <Typography.Paragraph code copyable style={{marginTop:18}}>{token}</Typography.Paragraph>
      <p>在客户终端运行以下命令，出现提示时粘贴凭据：</p>
      <pre className="preview-text">{`python -m clients.ingest upload --manifest <导出目录>/manifest.jsonl --platform ${window.location.origin} --batch ${tokenBatch}`}</pre>
      <p className="muted">企业自签名证书使用 --ca-file 指定受信任 CA 文件，不要关闭证书验证。</p>
    </Modal>
  </>;
}
