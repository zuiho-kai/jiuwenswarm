# Native 最简 APPEND / INTERRUPT

仅增加快模型路由和原 SDK 的执行对接，默认关闭。

```yaml
duplex_router:
  mode: active
  policy: model
  model_name: fast-model
```

快模型总期限默认沿用该模型的 SDK timeout，可显式设置 `timeout_seconds`。

## 临时测试：禁用快照过期拦截（2026-10-05）

`common/duplex_router.py` 中 `SNAPSHOT_FRESHNESS_CHECK_ENABLED = False` 暂时关闭
模型返回后的快照一致性检查及打断执行前的版本一致性检查，适用于所有监工后端。
旧检查代码保留；改回 `True` 并重启服务即可恢复。日志记录
`duplex freshness check bypassed stage=observation/commit`。
模型结果仍经过响应验证和概率阈值处理，超时、错误仍回退原 steer。
执行器仍保留运行状态、快照存在、待执行打断、消息去重及安全暂停检查。
此测试模式可能把旧状态下的判断应用于已经变化的计划，不宜作为生产默认策略。
下文关于过期回退的说明仅在恢复该开关后适用。

## Jev 监工后端

### 桌面 / CMD 日志（2026-10-06）

监工日志统一保存在用户工作区 `agent/.logs/duplex.log`，本机路径为
`C:/Users/yzdnh/.jiuwenswarm/agent/.logs/duplex.log`。无需重定向 CMD 的 stdout/stderr；
原组件日志与控制台输出不变。该文件使用现有 UTF-8、安全轮转及敏感数据过滤，
单文件上限 20 MiB，保留最多 20 份归档。跟随 `logging.agent_server` 文件日志级别。
记录 Jev/Clef 请求与概率、路由入口/回退、stale 开关绕过、安全暂停及打断提交；
用户追问入口仅汇集 `duplex ...` 诊断，不汇集普通对话或工具正文。
启动时记录 `duplex logging ready`，每行带 PID，便于区分服务重启及进程。

Jev 与 Clef 共用 `common/duplex_choice.py` 的 Choice 问题构造、配置校验、HTTP
传输、异常处理及脱敏决策日志；概率校验继续复用 `duplex_decision.py`。
适配层只保留各自 URL、认证环境变量、模型名、state 编码、提示词和响应包裹差异。
路由日志统一为 `duplex route ... backend=jev/clef`，不再重复输出 Jev 专属路由日志。
两者均只请求一次、不跟随重定向；新建客户端由公共层关闭，注入客户端由调用者管理。

在运行 agentserver 的环境中设置 `TYPESAFE_API_KEY`（或在其私有 `config/.env` 中设置，勿提交密钥），
将工作区 `config/config.yaml` 的路由配置改为下面内容，然后重启团队：

```yaml
duplex_router:
  mode: active
  policy: model
  backend: jev
  model_name: jev-1.13.0
  timeout_seconds: 2.0
  jev:
    api_base: https://api.typesafe.ai/v1
    api_key_env: TYPESAFE_API_KEY
    interrupt_threshold: 0.9
```

Jev 走 TypeSafe 托管 API，不在本地 GPU 加载权重，也不需要加入团队的聊天模型池。
已有 Qwen 服务和执行模型配置可以保留。省略 `backend` 时仍使用原 `sdk` 后端；
Jev 的 `model_name` 留空时固定使用 `jev-1.13.0`，省略总期限时使用 2 秒。
`api_base` 是包含 `/v1` 的 API 根地址，适配器在其后加 `/systemone`。

MindsHub 提供同一 Jev 类型化协议，使用其独立的 `/v1/decisions` 端点。
已有 `MINDSHUB_API_KEY` 可通过下列配置使用；这与下文的 `mindshub_air` 聊天后端不同：

```yaml
duplex_router:
  mode: active
  policy: model
  backend: jev
  model_name: jev-1.13.0
  timeout_seconds: 8.0
  jev:
    api_base: https://api.mindshub.ai/v1
    endpoint_path: decisions
    api_key_env: MINDSHUB_API_KEY
    interrupt_threshold: 0.9
```

