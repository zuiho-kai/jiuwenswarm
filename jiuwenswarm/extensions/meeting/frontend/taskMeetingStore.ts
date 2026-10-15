import { useSyncExternalStore } from 'react';
import type { RecordingState } from './meetingRuntime';

export type MeetingSnapshot = {
  state: 'idle' | 'starting' | 'active';
  sessionId: string | null;
  recording: RecordingState;
};
type Controller = {
  start: (sessionId: string) => Promise<void>;
  stop: () => void;
  record: () => void;
  stopAudio: () => void;
};
let controller: Controller | null = null;
let snapshot: MeetingSnapshot = { state: 'idle', sessionId: null, recording: 'ready' };
const listeners = new Set<() => void>();
const composers = new Map<string, (text: string) => void>();
const pending = new Map<string, string[]>();

export function registerMeetingController(next: Controller): () => void {
  controller = next;
  return () => {
    if (controller === next) controller = null;
  };
}
export function publishMeeting(update: Partial<MeetingSnapshot>): void {
  snapshot = { ...snapshot, ...update };
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
export function registerMeetingComposer(sessionId: string, append: (text: string) => void): () => void {
  composers.set(sessionId, append);
  for (const text of pending.get(sessionId) || []) append(text);
  pending.delete(sessionId);
  return () => {
    if (composers.get(sessionId) === append) composers.delete(sessionId);
  };
}
export function deliverMeetingTranscript(sessionId: string, text: string): void {
  const append = composers.get(sessionId);
  if (append) append(text);
  else pending.set(sessionId, [...(pending.get(sessionId) || []).slice(-3), text]);
}
export function clearMeetingTranscripts(sessionId: string): void {
  pending.delete(sessionId);
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
