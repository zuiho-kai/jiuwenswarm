# 全双工修改图

按当前实现整理，图中方法名可直接对应代码。

| 图 | PlantUML 源文件 | SVG 预览 |
| --- | --- | --- |
| 修改架构：原 SDK、接入层、快模型、Native 安全恢复 | [源文件](duplex-architecture.puml) | [预览](duplex-architecture.svg) |
| 输入处理：模式、去重、版本重算、追加、打断和生命周期竞态 | [源文件](duplex-routing-flow.puml) | [预览](duplex-routing-flow.svg) |
| 官方评测：原任务、多阶段执行、原评分器和配对报告 | [源文件](duplex-benchmark-flow.puml) | [预览](duplex-benchmark-flow.svg) |

对应实现：`agents/harness/team/duplex_shadow.py`、`duplex_native.py`、
`common/duplex_router.py` 和 `benchmarks/`（均位于 `jiuwenswarm/`）。
运行时细节见[实现说明](../duplex-a2a-shadow.md)，评测口径见[公开评测接入](../duplex-public-benchmarks.md)。

架构图中灰色为原 SDK，蓝色为包装入口，绿色为新增逻辑。原消息 DB、广播水位和 ACK
仍由 SDK 管理；快模型只看已提交摘要。工具副作用不会回滚，实例内去重不等于跨进程恰好一次。

评测流程图描述代码接入方式，不是公开成绩报告。AgentRadio 保留 Coral 通道；
InterruptBench 保留原网页轨迹、动作解析与评分，额外并发注入条件在图中单独标明。

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
