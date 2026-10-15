export type MeetingMember = { member_name: string; display_name: string };
export type MeetingContext = { leader: string; members: MeetingMember[] };
type Payload = Record<string, unknown>;
export interface Transport {
  request<T = unknown>(method: string, params?: Payload, options?: { timeoutMs?: number }): Promise<T>;
  on(event: string, handler: (event: { payload: Payload }) => void): () => void;
  onStateChange(handler: (state: string) => void): () => void;
}
export type MeetingLine = { id: string; speaker: string; text: string };
export type RecordingState = 'ready' | 'recording' | 'transcribing';

export function speechChunks(text: string, limit = 800): string[] {
  const chunks: string[] = [];
  let rest = text.trim();
  while (rest.length > limit) {
    const prefix = rest.slice(0, limit);
    const boundary = Math.max(prefix.lastIndexOf('。'), prefix.lastIndexOf('. '), prefix.lastIndexOf('\n'));
    const end = boundary >= limit / 2 ? boundary + 1 : limit;
    chunks.push(rest.slice(0, end));
    rest = rest.slice(end).trimStart();
  }
  if (rest) chunks.push(rest);
  return chunks;
}

export function replyFromEvent(name: string, payload: Payload, leader: string): MeetingLine | null {
  if (name === 'chat.final') {
    const text = payload.content ?? payload.output;
    if (typeof text !== 'string' || !text.trim()) return null;
    const speaker = String(payload.member_name || leader);
    if (speaker !== leader || ['teammate', 'worker', 'human_agent'].includes(String(payload.role || ''))) return null;
    return {
      id: String(payload.message_id || payload.request_id || ''),
      speaker,
      text,
    };
  }
  const event = payload.event as Payload | undefined;
  if (
    !event ||
    event.type !== 'team.message.p2p' ||
    event.to_member !== 'user' ||
    typeof event.content !== 'string' ||
    !event.content.trim()
  )
    return null;
  return {
    id: String(event.message_id || ''),
    speaker: String(event.from_member || leader),
    text: event.content,
  };
}

export class MeetingSession {
  private active = false;
  private disposed = false;
  private media: MediaStream | null = null;
  private recorder: MediaRecorder | null = null;
  private recordingTimer: ReturnType<typeof setTimeout> | null = null;
  private off: Array<() => void> = [];
  private playbackEpoch = 0;
  private finishAudio: (() => void) | null = null;
  private speechTail = Promise.resolve();
  private seen = new Set<string>();
  private recent = new Map<string, { source: string; time: number }>();
  private transcribing = false;
  private pendingSpeech = 0;

  constructor(
    private transport: Transport,
    private sessionId: string,
    public context: MeetingContext,
    private callbacks: {
      line: (line: MeetingLine) => void;
      draft: (text: string) => void;
      error: (code: string) => void;
      recording: (state: RecordingState) => void;
      ended: () => void;
    },
  ) {}

  async start(): Promise<void> {
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') throw new Error('unsupported');
    const media = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    if (this.disposed) {
      media.getTracks().forEach((track) => track.stop());
      return;
    }
    this.media = media;
    this.active = true;
    media.getAudioTracks().forEach((track) => {
      track.onended = () => this.fail('microphoneEnded');
    });
    this.off.push(
      this.transport.onStateChange((state) => {
        if (state !== 'ready') this.fail('disconnected');
      }),
    );
    this.off.push(
      this.transport.on('chat.error', ({ payload }) => {
        if (this.active && payload.session_id === this.sessionId) this.callbacks.error('deliveryFailed');
      }),
    );
    for (const name of ['chat.final', 'team.message', 'team.event']) {
      this.off.push(
        this.transport.on(name, ({ payload }) => {
          if (!this.active || payload.session_id !== this.sessionId) return;
          const line = replyFromEvent(name, payload, this.context.leader);
          if (line) this.reply(name, line);
        }),
      );
    }
  }

