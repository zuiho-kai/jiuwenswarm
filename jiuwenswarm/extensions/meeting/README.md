# Team meeting startup and voice

Enable **Team meetings** under **Settings → Experimental**. The switch persists as
`experimental.team_meeting_enabled` in the existing configuration system and
updates the Task controls immediately. The feature is off by default.

**Meeting input routing** offers **Leader** (default) and **None**. The choice is
saved in this browser and captured when the next meeting starts. Voice transcripts
mention only the Leader or have no mentions, respectively. Startup remains an
unmentioned message. This selector leaves SDK delivery and evaluation unchanged;
under the current SDK, unmentioned group messages are retained as context.

With Leader routing, the Leader uses the existing `send_message` tool to relay
requests silently to human-addressed agents. There is no separate forwarding
tool. The Leader first selects those agents with
`meeting_set_addressed_members(member_names)`, then uses `send_message(to, content)`
(or its existing `targets` form) to pass on the request without speech. Silent
sends are limited to the selected agents; user replies, broadcasts, and ordinary
discussion still go through `meeting_post_message` and the speaking queue.

The priority list is controlled explicitly by the Leader: replace it when focus
changes and clear it with `[]` when the exchange ends, is abandoned, or discussion
becomes general. Sending and playback do not reset it or interrupt the current
speaker. The Leader asks the target to reply to both user and Leader so priority
can be cleared silently. No spoken handoff, delivery acknowledgement, or redundant
summary is needed. Clarification, routing failure, or a substantive issue may
require intervention. Recipient selection uses the evolving roster; automatic
name matching and supervisor inference are separate work. Startup already builds
the Team, so `build_team` is hidden and blocked during coordinated publication.

When the roster is missing or unclear, the Leader can call the SDK's
`list_members` once, reuse existing members, and create only requested missing
AI participants with `spawn_teammate`. Clarification questions use
`meeting_post_message` with `mentions=["user"]` and a spoken question, then wait
for the human's answer. Teammates also mention the Leader so the answer can be
relayed back. A separate user agent is not required.

**JEV routing for teammates** is a separate Experimental switch for the default
Team. It sets `modes.team.jiuwen_team.duplex` to agent-core's native JEV router
(`enabled: true`, `mode: active`, `format: jev`). Existing Team endpoint, model,
key-environment and timeout settings are retained; absent settings use the SDK's
TypeSafe defaults and `TYPESAFE_API_KEY`. Start a new Team conversation to use
the setting. It evaluates teammate broadcasts and busy-member delivery, and does
not enable relevance evaluation of meeting user speech. Browser JEV settings are
independent. No live JEV request is made when saving the switch.

In **Tasks**, a compact meeting icon appears beside the voice controls in Team
conversations and on `/chat/new`. One click switches a new Agent task to Cluster
(Team) mode and creates the conversation. Start sends a visible **Start meeting**
group-chat message with no mentions, using the existing chat sender and selected
model/project/equipment. It prepares the Team without asking an Agent to reply.
The browser waits for that message's SDK acceptance and the meeting coordinator
before requesting microphone access. Initialization retries within the same
startup flow reuse the message identity. The text editor, Send button, and Enter
submission stay disabled until the existing startup and playback-ready handshake
finish. The microphone starts transmitting only after that handshake.

After confirmed speech, a pause automatically finishes the recording, transcribes
it, and sends the text through normal chat with the Leader as its group-chat
recipient. There is no editor review or manual Send step. The transcript remains
visible in the conversation. The active meeting controls also allow another recording,
stopping audio, or ending the meeting. Leaving the conversation or turning the
feature off releases the microphone and clears playback.

Server lifecycle: meeting controls attach to the existing group-chat Team after
its initialization message is accepted. Duplicate controls reuse the reservation.
Conversation and coordinator initialization follow their SDK behavior. The
browser bounds waiting for message acceptance to 60 seconds and abandons that
wait on page teardown or disconnection; this adds no active-meeting polling.

