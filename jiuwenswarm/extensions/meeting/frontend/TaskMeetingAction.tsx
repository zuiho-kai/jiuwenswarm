import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { LoaderCircle, Mic, Square, UsersRound, VolumeX } from 'lucide-react';
import type { ApplicationPluginTaskInputActionProps } from '../../../channels/web/frontend/src/applicationPlugins/types';
import {
  setTeamMeetingEnabled,
  useTeamMeetingEnabled,
} from '../../../channels/web/frontend/src/features/teamMeeting/featureFlag';
import { webClient } from '../../../channels/web/frontend/src/services/webClient';
import { toast } from '../../../channels/web/frontend/src/components/ui/Toast/toastStore';
import {
  recordMeeting,
  registerMeetingComposer,
  startMeeting,
  stopMeeting,
  stopMeetingAudio,
  useMeetingSnapshot,
} from './taskMeetingStore';
import './TaskMeetingAction.css';

export function TaskMeetingAction({
  fallback,
  teamEligible = false,
  onSelectTeamMode,
  sessionId,
  ensureSession,
  onTranscript,
  onMicrophoneActiveChange,
  microphoneBusy = false,
  disabled = false,
}: ApplicationPluginTaskInputActionProps) {
  const { t } = useTranslation();
  const enabled = useTeamMeetingEnabled();
  const snapshot = useMeetingSnapshot();
  const [creating, setCreating] = useState(false);
  const attempt = useRef(0);
  const cancelPending = useCallback(() => {
    attempt.current++;
  }, []);
  const mounted = useRef(true);
  const current = useRef({ sessionId, teamEligible });
  current.current = { sessionId, teamEligible };
  const active = snapshot.sessionId === sessionId;
  const status = creating ? 'starting' : active ? snapshot.state : 'idle';
  const recording = snapshot.recording;

  useEffect(() => {
    let cancelled = false;
    let retry: ReturnType<typeof setTimeout> | undefined;
    const load = () => {
      void webClient
        .request<Record<string, unknown>>('config.get')
        .then((config) => {
          if (!cancelled) setTeamMeetingEnabled(config.team_meeting_enabled);
        })
        .catch(() => {
          if (!cancelled) retry = setTimeout(load, 2000);
        });
    };
    load();
    return () => {
      cancelled = true;
      if (retry) clearTimeout(retry);
    };
  }, []);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      cancelPending();
    };
  }, [cancelPending]);
  useEffect(() => {
    if (sessionId && onTranscript) return registerMeetingComposer(sessionId, onTranscript);
  }, [sessionId, onTranscript]);
  useEffect(() => {
    onMicrophoneActiveChange?.(status !== 'idle');
    return () => onMicrophoneActiveChange?.(false);
  }, [status, onMicrophoneActiveChange]);
  useEffect(() => {
    if (!enabled || !teamEligible) {
      attempt.current++;
      setCreating(false);
      if (active && !teamEligible) stopMeeting();
    }
  }, [enabled, teamEligible, active]);

  const start = async () => {
    if (creating || microphoneBusy || disabled || (!teamEligible && !onSelectTeamMode)) return;
    const id = ++attempt.current;
    const origin = sessionId;
    setCreating(true);
    try {
      if (!teamEligible) onSelectTeamMode?.();
      const readyId = await ensureSession('');
      if (id !== attempt.current || !mounted.current || !enabled) return;
      if (!readyId || (current.current.sessionId !== origin && current.current.sessionId !== readyId)) return;
      await startMeeting(readyId);
    } catch (error) {
      if (id !== attempt.current || !mounted.current) return;
      const code =
        error instanceof Error && ['unsupported', 'disconnected'].includes(error.message)
          ? error.message
          : 'microphoneFailed';
      toast.open({ content: t(`meeting.errors.${code}`), variant: 'error' });
    } finally {
      if (id === attempt.current && mounted.current) setCreating(false);
    }
  };
  const stop = () => {
    attempt.current++;
    setCreating(false);
    stopMeeting();
  };
  const title = t(`meeting.task.${status === 'idle' ? 'start' : status === 'starting' ? 'starting' : 'stop'}`);
  return (
    <>
      {enabled && (teamEligible || onSelectTeamMode || status !== 'idle') && (
        <div className="task-meeting-actions" data-testid="chat-panel-meeting-controls" data-variant={status}>
          <button
            type="button"
            className={`chat-input-btn task-meeting-action is-${status}`}
            title={title}
            aria-label={title}
            aria-pressed={status === 'active'}
            disabled={status === 'idle' && (microphoneBusy || disabled)}
            data-testid="chat-panel-input-meeting"
            data-variant={status}
            onClick={() => {
              if (status !== 'idle') stop();
              else void start();
            }}
          >
            {status === 'starting' ? (
              <LoaderCircle className="chat-input-btn-icon task-meeting-spinner" aria-hidden="true" />
            ) : (
              <UsersRound className="chat-input-btn-icon" strokeWidth={1.8} aria-hidden="true" />
            )}
          </button>
          {status === 'active' && (
            <>
              <button
                type="button"
                className={`chat-input-btn${recording === 'recording' ? ' chat-input-btn--recording' : ''}`}
                title={t(`meeting.record.${recording}`)}
                aria-label={t(`meeting.record.${recording}`)}
                aria-pressed={recording === 'recording'}
                disabled={recording === 'transcribing'}
                data-testid="chat-panel-input-meeting-record"
                data-variant={recording}
                onClick={recordMeeting}
              >
                {recording === 'transcribing' ? (
                  <LoaderCircle className="chat-input-btn-icon task-meeting-spinner" aria-hidden="true" />
                ) : recording === 'recording' ? (
                  <Square className="chat-input-btn-icon" fill="currentColor" aria-hidden="true" />
                ) : (
                  <Mic className="chat-input-btn-icon" strokeWidth={1.8} aria-hidden="true" />
                )}
              </button>
              <button
                type="button"
                className="chat-input-btn"
                title={t('meeting.stopPlayback')}
                aria-label={t('meeting.stopPlayback')}
                data-testid="chat-panel-input-meeting-stop-audio"
                onClick={stopMeetingAudio}
              >
                <VolumeX className="chat-input-btn-icon" strokeWidth={1.8} aria-hidden="true" />
              </button>
            </>
          )}
        </div>
      )}
      {fallback}
    </>
  );
}
