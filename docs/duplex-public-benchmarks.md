# 全双工公开评测接入

需求来源是工作区 `jiuwen/全双工快慢Agent_完整调研与方案.md` 和
`jiuwen/全双工快慢Agent_Benchmark数据集调研.md`，以及后续 HTML 提案。
Native 路由当前采用 HTML 的 APPEND / INTERRUPT 二选一；公开评测遵循两份调研的
“不造新任务、不编写中途消息、沿用原评分器”。人工 Kafka 用例只用于机制测试。

## 已核实的原始输入

| 上游 | 固定版本 | 内容 |
| --- | --- | --- |
| [AgentRadio](https://github.com/Coral-Protocol/AgentRadio) | `5e4e137991ce5662d95cf890e534b5b223d24d6a` | 124 个任务、1,306 条 rubric、固定仓库 commit 和容器环境、四 Agent 五阶段协议 |
| [InterruptBench](https://github.com/HenryPengZou/InterruptBench) | `17da111e4858b93c0cab1d88f85e1735fbd1d423` | 六个更新集合，各 165 个任务，共 1,815 条原始 updates |

第三方仓库留在工作区外部路径，不把数据集复制进 Jiuwen。清单生成器检查版本及
tracked 文件无修改，记录原任务、环境、协议、评分器和数据文件的 SHA256：

```powershell
python -m jiuwenswarm.common.duplex_public_benchmark --agentradio D:/jiusi_agent/_repos/AgentRadio --interruptbench D:/jiusi_agent/_repos/InterruptBench --output D:/results/public-inputs.json
```

输出目录须存在；同名结果拒绝覆盖。输出状态明确为 `inputs_prepared_not_evaluated`。

## InterruptBench 输入桥接

`load_official_interrupt` 读取官方 raw、task config、interrupt spec 和 baseline
trajectory，校验任务 ID、初始意图与更新原文；直接执行固定版本上游的纯函数
`_resolve_interrupt_at_action` 计算触发位置，不另写一套百分比规则。

环境 runner 到达原动作边界后，可调用 `OfficialInterrupt.deliver`，通过真实
`AgentLifecycleHandler.on_user_input` 交给 Native 全双工控制器。边界错过、任务已终止
或输入被改写时拒绝投递；同一实验/阶段的重试使用相同 run_id。真实目标答案和
评分条件不会交给快模型或慢模型。

当前只支持初始阶段的一次更新。连续更新需要核对上游每一阶段生成的配置和历史，
不能把第二轮 config 偷换成原始初始任务。上游 `update_mode=append/replace` 是
用户意图更新方式，不是快模型应该输出 APPEND/INTERRUPT 的标签。

## 验证与剩余工作

2026-09-08：8 项上游输入契约测试、1 项真实 Native U2A 桥接测试通过。
测试读取外部官方数据；模型响应和 baseline 轨迹形状为测试夹具。
这证明输入与投递接线，**不代表 WebArena 任务完成或公开评测跑分**。

```powershell
$env:JIUWEN_AGENTRADIO_ROOT='D:/jiusi_agent/_repos/AgentRadio'
$env:JIUWEN_INTERRUPT_BENCH_ROOT='D:/jiusi_agent/_repos/InterruptBench'
python -m pytest tests/unit_tests/agentserver/test_duplex_public_benchmark.py tests/integration_tests/test_duplex_e2e.py -k 'public_benchmark or official_interruptbench' -q -o addopts=''
```

需要继续完成的实测链路：

1. AgentRadio：原容器环境、消息服务、四 Agent 协议接入 Jiuwen 执行器，保持原题、
   原工具与原评分器；分别运行 L2、L3、Jiuwen steer、全部 abort 重跑和语义路由。
2. InterruptBench：将桥接挂到真实 WebArena replay runner，保留页面 reset、官方
   updates、触发位置、完整 trajectory 与 `evaluator_router`；覆盖连续更新阶段。
3. 收集原任务成功率、makespan、采纳延迟、快慢模型总 token、取消请求成本和重复
   副作用。缺少指标必须报告缺失，不能把分类准确率代替任务成绩。

现有集成测试中的 `always_interrupt` 是安全边界恢复对照，不等同于公开方案的
“任意消息都 abort 后从头重跑”基线，不能冒用该组成绩。

本机检查尚未找到 Docker/Harbor 命令、已配置的 WebArena 站点或可用模型配置。
这些运行依赖及上述执行器接入尚未完成，因此目前没有公开任务成功率或性能结论。
