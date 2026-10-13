# Independent choice classifier

An async Python component with one public classification operation:

```python
choices = await classifier.classify(context="...", messages=["...", "..."])
# One full answer per configured question.
answer = choices["action"]
print(answer.choice, answer.probabilities, answer.confidence)
```

The package depends on `httpx` and Python 3.11+, with no JiuwenSwarm, agent-core,
native-harness, database or queue dependency. It reads only the supplied input.
The caller assembles context and applies delivery behavior.

Keep this package at `packages/choice-classifier` and install it on its own.
JiuwenSwarm integration, including when to call it and how to apply the label,
stays outside this package.

## Install and call

From the repository root:

```shell
python -m pip install ./packages/choice-classifier
```

Supply the endpoint, model and credential explicitly. The library does not read
environment variables or application configuration itself. The example below
uses caller-owned environment variables and an HTTP client for connection reuse.

```python
import asyncio
import os
import httpx
from choice_classifier import ClassifierConfig, ChoiceClassifier, TypedChoiceConfig, TypedChoiceProvider

async def main():
    config = TypedChoiceConfig(
        endpoint=os.environ["JEV_ENDPOINT"],  # Full /v1/systemone or /v1/decisions URL.
        model=os.environ["JEV_MODEL"],
        api_key=os.environ["JEV_API_KEY"],
    )
    async with httpx.AsyncClient() as client:
        classifier = ChoiceClassifier(
            TypedChoiceProvider(config, client=client),
            ClassifierConfig(timeout_seconds=2.0),
        )
        choices = await classifier.classify(
            context="The user requested a revenue report. I am calculating totals from input v1.",
            messages=["The user supplied corrected input v2. Use it for the current report."],
        )
        print(choices)

asyncio.run(main())
```

An equivalent runnable example is in `examples/classify.py`. It makes a real API
call when run with those variables set. No live API call is made during tests.

## Input and output contract

| Input | Contract |
| --- | --- |
| `context` | Nonempty text describing one recipient's relevant task, current activity, requirements and conversation. Callers may serialize structured context into this string. There is no required runtime schema. |
| `messages` | Nonempty list or tuple of nonempty strings, in caller-supplied order. The sequence is copied before awaiting the provider. |

`TypedChoiceConfig` installs the module prompt as a question named `action`. Pass `instructions` and `criteria` to replace that prompt. `TypedChoiceConfig.without_questions()` starts empty, and `add_question()` with no arguments installs the same prompt. Further calls add more named questions. Each question needs at least two criteria, and those labels do not have to be `INTERRUPT` and `APPEND`.

The output is `Decision = dict[str, ChoiceAnswer]`, with one answer per question
for the **whole batch**:

```python
@dataclass(frozen=True, slots=True)
class ChoiceAnswer:
    choice: str
    probabilities: Mapping[str, float]  # Exactly the question's criterion labels.
    confidence: float
```

Both `DecisionProvider.decide()` and `ChoiceClassifier.classify()` return this type.
This replaces the previous `dict[str, str]` result; use `result[name].choice` for
just the label. `TypedChoiceConfig` and `TypedChoiceProvider` replace the previous
`JevHttpConfig` and `JevHttpProvider` names.

For the original `action` question the labels are:

- `INTERRUPT`: the messages require changing ongoing work before it continues.
- `APPEND`: the messages can be incorporated without interrupting ongoing work.

A compatible addition or a task explicitly requested for later usually means
APPEND. A correction invalidating the current activity usually means INTERRUPT.
If context explicitly describes an idle recipient, there is no ongoing work to
interrupt, so the prompt calls for APPEND. This is a semantic judgment; the module
still calls the provider when invoked. It does not implement an idle bypass.

Use one call per recipient context. If individual messages require independent
decisions, call separately. A batch decision does not identify messages to discard
or rewrite. Caller ordering is preserved but does not establish instruction
authority. Include relevant source/role information in context or message text
when needed; sender claims alone do not establish authority.

There are no required recipient IDs, lifecycle enums, snapshot versions or native
runtime objects. Jiuwen retains its own correlation IDs and checks whether a
result still applies to the work before acting on it.

## Failures and limits

| Exception | Meaning |
| --- | --- |
| `InvalidInputError` | Wrong input type, empty message, invalid Unicode, or no questions configured. |
| `InsufficientContextError` | Context is blank. This is a subtype of InvalidInputError. |
| `InputTooLargeError` | More than 64 messages or over 32,768 UTF-8 bytes by default. |
| `InvalidResponseError` | Invalid typed answer, invalid probabilities, malformed JSON or response over the configured size. |
| `ClassifierTimeoutError` | Provider call exceeded the total deadline or had a transport timeout. |
| `ProviderError` | HTTP failure or unexpected injected-provider failure. |

All these inherit from `ClassifierError`. Invalid configuration raises `ValueError` at
construction. Caller cancellation propagates as `asyncio.CancelledError`.
Failures never become APPEND. Jiuwen chooses fallback behavior separately.

The input size counts the compact UTF-8 JSON object containing `context` and
`messages`, including its field names and escaping. It excludes the fixed prompt
and model name. Oversized input is rejected before inference, never truncated.
`ClassifierConfig` controls deadline, byte limit and message count. The deadline
includes HTTP connection setup and response streaming. Providers must cooperate
with async cancellation. The built-in HTTP provider uses a 65,536-byte decoded
response limit by default, configured through `TypedChoiceConfig`.

Blank context can be rejected mechanically. Nonempty but misleading or incomplete
context cannot be reliably detected by this interface; context quality and model
judgment remain evaluation concerns. The module has no access to hidden runtime
state to fill in missing information.

## Provider contract

