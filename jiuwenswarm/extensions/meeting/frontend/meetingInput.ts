import type { ChatSendOptions } from '../../../channels/web/frontend/src/types';
import type { Transport } from './meetingRuntime';

export type MeetingSender = (text: string, options?: ChatSendOptions) => void | Promise<boolean | void>;

/** Wait for public-message acceptance; the gateway's chat.send ACK is earlier. */
export function sendMeetingInput(
  transport: Transport,
  send: MeetingSender,
  sessionId: string,
  text: string,
  clientMessageId: string,
  mentions: string[],
  signal: AbortSignal,
): Promise<void> {
  return new Promise((resolve, reject) => {
    let settled = false;
    let requestId: string | undefined;
    const off: Array<() => void> = [];
    const timer = setTimeout(() => finish(new Error('deliveryFailed')), 60000);
    const finish = (error?: Error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      off.forEach((unsubscribe) => unsubscribe());
      signal.removeEventListener('abort', abort);
      if (error) reject(error);
      else resolve();
    };
    const abort = () => finish(new Error('disconnected'));
    off.push(
      transport.on('team.group_message.accepted', ({ payload }) => {
        const message = payload.message as Record<string, unknown> | undefined;
        if (
          payload.session_id === sessionId &&
          (payload.client_message_id || message?.client_message_id) === clientMessageId
        )
          finish();
      }),
      transport.on('chat.error', ({ payload }) => {
        if (payload.session_id !== sessionId || !requestId || payload.request_id !== requestId) return;
        const error = String(payload.error || payload.message || 'deliveryFailed');
        finish(
          new Error(
            /conversation.mode|conversation mode/i.test(error) ? 'meetingRequiresNewConversation' : 'deliveryFailed',
          ),
        );
      }),
      transport.onStateChange((state) => {
        if (state !== 'ready') finish(new Error('disconnected'));
      }),
    );
    signal.addEventListener('abort', abort, { once: true });
    if (signal.aborted) {
      abort();
      return;
    }
    Promise.resolve()
      .then(() => {
        if (settled) return;
        return send(text, {
          groupChat: {
            sessionId,
            clientMessageId,
            mentions,
            onRequestId: (id) => {
              requestId = id;
            },
          },
        });
      })
      .then((sent) => {
        if (sent === false) finish(new Error('deliveryFailed'));
      })
      .catch((error) => finish(error instanceof Error ? error : new Error('deliveryFailed')));
  });
}
