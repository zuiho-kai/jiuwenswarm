# Meeting VAD model

`silero_vad_v5.onnx` is the Silero v5 ONNX model distributed with
`@ricky0123/vad-web` 0.0.30, matching the model previously used by browser meetings.
It accepts 512-sample frames at 16 kHz and keeps recurrent state per stream.

- Upstream: https://github.com/snakers4/silero-vad
- Distribution: https://www.npmjs.com/package/@ricky0123/vad-web/v/0.0.30
- SHA256: `2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f`
- License: MIT, included in `LICENSE.silero`.

Bundled as package data so Gateway does not download model weights or require
frontend dependencies at runtime. Inference uses the Python `onnxruntime` package.
