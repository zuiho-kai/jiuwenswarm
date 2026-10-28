# Team meeting startup and voice

Enable **Team meetings** under **Settings → Experimental**. The switch persists as
`experimental.team_meeting_enabled` in the existing configuration system and
updates the Task controls immediately. The feature is off by default.

**Meeting input routing** offers **Leader** (default) and **None**. The choice is
saved in this browser and captured when the next meeting starts. Voice transcripts
mention only the Leader or have no mentions, respectively. Startup remains an
unmentioned message. This selector leaves SDK delivery and evaluation unchanged;
under the current SDK, unmentioned group messages are retained as context.

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
advances it. Stop audio, taking the microphone, or synthesis/playback failure holds
the queue and displays a persistent paused/failed state. End this meeting to start
again; seamless resume is deferred. Duplicate result receipts are idempotent,
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
upstream microphone streaming, supervisor recipient selection, JEV interruption
filtering, seamless speech resume, voice selection UI, a recorded full meeting
demonstration, and recovery after process restart.

Agent-side publication uses the host-consumer readiness gate. The shared native
member provider installs scoped meeting guidance and the
SDK's `meeting_post_message`, `meeting_read_history`, and `meeting_get_speaker`
tools for the Leader and later members. Work and code profiles use the same
provider. Native discussion output must queue before publication; unsupported
direct-delivery/runtime tools are blocked while this scope is active. Ending the
scope removes the tools and prompt. No SDK or dependency change is required.

The host grant driver is implemented with a controlled consumer boundary. Queue
admission wakes SDK grant scheduling; only an explicit matching owner playback
completion releases the lease. Publication retry preserves the original token
and message identity. Delivery/playback failures hold the turn. Meeting cleanup
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
