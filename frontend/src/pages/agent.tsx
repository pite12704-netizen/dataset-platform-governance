/** 【学习标注：数据工作助手页面】选择版本、对话、操作预览、确认记录和运行追踪。 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { Alert, App, Button, Card, Descriptions, Empty, Form, Input, InputNumber, Modal, Popconfirm, Select, Space, Spin, Table, Tabs, Tag } from 'antd';
import { ArrowRightOutlined, ClockCircleOutlined, PlusOutlined, ReloadOutlined, RobotOutlined, SendOutlined } from '@ant-design/icons';
import { api, bytes, date, labels, post, Row } from '../api';
import { PageTitle, Status } from './data';
import { appendResult, ChatRequest, keepPending, newChatRequest, readPending, savePending } from '../agent-request';
import '../agent.css';
import {WorkflowPlan,AgentTaskProgress} from '../agent-workflow';

const intents = [
  { value: 'auto', label: '自动识别' }, { value: 'agent', label: 'Agent 综合查询' }, { value: 'search', label: '找数据' },
  { value: 'compare', label: '版本对比' }, { value: 'quality', label: '解读质量' },
  { value: 'application', label: '起草申请' }, { value: 'jobs', label: '查询任务' },
  { value: 'quality_job', label: '运行质量检查' }, { value: 'convert', label: '格式转换' },
  { value: 'package', label: '打包数据' },
  {value:'diagnose',label:'流程诊断'},{value:'vision',label:'图像识别计划'},{value:'dedup',label:'去重计划'},
];
const actionLabels: Record<string, string> = { application: '使用申请', quality: '质量检查', convert: '格式转换', package: '数据打包',vision:'视觉预标注',dedup:'重复候选检测' };
const actionStatus: Record<string, string> = { pending: '待确认', completed: '已提交', cancelled: '已取消', expired: '已过期' };

export function AgentPage() {
  // 页面只负责收集选择和展示结果；权限判断与实际执行全部留在后端。
  const { message } = App.useApp();
  const [params] = useSearchParams();
  const [cap, setCap] = useState<Row>();
  const [conversations, setConversations] = useState<Row[]>([]);
  const [conversation, setConversation] = useState<string>();
  const [history, setHistory] = useState<Row[]>([]);const [pins,setPins]=useState<Row[]>([]);
  const [actions, setActions] = useState<Row[]>([]);
  const [runs, setRuns] = useState<Row[]>([]);
  const [catalog, setCatalog] = useState<Row[]>([]);
  const [choices, setChoices] = useState<Row[]>([]);
  const [versions, setVersions] = useState<string[]>([]);
  const [input, setInput] = useState('');
  const [intent, setIntent] = useState('auto');
  const [busy, setBusy] = useState(false);
  const [preparing, setPreparing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [plan, setPlan] = useState<Row>();
  const [tracking,setTracking]=useState<string>();
  useEffect(()=>{if(!tracking){const recent=actions.find(a=>a.status==='completed'&&['vision','dedup'].includes(a.kind)&&a.result?.job_id);if(recent)setTracking(recent.id);}},[actions,tracking]);
  const messageList = useRef<HTMLDivElement>(null);
  const sending = useRef(false);
  const account = useRef<string | undefined>(undefined);
  const pendingRef = useRef<ChatRequest | undefined>(undefined);
  const [pending, setPending] = useState<ChatRequest>();
  const setRequest = (payload?: ChatRequest) => {
    pendingRef.current = payload; setPending(payload);
    if (account.current) savePending(account.current, payload);
  };

  const refresh = useCallback(async () => {
    const [sessions, pending, logs] = await Promise.all([api('/agent/conversations'), api('/agent/actions'), api('/agent/runs')]);
    setConversations(sessions); setActions(pending); setRuns(logs);
  }, []);
  const addChoices = (items: Row[]) => setChoices(old => [...old.filter(o => !items.some(n => n.id === o.id)), ...items]);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [status, datasets, me] = await Promise.all([api('/agent/capabilities'), api('/datasets?limit=100'), api('/auth/me')]);
        if (cancelled) return;
        account.current = me.id;
        const restored = readPending(me.id);
        if (restored) { pendingRef.current = restored; setPending(restored); setInput(restored.message); }
        setCap(status); setCatalog(datasets.items); await refresh();
        const initial = params.get('version');
        if (initial && !cancelled) {
          const v = await api(`/versions/${initial}`);
          addChoices([{ ...v, name: v.dataset.name }]); setVersions([v.id]);
        }
      } catch (e: any) { if (!cancelled) message.error(e.message); }
    })();
    return () => { cancelled = true; };
  }, [refresh, message, params]);
  useEffect(() => { const list = messageList.current; list?.scrollTo({ top: list.scrollHeight, behavior: 'smooth' }); }, [history, busy]);

  const loadConversation = async (id: string) => {
    setBusy(true);
    try {
      const data = await api(`/agent/conversations/${id}`);
      setConversation(id); setVersions([]);setPins(await api(`/agent/conversations/${id}/pins`));
      setHistory(data.messages.map((r: Row) => ({ ...r.outputs, question: r.inputs.message || '确认并提交操作',
        run_id: r.id, tool:r.tool, source:r.source, mode: r.model, status: r.status, duration_ms: r.duration_ms })));
    } catch (e: any) { message.error(e.message); } finally { setBusy(false); }
  };
  const chooseDataset = async (id: string) => {
    try {
      const ds = await api(`/datasets/${id}`);
      addChoices(ds.versions.map((v: Row) => ({ ...v, name: ds.name })));
      message.info('已加载该数据集的可见版本，请在右侧选择');
    } catch (e: any) { message.error(e.message); }
  };
  const send = async (text = input, selectedIntent = intent, retry = false, selectedVersions = versions) => {
    // 【Agent 第一步】发送问题、明确意图、版本 ID 和对话 ID；响应始终包含来源和运行记录。
    if (!text.trim() || busy || sending.current || !account.current || (pendingRef.current && !retry)) return;
    const payload = pendingRef.current || newChatRequest(text, selectedIntent, selectedVersions, conversation);
    setRequest(payload); sending.current = true; setBusy(true);
    try {
      const result = await post('/agent/chat', payload);
      setRequest(); setConversation(result.conversation_id);
      setHistory(old => appendResult(old, { question: payload.message, ...result })); setInput('');
      await refresh().catch(e => message.warning('结果已保存，记录列表刷新失败：' + e.message));
    } catch (e: any) {
      if (!keepPending(e)) setRequest();
      message.error(e.message || '网络中断，暂未确认结果；可以检查记录或沿用原编号重试');
    } finally { sending.current = false; setBusy(false); }
  };
  const checkPending = async () => {
    const payload = pendingRef.current;
    if (!payload || sending.current) return;
    sending.current = true; setBusy(true);
    try {
      const row = await api(`/agent/requests/${payload.request_id}`);
      if (['running', 'interrupted'].includes(row.status)) { message.info('查询尚未完成或执行已中断，可稍后沿用原编号检查或重试'); return; }
      setRequest(); setConversation(row.conversation_id); setInput('');
      setHistory(old => appendResult(old, { ...row.outputs, question: payload.message,
        run_id: row.id, status: row.status, mode: row.model, duration_ms: row.duration_ms }));
      await refresh().catch(e => message.warning(e.message));
    } catch (e: any) { message.info(e.message); } finally { sending.current = false; setBusy(false); }
  };
  const prepare = async (draft: Row, values: Row) => {
    // 【Agent 第二步】把用户编辑后的草稿交给后端生成执行预览，此时没有业务副作用。
    setPreparing(true);
    try {
      const result = await post('/agent/actions', { kind: draft.kind, version_id: draft.version_id,
        conversation_id: conversation || null, ...values });
      setPlan(result); await refresh();
    } catch (e: any) { message.error(e.message); } finally { setPreparing(false); }
  };
  const cancel = async (id: string) => {
    setConfirming(true);
    try { await post(`/agent/actions/${id}/cancel`); setPlan(undefined); await refresh(); }
    catch (e: any) { message.error(e.message); } finally { setConfirming(false); }
  };
  const confirm = async () => {
    // 【Agent 第三步】用户在预览弹窗确认后提交；后端会重新检查权限并保证幂等。
    if (!plan) return;
    setConfirming(true);
    try {
      const submitted=await post(`/agent/actions/${plan.id}/confirm`, { confirmed: true });
      if(submitted.result?.job_id)setTracking(plan.id);
      setPlan(undefined); message.success('已提交，可在申请或任务中心查看进度'); await refresh();
      if (conversation) await loadConversation(conversation);
    } catch (e: any) { message.error(e.message); } finally { setConfirming(false); }
  };

  const renderResult = (h: Row) => <>
    <p className="agent-answer-text">{h.answer}</p>
    {!!h.execution?.steps?.length && <Card size="small" title="本次查询过程" className="agent-result-card">
      <Space wrap><Tag>{h.execution.rounds} 轮模型调用</Tag><Tag>{h.execution.tool_calls} 次工具调用</Tag>
        <Tag>{h.execution.stop_reason === 'completed' ? '完成' : '已停止'}</Tag>
        {!h.execution.usage_complete && <Tag>模型用量未完整提供</Tag>}</Space>
      <p className="muted">{h.execution.steps.filter((s: Row) => s.kind === 'tool').map((s: Row) =>
        `${s.tool} · ${s.status === 'completed' ? '成功' : '未执行或失败'}`).join(' → ') || '尚未取得工具依据'}</p>
    </Card>}
    {h.status === 'degraded' && <Alert type="warning" showIcon message="模型未完成本次回答，已保留取得的工具依据" description={h.failure_reason ? `停止原因：${h.failure_reason}` : '可使用找数据、查询任务等平台工具继续操作'} />}
    {h.execution?.steps?.some((s: Row) => s.truncated || s.has_more || s.partial_catalog) && <Alert type="info" showIcon message="本次工具结果包含截断或分页，只覆盖返回的部分数据" />}
    {h.status === 'failed' && <Alert type="warning" showIcon message="本次操作未执行，请检查所选版本和访问权限" />}
    {!!h.citations?.length && <div className="agent-citations">{h.citations.map((c: Row) => <div className="agent-citation" key={c.version_id || c.dataset_id}>
      <Link to={c.url || `/datasets/${c.dataset_id}`}><strong>{c.name}</strong> {c.number ? `v${c.number}` : '草稿'} <ArrowRightOutlined /></Link>
      <span>{labels[c.modality] || ''} · {labels[c.access_mode]}{c.license && ` · ${c.license}`}</span>
      {c.version_id && <Space wrap><Button size="small" onClick={() => {
        addChoices([{ id: c.version_id, number: c.number, name: c.name }]);
        setVersions(old => old.includes(c.version_id) ? old : [...old.slice(-3), c.version_id]);
        message.success('已加入，下次提问会携带此版本；历史回答不会自动更新');
      }} disabled={busy || !!pending || versions.includes(c.version_id)}>{versions.includes(c.version_id) ? '已加入所选版本' : '加入所选版本'}</Button>
        <Button size="small" disabled={busy || !!pending} onClick={() => {
          addChoices([{ id: c.version_id, number: c.number, name: c.name }]); setVersions([c.version_id]);
          void send(`解读《${c.name}》v${c.number}的质量报告`, 'quality', false, [c.version_id]);
        }}>解读此版本质量</Button></Space>}
    </div>)}</div>}
    {h.comparison && <Table size="small" rowKey="version_id" pagination={false} scroll={{ x: 760 }} dataSource={h.comparison} columns={[
      { title: '数据集 / 版本', render: (_: any, v: Row) => `${v.name} · v${v.number}` },
      { title: '模态', dataIndex: 'modality', render: v => labels[v] || v },
      { title: '规模', render: (_: any, v: Row) => `${v.asset_count} 个文件 / ${bytes(v.total_bytes)}` },
      { title: '许可', dataIndex: 'license' }, { title: '适用任务', dataIndex: 'task', render: x => typeof x === 'string' ? x : JSON.stringify(x) },
      { title: '划分', dataIndex: 'split', render: x => typeof x === 'string' ? x : JSON.stringify(x) },
      { title: '获取方式', dataIndex: 'access_mode', render: v => labels[v] },
    ]} />}
    {h.quality?.status === 'available' && <Card size="small" title="质量依据" className="agent-result-card">
      <Descriptions size="small" column={2} items={[
        { key: 'method', label: '检测方法', children: h.quality.model || `规则 · ${h.quality.method}` },
        { key: 'coverage', label: '文件覆盖', children: `${h.quality.checked} / ${h.quality.total}` },
        { key: 'failed', label: '失败文件', children: h.quality.failed },
        { key: 'dup', label: '重复文件', children: h.quality.duplicate_files },
        { key: 'date', label: '报告时间', children: date(h.quality.created_at) },
        { key: 'unchecked', label: '未检测', children: h.quality.not_checked?.join('、') || '见报告说明' },
      ]} />
    </Card>}
    {h.diagnosis&&<Card size="small" title="当前版本流程诊断" className="agent-result-card">
      {h.diagnosis.checks.map((item:Row)=><p key={item.code}><Tag color={item.ok===null?'default':item.ok?'green':'orange'}>{item.ok===null?'待完整验收':item.ok?'状态正常':'需处理'}</Tag>{item.detail}</p>)}
      <Space wrap>{Object.entries(h.diagnosis.links).map(([key,url])=><Link key={key} to={String(url)}>{({dataset:'数据集',reviews:'标注工作台',jobs:'处理任务',publications:'发布审批'} as Row)[key]}</Link>)}</Space>
      {h.diagnosis.limitations.map((text:string)=><p className="muted" key={text}>{text}</p>)}
    </Card>}
    {h.jobs && <><Table size="small" rowKey="id" dataSource={h.jobs} pagination={{ pageSize: 5 }} columns={[
      { title: '数据集', dataIndex: 'dataset_name' }, { title: '类型', dataIndex: 'kind', render: v => labels[v] || v },
      { title: '状态', dataIndex: 'status', render: v => <Status value={v} /> },
      { title: '进度', dataIndex: 'progress', render: v => `${v}%` },
      { title: '失败原因', dataIndex: 'error', ellipsis: true },
    ]} /><Link to="/jobs">前往任务中心刷新、取消或重试 <ArrowRightOutlined /></Link></>}
    {h.draft && <Card size="small" title={`${actionLabels[h.draft.kind]}草稿 · 尚未执行`} className="agent-result-card">
      <Form key={h.run_id} layout="vertical" initialValues={{ purpose: h.draft.purpose, duration_days: 30, target_format: undefined,image_limit:h.draft.image_limit||100,near_distance:h.draft.near_distance??6 }}
        onFinish={values => prepare(h.draft, h.draft.kind === 'application' ? { purpose: values.purpose, duration_days: values.duration_days } : h.draft.kind === 'convert' ? { target_format: values.target_format } : h.draft.kind==='vision'?{image_limit:values.image_limit}:h.draft.kind==='dedup'?{near_distance:values.near_distance}:{})}>
        {h.draft.kind === 'application' && <>
          <Form.Item label="申请用途（请补充真实用途）" name="purpose" rules={[{ required: true, min: 5, max: 5000 }]}><Input.TextArea rows={3} maxLength={5000} /></Form.Item>
          <Form.Item label="申请期限（天）" name="duration_days" rules={[{ required: true }]}><InputNumber min={1} max={365} /></Form.Item>
          <p className="muted">以当前个人身份提交。项目申请可在数据集详情页办理。</p>
        </>}
        {h.draft.kind === 'convert' && <Form.Item label="目标格式" name="target_format" rules={[{ required: true }]}><Select placeholder="选择与输入文件匹配的格式" options={['csv', 'json', 'png', 'jpg', 'webp'].map(v => ({ value: v, label: v.toUpperCase() }))} /></Form.Item>}
        {h.draft.kind==='vision'&&<Form.Item name="image_limit" label="本次最多识别图片数（1–1000）" rules={[{required:true}]}><InputNumber min={1} max={1000}/></Form.Item>}
        {h.draft.kind==='dedup'&&<Form.Item name="near_distance" label="近重复距离（越小越严格）" rules={[{required:true}]}><InputNumber min={0} max={16}/></Form.Item>}
        <Button htmlType="submit" loading={preparing} disabled={busy}>查看执行预览</Button>
      </Form>
    </Card>}
    {h.result?.url && <Link to={h.result.url}>查看提交结果 <ArrowRightOutlined /></Link>}
    <div className="agent-trace"><ClockCircleOutlined /> {h.mode === 'offline-tools' || h.mode === 'catalog-search' ? '平台工具' : h.mode} · {h.duration_ms} ms{h.usage?.total_tokens != null && ` · ${h.usage.total_tokens} tokens`}{h.run_id && ` · 记录 ${h.run_id.slice(0, 8)}`}</div>
  </>;

  return <>
    <PageTitle title="数据工作助手" subtitle="找数据、看质量、准备申请，让每一步都落到具体版本。" extra={<Space><Tag color={cap?.model_enabled ? 'green' : 'default'}>{cap?.model_enabled ? `模型辅助 · ${cap.model_name}` : '离线工具模式'}</Tag><Link to="/credentials"><Button>外部 Agent 接入</Button></Link></Space>} />
    <Tabs defaultActiveKey="chat" items={[
      { key: 'chat', label: '数据助手', children: <div className="agent-workspace">
        <Card className="agent-sessions" size="small" title="我的对话" extra={<Button aria-label="新建对话" type="text" icon={<PlusOutlined />} disabled={busy} onClick={() => { setConversation(undefined); setHistory([]); setVersions([]); setInput(''); }} />}>
          {!conversations.length && <p className="muted">发送消息后自动保存</p>}
          {conversations.map(c => <button key={c.id} disabled={busy} className={`agent-session ${conversation === c.id ? 'active' : ''}`} onClick={() => loadConversation(c.id)}><strong>{c.title}</strong><small>{date(c.updated_at)}</small></button>)}
          {conversation&&<Popconfirm title="清除当前对话内容及未确认草稿？" description="已执行任务、审批和去重回执保留；不会重复执行。" onConfirm={async()=>{try{await post(`/agent/conversations/${conversation}/clear`);setHistory([]);setPins([]);setVersions([]);setRequest();await refresh();message.success('对话内容已清除');}catch(e:any){message.error(e.message);}}}><Button danger disabled={busy}>清除当前对话内容</Button></Popconfirm>}
          {conversation&&pins.length>0&&<div><strong>固定的上下文线索</strong>{pins.map(pin=><p key={pin.id}>{pin.outputs.redacted?'权限已变化':pin.inputs.message?.slice(0,40)} <Button size="small" onClick={async()=>{try{await post(`/agent/conversations/${conversation}/runs/${pin.id}/pin`,{pinned:false});setPins(old=>old.filter(row=>row.id!==pin.id));setHistory(old=>old.map(row=>row.run_id===pin.id?{...row,memory_pinned:false}:row));}catch(e:any){message.error(e.message);}}}>取消</Button></p>)}</div>}
          <div className="agent-capability-note"><RobotOutlined /><p>先选版本，再解读、对比或加工。</p><p>申请与处理任务会先展示预览，确认后执行。</p><p>无模型时可使用关键词检索和全部业务工具。</p></div>
        </Card>
        <Card className="agent-chat-panel">
          {pending && <Alert type="warning" showIcon message="上次请求的结果尚未确认" description="重试将使用原问题、原版本和原请求编号；服务器已完成时会返回已保存结果。放弃重试不会取消后台查询。" action={<Space wrap><Button disabled={busy} onClick={checkPending}>检查已保存结果</Button><Button disabled={busy} onClick={() => send(pending.message, pending.intent, true)}>重试原请求</Button><Button disabled={busy} onClick={() => setRequest()}>放弃重试</Button></Space>} />}
          <div className="agent-context"><strong>所选数据版本</strong><div className="agent-context-selectors">
            <Select aria-label="选择数据集" placeholder="先选择数据集加载版本" disabled={busy} showSearch optionFilterProp="label" value={undefined} onChange={chooseDataset} options={catalog.map(d => ({ value: d.id, label: d.name }))} />
            <Select aria-label="选择版本" mode="multiple" maxCount={4} disabled={busy} placeholder="选择 1 个版本操作，2–4 个版本对比" value={versions} onChange={setVersions} showSearch optionFilterProp="label" options={choices.map(v => ({ value: v.id, label: `${v.name} · v${v.number}` }))} />
          </div></div>
          {tracking&&<AgentTaskProgress actionId={tracking}/>}
          <div className="agent-messages" aria-live="polite" ref={messageList}>
            {!history.length && <div className="assistant-welcome agent-welcome"><div className="assistant-logo"><RobotOutlined /></div><h2>从数据需求，到下一步行动</h2><p>在当前权限内检索资料，用具体版本支持每一次判断。</p><div className="prompt-grid">{[
              ['search', '有哪些可申请的数据集？'], ['compare', '对比所选数据版本'], ['quality', '解读所选版本的质量报告'],
              ['application', '帮我起草数据使用申请'], ['jobs', '查看我的任务进度'], ['quality_job', '为所选版本运行质量检查'],
              ['diagnose','这个版本下一步做什么？'],['vision','为所选版本安排图像识别'],['dedup','为所选版本发起重复检测'],
            ].map(([mode, text]) => <button disabled={busy || !cap || !!pending} key={mode} onClick={() => send(text, mode)}>{text}<ArrowRightOutlined /></button>)}</div></div>}
            {history.map((h, i) => <div className="chat-turn" key={h.run_id || i}><div className="question">{h.question}</div>{conversation&&h.run_id&&!h.draft&&!h.redacted&&(!h.source||h.source==='assistant')&&(!h.tool||['agent','search','compare','quality','jobs'].includes(h.tool))&&['completed','degraded'].includes(h.status)&&<Button size="small" disabled={busy} onClick={async()=>{try{await post(`/agent/conversations/${conversation}/runs/${h.run_id}/pin`,{pinned:!h.memory_pinned});setHistory(old=>old.map(row=>row.run_id===h.run_id?{...row,memory_pinned:!h.memory_pinned}:row));setPins(await api(`/agent/conversations/${conversation}/pins`));}catch(e:any){message.error(e.message);}}}>{h.memory_pinned?'取消固定线索':'固定上下文线索'}</Button>}<div className="answer"><RobotOutlined /><div className="agent-answer-body">{renderResult(h)}</div></div></div>)}
            {busy && <div className="agent-thinking"><Spin size="small" /> 正在读取平台数据…</div>}
          </div>
          <div className="agent-composer"><Select aria-label="助手操作类型" value={intent} options={intents.filter(i => i.value !== 'agent' || cap?.agent_tools_enabled)} onChange={setIntent} disabled={busy} />
            <Input.TextArea aria-label="给数据助手发消息" placeholder="例如：找问答语料，或先选择版本，再输入“解读质量报告”…" value={input} onChange={e => setInput(e.target.value)} maxLength={2000} autoSize={{ minRows: 2, maxRows: 5 }} onPressEnter={e => { if (!e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); } }} />
            <Button type="primary" loading={busy} icon={<SendOutlined />} disabled={!cap || !input.trim() || !!pending} onClick={() => send()}>发送</Button>
          </div>
          <p className="agent-composer-hint">Enter 发送 · Shift + Enter 换行 · 申请和加工必须经你确认</p>
        </Card>
      </div> },
      { key: 'actions', label: `操作记录${actions.some(a => a.status === 'pending') ? ' · 有待确认' : ''}`, children: <Card title="确认与执行记录" extra={<Button icon={<ReloadOutlined />} onClick={() => refresh().catch(e => message.error(e.message))}>刷新</Button>}>
        <Table rowKey="id" dataSource={actions} locale={{ emptyText: <Empty description="尚未准备操作。可在对话里起草申请或创建处理任务。" /> }} columns={[
          { title: '操作', dataIndex: 'kind', render: x => actionLabels[x] },
          { title: '输入版本', render: (_: any, r: Row) => `${r.summary.dataset_name} · v${r.summary.version_number}` },
          { title: '状态', dataIndex: 'status', render: x => <Tag>{actionStatus[x] || x}</Tag> },
          { title: '准备时间', dataIndex: 'created_at', render: date },
          { title: '操作', render: (_: any, r: Row) => r.status === 'pending' ? <Space><Button onClick={() => setPlan(r)}>查看并确认</Button><Button danger onClick={() => cancel(r.id)} loading={confirming}>取消</Button></Space> : r.result.url ? <Link to={r.result.url}>查看结果</Link> : '—' },
        ]} />
      </Card> },
      { key: 'runs', label: '运行追踪', children: <Card title="我的 Agent 调用" extra={<Button icon={<ReloadOutlined />} onClick={() => refresh().catch(e => message.error(e.message))}>刷新</Button>}><Table rowKey="id" dataSource={runs} columns={[
        { title: '工具', dataIndex: 'tool', render: x => intents.find(i => i.value === x)?.label || actionLabels[x] || x },
        { title: '来源', dataIndex: 'source' }, { title: '执行模式', dataIndex: 'model' },
        { title: '状态', dataIndex: 'status', render: v => <Tag>{v === 'degraded' ? '已降级' : labels[v] || v}</Tag> },
        { title: '耗时', dataIndex: 'duration_ms', render: x => `${x} ms` },
        { title: 'Token 用量', render: (_: any, r: Row) => r.outputs.usage?.total_tokens ?? '—' },
        { title: '时间', dataIndex: 'created_at', render: date },
      ]} expandable={{ expandedRowRender: r => <><p>记录 ID：{r.id}</p><p>{r.outputs.answer || '只读工具调用'}</p>{r.outputs.failure_reason && <p>失败原因：{r.outputs.failure_reason}</p>}
        {!!r.outputs.execution?.steps?.length && <Table size="small" rowKey="span_id" pagination={false} dataSource={r.outputs.execution.steps} columns={[
          { title: '步骤', dataIndex: 'kind' }, { title: '工具', dataIndex: 'tool' },
          { title: '状态', dataIndex: 'status' }, { title: '耗时', dataIndex: 'duration_ms', render: x => `${x} ms` },
          { title: '原因', dataIndex: 'error' },
        ]} />}</> }} /></Card> },
    ]} />
    <Modal title={`确认${plan ? actionLabels[plan.kind] : ''}`} open={!!plan} onCancel={() => !confirming && setPlan(undefined)} maskClosable={!confirming} confirmLoading={confirming} onOk={confirm} okText="确认提交" cancelText="稍后处理" width={650}>
      {plan && <><Alert type="info" showIcon message="确认后将使用当前身份提交，执行时再次检查版本权限。" /><Descriptions className="agent-plan" column={1} items={[
        { key: 'input', label: '输入版本', children: `${plan.summary.dataset_name} · v${plan.summary.version_number}` },
        { key: 'license', label: '使用许可', children: plan.summary.license },
        { key: 'output', label: '执行结果', children: plan.summary.output },
        { key: 'scale', label: '读取规模', children: `${plan.summary.input_files} 个文件 / ${bytes(plan.summary.input_bytes)}` },
        { key: 'resource', label: '资源预估', children: plan.summary.resource_estimate },
        ...(plan.kind === 'application' ? [{ key: 'purpose', label: '申请用途', children: plan.payload.purpose }, { key: 'duration', label: '申请期限', children: `${plan.payload.duration_days} 天` }] : []),
        ...(plan.payload.target_format ? [{ key: 'format', label: '目标格式', children: plan.payload.target_format.toUpperCase() }] : []),
        { key: 'expires', label: '确认有效期至', children: date(plan.expires_at) },
      ]} />{plan.summary.workflow&&<WorkflowPlan value={plan.summary.workflow}/>}<Button danger disabled={confirming} onClick={() => cancel(plan.id)}>取消此操作</Button></>}
    </Modal>
  </>;
}
