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


def _sources(native, plan):
    active = native.active_round
    query = active.original_query if active is not None else ""
    provider = getattr(native, "_duplex_goal_provider", None)
    if provider is not None:
        query = provider()
    current = next((item for item in plan.get("tasks", [])
                    if item.get("id") == plan.get("current_task_id")), {})
    scope = {"query": query, "goal": plan.get("goal"),
             "constraints": plan.get("constraints", [])}
    action = {"scope": scope, "task_id": plan.get("current_task_id"),
              "task": current.get("description") or current.get("content")}
    return scope, action, current, query


def current_plan(native, fallback=None, session=None):
    session = session if session is not None else getattr(native, "_session", None)
    if session is not None:
        return native.load_state(session).to_session_dict().get("task_plan") or {}
    return fallback or {}


def bind_intent(native, session):
    """Persist the committed sources which this explicit summary describes."""
    scope, action, _, _ = _sources(native, current_plan(native, session=session))
    session.update_state({"duplex_intent_sources": copy.deepcopy({
        "scope": scope, "action": action, "intent": native._duplex_intent,
    })})


def resolve_intent(native, plan):
    """A summary can enrich its source, but cannot override a changed source."""
    scope, action, current, query = _sources(native, plan)
    session = getattr(native, "_session", None)
    sources = session.get_state("duplex_intent_sources") if session is not None else None
    explicit = getattr(native, "_duplex_intent", {})
    # Old persisted summaries without provenance are not fresh by default.
    same_scope = bool(explicit and sources and sources.get("intent") == explicit
                      and sources.get("scope") == scope)
    same_action = same_scope and sources.get("action") == action
    goal = plan.get("goal") or (query if isinstance(query, str) else "")
    next_action = current.get("description") or current.get("content") or ""
    return {
        "goal": str((explicit.get("goal") if same_scope else "") or goal)[:2000],
        "next_action": str((explicit.get("next_action") if same_action else "") or next_action)[:1000],
        "current_hypothesis": explicit.get("current_hypothesis", "") if same_action else "",
        "constraints": list(explicit.get("constraints", []) if same_scope else plan.get("constraints", [])),
        "intent_source": "explicit" if same_action else "mixed" if same_scope else "committed_state",
    }


def publish_state(native, ctx, boundary):
    active = native.active_round
    if active is None or ctx.session is None:
        return
    plan = current_plan(native, session=ctx.session)
    messages = []
    context = native.react_agent.context_engine.get_context(session_id=ctx.session.get_session_id())
    if context is not None:
        messages = context.get_messages()
    # External adapters supply their authoritative, committed history.
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
        **resolve_intent(native, plan),
        "committed_output": committed,
        "boundary": boundary,
        "pending_tools": copy.deepcopy(getattr(native, "_duplex_tools", {})),
    }
    native._duplex_control_state = state
    ctx.session.update_state({"duplex_control_state": state})
