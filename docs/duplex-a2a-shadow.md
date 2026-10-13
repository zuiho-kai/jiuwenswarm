# 大小脑全双工：Agent 干活时收到新消息，自动决定怎么处理

Agent 执行任务时，经常收到用户的新要求、队友的新发现、团队广播和群聊消息。原来这些消息一律在下一次模型调用时插进去。这样有两个问题：

- 推翻任务前提的纠正来得太晚。Agent 会继续做一段要返工的工作。
- 和自己无关的广播也会把空闲的 Agent 叫醒。

本 PR 给每个 Agent 配一个监工（小脑）。新消息先按场景分类，需要时由监工判断，再决定怎么投递。执行 Agent（大脑）在监工判断期间继续工作。功能默认关闭。

> 例：执行 Agent 正在设计 Kafka 接入方案。队友私信"环境不能部署 Kafka，改用 Redis Streams"。监工判断必须打断。Agent 在安全位置停下，作废旧计划，带着这条消息重新规划。如果队友只说"Redis 连接信息在配置文件里"，消息排到这一轮结束后处理，Agent 不被打断。

## 一条消息怎么走

```
新消息
  │
  ├─ 1. 按场景分类
  ├─ 2. 投递策略决定：要不要问监工，问什么
  ├─ 3. 监工回答，附带概率
  └─ 4. 投递策略给出动作，交给 SDK 原有的投递方法执行
```

五种动作：

| 动作 | 效果 |
|---|---|
| 打断重规划 | 在安全位置暂停：正在生成就取消，正在执行工具就等它完成。保留上下文和工具结果，作废旧计划，带新消息重新规划 |
| 插入 | 不打断，下一次模型调用时插入 |
| 排队 | 不打断，这一轮结束后再处理 |
| 直接处理 | 目标空闲，马上开始 |
| 只记录 | 留在团队消息记录里并标为已读，不进入 Agent 上下文，不唤醒 |

两条原则：

1. "要不要打断"这一问只能让动作变轻，不能变重，一次只降一级。判错的代价最多是消息晚一点被处理。
   "有没有关系"这一问只用于广播。判成无关时消息只记录，Agent 看不到，所以门槛定在 0.8。
2. 监工失败或超时，消息照常投递：目标空闲就直接处理，目标忙就排队。消息不会丢。

## 各场景怎么处理

| 消息 | 问监工什么 | 目标空闲 | 目标忙 |
|---|---|---|---|
| 用户的消息（会话输入、@ 某个 Agent、用户广播，含语音转成的文字） | 不问 | 直接处理 | 插入（原有行为）。用户点停止按钮可以直接终止 |
| 队友私信 | 要不要打断 | 直接处理 | 必须打断 → 打断重规划；否则排队 |
| 群聊里 @ 某个 Agent | 要不要打断 | 直接处理 | 同队友私信 |
| 团队广播 | 有没有关系 + 要不要打断 | 无关 → 只记录；有关 → 直接处理 | 无关 → 只记录；必须打断 → 插入；否则排队 |
| 群聊没有 @ | 不问 | 只记录 | 只记录 |
| 系统提醒（看板巡视、催办等） | 不问 | 原样投递 | 原样投递（SDK 原有方式） |
| 图片、文件等非文本 | 不问 | 原样投递 | 原样投递 |

- "必须打断"指监工回答 INTERRUPT，且概率 ≥ 0.9。"无关"指监工回答 SKIP，且概率 ≥ 0.8。
- 用户的消息优先级最高：不经过监工，也不会被降级。
- 广播不会触发打断重规划，最多插入。
- 每个发送者 10 秒内最多发 1 条广播，超出的只记录。
- 同一个 Agent 同一时间只做一次判断。判断期间到的新消息，合并成下一批再判断。
- 执行动作前重新看一次目标状态。目标已经空闲，就直接处理。

## 监工用什么模型

监工只分两类，按"概率从哪来"区分：

| 类型 | 预设 | 怎么拿到概率 |
|---|---|---|
| 类 JEV 模型 | JEV、Cloudflare Clef | 服务端直接返回选项和概率 |
| 通用 LLM | 团队模型、OpenAI、自定义 | 模型只生成一个字母，用这个字母的 logprobs 算概率 |

模型不支持 logprobs 时直接报错，不改用文本猜测。分类逻辑在独立包 packages/choice-classifier 里，不依赖 JiuwenSwarm，可以单独安装。

## 怎么开启

设置 → 智能体 → A2A 双工监工：打开开关，先选类型，再选预设，然后填地址、模型和密钥。修改后重启团队。旧版配置（sdk / jev / mindshub / clef 四个后端）启动时自动转换。

## 接入方式

不改 SDK，不给 SDK 工具加参数。所有行为从一个安装入口挂到 SDK 现有的投递流程上。

- 关闭时，使用 SDK 原有的执行器和投递流程。
- 用到的 SDK 私有接口已在 0.1.19 上核对。安装前逐个检查，任何一个不匹配就全部不装，只记一条警告。

## Leader 全局停工

用户要求全员停止时，Leader 先广播暂停请求，再逐个确认成员已经停下。用户明确说恢复后，Leader 才重新组织执行。这条规则写在 Leader 的提示词里，不是后端强制停机。

## 配置项

```yaml
duplex_router:
  enabled: false  # 是否启用监工；关闭后恢复 Jiuwen 原有 Agent 间交互
  mode: "off"  # off / active
  provider: logprob_llm  # typed_choice（服务端直接返回选项概率）/ logprob_llm（普通 LLM 的 token logprobs）
  timeout_seconds: 2.0
  typed_choice:
    preset: jev  # jev / clef
    endpoint: https://api.typesafe.ai/v1/systemone  # jev：完整决策 URL；clef 按账户 ID 自动拼接
    model: jev-1.13.0  # clef 时为 clef / clef-flash
    api_key_env: DUPLEX_ROUTER_API_KEY
    account_id_env: CLOUDFLARE_ACCOUNT_ID  # 仅 clef
  logprob_llm:
    preset: team_model  # team_model / openai / custom
    model_name: ""  # team_model：团队模型池中的模型名
    api_base: ""  # openai / custom
    model: ""
    api_key_env: DUPLEX_ROUTER_API_KEY
```

## 测试

```bash
python -m pytest --no-cov -q tests/unit_tests/common/test_duplex_classifier.py tests/unit_tests/common/test_duplex_settings.py tests/unit_tests/common/test_delivery_policy.py tests/unit_tests/agentserver/test_duplex_shadow.py tests/unit_tests/agentserver/test_duplex_delivery.py tests/unit_tests/agentserver/test_team_leader_stop_prompt.py tests/integration_tests/test_duplex_e2e.py
```
