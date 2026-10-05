# Native 最简 APPEND / INTERRUPT

仅增加快模型路由和原 SDK 的执行对接，默认关闭。

```yaml
duplex_router:
  mode: active
  policy: model
  model_name: fast-model
```

快模型总期限默认沿用该模型的 SDK timeout，可显式设置 `timeout_seconds`。

## Jev 监工后端

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
路由观测仍记录 action/status/latency，不保存原始概率或密钥。

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
