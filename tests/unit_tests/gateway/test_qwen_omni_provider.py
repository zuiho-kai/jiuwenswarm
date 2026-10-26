"""Shared provider connections are independent of feature and session policy."""

import asyncio
from dataclasses import replace

import pytest

from jiuwenswarm.gateway.voice.providers import qwen_omni


@pytest.mark.asyncio
async def test_consumers_open_independent_connections_and_cancel_only_their_own(monkeypatch):
    connections = []

    class Connection:
        def __init__(self, url, options):
            self.url, self.options = url, options
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            self.closed = True

    def connect(url, **options):
        connection = Connection(url, options)
        connections.append(connection)
        return connection

    monkeypatch.setattr(qwen_omni.websockets, "connect", connect)
    config = qwen_omni.QwenOmniRealtimeConfig("wss://example.test/realtime", "first-key", "first-model", "Tina")
    entered = asyncio.Event()

    async def first_consumer():
        async with qwen_omni.open_qwen_omni_connection(config):
            entered.set()
            await asyncio.Future()

    first = asyncio.create_task(first_consumer())
    await asyncio.wait_for(entered.wait(), 1)
    async with qwen_omni.open_qwen_omni_connection(replace(config, api_key="second-key", model="second-model")):
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert connections[0].closed
        assert not connections[1].closed
        assert connections[0].url.endswith("model=first-model")
        assert connections[1].url.endswith("model=second-model")
        assert connections[0].options["additional_headers"] == {"Authorization": "Bearer first-key"}
        assert connections[1].options["additional_headers"] == {"Authorization": "Bearer second-key"}
    assert connections[1].closed


@pytest.mark.asyncio
async def test_invalid_configuration_never_opens_upstream(monkeypatch):
    def unexpected_connect(*_args, **_kwargs):
        pytest.fail("Invalid configuration must fail before a provider call")

    monkeypatch.setattr(qwen_omni.websockets, "connect", unexpected_connect)
    config = qwen_omni.QwenOmniRealtimeConfig("wss://example.test/realtime", "", "model", "Tina")
    with pytest.raises(ValueError, match="QWEN_OMNI_API_KEY"):
        async with qwen_omni.open_qwen_omni_connection(config):
            pytest.fail("Should not enter the upstream context")
