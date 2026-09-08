"""Publish a control view from committed public state at every safe boundary."""
import copy
from openjiuwen.core.single_agent.rail.base import AgentRail


class StatePublicationRail(AgentRail):
    priority = 1001

    def __init__(self, native):
        self.native = native

    def publish(self, ctx, boundary):
        self.native._duplex_version += 1
        publish_state(self.native, ctx, boundary)

    async def before_model_call(self, ctx):
        self.publish(ctx, "before_model_call")

    async def after_model_call(self, ctx):
        self.publish(ctx, "after_model_call")

    async def before_tool_call(self, ctx):
        call = getattr(ctx.inputs, "tool_call", None)
        if call is not None:
            card = self.native.ability_manager.get(call.name)
            self.native._duplex_tools[call.id] = {
                "name": call.name, "idempotent": bool(getattr(card, "idempotent", False)),
                "cancellable": False, "side_effects": None,
            }
        self.publish(ctx, "before_tool_call")

    async def after_tool_call(self, ctx):
        call = getattr(ctx.inputs, "tool_call", None)
        if call is not None:
            self.native._duplex_tools.pop(call.id, None)
        self.publish(ctx, "after_tool_call")

    async def after_react_iteration(self, ctx):
        self.publish(ctx, "after_react_iteration")


def publish_state(native, ctx, boundary):
    active = native.active_round
    if active is None or ctx.session is None:
        return
    plan = native.load_state(ctx.session).to_session_dict().get("task_plan") or {}
    explicit = native._duplex_intent
    current = next((item for item in plan.get("tasks", [])
                    if item.get("id") == plan.get("current_task_id")), {})
    query = active.original_query
    goal = explicit.get("goal") or plan.get("goal") or (query if isinstance(query, str) else "")
    messages = []
    context = native.react_agent.context_engine.get_context(session_id=ctx.session.get_session_id())
    if context is not None:
        messages = context.get_messages()
    # Some adapters own an authoritative external history (e.g. WebArena).
    # Use that history directly, rather than the executor's placeholder input.
    provider = getattr(native, "_duplex_context_provider", None)
    if provider is not None:
        messages = provider()
    committed = ""
    for message in reversed(messages or []):
        role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
        if role == "assistant" and isinstance(content, str) and content:
            committed = content[-2000:]
            break
    state = {
        "goal": str(goal)[:2000],
        "next_action": str(explicit.get("next_action") or current.get("description") or current.get("content") or "")[:1000],
        "current_hypothesis": explicit.get("current_hypothesis", ""),
        "constraints": list(explicit.get("constraints", [])),
        "committed_output": committed,
        "intent_source": "explicit" if explicit else "committed_state",
        "boundary": boundary,
        "pending_tools": copy.deepcopy(getattr(native, "_duplex_tools", {})),
    }
    native._duplex_control_state = state
    ctx.session.update_state({"duplex_control_state": state})
