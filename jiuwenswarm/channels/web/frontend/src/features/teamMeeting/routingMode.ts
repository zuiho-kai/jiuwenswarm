export type MeetingRoutingMode = 'leader' | 'none';

const storageKey = 'team-meeting-input-routing';
let fallback: MeetingRoutingMode = 'leader';

export function getMeetingRoutingMode(): MeetingRoutingMode {
  try {
    return window.localStorage.getItem(storageKey) === 'none' ? 'none' : 'leader';
  } catch {
    return fallback;
  }
}

export function setMeetingRoutingMode(mode: MeetingRoutingMode): void {
  fallback = mode;
  try {
    window.localStorage.setItem(storageKey, mode);
  } catch {
    // Keep the choice for this page when browser storage is unavailable.
  }
}
