# 全双工修改图

架构简图和输入流程图统一使用 ①–⑤，只展示一次新消息如何影响慢 Agent；代码细节见实现说明。

当前删减范围见 [简化说明](../duplex-simplification.md)。

## 特性评审入口

先看[五步总览](duplex-architecture.svg)，再按同样编号展开：

| 总览步骤 | 详细图 | 评审内容 | PUML |
| --- | --- | --- | --- |
| ② 接消息 | [打开](duplex-detail-02-intake.svg) | 原入口、顺序投递、重复输入处理 | [源文件](duplex-detail-02-intake.puml) |
| ③ 做判断 | [打开](duplex-detail-03-decision.svg) | 状态来源、显式期限、版本校验、失败重试 | [源文件](duplex-detail-03-decision.puml) |
| ④ 执行决定 | [打开](duplex-detail-04-apply.svg) | 追加时机、安全暂停、工具结果、恢复、已读确认 | [源文件](duplex-detail-04-apply.puml) |

这些图描述当前实现；恢复需要相同稳定会话身份、持久文件和可恢复的外部环境，不能据此承诺任意外部操作恰好一次。

| 图 | PlantUML 源文件 | SVG 预览 |
| --- | --- | --- |
| 架构简图：收消息 → 快模型判断 → 追加或打断 → 慢 Agent 继续 | [源文件](duplex-architecture.puml) | [预览](duplex-architecture.svg) |
| 输入流程：快模型判断，慢 Agent 的执行器负责停下和继续 | [源文件](duplex-routing-flow.puml) | [预览](duplex-routing-flow.svg) |
| 官方评测：原任务、多阶段执行、原评分器和配对报告 | [源文件](duplex-benchmark-flow.puml) | [预览](duplex-benchmark-flow.svg) |

对应实现：`agents/harness/team/duplex_{shadow,native,controller,ingress,state,ledger}.py`、
`common/duplex_router.py`，以及 `benchmarks/duplex_database_peer.py`、
`interruptbench_runner.py` 等评测入口（均位于 `jiuwenswarm/`）。
运行时细节见[实现说明](../duplex-a2a-shadow.md)，评测口径见[公开评测接入](../duplex-public-benchmarks.md)。

架构简图以中间方框标出新增处理，配套机制放在旁注，不逐个展开模块。原消息 DB、广播水位和 ACK
仍由 SDK 管理。InputController 按最早消息的截止时间合批，新消息能取消尚未应用的
快模型判断，不能抢占正在应用的 Native 事务。边界状态只投影已提交任务、公开输出
和工具状态；语义 hypothesis 未经 `update_working_intent` 显式发布时保持未知。

ToolLedger 将 Native 工具回执存入独立 SQLite：同一 session / call_id 且参数匹配、
回执可恢复时复用结果；非幂等调用结果不确定时拒绝自动重做，等待核对。
它不撤销外部副作用，也不代表跨进程全局恰好一次。消息去重仍有实例内状态与 DB ACK
各自的作用域，不能与工具账本混为同一保证。
active 路由在 ACK 前保存完整输入，再结合 Native 上下文检查点恢复。已增加真实强杀进程测试，
覆盖 DB 已读但消息未消费、工具收据已提交但上下文未更新两个窗口；主动停止不会自动恢复执行。

评测流程图描述当前代码接入，不是公开成绩报告。AgentRadio 的 Native 组保留 Coral
作为外部协议，经 DatabasePeer 把原输出写入 Jiuwen DB 本地收件账本，再走原 SDK
消息处理与 ACK。L2 / L3 沿用原上游执行器。

InterruptBench 在同一题中复用 NativePeer，reset、失败或退出时关闭；边界控制摘要
使用原 prompt 中的已提交历史。官方更新通过原用户输入处理器送入 Native。
动作解析、`env.step`、轨迹与评分由原环境负责；动作执行前在 Native 同一账本中预登记，
原轨迹追加后提交结果并关联检查点。Native 重建保留已提交动作证据；浏览器环境变化或结果不明时停止恢复，
不能靠重放点击恢复页面。图中并发注入是显式扩展条件，不能当作原版时序。

## 本地渲染

使用官方 [PlantUML](https://github.com/plantuml/plantuml/releases/tag/v1.2026.8) 1.2026.8 与 Java 24，
不向在线服务上传源码。所有图均使用 PlantUML 活动图，不依赖 Graphviz。
中文字体使用 Microsoft YaHei；其他机器应安装该字体或在源文件中替换为可用中文字体。

在仓库根目录执行（把 JAR 路径改为本机路径）：

```powershell
java -jar D:/tools/plantuml.jar -charset UTF-8 -checkonly 'docs/diagrams/*.puml'
java -jar D:/tools/plantuml.jar -charset UTF-8 -tsvg 'docs/diagrams/*.puml'
```

2026-09-08 验证：6 份源文件全部通过 PlantUML `-checkonly`，6 份 SVG 成功生成，
并在本机渲染为 PNG 逐图检查中文、文字边界和连接线。预览没有引用在线渲染服务。
