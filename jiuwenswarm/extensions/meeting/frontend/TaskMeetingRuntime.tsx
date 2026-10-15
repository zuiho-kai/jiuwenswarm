import { useCallback, useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import type { ApplicationPluginTaskRuntimeProps } from '../../../channels/web/frontend/src/applicationPlugins/types';
import { isTeamAgentMode } from '../../../channels/web/frontend/src/features/planMode/wireMode';
import { resolveTeamLeaderDisplayName } from '../../../channels/web/frontend/src/features/teamLeaderIdentity';
import { useTeamMeetingEnabled } from '../../../channels/web/frontend/src/features/teamMeeting/featureFlag';
import { webClient } from '../../../channels/web/frontend/src/services/webClient';
import { useSessionStore } from '../../../channels/web/frontend/src/stores/sessionStore';
import { toast } from '../../../channels/web/frontend/src/components/ui/Toast/toastStore';
import { MeetingSession, type MeetingContext } from './meetingRuntime';
import {
  clearMeetingTranscripts,
  deliverMeetingTranscript,
  publishMeeting,
  registerMeetingController,
  useMeetingSnapshot,
} from './taskMeetingStore';

export function TaskMeetingRuntime({ sessionId }: ApplicationPluginTaskRuntimeProps) {
  const { t, i18n } = useTranslation();
  const enabled = useTeamMeetingEnabled();
  const snapshot = useMeetingSnapshot();
  const current = useRef<{ sessionId: string; meeting: MeetingSession } | null>(null);
  const mounted = useRef(true);
  const roster = useSessionStore((state) => state.runtimes[sessionId || '']);

  const context = useCallback(
    (id: string): MeetingContext => {
      const runtime = useSessionStore.getState().getRuntime(id);
      const leader = runtime?.teamLeaderMemberIds[0] || 'team_leader';
      return {
        leader,
        members: [
          {
            member_name: leader,
            display_name:
              resolveTeamLeaderDisplayName(runtime?.teamLeaderIdentity, i18n.language) || t('meeting.leader'),
          },
          ...(runtime?.teamMembers || [])
            .filter(
              (member) =>
                member.member_id !== leader &&
                member.role !== 'human_agent' &&
                member.status.toLowerCase() !== 'shutdown' &&
                !member.workflow_run_id,
            )
            .map((member) => ({ member_name: member.member_id, display_name: member.name || member.member_id })),
        ],
      };
    },
    [i18n.language, t],
  );

  const stop = useCallback(() => {
    const active = current.current;
    if (active) {
      active.meeting.stop();
      clearMeetingTranscripts(active.sessionId);
    }
  }, []);

  const start = useCallback(
    async (id: string) => {
      if (!enabled || current.current) return;
      const mode = useSessionStore.getState().getRuntime(id)?.mode;
      if (!mode || !isTeamAgentMode(mode)) return;
      if (webClient.getState() !== 'ready') throw new Error('disconnected');
      const meeting: MeetingSession = new MeetingSession(webClient, id, context(id), {
        line: () => {}, // Task handlers already own the conversation timeline.
        draft: (text) => {
          if (mounted.current && current.current?.meeting === meeting) deliverMeetingTranscript(id, text);
        },
        error: (code) => {
          if (mounted.current) toast.open({ content: t(`meeting.errors.${code}`), variant: 'error' });
        },
        recording: (recording) => {
          if (mounted.current && current.current?.meeting === meeting) publishMeeting({ recording });
        },
        ended: () => {
          if (current.current?.meeting !== meeting) return;
          current.current = null;
          publishMeeting({ state: 'idle', sessionId: null, recording: 'ready' });
        },
      });
      current.current = { sessionId: id, meeting };
      publishMeeting({ state: 'starting', sessionId: id, recording: 'ready' });
      try {
        await meeting.start();
        if (mounted.current && current.current?.meeting === meeting) {
          publishMeeting({ state: 'active' });
          meeting.startRecording();
        }
      } catch (error) {
        meeting.stop();
        throw error;
      }
    },
    [context, enabled, t],
  );

  useEffect(
    () =>
      registerMeetingController({
        start,
        stop,
        record: () => {
          if (snapshot.recording === 'recording') current.current?.meeting.stopRecording();
          else current.current?.meeting.startRecording();
        },
        stopAudio: () => current.current?.meeting.stopPlayback(),
      }),
    [start, stop, snapshot.recording],
  );

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      stop();
    };
  }, [stop]);

  useEffect(() => {
    if (!enabled || (current.current && current.current.sessionId !== sessionId)) stop();
  }, [enabled, sessionId, stop]);
  useEffect(() => {
    if (current.current) current.current.meeting.context = context(current.current.sessionId);
  }, [roster, context]);
  return null;
}
