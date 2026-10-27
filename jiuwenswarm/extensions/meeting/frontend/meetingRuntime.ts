import { claimMeetingAudio } from '../../../channels/web/frontend/src/features/teamMeeting/playbackOwnership';
import { onTtsStop, requestTtsStop } from '../../../channels/web/frontend/src/utils/ttsControl';
import { MeetingPcmCapture, type MeetingCapture, type MeetingCaptureFactory } from './meetingCapture';

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

export type PlaybackState = 'idle' | 'loading' | 'playing' | 'paused' | 'failed';
type Speech = Payload & {
  session_id: string;
  meeting_id: string;
  request_id: string;
  lease_token: string;
  source_message_id: string;
  speaker_id: string;
  playback_id: string;
  chunks: string[];
  chunk_count: number;
  voice: string;
};
type Attempt = { speech: Speech; completed: number; reported: boolean };
const identityFields = [
  'session_id',
  'meeting_id',
  'request_id',
  'lease_token',
  'source_message_id',
  'speaker_id',
  'playback_id',
] as const;
function identity(speech: Speech): Payload {
  return Object.fromEntries(identityFields.map((key) => [key, speech[key]]));
}

export class MeetingSession {
  private active = false;
  private disposed = false;
  private media: MediaStream | null = null;
  private capture: MeetingCapture | null = null;
  private streamId: string | null = null;
  private listening = false;
  private resuming = false;
  private inputEpoch = 0;
  private inputSequence = 0;
  private inputTail = Promise.resolve();
  private pendingSamples = 0;
  private lastUtterance = 0;
  private off: Array<() => void> = [];
  private playbackEpoch = 0;
  private finishAudio: (() => void) | null = null;
  private speechTail = Promise.resolve();
  private seen = new Set<string>();
  private meetingId: string | null = null;
  private currentSpeech: Attempt | null = null;
  private paused = false;
  private claimingAudio = false;
  private offStop: (() => void) | null = null;
  private releaseAudio: () => void;
  private offAudio: (() => void) | null = null;

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
      playback?: (state: PlaybackState, speaker?: string, voice?: string) => void;
    },
    private readonly createCapture: MeetingCaptureFactory = (...args) => new MeetingPcmCapture(...args),
  ) {
    this.releaseAudio = claimMeetingAudio(sessionId);
    requestTtsStop();
  }

  async start(): Promise<void> {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error('unsupported');
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
    this.off.push(this.transport.on('meeting.speech.ready', ({ payload }) => this.receiveSpeech(payload)));
    this.off.push(
      this.transport.on('meeting.speech.error', ({ payload }) => {
        if (!this.matchesMeeting(payload) || !this.active) return;
        // A held lease has no automatic resume in this version.
        if (this.paused) return;
        this.stopPlayback(false);
        this.callbacks.playback?.('failed');
      }),
    );
    // This listener intentionally survives local teardown until End is confirmed.
    this.offStop = this.transport.on('meeting.playback.stop', ({ payload }) => {
      if (!this.matchesMeeting(payload) || typeof payload.stop_id !== 'string') return;
      this.stopPlayback(false);
      if (this.active) this.stop();
      void this.transport
        .request(
          'meeting.playback.stopped',
          {
            session_id: this.sessionId,
            meeting_id: this.meetingId,
            stop_id: payload.stop_id,
          },
          { timeoutMs: 20000 },
        )
        .catch(() => this.callbacks.error('cleanupFailed'));
    });
    this.offAudio = onTtsStop(() => {
      if (!this.claimingAudio && this.currentSpeech) this.stopPlayback();
    });
    this.off.push(
      this.transport.on('meeting.input.transcribing', ({ payload }) => {
        if (this.matchesInput(payload) && !this.listening) this.callbacks.recording('transcribing');
      }),
      this.transport.on('meeting.input.transcript', ({ payload }) => {
        if (!this.matchesInput(payload) || !this.acceptUtterance(payload)) return;
        if (typeof payload.transcript === 'string' && payload.transcript.trim())
          this.callbacks.draft(payload.transcript.trim());
        this.callbacks.recording(this.listening ? 'recording' : 'ready');
      }),
      this.transport.on('meeting.input.error', ({ payload }) => {
        if (!this.matchesInput(payload) || !this.acceptUtterance(payload)) return;
        this.callbacks.error('transcriptionFailed');
        this.callbacks.recording(this.listening ? 'recording' : 'ready');
      }),
    );
    this.capture = this.createCapture(
      media,
      (pcm) => this.sendPcm(pcm),
      () => this.fail('recordingFailed'),
    );
    await this.capture.start();
  }

  private matchesInput(payload: Payload): boolean {
    return this.active && !!this.streamId && this.matchesMeeting(payload) && payload.stream_id === this.streamId;
  }

  private acceptUtterance(payload: Payload): boolean {
    const id = payload.utterance_id;
    if (typeof id !== 'number' || !Number.isSafeInteger(id) || id <= this.lastUtterance) return false;
    this.lastUtterance = id;
    return true;
  }

  private inputIdentity(): Payload {
    return {
      session_id: this.sessionId,
      meeting_id: this.meetingId,
      stream_id: this.streamId,
    };
  }

  private queueInput(operation: () => Promise<void>): void {
    const epoch = this.inputEpoch;
    this.inputTail = this.inputTail
      .then(async () => {
        if (this.active && epoch === this.inputEpoch) await operation();
      })
      .catch(() => {
        if (this.active && epoch === this.inputEpoch) this.fail('inputStreamFailed');
      });
  }

  private sendPcm(pcm: Int16Array): void {
    if (!this.active || !this.listening || !this.streamId || !pcm.length) return;
    // A stalled connection must not build an unbounded microphone backlog.
    if (pcm.length > 3200 || this.pendingSamples + pcm.length > 32000) {
      this.fail('inputStreamFailed');
      return;
    }
    this.pendingSamples += pcm.length;
    const bytes = new Uint8Array(pcm.length * 2);
    const view = new DataView(bytes.buffer);
    pcm.forEach((sample, index) => view.setInt16(index * 2, sample, true));
    const encoded = btoa(String.fromCharCode(...bytes));
    const epoch = this.inputEpoch;
    const sequence = this.inputSequence++;
    // Send without waiting for earlier acknowledgements. The tail is a barrier
    // for Pause/Resume, while pendingSamples bounds the complete in-flight window.
    const request = Promise.resolve().then(async () => {
      try {
        if (!this.active || epoch !== this.inputEpoch) return;
        const result = await this.transport.request<{ accepted?: boolean }>(
          'meeting.input.append',
          {
            ...this.inputIdentity(),
            sequence,
            audio_base64: encoded,
          },
          { timeoutMs: 10000 },
        );
        if (!result.accepted) throw new Error('inputStreamFailed');
      } finally {
        if (epoch === this.inputEpoch) this.pendingSamples -= pcm.length;
      }
    });
    this.inputTail = Promise.all([this.inputTail, request])
      .then(() => {})
      .catch(() => {
        if (this.active && epoch === this.inputEpoch) this.fail('inputStreamFailed');
      });
  }

  startRecording(): void {
    if (!this.active || !this.streamId || this.listening || this.resuming) return;
    this.resuming = true;
    this.queueInput(async () => {
      try {
        const result = await this.transport.request<{ accepted?: boolean }>('meeting.input.resume', this.inputIdentity());
        if (!result.accepted) throw new Error('inputStreamFailed');
        if (!this.active) return;
        this.listening = true;
        this.callbacks.recording('recording');
      } finally {
        this.resuming = false;
      }
    });
  }

  stopRecording(): void {
    if (!this.active || !this.streamId || !this.listening) return;
    this.listening = false;
    this.callbacks.recording('ready');
    this.queueInput(async () => {
      const result = await this.transport.request<{ accepted?: boolean }>('meeting.input.pause', this.inputIdentity());
      if (!result.accepted) throw new Error('inputStreamFailed');
    });
  }

  async activatePlayback(meetingId: string, startupId: string): Promise<void> {
    if (!this.active || this.disposed) throw new Error('disconnected');
    this.meetingId = meetingId;
    const result = await this.transport.request<{ ready?: boolean; stream_id?: string }>(
      'meeting.playback.ready',
      {
        session_id: this.sessionId,
        meeting_id: meetingId,
        startup_id: startupId,
      },
      { timeoutMs: 20000 },
    );
    if (!this.active || this.disposed) return;
    if (!result.ready || !result.stream_id) throw new Error('playbackUnavailable');
    this.streamId = result.stream_id;
    this.listening = true;
    this.callbacks.recording('recording');
  }

  private matchesMeeting(payload: Payload): boolean {
    return !!this.meetingId && payload.session_id === this.sessionId && payload.meeting_id === this.meetingId;
  }

  private receiveSpeech(payload: Payload): void {
    if (!this.active || !this.matchesMeeting(payload)) return;
    if (identityFields.some((key) => typeof payload[key] !== 'string' || !payload[key])) return;
    if (
      !Array.isArray(payload.chunks) ||
      !payload.chunks.length ||
      payload.chunks.length > 64 ||
      payload.chunk_count !== payload.chunks.length ||
      payload.chunks.some((text) => typeof text !== 'string' || !text)
    )
      return;
    const speech = payload as Speech;
    if (this.seen.has(speech.playback_id)) return;
    this.seen.add(speech.playback_id);
    const attempt: Attempt = { speech, completed: 0, reported: false };
    const epoch = this.playbackEpoch;
    this.speechTail = this.speechTail
      .then(async () => {
        if (!this.active || this.paused || epoch !== this.playbackEpoch) {
          await this.report(attempt, 'stopped');
          return;
        }
        this.currentSpeech = attempt;
        this.callbacks.line({
          id: speech.source_message_id,
          speaker: speech.speaker_id,
          text: speech.chunks.join(''),
        });
        try {
          for (let index = 0; index < speech.chunk_count; index++) {
            if (!this.active || this.paused || epoch !== this.playbackEpoch) return;
            this.callbacks.playback?.('loading', speech.speaker_id, speech.voice);
            const result = await this.transport.request<{
              success: boolean;
              playback_id: string;
              chunk_index: number;
              audio_base64?: string;
              audio_mime?: string;
            }>('meeting.speech.synthesize', { ...identity(speech), chunk_index: index }, { timeoutMs: 90000 });
            if (!this.active || this.paused || epoch !== this.playbackEpoch) return;
            if (
              !result.success ||
              !result.audio_base64 ||
              result.playback_id !== speech.playback_id ||
              result.chunk_index !== index
            )
              throw new Error('No matching synthesized audio');
            this.callbacks.playback?.('playing', speech.speaker_id, speech.voice);
            const outcome = await this.play(result.audio_base64, result.audio_mime || 'audio/wav');
            if (outcome !== 'completed' || !this.active || epoch !== this.playbackEpoch) return;
            attempt.completed++;
          }
          // Clear before acknowledging: the next ready event can arrive before the RPC response.
          if (this.currentSpeech === attempt) this.currentSpeech = null;
          await this.report(attempt, 'completed');
          if (!this.paused && this.active) this.callbacks.playback?.('idle');
        } catch {
          if (epoch !== this.playbackEpoch || !this.active) return;
          this.paused = true;
          this.callbacks.playback?.('failed', speech.speaker_id, speech.voice);
          this.callbacks.error('playbackFailed');
          await this.report(attempt, 'failed').catch(() => this.callbacks.error('playbackFailed'));
        } finally {
          if (this.currentSpeech === attempt) this.currentSpeech = null;
        }
      })
      .catch(() => {
        this.paused = true;
        this.callbacks.playback?.('failed');
        this.callbacks.error('playbackFailed');
      });
  }

  private async report(attempt: Attempt, outcome: 'completed' | 'stopped' | 'failed'): Promise<void> {
    if (attempt.reported) return;
    attempt.reported = true;
    const params = {
      ...identity(attempt.speech),
      outcome,
      completed_chunks: attempt.completed,
    };
    // Identical receipts are idempotent even if the next speaker has been granted.
    try {
      await this.transport.request('meeting.playback.result', params, {
        timeoutMs: 20000,
      });
    } catch {
      await this.transport.request('meeting.playback.result', params, {
        timeoutMs: 20000,
      });
    }
  }

  private claimAudio(): void {
    this.claimingAudio = true;
    try {
      requestTtsStop();
    } finally {
      this.claimingAudio = false;
    }
  }

  private play(encoded: string, mime: string): Promise<'completed' | 'stopped'> {
    this.claimAudio();
    const bytes = Uint8Array.from(atob(encoded), (char) => char.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], { type: mime }));
    const audio = new Audio(url);
    return new Promise((resolve, reject) => {
      let finished = false;
      const finish = (outcome: 'completed' | 'stopped', error?: Error) => {
        if (finished) return;
        finished = true;
        audio.pause();
        audio.onended = null;
        audio.onerror = null;
        URL.revokeObjectURL(url);
        if (this.finishAudio === cancel) this.finishAudio = null;
        if (error) reject(error);
        else resolve(outcome);
      };
      const cancel = () => finish('stopped');
      this.finishAudio = cancel;
      audio.onended = () => finish('completed');
      audio.onerror = () => finish('stopped', new Error('Audio playback failed'));
      try {
        void audio.play().catch((error) => finish('stopped', error));
      } catch (error) {
        finish('stopped', error instanceof Error ? error : new Error('Audio playback failed'));
      }
    });
  }

  stopPlayback(notify = true): void {
    this.playbackEpoch++;
    this.finishAudio?.();
    if (!this.meetingId) return;
    this.paused = true;
    this.callbacks.playback?.('paused');
    const attempt = this.currentSpeech;
    this.currentSpeech = null;
    if (notify && attempt) void this.report(attempt, 'stopped').catch(() => this.callbacks.error('playbackFailed'));
  }

  finishCleanup(): void {
    this.offStop?.();
    this.offStop = null;
    this.offAudio?.();
    this.offAudio = null;
    this.releaseAudio();
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
    this.listening = false;
    this.inputEpoch++;
    this.capture?.stop();
    this.capture = null;
    if (this.streamId) void this.transport.request('meeting.input.stop', this.inputIdentity()).catch(() => {});
    this.off.splice(0).forEach((off) => off());
    this.media?.getTracks().forEach((track) => {
      track.onended = null;
      track.stop();
    });
    this.callbacks.ended();
  }
}
