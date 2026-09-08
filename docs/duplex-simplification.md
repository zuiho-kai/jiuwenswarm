# 删除额外机制

收件、事件调度和执行排队复用 SDK；不删除原方案要求的并发接收和 steer 降级。

## 已删除

- `duplex_ingress.py`：50ms DB 预取、额外收件锁、全局 EventBus 改写及后台派发任务。
- 评测适配器自建的 250ms 扫库和直接触发 drain；消息只由原 EventBus / mailbox poll 驱动。评测接收者只等待原 ACK 结果。
- Native 独立等待队列，以及三处 32 条容量限制。
- 正文、批次、摘要和约束的固定字符/条数限制，保留完整内容。
- 硬编码默认 2 秒及 30 秒上限，包括评测适配器中的同类默认值。

## 最小接入与保留的能力

1. 原处理器送入消息，先持久化完整输入，再交判断层并返回可靠接受。SDK 此时可以确认已读，继续处理新消息及生命周期事件，无须等待模型。
2. 判断层只保存当前判断和待纳入的输入。新消息取消未应用的旧判断，合批重判；不复制执行队列。
3. 总期限默认取 SDK 快模型 `ModelClientConfig.timeout`，显式 `timeout_seconds` 可覆盖。从最早输入起计算，重判不延长期限；不是无限等待，也没有额外杜撰的默认数字。
4. 判断失败或超时沿用原方案的 SDK steer；观测记录保留失败状态及 UNDECIDED，不能算成成功分类 APPEND。应用前版本失效也不执行旧打断。
5. SDK 的单一 supervisor 负责追加或安全暂停。暂停期间无法投递的输入不再另排队；已接受内容仍在持久记录中，应用错误明确记录，恢复受停止标记保护。

计划来源校验、ACK 前持久化、工具及网页动作收据保留，用于关闭已复现的丢失窗口。已读只表示可靠接受，不表示分类或执行完成。独立浏览器环境仍须存活或另行恢复，不能凭收据重造页面。

原方案来源：`D:/jiusi_agent/jiuwen_duplex_a2a_proposal.html` 的失败/超时降级说明。先前把这项也当成无依据设计删除，是误判；当前已恢复，且保留并发合批和生命周期不阻塞的功能断言。

当前图：[收件](diagrams/duplex-detail-02-intake.svg)、[判断](diagrams/duplex-detail-03-decision.svg)、[执行](diagrams/duplex-detail-04-apply.svg)。

## 最终验证

代码提交 `7c57e9e9`：本机 **162 passed**，233.92 秒，无跳过，含真实 Chromium 和进程强杀恢复；一个上游 Authlib 废弃提示。服务器 **89 passed、3 skipped**，65.90 秒；缺 Harbor 的 1 项及缺独立 WebArena 环境的 2 项跳过。

保留并验证：真实 DB 新消息取消旧判断并合批；真实用户入口的慢判断不阻塞 SDK 后续事件；SDK 模型 timeout 与显式路由期限都能触发原 steer；持久接受后强杀仍恢复输入；暂停不自动重启，工具及网页操作不重复执行。Ruff、diff 检查及 PlantUML 生成通过。

日志：`D:/jiusi_agent/duplex-native-intake-full.txt`、`D:/jiusi_agent/duplex-remote-results/duplex-native-intake-tests.log`。这些是功能回归结果，不是公开 Benchmark 分数。
