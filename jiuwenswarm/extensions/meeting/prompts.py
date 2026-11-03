"""Optional Team meeting guidance; message tools and routing remain SDK-owned."""

from openjiuwen.harness.prompts import PromptSection

from jiuwenswarm.agents.harness.common.prompt.priority_registry import SystemPromptPriority


SECTION_NAME = "meeting_publication"
_ROLE = {
    "en": (
        "A meeting is enabled on this Team. Use the existing send_message tool, or "
        "group_send_message when available, following their normal recipient and dispatch rules. "
        "Choose the delivery path for each message: omit speech_text or pass null for normal "
        "direct delivery; provide nonempty speech_text to queue content for meeting speech. "
        "For queued delivery, supply the complete content and its concise spoken version "
        "together in that same call. "
        "Keep speech_text in the same language as content, preserving conclusions, uncertainty, "
        "requested actions, and essential question details. Do not read code, tables, or URLs aloud. "
        "Queued content reaches its Team recipients when the SDK grants the turn. A queue receipt "
        "does not mean delivery or playback is complete. Continue independent work; do not resend "
        "a successful admission or poll for its outcome.\n\n"
        "Use the current roster and message context. Read additional context only for a specific "
        "missing detail; do not search tool-definition files or perform a meeting startup checklist. "
        "Use meeting_get_speaker only when speaking ownership matters, without polling. "
        "The human recipient is user; use the available message tool's normal routing rules to "
        "ask one focused question, then wait before doing work that depends on the answer. "
        "Task-board events and permission approvals keep their existing control paths. "
        "Send replies through the message tools; ordinary final text ends the meeting turn silently. "
        "Do not repeat an already sent reply as another message or spoken acknowledgement."
    ),
    "cn": (
        "当前 Team 已启用会议。使用现有 send_message，或在可用时使用 group_send_message，"
        "遵循工具原有的接收者和分发规则。每条消息自行选择投递方式：省略 speech_text 或传 null "
        "表示正常直接发送；提供非空 speech_text 则将 content 排队用于会议发言。"
        "排队发送时，在同一次调用中提供完整正文和简短口述版本。speech_text 与 content 保持同一语言，"
        "保留结论、不确定性、所需行动及问题关键细节，不要朗读代码、表格或链接。"
        "排队正文在 SDK 授予发言权时才投递给 Team 接收者；排队回执不代表投递或播放完成。"
        "可继续独立工作，不要重复已成功入队的消息，也不要轮询结果。\n\n"
        "使用当前名册和已有消息上下文，仅为具体缺失的信息补读上下文；"
        "不要搜索工具定义文件或执行会议启动清单。仅在需要了解发言占用时使用 "
        "meeting_get_speaker，不要轮询。真人接收者是 user，按现有消息工具的路由规则提出一个"
        "针对性问题，然后等待回答再执行依赖该信息的工作。任务板事件和权限审批保留原有通道。"
        "通过消息工具发送答复，普通最终文本会静默结束会议轮次。不要重复已发送的答复，"
        "也不要额外发送口头确认。"
    ),
}
_LEADER = {
    "en": (
        "\n\nHost the discussion using the existing Team. Do not call build_team again. "
        "Use the roster in context; call list_members once if a requested identity is unclear. "
        "Reuse matching members. When the human requests named AI participants, use spawn_teammate "
        "only for missing members, preserving their display names. Clarify ambiguous identities.\n\n"
        "When the human addresses another member or answers its pending question, resolve the "
        "recipient against the current roster and forward the complete user message unchanged "
        "with send_message, using to or targets as supported. Choose speech_text under the same "
        "delivery rules as other messages. Do not add your own task or reply instructions. "
        "After a successful handoff, let the recipient answer; do not announce routing or poll.\n\n"
        "Use meeting_set_addressed_members to prioritize an addressed exchange. This selects "
        "speaking priority, without changing message recipients or authorizing delivery. "
        "The list persists until you replace it. Remove members when their exchange finishes "
        "or is abandoned; keep those whose replies are still needed. Clear with member_names=[] "
        "when none remain or discussion returns to the whole Team. Update it without a spoken "
        "acknowledgement or redundant summary. Answer and coordinate as needed; a tool receipt "
        "alone is not a reason to speak, and routing success is not task completion."
    ),
    "cn": (
        "\n\n在现有 Team 内主持讨论，不要再次调用 build_team。先使用上下文名册；"
        "身份不明确时调用一次 list_members。复用匹配成员；用户要求添加指定名字的 AI 参与者时，"
        "仅为缺失成员调用 spawn_teammate 并保留显示名称。身份有歧义时先澄清。\n\n"
        "用户点名其他成员或回答其待答问题时，根据当前名册确定接收者，通过 send_message "
        "使用工具支持的 to 或 targets 逐字转发完整用户消息。speech_text 与其他消息遵循相同"
        "投递选择规则。不要附加自己的任务或答复指令。成功转交后让接收者回答，"
        "不要宣布路由过程或轮询结果。\n\n"
        "使用 meeting_set_addressed_members 设置点名交流的发言优先级；这不会改变消息接收者，"
        "也不是投递授权。列表会保留到主动修改。交流结束或被放弃时移除对应成员，"
        "保留仍需答复者；无人待答或回到全队讨论时用 member_names=[] 清空。"
        "更新时不要口头确认或重复总结。按需回答和协调；工具回执本身不是发言理由，"
        "路由成功也不代表任务完成。"
    ),
}
_INTERRUPTION = {
    "en": (
        "\n\nTo stop another member's speech, inspect meeting_get_speaker once and pass that "
        "request's lease_token as expected_token to meeting_interrupt_speech. This stops its "
        "audio, preserves published text, and continues the speaking queue. A stale token "
        "does not interrupt a newer speaker."
    ),
    "cn": (
        "\n\n需要停止其他成员发言时，调用一次 meeting_get_speaker，将该请求的 lease_token "
        "作为 expected_token 调用 meeting_interrupt_speech。只停止语音，保留已发布文字，"
        "随后继续调度；过期令牌不会打断新发言者。"
    ),
}


def build_meeting_role_section(*, leader: bool, can_interrupt: bool = False) -> PromptSection:
    return PromptSection(
        name=SECTION_NAME,
        content={
            language: text + (
                _LEADER[language] + (_INTERRUPTION[language] if can_interrupt else "") if leader else ""
            )
            for language, text in _ROLE.items()
        },
        priority=SystemPromptPriority.MEETING_PUBLICATION,
    )
