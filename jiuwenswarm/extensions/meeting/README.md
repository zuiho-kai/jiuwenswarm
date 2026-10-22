# Team meeting frontend (PR1, first increment)

Enable **Team meetings** under **Settings → Experimental**. The switch persists as
`experimental.team_meeting_enabled` in the existing configuration system and
updates the Task controls immediately. The feature is off by default.

In **Tasks**, a compact meeting icon appears beside the voice controls in Team
conversations and on `/chat/new`. One click switches a new Agent task to Cluster
(Team) mode and creates the conversation. After confirmed speech, a pause
automatically finishes the recording and transcribes into the normal message editor;
review/edit it and use the normal Send button. Use the ordinary Team mentions to
address members. The active meeting controls also allow another recording,
stopping audio, or ending the meeting. Leaving the conversation or turning the
feature off releases the microphone and clears playback.

Server lifecycle: a start waits for the Team's normal runtime activation.
Duplicate requests reuse the reservation. Once the Team is ready, conversation
and coordinator initialization follow their SDK behavior without additional
meeting-specific deadlines.

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

Replies stay in the existing Task conversation. Human-directed member replies
and Leader final replies play in order using the configured **default voice**.
The video-duplex plugin and its ASR/TTS providers must be configured. The frontend
reuses `video.transcribe` and `tts.synthesize`; backend voice matching is deferred.
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
returns to the Record message control after ASR; it does not auto-send or restart.
Stopping or leaving the meeting releases the VAD worker, audio graph, and microphone.

Still pending for the full RFC PR1: server-side Silero VAD and microphone
streaming, supervisor recipient selection, JEV interruption filtering, per-agent
voice configuration/matching, and a recorded real meeting demonstration. Speaking
locks, spoken summaries, and A2A/broadcast narration remain PR2/PR3 work.

Validation (from `jiuwenswarm/channels/web/frontend`):

```sh
npm run test:meeting
npm run test:i18n-locales
npm run test:task-full-duplex
npm run build
```

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
