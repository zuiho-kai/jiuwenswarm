type Translate = (key: string) => string;

/** Keep transport metadata and browser error names instead of replacing their cause. */
export function meetingErrorDetail(error: unknown): string {
  if (typeof error === 'string') return error;
  if (!error || typeof error !== 'object') return '';
  const value = error as { name?: string; message?: string; code?: unknown; requestId?: string; cause?: unknown };
  return [
    value.name && value.name !== 'Error' ? value.name : '',
    value.message,
    value.code ? `[${value.code}]` : '',
    value.requestId ? `request=${value.requestId}` : '',
    value.cause ? meetingErrorDetail(value.cause) : '',
  ]
    .filter(Boolean)
    .join(' ')
    .slice(0, 2000);
}

export function meetingFailure(stage: string, cause: unknown): Error {
  return Object.assign(new Error(stage), { cause });
}

export function formatMeetingError(t: Translate, stage: string, error?: unknown): string {
  const raw = meetingErrorDetail(error);
  const detail = raw
    .replace(/Bearer\s+[^\s"']+/gi, 'Bearer [redacted]')
    .replace(/([?&](?:api[_-]?key|token|secret)=)[^&\s]+/gi, '$1[redacted]');
  let diagnosis = 'unknown';
  if (/NotAllowedError|PermissionDenied|permission denied|权限.*拒绝/i.test(raw))
    diagnosis = /playback/i.test(stage) ? 'autoplay' : 'permission';
  else if (/NotFoundError|DevicesNotFound|no.*microphone/i.test(raw)) diagnosis = 'deviceMissing';
  else if (/NotReadableError|TrackStartError|device.*busy/i.test(raw)) diagnosis = 'deviceBusy';
  else if (/OverconstrainedError/i.test(raw)) diagnosis = 'deviceConstraints';
  else if (/\b(?:401|403)\b|unauthorized|forbidden|invalid.*key|鉴权/i.test(raw)) diagnosis = 'credentials';
  else if (/\b429\b|rate.?limit|quota|余额|配额/i.test(raw)) diagnosis = 'quota';
  else if (/timeout|timed out|REQUEST_TIMEOUT|超时/i.test(raw)) diagnosis = 'timeout';
  else if (/backlog|overload|queue.*full/i.test(raw)) diagnosis = 'overload';
  else if (/not configured|not set|missing.*(model|endpoint|key)|配置|ASR_NOT_CONFIGURED/i.test(raw))
    diagnosis = 'configuration';
  else if (/WS_|disconnect|network|fetch failed|connection|连接/i.test(raw)) diagnosis = 'connection';
  else if (/onnx|silero|worklet|wasm|vad|人声检测/i.test(raw)) diagnosis = 'capture';
  else if (/CHAT_SENDER_UNAVAILABLE|stale|out.of.order|identity|lease|configuration changed|stream.*ready/i.test(raw)) diagnosis = 'session';
  else if (/decode|codec|format|MEDIA_ERR|NotSupportedError/i.test(raw)) diagnosis = 'audioFormat';
  if (stage === 'meetingRequiresNewConversation') diagnosis = 'newConversation';
  if (stage === 'meetingBusy') diagnosis = 'owner';
  if (stage === 'unsupported') diagnosis = 'secureContext';
  if (stage === 'microphoneEnded') diagnosis = 'deviceMissing';
  const summary = t(`meeting.errors.${stage}`);
  const reason = detail && detail !== stage ? detail : t(`meeting.diagnostics.reason.${stage}`);
  const knownReason = reason.startsWith('meeting.') ? t('meeting.diagnostics.unknownReason') : reason;
  return `${summary} ${t('meeting.diagnostics.reasonLabel')}${knownReason} ${t('meeting.diagnostics.solutionLabel')}${t(`meeting.diagnostics.solution.${diagnosis}`)}`;
}
