import { useTranslation } from 'react-i18next';
import { Mic, MicOff, Square, UsersRound, VolumeX } from 'lucide-react';
import { useSessionStore } from '../../../channels/web/frontend/src/stores/sessionStore';
import { resolveTeamLeaderDisplayName } from '../../../channels/web/frontend/src/features/teamLeaderIdentity';
import { recordMeeting, stopMeeting, stopMeetingAudio, useMeetingSnapshot } from './taskMeetingStore';

export function MeetingPanel({ sessionId }: { sessionId: string | null }) {
  const { t, i18n } = useTranslation();
  const snapshot = useMeetingSnapshot();
  const runtime = useSessionStore((state) => state.runtimes[sessionId || '']);
  const ownsMeeting = !!sessionId && snapshot.sessionId === sessionId;
  const state = ownsMeeting ? snapshot.state : 'idle';
  const active = state === 'active';
  const canEnd = ['starting', 'active', 'cleanup_failed'].includes(state);
  const leader = resolveTeamLeaderDisplayName(runtime?.teamLeaderIdentity, i18n.language) || t('meeting.leader');
  const addressedAgents = snapshot.addressedMembers.map((name) => {
    if (runtime?.teamLeaderMemberIds.includes(name)) return `@${leader}`;
    const member = runtime?.teamMembers.find((item) => item.member_id === name);
    return `@${member?.name || name}`;
  });
  const buttonClass =
    'inline-flex min-h-10 items-center justify-center gap-2 rounded-md border border-border px-3 py-2 text-sm font-medium text-text hover:bg-bg-hover disabled:cursor-not-allowed disabled:opacity-50';

  return (
    <section className="min-w-0 flex-1 overflow-y-auto bg-card p-6" data-testid="team-area-meeting-panel">
      <div className="mx-auto flex max-w-[820px] flex-col gap-6">
        <header className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="flex items-center gap-2 text-lg font-semibold text-text" data-testid="team-area-meeting-title">
            <UsersRound size={20} aria-hidden="true" />
            {t('meeting.panel.title')}
          </h2>
          <span
            className="rounded-full bg-[var(--color-tag-surface)] px-3 py-1 text-sm text-text-secondary"
            role="status"
            data-testid="team-area-meeting-status"
            data-variant={state}
          >
            {t(`meeting.panel.states.${state}`)}
          </span>
        </header>

        {state === 'idle' ? (
          <p className="text-sm leading-6 text-text-secondary" data-testid="team-area-meeting-empty">
            {t('meeting.panel.empty')}
          </p>
        ) : (
          <>
            <dl
              className="grid gap-5 rounded-lg border border-border p-4 sm:grid-cols-2"
              data-testid="team-area-meeting-details"
            >
              <div>
                <dt className="mb-1 text-xs text-text-secondary">{t('meeting.panel.addressedAgents')}</dt>
                <dd className="text-sm font-medium text-text" data-testid="team-area-meeting-addressed-agents">
                  {addressedAgents.length ? addressedAgents.join(', ') : t('meeting.panel.noAddressedAgents')}
                </dd>
              </div>
              <div>
                <dt className="mb-1 text-xs text-text-secondary">{t('meeting.panel.speechRoute')}</dt>
                <dd className="text-sm font-medium text-text" data-testid="team-area-meeting-speech-route">
                  {snapshot.routingMode === 'leader' ? `@${leader}` : t('meeting.routing.none')}
                </dd>
              </div>
              <div>
                <dt className="mb-1 text-xs text-text-secondary">{t('meeting.panel.microphone')}</dt>
                <dd className="text-sm font-medium text-text" data-testid="team-area-meeting-microphone">
                  {active
                    ? t(`meeting.panel.microphoneStates.${snapshot.recording}`)
                    : t(`meeting.panel.states.${state}`)}
                </dd>
              </div>
              <div className="col-span-full">
                <dt className="mb-1 text-xs text-text-secondary">{t('meeting.panel.playback')}</dt>
                <dd className="text-sm text-text" data-testid="team-area-meeting-playback">
                  {snapshot.playback === 'idle'
                    ? t('meeting.panel.noSpeaker')
                    : t(`meeting.playback.${snapshot.playback}`, { speaker: snapshot.speaker, voice: snapshot.voice })}
                </dd>
              </div>
            </dl>

            <div className="flex flex-wrap gap-2" data-testid="team-area-meeting-controls">
              <button
                type="button"
                className={buttonClass}
                title={t(`meeting.record.${snapshot.recording}`)}
                disabled={!active || snapshot.recording === 'transcribing'}
                onClick={recordMeeting}
                data-testid="team-area-meeting-microphone-btn"
              >
                {snapshot.recording === 'recording' ? (
                  <MicOff size={16} aria-hidden="true" />
                ) : (
                  <Mic size={16} aria-hidden="true" />
                )}
                {t(
                  snapshot.recording === 'recording'
                    ? 'meeting.panel.pauseMicrophone'
                    : `meeting.record.${snapshot.recording}`,
                )}
              </button>
              <button
                type="button"
                className={buttonClass}
                disabled={!active || snapshot.playback === 'idle'}
                onClick={stopMeetingAudio}
                data-testid="team-area-meeting-stop-audio-btn"
              >
                <VolumeX size={16} aria-hidden="true" />
                {t('meeting.stopPlayback')}
              </button>
              <button
                type="button"
                className={`${buttonClass} bg-accent-subtle`}
                disabled={!canEnd}
                onClick={stopMeeting}
                data-testid="team-area-meeting-end-btn"
              >
                <Square size={16} aria-hidden="true" />
                {t(
                  state === 'cleanup_failed'
                    ? 'meeting.task.retryCleanup'
                    : state === 'closing'
                      ? 'meeting.task.closing'
                      : 'meeting.task.stop',
                )}
              </button>
            </div>

            {state === 'cleanup_failed' && (
              <p className="text-sm text-warn" role="alert" data-testid="team-area-meeting-cleanup-error">
                {t('meeting.errors.cleanupFailed')}
              </p>
            )}
            <div>
              <h3 className="mb-2 text-sm font-medium text-text" data-testid="team-area-meeting-transcript-title">
                {t('meeting.panel.transcript')}
              </h3>
              <p
                className="whitespace-pre-wrap break-words rounded-lg border border-border p-4 text-sm leading-6 text-text-secondary"
                data-testid="team-area-meeting-transcript"
              >
                {snapshot.lastTranscript || t('meeting.panel.waitingForSpeech')}
              </p>
            </div>
          </>
        )}
      </div>
    </section>
  );
}
