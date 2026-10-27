"""Explicit native-agent publication adapter; not enabled by meeting startup yet.

The host must supply a running coordinator and install one rail per member before
using this adapter. Browser activation waits for the grant/playback integration.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from openjiuwen.agent_teams.meeting import MeetingCoordinator, create_meeting_tools
from openjiuwen.agent_teams.tools.tool_base import TeamTool
from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ModelCallInputs,
    ToolCallInputs,
)
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.tools.base_tool import ToolOutput

from .config import DEFAULT_HISTORY_LIMIT, MAX_HISTORY_LIMIT
from .prompts import SECTION_NAME, build_meeting_role_section, history_contract, summary_contract

logger = logging.getLogger(__name__)
_POST = "meeting_post_message"
_STREAMING = "_meeting_previous_streaming"
_TURN = "_meeting_publication_turn"
# send_message includes point-to-point, multicast, and broadcast variants.
_BYPASS_TOOLS = frozenset({"send_message", "group_send_message", "ask_user_question"})
# These runtimes do not mount the native member policy. Reject their creation
# before it has side effects instead of allowing an uncoordinated speaker.
_UNSUPPORTED_TOOLS = frozenset({
    "spawn_external_cli", "spawn_bridge_agent", "spawn_human_agent", "spawn_passive_human",
    "swarmflow",
    "session_new", "session_send_message", "session_continue_queued",
})
_ORG_OBSERVATION_TOOLS = frozenset({
    "org_list_configured_teams", "org_list_expert_groups", "org_view_organization", "org_view_tasks",
    "org_get_leader_message", "org_list_leader_messages", "org_ack_leader_message",
    "org_view_child_tasks", "org_view_pending_reviews",
})
_CORRECTION = (
    "Publish your answer with meeting_post_message(content, speech_text, mentions). "
    "Generate complete text and a concise spoken summary together. "
    "Do not answer with ordinary final text."
)


def _unsupported_tool(name: str) -> bool:
    return name in _UNSUPPORTED_TOOLS or (name.startswith("org_") and name not in _ORG_OBSERVATION_TOOLS)


@dataclass
class _PublicationTurn:
    # The SDK copies ctx.extra for parallel tool calls. Keep this one mutable
    # per-invocation value so successful admission is visible to the model hook.
    queued_content: list[str] = field(default_factory=list)


class _ScopedTool(TeamTool):
    """Retained tool instances cannot publish after their adapter is removed."""

    def __init__(self, tool: TeamTool, owner: MeetingPublicationRail) -> None:
        super().__init__(deepcopy(tool.card))
        self._tool = tool
        self._owner = owner
        if self.card.name == _POST:
            self.card.description += "\n\n" + summary_contract(owner.language)
        elif self.card.name == "meeting_read_history":
            self.card.description = history_contract(owner.language)
            self.card.input_params["properties"]["limit"].update(
                maximum=MAX_HISTORY_LIMIT, default=DEFAULT_HISTORY_LIMIT,
            )

    async def invoke(self, inputs: dict[str, Any], **kwargs: Any) -> ToolOutput:
        if not self._owner.active:
            return ToolOutput(success=False, error="This meeting publication scope has ended")
        if self.card.name == "meeting_read_history" and isinstance(inputs, dict):
            limit = inputs.get("limit", DEFAULT_HISTORY_LIMIT)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_HISTORY_LIMIT:
                return ToolOutput(success=False, error=f"History limit must be an integer from 1 to {MAX_HISTORY_LIMIT}")
            # The SDK still defaults to 50; always supply the meeting's default.
            inputs = {**inputs, "limit": limit}
        result = await self._tool.invoke(inputs, **kwargs)
        if self.card.name == _POST and result.success and self._owner.on_queued is not None:
            try:
                self._owner.on_queued()
            except Exception:
                # Admission has already committed. Preserve its receipt so the
                # agent does not retry and enqueue another copy of the message.
                logger.exception("Meeting consumer wake-up failed after queue admission")
                result.data["audio_error"] = "meeting_consumer_unavailable"
        return result

    def render_for_llm(self, output: Any) -> str:
        return self._tool.render_for_llm(output)


class MeetingPublicationRail(DeepAgentRail):
    """Gate native discussion output while preserving ordinary tools on removal.

    Construct via ``create_meeting_publication_rail``. This is intentionally an
    explicit capability, not a global patch to Team tools or SDK prompts.
    """

    priority = 950

    def __init__(
        self, coordinator: MeetingCoordinator, member_name: str, *, leader: bool,
        language: str = "en", on_queued: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._client = coordinator.for_member(member_name)
        self._leader = leader
        self.language = language
        self.on_queued = on_queued
        self._tools: dict[str, TeamTool] = {}
        self._agent: Any = None
        self.system_prompt_builder: Any = None
        self.active = True

    def init(self, agent: Any) -> None:
        if self._agent is not None:
            if self._agent is not agent:
                raise ValueError("A meeting publication rail belongs to one member runtime")
            return
        if not self.active:
            raise ValueError("Cannot reinstall an ended meeting publication scope")
        builder = getattr(agent, "system_prompt_builder", None)
        if builder is None or getattr(agent, "ability_manager", None) is None:
            raise ValueError("Meeting publication requires a native agent with prompt and tool support")
        if builder.get_section(SECTION_NAME) is not None:
            raise ValueError("This member runtime already has a meeting publication scope")
        self._agent = agent
        self.system_prompt_builder = builder
        for tool in self._tools.values():
            agent.ability_manager.add_ability(tool.card, tool)
        builder.add_section(build_meeting_role_section(leader=self._leader))

    def uninit(self, agent: Any) -> None:
        self.active = False
        if self._agent is None:
            return
        for name in self._tools:
            agent.ability_manager.remove_ability(name)
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section(SECTION_NAME)
        self._agent = None
        self.system_prompt_builder = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        # The pinned native runtime streams model text before AFTER_MODEL_CALL.
        # Buffer this member's model response so ordinary final text stays hidden.
        ctx.extra[_STREAMING] = ctx.extra.get("_streaming", False)
        ctx.extra["_streaming"] = False
        ctx.extra.setdefault(_TURN, _PublicationTurn())
        try:
            state = await self._client.get_state()
        except (ValueError, RuntimeError, OSError, SQLAlchemyError):
            state = None
        if not self.active or state is None or state.closed:
            # Callback exceptions are logged by the SDK and may be swallowed.
            # Use its explicit control signal to stop before the model executes.
            ctx.request_force_finish({
                "output": "Meeting publication is unavailable or its scope has ended.",
                "result_type": "error", "meeting_error": "meeting_unavailable",
            })
            return
        if isinstance(ctx.inputs, ModelCallInputs) and ctx.inputs.tools:
            ctx.inputs.tools = [
                tool for tool in ctx.inputs.tools if tool.name not in _BYPASS_TOOLS and not _unsupported_tool(tool.name)
            ]

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        if _STREAMING in ctx.extra:
            ctx.extra["_streaming"] = ctx.extra.pop(_STREAMING)
        if ctx.exception is not None or ctx.has_force_finish_request:
            return
        if not self.active:
            ctx.request_force_finish({"output": "Meeting scope ended.", "result_type": "error"})
            return
        response = getattr(ctx.inputs, "response", None)
        if response is None or response.tool_calls:
            return
        turn = ctx.extra[_TURN]
        # A normal final answer ends the turn silently; it is not a meeting post.
        # This includes idle turns with nothing new to post.
        # Asking for another post here can duplicate an already queued message.
        ctx.request_force_finish({
            "output": "", "result_type": "answer", "meeting_queued": bool(turn.queued_content),
        })

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if isinstance(ctx.inputs, ToolCallInputs) and (
            ctx.inputs.tool_name in _BYPASS_TOOLS or _unsupported_tool(ctx.inputs.tool_name)
        ):
            # A model can still emit a hidden/cached tool name. Reject execution
            # rather than relying on the tool list or prompt for enforcement.
            inputs = ctx.inputs
            call_id = inputs.tool_call.id
            ctx.extra.setdefault("_skip_tool_calls", {})[call_id] = True
            message = (
                "This runtime or cross-Team delivery is not supported during a coordinated native meeting."
                if _unsupported_tool(inputs.tool_name) else _CORRECTION
            )
            inputs.tool_result = ToolOutput(success=False, error=message)
            inputs.tool_msg = ToolMessage(content=message, tool_call_id=call_id)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        if not isinstance(inputs, ToolCallInputs) or inputs.tool_name != _POST or ctx.exception is not None:
            return
        result = inputs.tool_result
        if isinstance(result, ToolOutput) and result.success and result.data.get("speech_request"):
            turn = ctx.extra[_TURN]
            turn.queued_content.append(result.data["message"]["content"].strip())


async def create_meeting_publication_rail(
    coordinator: MeetingCoordinator, member_name: str, *, language: str = "en",
    on_queued: Callable[[], None] | None = None,
) -> MeetingPublicationRail:
    """Prepare one trusted native member; caller owns installation and removal.

    Startup does not call this yet: a grant driver and playback consumer must
    exist before live agents can be switched to queued publication.
    """
    state = await coordinator.for_member(member_name).get_state()
    if state is None or state.closed:
        raise ValueError("An active meeting is required for publication tools")
    rail = MeetingPublicationRail(
        coordinator, member_name, leader=member_name == state.leader_member_name,
        language=language, on_queued=on_queued,
    )
    tools = await create_meeting_tools(coordinator.for_member(member_name), language=language)
    rail._tools = {
        name: _ScopedTool(tool, rail)
        for name, tool in tools.items()
        if name != "meeting_interrupt_speech"
    }
    return rail
