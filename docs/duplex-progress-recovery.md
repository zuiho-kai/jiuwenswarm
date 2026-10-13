# 中断草稿恢复

路由仍只有 APPEND / INTERRUPT。监工另返回 progress_policy：KEEP / DISCARD。

- 捕获本次模型调用已经流出的文本和 send_message 参数草稿。
  不捕获 reasoning_content，也不复用其他工具的未执行参数。
- 草稿最多向监工及恢复提示传递 8000 字符；每次模型调用重新开始收集。
- 在现有快照中携带 progress_draft，让监工结合新消息判断能否复用。
- SDK / Mindshub 使用 JSON 选项，Jev / Clef 使用额外的 typed choice 问题。
- KEEP 只复用监工实际看到的那个草稿版本，不带入判定期间新生成的内容。
  监工缺少选项时默认 DISCARD；typed choice 的 KEEP 也须达到当前置信阈值。
- 如果判定期间已经切换轮次，旧轮草稿不得带入新轮。
- INTERRUPT 仍清除旧计划。KEEP 把草稿作为未确认参考注入恢复提示；
  DISCARD 不注入草稿。两者都保留已提交历史和已完成工具结果。
- send_message 草稿不自动广播、不提前确认送达、不执行半截工具参数。
  恢复提示要求下一次消息完整、自包含，补充有效早期内容，不能假定其他 Agent 已看到草稿。
- 没有流出文本/工具参数的模型调用无法保留其未暴露的内部进度。
  这是上下文恢复，不是 token 或 KV cache 续写，不保证推理进度必然递增。

日志仅记录 policy、message_id、draft_chars，不记录草稿正文。
日志位置：`duplex progress decision` / `duplex progress recovery`。

测试：`tests/unit_tests/agentserver/test_duplex_progress.py`。
