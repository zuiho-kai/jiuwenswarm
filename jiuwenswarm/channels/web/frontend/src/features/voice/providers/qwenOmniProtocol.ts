// Qwen Omni wire primitives. Consumers provide prompts, tools and turn policy.

const QWEN_MAX_BASE64_IMAGE_BYTES = 256 * 1024;

export interface QwenOmniSessionOptions {
  voice?: string;
  tools?: Array<Record<string, unknown>>;
  inputRate: number;
  outputRate: number;
  instructions: string;
  turnDetection?: Record<string, unknown> | null;
  inputAudioTranscription?: boolean;
}

export interface QwenOmniMediaBatch {
  events: Array<Record<string, unknown>>;
  diagnostics: Array<{ event: string; details: Record<string, unknown> }>;
}

export interface QwenOmniMediaSnapshot {
  audioAppendSequence: number;
  imageAppendSequence: number;
  hasDeferredImage: boolean;
}

export function createQwenOmniSessionUpdate(options: QwenOmniSessionOptions): Record<string, unknown> {
  return {
    type: 'session.update',
    session: {
      modalities: ['audio', 'text'],
      voice: options.voice || 'Tina',
      instructions: options.instructions,
      audio: {
        input: { format: { type: 'pcm', sample_rate: options.inputRate } },
        output: { format: { type: 'pcm', sample_rate: options.outputRate } },
      },
      turn_detection:
        options.turnDetection === undefined
          ? {
              type: 'semantic_vad',
              threshold: 0.5,
              silence_duration_ms: 800,
            }
          : options.turnDetection,
      enable_input_audio_transcription: options.inputAudioTranscription ?? true,
      tools: options.tools || [],
    },
  };
}

export function createQwenOmniTextTurnEvents(text: string): Array<Record<string, unknown>> {
  return [
    {
      type: 'conversation.item.create',
      item: {
        type: 'message',
        role: 'user',
        content: [{ type: 'input_text', text }],
      },
    },
    createQwenOmniResponseEvent(),
  ];
}

export function createQwenOmniResponseEvent(): Record<string, unknown> {
  return { type: 'response.create' };
}

export function createQwenOmniCancelResponseEvent(): Record<string, unknown> {
  return { type: 'response.cancel' };
}

export function createQwenOmniFinishSessionEvent(): Record<string, unknown> {
  return { type: 'session.finish' };
}

export class QwenOmniMediaSequencer {
  private audioAppendSequence = 0;
  private imageAppendSequence = 0;
  private deferredVideoFrame: string | null = null;

  createBatch(audio: string, includeVideo: boolean, frame: string | null): QwenOmniMediaBatch {
    const events: Array<Record<string, unknown>> = [
      {
        type: 'input_audio_buffer.append',
        audio,
      },
    ];
    const diagnostics: QwenOmniMediaBatch['diagnostics'] = [];
    this.audioAppendSequence += 1;
    if (!includeVideo) return { events, diagnostics };

    const deferredFrame = this.deferredVideoFrame;
    this.deferredVideoFrame = null;
    if (frame && frame.length > QWEN_MAX_BASE64_IMAGE_BYTES) {
      diagnostics.push({
        event: 'realtime_video_frame_dropped',
        details: { reason: 'qwen_image_too_large', base64_bytes: frame.length },
      });
    } else if (frame) {
      this.deferredVideoFrame = frame;
    }

    // DashScope requires an established audio segment before a video frame.
    // Holding each frame until the next audio flush prevents a first-frame race.
    if (!deferredFrame) {
      if (this.audioAppendSequence === 1 && this.deferredVideoFrame) {
        diagnostics.push({
          event: 'qwen_first_image_deferred',
          details: { audio_append_sequence: this.audioAppendSequence },
        });
      }
      return { events, diagnostics };
    }

    events.push({ type: 'input_image_buffer.append', image: deferredFrame });
    this.imageAppendSequence += 1;
    return { events, diagnostics };
  }

  snapshot(): QwenOmniMediaSnapshot {
    return {
      audioAppendSequence: this.audioAppendSequence,
      imageAppendSequence: this.imageAppendSequence,
      hasDeferredImage: Boolean(this.deferredVideoFrame),
    };
  }

  reset(): void {
    this.audioAppendSequence = 0;
    this.imageAppendSequence = 0;
    this.deferredVideoFrame = null;
  }
}