`TypedChoiceProvider` uses the typed-choice wire format already used by this
repository: `model`, object `state` with `context` and `messages`, and a `questions`
object with one entry per configured question. Each entry has `type="choice"`,
`instructions`, and `criteria`. Omitting questions is rejected before the request.
`TypedChoiceConfig` sends that prompt by default: `INSTRUCTIONS` and `CRITERIA` on the `action` question. `without_questions()` sends nothing until `add_question()` adds one.

Each `answers` entry must match a requested question and contain `type="choice"`,
a `choice` that is one of that question's criteria, numeric `confidence`, and
probabilities for exactly those criteria. Probabilities must be in [0, 1], sum to
one within 1e-5, and agree with the selected choice. Additional provider metadata
is ignored.

Some hosts wrap that body in a vendor envelope, for example a REST response of
the form `{"success": true, "result": {...}}`. Pass `unwrap=` (a callable that
receives the decoded JSON and returns the inner body) to `TypedChoiceConfig` or
`LogprobLLMConfig`. It runs before validation. An exception raised by `unwrap`
becomes `InvalidResponseError` without chaining the raw body, unless it is
already a `ClassifierError`. The default `None` uses the body as is.

The validated choice, all criterion probabilities, and confidence are returned together. There is no probability threshold that
silently converts one label into another. Server confidence is preserved and range-validated independently of the winning
probability; it does not change the choice. There are no automatic retries or redirects. The module does
not log credentials, input text or raw response bodies. Injected HTTP clients stay
caller-owned; otherwise a temporary client is created and closed for each call.

An SDK-backed provider can implement `DecisionProvider` without changing callers:

```python
from choice_classifier import Decision

class MyProvider:
    async def decide(self, *, context: str, messages: tuple[str, ...]) -> Decision:
        # Call a configured provider/SDK and validate its response here.
        ...
```

Use either built-in provider through `ChoiceClassifier`, which supplies input
validation and the total deadline.

## Logprob LLM provider

`TypedChoiceProvider` supports JEV, Yale, and MCL services that share the typed-choice
wire contract. Select the service using only endpoint, model, and API key.
`LogprobLLMProvider` supports OpenAI-compatible chat completion APIs with token
logprobs. Both implement `DecisionProvider` and share answer validation.

```python
from choice_classifier import ChoiceClassifier, LogprobLLMConfig, LogprobLLMProvider

config = LogprobLLMConfig(
    endpoint="https://your-provider.example/v1",  # Base URL; /chat/completions is appended.
    model="your-logprob-capable-model",
    api_key="caller-supplied-credential",
)
classifier = ChoiceClassifier(LogprobLLMProvider(config))
```

The question configuration API is the same as `TypedChoiceConfig`, including
`without_questions()` and `add_question()`. Each question supports 2–26 criteria,
assigned letters A–Z in insertion order. Each question makes one request;
multiple questions run concurrently within the classifier's total deadline.
Requests use `max_tokens=1`, `logprobs=true`, `top_logprobs=20`, and `temperature=0`,
and no other fields by default. Vendor differences are configuration:

- `max_tokens_field="max_completion_tokens"` renames the one-token limit.
- `extra_body` adds vendor fields, for example `{"thinking": {"type": "disabled"}}`
  for services whose reasoning models would otherwise spend the single token on
  hidden reasoning and return no content logprobs. It cannot replace the fields above.

OpenAI rejects unknown fields and uses `max_completion_tokens`. Use the preset:

```python
config = LogprobLLMConfig.openai(model="gpt-4.1-mini", api_key=key)
# Reasoning-capable models return logprobs only without reasoning:
config = LogprobLLMConfig.openai(model="gpt-5.2", api_key=key, reasoning_effort="none")
```

The preset defaults to `https://api.openai.com/v1`; pass `endpoint=` for a relay.

For each letter, the provider strips token whitespace, exponentiates its logprob,
and normalizes over the returned label tokens. Whitespace variants of the same
letter contribute to the same label; absent labels receive probability zero.
The winning label becomes `choice`, and its probability becomes `confidence`.
These probabilities are conditional on the label tokens present in the returned
top 20, rather than the full model vocabulary. Missing logprobs or no matching
letters raises `InvalidResponseError`; response text is never used as a fallback.
Sibling requests are cancelled if one fails. Client ownership and response-size
limits follow the typed-choice provider's contract.

## Jiuwen integration boundary

Jiuwen decides when to invoke the classifier, assembles context, and maps answers
to runtime operations. Thresholds, routing, queues, logs, scene-specific rules,
and mindshub/clef migration belong outside this package and this PR.
`INTERRUPT` does not itself execute an abort, and `APPEND` does not select a queue.

## Validation

From the repository root, using a Python environment with this package's test extra:

```shell
python -m pip install -e "./packages/choice-classifier[test]"
python -m pytest -c packages/choice-classifier/pyproject.toml packages/choice-classifier/tests
```

Tests exercise input validation, typed-response validation, batch preservation,
logprob normalization and missing-label cases, concurrent questions, deadlines,
cancellation, HTTP failures, response limits and client ownership using
injected providers and mock HTTP transports. They do not measure model accuracy.

`examples/labeled_cases.json` contains proposed semantic cases for a separate live
evaluation, including the same update under different recipient contexts. Expected
labels and rationales are evaluation data and must not be included in model input.

On 2026-10-13, six synthetic live checks passed against the configured JEV service
using `jev-1.13.0`: default construction, no-argument `add_question()`, rewritten
criteria, custom labels, multiple questions, and an addition explicitly deferred
until later. Each returned a complete validated `ChoiceAnswer`; the observed
choices matched the expected labels. This checks API compatibility on those
examples, not benchmark accuracy. Logprob provider tests use mock responses only.