Meeting input explicitly sets `meeting_mode` before the initialization message.
AgentServer selects `TeamAgentSpec.conversation_history_location="session"` for a
meeting-created runtime; its archive is under
`<team-home>/sessions/<session-id>/conversation/history.jsonl`. Ordinary group-chat
requests retain the SDK's existing shared-workspace layout. Reopening a meeting
archive remembers its location. Existing ordinary group-chat runtimes are not
silently converted; use a new meeting conversation. This requires an updated
agent-core SDK supporting the option; older SDKs fail with a specific update error.
File tools retain their current permissions, and the SDK still supplies scoped
message excerpts without granting broader filesystem access.

The dependency pin has not yet been updated. A new default Team starts with its
Leader; membership can grow through existing SDK tools. Existing ordinary Team
runtimes are not converted. Start a new conversation for a meeting in that case.
Ending a meeting leaves its group-chat runtime intact. This browser remembers
accepted group-chat conversations (including across tab reloads), so subsequent
typed input uses group-chat routing to the Leader as well.

End can cancel server preparation after conversation creation. `meeting.end`
requires the exact active meeting ID; an earlier startup uses `meeting.release`,
scoped to the connection owner and startup ID. The browser retains its shutdown
listener while cleanup is pending. Unconfirmed cleanup shows **Retry End meeting**
and prevents a replacement meeting until cleanup succeeds.

The manager retains lifecycle tasks independently of request cancellation. A
cancelled or abandoned initializer must finish unwinding before its coordinator
is cleaned up and the session becomes idle. During cleanup, new starts are
rejected. A cleanup error retains the coordinator as `cleanup_failed`; another
start retries that cleanup and asks the caller to retry afterward. Server shutdown
drains meetings before closing runtime resources. This protects the running
process; recovery after a process crash is not implemented here.

The browser checks meeting status while startup is pending. Once active, it uses
request errors and connection-state notifications without periodic meeting status
requests or automatic reconnection/resume.

After local media and event listeners are ready, `meeting.playback.ready` installs
an owner-specific Gateway route and activates native publication/grant scheduling.
AgentServer freezes each granted spoken summary, member voice, source message,
lease and playback identity. The browser calls `meeting.speech.synthesize` with
that identity and a chunk index; it cannot override text, model or voice. Gateway
resolves the canonical chunk on AgentServer, synthesizes it using the shared Qwen
Omni component, then rechecks the lease/configuration before returning WAV audio.
Credentials stay on Gateway. Qwen must be configured through `QWEN_OMNI_*`;
input uses Settings > Speech transcription (`ASR_API_BASE`, `ASR_API_KEY`, and
`ASR_MODEL_NAME`), independently of the video-duplex provider and voice settings.

Only natural playback completion of every chunk reports `meeting.playback.result`
as completed and releases the SDK speaking lease. Synthesized audio alone never
advances it as completed. A synthesis/playback error shows its failure, drains the
failed synthesis, and marks that speech interrupted before continuing to the next
queued item. Published full text remains available. Stop audio still holds the
queue in a paused state; end the meeting to start again. Duplicate result receipts are idempotent,
including after the next grant. Foreign identities and stale leases are rejected.

End revokes synthesis, sends `meeting.playback.stop`, and waits for the browser's
`meeting.playback.stopped` receipt. The stop acknowledgement handler runs without
the driver lock; Gateway drains provider cancellation before forwarding the
receipt. Missing receipts retain `cleanup_failed` for an explicit End retry.
Disconnect never fabricates successful playback. Late audio cannot restart a
locally stopped player. Automatic chat-event narration is suppressed for the owned
session, and manual TTS shares an audio-stop signal with meeting playback.

Replies remain in the existing Task conversation. There is no standalone meeting
sidebar destination or second conversation editor. This path supports native
in-process Team members; process runtimes, Team plan mode and unsupported rosters
reject coordinated activation. Video-duplex files and their imports are unchanged.

## Human and Leader interruption

