import assert from 'node:assert/strict';
import test from 'node:test';
import { MeetingVad } from '../../../channels/web/frontend/node_modules/.cache/meeting/meetingVad.mjs';

const tick = () => new Promise((resolve) => setImmediate(resolve));
function audioHost({ addModule = async () => {} } = {}) {
  const saved = Object.fromEntries(
    ['window', 'Worker', 'AudioContext', 'AudioWorkletNode'].map((key) => [key, globalThis[key]]),
  );
  const workers = [],
    contexts = [],
    captures = [],
    nodes = [];
  const node = () => {
    const value = {
      disconnected: false,
      connect(next) {
        return next;
      },
      disconnect() {
        this.disconnected = true;
      },
    };
    nodes.push(value);
    return value;
  };
  globalThis.window = globalThis;
  globalThis.Worker = class {
    messages = [];
    terminated = false;
    constructor() {
      workers.push(this);
    }
    postMessage(message) {
      this.messages.push(message);
      if (message.type === 'init') queueMicrotask(() => this.onmessage?.({ data: { type: 'ready' } }));
    }
    terminate() {
      this.terminated = true;
    }
    emit(detections, generation = this.messages.at(-1).generation) {
      this.onmessage?.({
        data: {
          type: 'detections',
          detections,
          generation,
          capturedAt: performance.now(),
        },
      });
    }
  };
  globalThis.AudioContext = class {
    sampleRate = 48000;
    closed = false;
    resumed = false;
    destination = {};
    audioWorklet = { addModule };
    constructor() {
      contexts.push(this);
    }
    createMediaStreamSource(stream) {
      this.stream = stream;
      return node();
    }
    createGain() {
      const gain = node();
      gain.gain = { value: 1 };
      this.gain = gain;
      return gain;
    }
    async resume() {
      this.resumed = true;
    }
    async close() {
      this.closed = true;
    }
  };
  globalThis.AudioWorkletNode = class {
    port = {
      onmessage: null,
      closed: false,
      close() {
        this.closed = true;
      },
    };
    disconnected = false;
    constructor(context, name) {
      this.context = context;
      this.name = name;
      captures.push(this);
    }
    connect(next) {
      return next;
    }
    disconnect() {
      this.disconnected = true;
    }
  };
  const stream = { ownedByMeeting: true };
  const detections = [],
    errors = [];
  const vad = new MeetingVad(
    stream,
    (event) => detections.push(event),
    () => errors.push('vadUnavailable'),
  );
  return {
    workers,
    contexts,
    captures,
    nodes,
    stream,
    vad,
    detections,
    errors,
    push: () => captures.at(-1).port.onmessage({ data: new Int16Array(4800).fill(1000).buffer }),
    restore() {
      vad.stop();
      for (const [key, value] of Object.entries(saved)) {
        if (value === undefined) delete globalThis[key];
        else globalThis[key] = value;
      }
    },
  };
}

test('meeting VAD reuses the microphone and feeds resampled 16kHz PCM to the shared worker', async () => {
  const h = audioHost();
  try {
    await h.vad.start();
    assert.equal(h.contexts[0].stream, h.stream);
    assert.equal(h.contexts[0].gain.gain.value, 0);
    assert.equal(h.contexts[0].resumed, true);
    assert.equal(h.captures[0].name, 'jiuwen-duplex-capture');
    h.push();
    const batch = h.workers[0].messages.at(-1);
    assert.equal(batch.type, 'audio');
    assert.equal(new Int16Array(batch.pcm).length, 1600);
    h.workers[0].emit([{ state: 'started' }]);
    assert.deepEqual(h.detections, [{ state: 'started' }]);
    const late = h.captures[0].port.onmessage;
    h.vad.stop();
    late({ data: new Int16Array(4800).buffer });
    h.workers[0].emit([{ state: 'ended' }]);
    assert.equal(h.detections.length, 1);
    assert.equal(h.workers[0].terminated, true);
    assert.equal(h.contexts[0].closed, true);
    assert.equal(h.captures[0].port.closed, true);
    assert.ok(h.nodes.every((node) => node.disconnected));
    assert.equal(h.captures[0].disconnected, true);
  } finally {
    h.restore();
  }
});

test('reset discards pending detections and resets speech history for the next recording', async () => {
  const h = audioHost();
  try {
    await h.vad.start();
    h.push();
    const oldGeneration = h.workers[0].messages.at(-1).generation;
    h.vad.reset();
    h.push();
    h.workers[0].emit([{ state: 'ended' }], oldGeneration);
    assert.deepEqual(h.detections, []);
    const next = h.workers[0].messages.at(-1);
    assert.equal(next.reset, true);
    assert.notEqual(next.generation, oldGeneration);
    h.workers[0].emit([{ state: 'started' }]);
    assert.deepEqual(h.detections, [{ state: 'started' }]);
  } finally {
    h.restore();
  }
});

test('stopping during worklet initialization closes resources without late capture', async () => {
  let ready;
  const h = audioHost({
    addModule: () =>
      new Promise((resolve) => {
        ready = resolve;
      }),
  });
  try {
    const start = h.vad.start();
    await tick();
    h.vad.stop();
    ready();
    await start;
    assert.equal(h.captures.length, 0);
    assert.equal(h.contexts[0].closed, true);
    assert.equal(h.workers[0].terminated, true);
    assert.deepEqual(h.errors, []);
  } finally {
    h.restore();
  }
});

test('worklet failure cleans up the loaded model and audio context', async () => {
  const h = audioHost({
    addModule: async () => {
      throw new Error('worklet unavailable');
    },
  });
  try {
    await assert.rejects(h.vad.start(), /worklet unavailable/);
    assert.equal(h.contexts[0].closed, true);
    assert.equal(h.workers[0].terminated, true);
  } finally {
    h.restore();
  }
});

test('capture processor failure reports VAD unavailability and stop suppresses late errors', async () => {
  const h = audioHost();
  try {
    await h.vad.start();
    const late = h.captures[0].onprocessorerror;
    late();
    assert.deepEqual(h.errors, ['vadUnavailable']);
    h.vad.stop();
    late();
    assert.equal(h.errors.length, 1);
  } finally {
    h.restore();
  }
});
