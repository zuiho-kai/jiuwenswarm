import { claimMeetingAudio } from '../../../channels/web/frontend/src/features/teamMeeting/playbackOwnership';
import { onTtsStop, requestTtsStop } from '../../../channels/web/frontend/src/utils/ttsControl';
import type { SpeechDetection } from '../../../channels/web/frontend/src/utils/speechDetection/speechGate';
import { MeetingVad, type MeetingSpeechMonitor, type MeetingSpeechMonitorFactory } from './meetingVad';

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
  private recorder: MediaRecorder | null = null;
  private recordingTimer: ReturnType<typeof setTimeout> | null = null;
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
  private transcribing = false;
  private finishingRecording = false;
  private speechDetected = false;
  private vad: MeetingSpeechMonitor | null = null;

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
    private readonly createVad: MeetingSpeechMonitorFactory = (...args) => new MeetingVad(...args),
  ) {
    this.releaseAudio = claimMeetingAudio(sessionId);
    requestTtsStop();
  }

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
    try {
      this.vad = this.createVad(
        media,
        (detection) => this.detectSpeech(detection),
        () => this.disableVad(),
      );
      await this.vad.start();
    } catch {
      if (!this.disposed) this.disableVad();
    }
  }

  private disableVad(): void {
    this.vad?.stop();
    this.vad = null;
    if (this.active) this.callbacks.error('vadUnavailable');
  }

  private detectSpeech(detection: SpeechDetection): void {
    if (!this.active || this.recorder?.state !== 'recording' || this.finishingRecording) return;
    if (detection.state === 'started') this.speechDetected = true;
    if (detection.state === 'ended' && this.speechDetected) this.stopRecording();
  }

  startRecording(takeFloor = true): void {
    if (
      !this.active ||
      !this.media ||
      this.transcribing ||
      this.finishingRecording ||
      this.recorder?.state === 'recording' ||
      (!takeFloor && this.seen.size > 0)
    )
      return;
    // Taking the microphone holds any current speech lease until End meeting.
    if (takeFloor) this.stopPlayback();
    this.claimAudio();
    const mimeType = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'].find((mime) =>
      MediaRecorder.isTypeSupported(mime),
    );
    try {
      this.vad?.reset();
      this.speechDetected = false;
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
        this.finishingRecording = false;
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
      this.recordingTimer = setTimeout(() => {
        // Do not send a full recording of silence/noise to ASR. Manual finish remains available.
        if (this.vad && !this.speechDetected) rejected = true;
        this.stopRecording();
      }, 30000);
    } catch {
      this.callbacks.error('recordingFailed');
    }
  }

  stopRecording(): void {
    this.clearRecordingTimer();
    if (this.recorder?.state === 'recording') {
      this.finishingRecording = true;
      this.recorder.stop();
    }
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

  async activatePlayback(meetingId: string, startupId: string): Promise<void> {
    if (!this.active || this.disposed) throw new Error('disconnected');
    this.meetingId = meetingId;
    const result = await this.transport.request<{ ready?: boolean }>(
      'meeting.playback.ready',
      {
        session_id: this.sessionId,
        meeting_id: meetingId,
        startup_id: startupId,
      },
      { timeoutMs: 20000 },
    );
    if (!result.ready) throw new Error('playbackUnavailable');
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
        this.callbacks.line({ id: speech.source_message_id, speaker: speech.speaker_id, text: speech.chunks.join('') });
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
    const params = { ...identity(attempt.speech), outcome, completed_chunks: attempt.completed };
    // Identical receipts are idempotent even if the next speaker has been granted.
    try {
      await this.transport.request('meeting.playback.result', params, { timeoutMs: 20000 });
    } catch {
      await this.transport.request('meeting.playback.result', params, { timeoutMs: 20000 });
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
    this.vad?.stop();
    this.vad = null;
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