After ASR, human speech during an active speaking lease is checked through the
SDK's existing duplex supervisor classifier and the Team's configured endpoint,
model, key environment and timeout. Without a configured endpoint it uses the
shared JEV defaults (`TYPESAFE_API_KEY`). The judgment concerns taking the audio
floor only: it does not reject, rewrite or reroute the human message. A confident
`INTERRUPT` decision (at least 0.9) cuts speech; acknowledgements and uncertain,
failed or timed-out judgments leave speech running and still deliver the text.
VAD alone never stops agent audio.

The Leader can inspect `meeting_get_speaker` and call
`meeting_interrupt_speech(expected_token)` with its observed lease token. Only
the Leader receives this tool, and only while the host playback consumer is
installed. A stale token cannot cut a later speaker's speech. Interruptions stop
audio only; published full text and the remaining queued requests are preserved.

Both paths revoke the matching SDK lease and send `meeting.speech.stop` with its
complete playback identity. The browser stops only that speech and keeps its
microphone active. Gateway drains synthesis cancellation before forwarding the
browser's `meeting.playback.stopped` acknowledgement. Cancelled Qwen sessions
are replaced before the next speaker. Late synthesis, duplicate speech events
and late completion receipts cannot replay or complete an interrupted speech.

The SDK interruption barrier blocks new grants until this stop is confirmed.
For human interruption it also waits for `meeting.input.admitted`, sent only
after the existing `team.group_message.accepted` event confirms public input
delivery. Duplicate admission receipts are idempotent. Failed delivery or a
missing audio stop acknowledgement keeps the barrier held; End meeting remains
the recovery action. Explicit Stop audio and audio failures retain their
existing paused/failed behavior and are distinct from semantic interruption.

Automated regressions cover the real SDK/SQLite queue, Gateway routes, native
Leader tool and browser player using controlled supervisor/audio services.
Physical microphone/speaker behavior and real supervisor classification still
require a live meeting acceptance run.

Local speech-end detection reuses the full-duplex extension's Silero v5 worker,
PCM capture worklet, and 16 kHz resampler. The existing speech gate confirms speech
before ending the recording after 640 ms of silence. Short pauses and initial
silence do not finish an utterance. Each recording resets the detector so old
speech cannot end the next recording. This is acoustic end detection, not semantic
turn completion.

Manual finish remains available. The 30-second limit still bounds recordings;
recordings with no confirmed speech are discarded at that limit. If VAD cannot
load or fails, a notice explains how to finish manually. Each completed recording
automatically submits its transcript after ASR, then returns to the Record message
control; recording does not restart automatically.
Stopping or leaving the meeting releases the VAD worker, audio graph, and microphone.

Deferred work includes semantic turn completion, server-side Silero VAD,
upstream microphone streaming, supervisor recipient selection, seamless speech resume, voice selection UI, a recorded full meeting
demonstration, and recovery after process restart.

### Ordered transcription batches

Three ASR workers transcribe concurrently with a
60-second timeout per request while listening continues. A result map restores
utterance order, with IDs starting at one per stream. At most seven utterances
can be queued, transcribing, or awaiting publication in total; overload stops
input explicitly rather than growing the buffer.

Consecutive results form one message after four utterances or 1.5 seconds from
the first consecutive ready result, whichever comes first. Later results do not
reset that deadline. An earlier pending ID waits for its ASR result or timeout;
the batch timer never skips it. Failed requests emit ordered errors, and empty
or filler-only transcripts advance the sequence without adding text. Errors
split a batch so earlier text is delivered before the error ID. Each transcript
event uses the last included utterance ID, preserving the browser's existing
ordering and admission checks. Interruption preparation runs once per message,
separately from the ASR workers. The shared Speech transcription adapter
receives WAV audio.

Agent-side publication uses the host-consumer readiness gate. The shared native
member provider installs scoped meeting guidance and the
SDK's `meeting_post_message`, `meeting_read_history`, and `meeting_get_speaker`
tools for the Leader and later members, plus the Leader-only addressed-member
and `meeting_interrupt_speech` tools. The Leader retains the existing `send_message` for silent addressed handoffs.
Work and code profiles use the same
provider. Native discussion output must queue before publication; unsupported
direct-delivery/runtime tools are blocked while this scope is active. Ending the
scope removes the tools and prompt. No SDK or dependency change is required.
Meeting tool schemas stay directly exposed on every model request, including
when progressive tool discovery is enabled. Members use the supplied message
excerpts and roster first; startup does not require browsing `prompts/tool/`,
workspace metadata, or history files. Additional history is read only to fill
specific missing context needed by the user's request.