  startRecording(): void {
    if (!this.active || !this.media || this.transcribing || this.recorder?.state === 'recording') return;
    // Recording is an explicit request to take the floor. Automatic speech
    // interruption will be supplied by the later meeting backend.
    this.stopPlayback();
    const mimeType = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'].find((mime) =>
      MediaRecorder.isTypeSupported(mime),
    );
    try {
      const recorder = new MediaRecorder(this.media, mimeType ? { mimeType } : undefined);
      this.recorder = recorder;
      const parts: Blob[] = [];
      let bytes = 0;
      let rejected = false;
      recorder.ondataavailable = ({ data }) => {
        bytes += data.size;
        if (bytes > 1_400_000) {
          rejected = true;
          this.callbacks.error('recordingTooLarge');
          this.stopRecording();
        } else if (data.size) parts.push(data);
      };
      recorder.onerror = () => {
        rejected = true;
        this.callbacks.error('recordingFailed');
        this.stopRecording();
        if (this.active) this.callbacks.recording('ready');
      };
      recorder.onstop = () => {
        this.clearRecordingTimer();
        if (!this.active || rejected) {
          if (this.active) this.callbacks.recording('ready');
          return;
        }
        void this.transcribe(
          new Blob(parts, {
            type: recorder.mimeType || mimeType || 'audio/webm',
          }),
        );
      };
      recorder.start(250);
      this.callbacks.recording('recording');
      this.recordingTimer = setTimeout(() => this.stopRecording(), 30000);
    } catch {
      this.callbacks.error('recordingFailed');
    }
  }

  stopRecording(): void {
    this.clearRecordingTimer();
    if (this.recorder?.state === 'recording') this.recorder.stop();
  }

  private clearRecordingTimer(): void {
    if (this.recordingTimer !== null) clearTimeout(this.recordingTimer);
    this.recordingTimer = null;
  }

  private async transcribe(blob: Blob): Promise<void> {
    if (!blob.size || !this.active) {
      this.callbacks.recording('ready');
      return;
    }
    this.transcribing = true;
    this.callbacks.recording('transcribing');
    try {
      const audioDataUrl = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.onerror = () => reject(new Error('Audio read failed'));
        reader.readAsDataURL(blob);
      });
      if (!this.active) return;
      const result = await this.transport.request<{ transcript?: string }>(
        'video.transcribe',
        {
          session_id: this.sessionId,
          audio_data_url: audioDataUrl,
        },
        { timeoutMs: 60000 },
      );
      if (this.active) {
        if (result.transcript?.trim()) this.callbacks.draft(result.transcript.trim());
        else this.callbacks.error('emptyTranscript');
      }
    } catch {
      if (this.active) this.callbacks.error('transcriptionFailed');
    } finally {
      this.transcribing = false;
      if (this.active) this.callbacks.recording('ready');
    }
  }

  private reply(source: string, line: MeetingLine): void {
    const id = `${line.speaker}:${line.id || line.text}`;
    if (this.seen.has(id)) return;
    this.seen.add(id);
    if (this.seen.size > 2000) this.seen.delete(this.seen.values().next().value!);
    const contentKey = `${line.speaker}:${line.text}`;
    const previous = this.recent.get(contentKey);
    this.recent.set(contentKey, { source, time: Date.now() });
    if (this.recent.size > 200) this.recent.delete(this.recent.keys().next().value!);
    if (previous && previous.source !== source && Date.now() - previous.time < 10000) return;
    this.callbacks.line(line);
    const chunks = speechChunks(line.text);
    if (this.pendingSpeech + chunks.length > 64) {
      this.callbacks.error('playbackOverloaded');
      return;
    }
    const epoch = this.playbackEpoch;
    for (const text of chunks) {
      this.pendingSpeech++;
      this.speechTail = this.speechTail
        .then(async () => {
          if (!this.active || epoch !== this.playbackEpoch) return;
          // Backend voice matching is deliberately deferred. All members use the
          // existing endpoint's configured default voice in this frontend pass.
          const result = await this.transport.request<{
            success: boolean;
            audio_base64?: string;
            audio_mime?: string;
          }>('tts.synthesize', { session_id: this.sessionId, text }, { timeoutMs: 60000 });
          if (!this.active || epoch !== this.playbackEpoch) return;
          if (!result.success || !result.audio_base64) throw new Error('No synthesized audio');
          await this.play(result.audio_base64, result.audio_mime || 'audio/mpeg');
        })
        .catch(() => {
          if (this.active && epoch === this.playbackEpoch) this.callbacks.error('playbackFailed');
        })
        .finally(() => {
          if (epoch === this.playbackEpoch) this.pendingSpeech--;
        });
    }
  }

  private play(encoded: string, mime: string): Promise<void> {
    const bytes = Uint8Array.from(atob(encoded), (char) => char.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], { type: mime }));
    const audio = new Audio(url);
    return new Promise<void>((resolve, reject) => {
      let finished = false;
      const finish = (error?: Error) => {
        if (finished) return;
        finished = true;
        audio.pause();
        audio.onended = null;
        audio.onerror = null;
        URL.revokeObjectURL(url);
        if (this.finishAudio === cancel) this.finishAudio = null;
        if (error) reject(error);
        else resolve();
      };
      const cancel = () => finish();
      this.finishAudio = cancel;
      audio.onended = () => finish();
      audio.onerror = () => finish(new Error('Audio playback failed'));
      void audio.play().catch((error) => finish(error));
    });
  }

  stopPlayback(): void {
    this.playbackEpoch++;
    this.finishAudio?.();
    this.speechTail = Promise.resolve();
    this.pendingSpeech = 0;
  }

  private fail(code: string): void {
    if (!this.active) return;
    this.callbacks.error(code);
    this.stop();
  }

  stop(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.active = false;
    this.stopPlayback();
    this.clearRecordingTimer();
    this.off.splice(0).forEach((off) => off());
    if (this.recorder) {
      this.recorder.onstop = null;
      this.recorder.ondataavailable = null;
      this.recorder.onerror = null;
      if (this.recorder.state === 'recording') this.recorder.stop();
    }
    this.media?.getTracks().forEach((track) => {
      track.onended = null;
      track.stop();
    });
    this.callbacks.ended();
  }
}
