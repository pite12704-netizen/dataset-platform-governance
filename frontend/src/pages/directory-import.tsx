/** 网页用户选择目录即可接入，不安装运行环境、不接触源码或批次令牌。 */
import {useContext,useEffect,useRef,useState} from 'react';
import {Link,useSearchParams,useBlocker} from 'react-router-dom';
import {Alert,App,Button,Card,Checkbox,Descriptions,Input,Modal,Pagination,Popconfirm,Progress,Select,Space,Table} from 'antd';
import {FolderOpenOutlined} from '@ant-design/icons';
import {api,bytes,key,Row} from '../api';
import {Session} from '../main';
import {DirectoryDraft,DirectorySelection,prepareDirectory,selectDirectory,transferDirectory} from '../directory-import';
import {clearDirectoryDraft,readDirectoryDraft,saveDirectoryDraft} from '../directory-drafts';
import {directoryHasher} from '../directory-hasher';
import {uploadRequest} from '../uploads';

export function DirectoryImportPage(){
  const {user,config}=useContext(Session),{message}=App.useApp(),[params,setParams]=useSearchParams();
  const versionId=params.get('version')||'',resumeBatch=params.get('batch')||'';
  const [datasets,setDatasets]=useState<Row[]>([]),[dataset,setDataset]=useState(''),[versions,setVersions]=useState<Row[]>([]);
  const [version,setVersion]=useState<Row>(),[draft,setDraft]=useState<DirectoryDraft>();
  const [selection,setSelection]=useState<DirectorySelection>(),[source,setSource]=useState(''),[license,setLicense]=useState('');
  const [ready,setReady]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[status,setStatus]=useState('');
  const [progress,setProgress]=useState(0),[ackSkipped,setAckSkipped]=useState(false),[issuePage,setIssuePage]=useState(1);
  const [result,setResult]=useState<Row>();
  const blocker=useBlocker(busy);
  const controller=useRef<AbortController|null>(null),input=useRef<HTMLInputElement|null>(null),searchTimer=useRef<ReturnType<typeof setTimeout>|undefined>(undefined);
  const mounted=useRef(true);
  const supported=typeof Worker!=='undefined' && !!globalThis.indexedDB && !!navigator.locks &&
    'webkitdirectory' in document.createElement('input');
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;controller.current?.abort();clearTimeout(searchTimer.current);};},[]);
  useEffect(()=>{
    if(!busy)return;
    const prevent=(event:BeforeUnloadEvent)=>{event.preventDefault();event.returnValue='';};
    window.addEventListener('beforeunload',prevent);return()=>window.removeEventListener('beforeunload',prevent);
  },[busy]);
  useEffect(()=>{if(!busy&&blocker.state==='blocked')blocker.proceed();},[busy,blocker]);
  useEffect(()=>{
    let current=true;controller.current?.abort();setReady(false);setSelection(undefined);setDraft(undefined);setResult(undefined);setError('');
    if(!versionId || !user)return;
    Promise.all([api(`/versions/${versionId}`),readDirectoryDraft(user.id,versionId)]).then(([v,saved])=>{
      if(!current)return;
      if(!v.can_manage || !['draft','rejected'].includes(v.status))throw new Error('请选择你负责的草稿版本');
      if(!['image','multimodal'].includes(v.dataset.modality))throw new Error('图片文件夹上传仅支持图像或多模态数据集，请选择正确类型');
      if(resumeBatch && saved?.batchId!==resumeBatch)throw new Error('本浏览器没有此批次的续传清单。请回原浏览器继续；清理记录或更换电脑后不能自动恢复此网页批次。');
      setVersion(v);setDraft(saved);setSource(saved?.source??v.source??'');setLicense(saved?.license??v.license??'');setReady(true);
    }).catch(e=>{if(current)setError(e.message);});
    return()=>{current=false;controller.current?.abort();};
  },[versionId,user?.id,resumeBatch]);
  const searchDatasets=async(q='')=>{
    try{const response=await api(`/datasets?mine=true&limit=50&q=${encodeURIComponent(q)}`);if(mounted.current)setDatasets(response.items);}
    catch(e:any){if(mounted.current)setError(e.message);}
  };
  useEffect(()=>{if(!versionId)searchDatasets();},[versionId,user?.id]);
  const chooseDataset=async(id:string)=>{
    setDataset(id);setVersions([]);setError('');
    try{const response=await api(`/datasets/${id}`);setVersions(response.versions.filter((v:Row)=>['draft','rejected'].includes(v.status)));}
    catch(e:any){setError(e.message);}
  };
  const start=async()=>{
    if(!user || !selection || busy || !ready)return;
    const abort=new AbortController();controller.current=abort;setBusy(true);setError('');setResult(undefined);setProgress(0);
    try{
      await navigator.locks.request(`directory-import:${user.id}:${versionId}`,{ifAvailable:true},async lock=>{
        if(!lock)throw new Error('另一个页面正在处理此版本，请先在那个页面暂停');
        const previous=await readDirectoryDraft(user.id,versionId);
        if(resumeBatch && previous?.batchId!==resumeBatch)throw new Error('此浏览器续传记录已变化，请重新打开批次');
        const hasher=directoryHasher(abort.signal);
        let prepared:DirectoryDraft;
        try{
          setStatus('正在校验文件夹，校验完成后才会开始传输');
          prepared=await prepareDirectory(selection,previous??{ownerId:user.id,versionId,createKey:key(),
            sourceId:'browser-'+key(),source,license},hasher.hash,abort.signal,(count,total)=>{
            setStatus(`校验文件 ${count} / ${total}`);setProgress(Math.round(count/total*100));
          },previous);
        }finally{hasher.close();}
        const save=async(value:DirectoryDraft)=>{await saveDirectoryDraft(value);if(mounted.current)setDraft(value);};
        setProgress(0);setStatus('开始上传');
        const response=await transferDirectory(prepared,selection,
          (path,init={})=>uploadRequest(path,init,abort.signal,setStatus),save,abort.signal,(count,total,path)=>{
            if(mounted.current){setProgress(Math.round(count/total*100));setStatus(`完整传输 ${count} / ${total} · ${path}`);}
          });
        if(mounted.current){setResult(response);setStatus('文件已完整传输，请核对后台检查结果');}
      });
    }catch(e:any){
      if(mounted.current){if(abort.signal.aborted)setStatus('已暂停。留在本页可继续；刷新后重新选择同一文件夹。');else setError(e.message);}
    }finally{if(mounted.current)setBusy(false);controller.current=null;}
  };
  const issues=[...(selection?.blocked??[]),...(selection?.skipped??[])];
  return <><Link to={versionId?`/imports?version=${versionId}`:'/imports'}>← 导入批次</Link><div className="page-title" style={{marginTop:18}}><div>
    <h1>从本地文件夹上传图片</h1><p>选择文件夹即可递归接入图片，自动校验、分片上传，并跟踪每张图片的检查结果。</p>
  </div></div>
    <Alert showIcon type="info" message="无需安装软件或下载项目源码" description="上传期间请保留此页、保持联网并避免电脑休眠。查看服务器进度会打开新标签页，上传继续在此执行；切换本站其他页面前会询问是否暂停。刷新后需重新选择原文件夹续传。"/>
    {!supported&&<Alert style={{marginTop:16}} type="warning" showIcon message="当前浏览器不具备目录上传和续传能力，请使用支持这些功能的桌面浏览器。"/>}
    {error&&<Alert style={{marginTop:16}} type="error" showIcon message={error}/>}
    {!versionId?<Card title="先选择目标草稿版本" style={{marginTop:20}}><Space wrap>
      <Select style={{width:300}} value={dataset||undefined} placeholder="选择我的数据集" showSearch filterOption={false}
        onSearch={q=>{clearTimeout(searchTimer.current);searchTimer.current=setTimeout(()=>searchDatasets(q),300);}}
        options={datasets.map(d=>({value:d.id,label:d.name}))} onChange={chooseDataset}/>
      <Select style={{width:220}} placeholder="选择草稿版本" options={versions.map(v=>({value:v.id,label:`版本 ${v.number}`}))}
        onChange={value=>setParams({version:value})}/><Link to="/upload">创建新数据集和草稿</Link>
    </Space></Card>:<Card title={version?`${version.dataset?.name||'目标数据集'} · 版本 ${version.number}`:'正在读取目标版本'} style={{marginTop:20}}>
      {version&&<p><Link to={`/datasets/${version.dataset_id}?version=${versionId}`} target="_blank" rel="noopener noreferrer">查看目标数据集（新标签页）</Link></p>}
      <label>来源说明<Input.TextArea value={source} onChange={e=>setSource(e.target.value)} maxLength={2000} disabled={busy||!!draft} rows={2}/></label>
      <label>使用许可<Input value={license} onChange={e=>setLicense(e.target.value)} maxLength={1000} disabled={busy||!!draft} style={{margin:'8px 0 16px'}}/></label>
      <input type="file" multiple hidden ref={node=>{input.current=node;if(node)node.setAttribute('webkitdirectory','');}}
        aria-label="选择本地图片文件夹" disabled={busy||!supported||!ready} onChange={e=>{
          try{setSelection(selectDirectory(Array.from(e.target.files??[]),config));setAckSkipped(false);setIssuePage(1);setError('');setResult(undefined);}
          catch(err:any){setSelection(undefined);setError(err.message);}e.target.value='';
        }}/>
      <Space wrap><Button icon={<FolderOpenOutlined/>} disabled={busy||!supported||!ready} onClick={()=>input.current?.click()}>
        {draft?'重新选择原文件夹':'选择本地文件夹'}</Button>
        <Button type="primary" loading={busy} disabled={!ready||!supported||!selection?.files.length||!!selection?.blocked.length||
          !!selection?.skipped.length&&!ackSkipped} onClick={start}>{draft?'校验并继续上传':'校验并开始上传'}</Button>
        {busy&&<Button onClick={()=>controller.current?.abort()}>暂停</Button>}
        {draft?.batchId&&<Link to={`/imports/${draft.batchId}`} target="_blank" rel="noopener noreferrer">查看服务器进度与检查结果（新标签页）</Link>}
      </Space>
      {selection&&<Descriptions style={{marginTop:18}} items={[
        {key:'count',label:'合格图片',children:selection.files.length}, {key:'bytes',label:'原图大小',children:bytes(selection.bytes)},
        {key:'blocked',label:'须修复',children:selection.blocked.length},{key:'skipped',label:'不支持格式',children:selection.skipped.length}]}/>}
      {!!selection?.blocked.length&&<Alert type="error" message="请先修复以下不合格图片，再重新选择文件夹；不会悄悄省略这些图片。"/>}
      {!!selection?.skipped.length&&<Checkbox checked={ackSkipped} disabled={busy} onChange={e=>setAckSkipped(e.target.checked)}>
        我确认不上传列出的 {selection.skipped.length} 个不支持格式的文件</Checkbox>}
      {!!issues.length&&<><Table style={{marginTop:16}} rowKey="path" pagination={false} dataSource={issues.slice((issuePage-1)*50,issuePage*50)}
        columns={[{title:'相对文件路径',dataIndex:'path',ellipsis:true},{title:'问题',dataIndex:'reason'}]}/>
        <Pagination current={issuePage} pageSize={50} total={issues.length} showSizeChanger={false} onChange={setIssuePage}/></>}
      {(busy||status)&&<><Progress style={{marginTop:20}} percent={progress}/><p aria-live="polite">{status}</p></>}
      {result&&<Alert showIcon type={result.status==='completed'?'success':'warning'} message={result.status==='completed'?'整批文件检查通过':'传输已完成，后台检查结果仍需处理'}
        description={`检查通过 ${result.counts?.clean??0}，检查中 ${result.counts?.checking??0}，失败 ${result.counts?.failed??0}。文件入库后仍须治理、人工复核和发布。`}/>}
      {draft&&!busy&&<p style={{marginTop:18}}><Popconfirm title="仅清除这台浏览器的续传记录？" description="不会取消或删除服务器批次。清除后无法自动匹配原批次，请先确认已完成或已在批次页取消。"
        onConfirm={async()=>{try{await navigator.locks.request(`directory-import:${user!.id}:${versionId}`,{ifAvailable:true},async lock=>{
          if(!lock)throw new Error('其他页面正在上传，不能清除记录');await clearDirectoryDraft(user!.id,versionId);setDraft(undefined);setSelection(undefined);
        });}catch(e:any){message.error(e.message);}}}><Button type="link">清除本机续传记录以准备新批次</Button></Popconfirm></p>}
    </Card>}
    <Modal title="正在校验或上传，是否暂停并离开？" open={blocker.state==='blocked'}
      okText="暂停并离开" cancelText="留在此页继续上传" onCancel={()=>blocker.reset?.()}
      onOk={()=>{controller.current?.abort();blocker.proceed?.();}}>
      <p>离开当前页面会停止继续读取本地图片。已完成文件和已收到的分片会保留，之后可重新选择原目录续传。</p>
      <p>仅查看服务器进度请留在此页，使用“查看服务器进度与检查结果（新标签页）”。</p>
    </Modal>
  </>;
}
