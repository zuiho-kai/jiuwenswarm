import assert from 'node:assert/strict';
import test from 'node:test';
import { MeetingPcmCapture } from '../../../channels/web/frontend/node_modules/.cache/meeting/meetingCapture.mjs';

function host(t, addModule = async () => {}) {
  const saved = { AudioContext: globalThis.AudioContext, AudioWorkletNode: globalThis.AudioWorkletNode };
  const nodes = [], chunks = [], errors = [], stream = {};
  let context, worklet;
  const node = () => {
    const value = { gain: { value: 1 }, connect(next) { return next; }, disconnect() { this.disconnected = true; } };
    nodes.push(value);
    return value;
  };
  globalThis.AudioContext = class {
    sampleRate = 48000;
    destination = {};
    audioWorklet = { addModule };
    constructor() { context = this; }
    createMediaStreamSource(input) { this.stream = input; return node(); }
    createGain() { this.gain = node(); return this.gain; }
    async resume() { this.resumed = true; }
    async close() { this.closed = true; }
  };
  globalThis.AudioWorkletNode = class {
    constructor(_, name) {
      worklet = node();
      worklet.name = name;
      worklet.port = { close() { this.closed = true; } };
      return worklet;
    }
  };
  const capture = new MeetingPcmCapture(stream, pcm => chunks.push(pcm), () => errors.push('failed'));
  t.after(() => {
    capture.stop();
    for (const [key, value] of Object.entries(saved)) {
      if (value === undefined) delete globalThis[key];
      else globalThis[key] = value;
    }
  });
  return { capture, stream, chunks, errors, nodes, get context() { return context; }, get worklet() { return worklet; } };
}

test('capture resamples PCM and releases the graph without late callbacks', async t => {
  const h = host(t);
  await h.capture.start();
  assert.equal(h.context.stream, h.stream);
  assert.equal(h.context.gain.gain.value, 0);
  assert.ok(h.context.resumed);
  assert.equal(h.worklet.name, 'jiuwen-duplex-capture');
  const late = h.worklet.port.onmessage;
  late({ data: new Int16Array(4800).fill(1000).buffer });
  assert.equal(h.chunks[0].length, 1600);
  assert.equal(h.chunks[0][0], 1000);
  h.capture.stop();
  late({ data: new Int16Array(4800).buffer });
  assert.equal(h.chunks.length, 1);
  assert.ok(h.context.closed && h.worklet.port.closed && h.nodes.every(node => node.disconnected));
});

test('cancellation during initialization prevents late capture', async t => {
  let ready;
  const h = host(t, () => new Promise(resolve => { ready = resolve; }));
  const start = h.capture.start();
  h.capture.stop();
  ready();
  await start;
  assert.equal(h.worklet, undefined);
  assert.ok(h.context.closed);
});

test('worklet loading failure releases the context', async t => {
  const h = host(t, async () => { throw new Error('unavailable'); });
  await assert.rejects(h.capture.start(), /unavailable/);
  assert.ok(h.context.closed);
});

test('processor failure is reported once and ignored after stop', async t => {
  const next = host(t);
  await next.capture.start();
  const late = next.worklet.onprocessorerror;
  late();
  next.capture.stop();
  late();
  assert.deepEqual(next.errors, ['failed']);
});
