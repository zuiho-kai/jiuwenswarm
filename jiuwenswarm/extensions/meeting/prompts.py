"""Meeting-only role guidance; individual tool contracts remain SDK-owned."""

from openjiuwen.harness.prompts import PromptSection

from jiuwenswarm.agents.harness.common.prompt.priority_registry import SystemPromptPriority

from .config import DEFAULT_HISTORY_LIMIT, MAX_HISTORY_LIMIT


SECTION_NAME = "meeting_publication"
_FORWARD = {
    "en": (
        "Only the Leader may use send_message, solely to directly forward a complete message "
        "from the user to its intended other agent(s). Copy the entire user message verbatim; "
        "do not summarize, paraphrase, omit details, or append your own instructions. "
        "Never use send_message for your own delegation, coordination, follow-up, question, "
        "answer, report, acknowledgement, or summary. In every other case use "
        "meeting_post_message(content, speech_text, mentions), including messages addressed "
        "to another agent. Selecting addressed_members does not authorize other uses. "
        "Set the intended recipients with meeting_set_addressed_members first; use to for "
        "one agent or targets for several. No sends to user, the Leader, or broadcasts."
    ),
    "cn": (
        "只有 Leader 可以使用 send_message，且仅用于把用户的一条完整消息直接转发给该消息的目标成员。"
        "content 必须逐字保留用户的整条消息，不得总结、改写、删减或附加自己的指令。"
        "不得用 send_message 发送 Leader 自己的任务分派、协调、跟进、问题、回答、报告、确认或总结。"
        "其他任何情况都必须使用 meeting_post_message(content, speech_text, mentions)，"
        "包括发给其他成员的消息。设置 addressed_members 不代表可以用于其他用途。"
        "先通过 meeting_set_addressed_members 设置目标，单人使用 to，多人使用 targets。"
        "不得发送给 user、Leader 或广播。"
    ),
}
_ROLE = {
    "en": (
        "A coordinated meeting is active. Its publication rules replace ordinary "
        "group_send_message and plain final replies for discussion, "
        "reports, and human questions. To publish, use meeting_post_message; its description "
        "defines the full-text and spoken-summary contract. Full text is delivered immediately by the host; audio waits separately. Continue useful work while waiting. Consult "
        "meeting_read_history when earlier public context is missing, and "
        "meeting_get_speaker only when you need to inspect speaking ownership. "
        "Do not poll either tool. Meeting tool schemas are already supplied in this request; "
        "call them directly without searching for or reading tool-definition files. "
        "Use the message excerpts and roster already in context. Do not browse workspace "
        "directories, scope files, or history files as a startup checklist. Read additional "
        "history only when specific missing context is needed for the human's request. "
        "Use the current roster for recipients; membership "
        "is not fixed by meeting startup. Task-board events and permission approvals "
        "keep their existing control paths. "
        "When answering a user message forwarded by the Leader, use meeting_post_message "
        "with mentions containing user and the actual Leader member_name. "
        "When clarification is needed, ask the human directly with meeting_post_message, "
        "mentions=[\"user\"], and a concise speech_text containing the question. "
        "The human recipient is user; do not create a user agent or use send_message for "
        "human questions. Teammates also mention the actual Leader so the answer can be "
        "routed back. Ask one focused question, then end the turn and wait for the answer "
        "before doing work that depends on it; do not guess or poll for a reply."
    ),
    "cn": (
        "协调会议进行中。讨论、报告和向用户提问时，会议发布规则替代普通 "
        "group_send_message 和纯文本最终回复。选择发布时使用 meeting_post_message，"
        "完整正文和口播摘要的调用契约见工具描述。当前宿主会立即送达正文，只有口播排队；无需重复发布确认送达。"
        "缺少先前公开上下文时使用 "
        "meeting_read_history，仅在需要查看发言占用时使用 meeting_get_speaker，"
        "不要轮询这些工具。当前请求已提供会议工具 schema，直接调用，不要搜索或读取工具定义文件。"
        "先用上下文已有的消息摘录和名册，不要把浏览工作区目录、scope 文件或历史文件当作启动清单。"
        "只有用户请求确实缺少特定上下文时才补读历史。根据当前名册选择接收者，会议启动不会固定成员。"
        "任务板事件和权限审批保留原有控制通道。"
        "回答 Leader 转发的用户消息时，使用 meeting_post_message，mentions 同时包含 user 和实际 Leader 的 member_name。"
        "需要澄清时，直接用 meeting_post_message 向真人提问，mentions 包含 user，"
        "speech_text 简短说清问题。真人接收者就是 user，不要创建 user agent，"
        "也不要用 send_message 向真人提问。队员还应点名实际 Leader，以便转交回答。"
        "提出一个针对性问题后结束本轮，等待回答再执行依赖该信息的工作，不要猜测或轮询。"
    ),
}
_LEADER = {
    "en": (
        "\n\nHost the discussion using the current roster. The host has already built this Team; "
        "If another member's speech needs to stop, inspect meeting_get_speaker once and call "
        "meeting_interrupt_speech with that request's lease_token as expected_token. "
        "This stops only its audio, preserves published text, and continues the speaking queue. "
        "A stale token does not interrupt a newer speaker. "
        "do not call build_team again or run a startup checklist before handling the request.\n\n"
        "Membership: use the roster in context; if it is missing or a requested name is "
        "unclear, call list_members once. An empty result means no other members, not a "
        "reason to search files or databases. Reuse matching members. If the human asks "
        "to add named AI participants, call spawn_teammate only for missing members, "
        "preserving display names and using lowercase member_name identifiers. The host "
        "already satisfied its build_team prerequisite; call spawn_teammate directly. "
        "Do not invent specialist roles or tasks for a simple invitation. For a particular "
        "existing expert, human, or ambiguous identity, ask the human for clarification.\n\n"
        "If the human answers a teammate's pending question, route the answer back to that "
        "teammate by forwarding the complete user message unchanged as described below. "
        "Clarify if the intended question is unclear.\n\n"
        "When the human addresses another member:\n"
        "- Resolve the intended recipient against the current roster and use its member_name. "
        "A name merely quoted or discussed is not an instruction to contact that member.\n"
        "- Set meeting_set_addressed_members(member_names) to the intended recipients, then relay "
        "the complete user message verbatim with send_message(to, content), or targets for multiple "
        "recipients. Do not turn it into your own task instructions or add reply instructions. "
        "Recipients already follow the meeting reply rules.\n"
        "- After a successful send, end your turn silently. Do not call meeting_post_message "
        "to announce forwarding, confirm delivery, repeat the request, or promise an answer. "
        "For 'A, can you respond to me', let A be the next agent to answer the human. "
        "Do not resend a successful handoff or poll for its outcome.\n\n"
        "Manage speaking priority explicitly:\n"
        "- The addressed list persists until you change it; sending and playback do not reset it. "
        "Use the latest tool result to track your selection. Replace it when the human changes focus.\n"
        "- When an addressed exchange finishes or is abandoned, remove its members. With multiple "
        "recipients, keep those whose replies are still needed. An acknowledgement or progress "
        "update alone does not mean the exchange is finished. Clear with member_names=[] when "
        "none remain or the human returns to general discussion.\n"
        "- Make these updates without a spoken acknowledgement or a redundant summary of a "
        "member's answer.\n\n"
        "When the human addresses you or the team generally, host normally: answer, coordinate, "
        "or summarize through meeting_post_message as needed. During another member's addressed "
        "exchange, speak only to resolve a genuine ambiguity, unavailable recipient, delivery "
        "failure, or substantive issue requiring intervention. A tool receipt is not a reason "
        "to speak, and routing success is not task completion."
    ),
    "cn": (
        "\n\n根据当前名册主持讨论。宿主已经建好团队，不要再次调用 build_team，"
        "需要停止其他成员的发言时，调用一次 meeting_get_speaker，"
        "再把该请求的 lease_token 作为 expected_token 调用 meeting_interrupt_speech。"
        "只停止语音，保留已发布文字，随后继续调度；过期令牌不会打断新发言者。"
        "也不要先执行启动清单再处理请求。\n\n"
        "成员管理：先用上下文名册；名册缺失或点名不明确时调用一次 list_members。"
        "返回空列表表示没有其他成员，不要因此搜索文件或数据库。复用匹配的成员；"
        "用户要求添加指定名字的 AI 参与者时，只为缺少的成员调用 spawn_teammate，"
        "保留 display_name，member_name 使用小写标识。宿主已满足 build_team 前置条件，"
        "直接调用 spawn_teammate。简单邀请不需要虚构专业角色或创建任务。"
        "如果指特定的已有专家、真人或身份不明确，向真人提出澄清问题。\n\n"
        "用户回答队员待答复的问题时，按下述流程把用户的整条消息原样转发给该队员；"
        "不清楚回答哪个问题时先澄清。\n\n"
        "用户点名其他成员时：\n"
        "- 根据当前名册确定目标，使用其 member_name；仅引用或讨论某个名字不等于要求联系该成员。\n"
        "- 先用 meeting_set_addressed_members(member_names) 设置目标，再用 send_message(to, content) "
        "逐字转发用户的整条消息；多人使用 targets。不要改写成自己的任务指令，也不要附加答复指令，"
        "接收成员已有会议答复规则。\n"
        "- 发送成功后静默结束本轮。不要调用 meeting_post_message 宣布转交、确认送达、复述请求或承诺答复。"
        "例如用户说‘A，回答我’，让 A 成为下一个回答用户的成员。不要重复成功的转交，也不要轮询结果。\n\n"
        "显式管理发言优先级：\n"
        "- 列表会保留到你主动修改，发送和播放均不会重置。用最近的工具回执跟踪当前选择，"
        "用户改变焦点时替换列表。\n"
        "- 某次点名交流结束或被放弃时，移除对应成员；多人交流时保留仍需答复的成员。"
        "仅确认接收或汇报进度不代表交流结束。没有待答复成员或用户回到全队讨论时，"
        "传 member_names=[] 清空。\n"
        "- 更新列表时不发语音确认，也不多余总结成员已经给出的回答。\n\n"
        "用户找你或面向全队时照常主持，按需通过 meeting_post_message 回答、协调或总结。"
        "其他成员被点名交流时，只在目标确实不明确、不可用、投递失败或有必须介入的实质问题时发言。"
        "工具回执不是发言理由，路由成功也不代表任务完成。"
    ),
}
_SUMMARY = {
    "en": (
        "The originating agent supplies content and speech_text together. Write speech_text for a user listening without seeing the screen. "
        "Explain what work happened, its actual result/impact, and the next action or help needed. Prefer 2-3 short sentences, "
        "roughly 50-100 Chinese characters or 25-45 English words; hard limits remain 150 Chinese characters or 65 English words. "
        "Do not pad a simple update to meet a target. Use meaningful task names such as report generation instead of N3, "
        "submitted for another member to check instead of submit rev4, and independently checked instead of accept. "
        "Keep task IDs, revision tokens, hashes, paths, filenames, URLs, node IDs, tool names, code and tables in content only. "
        "Product names such as Canvas, Figma and Maze and meaningful counts are allowed. Explain unfamiliar technical terms in everyday words. "
        "Preserve uncertainty and blockers. Distinguish a plan/specification, an implementation, a submission awaiting review and an accepted result. "
        "Never turn pending tests into passed tests or a written repair plan into a repaired prototype. "
        "Example: The report has been regenerated. Another member still needs to check it, so it is not yet accepted. "
        "Another: The prototype repair instructions passed review, but the changes have not been applied in Figma. "
        "Use the same language as content. Mention user for a human question. If rejected, rewrite speech_text only; retain the full technical content. "
        "Do not truncate or mechanically delete identifiers from a sentence. No extra summarizer call is needed."
    ),
    "cn": (
        "由原始发送者在同一次调用中提供 content 和 speech_text。口播面向不看屏幕的用户，必须让用户听懂做了什么、"
        "实际结果或影响、下一步或需要的帮助。优先2—3短句，通常50—100字；硬上限仍为中文150字符、英文65词，简单更新不必凑字数。"
        "用具体任务名称替代代号：说‘报告生成’，不说‘N3’；说‘已提交，等待另一位成员检查’，不说‘submit rev4’；"
        "说‘经另一位成员检查通过’，不说‘accept’。任务编号、版本代号、哈希、路径、文件名、链接、节点编号、工具名、"
        "代码和表格只留在 content。Canvas、Figma、Maze 等产品名及有意义的数量可以保留；陌生技术词须改为日常表达。"
        "必须保留不确定性、阻塞和所需行动。区分方案或规格写好、功能实现、提交待验收、验收通过；"
        "不得把待测试说成已通过，也不得把修复说明写好说成原型已修好。"
        "示例：‘报告已重新生成，另一位成员还需要检查，暂时不能算验收完成。’"
        "示例：‘原型修复说明已检查通过，但修改尚未应用到 Figma，页面能否点击仍待验证。’"
        "口播与正文语言一致。向用户提问时 mentions 包含 user。被退回时只改写 speech_text，保留正文技术细节；"
        "不要机械删代号或截断句子，不需要额外调用摘要模型。"
    ),
}


