# BM25与只读Agent证据检查

后续更新：2026-11-06 已接入[本地 Schema 决策协议](agent-local-model-protocol.md)，
通过原十项及十项留出场景的真实模型门禁并部署启用。下文 v5 的 0/10 与关闭状态属于历史阶段。

2026-11-05。将文本召回从简单关键词加权扩展为真正的BM25，同时强化只读工具循环。
没有赋予模型发布、授权审批、任意SQL、路径读取或无确认加工能力。

## BM25在哪实现

`backend/app/bm25.py`：

- tokenize：NFKC规范化，小写英文/标识符按词，中文按字符二元词；分词版本固定。
- field_scores：计算授权语料内DF、IDF、平均长度、词频饱和和长度归一化。
- rank：标题、正文分别计算BM25，再以3:1加权，按分数和稳定ID排序。
- 默认k1=1.2、b=0.75，分数不是匹配概率，不宣称与Elasticsearch分词/实现逐分一致。

`retrieval_service.search` 先由数据库查询当前身份可见的知识/版本检索单元，
再在完整的有界授权语料里计算统计量；不是先SQL截取120个候选再伪装成全库BM25。
BM25和向量分别召回后用已有RRF融合，可选重排；返回前继续检查权限、来源指纹和业务条件。
索引未覆盖时仍有实时目录词法补足，其match_basis与索引结果区分。

配置：

- DP_RETRIEVAL_LEXICAL_MODE=bm25（默认），keyword保留旧实现便于对照/回退。
- DP_RETRIEVAL_BM25_MAX_ENTRIES=5000。
- DP_RETRIEVAL_BM25_MAX_BYTES=16777216。

超预算明确返回413并要求缩小组织/版本范围，不静默只处理前N个文件。
本版为有界CPU语料评分，不是持久倒排引擎、分布式搜索服务或PB级方案。
语料显著增长时应评测独立倒排索引或向量服务的本地稀疏检索适配，不能无限调高预算。
BM25不依赖聊天模型；向量服务不可用时仍可返回授权词法依据。
元数据提及、模型预测、人工复核标签仍是不同证据层，不因换算法而混用。

## Agent新增了什么

`agent_runtime.run_loop_async` 仍按model → tool → model → validation执行：

- 同工具、同规范化参数在单次运行中最多执行两次，继续重复以repeated_tool_call停止。
- 文件数、许可、规模、版本详情必须实际成功读取get_version；版本比较需compare_versions；
  质量与准确率需get_quality。仅搜索目录就猜测文件数不能标作成功。
- 新停止原因insufficient_tool_evidence与已有JSON不合规、未知引用、越权等分开记录。
- 原有时间、轮数、工具次数、上下文字节及Token预算继续生效。
- Prompt/行为版本升级dataset-read-agent-v5-evidence-contract，旧模型启用证据不能复用。

这是有界只读调查器，不是无人审批、任意执行的“完全自主”系统。
此次BM25接入的是检索工作台、知识问答和语义检索分支；原生工具循环的目录工具
仍为其现有业务接口，不能把两个入口误称为已经完全融合的自主RAG Agent。

## 实模型验收结果如何解释

对当前本地Qwen3-VL-4B运行十个合成工具场景，仅发送合成元数据，不访问业务库或图片。
v5严格门禁本轮0/10通过，主要是最终JSON不合规或没有读取回答所需工具。
这不是通用模型基准，也不是其视觉识别准确率；已有单轮RAG与视觉调用可用，
不代表多轮自主工具选择也可靠。不能修改期望事实、绕过门禁或增加权限来“修成通过”。
因此保留DP_AGENT_TOOLS_ENABLED=false。下一步需要改进模型适配/选择、结构化输出和
任务评测，再以真实证据启用；本轮没有把自主工具循环强行上线。

## 测试与学习

- test_bm25：中文/英文/标识符分词、文档长度归一化、词频饱和、标题权重、
  无匹配、权限隔离及向量故障时的BM25回退。
- test_agent_runtime：重复调用保护、仅目录搜索不能支撑文件数、原有权限/预算/引用。
- 原检索与会话记忆测试继续验证组合标签、否定、重复确认和历史权限。
- 实模型报告保存在本机忽略的部署操作目录，未加入公开源码。

面试可以说“实现字段加权BM25与向量融合，以及受预算和证据约束的只读Agent运行时”。
不能说“已启用完全自主生产Agent”或“BM25使召回率提升X%”；效果收益须另做相关性标注评测。

## 当前部署记录

已通过54项专项/回归、Ruff与仓库审计；真实编码器/Qdrant/LM Studio在临时语料上
完成BM25融合、RAG、追问与权限联调。已同步部署业务镜像，HTTPS健康；数据库版本
保持d91，账号与资产数量不变。运行配置确认lexical_method=bm25、agent_tools_enabled=false。
没有自动修改标签、发布数据或重新处理全部历史图片。

参考：[BM25参数](https://www.elastic.co/docs/reference/elasticsearch/index-settings/similarity)、
[工作流与Agent的区别](https://www.anthropic.com/research/building-effective-agents)。
