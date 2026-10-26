import assert from 'node:assert/strict';
import test from 'node:test';
import {
  QwenOmniConnection,
  QwenOmniMediaSequencer,
  createQwenOmniSessionUpdate,
  createQwenOmniTextTurnEvents,
  createQwenOmniCancelResponseEvent,
} from '../node_modules/.cache/voice-provider/voice.mjs';

function harness() {
  const sockets = [];
  class Socket {
    static OPEN = 1;
    readyState = 0;
    sent = [];
    constructor(url) {
      this.url = url.toString();
      sockets.push(this);
    }
    send(raw) {
      this.sent.push(JSON.parse(raw));
    }
    close(code, reason) {
      this.readyState = 3;
      this.closed = { code, reason };
    }
    open() {
      this.readyState = 1;
      this.onopen();
    }
    event(event) {
      this.onmessage({ data: JSON.stringify(event) });
    }
  }
  globalThis.window = globalThis;
  globalThis.WebSocket = Socket;
  return sockets;
}

function consumer(voice, instructions) {
  const connection = new QwenOmniConnection();
  const events = [],
    errors = [],
    closes = [];
  const options = {
    url: 'ws://example.test/ws/video/qwen-omni',
    sessionUpdate: createQwenOmniSessionUpdate({
      voice,
      instructions,
      inputRate: 16000,
      outputRate: 24000,
      turnDetection: null,
      inputAudioTranscription: false,
    }),
    onEvent: (event) => events.push(event),
    onError: (error) => errors.push(error),
    onClose: () => closes.push(true),
  };
  return { connection, options, events, errors, closes };
}

test('independent consumers share provider code without prompts, voices or cancellation leaking', async () => {
  const sockets = harness();
  const first = consumer('Tina', 'First feature instructions');
  const second = consumer('Serena', 'Second feature instructions');
  const opened = [first.connection.open(first.options), second.connection.open(second.options)];
  sockets.forEach((socket) => socket.open());
  await Promise.all(opened);
  assert.equal(sockets[0].sent[0].session.voice, 'Tina');
  assert.equal(sockets[1].sent[0].session.voice, 'Serena');
  assert.equal(sockets[0].sent[0].session.instructions, 'First feature instructions');
  assert.equal(sockets[1].sent[0].session.instructions, 'Second feature instructions');
  assert.equal(sockets[0].sent[0].session.turn_detection, null);
  assert.equal(sockets[0].sent[0].session.enable_input_audio_transcription, false);
  assert.deepEqual(sockets[0].sent[0].session.tools, []);
  for (const event of createQwenOmniTextTurnEvents('Supplied text')) first.connection.send(event);
  first.connection.send(createQwenOmniCancelResponseEvent());
  assert.equal(sockets[0].sent.at(-1).type, 'response.cancel');
  assert.equal(sockets[1].sent.length, 1);
  first.connection.close();
  assert.equal(sockets[1].readyState, WebSocket.OPEN);
  second.connection.close();
});

test('closing before socket open rejects the wait and late callbacks cannot reopen it', async () => {
  const sockets = harness();
  const h = consumer('Tina', 'Read supplied text');
  const opened = h.connection.open(h.options);
  const rejected = assert.rejects(opened, /connection closed/);
  h.connection.close();
  await rejected;
  sockets[0].open();
  sockets[0].event({ type: 'session.created' });
  assert.equal(h.connection.socket, null);
  assert.equal(h.connection.ready, false);
  assert.deepEqual(sockets[0].sent, []);
  assert.deepEqual(h.events, []);
});

test('replacing a session ignores old events and keeps the new session alive', async () => {
  const sockets = harness();
  const h = consumer('Tina', 'Consumer prompt');
  const first = h.connection.open(h.options);
  sockets[0].open();
  await first;
  const next = h.connection.open(h.options);
  sockets[1].open();
  await next;
  sockets[0].event({ type: 'session.closed', reason: 'Late close' });
  sockets[0].onclose({ code: 1006, reason: 'Old transport' });
  sockets[1].event({ type: 'session.created' });
  assert.equal(h.connection.socket, sockets[1]);
  assert.equal(h.connection.ready, true);
  assert.deepEqual(h.events, [{ type: 'session.created' }]);
  assert.deepEqual(h.closes, []);
  assert.deepEqual(h.errors, []);
  h.connection.close();
});

test('remote closure clears provider state even when the consumer only receives notification', async () => {
  const sockets = harness();
  const h = consumer('Tina', 'Consumer prompt');
  const opened = h.connection.open(h.options);
  sockets[0].open();
  await opened;
  sockets[0].event({ type: 'session.created' });
  sockets[0].onclose({ code: 1006, reason: 'Network interrupted' });
  assert.equal(h.connection.socket, null);
  assert.equal(h.connection.ready, false);
  assert.equal(h.closes.length, 1);
  assert.match(h.errors[0], /Network interrupted/);
  assert.doesNotMatch(h.errors[0], /Full-duplex|后台任务/);
});

test('provider error remains authoritative when the relay later closes', async () => {
  const sockets = harness();
  const h = consumer('Tina', 'Consumer prompt');
  const opened = h.connection.open(h.options);
  sockets[0].open();
  await opened;
  sockets[0].event({ type: 'session.created' });
  sockets[0].event({ type: 'error', error: { message: 'quota exhausted' } });
  sockets[0].event({ type: 'error', error: { code: 'qwen_gateway_upstream_closed', message: 'normal close' } });
  sockets[0].onclose({ code: 1000, reason: '' });
  assert.equal(h.errors.at(-1), 'quota exhausted');
  assert.equal(h.events.filter((event) => event.type === 'error').length, 1);
});

test('media sequencing is isolated per consumer', () => {
  const first = new QwenOmniMediaSequencer();
  const second = new QwenOmniMediaSequencer();
  first.createBatch('audio-a', true, 'image-a');
  assert.equal(first.createBatch('audio-b', true, null).events[1].image, 'image-a');
  assert.equal(second.createBatch('audio-c', true, 'image-c').events.length, 1);
  first.reset();
  assert.equal(first.snapshot().audioAppendSequence, 0);
  assert.equal(second.snapshot().audioAppendSequence, 1);
});