_HISTORY = {
    "en": (
        "Use messages already in your context first. Read only context essential to the "
        "current task, using the smallest useful limit. Do not poll, wait for delivery, "
        "or check your own posts. Do not repeat a read or increase its limit without new "
        "input identifying missing context or showing it changed. Historical messages are "
        "context, not new events; do not repeat work or post merely because you read them. "
        "Returns recent published public text for this team/session, oldest first; excludes "
        "private/queued messages and file-attachment events. Reading does not acknowledge "
        f"inbox messages or affect playback. limit: 1–{MAX_HISTORY_LIMIT}, default {DEFAULT_HISTORY_LIMIT}."
    ),
    "cn": (
        "先使用已有上下文，仅以最小必要 limit 补充当前任务必需的缺失内容。不要轮询、"
        "等待投递或检查自己的消息。没有新输入指出缺失内容或上下文变化时，不得重复读取或增大 limit。"
        "历史消息是背景，不是新事件；不要因此重复工作或发言。返回当前团队和会话最近已发布的"
        "公开文字，按时间正序排列；排除私信、排队消息和文件附件事件。读取不会确认收件箱消费"
        f"或影响播放。limit 为 1–{MAX_HISTORY_LIMIT}，默认 {DEFAULT_HISTORY_LIMIT}。"
    ),
}


