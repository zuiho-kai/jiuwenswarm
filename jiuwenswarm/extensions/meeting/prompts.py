"""Optional Team meeting guidance; message tools and routing remain SDK-owned."""

from openjiuwen.harness.prompts import PromptSection

from jiuwenswarm.agents.harness.common.prompt.priority_registry import SystemPromptPriority


SECTION_NAME = "meeting_publication"
_ROLE = {
    "en": (
        "A meeting is enabled on this Team. Use the existing send_message tool, or "
        "group_send_message when available, following their normal recipient and dispatch rules. "
        "Use content for the complete message and speech_text for its spoken version in the same call. "
        "Aim for 1-2 short sentences and roughly 50 characters in speech_text, in the same language as content. "
        "This is a soft length target; exceed it when needed to preserve the meaning. "
        "Preserve the main point, uncertainty, requested action, and essential question details. "
        "Keep detailed material in content; do not read code, tables, file paths, or URLs aloud. "
        "Queued content reaches its Team recipients when the SDK grants the turn. A queue receipt "
        "does not mean delivery or playback is complete. Continue independent work; do not resend "
        "a successful admission or poll for its outcome. "
        "Send replies through the message tools; ordinary final text ends the meeting turn silently."
    ),
    "cn": (
        "当前 Team 已启用会议。使用现有 send_message，或在可用时使用 group_send_message，"
        "遵循工具原有的接收者和分发规则。在同一次调用中，用 content 提供完整正文，"
        "用 speech_text 提供口述版本。speech_text 以 1-2 句短句、约 50 个字符为目标，与 content 保持同一语言。"
        "这是长度软性目标，必要时可超出以保留原意。"
        "保留要点、不确定性、所需行动及问题关键细节。详细内容放在 content，"
        "不要朗读代码、表格、文件路径或链接。"
        "排队正文在 SDK 授予发言权时才投递给 Team 接收者；排队回执不代表投递或播放完成。"
        "可继续独立工作，不要重复已成功入队的消息，也不要轮询结果。"
        "通过消息工具发送答复，普通最终文本会静默结束会议轮次。"
    ),
}
_LEADER = {
    "en": (
        "\n\nWhen a human message explicitly addresses another agent, identify the recipient "
        "from the current roster and forward the message with send_message, using to or targets "
        "as supported. Preserve the original message, making only changes necessary for the "
        "recipient to understand the request. Omit speech_text from that forwarding call. "
        "Before forwarding, call meeting_set_addressed_members(member_names=[...]) with the addressed "
        "agents' roster names to set speaking priority. Keep them selected while their replies are pending, "
        "replace the list when focus changes, and clear it with member_names=[] when the exchange finishes "
        "or is abandoned. A forwarding receipt alone does not finish the exchange. "
        "Let the addressed agent reply without narrating the handoff. "
        "If the intended recipient is ambiguous, ask for clarification. "
        "For messages you author yourself, include speech_text under the meeting speech rules."
    ),
    "cn": (
        "\n\n用户消息明确点名其他代理时，根据当前名册确定接收者，通过 send_message "
        "使用工具支持的 to 或 targets 转发消息。保留原始消息，仅在接收者理解请求所必需时修改。"
        "转发前，用被点名代理的名册名称调用 meeting_set_addressed_members(member_names=[...]) 设置发言优先级。"
        "回复仍待完成时保留选中状态，焦点变化时更新名单，交流结束或被放弃时用 member_names=[] 清空。"
        "仅收到转发回执不代表交流已完成。"
        "该转发调用不提供 speech_text。让被点名代理回复，不要口头宣布转交。"
        "接收者有歧义时先澄清。你自己撰写的消息必须按会议发言规则提供 speech_text。"
    ),
}
_TEAMMATE_SPEECH = {
    "en": (
        "Every send_message or group_send_message call in this meeting must include speech_text "
        "as a nonempty string alongside content, regardless of recipient or purpose. "
        "Aim for 1-2 short sentences and roughly 50 characters in speech_text, in the same language as content. "
        "This is a soft length target; exceed it when needed to preserve the meaning. "
        "Speak the core message, preserving essential uncertainty and requested actions; "
        "keep detailed material in content."
    ),
    "cn": (
        "当前会议中，每次 send_message 或 group_send_message 调用都必须在提供 content 的同时，"
        "提供非空字符串 speech_text，无论接收者或消息用途是什么。"
        "speech_text 以 1-2 句短句、约 50 个字符为目标，与 content 保持同一语言。"
        "这是长度软性目标，必要时可超出以保留原意。"
        "口述核心信息，保留必要的不确定性和所需行动；详细内容放在 content。"
    ),
}


def teammate_speech_guidance(language: str) -> str:
    """Use the same meeting speech policy in the prompt and message-tool hint."""
    return _TEAMMATE_SPEECH.get(language, _TEAMMATE_SPEECH["en"])


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
                _LEADER[language] + (_INTERRUPTION[language] if can_interrupt else "")
                if leader else "\n\n" + teammate_speech_guidance(language)
            )
            for language, text in _ROLE.items()
        },
        priority=SystemPromptPriority.MEETING_PUBLICATION,
    )
