"""Deterministic model responses: no network or real desktop access."""

from copy import deepcopy
import uuid

from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    ToolCall,
    Model,
    ModelClientConfig,
    ModelRequestConfig,
)
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk


def create_text_response(text):
    return AssistantMessage(content=text)


def create_tool_call_response(name, arguments):
    return AssistantMessage(
        content="",
        tool_calls=[
            ToolCall(
                id=uuid.uuid4().hex, type="function", name=name, arguments=arguments
            )
        ],
    )


class MockLLMModel:
    def __init__(self):
        self.set_responses([])

    def set_responses(self, responses):
        self.responses = iter(responses)
        self.call_history = []

    async def invoke(self, messages, **kwargs):
        self.call_history.append(deepcopy(messages))
        return next(self.responses)

    async def stream(self, messages, **kwargs):
        answer = await self.invoke(messages, **kwargs)
        yield AssistantMessageChunk(
            content=answer.content, tool_calls=answer.tool_calls
        )


def scripted_model(responses):
    model = Model(
        model_client_config=ModelClientConfig(
            client_provider="OpenAI",
            api_base="http://test.invalid/v1",
            api_key="test",
        ),
        model_config=ModelRequestConfig(model="test-model"),
    )
    client = MockLLMModel()
    client.set_responses(responses)
    model._client = client
    return model, client
