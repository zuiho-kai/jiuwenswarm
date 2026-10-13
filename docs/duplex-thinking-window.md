# 动态最小思考窗口

每个 DuplexNativeHarness 独立维护窗口，不跨 Agent、会话共享。

- 初始 300ms，下限 100ms，上限 8s。
- 每轮首次模型调用开始计时；同轮后续调用不重新计时。
- 成功提交中断时窗口乘 2，恢复执行开启新的保护窗口。
- 每轮首个非空完整模型响应（文本或工具调用）将窗口减少 100ms。
  不使用内部思考 token、空响应或心跳作为输出信号。
- 保护期内暂存成员消息，不调用监工、不提前确认已读。
- 到期读取最新快照，合并并发交付的消息后调用监工。
- SDK 的顺序消息 drain 仍逐条等待交付，因此顺序队列不保证批量合并；
  后续消息如果遇到中断恢复，会等待加长后的新窗口。
- 提交中断前再次检查窗口，防止旧监工请求打断新保护轮；拒绝时追加消息。
- 取消等待不会留下后台定时任务；停止后的运行时不能被延迟消息重新启动。
- 手动停止、取消和原有工具安全暂停语义保持不变。
- 仅监工 active/model 路径使用窗口，监工关闭和原生交流路径不受影响。

日志：`duplex thinking begin/backoff/output/flush`，以及
`duplex interrupt rejected ... reason=thinking_window`。

该策略降低连续中断风险，但 8s 上限不保证超过 8s 的模型一定完成响应。
没有修改快照 stale 开关和监工阈值。后续增加的未发送草稿恢复机制见
`docs/duplex-progress-recovery.md`，不属于 token 或 KV cache 续写。

验证：`python -m pytest -o addopts='' tests/unit_tests/agentserver/test_duplex_thinking.py tests/unit_tests/agentserver/test_duplex_shadow.py -q`。
这些为模拟慢模型和路由的自动测试，不是外部模型在线评测。
