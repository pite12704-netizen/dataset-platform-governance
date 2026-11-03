/** 图像试验结果页：所有图片和报告仍通过平台鉴权接口读取，不直接暴露本地路径。 */
import { useContext, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, App, Button, Empty, Modal, Select, Space, Spin, Tabs, Tag } from 'antd';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { api, ApiError, post, retryAfterSeconds, Row } from '../api';
import { previewQueue } from '../preview-queue';
import { Session } from '../main';
import '../image-pilot.css';

const words: Record<string, string> = {
  hat: '戴帽子', no_hat: '未戴帽子', short_sleeves: '短袖', long_sleeves: '长袖', sleeveless: '无袖',
  unclear: '不确定', white: '白色', black: '黑色', red: '红色', blue: '蓝色', green: '绿色', yellow: '黄色',
  other_or_unclear: '其他 / 不确定', exact: '完全重复', near: '重编码近重复',
};
export function AssetImage({id, alt, path, binary=false, className=''}: {id: string; alt: string; path?: string; binary?:boolean; className?:string}) {
  const {user}=useContext(Session);
  const [source, setSource] = useState(''), [error, setError] = useState('');
  const root=useRef<HTMLDivElement>(null),[visibleFor,setVisibleFor]=useState('');
  const identity=`${user?.id||'anonymous'}:${path||id}:${binary}`;
  useEffect(()=>{
    if(!root.current)return;
    if(typeof IntersectionObserver==='undefined'){setVisibleFor(identity);return;}
    const observer=new IntersectionObserver(entries=>{if(entries.some(e=>e.isIntersecting)){setVisibleFor(identity);observer.disconnect();}}, {rootMargin:'240px'});
    observer.observe(root.current);return()=>observer.disconnect();
  },[identity]);
  useEffect(() => {
    let active = true,objectUrl='';const controller=new AbortController();
    setSource(''); setError('');
    if(visibleFor!==identity)return()=>controller.abort();
    const endpoint=path||`/assets/${encodeURIComponent(id)}/preview`;
    previewQueue.load<Row|Blob>(identity,async signal=>{
      if(!binary)return api(endpoint,{signal});
      const response=await fetch('/api/v1'+endpoint,{credentials:'include',signal});
      if(!response.ok){const data=await response.json().catch(()=>({}));throw new ApiError(data.message||'原图不可用',response.status,retryAfterSeconds(response.headers.get('Retry-After')));}
      return response.blob();
    },controller.signal).then(result=>{
      if(!active)return;
      if(result instanceof Blob){
        if(!result.type.startsWith('image/'))throw new Error('此资产没有图像预览');
        objectUrl=URL.createObjectURL(result);setSource(objectUrl);
      }else{
        if(result.kind!=='image'||!result.content?.startsWith('data:image/jpeg;base64,'))throw new Error('此资产没有图像预览');
        setSource(result.content);
      }
    }).catch(e=>{if(active)setError([429,503].includes(e.status)?'预览暂时繁忙，请稍后刷新页面':e.message||'预览读取失败，请刷新重试');});
    return()=>{active=false;controller.abort();if(objectUrl)URL.revokeObjectURL(objectUrl);};
  }, [id,path,binary,identity,visibleFor]);
  return <div ref={root} className={'asset-preview '+className}>{source?<img src={source} alt={alt}/>:<div className="pilot-image-loading" role="status">{error || '正在排队读取授权预览…'}</div>}</div>;
}

