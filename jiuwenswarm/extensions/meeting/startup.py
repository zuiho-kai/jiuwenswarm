"""Meeting-only selection of SDK session-owned conversation history."""

from .meeting_manager import _meeting_enabled


def meeting_history_requested(params: dict, group_input: dict | None, runtime=None) -> bool:
    requested = params.get("meeting_mode", False)
    if type(requested) is not bool:
        raise ValueError("meeting_mode must be a boolean")
    if not requested:
        return False
    if not _meeting_enabled():
        raise ValueError("Team meetings are disabled")
    if group_input is None:
        raise ValueError("Meeting startup requires explicit group chat input")
    if runtime is not None:
        backend = getattr(runtime, "team_backend", None)
        archive = getattr(backend, "_group_conversation", None)
        spec = getattr(backend, "group_chat_spec", None)
        location = getattr(archive, "history_location", None) or getattr(spec, "conversation_history_location", None)
        if location != "session":
            raise ValueError("Conversation mode requires a new meeting conversation")
    return True


def configure_meeting_history(spec) -> None:
    if "conversation_history_location" not in getattr(type(spec), "model_fields", {}):
        raise ValueError("Meeting history requires the updated agent-core SDK")
    spec.conversation_history_location = "session"
