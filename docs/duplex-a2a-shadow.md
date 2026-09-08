> 当前删减范围及验证见 [简化说明](duplex-simplification.md)。下文较早回归记录保留当时行为。

# Native 全双工 A2A / U2A

基于方案实现可运行的 Native 闭环：消息进入原消息库，快模型决定 APPEND / INTERRUPT，
Native supervisor 在安全边界注入或取消恢复，再由原消息处理器确认投递。
默认关闭；支持影子观察和真实执行，不修改推理引擎。

## 开启

用户工作区 `config.yaml` 顶层配置如下，修改 mode 后重启团队：

```yaml
duplex_router:
  mode: active            # "off" / shadow / active
  policy: model           # model / always_interrupt / steer / serial
  model_name: fast-model  # 当前团队模型池能解析的快模型名称
  timeout_seconds: 2.0    # 快模型总预算，含最多一次快照重算；最大 30 秒
```

慢模型沿用团队原配置，凭据留在现有模型池中。模型配置缺失、调用异常、超时或输出
不合法时退化为追加。`always_interrupt` 复用同一套安全恢复机制，用于规则对照。
`steer` / `serial` 用于投递策略对照。关闭模式使用原 SDK Native；影子模式也不安装
真实打断运行时，不给慢模型增加工具，只异步记录判断。

## 实际链路

1. A2A 的普通 `plain` / `text` 消息与广播沿用 SDK 的 DB、EventBus 和 poll。
   渲染内容保留 message_id，由原 `TeamAgent.deliver_input` 接入路由。
2. U2A 的 `AgentLifecycleHandler.on_user_input` 接入同一控制器。
   框架模板、JSON 审批/控制消息、human avatar 不交给快模型。
3. `TeamHarness.inner_agent` 暴露真正的 Native；只在 active 模式通过构造入口替换为
   `DuplexNativeHarness`。外部 CLI 仍走 SDK 原来的 steer / follow-up 能力降级。
4. APPEND 入原 steering 队列，当前模型和工具继续。即使消息到达于最后一次生成中，
   也会安排一次 continuation，保证已确认的追加消息有机会被采纳。
5. INTERRUPT 是 supervisor 内的一条事务命令：再校验版本、暂停、注入、恢复。
   模型阶段取消请求并恢复检查点；工具阶段等待完成并保留结果，不回滚外部副作用。
6. pause / abort / stop 可以取代待完成的路由事务；这种情况下不自动重启，消息保持
   未读供原恢复路径处理。DB ACK 失败的同实例重试根据 message_id 去重。

## 上下文与一致性

- 快模型每次创建独立 TinyAgent；名字唯一，避免多收件人共享工具资源。只读控制摘要，
  不读完整历史、未完成输出、隐藏思维或 KV。
- active 模式提供 `update_working_intent` 工具，慢模型显式提交 goal、hypothesis、
  next_action、constraints。它写入当前 SDK session；未提交时使用已确认任务计划，
  未知假设保持空值，不从生成中的内容猜测。
- Native 在模型调用前补充安全检查点，此时原用户任务和已采纳的 steering 已进入上下文，
  但新生成尚未开始。这也覆盖“第一次模型调用就被打断”的恢复。
- context_version 随运行状态、阶段、输入、Working Intent 和检查点变化递增。
  快模型结果过期时最多重算一次；supervisor 最终再验版本，旧决策不能打断新状态。
- 尚未提交的追加消息保留到安全检查点，取消重放保持顺序；完成的工具结果保留在
  SDK 原上下文中。运行中暂停恢复不重复已经完成的工具。
- checkpoint_id 是运行实例内的轮次/检查点标签，不是跨进程数据库地址。投递去重
  作用域是当前 Native 实例；进程崩溃后的恢复继续由 SDK 的持久化机制负责。
- 已删除固定接收容量和字符截断，保留完整消息。判断异常按原方案退回 SDK steer，观测单独记录失败。