`endpoint_path` 只接受 `systemone` 或 `decisions`，默认 `systemone`。
回放时可用 `--jev-api-base https://api.mindshub.ai/v1 --jev-endpoint-path decisions
--jev-api-key-env MINDSHUB_API_KEY`。见 [MindsHub Decisions 文档](https://docs.mindshub.ai/inference/decisions)。
2026-09-30 使用本机私有密钥实测六条路由样例：6/6 正确、0 次失败、0 次误中断，
p50 约 1.79 秒、p95 约 2.09 秒。此结果仅验证小样本路由，不代表完整任务质量。

## Cloudflare Clef 后端

Cloudflare Clef 使用相同的 APPEND / INTERRUPT 路由契约，作为可选后端接入；
不会自动替换现有 Jev 配置。在运行 agentserver 的环境中设置
`CLOUDFLARE_ACCOUNT_ID` 和 `CLOUDFLARE_AUTH_TOKEN`，然后配置：

```yaml
duplex_router:
  mode: active
  policy: model
  backend: clef
  timeout_seconds: 2.0
  clef:
    model: clef  # 或 clef-flash
    interrupt_threshold: 0.9
```

适配器向 Cloudflare Workers AI 的 `@cf/cloudflare/clef`（或 `clef-flash`）
发送一次类型化 Choice 请求。账号与令牌只从环境变量读取，不写入仓库；
HTTP 错误、超时、非法结果或过期快照仍按现有路由回退到 SDK steer。
合并时只验证了模拟响应和路由链路，真实 Cloudflare 账户调用仍需单独验证。
[Cloudflare Clef 文档](https://developers.cloudflare.com/workers-ai/models/clef/)。

Clef 排查日志不保存密钥、请求/响应正文或状态内容。`Clef request start` 的
`request_id` 将 HTTP 状态、耗时、原始 `choice`、`p_interrupt/p_append`、阈值和最终动作关联；
`reason=below_threshold` 表示模型选择中断但被阈值挡住。广播消息 ID 会被多个接收者复用，
需结合路由入口的 `recipient` 和快照版本区分各次判断。
`duplex user followup dispatch` 记录 Web 追问入口；`duplex user input entry` 记录 SDK 事件入口。
`duplex delivery bypass` / `duplex route bypass` 解释未判断的原因（未包装、非 active、
不支持的 harness、无快照、policy 或 use_steer）。模型建议不等于完成打断：
只有 `duplex interrupt committed ... replan_input_sent=true` 和最终
`duplex route effective ... action=INTERRUPT` 才确认安全暂停与重规划输入提交；
`replan_input_sent` 不代表重规划任务已经完成。`stale` 或 `superseded` 表示判断未执行。

适配器仅发送已有 `goal / next_action / last_action / phase` 和消息的发送者、ID、原文，
不发送完整上下文或隐藏推理。状态和消息会离开本机到达所配置的 API。
同一套路由规则放入 `Choice.instructions`，选项固定为 `APPEND` 和 `INTERRUPT`。
校验响应类型、选项、概率分布及置信度后，只有 `choice=INTERRUPT` 且
`probabilities.INTERRUPT >= interrupt_threshold` 才建议中断，否则 APPEND。
阈值必须满足 `0.5 < threshold <= 1`；默认 0.9 只是保守的初始策略，未经过本项目准确率校准，
阈值越高越容易漏掉应中断的消息。这里使用选项概率，不能把阈值直接套到 `confidence` 字段。

每条消息只请求一次，无自动重试。缺失密钥、HTTP 错误（含 429/529）、非法响应均走原 steer；
总超时覆盖整个判断，HTTP 超时也记录为 `timeout`。返回后仍进行快照版本校验；
过期判断不执行，中断继续使用 Native supervisor 的安全暂停、保留已提交工具结果和重新规划机制。
路由观测记录 action/status/latency；Clef 调试日志另记录经过校验的选项概率，不保存密钥。

可先用现有 6 条样例验证真实 API（只测路由，不执行 Agent 动作）：

```bash
python -m jiuwenswarm.common.duplex_benchmark \
  tests/fixtures/duplex/routing_cases.jsonl \
  --backend jev --jev-model jev-1.13.0 \
  --jev-interrupt-threshold 0.9 --timeout-seconds 2 \
  --repeats 1 --output /tmp/duplex-jev-replay.json
```

CLI 也支持 `--jev-api-base` 和 `--jev-api-key-env`；密钥只从环境读取。
报告中的失败仍计入准确率分母。6 条样例只够冒烟测试，正式启用前需增加真实中英文 A2A 冲突、
权限边界、误中断和注入样本，并针对固定版本选阈值。
[TypeSafe API](https://docs.typesafe.ai/api) · [Jev 已知局限](https://docs.typesafe.ai/model-jaggedness/jev-1.13)

2026-09-25 接线验证：在现有评测服务器运行 `test_duplex_jev.py`、`test_duplex_shadow.py`
和 `test_duplex_e2e.py`，116 通过、2 跳过。Jev/聊天模型 HTTP 响应为测试夹具，
SQLite、SDK 消息投递、Native 暂停恢复、工具执行和 ACK 为真实链路。
覆盖高概率中断、工具提交后暂停、低概率 APPEND、529、缺失密钥、超时和过期响应回退。
该结果不代表真实 Jev 的连通性、准确率或延迟；尚未配置生产 Jev 密钥，也未切换现有监工服务。

## MindsHub 聊天监工后端

`mindshub_air` 是聊天补全模型，不是 Jev 的类型化 Decisions 模型。此后端调用
`https://api.mindshub.ai/v1/chat/completions`，要求模型只返回
`{"action":"APPEND"}` 或 `{"action":"INTERRUPT"}`。结果必须是严格 JSON，
不接受代码围栏、额外字段或其他动作；HTTP 错误、超时、无效响应仍退回原 SDK steer。
聊天输出没有 Jev 的选项概率，因此不能使用 Jev 的概率阈值。

在本机工作区私有 `config/.env` 中设置 `MINDSHUB_API_KEY`，并在
`config/config.yaml` 配置：

```yaml
duplex_router:
  mode: active
  policy: model
  backend: mindshub
  model_name: mindshub_air
  timeout_seconds: 8.0
  mindshub:
    api_base: https://api.mindshub.ai/v1
    api_key_env: MINDSHUB_API_KEY
```

本地回放命令：

```bash
python -m jiuwenswarm.common.duplex_benchmark \
  tests/fixtures/duplex/routing_cases.jsonl \
  --backend mindshub --mindshub-model mindshub_air \
  --timeout-seconds 8 --repeats 1 --output /tmp/mindshub-routing.json
```

2026-09-30 实测：六条路由样例 6/6，失败 0，误中断 0，p50 约 3.19 秒，
p95 约 4.03 秒。仅是小样本路由冒烟测试，不代表真实任务质量或生产可靠性。
发送给服务商的内容包括任务目标、当前/上一动作及来信原文，请按数据政策启用。

## 执行语义

| 结果 | 执行 |
| --- | --- |
| APPEND | 原 steer，下一次原有模型调用前注入 |
| INTERRUPT | 原 SDK 安全暂停；保留当前 ctx，作废旧计划并结合新消息重新规划 |
| 失败 / 超时 / 非法结果 / 过期 | 原 steer |

快模型只返回 `{"action":"APPEND"}` 或 `{"action":"INTERRUPT"}`，状态编号只在本地校验。每条消息只判断一次。慢模型的工具列表和提示不增加内容；不生成状态摘要。快模型读取已有任务、计划和执行位置。其他模式走原 SDK，不再运行影子观察工作线程。

消息收发、排序、工具执行与已读确认均沿用原 Jiuwen。路由在原投递调用内等待判断结果；没有另建后台投递或持久接受机制。

代码：`duplex_shadow.py` 保留原安装入口名，负责消息对接与独立快模型调用；`duplex_native.py` 只实现 supervisor 内的打断适配；`common/duplex_router.py` 判断一次并校验响应。

[删减范围与边界](duplex-simplification.md) · [架构及数据流图](diagrams/README.md)