The host grant driver is implemented with a controlled consumer boundary. Queue
admission wakes SDK grant scheduling; matching owner playback completion releases
the lease as completed. An owner-confirmed audio failure revokes that speech as
interrupted and advances after synthesis has drained. Publication retry preserves
the original token and message identity. Delivery failures and explicit audio
stops hold the turn. Meeting cleanup
drains the worker and confirms consumer stop before closing the SDK coordinator.

Publication and grant scheduling accept an optional
`MemberVoiceRegistry` from `jiuwenswarm.common.voice_matching`. The host supplies
the exact Qwen Omni model and a registry scoped to the native Team/session; no
Gateway credentials are read by the meeting runtime. Existing roster bindings
are initialized with the Leader first. The common member rail attaches an
immutable `VoiceAssignment` for later native members and refreshes existing
members before their next model call.

Matching uses explicit preference, then an unused compatible voice, then the
least-used voice with catalog order breaking ties. The packaged catalog is a
curated Mandarin/English subset in `resources/voices/qwen_omni.yaml`. Reuse the
same registry for the same native runtime to retain assignments across meetings;
create a new one when that runtime is replaced. Catalog updates affect future
assignments while retained bindings keep queued speech reproducible.
`publication.voice_for_speech(request).provider_voice_id` supplies the voice
argument for the Gateway TTS component. Lookup does not release the speaking
lease or start audio. Browser readiness now creates/reuses this registry for the
same native runtime; process-restart restoration remains deferred.

Validation (from `jiuwenswarm/channels/web/frontend`):

```sh
npm run test:meeting
npm run test:i18n-locales
npm run test:task-full-duplex
npm run build
```

Backend lifecycle validation (from the repository root, using the project
Python environment):

```sh
python -m pytest tests/unit_tests/agentserver/test_meeting_browser_playback.py tests/unit_tests/agentserver/test_meeting_manager.py tests/unit_tests/agentserver/test_meeting_sdk_dependency.py tests/unit_tests/agentserver/test_team_helpers.py tests/unit_tests/common/test_team_meeting_config.py tests/unit_tests/gateway/test_app_web_handlers.py -o addopts='' -q --asyncio-mode=auto
```

Set `JIUWENSWARM_DATA_DIR` to a temporary directory to isolate application logs
and data. On Windows, use a normal shell if sandbox restrictions stall asyncio.

For a real-model browser smoke test, run the frontend dev server and open
`/tests/fixtures/meetingVadBrowser.html`. **Run silence test** uses a generated
silent stream and checks that Silero/WASM/the audio worklet load without false
speech detection. **Run speech-end test** uses a local WAV sample at
`node_modules/.cache/meeting/vad-test-speech.wav`. To generate it on Windows,
from the frontend directory:

```powershell
Add-Type -AssemblyName System.Speech
$vadSamplePath = Join-Path (Get-Location) 'node_modules/.cache/meeting/vad-test-speech.wav'
$vadSynth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$vadSynth.SetOutputToWaveFile($vadSamplePath)
$vadSynth.Speak('This is a test of meeting speech detection. I have finished speaking.')
$vadSynth.Dispose()
```

The browser tests use synthetic audio only; they do not access a physical
microphone or call ASR/TTS providers.

The opt-in protocol smoke test uses the configured Qwen service and saves WAV
artifacts under `tmp/meeting-playback-smoke` (isolated SDK/SQLite session):

```powershell
$env:MEETING_LIVE_TTS_SMOKE='1'
python -m pytest tests/unit_tests/agentserver/test_meeting_browser_playback.py -k live_qwen -o addopts='' -q --asyncio-mode=auto
```

This checks live synthesis through RPC and push routing. It simulates playback
receipts; physical microphone/speaker quality and a complete live browser meeting
still require user acceptance testing.
