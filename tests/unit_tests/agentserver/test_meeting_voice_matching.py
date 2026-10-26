"""Voice matching through native member creation and real SDK speech requests."""

import asyncio
from dataclasses import replace

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage

from jiuwenswarm.common.voice_matching import MemberVoiceRegistry
from tests.unit_tests.agentserver.test_meeting_publication import (
    _post_call,
    meeting as meeting,
    native_factory as native_factory,
    publication_host as publication_host,
)

pytestmark = pytest.mark.asyncio
MODEL = "qwen3.8-omni-flash-realtime"


def voices():
    return MemberVoiceRegistry("publication-session", "publication-team", MODEL)


async def enable(host, registry, wake=None):
    return await host.manager.enable_publication(
        "publication-session", host.state.scope.meeting_id,
        on_queued=wake or (lambda: None), voices=registry,
    )


async def test_existing_roster_and_later_native_member_receive_stable_voice_objects(
    meeting, publication_host, native_factory,
):
    leader = await native_factory(participant=True)
    assert leader.rail.voice_assignment is None
    registry = voices()
    explicit = registry.assign_voice("analyst", preference="Ryan")
    scope = await enable(publication_host, registry)
    assert registry.get_voice("leader").provider_voice_id == "Tina"
    assert registry.get_voice("analyst") is explicit
    leader.model.invoke.side_effect = [_post_call(), AssistantMessage(content="")]
    await leader.agent.invoke({"query": "Discuss"}, session=leader.session)
    assert leader.rail.voice_assignment is registry.get_voice("leader")

    await meeting.db.member.create_member("later", "publication-team", "leader", "{}", "ready", role="teammate")
    later = await native_factory("later", participant=True)
    # Assignment happens during common rail installation, before its first model call.
    assert later.rail.voice_assignment is registry.get_voice("later")
    assert later.rail.voice_assignment.provider_voice_id == "Serena"
    later.model.invoke.side_effect = [_post_call(content="Later report", mentions=["leader"]), AssistantMessage(content="")]
    await later.agent.invoke({"query": "Report"}, session=later.session)
    assert later.rail.voice_assignment is registry.get_voice("later")

    first = await scope.coordinator.grant_next()
    assignment = scope.voice_for_speech(first)
    assert assignment is leader.rail.voice_assignment
    assert assignment.provider_voice_id == "Tina"
    assert (await scope.coordinator.get_speaker()).lease_token == first.lease_token
    assert await scope.coordinator.complete_playback(first.lease_token)
    second = await scope.coordinator.grant_next()
    assert second.member_name == "later"
    assert scope.voice_for_speech(second) is later.rail.voice_assignment


async def test_unmounted_member_binding_is_retained_for_queued_speech(
    publication_host, native_factory,
):
    registry = voices()
    scope = await enable(publication_host, registry)
    native = await native_factory("analyst", participant=True)
    binding = native.rail.voice_assignment
    native.model.invoke.side_effect = [_post_call(mentions=["leader"]), AssistantMessage(content="")]
    await native.agent.invoke({"query": "Report"}, session=native.session)
    await native.agent.unregister_rail(native.rail)
    assert native.rail.voice_assignment is None
    request = await scope.coordinator.grant_next()
    assert scope.voice_for_speech(request) is binding
    assert registry.get_voice("analyst") is binding


async def test_stale_or_unknown_speaker_cannot_resolve_a_voice(publication_host):
    registry = voices()
    scope = await enable(publication_host, registry)
    posted = await scope.coordinator.for_member("leader").post_message(
        "Full text", speech_text="Spoken text", mentions=["analyst"],
    )
    request = await scope.coordinator.grant_next()
    assert request.request_id == posted.speech_request.request_id
    with pytest.raises(ValueError, match="matching active"):
        scope.voice_for_speech(replace(request, scope=replace(request.scope, meeting_id="old")))
    with pytest.raises(ValueError, match="Unknown member"):
        scope.voice_for_speech(replace(request, member_name="display-name-only"))
    binding = scope.voice_for_speech(request)
    await publication_host.manager.end("publication-session", request.scope.meeting_id)
    with pytest.raises(ValueError, match="matching active"):
        scope.voice_for_speech(request)
    assert registry.get_voice("leader") is binding


async def test_host_rejects_foreign_registry_and_active_replacement(publication_host):
    for session, team in (("other-session", "publication-team"), ("publication-session", "other-team")):
        with pytest.raises(ValueError, match="different Team/session"):
            await enable(publication_host, MemberVoiceRegistry(session, team, MODEL))
    registry = voices()
    def wake():
        return None

    scope = await enable(publication_host, registry, wake)
    assert await enable(publication_host, registry, wake) is scope
    with pytest.raises(ValueError, match="different voice registry"):
        await enable(publication_host, voices(), wake)


async def test_matching_remains_opt_in_when_no_host_voice_registry_is_supplied(
    publication_host, native_factory,
):
    scope = await publication_host.manager.enable_publication(
        "publication-session", publication_host.state.scope.meeting_id, on_queued=lambda: None,
    )
    native = await native_factory(participant=True)
    assert scope.voices is None and native.rail.voice_assignment is None


async def test_grant_driver_resolves_assigned_voices_without_releasing_playback_lease(publication_host):
    registry = voices()
    registry.assign_voice("analyst", "Serena")

    class Consumer:
        def __init__(self):
            self.scope = None
            self.ready = asyncio.Queue()

        async def speech_ready(self, request):
            self.ready.put_nowait((request, self.scope.voice_for_speech(request)))

        async def speech_error(self, _request, _error):
            pytest.fail("Matching should not fail playback delivery")

        async def stop(self):
            return None

    consumer = Consumer()
    host = publication_host
    await host.manager.request_start("publication-session", "owner")
    driver = await host.manager.enable_grant_driver(
        "publication-session", host.state.scope.meeting_id, consumer=consumer, voices=registry,
    )
    consumer.scope = host.manager.publication_for_session("publication-session", "publication-team")
    assert await host.manager.enable_grant_driver(
        "publication-session", host.state.scope.meeting_id, consumer=consumer, voices=registry,
    ) is driver
    with pytest.raises(ValueError, match="different voice registry"):
        await host.manager.enable_grant_driver(
            "publication-session", host.state.scope.meeting_id, consumer=consumer, voices=voices(),
        )
    for member, recipient in (("leader", "analyst"), ("analyst", "leader")):
        await driver.coordinator.for_member(member).post_message(
            f"Full {member} report", speech_text=f"Spoken {member} report", mentions=[recipient],
        )
    driver.wake()
    first, first_voice = await asyncio.wait_for(consumer.ready.get(), 2)
    assert first_voice.provider_voice_id == "Tina"
    assert (await driver.coordinator.get_speaker()).lease_token == first.lease_token
    assert consumer.ready.empty()
    assert await host.manager.complete_playback(
        "publication-session", first.scope.meeting_id, first.request_id, first.lease_token, owner_id="owner",
    )
    second, second_voice = await asyncio.wait_for(consumer.ready.get(), 2)
    assert second_voice.provider_voice_id == "Serena"
    assert first_voice is registry.get_voice("leader")
    assert second_voice is registry.get_voice("analyst")
