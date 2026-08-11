import assert from 'node:assert/strict';
import test from 'node:test';

import { encodePcm16MonoWav } from '../node_modules/.cache/audio-wav/components/VideoLivePanel/audioWav.js';

test('encodes stereo samples as a mono PCM16 WAV', async () => {
  const channels = [new Float32Array([1, -1, 0.5, -0.5]), new Float32Array([1, -1, -0.5, 0.5])];
  const wav = encodePcm16MonoWav({
    numberOfChannels: channels.length,
    length: channels[0].length,
    sampleRate: 16_000,
    getChannelData: channel => channels[channel],
  });
  const bytes = Buffer.from(await wav.arrayBuffer());

  assert.equal(wav.type, 'audio/wav');
  assert.equal(bytes.toString('ascii', 0, 4), 'RIFF');
  assert.equal(bytes.toString('ascii', 8, 12), 'WAVE');
  assert.equal(bytes.readUInt16LE(22), 1);
  assert.equal(bytes.readUInt32LE(24), 16_000);
  assert.equal(bytes.readUInt16LE(34), 16);
  assert.equal(bytes.readUInt32LE(40), 8);
  assert.equal(bytes.readInt16LE(44), 0x7fff);
  assert.equal(bytes.readInt16LE(46), -0x8000);
  assert.equal(bytes.readInt16LE(48), 0);
  assert.equal(bytes.readInt16LE(50), 0);
});

test('downsamples browser audio to 16 kHz', async () => {
  const samples = new Float32Array(48_000);
  const wav = encodePcm16MonoWav({
    numberOfChannels: 1,
    length: samples.length,
    sampleRate: 48_000,
    getChannelData: () => samples,
  });
  const bytes = Buffer.from(await wav.arrayBuffer());

  assert.equal(bytes.readUInt32LE(24), 16_000);
  assert.equal(bytes.readUInt32LE(40), 32_000);
  assert.equal(bytes.length, 44 + 32_000);
});
