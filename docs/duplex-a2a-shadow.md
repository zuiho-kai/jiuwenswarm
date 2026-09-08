# A2A 快慢模型：影子路由

基于 `jiuwen_duplex_a2a_proposal.html` 的第一阶段：Native A2A 影子路由和离线分类回放。
默认关闭。快模型即使输出 `INTERRUPT`，实际仍执行原来的 steer / follow-up。
本阶段没有启用取消重启，不宣称已实现完整全双工或取得性能收益。

## 开启

在用户工作区 `config.yaml` 顶层添加，重启 AgentServer：

```yaml
duplex_router:
  mode: shadow             # off（默认）或 shadow
  model_name: fast-model   # 必须是当前团队模型池可解析的模型名
  timeout_seconds: 2.0    # 每批含一次版本重算的总预算，最大 30 秒
```

配置不会自动选择慢模型，也不会改动团队拓扑。每个收件方一个后台 worker，
最多等待 32 条消息，批内按原投递顺序合并；拥塞只跳过观察，不丢实际消息。
每条消息最多采集 4,000 字符，每批发送的消息正文最多 12,000 字符，截断会显式标记。
运行中修改模型名/超时需要重启团队，`mode: off` 会立即停止接纳新观察。

## 接入与边界

- SDK 从 DB 读取消息并展开模板后，在 `_format_message` 返回前提交观察。
  原来的消息投递、投递成功后批量 ACK、广播水位和事件丢失轮询均由 SDK 负责。
- Native 的 `active_round`、最近 `SafeStateSnapshot` 提供原任务、已提交计划、
  当前任务、模型/工具阶段。快模型不读取完整历史、半截输出或隐藏思维。
- SDK 尚无显式 Working Intent 提交协议，因此 `current_hypothesis`、`constraints`
  及工具副作用属性保持未知，不从输出猜测；离线回放可显式提供这些字段。
- 快模型使用独立临时 TinyAgent。结果必须严格符合枚举和快照标识；状态变化时
  在同一时间预算内重算一次，再过期则丢弃。错误和超时记录 APPEND 建议。
- 版本号由当前 Native 状态派生，仅用于影子观察；它不是 DB 版本，也不能用作
  原子重启的并发控制。checkpoint_id 是轮次/迭代标签，不能据此跨进程恢复。
- human/bridge/外部 CLI、非 text 协议和框架模板不参与观察；确定性系统事件继续原路径。
- 日志前缀 `duplex.shadow` 后为 JSON，含消息 ID、收件人、session、轮次、
  检查点标签、建议、状态、耗时和重算次数，不含消息正文。`effective_action=UNCHANGED`
  表示未改变原行为，观察也不代表消息已被慢模型采纳。
- 安装入口在 `bootstrap.configure_agent_teams_home`。当前 SDK 没有公开的
  pre-delivery observer，因此用可重复安装的窄适配器包装 `_format_message` 和
  `_dispose_tiny_agents`（团队停止时取消观察）。SDK 升级须跑接入测试。

## 验证与回放

```powershell
python -m pytest tests/unit_tests/agentserver/test_duplex_shadow.py -q -o addopts=''
python -m jiuwenswarm.common.duplex_benchmark tests/fixtures/duplex/routing_cases.jsonl --validate-only
python -m jiuwenswarm.common.duplex_benchmark tests/fixtures/duplex/routing_cases.jsonl --model-config D:/private/fast-model.json --output D:/results/duplex.json --repeats 3
```

`fast-model.json` 是 SDK 的 `TeamModelConfig`，例如：

```json
{
  "model_client_config": {
    "client_provider": "OpenAI",
    "api_base": "http://127.0.0.1:8000/v1",
    "api_key": "replace-me"
  },
  "model_request_config": {"model_name": "fast-model", "temperature": 0}
}
```

不要把凭据提交到仓库。回放报告输出每次结果、包含失败的准确率、失败数量、
误打断数量和 p50/p95 决策耗时。输入中的 expected_action 不会发送给模型。
六个示例是人工构造的机制用例，包含 U2A 离线用例；线上入口当前只覆盖 A2A。
它们不是原 Benchmark 任务，不测原任务成功率、采纳延迟、总工期或 token 收益。

## 后续开启真实打断前

1. 在 agent-core 增加公开投递观察接口、权威 context_version 和安全边界 Working Intent。
2. 在 Native supervisor 内完成带版本校验的原子 pause/restart；模型阶段回滚未提交输出，
   工具阶段等完成并记账后重规划。不能在产品层简单串联 abort + send。
3. 保留原验收器选取真实任务，固定模型、工具、环境、消息与注入边界，运行 G0 串行、
   G1 现有 steer、G2 全量打断、G3 选择性打断四组；每组分别报告原任务与注入任务结果。
4. 验证消息不丢、不可逆工具不重复、错误版本不生效后，再讨论启用 active 模式。

## 本地验证记录（2026-09-08）

基线 `origin/develop e69feefa`，本地 SDK `openjiuwen 0.1.16`。
新增测试覆盖快照失效重算、超时/取消、背压、SDK 真正的 mailbox drain、投递失败
保留未读、广播原对象 ACK、poll 补偿、停机清理、配置关闭与控制消息绕过。
回放输入校验通过。尚未调用真实小模型，也未运行完整任务四组对照。

扩大回归中 `test_team_manager_registry.py` 为 51 通过、9 失败；撤下本次接入后
重跑仍为同样 9 项失败，均因主干引用的 `ModelAnomalyDetectionRail` 不在本地 SDK
中。该依赖不匹配未通过修改业务代码掩盖。
