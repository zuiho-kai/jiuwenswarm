import { useCallback, useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import type { ApplicationPluginTaskRuntimeProps } from '../../../channels/web/frontend/src/applicationPlugins/types';
import { isTeamAgentMode } from '../../../channels/web/frontend/src/features/planMode/wireMode';
import { resolveTeamLeaderDisplayName } from '../../../channels/web/frontend/src/features/teamLeaderIdentity';
import { useTeamMeetingEnabled } from '../../../channels/web/frontend/src/features/teamMeeting/featureFlag';
import { webClient } from '../../../channels/web/frontend/src/services/webClient';
import { rememberGroupChatInput } from '../../../channels/web/frontend/src/features/groupChatInput';
import { useSessionStore } from '../../../channels/web/frontend/src/stores/sessionStore';
import { toast } from '../../../channels/web/frontend/src/components/ui/Toast/toastStore';
import { MeetingSession, type MeetingContext } from './meetingRuntime';
import { sendMeetingInput } from './meetingInput';
import { publishMeeting, registerMeetingController, useMeetingSnapshot } from './taskMeetingStore';

type ServerMeeting = {
  state: 'idle' | 'starting' | 'active' | 'closing' | 'cleanup_failed';
  meeting_id?: string;
};

export function TaskMeetingRuntime({ sessionId, onSendMessage }: ApplicationPluginTaskRuntimeProps) {
  const { t, i18n } = useTranslation();
  const enabled = useTeamMeetingEnabled();
  const snapshot = useMeetingSnapshot();
  const current = useRef<{
    sessionId: string;
    meeting: MeetingSession;
    meetingId: string | null;
    input: AbortController;
  } | null>(null);
  const sender = useRef(onSendMessage);
  sender.current = onSendMessage;
  const initialization = useRef(new Map<string, { id: string; text: string }>());
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
            .map((member) => ({
              member_name: member.member_id,
              display_name: member.name || member.member_id,
            })),
        ],
      };
    },
    [i18n.language, t],
  );

  const stop = useCallback(() => {
    const active = current.current;
    if (active) {
      active.input.abort();
      active.meeting.stop();
    }
  }, []);

  const endServer = useCallback((id: string, meetingId: string | null, startupId: string) => {
    const method = meetingId ? 'meeting.end' : 'meeting.release';
    void webClient
      .request(method, {
        session_id: id,
        meeting_id: meetingId || '',
        startup_id: startupId,
      })
      .catch(() => {});
  }, []);

  const start = useCallback(
    async (id: string) => {
      if (!enabled || current.current) return;
      const mode = useSessionStore.getState().getRuntime(id)?.mode;
      if (!mode || !isTeamAgentMode(mode)) return;
      if (webClient.getState() !== 'ready') throw new Error('disconnected');
      const send = sender.current;
      if (!send) throw new Error('disconnected');
      const startupId = crypto.randomUUID();
      const input = new AbortController();
      const meeting: MeetingSession = new MeetingSession(webClient, id, context(id), {
        line: () => {}, // Task handlers already own the conversation timeline.
        draft: (text) => {
          if (!mounted.current || current.current?.meeting !== meeting) return;
          void sendMeetingInput(
            webClient,
            send,
            id,
            text,
            crypto.randomUUID(),
            [context(id).leader],
            input.signal,
          ).catch(() => {
            if (mounted.current && current.current?.meeting === meeting)
              toast.open({
                content: t('meeting.errors.deliveryFailed'),
                variant: 'error',
              });
          });
        },
        error: (code) => {
          if (mounted.current)
            toast.open({
              content: t(`meeting.errors.${code}`),
              variant: 'error',
            });
        },
        recording: (recording) => {
          if (mounted.current && current.current?.meeting === meeting) publishMeeting({ recording });
        },
        ended: () => {
          const active = current.current;
          if (active?.meeting !== meeting) return;
          active.input.abort();
          current.current = null;
          endServer(active.sessionId, active.meetingId, startupId);
          publishMeeting({
            state: 'idle',
            sessionId: null,
            recording: 'ready',
          });
        },
      });
      current.current = { sessionId: id, meeting, meetingId: null, input };
      publishMeeting({ state: 'starting', sessionId: id, recording: 'ready' });
      try {
        let initial = initialization.current.get(id);
        if (!initial) {
          initial = {
            id: `meeting-start-${startupId}`,
            text: t('meeting.startMessage'),
          };
          initialization.current.set(id, initial);
        }
        await sendMeetingInput(webClient, send, id, initial.text, initial.id, [], input.signal);
        rememberGroupChatInput(id);
        if (current.current?.meeting !== meeting) return;
        let server = await webClient.request<ServerMeeting>('meeting.start', {
          session_id: id,
          startup_id: startupId,
        });
        if (current.current?.meeting !== meeting) {
          endServer(id, server.meeting_id || null, startupId);
          return;
        }
        while (server.state === 'starting') {
          await new Promise((resolve) => setTimeout(resolve, 1000));
          if (current.current?.meeting !== meeting) return;
          server = await webClient.request<ServerMeeting>('meeting.status', {
            session_id: id,
            startup_id: startupId,
          });
          if (current.current?.meeting !== meeting) {
            // Release only this startup; a newer attempt may now own the session.
            endServer(id, null, startupId);
            return;
          }
        }
        if (server.state !== 'active' || !server.meeting_id) throw new Error('meetingStartFailed');
        current.current.meetingId = server.meeting_id;
        try {
          await meeting.start();
        } catch (error) {
          if (error instanceof Error && ['unsupported', 'disconnected'].includes(error.message)) throw error;
          throw new Error('microphoneFailed');
        }
        if (mounted.current && current.current?.meeting === meeting) {
          initialization.current.delete(id);
          publishMeeting({ state: 'active' });
          meeting.startRecording();
        }
      } catch (error) {
        meeting.stop();
        throw error;
      }
    },
    [context, enabled, endServer, t],
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
