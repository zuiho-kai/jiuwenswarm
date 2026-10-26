"""Only explicit meeting input selects SDK session archive storage."""

from types import SimpleNamespace

import pytest

from jiuwenswarm.extensions.meeting import startup


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(startup, "_meeting_enabled", lambda: True)


def test_ordinary_group_input_and_start_message_text_do_not_enable_meeting_storage():
    assert not startup.meeting_history_requested({}, {"body": "Start meeting"})
    assert not startup.meeting_history_requested({"meeting_mode": False}, {"body": "Start meeting"})


def test_meeting_marker_requires_enabled_feature_and_explicit_group_input(monkeypatch):
    with pytest.raises(ValueError, match="boolean"):
        startup.meeting_history_requested({"meeting_mode": "true"}, {})
    with pytest.raises(ValueError, match="group chat"):
        startup.meeting_history_requested({"meeting_mode": True}, None)
    monkeypatch.setattr(startup, "_meeting_enabled", lambda: False)
    with pytest.raises(ValueError, match="disabled"):
        startup.meeting_history_requested({"meeting_mode": True}, {})


def test_existing_ordinary_group_is_not_silently_converted():
    runtime = SimpleNamespace(team_backend=SimpleNamespace(group_chat_spec=SimpleNamespace()))
    with pytest.raises(ValueError, match="new meeting conversation"):
        startup.meeting_history_requested({"meeting_mode": True}, {}, runtime)
    runtime.team_backend._group_conversation = SimpleNamespace(history_location="session")
    assert startup.meeting_history_requested({"meeting_mode": True}, {}, runtime)


def test_spec_option_is_selected_without_changing_workspace_configuration():
    class SupportedSpec:
        model_fields = {"conversation_history_location": object()}
        conversation_history_location = None
        workspace = object()
    spec = SupportedSpec()
    workspace = spec.workspace
    startup.configure_meeting_history(spec)
    assert spec.conversation_history_location == "session" and spec.workspace is workspace
    with pytest.raises(ValueError, match="updated agent-core"):
        startup.configure_meeting_history(SimpleNamespace())
