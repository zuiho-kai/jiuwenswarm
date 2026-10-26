# Team meeting startup and voice

Enable **Team meetings** under **Settings → Experimental**. The switch persists as
`experimental.team_meeting_enabled` in the existing configuration system and
updates the Task controls immediately. The feature is off by default.

In **Tasks**, a compact meeting icon appears beside the voice controls in Team
conversations and on `/chat/new`. One click switches a new Agent task to Cluster
(Team) mode and creates the conversation. Start sends a visible **Start meeting**
group-chat message with no mentions, using the existing chat sender and selected
model/project/equipment. It prepares the Team without asking an Agent to reply.
The browser waits for that message's SDK acceptance and the meeting coordinator
before requesting microphone access. Initialization retries within the same
startup flow reuse the message identity.

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

The SDK and dependency pins are unchanged. A new default Team starts with its
Leader; membership can grow through existing SDK tools. Existing ordinary Team
runtimes are not converted. Start a new conversation for a meeting in that case.
Ending a meeting leaves its group-chat runtime intact. This browser remembers
accepted group-chat conversations (including across tab reloads), so subsequent
typed input uses group-chat routing to the Leader as well.

The End control is disabled during startup; `meeting.end` requires an active
meeting and its exact meeting ID. Leaving the page or disconnecting uses
`meeting.release`, scoped to the connection owner and startup ID, so late cleanup
cannot affect a replacement.

The manager retains lifecycle tasks independently of request cancellation. A
cancelled or abandoned initializer must finish unwinding before its coordinator
is cleaned up and the session becomes idle. During cleanup, new starts are
rejected. A cleanup error retains the coordinator as `cleanup_failed`; another
start retries that cleanup and asks the caller to retry afterward. Server shutdown
drains meetings before closing runtime resources. This protects the running
process; recovery after a process crash is not implemented here.

The browser checks meeting status while startup is pending. Once active, it uses
existing request errors and connection-state notifications, without periodic
meeting status requests. ASR/TTS failures display errors; a disconnect stops local
capture and playback. ASR/TTS requests do not validate meeting ownership, so their
success alone does not establish that the server meeting is still active. Immediate
detection of server-only closure while the connection stays open is not provided.

Replies stay in the existing Task conversation. Human-directed member replies
and Leader final replies play in order using the configured **default voice**.
The video-duplex plugin and its ASR/TTS providers must be configured. The frontend
reuses `video.transcribe` and `tts.synthesize`; per-member matching is not wired
into that browser path yet.
There is no standalone meeting sidebar destination or second conversation editor.

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

Still pending for the full RFC PR1: server-side Silero VAD and microphone
streaming, supervisor recipient selection, JEV interruption filtering, per-agent
voice UI/browser integration, and a recorded real meeting demonstration. Speaking
locks, spoken summaries, and A2A/broadcast narration remain PR2/PR3 work.

Agent-side publication wiring is now implemented behind an internal host-consumer
gate. The shared native member provider installs scoped meeting guidance and the
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

Browser startup does not enable that gate yet: coordinated TTS delivery, browser
transport, and actual playback acknowledgements remain deferred. Browser narration
retains the behavior described above. This increment supports native in-process
members; process runtimes, Team plan mode, and unsupported rosters reject
coordinated activation. No active-meeting polling was added.

Host-enabled publication and grant scheduling now accept an optional
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
lease or start audio. Browser playback wiring and process-restart restoration
remain deferred; no registry means existing behavior is unchanged.

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
python -m pytest tests/unit_tests/agentserver/test_meeting_manager.py tests/unit_tests/agentserver/test_meeting_sdk_dependency.py tests/unit_tests/agentserver/test_team_helpers.py tests/unit_tests/common/test_team_meeting_config.py tests/unit_tests/gateway/test_app_web_handlers.py -o addopts='' -q --asyncio-mode=auto
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
