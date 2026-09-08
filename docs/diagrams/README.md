# 全双工修改图

按当前实现整理，图中方法名可直接对应代码。

| 图 | PlantUML 源文件 | SVG 预览 |
| --- | --- | --- |
| 修改架构：SDK 接入、输入控制器、边界状态、工具账本 | [源文件](duplex-architecture.puml) | [预览](duplex-architecture.svg) |
| 输入处理：消息合批、快模型抢占、截止时间与安全恢复 | [源文件](duplex-routing-flow.puml) | [预览](duplex-routing-flow.svg) |
| 官方评测：原任务、多阶段执行、原评分器和配对报告 | [源文件](duplex-benchmark-flow.puml) | [预览](duplex-benchmark-flow.svg) |

对应实现：`agents/harness/team/duplex_{shadow,native,controller,ingress,state,ledger}.py`、
`common/duplex_router.py`，以及 `benchmarks/duplex_database_peer.py`、
`interruptbench_runner.py` 等评测入口（均位于 `jiuwenswarm/`）。
运行时细节见[实现说明](../duplex-a2a-shadow.md)，评测口径见[公开评测接入](../duplex-public-benchmarks.md)。

架构图中灰色为原 SDK，蓝色为包装入口，绿色为新增逻辑。原消息 DB、广播水位和 ACK
仍由 SDK 管理。InputController 按最早消息的截止时间合批，新消息能取消尚未应用的
快模型判断，不能抢占正在应用的 Native 事务。边界状态只投影已提交任务、公开输出
和工具状态；语义 hypothesis 未经 `update_working_intent` 显式发布时保持未知。

ToolLedger 将 Native 工具回执存入独立 SQLite：同一 session / call_id 且参数匹配、
回执可恢复时复用结果；非幂等调用结果不确定时拒绝自动重做，等待核对。
它不撤销外部副作用，也不代表跨进程全局恰好一次。消息去重仍有实例内状态与 DB ACK
各自的作用域，不能与工具账本混为同一保证。
当前消息接受与 Native 检查点的验证范围仍是存活实例；工具账本持久化不代表整个消息链已实现崩溃恢复。

评测流程图描述当前代码接入，不是公开成绩报告。AgentRadio 的 Native 组保留 Coral
作为外部协议，经 DatabasePeer 把原输出写入 Jiuwen DB 本地收件账本，再走原 SDK
消息处理与 ACK。L2 / L3 沿用原上游执行器。

InterruptBench 在同一题中复用 NativePeer，reset、失败或退出时关闭；边界控制摘要
使用原 prompt 中的已提交历史。动作解析、`env.step`、轨迹与评分由原环境负责，
**WebArena 动作没有进入 Native 工具账本**。图中并发注入是显式扩展条件，不能当作原版时序。

## 本地渲染

使用官方 [PlantUML](https://github.com/plantuml/plantuml/releases/tag/v1.2026.8) 1.2026.8 与 Java 24，
不向在线服务上传源码。架构图采用内置 Smetana 布局，不依赖 Graphviz。
中文字体使用 Microsoft YaHei；其他机器应安装该字体或在源文件中替换为可用中文字体。

在仓库根目录执行（把 JAR 路径改为本机路径）：

```powershell
java -jar D:/tools/plantuml.jar -charset UTF-8 -checkonly 'docs/diagrams/*.puml'
java -jar D:/tools/plantuml.jar -charset UTF-8 -tsvg 'docs/diagrams/*.puml'
```

2026-09-08 验证：3 份源文件全部通过 PlantUML `-checkonly`，3 份 SVG 成功生成，
并在本机渲染为 PNG 逐图检查中文、文字边界和连接线。预览没有引用在线渲染服务。