export function ImagePilotPage() {
  const { versionId } = useParams();
  const navigate = useNavigate(), { message } = App.useApp();
  const [importing, setImporting] = useState(false);
  const [report, setReport] = useState<Row | null>(null), [version, setVersion] = useState<Row | null>(null);
  const [datasets, setDatasets] = useState<Row[]>([]), [loading, setLoading] = useState(true), [error, setError] = useState('');
  const [hat, setHat] = useState('all'), [sleeve, setSleeve] = useState('all'), [people, setPeople] = useState('all');
  const [color, setColor] = useState('all');
  const [selected, setSelected] = useState<Row | null>(null);
  useEffect(() => {
    let active = true;
    setLoading(true); setError(''); setReport(null); setVersion(null); setSelected(null);
    (async () => {
      if (!versionId) {
        const result = await api('/datasets?mine=true&modality=image&limit=100');
        if (active) setDatasets(result.items);
        return;
      }
      const v = await api(`/versions/${versionId}`);
      if (!v.can_download) throw new Error('请先获得此版本授权，并在数据集详情页确认使用许可。');
      const asset = v.assets.find((a: Row) => a.filename === 'image_pilot_report.json');
      if (!asset) throw new Error('此版本没有图像试验报告。');
      const result = await api(`/assets/${asset.id}/download`);
      if (result.schema_version !== 1 || !Array.isArray(result.images)) throw new Error('报告格式不受支持。');
      if (active) { setVersion(v); setReport(result); }
    })().catch(e => active && setError(e.message)).finally(() => active && setLoading(false));
    return () => { active = false; };
  }, [versionId]);
  const images = useMemo(() => (report?.images || []).filter((r: Row) => {
    if (people === 'single' && r.person_count !== 1) return false;
    if (people === 'multi' && r.person_count < 2) return false;
    // 所有属性条件必须同时落在同一个人物上，不能将两个人的属性拼成一次命中。
    return r.persons.some((p: Row) => (hat === 'all' || p.attributes.hat.label === hat) &&
      (sleeve === 'all' || p.attributes.sleeve.label === sleeve) &&
      (color === 'all' || p.attributes.upper_color.label === color));
  }), [report, people, hat, sleeve, color]);
  if (loading) return <Spin tip="读取真实试验结果"><div style={{height:180}}/></Spin>;
  if (error) return <Alert type="error" message={error} showIcon action={<Link to="/image-pilot">返回试验列表</Link>}/>;
  if (!versionId) return <div className="image-pilot">
    <h1>图像识别</h1><p className="muted">选择自己的图像数据集，从版本中发起人物识别并进入人工复核。模型结果是候选标签。</p>
    {datasets.length ? datasets.map(d => <div className="pilot-dataset" key={d.id}>
      <div><h3>{d.name}</h3><p>{d.description}</p></div>
      <Space><Link to={`/datasets/${d.id}`}><Button type="primary">选择版本并识别</Button></Link></Space>
    </div>) : <Empty description={<span>尚无图像数据集，先<Link to="/upload">上传图片并创建版本</Link>。</span>}/>}
  </div>;
  if (!report || !version) return null;
  const metrics = report.summary;
  const exportCurrent = () => {
    const blob = new Blob([JSON.stringify({status:'pending_human_review', source_version_id:versionId,
      filters:{hat,sleeve,color,people,match:'same_person'}, images}, null, 2)], {type:'application/json'});
    const url = URL.createObjectURL(blob), link = document.createElement('a');
    link.href = url; link.download = 'image-selection-pending-review.json'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return <div className="image-pilot">
    <div className="pilot-heading"><div><Link to="/image-pilot">图像试验 /</Link><h1>人物图片去重与属性整理</h1>
      <p className="muted">COCO 2017 · {report.model_name} · 当前版本 v{version.number}</p></div>
      <Space><Link to={`/datasets/${version.dataset.id}`}><Button>版本与文件</Button></Link>
      {version.can_manage && <Button type="primary" loading={importing} onClick={async()=>{
        setImporting(true); try {
          const asset=version.assets.find((a:Row)=>a.filename==='image_pilot_report.json');
          const batch=await post('/image-reviews/import',{report_asset_id:asset.id}); navigate(`/image-reviews/${batch.id}`);
        } catch(e:any){message.error(e.message);} finally{setImporting(false);}
      }}>进入逐人复核</Button>}</Space></div>
    <Alert type="warning" showIcon message="这是实际模型输出的试验结果，尚未完成人工复核。"
      description="人物框来自 COCO 原始标注；帽子、袖长和颜色由本地视觉模型判断。本页不把预测当作正确答案，也不宣称属性准确率。"/>
    <div className="pilot-metrics">{[
      ['原始图片',metrics.original_images],['单人 / 多人',`${metrics.single_person_images} / ${metrics.multi_person_images}`],
      ['人物实例',metrics.persons],['完全重复 / 近重复',`${metrics.exact_duplicates} / ${metrics.near_duplicate_candidates}`],
      ['保留图片',metrics.kept_images],['人工复核',metrics.human_reviewed],
    ].map(([label,value])=><div key={String(label)}><span>{label}</span><strong>{value}</strong></div>)}</div>
    <Tabs items={[{key:'images',label:'属性筛选',children:<>
      <div className="pilot-filters"><Space wrap>
        <Select aria-label="人物数量" value={people} onChange={setPeople} style={{width:140}}
          options={[{value:'all',label:'全部人数'},{value:'single',label:'单人图片'},{value:'multi',label:'多人图片'}]}/>
        <Select aria-label="帽子属性" value={hat} onChange={setHat} style={{width:140}}
          options={[{value:'all',label:'所有帽子属性'},...['hat','no_hat','unclear'].map(value=>({value,label:words[value]}))]}/>
        <Select aria-label="袖长属性" value={sleeve} onChange={setSleeve} style={{width:140}}
          options={[{value:'all',label:'所有袖长属性'},...['short_sleeves','long_sleeves','sleeveless','unclear'].map(value=>({value,label:words[value]}))]}/>
        <Select aria-label="上衣颜色" value={color} onChange={setColor} style={{width:155}}
          options={[{value:'all',label:'所有上衣颜色'},...['white','black','red','blue','green','yellow','other_or_unclear'].map(value=>({value,label:words[value]}))]}/>
        <span>命中 {images.length} 张 · 组合条件对应同一个人</span>
      </Space><Button onClick={exportCurrent} disabled={!images.length}>导出当前候选清单</Button></div>
      <div className="pilot-grid">{images.map((r:Row)=><button className="pilot-image" key={r.id} onClick={()=>setSelected(r)}>
        <AssetImage id={r.asset_id} alt={`COCO ${r.id}，${r.person_count} 人`}/>
        <div><strong>{r.filename}</strong><span>{r.person_count} 人 · 待人工复核</span>
          <div className="pilot-labels">{Array.from(new Set(r.persons.flatMap((p:Row)=>[p.attributes.hat.label,p.attributes.sleeve.label])))
            .map((v:any)=><Tag key={v}>{words[v] || v}</Tag>)}</div></div>
      </button>)}</div>{!images.length && <Empty description="没有符合当前组合条件的图片"/>}
    </>},{key:'duplicates',label:'重复图片对比',children:<>
      <p>加入了 {report.fixture_count} 张明确标记的重复测试副本，原图未删除。该结果只验证这批受控样本。</p>
      <div className="pilot-duplicates">{report.duplicates.map((d:Row)=><div className="pilot-pair" key={d.filename}>
        <div><AssetImage id={d.keep_asset_id} alt={`保留 ${d.keep}`}/><span>保留原图 · {d.keep}</span></div>
        <div><AssetImage id={d.asset_id} alt={`测试副本 ${d.filename}`}/><span>排除测试副本 · {d.filename}</span></div>
        <p><Tag>{words[d.kind]}</Tag>哈希距离 {d.distance} · 平均像素差 {d.pixel_mae}</p>
      </div>)}</div>
    </>},{key:'workflow',label:'流程验证',children:<>
      <p>以下是本地测试账号通过真实业务接口执行的结果。流程审核为演示，不等于人工检查了每个属性。</p>
      {(report.workflow || []).map((step:Row)=><div className="pilot-step" key={step.name}>
        <Tag color={step.passed?'success':'error'}>{step.passed?'通过':'失败'}</Tag><strong>{step.name}</strong><span>{step.detail}</span></div>)}
      <p className="muted">来源：COCO 2017 验证集；本批图片按原始元数据选择 CC BY 2.0。来源链接与逐图记录保存在导出清单中。</p>
    </>} ]}/>
    <Modal title={selected ? `${selected.filename} · ${selected.person_count} 个人物` : ''} open={!!selected}
      onCancel={()=>setSelected(null)} footer={null} width={1050}>
      {selected && <div className="pilot-inspect"><div className="pilot-boxes">
        <AssetImage id={selected.asset_id} alt="人物框与原图"/>
        {selected.persons.map((p:Row,index:number)=><div className="pilot-box" key={p.id} style={{
          left:`${100*p.bbox[0]/selected.width}%`,top:`${100*p.bbox[1]/selected.height}%`,
          width:`${100*p.bbox[2]/selected.width}%`,height:`${100*p.bbox[3]/selected.height}%`}}><span>人物 {index+1}</span></div>)}
      </div><div>{selected.persons.map((p:Row,index:number)=><div className="pilot-person" key={p.id}>
        <h3>人物 {index+1}</h3><p>帽子：{words[p.attributes.hat.label]}</p>
        <p>袖长：{words[p.attributes.sleeve.label]}</p><p>上衣颜色：{words[p.attributes.upper_color.label]}</p>
        <Tag>模型候选 · 待人工复核</Tag>
      </div>)}</div></div>}
    </Modal>
  </div>;
}
