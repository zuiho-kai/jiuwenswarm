import { formatMeetingError } from './meetingErrors';
import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { LoaderCircle, Mic, MicOff, UsersRound, SkipForward, ChevronDown, Square } from 'lucide-react';
import type { ApplicationPluginTaskInputActionProps } from '../../../channels/web/frontend/src/applicationPlugins/types';
import { blockTaskInput } from '../../../channels/web/frontend/src/applicationPlugins/taskInputReadiness';
import {
  setTeamMeetingEnabled,
  useTeamMeetingEnabled,
} from '../../../channels/web/frontend/src/features/teamMeeting/featureFlag';
import { webClient } from '../../../channels/web/frontend/src/services/webClient';
import { toast } from '../../../channels/web/frontend/src/components/ui/Toast/toastStore';
import {
  publishMeeting,
  recordMeeting,
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
  onMicrophoneActiveChange,
  onStopActionChange,
  microphoneBusy = false,
  disabled = false,
}: ApplicationPluginTaskInputActionProps) {
  const { t } = useTranslation();
  const enabled = useTeamMeetingEnabled();
  const snapshot = useMeetingSnapshot();
  const [creating, setCreating] = useState(false);
  const [controlsOpen, setControlsOpen] = useState(true);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const attempt = useRef(0);
  const releaseCreation = useRef<(() => void) | undefined>();
  const cancelPending = useCallback(() => {
    attempt.current++;
    releaseCreation.current?.();
    releaseCreation.current = undefined;
  }, []);
  const mounted = useRef(true);
  const current = useRef({ sessionId, teamEligible });
  current.current = { sessionId, teamEligible };
  const active =
    snapshot.sessionId === sessionId || snapshot.state === 'closing' || snapshot.state === 'cleanup_failed';
  const status = creating ? 'starting' : active ? snapshot.state : 'idle';
  const recording = snapshot.recording;

  useEffect(() => {
    let cancelled = false;
    let reportedConfigError = false;
    let retry: ReturnType<typeof setTimeout> | undefined;
    const load = () => {
      void webClient
        .request<Record<string, unknown>>('config.get')
        .then((config) => {
          if (!cancelled) setTeamMeetingEnabled(config.team_meeting_enabled);
        })
        .catch((error) => {
          if (!cancelled && !reportedConfigError) {
            reportedConfigError = true;
            const content = formatMeetingError(t, 'configurationReadFailed', error);
            publishMeeting({ errorSessionId: current.current.sessionId, lastError: content });
            toast.open({ content, variant: 'error' });
          }
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
    onMicrophoneActiveChange?.(status !== 'idle');
    return () => onMicrophoneActiveChange?.(false);
  }, [status, onMicrophoneActiveChange]);
  useEffect(() => {
    // Session creation removes the draft runtime before selecting the new ID.
    // Its temporary Agent fallback must not cancel the pending meeting start.
    const creatingDraft = sessionId === 'new' && releaseCreation.current !== undefined;
    if (!enabled || (!teamEligible && !creatingDraft)) {
      cancelPending();
      setCreating(false);
      if (active && !teamEligible) stopMeeting();
    }
  }, [enabled, teamEligible, active, sessionId, cancelPending]);

  const start = async () => {
    if (creating || microphoneBusy || disabled || (!teamEligible && !onSelectTeamMode)) return;
    const id = ++attempt.current;
    const origin = sessionId;
    const release = blockTaskInput(null);
    releaseCreation.current = release;
    publishMeeting({ lastError: undefined });
    setCreating(true);
    try {
      if (!teamEligible) onSelectTeamMode?.();
      const readyId = await ensureSession('');
      if (id !== attempt.current || !mounted.current || !enabled) return;
      if (!readyId || (current.current.sessionId !== origin && current.current.sessionId !== readyId)) return;
      setCreating(false);
      const started = startMeeting(readyId);
      release();
      releaseCreation.current = undefined;
      await started;
    } catch (error) {
      if (id !== attempt.current || !mounted.current) return;
      const code =
        error instanceof Error && error.message.includes('Another connection owns this meeting')
          ? 'meetingBusy'
          : error instanceof Error &&
              [
                'unsupported',
                'disconnected',
                'microphoneFailed',
                'playbackUnavailable',
                'meetingStartFailed',
                'deliveryFailed',
                'meetingRequiresNewConversation',
              ].includes(error.message)
            ? error.message
            : 'meetingStartFailed';
      const content = formatMeetingError(t, code, error);
      publishMeeting({ errorSessionId: current.current.sessionId, lastError: content });
      toast.open({ content, variant: 'error' });
    } finally {
      release();
      if (releaseCreation.current === release) releaseCreation.current = undefined;
      if (id === attempt.current && mounted.current) setCreating(false);
    }
  };
  const stop = useCallback(() => {
    cancelPending();
    setCreating(false);
    stopMeeting();
  }, [cancelPending]);
  const title = t(
    `meeting.task.${status === 'idle' ? 'start' : status === 'starting' ? 'starting' : status === 'closing' ? 'closing' : status === 'cleanup_failed' ? 'retryCleanup' : 'stop'}`,
  );
  useEffect(() => {
    onStopActionChange?.(
      status === 'idle'
        ? null
        : {
            onStop: stop,
            label: status === 'starting' ? t('meeting.task.stop') : title,
            disabled: status === 'closing',
            sessionId: snapshot.sessionId,
          },
    );
    return () => onStopActionChange?.(null);
  }, [onStopActionChange, status, title, stop, snapshot.sessionId, t]);
  useEffect(() => {
    if (status === 'idle') setControlsOpen(true);
  }, [status]);
  const minimizeControls = () => {
    setControlsOpen(false);
    trigger.current?.focus();
  };
  const stopLabel = t(
    status === 'cleanup_failed'
      ? 'meeting.task.retryCleanup'
      : status === 'closing'
        ? 'meeting.task.closing'
        : 'meeting.task.stop',
  );
  return (
    <>
      {(enabled || status !== 'idle') && (teamEligible || onSelectTeamMode || status !== 'idle') && (
        <div className="task-meeting-actions" data-testid="chat-panel-meeting-controls" data-variant={status}>
          <button
            ref={trigger}
            type="button"
            className={`chat-input-btn task-meeting-action is-${status}`}
            title={status === 'idle' ? title : t('meeting.controls.title')}
            aria-label={status === 'idle' ? title : t('meeting.controls.title')}
            aria-pressed={status === 'active'}
            aria-expanded={status === 'idle' ? undefined : controlsOpen}
            aria-controls={status === 'idle' ? undefined : 'meeting-control-layer'}
            disabled={status === 'idle' && (microphoneBusy || disabled)}
            data-testid="chat-panel-input-meeting"
            data-variant={status}
            onClick={() => (status === 'idle' ? void start() : setControlsOpen((open) => !open))}
          >
            {status === 'starting' || status === 'closing' ? (
              <LoaderCircle className="chat-input-btn-icon task-meeting-spinner" aria-hidden="true" />
            ) : (
              <UsersRound className="chat-input-btn-icon" strokeWidth={1.8} aria-hidden="true" />
            )}
            {status !== 'idle' && <span>{t(`meeting.panel.states.${status}`)}</span>}
          </button>
        </div>
      )}
      {status !== 'idle' &&
        controlsOpen &&
        typeof document !== 'undefined' &&
        createPortal(
          <section
            id="meeting-control-layer"
            className="task-meeting-layer"
            role="region"
            aria-label={t('meeting.controls.title')}
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                event.stopPropagation();
                minimizeControls();
              }
            }}
            data-testid="chat-panel-meeting-layer"
            data-variant={status}
          >
            <header className="task-meeting-layer__header">
              <div className="task-meeting-layer__title">
                <UsersRound size={17} aria-hidden="true" />
                <strong data-testid="chat-panel-meeting-layer-title">{t('meeting.controls.title')}</strong>
                <span data-testid="chat-panel-meeting-layer-status">{t(`meeting.panel.states.${status}`)}</span>
              </div>
              <button
                type="button"
                className="task-meeting-layer__collapse"
                title={t('meeting.controls.minimize')}
                aria-label={t('meeting.controls.minimize')}
                onClick={minimizeControls}
                data-testid="chat-panel-meeting-minimize"
              >
                <ChevronDown size={16} aria-hidden="true" />
              </button>
            </header>
            <div className="task-meeting-layer__body">
              {status === 'starting' && (
                <p className="task-meeting-layer__message" role="status" data-testid="chat-panel-meeting-waiting">
                  {t('meeting.task.waiting')}
                </p>
              )}
              {status === 'cleanup_failed' && (
                <p className="task-meeting-layer__message" role="alert" data-testid="chat-panel-meeting-cleanup-error">
                  {t('meeting.errors.cleanupFailed')}
                </p>
              )}
              {status === 'active' && (
                <>
                  <p
                    className="task-meeting-layer__message"
                    role="status"
                    data-testid="chat-panel-meeting-playback"
                    data-variant={snapshot.playback}
                  >
                    {snapshot.playback === 'idle'
                      ? t('meeting.panel.noSpeaker')
                      : t(`meeting.playback.${snapshot.playback}`, {
                          speaker: snapshot.speaker,
                        })}
                  </p>
                  <div className="task-meeting-layer__controls" data-testid="chat-panel-meeting-layer-controls">
                    <button
                      type="button"
                      className="task-meeting-layer__control task-meeting-microphone"
                      title={t(`meeting.record.${recording}`)}
                      aria-label={t(`meeting.record.${recording}`)}
                      aria-pressed={recording === 'recording'}
                      disabled={recording === 'transcribing'}
                      data-testid="chat-panel-input-meeting-record"
                      data-variant={recording}
                      onClick={recordMeeting}
                    >
                      {recording === 'transcribing' ? (
                        <LoaderCircle size={20} className="task-meeting-spinner" aria-hidden="true" />
                      ) : recording === 'recording' ? (
                        <Mic size={20} aria-hidden="true" />
                      ) : (
                        <MicOff size={20} aria-hidden="true" />
                      )}
                      <span data-testid="chat-panel-meeting-microphone-state">
                        {t(`meeting.panel.microphoneStates.${recording}`)}
                      </span>
                    </button>
                    <button
                      type="button"
                      className="task-meeting-layer__control"
                      disabled={snapshot.playback === 'idle'}
                      title={t('meeting.skipSpeech')}
                      aria-label={t('meeting.skipSpeech')}
                      data-testid="chat-panel-input-meeting-stop-audio"
                      onClick={stopMeetingAudio}
                    >
                      <SkipForward size={20} aria-hidden="true" />
                      <span data-testid="chat-panel-meeting-skip-label">{t('meeting.skipSpeech')}</span>
                    </button>
                  </div>
                </>
              )}
              <button
                type="button"
                className="task-meeting-layer__end"
                disabled={status === 'closing'}
                onClick={stop}
                data-testid="chat-panel-meeting-layer-end"
              >
                <Square size={13} aria-hidden="true" />
                {stopLabel}
              </button>
            </div>
          </section>,
          document.body,
        )}
      {snapshot.lastError &&
        snapshot.errorSessionId === sessionId &&
        typeof document !== 'undefined' &&
        createPortal(
          <div role="alert" className="task-meeting-error" data-testid="chat-panel-meeting-error">
            <span data-testid="chat-panel-meeting-error-detail">{snapshot.lastError}</span>
            <button
              type="button"
              onClick={() => publishMeeting({ lastError: undefined })}
              aria-label={t('meeting.diagnostics.dismiss')}
              data-testid="chat-panel-meeting-error-dismiss"
            >
              ×
            </button>
          </div>,
          document.body,
        )}
      {fallback}
    </>
  );
}
