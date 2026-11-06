# 本地模型自主工具决策：Schema 协议与真实门禁

本轮没有把门禁改成直接通过。保留原生工具协议，同时增加 `DP_AGENT_PROTOCOL=json-schema`。
模型仍逐步决定调用哪个工具以及参数；后端把有效 JSON 决策转换为现有工具执行契约。
写操作继续走人工确认的 LangGraph 流程，模型没有发布、批准申请或修改标注的权限。

`agent_protocol.request_payload` 把工具清单、参数 Schema 和字段含义提供给模型，
用 LM Studio 的 `response_format.json_schema` 约束单步 `tool/answer` 决策。
已经明确选择、用户明确提供或当前工具返回的版本 ID 被用于参数枚举，减少本地模型抄错长 ID。
枚举仅减少格式错误，具体版本能否访问仍由业务工具逐次鉴权。
历史聊天和描述中的字符串不会被当作当前权威版本来源。

`normalize_response` 只翻译严格匹配的决策，不补造工具、参数或事实。
后续继续进入 `agent_runtime` 的参数校验、scope、身份复核、重复调用限制、预算及证据检查。
`version_id` 是资源标识，`number` 是版本号，`asset_count` 是文件数；质量字段和未检测项有明确含义。

`scripts/evaluate_agent.py --live --heldout-seed <整数>` 同时运行原十项和额外十项留出场景。
留出场景改变版本 ID、版本号、文件数、许可和检查数字，问题中不给出这些事实的正确值。
评测仍用真实模型和合成业务工具，不读取业务数据库、图片或客户文件。
JSON 协议上线须二十项全部通过；不能只提交原十项报告。

`agent_release.validate` 绑定模型名称、实际权重版本、协议、提示版本、报告 SHA-256、场景及有效期。
上线报告以只读文件挂载，配置只保存路径和摘要；公开仓库不包含本机部署文件和模型权重。
报告有效期十四天，更新模型、协议或提示版本后需要重新评测并更新报告。

本轮在当前 Qwen3-VL-4B 本地权重上复现原生协议原十项未通过；
Schema 决策协议在原十项与留出十项上取得通过结果。它说明这些工具契约场景可用，
不等于视觉准确率、任意任务正确率、提示注入完整防护或容量认证。

学习顺序：`agent_protocol → agent_runtime → agent_tools/invoke_tool → assistant_tools`，
再对照 `agent_eval_cases.heldout/grade → evaluate_agent.live → agent_release.validate`。
协议失败会停止并保留实际取得的依据，不会把错误结果当作成功，也不会在后台扩大权限。

参考：[LM Studio Structured Output](https://lmstudio.ai/docs/developer/openai-compat/structured-output)。
