import { useSyncExternalStore } from 'react';
import { blockTaskInput } from '../../../channels/web/frontend/src/applicationPlugins/taskInputReadiness';
import type { RecordingState, PlaybackState } from './meetingRuntime';

export type MeetingSnapshot = {
  state: 'idle' | 'starting' | 'active' | 'closing' | 'cleanup_failed';
  sessionId: string | null;
  recording: RecordingState;
  playback: PlaybackState;
  speaker: string;
  voice: string;
};
type Controller = {
  start: (sessionId: string) => Promise<void>;
  stop: () => void;
  record: () => void;
  stopAudio: () => void;
};
let controller: Controller | null = null;
let snapshot: MeetingSnapshot = {
  state: 'idle',
  sessionId: null,
  recording: 'ready',
  playback: 'idle',
  speaker: '',
  voice: '',
};
const listeners = new Set<() => void>();
let releaseInput: (() => void) | undefined;

export function registerMeetingController(next: Controller): () => void {
  controller = next;
  return () => {
    if (controller === next) controller = null;
  };
}
export function publishMeeting(update: Partial<MeetingSnapshot>): void {
  snapshot = { ...snapshot, ...update };
  const previous = releaseInput;
  releaseInput = snapshot.state === 'starting'
    ? blockTaskInput(snapshot.sessionId)
    : undefined;
  previous?.();
  listeners.forEach((listener) => listener());
}
export function useMeetingSnapshot(): MeetingSnapshot {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => snapshot,
    () => snapshot,
  );
}
export async function startMeeting(sessionId: string): Promise<void> {
  if (!controller) throw new Error('disconnected');
  await controller.start(sessionId);
}
export function stopMeeting(): void {
  controller?.stop();
}
export function recordMeeting(): void {
  controller?.record();
}
export function stopMeetingAudio(): void {
  controller?.stopAudio();
}
