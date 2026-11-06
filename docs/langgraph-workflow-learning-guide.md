# LangGraph 可恢复治理流程：实现与学习指南

本次把真实 LangGraph `StateGraph` 接入现有助手的人工确认操作，覆盖使用申请、质量检查、转换、打包、视觉预标注和去重任务。
业务入口继续生成结构化草稿和执行预览，图负责确认等待、恢复提交及结果汇总。图片识别仍由视觉 Worker 调用配置的模型完成。

这是固定业务工作流。模型自主选择工具的 `agent_runtime` 是独立模块，其实模型启用门禁继续保留；接入 LangGraph 不会自动启用它。
RAG、BM25、图片相似检索继续通过原来的检索服务运行，本轮没有把它们全部改造成自主规划节点。

## 一、用户会看到什么

1. 在数据工作助手选择版本，输入“对这个版本做图像识别，先做 100 张”，或选择去重、质量等意图。
2. 检查草稿参数，点击“查看执行预览”。后台先保存 `AgentAction`，图在人工确认节点暂停；此时不创建业务任务。
3. 弹窗增加“可恢复的处理流程”，显示等待确认。刷新页面后，可在“操作记录”重新打开同一份预览。
4. 点击“确认提交”。后端用当前账号重新检查授权、输入、模板和模型指纹，创建唯一的任务回执。
5. Celery/视觉 Worker 继续后台处理。关闭网页不会取消任务；重新进入可以看到当前任务状态。
6. 图等待期间不占用数据库连接。网页收到任务结束快照后自动提交一次结果汇总请求；也可以展开操作记录，点击“更新流程结果”。
7. 遇到可恢复的流程中断，使用“恢复流程”。未确认的计划仍须本人确认，恢复按钮不能替代确认。

批量视觉任务仍有现有分片与容量上限；LangGraph 不负责提高图片识别准确率，也不取消独立人工复核。

## 二、完整调用顺序

预览：`AgentPage.prepare → POST /agent/actions → prepare_action → agent_graph.register → drive(start)`。

图内节点：`START → plan → approval → submit → wait_task → END`。使用申请提交后可直接结束；
任务未完成时，`wait_task` 通过 `interrupt` 暂停；成功后返回真实结果。任务失败或取消时保留等待，
由用户在任务中心重试后继续跟踪同一任务；刷新流程不会替用户重试或重新提交任务。

确认：`POST /agent/actions/{id}/confirm → drive(confirm) → Command(resume={approved: true}) → submit → execute_action`。

任务执行：`execute_action(dispatch_task=False)` 只提交业务事务；图释放执行租约之后，才调用现有的 `dispatch` 或 `start_vision`。
任务发布使用原有的持久化派发与恢复机制。图的检查点和业务记录分属短事务，靠确认单 ID 和业务幂等键处理跨事务故障。

恢复：`POST /agent/actions/{id}/resume` 只接收 `recover/refresh`，后端读取检查点并选择 `invoke(None)` 或刷新任务等待的 `Command`。
客户端不能提交 `approved`、任意图状态、节点名称或检查点 ID。

## 三、逐文件与方法

`backend/app/agent_graph.py`：

- `WorkflowState`：图的状态契约，只包括确认单 ID、版本 ID、计划摘要哈希、确认状态、阶段和任务状态。
- `register`：在保存预览的同一事务中建立图回执，并按用户对应的配额哨兵锁检查有效待确认数量，避免升级 User 外键共享锁造成死锁。`thread_id` 由服务端确认单 ID 派生。
- `receipt/public_action`：给前端输出引擎、版本、阶段和是否可恢复；不返回租约凭据和完整检查点。
- `_authorize`：每个节点及状态查询重新认证账号、数据可见性、内容授权和负责人身份，并核对流程归属。
- `plan_hash`：把原确认单的参数和预览绑定到图，图内不复制申请用途、完整文件清单或原文。
- `build_graph`：真正构造四个节点和条件边。`approval` 在任何任务创建前调用 `interrupt`；`submit` 复用业务事务；`wait_task` 查询真实任务。
- `drive`：取得执行租约、装配持久化图、提交受控恢复命令、压缩检查点历史、释放租约，再派发已提交任务。
- `invalidate`：取消或清除对话时，在同一事务中使图失效并清理检查点。
- `workflow_status`：只读返回流程摘要及当前任务快照；GET 不驱动图、不写检查点。