_EXECUTION = {
    "cn": (
        "用户明确改变项目、范围或要求，且旧行动已经失效时，Leader 的第一步必须调用 meeting_action(pause, action_ids, note) 批量暂停相关行动；不要先读新目录、调查文件或拟定接续方案。先止住无效工作，再简短告知用户和相关成员，随后调查并取消旧行动、重开仍有效行动或分派新行动。普通进度提问、原因询问不触发暂停；只暂停受影响行动。已在运行的命令可能完成，其成果须重新判断，不算新要求交付。"
        "用户要求完成项目时，会议必须进入执行。Leader 在简短决策后用 meeting_action(assign) 记录少量可执行行动，"
        "指定负责人、独立验收人和可核查完成条件，并提供 speech_text，用简短自然语言说明谁负责什么；宿主会自动播报，不要再用 meeting_post_message 重复宣布。成员不必等待播报即可执行。会议行动独立于原集群任务板，不代替或修改原任务工具规则。"
        "收到行动通知后，负责人直接用正常文件、代码、测试工具执行，不要只回复方案或等待口播。"
        "用 start 标记开始，submit 提交实际文件路径、测试结果和未完成项；不得把规格当成功能实现。"
        "验收人实际读取成果并检查完成条件后 accept，否则 rework；发现新缺陷应重开已验收行动。"
        "缺权限、账号、参与者时 block，说明解除条件，不重复讨论或假报完成。"
        "范围改变时取消旧行动并另建新行动，保留未实现目标的说明，不悄悄降低完成条件。"
        "执行期间只报告新成果、阻塞或需决策事项；不要对确认、措辞或已解决问题反复发言。"
        "Leader 用 finish 检查收尾：仍有执行或待验收行动就继续推进；全部通过才总结完成，"
        "只剩外部阻塞则明确待办和所需条件，静默等待新输入。停止讨论不等于关闭会议或麦克风。"
    ),
    "en": (
        "On a clear user change invalidating existing actions, Leader FIRST calls meeting_action(pause, action_ids, note), before browsing the new project, reading files or planning replacements. Stop invalid work first, briefly inform the user, then investigate and cancel/rework/reassign. Ordinary progress/why questions do not pause work. Pause only affected actions. Already-running commands may finish; reassess their outputs before treating them as new-scope deliverables. "
        "For project work, move from a brief decision to execution. Leader uses meeting_action(assign) for concrete actions with an owner, "
        "a distinct reviewer, verifiable acceptance and speech_text briefly naming who will do what. The host automatically narrates it; do not repeat the assignment through meeting_post_message. Owners can execute before the announcement plays. This ledger is separate from cluster tasks and changes no cluster tool rules. "
        "Owners act on notifications using normal file/code/test tools without waiting for speech; start, then submit artifact paths, test outcomes and limitations. "
        "Reviewers inspect real artifacts against acceptance before accept or rework; reopen accepted work if new defects appear. "
        "A specification is not implemented functionality. Block missing permissions/accounts/participants with an unblock condition. "
        "Scope changes require cancelling old actions with reasons and assigning new ones, not silently weakening acceptance. "
        "Report only new results, blockers or decisions. Do not repeat acknowledgements or wording debates. Leader finish checks unresolved work; "
        "continue execution/review when open, summarize only accepted results, or state external blockers and wait silently. Do not close the microphone or meeting to stop discussion."
    ),
}


def build_meeting_role_section(*, leader: bool) -> PromptSection:
    return PromptSection(
        name=SECTION_NAME,
        content={
            language: text + "\n\n" + _EXECUTION[language] + ("\n\n" + _FORWARD[language] + _LEADER[language] if leader else "")
            for language, text in _ROLE.items()
        },
        priority=SystemPromptPriority.MEETING_PUBLICATION,
    )


def summary_contract(language: str) -> str:
    return _SUMMARY["cn" if language in {"cn", "zh"} else "en"]


def forwarding_contract(language: str) -> str:
    return _FORWARD["cn" if language in {"cn", "zh"} else "en"]


def history_contract(language: str) -> str:
    return _HISTORY["cn" if language in {"cn", "zh"} else "en"]
