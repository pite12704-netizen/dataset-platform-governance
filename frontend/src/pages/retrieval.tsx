/** 【学习标注：检索工作台】知识出处、数据集条件搜索与图片相似检索。
 * 后端决定可访问范围；页面不接触对象存储地址、模型密钥或向量过滤表达式。
 */
import { useContext, useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { Alert, App, Button, Card, Checkbox, Form, Input, Modal, Select, Space, Spin, Table, Tabs, Tag } from 'antd';
import { api, post, put, Row } from '../api';
import { Session } from '../main';
import { PageTitle } from './data';

export function RetrievalPage() {
  const { user, spaces } = useContext(Session);
  const { message } = App.useApp();
  const [params] = useSearchParams();
  const [cap, setCap] = useState<Row>();
  const [tab, setTab] = useState('knowledge');
  const [query, setQuery] = useState('');
  const [result, setResult] = useState<Row>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [docs, setDocs] = useState<Row[]>([]), [docOffset, setDocOffset] = useState<number>();
  const [document, setDocument] = useState<Row>(), [editing, setEditing] = useState(false);
  const [catalog, setCatalog] = useState<Row[]>([]), [versions, setVersions] = useState<Row[]>([]);
  const [version, setVersion] = useState<string>(params.get('version') || '');
  const [images, setImages] = useState<Row[]>([]), [cursor, setCursor] = useState<string>();
  const [asset, setAsset] = useState<string>(), [file, setFile] = useState<File>();
  const [modality, setModality] = useState<string>(), [license, setLicense] = useState(''), [commercial, setCommercial] = useState(false);
  const [status, setStatus] = useState<Row>();
  const [form] = Form.useForm();
  const sequence = useRef(0), controller = useRef<AbortController | undefined>(undefined);
  const scopeSequence = useRef(0), currentVersion = useRef(version);
  const clearQuery = () => { sequence.current++; controller.current?.abort(); setBusy(false); setResult(undefined); setError(''); };
  const loadDocs = async (offset = 0) => {
    const data = await api(`/retrieval/documents?offset=${offset}`);
    setDocs(old => offset ? [...old, ...data.items] : data.items);
    setDocOffset(data.next_offset ?? undefined);
  };
  useEffect(() => {
    let live = true;
    Promise.all([api('/retrieval/capabilities'), api('/datasets?limit=100')]).then(async ([c, ds]) => {
      if (!live) return;
      setCap(c); setCatalog(ds.items);
      if (c.enabled) await loadDocs();
    }).catch(e => { if (live) setError(e.message); });
    return () => { live = false; sequence.current++; scopeSequence.current++; controller.current?.abort(); };
  }, []);
  useEffect(() => {
    const id = params.get('document');
    if (id && cap?.enabled) api(`/retrieval/documents/${encodeURIComponent(id)}`).then(setDocument).catch(e => message.error(e.message));
  }, [params, cap?.enabled, message]);
  useEffect(() => {
    const id = params.get('version');
    if (!id || !cap?.enabled) return;
    let live = true; const generation=++scopeSequence.current; currentVersion.current=id; setVersion(id); clearQuery();
    Promise.all([api(`/versions/${encodeURIComponent(id)}`), api(`/retrieval/versions/${encodeURIComponent(id)}/images`)]).then(([v, page]) => {
      if (!live || generation!==scopeSequence.current) return;
      setVersions([v]); setImages(page.items); setCursor(page.next_cursor ?? undefined); setTab('image');
    }).catch(e => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [params, cap?.enabled]);
  const run = async (operation: (signal: AbortSignal) => Promise<Row>) => {
    const id = ++sequence.current;
    controller.current?.abort(); const next = new AbortController(); controller.current = next;
    setBusy(true); setError(''); setResult(undefined);
    try { const found = await operation(next.signal); if (id === sequence.current) setResult(found); }
    catch (e: any) { if (id === sequence.current && e.name !== 'AbortError') setError(e.message); }
    finally { if (id === sequence.current) setBusy(false); }
  };
  const changeTab = (value: string) => {
    clearQuery(); setTab(value);
  };
  const loadImages = async (id: string, after = '') => {
    const generation=scopeSequence.current;
    const page = await api(`/retrieval/versions/${encodeURIComponent(id)}/images?cursor=${encodeURIComponent(after)}`);
    if(generation!==scopeSequence.current || id!==currentVersion.current)return;
    setImages(old => after ? [...old, ...page.items] : page.items); setCursor(page.next_cursor ?? undefined);
  };
  const chooseVersion = async (id: string) => {
    scopeSequence.current++; currentVersion.current=id; clearQuery();
    setVersion(id); setAsset(undefined); setImages([]); setStatus(undefined); setResult(undefined);
    try { await loadImages(id); } catch (e: any) { setError(e.message); }
  };
  const chooseDataset = async (id: string) => {
    const generation=++scopeSequence.current; currentVersion.current=''; clearQuery();
    setVersion(''); setVersions([]); setImages([]); setAsset(undefined); setCursor(undefined); setStatus(undefined);
    try { const ds = await api(`/datasets/${id}`); if(generation===scopeSequence.current)setVersions(ds.versions); } catch (e: any) { message.error(e.message); }
  };
  const saveDocument = async (values: Row) => {
    try {
      const data = { ...values, space_id: values.space_id || null, expected_revision: document?.revision };
      const saved = document ? await put(`/retrieval/documents/${document.id}`, data) : await post('/retrieval/documents', data);
      setDocument({ ...saved, can_manage: true }); setEditing(false); await loadDocs(); message.success('资料已保存；发布的资料将进入后台索引');
    } catch (e: any) { message.error(e.message); }
  };
  const queryImages = () => run(async signal => {
    if (file) {
      const body = new FormData(); body.append('version_id', version); body.append('file', file);
      const response = await fetch('/api/v1/retrieval/images/query', { method: 'POST', body, signal,
        credentials: 'include', headers: { 'X-Requested-With': 'dataset-platform' } });
      const data = await response.json(); if (!response.ok) throw new Error(data.message || '图片检索失败');
      return data;
    }
    return api('/retrieval/images/search', { method: 'POST', signal,
      body: JSON.stringify({ version_id: version, asset_id: asset, limit: 12 }) });
  });
  const versionPicker = <Space wrap>
    <Select aria-label="检索数据集" style={{ minWidth: 220 }} placeholder="选择数据集加载版本" options={catalog.map(d => ({ value: d.id, label: d.name }))} onChange={chooseDataset} />
    <Select aria-label="检索版本" style={{ minWidth: 200 }} placeholder="选择有权读取的版本" value={version || undefined}
      options={versions.map(v => ({ value: v.id, label: `v${v.number} · ${v.status}` }))} onChange={chooseVersion} />
  </Space>;
  return <>
    <PageTitle title="知识与多模态检索" subtitle="知识回答附原文；目录匹配具体版本；相似图片仍需人工判断。" />
    {!cap && <Spin />}
    {cap && !cap.enabled && <Alert type="info" showIcon message="向量检索未启用" description="需先完成数据库升级、配置编码服务和向量服务。当前可使用实时关键词目录搜索。" />}
    <Tabs activeKey={tab} onChange={changeTab} items={[
      { key: 'knowledge', label: '平台知识问答' }, { key: 'catalog', label: '自然语言找数据集' }, { key: 'image', label: '图片相似检索' },
    ]} />
    {tab === 'knowledge' && <>
      <Card><Space.Compact style={{ width: '100%' }}>
        <Input aria-label="知识问题" value={query} maxLength={500} onChange={e => setQuery(e.target.value)} placeholder="例如：独立复核与发布审核有什么区别？" />
        <Button type="primary" loading={busy} disabled={!cap?.enabled || !query.trim()} onClick={() => run(signal => api('/retrieval/knowledge/ask', { method: 'POST', signal, body: JSON.stringify({ query }) }))}>检索并回答</Button>
      </Space.Compact></Card>
      {result && <Card title="有出处的回答" style={{ marginTop: 16 }}><p style={{ whiteSpace: 'pre-wrap' }}>{result.answer}</p>
        <Tag>{result.mode === 'knowledge-rag' ? '模型结合资料生成' : '原文依据'}</Tag>
        {result.knowledge_citations?.map((c: Row, i: number) => <Card size="small" key={c.entry_id} style={{ marginTop: 12 }}
          title={`[${i + 1}] ${c.name} · ${c.section} · 修订 ${c.revision}`} extra={<Button onClick={async () => { try { setDocument(await api(`/retrieval/documents/${c.document_id}`)); } catch (e: any) { message.error(e.message); } }}>查看授权原文</Button>}>
          <p style={{ whiteSpace: 'pre-wrap' }}>{c.excerpt}</p></Card>)}
      </Card>}
      {cap?.enabled && <Card title="知识资料" style={{ marginTop: 16 }} extra={<Space><Button onClick={() => loadDocs().catch(e => message.error(e.message))}>刷新</Button>
        <Button onClick={() => { setDocument(undefined); form.resetFields(); setEditing(true); }}>新建资料</Button></Space>}>
        <p>平台通用资料由平台管理员维护，组织资料由组织管理员维护。正文请勿包含密钥或客户隐私。</p>
        <Table rowKey="id" dataSource={docs} pagination={false} columns={[
          { title: '文档', dataIndex: 'title' }, { title: '修订', dataIndex: 'revision' }, { title: '状态', dataIndex: 'status' },
          { title: '操作', render: (_: any, row: Row) => <Button onClick={async () => { try { setDocument(await api(`/retrieval/documents/${row.id}`)); } catch (e: any) { message.error(e.message); } }}>查看</Button> },
        ]} />{docOffset !== undefined && <Button onClick={() => loadDocs(docOffset).catch(e => message.error(e.message))}>加载更多资料</Button>}
      </Card>}
    </>}
    {tab === 'catalog' && <>
      <Card><Space direction="vertical" style={{ width: '100%' }}>
        <Input.TextArea aria-label="数据集需求" value={query} maxLength={500} onChange={e => setQuery(e.target.value)} placeholder="描述场景、任务及数据需求" />
        <Space wrap><Select aria-label="数据模态" allowClear placeholder="不限模态" value={modality} onChange={setModality} style={{ width: 150 }} options={['image', 'text', 'audio', 'video', 'tabular', 'multimodal'].map(value => ({ value, label: value }))} />
          <Input aria-label="许可名称" value={license} onChange={e => setLicense(e.target.value)} placeholder="许可名称精确匹配（可选）" style={{ width: 240 }} />
          <Checkbox checked={commercial} onChange={e => setCommercial(e.target.checked)}>仅资料明确声明允许商用</Checkbox>
          <Button type="primary" loading={busy} disabled={!query.trim() || !cap} onClick={() => run(signal => api('/retrieval/catalog/search', { method: 'POST', signal,
            body: JSON.stringify({ query, modality, license_name: license || undefined, commercial }) }))}>搜索版本</Button>
        </Space><p>商业使用条件依据版本卡片 usage_policy.commercial_use=allowed 的声明筛选，仍需核对许可及来源权利。</p>
      </Space></Card>
      {result && <Card title={`找到 ${result.items?.length || 0} 个版本 · ${result.mode}`} style={{ marginTop: 16 }}>
        {result.applied_filters && <p>实际筛选：模态 {result.applied_filters.modality || '不限'} · 许可 {result.applied_filters.license || '不限'} · {result.applied_filters.commercial_use_declared ? '须明确声明允许商用' : '未要求商业使用声明'}</p>}
        {result.items?.map((item: Row) => <Card key={item.version_id} size="small" style={{ marginBottom: 12 }} title={<Link to={item.url}>{item.name} · v{item.number}</Link>}>
          <p>{item.description}</p><Tag>{item.license}</Tag><span>{item.match_basis}</span></Card>)}
        {!result.items?.length && <p>当前权限和条件下未找到候选；请调整需求或检查索引状态。</p>}
      </Card>}
    </>}
    {tab === 'image' && <>
      <Alert type="info" message="检索相似内容，不自动删除或判定重复" description="请选择有权读取的版本。查询图仅用于本次检索，不会自动加入数据集。" />
      <Card style={{ marginTop: 16 }}><Space direction="vertical" style={{ width: '100%' }}>{versionPicker}
        <Space wrap><Select aria-label="查询图片" showSearch optionFilterProp="label" placeholder="使用版本内图片查询" style={{ minWidth: 280 }} value={asset} onChange={value => { setAsset(value); setFile(undefined); }}
          options={images.map(i => ({ value: i.id, label: i.filename }))} />
          {cursor && <Button onClick={() => loadImages(version, cursor).catch(e => message.error(e.message))}>加载更多图片</Button>}
          <label>或选择本地查询图：<input aria-label="本地查询图" type="file" accept="image/jpeg,image/png,image/webp,image/bmp" onChange={e => { const f = e.target.files?.[0]; if (f && f.size > (cap?.max_image_bytes || 0)) { message.error('图片超过容量限制'); return; } setFile(f); setAsset(undefined); }} /></label>
          {file && <Tag>{file.name}</Tag>}
          <Button type="primary" loading={busy} disabled={!cap?.image_enabled || !version || (!asset && !file)} onClick={queryImages}>查找相似图片</Button>
        </Space>
        <Space wrap><Button disabled={!cap?.enabled || !version} onClick={async () => { try { await post(`/retrieval/versions/${version}/index`); message.success('已登记索引事件，由独立 Worker 执行'); } catch (e: any) { message.error(e.message); } }}>负责人：建立/重建版本索引</Button>
          <Button disabled={!version || !cap?.enabled} onClick={async () => { try { setStatus(await api(`/retrieval/versions/${version}/index`)); } catch (e: any) { message.error(e.message); } }}>负责人：查看索引状态</Button>
        </Space>
        {status && <><Space wrap>{Object.entries(status.counts).map(([k, v]) => <Tag key={k}>{k}：{String(v)}</Tag>)}</Space>
          {status.errors.map((e: Row) => <p key={e.id}>{e.kind} · {e.error_code} · 尝试 {e.attempts} 次 {e.status === 'failed' && <Button onClick={async () => { try { await post(`/retrieval/events/${e.id}/retry`); setStatus(await api(`/retrieval/versions/${version}/index`)); } catch (err: any) { message.error(err.message); } }}>重试</Button>}</p>)}</>}
      </Space></Card>
      {result && <Card title={`相似结果 ${result.items?.length || 0} 张`} style={{ marginTop: 16 }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(180px,1fr))', gap: 16 }}>{result.items?.map((i: Row) => <div key={i.asset_id}>
          <img src={i.preview_url} alt={i.name} loading="lazy" style={{ width: '100%', height: 180, objectFit: 'contain' }} /><p>{i.name}</p>
          <Button onClick={() => { setAsset(i.asset_id); setFile(undefined); }}>以此图继续查询</Button></div>)}</div>
        {!result.items?.length && <p>暂无有效候选，请确认此版本的图片索引已完成。</p>}
      </Card>}
    </>}
    {result?.degraded && <Alert style={{ marginTop: 16 }} type="warning" message="部分检索服务不可用，已返回当前可用依据" />}
    {error && <Alert style={{ marginTop: 16 }} type="error" showIcon message={error} />}
    <Modal open={!!document && !editing} onCancel={() => setDocument(undefined)} footer={null} title={document?.title} width={860}>
      <p>修订 {document?.revision} · {document?.status}</p><pre style={{ whiteSpace: 'pre-wrap', maxHeight: '60vh', overflow: 'auto' }}>{document?.body}</pre>
      {document?.can_manage && <Space wrap><Button onClick={() => { form.setFieldsValue({ ...document, space_id: document.space_id || '' }); setEditing(true); }}>编辑此资料</Button>
        <Button onClick={async () => { try { await post(`/retrieval/documents/${document.id}/index`); message.success('已登记重建事件'); } catch (e: any) { message.error(e.message); } }}>重建此资料索引</Button>
        <Button onClick={async () => { try { const data = await api(`/retrieval/documents/${document.id}/index`); const e = data.event;
          if (!e) { message.info('尚未登记索引事件'); return; }
          Modal.info({ title: '文档索引状态', content: <Space direction="vertical"><span>{e.status} · 尝试 {e.attempts} 次 · 片段 {e.processed}</span><span>{e.error_code || '无已记录错误'}</span></Space> });
        } catch (e: any) { message.error(e.message); } }}>查看索引状态</Button></Space>}
    </Modal>
    <Modal open={editing} onCancel={() => setEditing(false)} onOk={() => form.submit()} title={document ? '编辑知识资料' : '新建知识资料'} width={820}>
      <Form form={form} layout="vertical" onFinish={saveDocument} initialValues={{ status: 'draft' }}>
        <Form.Item name="title" label="标题" rules={[{ required: true }]}><Input maxLength={200} /></Form.Item>
        <Form.Item name="space_id" label="可见空间"><Select disabled={!!document} placeholder="选择组织；仅平台管理员可建通用资料" options={[
          ...(user?.platform_admin ? [{ value: '', label: '平台通用（所有已登录用户）' }] : []), ...spaces.map(s => ({ value: s.id, label: s.name }))]} /></Form.Item>
        <Form.Item name="status" label="状态" rules={[{ required: true }]}><Select options={[{ value: 'draft', label: '草稿' }, { value: 'published', label: '发布并索引' }, { value: 'archived', label: '下架' }]} /></Form.Item>
        <p>导入文本/Markdown：<input aria-label="导入知识文档" type="file" accept=".txt,.md,text/plain,text/markdown" onChange={async e => {
          const f=e.target.files?.[0]; if(!f)return;
          if(f.size>60000){message.error('请导入60KB以内的UTF-8文本，并按主题拆分较长资料');return;}
          try{const body=new TextDecoder('utf-8',{fatal:true}).decode(await f.arrayBuffer());if(body.length>18000)throw new Error('正文超过18000字');form.setFieldsValue({body});}
          catch(err:any){message.error(err.message || '无法读取UTF-8文档');}
        }}/></p>
        <Form.Item name="body" label="正文（支持 Markdown 标题分节，原文按纯文本展示）" rules={[{ required: true }]}><Input.TextArea rows={14} maxLength={18000} /></Form.Item>
      </Form>
    </Modal>
  </>;
}
