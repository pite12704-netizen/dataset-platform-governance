# 项目阅读与演示路线

面向图像训练数据生产：接收文件，检查和去重，生成模型候选，人工标注与独立复核，最后发布及授权交付。
数据工作助手是同一平台的业务模块，复用账号、数据版本、授权和任务服务。

## 阅读方向一：后端与数据治理

从 [架构与一致性](public/architecture.md) 开始，然后任选一条链路：

- 上传一致性：`uploads.py/imports.py → storage.py → tasks.py`。SQL 与对象存储没有共享事务，检查隔离状态、配额预留、SHA-256、完成/撤销和补偿清理。
- 任务恢复：`tasks.py → dispatching.py → vision_plans.py → job_payloads.py`。区分派发、执行领取与业务幂等；大清单不放在反复更新的心跳行。
- 多人协作：`review_workbench.py → image_completeness.py → joint_review.py`。领取租约、revision、整图检查、去重候选和独立复核分别管理。
- 授权交付：`policy.py → workflows.py → quality_contract.py → delivery.py → downloads.py`。发布、授权和下载各自校验，产物绑定明确版本。

对应回归入口是 [backend/tests](../backend/tests)：查看 `test_agent_langgraph.py`、`test_agent_workflows.py`、
`test_joint_review.py`、`test_lifecycle.py` 和名称带 `mysql` 的隔离专项。
MySQL 专项默认跳过，须明确连接专用测试库，不能用客户业务库运行。

## 阅读方向二：Agent、LangGraph 与检索

- [助手入口](../backend/app/assistant.py)：意图、版本上下文、操作预览与确认。
- [工具运行时](../backend/app/agent_runtime.py)：模型选择只读工具，后端按白名单、参数、授权、证据与预算执行。
- [Schema 协议](agent-local-model-protocol.md)：适配本地模型的单步决策；协议约束与真实模型能力是不同问题。
- [LangGraph 流程](langgraph-workflow-learning-guide.md)：状态图、人工中断、SQL 检查点、租约隔离及提交后故障恢复。
- [检索链路](retrieval-learning-guide.md)：事务 outbox、BGE-M3/Chinese-CLIP、Qdrant、BM25、RRF、引用与重新鉴权。
- [会话需求摘要](agent-automatic-query-memory.md)：持续增删检索条件，旧聊天不充当当前授权或权威事实。

图负责业务步骤与暂停恢复，Celery 负责后台计算。模型自主只读调查器和人工确认的写操作图保持独立。
当前没有通用任意代码工具、数据库直连工具、自动发布审批、多 Agent 团队或训练托管能力。

## 无外部服务的演示

按 README 安装锁定依赖并构建前端后，运行：

```bash
python scripts/start_public_sandbox.py --langgraph
```

Windows 安装在虚拟环境时，命令中的 Python 为 `.venv\Scripts\python.exe`；
Linux/macOS 为 `.venv/bin/python`。打开启动输出中的本地地址。
每次启动分配新的空库和本地对象目录，不读取维护者部署配置，不创建默认管理员。

1. 注册一个新账号并登录；学习环境关闭邮件验证，没有真实 SMTP。
2. 在个人空间创建一个“文本”数据集和草稿版本，来源填写“自有合成演示”，许可填写 `test-only`。
3. 新建 UTF-8 文件 `sample.csv`，内容如下；上传到该版本，等待文件检查完成。

```csv
question,answer
hello,world
sample,synthetic
```

4. 打开数据工作助手，选择该数据版本；选择“运行质量检查”，发送请求。
5. 检查草稿并点击“查看执行预览”。可恢复流程应处于等待确认，尚未创建该质量任务。
6. 刷新页面，从“操作记录”重新打开预览并确认。任务执行完成后查看报告与流程结果。
7. 重复确认应返回原任务回执。关闭网页只会断开显示连接，不能撤销已提交的后台任务。

这是隔离功能演示，不用于外网生产服务。基础学习环境用 SQLite/本地存储，真实部署使用 MySQL/私有对象存储及 Worker。
图像预标注、语义向量检索和自主工具调查需要另外配置模型/服务；缺少模型时不应把规则或演示结果标成模型结果。

## 核对实现与验证边界

执行 `bash scripts/check-cloud.sh` 可跑隔离检查；Windows 可使用同等的 pytest、Ruff 和 npm 命令。
GitHub Actions 的绿色状态仅证明该提交所配置的检查通过。
实模型门禁使用合成工具及留出事实，不能推导真实图片准确率；配置上限也不能推导生产容量。

本仓库公开的是实现、迁移、测试、配置模板和学习资料。
真实账号、邮件配置、数据库备份、客户文件、数据集原图、模型权重及本机维护记录均不随源码发布。
简历若分两段介绍，请将第二段明确写为“平台 Agent 子模块”，共用这个仓库链接。
