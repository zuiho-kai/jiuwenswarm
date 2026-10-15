import { SileroVad, type SpeechDetection } from '../../../channels/web/frontend/src/utils/speechDetection/sileroVad';
import { resamplePcm16 } from '../../video_duplex/frontend/VideoLivePanel/joyaiVoice';

export interface MeetingSpeechMonitor {
  start(): Promise<void>;
  reset(): void;
  stop(): void;
}

export type MeetingSpeechMonitorFactory = (
  stream: MediaStream,
  onDetection: (detection: SpeechDetection) => void,
  onError: () => void,
) => MeetingSpeechMonitor;

/** Observe the meeting's existing microphone stream; ASR still receives the recorder's blob. */
export class MeetingVad implements MeetingSpeechMonitor {
  private readonly vad: SileroVad;
  private context: AudioContext | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private capture: AudioWorkletNode | null = null;
  private silent: GainNode | null = null;
  private stopped = false;

  constructor(
    private readonly stream: MediaStream,
    onDetection: (detection: SpeechDetection) => void,
    private readonly onError: () => void,
  ) {
    this.vad = new SileroVad(
      (detection) => {
        if (!this.stopped) onDetection(detection);
      },
      () => {
        if (!this.stopped) onError();
      },
    );
  }

  async start(): Promise<void> {
    if (this.stopped) return;
    try {
      await this.vad.start();
      if (this.stopped) return;
      const context = new AudioContext({ sampleRate: 16_000 });
      this.context = context;
      await context.audioWorklet.addModule(
        new URL('../../video_duplex/frontend/VideoLivePanel/duplex-capture.js', import.meta.url),
      );
      if (this.stopped) return;
      this.source = context.createMediaStreamSource(this.stream);
      this.capture = new AudioWorkletNode(context, 'jiuwen-duplex-capture');
      this.capture.port.onmessage = ({ data }) => {
        if (!this.stopped) this.vad.push(resamplePcm16(new Int16Array(data), context.sampleRate, 16_000));
      };
      this.capture.onprocessorerror = () => {
        if (!this.stopped) this.onError();
      };
      this.silent = context.createGain();
      this.silent.gain.value = 0;
      this.source.connect(this.capture);
      this.capture.connect(this.silent).connect(context.destination);
      await context.resume();
    } catch (error) {
      this.stop();
      throw error;
    }
  }

  reset(): void {
    this.vad.reset();
  }

  stop(): void {
    if (this.stopped) return;
    this.stopped = true;
    this.vad.stop();
    if (this.capture) {
      this.capture.port.onmessage = null;
      this.capture.onprocessorerror = null;
      this.capture.port.close();
      this.capture.disconnect();
      this.capture = null;
    }
    this.source?.disconnect();
    this.source = null;
    this.silent?.disconnect();
    this.silent = null;
    if (this.context) void this.context.close().catch(() => {});
    this.context = null;
  }
}
