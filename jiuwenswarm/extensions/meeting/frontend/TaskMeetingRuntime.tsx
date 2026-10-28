import { useCallback, useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import type { ApplicationPluginTaskRuntimeProps } from '../../../channels/web/frontend/src/applicationPlugins/types';
import { isTeamAgentMode } from '../../../channels/web/frontend/src/features/planMode/wireMode';
import { resolveTeamLeaderDisplayName } from '../../../channels/web/frontend/src/features/teamLeaderIdentity';
import { useTeamMeetingEnabled } from '../../../channels/web/frontend/src/features/teamMeeting/featureFlag';
import { getMeetingRoutingMode } from '../../../channels/web/frontend/src/features/teamMeeting/routingMode';
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

type ActiveMeeting = {
  sessionId: string;
  meeting: MeetingSession;
  meetingId: string | null;
  startupId: string;
  input: AbortController;
  ending?: Promise<void>;
};

// Keep pending cleanup reachable if the Task panel unmounts and returns.
// Unmount always stops local media; this reference never resumes a meeting.
const retainedMeeting: { current: ActiveMeeting | null } = { current: null };

export function TaskMeetingRuntime({ sessionId, onSendMessage }: ApplicationPluginTaskRuntimeProps) {
  const { t, i18n } = useTranslation();
  const enabled = useTeamMeetingEnabled();
  const translator = useRef(t);
  translator.current = t;
  const snapshot = useMeetingSnapshot();
  const current = retainedMeeting;
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

  const endServer = useCallback((active: ActiveMeeting): Promise<void> => {
    if (active.ending) return active.ending;
    active.input.abort();
    publishMeeting({ state: 'closing', recording: 'ready' });
    active.ending = (async () => {
      try {
        const ended = await webClient.request<ServerMeeting>(
          active.meetingId ? 'meeting.end' : 'meeting.release',
          {
            session_id: active.sessionId,
            meeting_id: active.meetingId || '',
            startup_id: active.startupId,
          },
          { timeoutMs: 25000 },
        );
        if (ended.state !== 'idle') throw new Error('cleanupPending');
        active.meeting.finishCleanup();
        if (current.current === active) {
          current.current = null;
          publishMeeting({
            state: 'idle',
            sessionId: null,
            recording: 'ready',
            playback: 'idle',
            speaker: '',
            voice: '',
          });
        }
      } catch {
        // A lost End response can still mean cleanup finished. One status query
        // resolves that ambiguity; active meetings are never polled or resumed.
        let idle = false;
        try {
          const status = await webClient.request<ServerMeeting>('meeting.status', {
            session_id: active.sessionId,
            startup_id: active.startupId,
          });
          idle = status.state === 'idle';
        } catch {
          /* Keep the explicit cleanup retry available. */
        }
        if (idle) {
          active.meeting.finishCleanup();
          if (current.current === active) {
            current.current = null;
            publishMeeting({
              state: 'idle',
              sessionId: null,
              recording: 'ready',
              playback: 'idle',
              speaker: '',
              voice: '',
            });
          }
        } else if (current.current === active) {
          publishMeeting({ state: 'cleanup_failed', recording: 'ready' });
          if (mounted.current)
            toast.open({
              content: translator.current('meeting.errors.cleanupFailed'),
              variant: 'error',
            });
        }
      } finally {
        active.ending = undefined;
      }
    })();
    return active.ending;
  }, []);

  const stop = useCallback(() => {
    const active = current.current;
    if (!active) return;
    active.input.abort();
    active.meeting.stop();
    void endServer(active);
  }, [endServer]);

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
      const routingMode = getMeetingRoutingMode();
      const meeting: MeetingSession = new MeetingSession(webClient, id, context(id), {
        line: () => {}, // Task handlers already own the conversation timeline.
        draft: (text) => {
          if (!mounted.current || current.current?.meeting !== meeting || input.signal.aborted)
            throw new Error('disconnected');
          publishMeeting({ lastTranscript: text });
          return sendMeetingInput(
            webClient,
            send,
            id,
            text,
            crypto.randomUUID(),
            routingMode === 'leader' ? [context(id).leader] : [],
            input.signal,
          );
        },
        error: (code) => {
          if (mounted.current && current.current?.meeting === meeting && !input.signal.aborted)
            toast.open({
              content: t(`meeting.errors.${code}`),
              variant: 'error',
            });
        },
        recording: (recording) => {
          if (mounted.current && current.current?.meeting === meeting) publishMeeting({ recording });
        },
        playback: (playback, member, voice) => {
          if (current.current?.meeting !== meeting || input.signal.aborted) return;
          const speaker = context(id).members.find((item) => item.member_name === member)?.display_name || member || '';
          publishMeeting({ playback, speaker, voice: voice || '' });
        },
        addressedMembers: (names) => {
          if (mounted.current && current.current?.meeting === meeting && !input.signal.aborted)
            publishMeeting({ addressedMembers: names });
        },
        ended: () => {
          const active = current.current;
          if (active?.meeting === meeting) void endServer(active);
        },
      });
      const active: ActiveMeeting = {
        sessionId: id,
        meeting,
        meetingId: null,
        startupId,
        input,
      };
      current.current = active;
      publishMeeting({
        state: 'starting',
        sessionId: id,
        recording: 'ready',
        playback: 'idle',
        speaker: '',
        voice: '',
        routingMode,
        lastTranscript: '',
        addressedMembers: [],
      });
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
        if (current.current?.meeting !== meeting || input.signal.aborted) return;
        let server = await webClient.request<ServerMeeting>('meeting.start', {
          session_id: id,
          startup_id: startupId,
        });
        if (current.current?.meeting !== meeting || input.signal.aborted) return;
        while (server.state === 'starting') {
          await new Promise((resolve) => setTimeout(resolve, 1000));
          if (current.current?.meeting !== meeting || input.signal.aborted) return;
          server = await webClient.request<ServerMeeting>('meeting.status', {
            session_id: id,
            startup_id: startupId,
          });
          if (current.current?.meeting !== meeting || input.signal.aborted) return;
        }
        if (server.state !== 'active' || !server.meeting_id) throw new Error('meetingStartFailed');
        active.meetingId = server.meeting_id;
        try {
          await meeting.start();
        } catch (error) {
          if (error instanceof Error && ['unsupported', 'disconnected'].includes(error.message)) throw error;
          throw new Error('microphoneFailed');
        }
        if (mounted.current && current.current === active && !input.signal.aborted) {
          try {
            await meeting.activatePlayback(server.meeting_id, startupId);
          } catch {
            throw new Error('playbackUnavailable');
          }
          if (current.current !== active || input.signal.aborted) return;
          initialization.current.delete(id);
          publishMeeting({ state: 'active' });
        }
      } catch (error) {
        meeting.stop();
        await endServer(active);
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
      if (!current.current?.input.signal.aborted) stop();
    };
  }, [stop]);

  useEffect(() => {
    if (
      current.current &&
      !current.current.input.signal.aborted &&
      (!enabled || current.current.sessionId !== sessionId)
    )
      stop();
  }, [enabled, sessionId, stop]);
  useEffect(() => {
    if (current.current) current.current.meeting.context = context(current.current.sessionId);
  }, [roster, context]);
  return null;
}
