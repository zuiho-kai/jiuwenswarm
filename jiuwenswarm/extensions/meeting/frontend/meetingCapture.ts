import { JoyAIVoiceSession, resamplePcm16 } from '../../video_duplex/frontend/VideoLivePanel/joyaiVoice';

export interface MeetingCapture {
  start(): Promise<void>;
  stop(): void;
  setListening?(listening: boolean): void;
}

export type MeetingCaptureFactory = (
  stream: MediaStream,
  onPcm: (pcm: Int16Array) => void,
  onError: (error?: unknown) => void,
  onSpeech?: (active: boolean) => void,
  onTurn?: (audioDataUrl: string) => void,
) => MeetingCapture;

/** Current meetings reuse JoyAI capture and segmentation; PCM callbacks support older callers. */
export class MeetingPcmCapture implements MeetingCapture {
  private joyai: JoyAIVoiceSession | null = null;
  private context: AudioContext | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private capture: AudioWorkletNode | null = null;
  private silent: GainNode | null = null;
  private stopped = false;
  private listening = true;
  private vad: import('../../../channels/web/frontend/src/utils/speechDetection/sileroVad').SileroVad | null = null;

  constructor(
    private readonly stream: MediaStream,
    private readonly onPcm: (pcm: Int16Array) => void,
    private readonly onError: (error?: unknown) => void,
    private readonly onSpeech?: (active: boolean) => void,
    private readonly onTurn?: (audioDataUrl: string) => void,
  ) {}

  async start(): Promise<void> {
    if (this.stopped) return;
    try {
      if (this.onTurn) {
        this.joyai = new JoyAIVoiceSession({
          onSpeechStart: () => this.onSpeech?.(true),
          onTurnAudio: (audio) => {
            this.onSpeech?.(false);
            this.onTurn?.(audio);
          },
          onState: () => {},
          onError: (message) => this.onError(message),
        });
        this.joyai.setListening(this.listening);
        await this.joyai.start(this.stream);
        return;
      }
      const context = new AudioContext({ sampleRate: 16_000 });
      this.context = context;
      await context.audioWorklet.addModule(
        new URL('../../video_duplex/frontend/VideoLivePanel/duplex-capture.js', import.meta.url),
      );
      if (this.stopped) return;
      if (this.onSpeech) {
        const { SileroVad } = await import('../../../channels/web/frontend/src/utils/speechDetection/sileroVad');
        if (this.stopped) return;
        this.vad = new SileroVad(
          (detection) => {
            if (this.stopped || !this.listening) return;
            if (detection.state === 'started') this.onSpeech?.(true);
            else if (detection.state === 'ended') this.onSpeech?.(false);
          },
          (message) => this.onError(message),
        );
        await this.vad.start();
        if (this.stopped) return;
      }
      this.source = context.createMediaStreamSource(this.stream);
      this.capture = new AudioWorkletNode(context, 'jiuwen-duplex-capture');
      this.capture.port.onmessage = ({ data }) => {
        if (!this.stopped) {
          const pcm = resamplePcm16(new Int16Array(data), context.sampleRate, 16_000);
          this.onPcm(pcm);
          if (this.listening) this.vad?.push(pcm);
        }
      };
      this.capture.onprocessorerror = () => {
        if (!this.stopped) this.onError(new Error('AudioWorklet processor stopped while capturing microphone audio'));
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

  setListening(listening: boolean): void {
    this.joyai?.setListening(listening);
    this.listening = listening;
    this.vad?.reset();
    if (!listening) this.onSpeech?.(false);
  }

  stop(): void {
    if (this.stopped) return;
    this.stopped = true;
    this.joyai?.stop();
    this.joyai = null;
    this.vad?.stop();
    this.vad = null;
    if (this.capture) {
      this.capture.port.onmessage = null;
      this.capture.onprocessorerror = null;
      this.capture.port.close();
      this.capture.disconnect();
    }
    this.source?.disconnect();
    this.silent?.disconnect();
    void this.context?.close().catch(() => {});
  }
}
