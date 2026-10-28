# Meeting voice detection settings

Settings > Experimental > Team meetings exposes two controls for the meeting's
backend Silero v5 detector:

| Control | Default | Accepted values |
| --- | --- | --- |
| Meeting speech-start confidence | 0.5 | A number from 0 to 1 |
| Meeting start confirmation frames | 5 | A whole number from 1 to 10 |

Use **Modify**, enter a value, and **Save**. End and restart the meeting to apply
the saved values. Clearing a field restores its default.

The detector confirms speech after the configured number of qualifying frames
within the last ten frames. Each frame is 32 ms, so five consecutive qualifying
frames confirm speech in 160 ms. Lower confidence and fewer frames make speech
start more readily, with a greater chance of accepting background noise.

The values persist in `.env` as `MEETING_VAD_START_THRESHOLD` and
`MEETING_VAD_START_FRAMES`. Invalid settings are rejected when saving; malformed
values supplied directly in the environment fall back to the defaults.

These settings apply only to the backend meeting detector in
`jiuwenswarm/extensions/meeting/input_audio.py`. The browser detector and the
video-duplex feature keep their existing behavior. The meeting's 640 ms silence
timeout, minimum audio level, pre-roll, and ASR provider settings are unchanged.
