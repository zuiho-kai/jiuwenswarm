"""Meeting-only role guidance; individual tool contracts remain SDK-owned."""

from openjiuwen.harness.prompts import PromptSection

from jiuwenswarm.agents.harness.common.prompt.priority_registry import SystemPromptPriority

from .config import DEFAULT_HISTORY_LIMIT, MAX_HISTORY_LIMIT


SECTION_NAME = "meeting_publication"
_ROLE = {
    "en": (
        "A coordinated meeting is active. Its publication rules replace ordinary "
        "send_message/group_send_message and plain final replies for discussion, "
        "reports, and human questions. To publish, use meeting_post_message; its description "
        "defines the full-text and spoken-summary contract. Queued does not mean "
        "delivered: continue useful work while waiting. Consult "
        "meeting_read_history when earlier public context is missing, and "
        "meeting_get_speaker only when you need to inspect speaking ownership. "
        "Do not poll either tool. Use the current roster for recipients; membership "
        "is not fixed by meeting startup. Task-board events and permission approvals "
        "keep their existing control paths."
    ),
    "cn": (
        "协调会议进行中。讨论、报告和向用户提问时，会议发布规则替代普通 "
        "send_message/group_send_message 和纯文本最终回复。选择发布时使用 meeting_post_message，"
        "完整正文和口播摘要的调用契约见工具描述。排队不代表已送达，等待期间可继续工作。"
        "缺少先前公开上下文时使用 "
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
