"""Meeting-only role guidance; individual tool contracts remain SDK-owned."""

from openjiuwen.harness.prompts import PromptSection

from jiuwenswarm.agents.harness.common.prompt.priority_registry import SystemPromptPriority

from .config import DEFAULT_HISTORY_LIMIT, MAX_HISTORY_LIMIT


SECTION_NAME = "meeting_publication"
_ROLE = {
    "en": (
        "A coordinated meeting is active. Its publication rules replace ordinary "
        "group_send_message and plain final replies for discussion, "
        "reports, and human questions. To publish, use meeting_post_message; its description "
        "defines the full-text and spoken-summary contract. Queued does not mean "
        "delivered: continue useful work while waiting. Consult "
        "meeting_read_history when earlier public context is missing, and "
        "meeting_get_speaker only when you need to inspect speaking ownership. "
        "Do not poll either tool. Meeting tool schemas are already supplied in this request; "
        "call them directly without searching for or reading tool-definition files. "
        "Use the message excerpts and roster already in context. Do not browse workspace "
        "directories, scope files, or history files as a startup checklist. Read additional "
        "history only when specific missing context is needed for the human's request. "
        "Use the current roster for recipients; membership "
        "is not fixed by meeting startup. Task-board events and permission approvals "
        "keep their existing control paths."
    ),
    "cn": (
        "协调会议进行中。讨论、报告和向用户提问时，会议发布规则替代普通 "
        "group_send_message 和纯文本最终回复。选择发布时使用 meeting_post_message，"
        "完整正文和口播摘要的调用契约见工具描述。排队不代表已送达，等待期间可继续工作。"
        "缺少先前公开上下文时使用 "
        "meeting_read_history，仅在需要查看发言占用时使用 meeting_get_speaker，"
        "不要轮询这些工具。当前请求已提供会议工具 schema，直接调用，不要搜索或读取工具定义文件。"
        "先用上下文已有的消息摘录和名册，不要把浏览工作区目录、scope 文件或历史文件当作启动清单。"
        "只有用户请求确实缺少特定上下文时才补读历史。根据当前名册选择接收者，会议启动不会固定成员。"
        "任务板事件和权限审批保留原有控制通道。"
    ),
}
_LEADER = {
    "en": (
        "\n\nHost the discussion using the current roster. The host has already built this Team; "
        "do not call build_team again or run a startup checklist before handling the request.\n\n"
        "When the human addresses another member:\n"
        "- Resolve the intended recipient against the current roster and use its member_name. "
        "A name merely quoted or discussed is not an instruction to contact that member.\n"
        "- Set meeting_set_addressed_members(member_names) to the intended recipients, then relay "
        "the original request with send_message(to, content), or targets for multiple recipients. "
        "Preserve the human's meaning, constraints, and requested output; add only the context "
        "the recipient needs. Do not invent extra work.\n"
        "- Ask the recipient to answer through meeting_post_message with mentions containing "
        "user and your actual Leader member_name. This lets the human hear the answer and lets "
        "you manage priority silently.\n"
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
        "也不要先执行启动清单再处理请求。\n\n"
        "用户点名其他成员时：\n"
        "- 根据当前名册确定目标，使用其 member_name；仅引用或讨论某个名字不等于要求联系该成员。\n"
        "- 先用 meeting_set_addressed_members(member_names) 设置目标，再用 send_message(to, content) "
        "转交原始请求；多人使用 targets。保留用户原意、约束和所需输出，只补充目标执行所必需的背景，"
        "不要擅自增加任务。\n"
        "- 请目标通过 meeting_post_message 答复，mentions 同时包含 user 和你实际的 Leader member_name，"
        "让用户直接听到回答，也让你能静默管理优先级。\n"
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
        "The originating agent supplies both fields in this call. Keep speech_text "
        "concise and in the same language as content, preserving conclusions, "
        "uncertainty, requested actions, and essential question details. Do not "
        "read code, tables, or URLs aloud. Mention user for a human question."
    ),
    "cn": (
        "由原始发送者在同一次调用中提供两个字段。speech_text 应简短、与 content "
        "语言一致，并保留结论、不确定性、所需行动及问题关键细节。不要朗读代码、"
        "表格或链接。向用户提问时在 mentions 中使用 user。"
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


def build_meeting_role_section(*, leader: bool) -> PromptSection:
    return PromptSection(
        name=SECTION_NAME,
        content={language: text + (_LEADER[language] if leader else "") for language, text in _ROLE.items()},
        priority=SystemPromptPriority.MEETING_PUBLICATION,
    )


def summary_contract(language: str) -> str:
    return _SUMMARY["cn" if language in {"cn", "zh"} else "en"]


def history_contract(language: str) -> str:
    return _HISTORY["cn" if language in {"cn", "zh"} else "en"]
