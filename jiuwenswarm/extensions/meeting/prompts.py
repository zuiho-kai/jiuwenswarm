"""Meeting-only role guidance; individual tool contracts remain SDK-owned."""

from openjiuwen.harness.prompts import PromptSection

from jiuwenswarm.agents.harness.common.prompt.priority_registry import SystemPromptPriority


SECTION_NAME = "meeting_publication"
_ROLE = {
    "en": (
        "A coordinated meeting is active. Its publication rules replace ordinary "
        "send_message/group_send_message and plain final replies for discussion, "
        "reports, and human questions. Use meeting_post_message; its description "
        "defines the full-text and spoken-summary contract. Queued does not mean "
        "delivered: continue useful work while waiting. When finished, return an "
        "empty final answer after posting every new conclusion. Consult "
        "meeting_read_history when earlier public context is missing, and "
        "meeting_get_speaker only when you need to inspect speaking ownership. "
        "Do not poll either tool. Use the current roster for recipients; membership "
        "is not fixed by meeting startup. Task-board events and permission approvals "
        "keep their existing control paths."
    ),
    "cn": (
        "协调会议进行中。讨论、报告和向用户提问时，会议发布规则替代普通 "
        "send_message/group_send_message 和纯文本最终回复。使用 meeting_post_message，"
        "完整正文和口播摘要的调用契约见工具描述。排队不代表已送达，等待期间可继续工作。"
        "结束前通过工具发布每个新结论，然后返回空的最终回复。缺少先前公开上下文时使用 "
        "meeting_read_history，仅在需要查看发言占用时使用 meeting_get_speaker，"
        "不要轮询这些工具。根据当前名册选择接收者，会议启动不会固定成员。"
        "任务板事件和权限审批保留原有控制通道。"
    ),
}
_LEADER = {
    "en": " Host the discussion and choose delegation and mentions using the evolving roster.",
    "cn": " 主持讨论，根据动态名册决定分工和 mentions。",
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


def build_meeting_role_section(*, leader: bool) -> PromptSection:
    return PromptSection(
        name=SECTION_NAME,
        content={language: text + (_LEADER[language] if leader else "") for language, text in _ROLE.items()},
        priority=SystemPromptPriority.MEETING_PUBLICATION,
    )


def summary_contract(language: str) -> str:
    return _SUMMARY["cn" if language in {"cn", "zh"} else "en"]