安装入口仍是 `bootstrap.configure_agent_teams_home`。SDK 尚无公开的 pre-delivery
扩展接口，适配代码集中在 `duplex_shadow.py`；真实状态机在 `duplex_native.py`。
没有修改已安装 SDK 的文件。升级 SDK 时必须重跑集成测试。

## 端到端验证

```powershell
python -m pytest tests/integration_tests/test_duplex_e2e.py -v -o addopts=''
python -m pytest tests/unit_tests/agentserver/test_duplex_shadow.py tests/unit_tests/agentserver/test_team_name_generator.py tests/unit_tests/agentserver/test_team_manager_registry.py -q -o addopts=''
```

集成测试使用真实 SQLite、InProcessMessager、EventBus、消息处理器、TeamHarness、
Native supervisor、ReAct、工具和 TinyAgent HTTP 客户端；只有模型端点按脚本响应。
2026-09-08 使用锁定 SDK 验证：96 项单元测试和 18 项端到端测试通过，Ruff 通过。
验证模型阶段恢复、工具阶段等待、未消费追加消息保留、最终生成后的追加采纳、版本
竞态、生命周期取代、ACK 失败重试、广播水位、事件丢失轮询、U2A、Working Intent
以及影子模式实际接入。测试工具向临时文件执行不可撤销写入，并断言只写一次。

四组对照在同一工具边界注入同一消息：G0 原 SDK 串行、G1 原 SDK steer、
G2 安全全量打断、G3 快模型选择性打断。测试报告记录快慢调用次数，检查原任务和
新增消息进入最终上下文。它们是机制对照，不是质量或性能收益跑分。

## 真实快模型分类回放

此回放只诊断路由，不能代替方案要求的公开任务评测。
AgentRadio / InterruptBench 原始输入、接入进展和缺口见
[公开评测接入](duplex-public-benchmarks.md)。

```powershell
python -m jiuwenswarm.common.duplex_benchmark tests/fixtures/duplex/routing_cases.jsonl --validate-only
python -m jiuwenswarm.common.duplex_benchmark tests/fixtures/duplex/routing_cases.jsonl --model-config D:/private/fast-model.json --output D:/results/duplex.json --repeats 3
```

配置文件使用 SDK 的 TeamModelConfig：

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

报告包含失败在内的准确率、误打断、失败数量及 p50/p95 决策耗时；expected_action
不会发送给模型。示例是人工机制用例，不冒充原数据集。真实模型下的任务成功率、
采纳延迟、总工期、token 成本和公开 Benchmark 结果需要独立实测；当前没有这些跑分。

依赖以仓库 `uv.lock` 锁定的 agent-core `94e10cb` 为准。共享环境的旧安装缺少主干所需
Rail，直接使用远端最新 SDK 又有接口漂移；验证应使用锁定版本，不用业务代码掩盖依赖问题。

## 2026-09-08 评审后的修正

本节是第一轮修正记录。第二次评审 F1–F4 的最新修复与验证见
[第二轮修复](duplex-review-fixes.md)。

每个 Native 现在持有一个输入控制器。普通 DB 消息在原 drain 等待分类时仍可登记，
新消息取消尚未应用的旧分类并合批重判，总期限取 SDK 快模型 timeout（可显式覆盖），从最早输入起算；没有额外默认容量。
原 drain 保留排序和 ACK；控制消息仍走原处理。模型分类不再阻塞 EventBus 的其他事件。

状态 rail 在模型、工具和迭代边界发布已提交状态；没有显式 Working Intent 时，
假设字段保持未知。发布边界状态不代表模型自动生成了新的语义摘要。

Native 工具执行前写 SQLite 账本，完成后保存可重建结果，同一会话和 tool_call_id
重试复用结果。非幂等工具结果不明时拒绝自动重复执行。默认路径为
`~/.jiuwenswarm/duplex-state/duplex-tools.sqlite3`，可用 `JIUWEN_DUPLEX_LEDGER_PATH` 指定。
这不是外部系统的 exactly-once 保证；新进程恢复消息接受记录及检查点仍未完成验收。
当前消息去重和 Native 安全恢复的证明范围仍是同一存活实例，不能据此宣称崩溃恢复完成。