`backend/app/agent_graph_store.py`：

- `SQLGraphSaver`：实现 LangGraph `BaseCheckpointSaver` 的同步持久化接口，使用平台已有 SQLAlchemy/MySQL。
- `acquire/guard/release`：以数据库 CAS、随机令牌和到期时间协调多个 API 进程。每次写入重新检查令牌，旧进程不能覆盖新进程的检查点。
- `get_tuple`：读取最新完整状态及按 `task_path/task_id/write_index` 排序的节点待提交写入，返回 `CheckpointTuple`。
- `put`：保存完整状态、元信息和父检查点引用，检查单行体积及数量。
- `put_writes`：保存节点结果以及中断/恢复回执。普通节点写入保留首次结果，框架保留通道按约定更新。
- `prune_history`：保留最新 8 个完整快照及对应写入，避免每次刷新无限累积历史。
- `pack/unpack/SafeGraphSerializer`：使用封闭 JSON 类型集合，仅支持基本类型、元组及显式 `Interrupt`。不使用 pickle、动态模块导入或任意类构造。

每个检查点方法独立创建并关闭短会话，适配 LangGraph 后台写入线程。不会把请求线程的 `Session` 交给另一个线程。
当前图只使用完整 `LastValue` 状态，不支持 `DeltaChannel`、子图命名空间、时间旅行或任意通用图；以后扩展这些能力必须同步修改保存与裁剪规则。

`backend/app/agent_graph_api.py`：鉴权状态入口，以及有账号/进程并发准入保护的恢复入口。同步图运行放在线程池中执行，避免阻塞 FastAPI 事件循环。

`backend/app/assistant.py`：预览后注册图；有图的确认单通过图继续执行；原业务确认函数增加 `dispatch_task` 参数以划分事务与派发边界。
新图的普通质量/转换/打包预览也冻结文件摘要与版本元数据；确认时在版本锁内复核，新增文件后必须重新预览。
已有 legacy 确认单继续走原逻辑，不自动迁移历史操作。配置切回 legacy 只影响新计划，已存在的图仍按自身回执处理。

`backend/app/agent_retention.py`：清理会话或到期内容时，一并删除检查点中的内容，并撤销未确认计划的恢复能力。

`frontend/src/agent-workflow.tsx` 和 `pages/agent.tsx`：预览步骤、恢复按钮、操作记录展开，以及任务结束后的一次结果汇总。
后台业务任务进度仍复用原 SSE 快照，不能将其称为 LangGraph token 流或完整历史事件回放。

## 四、数据库、故障与体积边界

新增迁移 `e24b7c91d602`，父版本 `d91e7a62b430`。应用表从 45 增至 48：

- `agent_graph_workflows`：一份确认单对应一条图回执，记录图版本、阶段和租约。
- `agent_graph_checkpoints`：完整状态、父引用、框架元信息，单个状态不超过 32 KiB。
- `agent_graph_writes`：节点写入及 interrupt/resume 信息，每项不超过 8 KiB。

单次执行最多临时累积 64 个检查点、256 条待提交写入，正常完成一轮后只保留最新 8 个检查点及关联写入；
默认每人最多 20 份有效待确认图。与现有内容保留周期一起控制总量，不能无限提高这些预算。
不在检查点中复制完整图片清单，进度 GET 不反复写状态，避免复现大 JSON 热更新导致的 binlog 放大。

业务提交与图检查点之间存在故障窗口：任务创建成功后若尚未保存节点结果，恢复会再次进入 `submit`，
复用 `AgentAction` 状态、CAS 和 `agent-{action_id}` 幂等键取得原业务回执。不能把框架检查点误认为跨数据库、Redis 的全局事务。

运行进程崩溃后，活跃租约须等到到期才能由新进程取得；默认 120 秒。每个节点写入会续租。
图暂停时立即释放租约；不会一直占锁等待用户或模型任务。断线、超时后应先查询原确认单状态。

备份格式升级为 v13。旧 d91/v12 可只读备份并升级到新格式。
恢复备份时保留任务/申请的业务回执，但使图失效、清空图检查点，并取消尚未提交的图确认单，避免恢复旧批准后自动执行。
普通重启恢复保留检查点；“从备份恢复”采用更严格的失效策略，两者不同。

