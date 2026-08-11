import assert from 'node:assert/strict';
import test from 'node:test';

import { selectMonitorFrames, selectTemporalFrames } from '../node_modules/.cache/monitor-frame-queue/components/VideoLivePanel/monitorFrameQueue.js';

test('keeps every frame when the buffer fits in one request', () => {
  const frames = Array.from({ length: 4 }, (_, index) => index);
  assert.deepEqual(selectTemporalFrames(frames, 8), [0, 1, 2, 3]);
});

test('samples the complete buffered timeline including both ends', () => {
  const frames = Array.from({ length: 17 }, (_, index) => index);
  const selected = selectTemporalFrames(frames, 8);

  assert.equal(selected.length, 8);
  assert.equal(selected[0], 0);
  assert.equal(selected[selected.length - 1], 16);
  assert.deepEqual(selected, [0, 2, 5, 7, 9, 11, 14, 16]);
});

test('a one-frame budget keeps the newest observation', () => {
  assert.deepEqual(selectTemporalFrames([1, 2, 3], 1), [3]);
});

test('translation mode sparsely samples the retained recent timeline', () => {
  assert.deepEqual(selectMonitorFrames([1, 2, 3, 4, 5, 6], 4, true), [1, 3, 4, 6]);
});

test('general monitoring still samples the complete timeline', () => {
  assert.deepEqual(selectMonitorFrames([1, 2, 3, 4, 5], 3, false), [1, 3, 5]);
});
