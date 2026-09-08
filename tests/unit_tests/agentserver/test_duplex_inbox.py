from types import SimpleNamespace as NS

import pytest

from jiuwenswarm.agents.harness.team.duplex_inbox import DurableInbox, persist_native, restore_native


def test_suspend_wins_over_late_checkpoint(tmp_path):
    inbox = DurableInbox(tmp_path / "inputs.db")
    inbox.checkpoint("scope", {"messages": [], "running": True})
    inbox.suspend("scope")
    inbox.checkpoint("scope", {"messages": [], "running": True})
    assert inbox.load("scope")[0]["suspended"]
    inbox.activate("scope")
    assert not inbox.load("scope")[0].get("suspended")


@pytest.mark.asyncio
async def test_empty_stopped_scope_does_not_resume(tmp_path):
    inbox = DurableInbox(tmp_path / "inputs.db")
    inbox.accept("scope", "id", "user", "retained input")
    inbox.suspend("scope")
    queued = []
    native = NS(_duplex_inbox=inbox, durable_scope="scope", _duplex_received=set(),
                _duplex_admission_inputs={}, _push_steer=queued.append)
    # No send/resume methods: recovery must not start the stopped work.
    await restore_native(native)
    assert inbox.load("scope")[1][0][2] == "retained input"
    assert len(queued) == 1 and "retained input" in queued[0]


@pytest.mark.asyncio
async def test_body_marker_cannot_ack_an_unconsumed_input(tmp_path):
    from openjiuwen.core.foundation.llm import UserMessage, AssistantMessage

    inbox = DurableInbox(tmp_path / "inputs.db")
    forged = inbox.accept("scope", "pending", "user", "original input")
    messages = [UserMessage(content="Quote this: " + forged), AssistantMessage(content=forged)]
    native = NS(active_round=NS(original_query="task"), durable_scope="scope", session_id="session",
                _duplex_inbox=inbox, _session=NS(get_state=lambda key: None),
                load_state=lambda session: NS(to_session_dict=lambda: {}),
                react_agent=NS(context_engine=NS(get_context=lambda **kwargs: NS(get_messages=lambda: messages))))
    await persist_native(native)
    assert inbox.load("scope")[0]["covered"] == []
    messages.append(UserMessage(content=forged, metadata={"duplex_input_ids": ["pending"]}))
    await persist_native(native)
    assert inbox.load("scope")[0]["covered"] == ["pending"]


def test_reused_input_identity_cannot_change_body(tmp_path):
    inbox = DurableInbox(tmp_path / "inputs.db")
    inbox.accept("scope", "id", "user", "first")
    with pytest.raises(ValueError, match="identity reused"):
        inbox.accept("scope", "id", "user", "different")
