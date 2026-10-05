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

在运行 agentserver 的环境中设置 `TYPESAFE_API_KEY`。密钥只放在环境变量或私有 `config/.env`，不要提交。将工作区 `config/config.yaml` 改为：

```yaml
duplex_router:
  mode: active
  policy: model
  backend: jev
  model_name: jev-1.13.0
  timeout_seconds: 2.0
  jev:
    api_base: https://api.typesafe.ai/v1
    endpoint_path: systemone
    api_key_env: TYPESAFE_API_KEY
    interrupt_threshold: 0.9
```

`backend: jev` 走 TypeSafe 的 Choice 接口，不把 Jev 放进团队聊天模型池。省略 `backend` 或保持 `sdk` 时，仍使用 `classifier` 指定的团队模型或 Clef。`model_name` 留空时使用 `jev-1.13.0`。`endpoint_path` 只接受 `systemone` 或 `decisions`。

适配器只发送 `goal`、`next_action`、`last_action`、`phase` 和消息的发送者、ID、原文。只有 `choice=INTERRUPT` 且 `probabilities.INTERRUPT` 不低于阈值才建议中断，否则按 APPEND。阈值必须大于 0.5 且不超过 1。HTTP 错误、非法响应和总超时都退回原来的 steer。

```bash
python -m jiuwenswarm.common.duplex_benchmark \
  tests/fixtures/duplex/routing_cases.jsonl \
  --backend jev --jev-model jev-1.13.0 \
  --jev-interrupt-threshold 0.9 --timeout-seconds 2 \
  --repeats 1 --output /tmp/duplex-jev-replay.json
```

| 结果 | 执行 |
| --- | --- |
| APPEND | 原 steer，下一次原有模型调用前注入 |
| INTERRUPT | 原 SDK 安全暂停；保留当前 ctx，作废旧计划并结合新消息重新规划 |
| 失败 / 超时 / 非法结果 / 过期 | 原 steer |

快模型只返回 `{"action":"APPEND"}` 或 `{"action":"INTERRUPT"}`，状态编号只在本地校验。每条消息只判断一次。慢模型的工具列表和提示不增加内容；不生成状态摘要。快模型读取已有任务、计划和执行位置。其他模式走原 SDK，不再运行影子观察工作线程。

消息收发、排序、工具执行与已读确认均沿用原 Jiuwen。路由在原投递调用内等待判断结果；没有另建后台投递或持久接受机制。

代码：`duplex_shadow.py` 保留原安装入口名，负责消息对接与独立快模型调用；`duplex_native.py` 只实现 supervisor 内的打断适配；`common/duplex_router.py` 判断一次并校验响应。

[删减范围与边界](duplex-simplification.md) · [架构及数据流图](diagrams/README.md)
