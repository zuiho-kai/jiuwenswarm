// Provider transport only: consumers own prompts, microphones, tools and playback.

export interface QwenOmniConnectionOptions {
  url: string;
  sessionUpdate: Record<string, unknown>;
  onEvent: (event: Record<string, unknown>, raw: string) => void;
  onError: (message: string) => void;
  onClose: () => void;
  onClosing?: () => void;
  onDiagnostic?: (event: string, details: Record<string, unknown>) => void;
  disconnectMessage?: (code: number, reason: string, serviceError: string) => string;
}

export function readableError(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value && typeof value === 'object') {
    const error = value as Record<string, unknown>;
    const message = error.message || error.detail || error.code || error.error;
    if (message) return readableError(message);
    try {
      return JSON.stringify(value);
    } catch {
      return 'Realtime 服务错误';
    }
  }
  return String(value || 'Realtime 服务错误');
}

export class QwenOmniConnection {
  socket: WebSocket | null = null;
  ready = false;
  private rejectOpening: ((error: Error) => void) | null = null;

  close(): void {
    const socket = this.socket;
    this.socket = null;
    this.ready = false;
    this.rejectOpening?.(new Error('Realtime connection closed'));
    this.rejectOpening = null;
    socket?.close(1000, 'client stop');
  }

  open(options: QwenOmniConnectionOptions): Promise<void> {
    this.close();
    return new Promise((resolve, reject) => {
      const url = new URL(options.url);
      const socket = new WebSocket(url);
      this.socket = socket;
      let initSent = false;
      let settled = false;
      let reportedStartupError = false;
      let lastServiceError = '';
      const initTimeout = window.setTimeout(() => {
        if (settled) return;
        settled = true;
        socket.close(1000, 'session init timeout');
        reject(new Error('Realtime 会话初始化超时，远端未返回 session.created'));
      }, 30_000);
      const resolveOnce = () => {
        if (settled) return;
        settled = true;
        window.clearTimeout(initTimeout);
        resolve();
      };
      const rejectOnce = (error: Error) => {
        if (settled) return;
        settled = true;
        window.clearTimeout(initTimeout);
        reject(error);
      };
      this.rejectOpening = rejectOnce;
      const sendInit = async () => {
        if (initSent) return;
        initSent = true;
        this.send(options.sessionUpdate);
      };
      socket.onopen = () => {
        if (this.socket !== socket) return;
        options.onDiagnostic?.('realtime_websocket_open', { url: url.toString() });
        void sendInit();
        // Preserve the existing connection timing. Consumers may send input
        // once open; ready separately tracks the provider's session events.
        resolveOnce();
      };
      socket.onmessage = ({ data }) => {
        if (this.socket !== socket) return;
        if (typeof data !== 'string') return;
        try {
          const event = JSON.parse(data) as Record<string, unknown>;
          const type = String(event.type || '');
          if (type === 'error') {
            const error = event.error as Record<string, unknown> | undefined;
            // The gateway's close notice must not erase the provider's quota/auth error.
            if (lastServiceError && error?.code === 'qwen_gateway_upstream_closed') return;
            lastServiceError = readableError(event.error || event);
          }
          const response = event.response as Record<string, unknown> | undefined;
          if (type === 'response.done' && response?.status === 'failed') {
            const details = response.status_details as Record<string, unknown> | undefined;
            lastServiceError = readableError(
              details?.error || details || 'Realtime 模型响应失败，服务端未提供具体原因',
            );
          }
          if (type === 'session.closed') {
            if (!this.ready) reportedStartupError = true;
            const closeReason = readableError(
              event.error || event.reason || lastServiceError || '远端已结束会话，未提供具体原因',
            );
            const startupAlreadyResolved = settled;
            options.onDiagnostic?.('realtime_websocket_error', {
              url: url.toString(),
              message: closeReason,
            });
            const message = `${this.ready ? 'Realtime 会话已结束' : 'Realtime 会话初始化失败'}：${closeReason}`;
            rejectOnce(new Error(message));
            if (startupAlreadyResolved) options.onError(message);
            try {
              options.onClose();
            } finally {
              if (this.socket === socket) this.close();
            }
            return;
          }
          if (type === 'session.queue_done' || type === 'queue_done') {
            void sendInit();
            return;
          }
          if (type === 'session.updated' || type === 'session.created') {
            this.ready = true;
            options.onDiagnostic?.('realtime_session_ready', {});
          }
          if (type === 'error' && !this.ready) {
            reportedStartupError = true;
            rejectOnce(new Error(readableError(event.error || event)));
          }
          options.onEvent(event, data);
        } catch {
          options.onError('Realtime 返回了无效事件');
        }
      };
      socket.onerror = () => {
        if (this.socket !== socket) return;
        options.onDiagnostic?.('realtime_websocket_error', {
          url: url.toString(),
        });
        const message = lastServiceError || 'Realtime 网络连接异常，浏览器未提供具体原因，请检查网络或模型服务。';
        const startupAlreadyResolved = settled;
        rejectOnce(new Error(message));
        if (startupAlreadyResolved) options.onError(message);
      };
      socket.onclose = ({ code, reason }) => {
        if (this.socket !== socket) return;
        options.onClosing?.();
        options.onDiagnostic?.('realtime_websocket_closed', {
          code,
          message: reason,
        });
        if (!this.ready && !reportedStartupError) {
          const closeReason =
            lastServiceError || reason || (code === 1000 ? '远端在初始化阶段主动关闭' : `关闭代码 ${code}`);
          const startupAlreadyResolved = settled;
          rejectOnce(new Error(`Realtime 会话初始化失败：${closeReason}`));
          if (startupAlreadyResolved) options.onError(`Realtime 会话初始化失败：${closeReason}`);
        } else if (this.ready) {
          options.onError(
            options.disconnectMessage?.(code, reason, lastServiceError) ||
              lastServiceError ||
              `Realtime 连接已断开（${code}：${reason || '服务端未提供具体原因'}）`,
          );
        }
        // Consumers release their media/work; provider state is always closed.
        try {
          options.onClose();
        } finally {
          if (this.socket === socket) this.close();
        }
      };
    });
  }

  send(event: Record<string, unknown>): void {
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(event));
  }
}
