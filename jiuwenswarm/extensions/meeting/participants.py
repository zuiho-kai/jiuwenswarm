"""Native member mounting through the same capability-gated rail path as Plan/Avatar."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import SQLAlchemyError

from openjiuwen.agent_teams.meeting import MeetingCoordinator
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ModelCallInputs
from openjiuwen.harness.rails.base import DeepAgentRail

from .publication import MeetingPublicationRail, create_meeting_publication_rail

if TYPE_CHECKING:
    from openjiuwen.agent_teams.schema.meeting import SpeechRequest
    from jiuwenswarm.common.voice_matching import MemberVoiceRegistry, VoiceAssignment

_POLICY = "_meeting_member_publication_policy"
logger = logging.getLogger(__name__)


class MeetingPublicationSession:
    """A host-enabled scope, created only after a consumer can wake on admission."""

    def __init__(
        self, coordinator: MeetingCoordinator, on_queued: Callable[[], None], *,
        voices: MemberVoiceRegistry | None = None,
        on_addressed_members: Callable[[list[str]], Awaitable[None]] | None = None,
        on_interrupt=None,
    ) -> None:
        if not callable(on_queued):
            raise ValueError("A meeting publication consumer wake-up callback is required")
        if voices is not None and (
            voices.session_id != coordinator.scope.session_id or voices.team_name != coordinator.scope.team_name
        ):
            raise ValueError("Voice registry belongs to a different Team/session")
        self.coordinator = coordinator
        self.on_queued = on_queued
        self.voices = voices
        self.on_addressed_members = on_addressed_members
        self.on_interrupt = on_interrupt
        self.closed = False
        self._members: set[MeetingPublicationRail] = set()

    def voice_for_speech(self, request: SpeechRequest) -> VoiceAssignment:
        """Resolve a granted request's canonical speaker to its immutable voice.

        A transport consumer passes provider_voice_id to TTS on Gateway. This
        lookup performs no synthesis and never changes queue/lease ownership.
        """
        if self.closed or request.scope != self.coordinator.scope or self.voices is None:
            raise ValueError("Speech voice requires the matching active publication scope")
        return self.voices.get_voice(request.member_name)

    async def attach(self, agent: Any, member_name: str, language: str) -> MeetingPublicationRail:
        if self.closed:
            raise ValueError("Meeting publication scope has ended")
        if self.voices is not None:
            self.voices.assign_voice(member_name, language=language)
        policy = await create_meeting_publication_rail(
            self.coordinator, member_name, language=language, on_queued=self.on_queued,
            on_addressed_members=self.on_addressed_members,
            on_interrupt=self.on_interrupt,
        )
        if self.closed:
            raise ValueError("Meeting publication scope has ended")
        policy.init(agent)
        self._members.add(policy)
        return policy

    def detach(self, policy: MeetingPublicationRail) -> None:
        self._members.discard(policy)
        if policy._agent is not None:
            policy.uninit(policy._agent)

    def close(self) -> None:
        self.closed = True
        for policy in tuple(self._members):
            self.detach(policy)


def _resolve_session(session_id: str, team_name: str) -> MeetingPublicationSession | None:
    # Do not construct a TeamManager or a meeting manager for ordinary messages.
    from .meeting_manager import get_active_publication

    return get_active_publication(session_id, team_name)


class MeetingParticipantRail(DeepAgentRail):
    """Always mounted, inert until this exact session has a host-enabled scope.

    Providers apply this to both the Leader and newly constructed native members.
    Per-invocation policy references survive tool-context copies and prevent late
    calls from falling back to unrestricted sending after meeting closure.
    """

    priority = 4

    def __init__(
        self, session_id: str, team_name: str, member_name: str, *, language: str = "en",
        resolve_session: Callable[[str, str], MeetingPublicationSession | None] = _resolve_session,
    ) -> None:
        super().__init__()
        self._session_id = session_id
        self._team_name = team_name
        self._member_name = member_name
        self._language = language
        self._resolve = resolve_session
        self._agent: Any = None
        self._session: MeetingPublicationSession | None = None
        self._policy: MeetingPublicationRail | None = None
        self.voice_assignment: VoiceAssignment | None = None

    def init(self, agent: Any) -> None:
        self._agent = agent
        session = self._resolve(self._session_id, self._team_name)
        if session is not None and session.voices is not None:
            self.voice_assignment = session.voices.assign_voice(self._member_name, language=self._language)

    def uninit(self, agent: Any) -> None:
        if self._session is not None and self._policy is not None:
            self._session.detach(self._policy)
        self._session = None
        self._policy = None
        self._agent = None
        self.voice_assignment = None

    @staticmethod
    def _stop(ctx: AgentCallbackContext) -> None:
        ctx.request_force_finish({
            "output": "Meeting publication is unavailable or its scope has ended.",
            "result_type": "error", "meeting_error": "meeting_unavailable",
        })

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        previous = ctx.extra.get(_POLICY)
        session = self._resolve(self._session_id, self._team_name)
        if previous is not None and (not previous.active or session is not self._session):
            self._stop(ctx)
            return
        if self._session is not session:
            if self._session is not None and self._policy is not None:
                self._session.detach(self._policy)
            self._session, self._policy = session, None
        if session is None:
            return
        try:
            if session.voices is not None:
                self.voice_assignment = session.voices.assign_voice(self._member_name, language=self._language)
            if self._policy is None:
                self._policy = await session.attach(self._agent, self._member_name, self._language)
                if isinstance(ctx.inputs, ModelCallInputs) and not ctx.extra.get("_finalizing_max_iterations"):
                    # Append only the new tools: never restore an unrelated tool
                    # that an earlier permission/mode rail has already filtered.
                    tools = list(ctx.inputs.tools or [])
                    names = {tool.name for tool in tools}
                    tools.extend(
                        tool for tool in await ctx.agent.ability_manager.list_tool_info()
                        if tool.name in self._policy._tools and tool.name not in names
                    )
                    ctx.inputs.tools = tools
            ctx.extra[_POLICY] = self._policy
            await self._policy.before_model_call(ctx)
        except (ValueError, RuntimeError, OSError, SQLAlchemyError) as exc:
            logger.warning("Meeting publication unavailable for session=%s member=%s: %s", self._session_id, self._member_name, exc)
            self._stop(ctx)

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        policy = ctx.extra.get(_POLICY)
        if policy is not None:
            await policy.after_model_call(ctx)

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        policy = ctx.extra.get(_POLICY)
        if policy is not None:
            # Reconcile feature-off/runtime replacement on this operation too.
            session = self._resolve(self._session_id, self._team_name)
            if session is not self._session or not policy.active:
                self._stop(ctx)
                return
            await policy.before_tool_call(ctx)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        policy = ctx.extra.get(_POLICY)
        if policy is not None:
            await policy.after_tool_call(ctx)