## 五、配置和本地体验

公开克隆默认 `DP_AGENT_WORKFLOW_ENGINE=legacy`，避免在未迁移的数据库上启用新表。
新空库或经审阅升级到 e24 后，使用 `config/examples/langgraph.env.example` 中的：

```dotenv
DP_AGENT_WORKFLOW_ENGINE=langgraph
DP_AGENT_GRAPH_LEASE_SECONDS=120
DP_AGENT_GRAPH_MAX_PENDING=20
DP_AGENT_TOOLS_ENABLED=false
```

不需要新增模型、向量库、LangGraph Server、付费 LangSmith 或单独的 PostgreSQL。
LangGraph 依赖自带 `langchain-core` 等组件，未引入完整 LangChain Agent 来重写原业务接口；
图调用显式设置 `tracing_context(enabled=False)`，即使宿主机存在追踪变量，也不上传业务追踪。

在全新克隆、已安装哈希锁依赖并完成前端构建后，可使用独立学习环境：

```bash
python scripts/start_public_sandbox.py --langgraph
```

该命令每次创建新的空学习库，不读取现有部署文件，不连接维护者服务。
自行注册、上传少量测试图片，然后用“去重计划”即可体验无模型的确认、任务处理和结果汇总。
学习环境的视觉模型默认关闭；真实视觉预标注仍须单独配置并验证模型。
现有业务实例上线需要加密备份、排空任务、数据库迁移及同步更新 API/Worker/Web；不要直接对已有业务库运行学习命令或自动升级。

## 六、验证和面试边界

三个容易混淆的概念：`conversation_id` 标识聊天；`thread_id` 在这里标识一份确认单的执行流程；
`query_memory` 保存检索需求。一个聊天可以产生多份独立计划，检查点也不等于聊天记录或长期记忆。

三个时间限制同样独立：确认单默认 15 分钟有效，仅约束尚未提交的操作；图执行租约默认 120 秒，用于崩溃后的进程接管；
后台图片任务有自己的执行与重试预算。已经提交的任务不会因为预览到期而取消。

`backend/tests/test_agent_langgraph.py` 使用真实 LangGraph、隔离 SQLite 和合成图片，覆盖暂停、确认、跨实例恢复、
提交后崩溃、并发确认、租约隔离、权限/内容变化、过期、清除内容、检查点预算、备份失效及迁移。
`test_agent_langgraph_mysql.py` 是显式隔离库的 MySQL 专项，拒绝业务数据库名称和账号。
前端浏览器测试验证预览、确认、恢复操作及进度展示，使用合成 API 和独立浏览器。

可以表述“使用 LangGraph 构建人工确认治理流程，通过 SQL 检查点、租约隔离和业务幂等支持中断恢复”。
不能据此声称多 Agent 自主协作、自动批准数据发布、检索效果提升、海量吞吐或实模型工具规划已经验收。

2026-11-06 本轮验证结果：

- 全量后端/脚本隔离回归共 515 项：469 通过、46 按外部服务隔离策略跳过、0 失败。
- 独立原生 MySQL 8.0.26 另行通过 6 项，覆盖并发确认、跨实例恢复、提交后崩溃、租约隔离、跨版本配额及任务重试。
- 编译后的前端与合成 API 通过 5 项独立浏览器测试；前端构建和 Ruff 检查通过。
- 101 项锁定依赖已补充 1,896 个官方文件哈希，检查时无未撤回的 PyPI 已公告漏洞；这不等于没有未知漏洞。
- 仓库审计及干净公开源码导出检查通过；没有在本轮推送 GitHub。

以上是源码阶段的隔离验证记录，没有使用业务图片或模型评测数据。
后续获得明确部署授权后，已完成备份读回、旧表一致性检查、e24 迁移及主实例部署，
并开启 LangGraph 和经实模型门禁验证的 Schema 自主只读工具协议。公开克隆仍默认 legacy，需自行配置；
固定人工确认流程与模型自主工具门禁保持独立。

参考：[StateGraph](https://docs.langchain.com/oss/python/langgraph/overview)、
[Interrupt 与 Command](https://docs.langchain.com/oss/python/langgraph/interrupts)、
[持久化](https://docs.langchain.com/oss/python/langgraph/persistence)。
