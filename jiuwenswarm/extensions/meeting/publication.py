"""Meeting controls and playback wake-ups alongside SDK-owned Team messaging."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from openjiuwen.agent_teams.meeting import MeetingCoordinator, create_meeting_tools
from openjiuwen.agent_teams.tools.tool_base import TeamTool
from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.foundation.tool import ToolCard, ToolExposure
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ModelCallInputs,
    ToolCallInputs,
)
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.tools.base_tool import ToolOutput

from .prompts import SECTION_NAME, build_meeting_role_section

logger = logging.getLogger(__name__)
_SEND_TOOLS = frozenset({"send_message", "group_send_message"})
_ADDRESSED = "meeting_set_addressed_members"
_STREAMING = "_meeting_previous_streaming"
_TURN = "_meeting_publication_turn"
# These runtimes do not mount the native member policy. Reject their creation
# before it has side effects instead of allowing an uncoordinated speaker.
_UNSUPPORTED_TOOLS = frozenset({
    "build_team",
    "spawn_external_cli", "spawn_bridge_agent", "spawn_human_agent", "spawn_passive_human",
    "swarmflow",
    "session_new", "session_send_message", "session_continue_queued",
})
_ORG_OBSERVATION_TOOLS = frozenset({
    "org_list_configured_teams", "org_list_expert_groups", "org_view_organization", "org_view_tasks",
    "org_get_leader_message", "org_list_leader_messages", "org_ack_leader_message",
    "org_view_child_tasks", "org_view_pending_reviews",
})


def _unsupported_tool(name: str) -> bool:
    return name in _UNSUPPORTED_TOOLS or (name.startswith("org_") and name not in _ORG_OBSERVATION_TOOLS)


@dataclass
class _PublicationTurn:
    # The SDK copies ctx.extra for parallel tool calls. Keep this one mutable
    # per-invocation value so successful admission is visible to the model hook.
    queued: bool = False


class MeetingSetAddressedMembersTool(TeamTool):
    def __init__(
        self, coordinator: MeetingCoordinator, member_name: str,
        on_addressed_members: Callable[[list[str]], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(ToolCard(
            id=f"team.{_ADDRESSED}", name=_ADDRESSED,
            description=(
                "Replace the meeting's addressed-member speaking-priority list. Leader only. "
                "Use current roster member names; pass member_names=[] to clear it. "
                "Set it for a human-addressed exchange, replace it when the focus changes, "
                "and clear it when the exchange finishes or is abandoned. The list persists "
                "until you change it; forwarding and playback do not change it automatically. "
                "This does not send a message, speak, or interrupt the current speaker."
            ),
            input_params={
                "type": "object", "properties": {
                    "member_names": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
                },
                "required": ["member_names"], "additionalProperties": False,
            },
        ))
        self.coordinator = coordinator
        self.client = coordinator.for_member(member_name)
        self.on_addressed_members = on_addressed_members

    async def invoke(self, inputs: dict, **kwargs) -> ToolOutput:
        if not isinstance(inputs, dict) or set(inputs) != {"member_names"}:
            return ToolOutput(success=False, error="Provide only member_names; use [] to clear")
        names = inputs["member_names"]
        if (
            not isinstance(names, list) or len(names) > 100
            or any(not isinstance(name, str) or not name.strip() or len(name) > 255 for name in names)
        ):
            return ToolOutput(success=False, error="member_names must be a list of at most 100 member names")
        try:
            state = await self.client.get_state()
            if state is None or not state.enabled or state.leader_member_name != self.client.member_name:
                raise ValueError("Only the active meeting Leader may change addressed members")
            names = sorted(set(names))
            for name in names:
                await self.coordinator.for_member(name).get_state()
            if not await self.coordinator.set_addressed_members(names):
                raise ValueError("Meeting has ended")
            if self.on_addressed_members is not None:
                try:
                    await self.on_addressed_members(names)
                except Exception:
                    logger.exception("Meeting addressed-member notification failed")
            return ToolOutput(success=True, data={"addressed_members": names})
        except ValueError as exc:
            return ToolOutput(success=False, error=str(exc))
        except (RuntimeError, OSError, SQLAlchemyError):
            logger.exception("Meeting addressed-member update failed")
            return ToolOutput(success=False, error="meeting_priority_update_failed")


class _ScopedTool(TeamTool):
    """Retained meeting controls stop working when their scope is removed."""

    def __init__(self, tool: TeamTool, owner: MeetingPublicationRail) -> None:
        super().__init__(deepcopy(tool.card))
        # Meeting control must remain callable without progressive discovery.
        self.card.exposure = ToolExposure.DIRECT
        self.card.set_exposure_declared(True)
        self._tool = tool
        self._owner = owner

    async def invoke(self, inputs: dict[str, Any], **kwargs: Any) -> ToolOutput:
        if not self._owner.active:
            return ToolOutput(success=False, error="This meeting publication scope has ended")
        if self.card.name == "meeting_interrupt_speech":
            if (
                not isinstance(inputs, dict) or set(inputs) != {"expected_token"}
                or not isinstance(inputs["expected_token"], str) or not inputs["expected_token"].strip()
            ):
                return ToolOutput(success=False, error="Provide the observed expected_token only")
            try:
                interruption = await self._owner.on_interrupt(self._owner._client.member_name, inputs["expected_token"])
                return ToolOutput(success=True, data=asdict(interruption) if interruption is not None else None)
            except (ValueError, RuntimeError, OSError, SQLAlchemyError) as exc:
                return ToolOutput(success=False, error=str(exc))
        return await self._tool.invoke(inputs, **kwargs)

    def render_for_llm(self, output: Any) -> str:
        return self._tool.render_for_llm(output)


class MeetingPublicationRail(DeepAgentRail):
    """Attach meeting controls without replacing SDK send tools or their schemas.

    Construct via ``create_meeting_publication_rail``. This is intentionally an
    explicit capability, not a global patch to Team tools or SDK prompts.
    """

    priority = 950

    def __init__(
        self, coordinator: MeetingCoordinator, member_name: str, *, leader: bool,
        language: str = "en", on_queued: Callable[[], None] | None = None,
        on_interrupt=None,
    ) -> None:
        super().__init__()
        self._client = coordinator.for_member(member_name)
        self._leader = leader
        self.language = language
        self.on_queued = on_queued
        self.on_interrupt = on_interrupt
        self._tools: dict[str, TeamTool] = {}
        self._agent: Any = None
        self.system_prompt_builder: Any = None
        self._bootstrap: Any = None
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
            if self._bootstrap is not None:
                self.system_prompt_builder.add_section(self._bootstrap)
                self._bootstrap = None
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
        if not self.active or state is None or not state.enabled:
            # Callback exceptions are logged by the SDK and may be swallowed.
            # Use its explicit control signal to stop before the model executes.
            ctx.request_force_finish({
                "output": "Meeting publication is unavailable or its scope has ended.",
                "result_type": "error", "meeting_error": "meeting_unavailable",
            })
            return
        if self._leader and self.system_prompt_builder is not None:
            # Group startup already built the Team. The SDK's ordinary Leader
            # policy re-adds its cold-start instruction before each model call.
            bootstrap = self.system_prompt_builder.get_section("team_bootstrap")
            if bootstrap is not None:
                self._bootstrap = bootstrap
                self.system_prompt_builder.remove_section("team_bootstrap")
        if isinstance(ctx.inputs, ModelCallInputs) and ctx.inputs.tools:
            ctx.inputs.tools = [
                tool for tool in ctx.inputs.tools if not self._blocked_tool(tool.name)
            ]

    def _blocked_tool(self, name: str) -> bool:
        return _unsupported_tool(name)

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
            "output": "", "result_type": "answer", "meeting_queued": turn.queued,
        })

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if not isinstance(ctx.inputs, ToolCallInputs):
            return
        inputs = ctx.inputs
        message = None
        if self._blocked_tool(inputs.tool_name):
            message = (
                "The host has already built this Team. Use the existing roster; do not call build_team again."
                if inputs.tool_name == "build_team" else
                "This runtime or cross-Team delivery is not supported during a coordinated native meeting."
            )
        if message is not None:
            # A model can still emit a hidden/cached tool name. Reject execution
            # rather than relying on the tool list or prompt for enforcement.
            call_id = inputs.tool_call.id
            ctx.extra.setdefault("_skip_tool_calls", {})[call_id] = True
            inputs.tool_result = ToolOutput(success=False, error=message)
            inputs.tool_msg = ToolMessage(content=message, tool_call_id=call_id)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        if not isinstance(inputs, ToolCallInputs) or inputs.tool_name not in _SEND_TOOLS or ctx.exception is not None:
            return
        result = inputs.tool_result
        if not isinstance(result, ToolOutput) or not result.success or not isinstance(result.data, dict):
            return
        # SDK send_message and group_send_message use different admission receipts.
        # Direct sends, including omitted/null speech_text, never wake playback.
        if result.data.get("type") != "queued" and result.data.get("queued") is not True:
            return
        ctx.extra.setdefault(_TURN, _PublicationTurn()).queued = True
        if self.on_queued is not None:
            try:
                self.on_queued()
            except Exception:
                # Admission already committed. Preserve the SDK receipt so the
                # caller does not retry a successful send and queue a duplicate.
                logger.exception("Meeting consumer wake-up failed after queue admission")


async def create_meeting_publication_rail(
    coordinator: MeetingCoordinator, member_name: str, *, language: str = "en",
    roster_tool: TeamTool | None = None,
    on_queued: Callable[[], None] | None = None,
    on_addressed_members: Callable[[list[str]], Awaitable[None]] | None = None,
    on_interrupt=None,
) -> MeetingPublicationRail:
    """Prepare one trusted native member; caller owns installation and removal.

    Startup does not call this yet: a grant driver and playback consumer must
    exist before live agents can be switched to queued publication.
    """
    state = await coordinator.for_member(member_name).get_state()
    if state is None or not state.enabled:
        raise ValueError("An active meeting is required for publication tools")
    rail = MeetingPublicationRail(
        coordinator, member_name, leader=member_name == state.leader_member_name,
        language=language, on_queued=on_queued, on_interrupt=on_interrupt,
    )
    tools = await create_meeting_tools(coordinator.for_member(member_name), language=language)
    if roster_tool is not None:
        tools[roster_tool.card.name] = roster_tool
    if rail._leader:
        tools[_ADDRESSED] = MeetingSetAddressedMembersTool(coordinator, member_name, on_addressed_members)
    rail._tools = {
        name: _ScopedTool(tool, rail)
        for name, tool in tools.items()
        if name != "meeting_interrupt_speech" or on_interrupt is not None
    }
    return rail
