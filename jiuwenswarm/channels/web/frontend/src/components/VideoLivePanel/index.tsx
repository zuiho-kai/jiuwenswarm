import { ChangeEvent, FormEvent, useCallback, useEffect, useRef, useState } from 'react';
import { Camera, FileVideo, LoaderCircle, Mic, Monitor, Play, Radar, Send, Square, Video, Volume2, VolumeX, X } from 'lucide-react';
import { webClient, webRequest } from '../../services/webClient';
import { useChatStore } from '../../stores/chatStore';
import { fetchTtsAudio, sanitizeTtsText } from '../../utils';
import { MarkdownRenderer } from '../MarkdownRenderer';
import { selectMonitorFrames } from './monitorFrameQueue';
import './VideoLivePanel.css';

type VideoSource = 'camera' | 'file' | 'screen' | null;
type AbortReason = 'manual' | 'timeout' | 'source' | 'barge-in';

interface CapturedFrame {
  client_frame_id: string;
  frame_seq: number;
  data_url: string;
  captured_at: number;
  source_id: string;
  source_label: string;
  width: number;
  height: number;
}

interface ScreenSource {
  id: string;
  name: string;
  stream: MediaStream;
}

interface AudioInput {
  data_url: string;
  source_label: string;
}

interface SourceAudioCapture {
  id: string;
  label: string;
  stream: MediaStream;
  recorder: MediaRecorder | null;
  chunks: Blob[];
  latestBlob: Blob | null;
  timerId: number | null;
  disposed: boolean;
  waiters: Array<() => void>;
}

interface VideoAskResponse {
  answer?: string;
  transcript?: string;
  ignored?: boolean;
  outcome?: 'success' | 'voice_rejected' | 'empty_model_answer';
  latency_ms?: number;
  first_token_ms?: number;
  model?: string;
  native_audio_emitted?: boolean;
}

type MonitorDecision = 'hold' | 'emit' | 'uncertain';
type MonitorPhase = 'idle' | 'watching' | 'evaluating' | 'uncertain' | 'backoff' | 'error';

interface MonitorEvent {
  event_key: string;
  response: string;
  emitted_at: number;
  occurrence_id?: string;
}

interface MonitorEvaluateResponse {
  decision: MonitorDecision;
  trace_id?: string;
  monitor_run_id?: string;
  event_key?: string;
  occurrence_id?: string;
  display_action?: 'append' | 'replace';
  response?: string;
  latency_ms?: number;
  model?: string;
}

interface VideoRealtimeStartResponse {
  enabled?: boolean;
  connected?: boolean;
  model?: string;
  protocol?: string;
}

interface MonitorIntentResponse {
  action?: 'chat' | 'start_monitor';
  instruction?: string;
  confidence?: number;
  model?: string;
}

interface VideoMonitorStartResult {
  outcome: 'started' | 'needs_source' | 'already_active' | 'failed';
  state: 'idle' | 'awaiting_source' | 'active';
  instruction: string;
  sessionId: string;
  runId: string;
  error?: string;
}

interface ConversationTurn {
  id: string;
  question: string;
  answer: string;
}

const FRAME_INTERVAL_MS = 500;
const MEMORY_INTERVAL_MS = 1_000;
const MAX_FRAMES = 6;
const MAX_SCREENS = 4;
const MAX_FRAME_WIDTH = 1_024;
const REQUEST_TIMEOUT_MS = 45_000;
const ASR_REQUEST_TIMEOUT_MS = 50_000;
const RECORDING_LIMIT_MS = 15_000;
const SOURCE_AUDIO_SEGMENT_MS = 3_000;
const MONITOR_INTERVAL_MS = 1_000;
const MONITOR_BUSY_BACKOFF_MS = [3_000, 6_000, 12_000, 24_000, 30_000] as const;
const MONITOR_BUSY_FRAME_WINDOW_MS = 8_000;
const MONITOR_FRAME_COUNT = 8;
const TRANSLATION_MONITOR_FRAME_COUNT = 4;
const MONITOR_HISTORY_MS = 4_000;
const TRANSLATION_MONITOR_HISTORY_MS = 3_000;
const MONITOR_BUFFER_FRAME_COUNT = 48;
const MONITOR_EVENT_HISTORY_LIMIT = 100;
const AUDIO_MIME_TYPES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus'];

function prefersLatestMonitorFrames(instruction: string): boolean {
  return /(?:翻译|译为|译成|中译|英译|translate|translation|subtitle)/i.test(instruction);
}

function lastMatchingIndex<T>(items: readonly T[], predicate: (item: T) => boolean): number {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    if (predicate(items[index])) return index;
  }
  return -1;
}

function isMonitorProviderBusyError(error: unknown): boolean {
  const message = error instanceof Error ? `${error.name} ${error.message}` : String(error);
  return /(?:\b429\b|\b502\b|\b503\b|\b504\b|\b50508\b|origin_bad_gateway|retryable['"]?\s*:\s*true|system is too busy|too many requests|服务(?:器)?繁忙|请求过于频繁)/i.test(
    message
  );
}

function monitorProviderRetryDelayMs(error: unknown, attemptIndex: number): number {
  const fallback = MONITOR_BUSY_BACKOFF_MS[Math.min(attemptIndex, MONITOR_BUSY_BACKOFF_MS.length - 1)];
  const message = error instanceof Error ? error.message : String(error);
  const match = message.match(/\bretry_after['"]?\s*:\s*(\d+(?:\.\d+)?)/i);
  const retryAfterSeconds = match ? Number(match[1]) : 0;
  if (!Number.isFinite(retryAfterSeconds) || retryAfterSeconds <= 0) return fallback;
  return Math.max(fallback, Math.min(retryAfterSeconds * 1_000, 300_000));
}

function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(reader.error || new Error('读取录音失败'));
    reader.readAsDataURL(blob);
  });
}

async function audioBlobToWavDataUrl(blob: Blob): Promise<string> {
  const context = new AudioContext();
  try {
    const decoded = await context.decodeAudioData(await blob.arrayBuffer());
    const sampleRate = 16_000;
    const frameCount = Math.max(1, Math.ceil(decoded.duration * sampleRate));
    const offline = new OfflineAudioContext(1, frameCount, sampleRate);
    const source = offline.createBufferSource();
    source.buffer = decoded;
    source.connect(offline.destination);
    source.start();
    const rendered = await offline.startRendering();
    const samples = rendered.getChannelData(0);
    const wav = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(wav);
    const writeText = (offset: number, value: string) => {
      for (let index = 0; index < value.length; index += 1) {
        view.setUint8(offset + index, value.charCodeAt(index));
      }
    };
    writeText(0, 'RIFF');
    view.setUint32(4, 36 + samples.length * 2, true);
    writeText(8, 'WAVE');
    writeText(12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    writeText(36, 'data');
    view.setUint32(40, samples.length * 2, true);
    for (let index = 0; index < samples.length; index += 1) {
      const sample = Math.max(-1, Math.min(1, samples[index]));
      view.setInt16(44 + index * 2, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
    }
    return blobToDataUrl(new Blob([wav], { type: 'audio/wav' }));
  } finally {
    await context.close();
  }
}

export function VideoLivePanel() {
  const activeSessionId = useChatStore(state => state.activeSessionId);
  const videoRef = useRef<HTMLVideoElement>(null);
  const captureCanvasesRef = useRef<Map<string, HTMLCanvasElement>>(new Map());
  const capturePendingRef = useRef<Map<string, number>>(new Map());
  const captureTokenRef = useRef(0);
  const cameraStreamRef = useRef<MediaStream | null>(null);
  const screenStreamsRef = useRef<Map<string, MediaStream>>(new Map());
  const screenVideoRefs = useRef<Map<string, HTMLVideoElement>>(new Map());
  const fileUrlRef = useRef<string | null>(null);
  const fileCaptureStreamRef = useRef<MediaStream | null>(null);
  const sourceAudioCapturesRef = useRef<Map<string, SourceAudioCapture>>(new Map());
  const framesRef = useRef<CapturedFrame[]>([]);
  const memoryFramesRef = useRef<Map<string, CapturedFrame>>(new Map());
  const memoryRequestRef = useRef<Promise<void> | null>(null);
  const frameSequencesRef = useRef<Map<string, number>>(new Map());
  const lastMemoryFlushAtRef = useRef(0);
  const requestAbortRef = useRef<AbortController | null>(null);
  const requestStartedAtRef = useRef(0);
  const requestAbortReasonsRef = useRef<WeakMap<AbortController, AbortReason>>(new WeakMap());
  const activeRequestIdRef = useRef<string | null>(null);
  const streamFirstTokenMsRef = useRef<number | null>(null);
  const activeSessionIdRef = useRef<string | null>(activeSessionId);
  const isAskingRef = useRef(false);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const microphoneStreamRef = useRef<MediaStream | null>(null);
  const audioChunksRef = useRef<Blob[]>([]);
  const recordingTimeoutRef = useRef<number | null>(null);
  const voiceVadTimerRef = useRef<number | null>(null);
  const voiceAudioContextRef = useRef<AudioContext | null>(null);
  const voiceConversationRef = useRef(false);
  const startVoiceListeningRef = useRef<() => Promise<void>>(async () => undefined);
  const activeTurnIdRef = useRef<string | null>(null);
  const assistantAudioRef = useRef<HTMLAudioElement | null>(null);
  const ttsQueueRef = useRef<Promise<void>>(Promise.resolve());
  const ttsGenerationRef = useRef(0);
  const ttsBufferRef = useRef('');
  const streamedAnswerRef = useRef('');
  const nativeAudioExpectedRef = useRef(false);
  const assistantSpeechTextRef = useRef('');
  const ttsAbortControllersRef = useRef<Set<AbortController>>(new Set());
  const monitorInFlightRef = useRef(false);
  const monitorAbortRef = useRef<AbortController | null>(null);
  const monitorEventsRef = useRef<MonitorEvent[]>([]);
  const monitorResponseTextRef = useRef<HTMLSpanElement>(null);
  const monitorInstructionRef = useRef('');
  const monitorOwnerSessionIdRef = useRef('');
  const isMonitoringRef = useRef(false);
  const monitorRunIdRef = useRef('');
  const monitorSequenceRef = useRef(0);
  const monitorSkippedIntervalsRef = useRef(0);
  const monitorFramesRef = useRef<CapturedFrame[]>([]);
  const monitorBufferDroppedRef = useRef(0);
  const monitorLastRequestStartedAtRef = useRef(0);
  const monitorBusyAttemptRef = useRef(0);
  const monitorRetryAtRef = useRef(0);
  const evaluateMonitorRef = useRef<() => Promise<void>>(async () => undefined);
  const pendingMonitorStartRef = useRef<{ instruction: string; sessionId: string } | null>(null);
  const startMonitorRequestRef = useRef<
    (instruction: string, sessionId: string) => Promise<VideoMonitorStartResult>
  >(async (instruction, sessionId) => ({
    outcome: 'failed',
    state: 'idle',
    instruction,
    sessionId,
    runId: '',
    error: '视频监控尚未初始化。',
  }));

  const [source, setSource] = useState<VideoSource>(null);
  const [sourceName, setSourceName] = useState('');
  const [screens, setScreens] = useState<ScreenSource[]>([]);
  const [isPlaying, setIsPlaying] = useState(false);
  const [frameCount, setFrameCount] = useState(0);
  const [question, setQuestion] = useState('');
  const [answer, setAnswer] = useState('');
  const [error, setError] = useState('');
  const [isAsking, setIsAsking] = useState(false);
  const [latencyMs, setLatencyMs] = useState<number | null>(null);
  const [firstTokenMs, setFirstTokenMs] = useState<number | null>(null);
  const [model, setModel] = useState('等待首次请求');
  const [requestCount, setRequestCount] = useState(0);
  const [elapsedMs, setElapsedMs] = useState(0);
  const [isRecording, setIsRecording] = useState(false);
  const [audioSourceCount, setAudioSourceCount] = useState(0);
  const [isTranslating, setIsTranslating] = useState(false);
  const [liveSubtitle, setLiveSubtitle] = useState('');
  const [translationCount, setTranslationCount] = useState(0);
  const [targetLanguage, setTargetLanguage] = useState('中文');
  const [toolStatus, setToolStatus] = useState('');
  const [isVoiceConversation, setIsVoiceConversation] = useState(false);
  const [conversationTurns, setConversationTurns] = useState<ConversationTurn[]>([]);
  const [isSpeechEnabled, setIsSpeechEnabled] = useState(true);
  const [isSpeaking, setIsSpeaking] = useState(false);
  const [monitorInstruction, setMonitorInstruction] = useState('画面中出现新的英文内容时，立即翻译成中文。');
  const [isMonitoring, setIsMonitoring] = useState(false);
  const [monitorPhase, setMonitorPhase] = useState<MonitorPhase>('idle');
  const [monitorResponse, setMonitorResponse] = useState('');
  const [monitorEvaluationCount, setMonitorEvaluationCount] = useState(0);
  const [monitorResponseCount, setMonitorResponseCount] = useState(0);
  const [monitorRetrySeconds, setMonitorRetrySeconds] = useState(0);

  useEffect(() => {
    if (monitorResponseTextRef.current) monitorResponseTextRef.current.scrollTop = 0;
  }, [monitorResponse]);

  const beginConversationTurn = useCallback((prompt: string) => {
    const id = `turn-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    activeTurnIdRef.current = id;
    setAnswer('');
    setConversationTurns(turns => [...turns, { id, question: prompt || '🎙️ 语音提问', answer: '' }].slice(-30));
    return id;
  }, []);

  const appendTurnAnswer = useCallback((id: string, delta: string) => {
    if (!delta) return;
    setAnswer(current => current + delta);
    setConversationTurns(turns => turns.map(turn => (turn.id === id ? { ...turn, answer: turn.answer + delta } : turn)));
  }, []);

  const replaceTurnAnswer = useCallback((id: string, value: string) => {
    setAnswer(value);
    setConversationTurns(turns => turns.map(turn => (turn.id === id ? { ...turn, answer: value } : turn)));
  }, []);

  const replaceTurnQuestion = useCallback((id: string, value: string) => {
    if (!value.trim()) return;
    setConversationTurns(turns => turns.map(turn => (turn.id === id ? { ...turn, question: value.trim() } : turn)));
  }, []);

  const stopSpeechPlayback = useCallback(() => {
    ttsGenerationRef.current += 1;
    ttsBufferRef.current = '';
    ttsAbortControllersRef.current.forEach((controller) => controller.abort());
    ttsAbortControllersRef.current.clear();
    const audio = assistantAudioRef.current;
    if (audio) {
      audio.pause();
      audio.currentTime = 0;
      assistantAudioRef.current = null;
    }
    ttsQueueRef.current = Promise.resolve();
    setIsSpeaking(false);
  }, []);

  const pauseSpeechPlayback = useCallback(() => {
    const audio = assistantAudioRef.current;
    if (audio && !audio.paused) {
      audio.pause();
      setIsSpeaking(false);
    }
  }, []);

  const resumeSpeechPlayback = useCallback(() => {
    const audio = assistantAudioRef.current;
    if (!audio || !audio.paused || audio.ended) return;
    setIsSpeaking(true);
    void audio.play().then(() => {
      if (voiceConversationRef.current) {
        void startVoiceListeningRef.current();
      }
    }).catch(() => stopSpeechPlayback());
  }, [stopSpeechPlayback]);

  const playAssistantAudio = useCallback((audioBase64: string, mime: string) => (
    new Promise<void>((resolve) => {
      const audio = new Audio(`data:${mime};base64,${audioBase64}`);
      assistantAudioRef.current = audio;
      const finish = () => {
        if (assistantAudioRef.current === audio) assistantAudioRef.current = null;
        setIsSpeaking(false);
        resolve();
      };
      audio.onended = finish;
      audio.onerror = finish;
      setIsSpeaking(true);
      void audio.play().then(() => {
        if (voiceConversationRef.current) {
          void startVoiceListeningRef.current();
        }
      }).catch(finish);
    })
  ), []);

  const enqueueSpeechSegment = useCallback((rawText: string) => {
    if (!isSpeechEnabled) return;
    const text = sanitizeTtsText(rawText, 300);
    if (!text) return;
    const generation = ttsGenerationRef.current;
    const controller = new AbortController();
    ttsAbortControllersRef.current.add(controller);
    // Start synthesis immediately; playback remains ordered by ttsQueueRef.
    const audioRequest = fetchTtsAudio(
      text,
      activeSessionIdRef.current || undefined,
      controller.signal,
    ).finally(() => ttsAbortControllersRef.current.delete(controller));
    ttsQueueRef.current = ttsQueueRef.current.then(async () => {
      const response = await audioRequest;
      if (
        generation !== ttsGenerationRef.current
        || !response?.success
        || !response.audio_base64
      ) return;
      await playAssistantAudio(
        response.audio_base64,
        response.audio_mime || 'audio/mpeg',
      );
    }).catch(() => undefined);
  }, [isSpeechEnabled, playAssistantAudio]);

  const queueSpeechText = useCallback((delta: string, flush = false) => {
    if (!isSpeechEnabled) return;
    ttsBufferRef.current += delta;
    let boundary = -1;
    for (let index = 0; index < ttsBufferRef.current.length; index += 1) {
      if ('。！？!?；;\n'.includes(ttsBufferRef.current[index])) boundary = index;
      if (boundary >= 0 && (boundary >= 24 || index >= 150)) break;
    }
    while (boundary >= 0) {
      const segment = ttsBufferRef.current.slice(0, boundary + 1);
      ttsBufferRef.current = ttsBufferRef.current.slice(boundary + 1);
      enqueueSpeechSegment(segment);
      boundary = -1;
      for (let index = 0; index < ttsBufferRef.current.length; index += 1) {
        if ('。！？!?；;\n'.includes(ttsBufferRef.current[index])) boundary = index;
        if (boundary >= 0 && (boundary >= 24 || index >= 150)) break;
      }
    }
    if (ttsBufferRef.current.length >= 180) {
      enqueueSpeechSegment(ttsBufferRef.current.slice(0, 180));
      ttsBufferRef.current = ttsBufferRef.current.slice(180);
    }
    if (flush && ttsBufferRef.current.trim()) {
      enqueueSpeechSegment(ttsBufferRef.current);
      ttsBufferRef.current = '';
    }
  }, [enqueueSpeechSegment, isSpeechEnabled]);

  useEffect(() => {
    activeSessionIdRef.current = activeSessionId;
  }, [activeSessionId]);

  useEffect(() => {
    let disposed = false;
    let retryTimer: number | null = null;
    const controller = new AbortController();

    const connectRealtime = async (attempt: number) => {
      try {
        const result = await webRequest<VideoRealtimeStartResponse>(
          'video.realtime.start',
          {},
          { timeoutMs: 10_000, signal: controller.signal },
        );
        if (!disposed && result.model) setModel(result.model);
      } catch (realtimeError) {
        if (disposed || controller.signal.aborted) return;
        if (attempt < 2) {
          retryTimer = window.setTimeout(
            () => void connectRealtime(attempt + 1),
            750 * (attempt + 1),
          );
          return;
        }
        console.warn('[VideoRealtime] preconnect failed; the first request will retry.', realtimeError);
      }
    };

    void connectRealtime(0);
    return () => {
      disposed = true;
      controller.abort();
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      void webRequest('video.realtime.stop', {}, { timeoutMs: 5_000 }).catch(() => undefined);
    };
  }, []);

  const stopSourceAudio = useCallback((sourceId: string) => {
    const capture = sourceAudioCapturesRef.current.get(sourceId);
    if (!capture) return;
    capture.disposed = true;
    if (capture.timerId !== null) window.clearTimeout(capture.timerId);
    capture.timerId = null;
    capture.recorder && (capture.recorder.onstop = null);
    if (capture.recorder?.state === 'recording') capture.recorder.stop();
    capture.waiters.splice(0).forEach(resolve => resolve());
    sourceAudioCapturesRef.current.delete(sourceId);
    setAudioSourceCount(sourceAudioCapturesRef.current.size);
  }, []);

  const attachSourceAudio = useCallback(
    (sourceId: string, label: string, stream: MediaStream) => {
      stopSourceAudio(sourceId);
      const liveAudioTracks = stream.getAudioTracks().filter(track => track.readyState === 'live');
      if (liveAudioTracks.length === 0 || typeof MediaRecorder === 'undefined') return false;

      const capture: SourceAudioCapture = {
        id: sourceId,
        label,
        stream,
        recorder: null,
        chunks: [],
        latestBlob: null,
        timerId: null,
        disposed: false,
        waiters: [],
      };
      const mimeType = AUDIO_MIME_TYPES.find(type => MediaRecorder.isTypeSupported(type));

      const startSegment = () => {
        if (capture.disposed) return;
        const tracks = capture.stream.getAudioTracks().filter(track => track.readyState === 'live');
        if (tracks.length === 0) return;
        capture.chunks = [];
        const recorder = new MediaRecorder(new MediaStream(tracks), mimeType ? { mimeType } : undefined);
        capture.recorder = recorder;
        recorder.ondataavailable = event => {
          if (event.data.size > 0) capture.chunks.push(event.data);
        };
        recorder.onstop = () => {
          if (capture.timerId !== null) window.clearTimeout(capture.timerId);
          capture.timerId = null;
          const blob = new Blob(capture.chunks, { type: recorder.mimeType || 'audio/webm' });
          if (blob.size > 0) capture.latestBlob = blob;
          capture.chunks = [];
          capture.recorder = null;
          capture.waiters.splice(0).forEach(resolve => resolve());
          if (!capture.disposed) startSegment();
        };
        recorder.start(250);
        capture.timerId = window.setTimeout(() => {
          if (recorder.state === 'recording') recorder.stop();
        }, SOURCE_AUDIO_SEGMENT_MS);
      };

      try {
        sourceAudioCapturesRef.current.set(sourceId, capture);
        startSegment();
        setAudioSourceCount(sourceAudioCapturesRef.current.size);
        return true;
      } catch {
        capture.disposed = true;
        sourceAudioCapturesRef.current.delete(sourceId);
        setAudioSourceCount(sourceAudioCapturesRef.current.size);
        return false;
      }
    },
    [stopSourceAudio]
  );

  const snapshotSourceAudio = useCallback(async (): Promise<AudioInput[]> => {
    const captures = Array.from(sourceAudioCapturesRef.current.values()).filter(capture => !capture.disposed);
    await Promise.all(
      captures.map(
        capture =>
          new Promise<void>(resolve => {
            const recorder = capture.recorder;
            if (!recorder || recorder.state !== 'recording') {
              resolve();
              return;
            }
            capture.waiters.push(resolve);
            if (capture.timerId !== null) window.clearTimeout(capture.timerId);
            capture.timerId = null;
            recorder.stop();
          })
      )
    );
    const inputs = await Promise.all(
      captures.map(async capture => {
        if (!capture.latestBlob) return null;
        return {
          data_url: await audioBlobToWavDataUrl(capture.latestBlob),
          source_label: capture.label,
        };
      })
    );
    return inputs.filter((input): input is AudioInput => input !== null);
  }, []);

  const stopMonitoring = useCallback((clearResponse = false) => {
    const monitorRunId = monitorRunIdRef.current;
    const ownerSessionId = monitorOwnerSessionIdRef.current;
    isMonitoringRef.current = false;
    monitorRunIdRef.current = '';
    monitorInstructionRef.current = '';
    monitorOwnerSessionIdRef.current = '';
    monitorAbortRef.current?.abort();
    monitorAbortRef.current = null;
    monitorInFlightRef.current = false;
    monitorSkippedIntervalsRef.current = 0;
    monitorFramesRef.current = [];
    monitorBufferDroppedRef.current = 0;
    monitorLastRequestStartedAtRef.current = 0;
    monitorBusyAttemptRef.current = 0;
    monitorRetryAtRef.current = 0;
    setMonitorRetrySeconds(0);
    setIsMonitoring(false);
    setMonitorPhase('idle');
    if (clearResponse) setMonitorResponse('');
    if (monitorRunId) {
      void webRequest(
        'video.monitor.cancel',
        {
          ...(ownerSessionId ? { session_id: ownerSessionId } : {}),
          monitor_run_id: monitorRunId,
        },
        { timeoutMs: 5_000 }
      ).catch(() => undefined);
    }
  }, []);

  const releaseSource = useCallback(() => {
    stopSourceAudio('camera');
    stopSourceAudio('file');
    cameraStreamRef.current?.getTracks().forEach(track => track.stop());
    cameraStreamRef.current = null;
    fileCaptureStreamRef.current?.getTracks().forEach(track => track.stop());
    fileCaptureStreamRef.current = null;
    if (fileUrlRef.current) {
      URL.revokeObjectURL(fileUrlRef.current);
      fileUrlRef.current = null;
    }
    const video = videoRef.current;
    if (video) {
      video.pause();
      video.srcObject = null;
      video.removeAttribute('src');
      video.load();
    }
    framesRef.current = [];
    memoryFramesRef.current.clear();
    ['camera', 'file', 'video'].forEach(sourceId => {
      captureCanvasesRef.current.delete(sourceId);
      capturePendingRef.current.delete(sourceId);
    });
    lastMemoryFlushAtRef.current = 0;
    setFrameCount(0);
    setIsPlaying(false);
  }, [stopSourceAudio]);

  const releaseScreens = useCallback(
    (updateState = true) => {
      screenStreamsRef.current.forEach((stream, screenId) => {
        stopSourceAudio(screenId);
        stream.getTracks().forEach(track => track.stop());
        captureCanvasesRef.current.delete(screenId);
        capturePendingRef.current.delete(screenId);
      });
      screenStreamsRef.current.clear();
      screenVideoRefs.current.clear();
      if (updateState) setScreens([]);
    },
    [stopSourceAudio]
  );

  const abortQuestion = useCallback((reason: AbortReason) => {
    const controller = requestAbortRef.current;
    if (controller) {
      requestAbortReasonsRef.current.set(controller, reason);
      controller.abort();
    }
    requestAbortRef.current = null;
    activeRequestIdRef.current = null;
    isAskingRef.current = false;
    setIsAsking(false);
    setToolStatus('');
    stopSpeechPlayback();
  }, [stopSpeechPlayback]);

  const releaseMicrophone = useCallback(() => {
    if (recordingTimeoutRef.current !== null) {
      window.clearTimeout(recordingTimeoutRef.current);
      recordingTimeoutRef.current = null;
    }
    if (voiceVadTimerRef.current !== null) {
      window.clearInterval(voiceVadTimerRef.current);
      voiceVadTimerRef.current = null;
    }
    const audioContext = voiceAudioContextRef.current;
    voiceAudioContextRef.current = null;
    if (audioContext) void audioContext.close().catch(() => undefined);
    microphoneStreamRef.current?.getTracks().forEach(track => track.stop());
    microphoneStreamRef.current = null;
  }, []);

  const cancelRecording = useCallback(
    (updateState = true) => {
      const recorder = mediaRecorderRef.current;
      if (recorder) {
        recorder.onstop = null;
        if (recorder.state !== 'inactive') recorder.stop();
      }
      mediaRecorderRef.current = null;
      audioChunksRef.current = [];
      releaseMicrophone();
      if (updateState) setIsRecording(false);
    },
    [releaseMicrophone]
  );

  const closeSource = () => {
    stopMonitoring(true);
    voiceConversationRef.current = false;
    setIsVoiceConversation(false);
    if (isAskingRef.current) abortQuestion('source');
    cancelRecording();
    stopSpeechPlayback();
    releaseScreens();
    releaseSource();
    setSource(null);
    setSourceName('');
    setAnswer('');
    setError('');
    setIsTranslating(false);
    setLiveSubtitle('');
    void webRequest('video.task.stop', {
      ...(activeSessionId ? { session_id: activeSessionId } : {}),
    }).catch(() => undefined);
  };

  useEffect(() => () => {
    isMonitoringRef.current = false;
    monitorAbortRef.current?.abort();
    requestAbortRef.current?.abort();
    const sessionId = activeSessionIdRef.current;
    void webRequest('video.task.stop', {
      ...(sessionId ? { session_id: sessionId } : {}),
    }).catch(() => undefined);
    cancelRecording(false);
    stopSpeechPlayback();
    releaseScreens(false);
    releaseSource();
  }, [cancelRecording, releaseScreens, releaseSource, stopSpeechPlayback]);

  useEffect(() => {
    const offStarted = webClient.on<{ request_id?: unknown; model?: unknown }>('video.started', (event) => {
      const requestId = event.payload.request_id;
      if (typeof requestId === 'string' && requestId === activeRequestIdRef.current) {
        if (typeof event.payload.model === 'string' && event.payload.model) {
          setModel(event.payload.model);
        }
        setToolStatus('');
      }
    });
    const offDelta = webClient.on<{ request_id?: unknown; content?: unknown }>('video.delta', event => {
      if (event.payload.request_id !== activeRequestIdRef.current) return;
      const content = event.payload.content;
      if (typeof content === 'string' && content) {
        if (streamFirstTokenMsRef.current === null) {
          streamFirstTokenMsRef.current = Math.round(performance.now() - requestStartedAtRef.current);
          setFirstTokenMs(streamFirstTokenMsRef.current);
        }
        streamedAnswerRef.current += content;
        if (!nativeAudioExpectedRef.current) queueSpeechText(content);
        const turnId = activeTurnIdRef.current;
        if (turnId) appendTurnAnswer(turnId, content);
      }
    });
    const offAudio = webClient.on<{
      request_id?: unknown;
      audio_base64?: unknown;
      audio_mime?: unknown;
    }>('video.audio', (event) => {
      if (event.payload.request_id !== activeRequestIdRef.current) return;
      const audioBase64 = event.payload.audio_base64;
      if (typeof audioBase64 !== 'string' || !audioBase64) return;
      void playAssistantAudio(
        audioBase64,
        typeof event.payload.audio_mime === 'string'
          ? event.payload.audio_mime
          : 'audio/wav',
      );
    });
    const offTranscript = webClient.on<{
      request_id?: unknown;
      text?: unknown;
      accepted?: unknown;
    }>('video.transcript', event => {
      if (event.payload.request_id !== activeRequestIdRef.current) return;
      if (event.payload.accepted !== true) return;
      stopSpeechPlayback();
      if (typeof event.payload.text === 'string') {
        const turnId = activeTurnIdRef.current;
        if (turnId) replaceTurnQuestion(turnId, event.payload.text);
      }
    });
    const offVideoToolStatus = webClient.on<{
      request_id?: unknown;
      status?: unknown;
    }>('video.tool_status', event => {
      if (event.payload.request_id !== activeRequestIdRef.current) return;
      if (typeof event.payload.status === 'string') {
        setToolStatus(event.payload.status);
      }
    });
    const offTaskResponse = webClient.on<{
      text?: unknown;
      source_id?: unknown;
      frame_seq?: unknown;
    }>('video.task.response', event => {
      const text = event.payload.text;
      if (typeof text !== 'string' || !text.trim()) return;
      setLiveSubtitle(text.trim());
      setAnswer(text.trim());
      setTranslationCount(count => count + 1);
    });
    const offTaskError = webClient.on<{ error?: unknown }>('video.task.error', event => {
      const taskError = event.payload.error;
      if (typeof taskError === 'string' && taskError) setError(taskError);
    });
    const offMemoryError = webClient.on<{ error?: unknown }>('video.memory.error', event => {
      const memoryError = event.payload.error;
      if (typeof memoryError === 'string' && memoryError) setError(memoryError);
    });
    return () => {
      offStarted();
      offDelta();
      offAudio();
      offTranscript();
      offVideoToolStatus();
      offTaskResponse();
      offTaskError();
      offMemoryError();
    };
  }, [appendTurnAnswer, playAssistantAudio, queueSpeechText, replaceTurnQuestion, stopSpeechPlayback]);

  useEffect(() => {
    if (!isAsking) {
      setElapsedMs(0);
      return;
    }
    const updateElapsed = () => {
      setElapsedMs(Math.round(performance.now() - requestStartedAtRef.current));
    };
    updateElapsed();
    const timer = window.setInterval(updateElapsed, 100);
    return () => window.clearInterval(timer);
  }, [isAsking]);

  const flushMemoryFrames = useCallback(async () => {
    if (!activeSessionId) return;
    if (memoryRequestRef.current) await memoryRequestRef.current;
    if (memoryFramesRef.current.size === 0) return;
    const drain = (async () => {
      while (memoryFramesRef.current.size > 0) {
        const frames = Array.from(memoryFramesRef.current.values());
        memoryFramesRef.current.clear();
        await webRequest('video.observe', { session_id: activeSessionId, frames }, { timeoutMs: 30_000 });
      }
    })();
    memoryRequestRef.current = drain;
    try {
      await drain;
    } finally {
      if (memoryRequestRef.current === drain) memoryRequestRef.current = null;
    }
  }, [activeSessionId]);

  useEffect(() => {
    if (!isPlaying) return;

    const captureVideo = (video: HTMLVideoElement, sourceId: string, sourceLabel: string) => {
      if (capturePendingRef.current.has(sourceId)) return;
      if (video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return;
      if (!video.videoWidth || !video.videoHeight) return;

      let canvas = captureCanvasesRef.current.get(sourceId);
      if (!canvas) {
        canvas = document.createElement('canvas');
        captureCanvasesRef.current.set(sourceId, canvas);
      }
      const scale = Math.min(1, MAX_FRAME_WIDTH / video.videoWidth);
      canvas.width = Math.round(video.videoWidth * scale);
      canvas.height = Math.round(video.videoHeight * scale);
      const context = canvas.getContext('2d');
      if (!context) return;

      context.drawImage(video, 0, 0, canvas.width, canvas.height);
      const nextFrameSeq = frameSequencesRef.current.get(sourceId) ?? 0;
      frameSequencesRef.current.set(sourceId, nextFrameSeq + 1);
      const capturedAt = Date.now();
      const capturedWidth = canvas.width;
      const capturedHeight = canvas.height;
      const capturedStream = video.srcObject;
      const capturedCurrentSrc = video.currentSrc;
      const captureToken = captureTokenRef.current + 1;
      captureTokenRef.current = captureToken;
      capturePendingRef.current.set(sourceId, captureToken);
      canvas.toBlob(
        blob => {
          if (capturePendingRef.current.get(sourceId) === captureToken) {
            capturePendingRef.current.delete(sourceId);
          }
          if (!blob) return;
          const isCurrentVideo =
            (screenVideoRefs.current.get(sourceId) === video || videoRef.current === video) &&
            video.srcObject === capturedStream &&
            video.currentSrc === capturedCurrentSrc;
          if (!isCurrentVideo) return;
          void blobToDataUrl(blob)
            .then(dataUrl => {
              const stillCurrent =
                (screenVideoRefs.current.get(sourceId) === video || videoRef.current === video) &&
                video.srcObject === capturedStream &&
                video.currentSrc === capturedCurrentSrc;
              if (!stillCurrent) return;
              const frame: CapturedFrame = {
                client_frame_id: typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : `${sourceId}-${capturedAt}-${nextFrameSeq}`,
                frame_seq: nextFrameSeq,
                data_url: dataUrl,
                captured_at: capturedAt,
                source_id: sourceId,
                source_label: sourceLabel,
                width: capturedWidth,
                height: capturedHeight,
              };
              framesRef.current.push(frame);
              framesRef.current.sort((left, right) => left.captured_at - right.captured_at);
              if (framesRef.current.length > MAX_FRAMES) {
                framesRef.current.splice(0, framesRef.current.length - MAX_FRAMES);
              }
              memoryFramesRef.current.set(sourceId, frame);
              if (isMonitoringRef.current) {
                monitorFramesRef.current.push(frame);
                const overflow = monitorFramesRef.current.length - MONITOR_BUFFER_FRAME_COUNT;
                if (overflow > 0) {
                  monitorFramesRef.current.splice(0, overflow);
                  monitorBufferDroppedRef.current += overflow;
                }
              }
              setFrameCount(framesRef.current.length);
              const now = Date.now();
              if (isMonitoringRef.current && !monitorInFlightRef.current && monitorRetryAtRef.current <= now) {
                queueMicrotask(() => void evaluateMonitorRef.current());
              }
            })
            .catch(() => undefined);
        },
        'image/jpeg',
        0.8
      );
    };

    const capture = () => {
      if (source === 'screen') {
        screens.forEach(screen => {
          const video = screenVideoRefs.current.get(screen.id);
          if (video) captureVideo(video, screen.id, screen.name);
        });
      } else {
        const video = videoRef.current;
        if (video) {
          captureVideo(video, source || 'video', source === 'camera' ? '摄像头' : sourceName || '本地视频');
        }
      }
      const now = Date.now();
      if (now - lastMemoryFlushAtRef.current >= MEMORY_INTERVAL_MS) {
        lastMemoryFlushAtRef.current = now;
        void flushMemoryFrames().catch(() => undefined);
      }
    };

    capture();
    const timer = window.setInterval(capture, FRAME_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [flushMemoryFrames, isPlaying, screens, source, sourceName]);

  const startCamera = async () => {
    stopMonitoring(true);
    if (isTranslating) {
      await webRequest('video.task.stop', {
        ...(activeSessionId ? { session_id: activeSessionId } : {}),
      }).catch(() => undefined);
      setIsTranslating(false);
    }
    cancelRecording();
    releaseScreens();
    releaseSource();
    setSource(null);
    setSourceName('');
    setError('');
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 1280 }, height: { ideal: 720 } },
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
        },
      });
      cameraStreamRef.current = stream;
      const hasAudio = attachSourceAudio('camera', '摄像头麦克风', stream);
      setSource('camera');
      setSourceName('Camera');
      const video = videoRef.current;
      if (!video) {
        releaseSource();
        setSource(null);
        setSourceName('');
        return;
      }
      video.srcObject = stream;
      video.muted = true;
      try {
        await video.play();
        setIsPlaying(true);
        if (!hasAudio) setError('摄像头画面已打开，但没有可用的麦克风音轨。');
      } catch (playError) {
        setError(playError instanceof Error ? `摄像头已连接，但画面播放失败：${playError.message}` : '摄像头已连接，但画面播放失败。');
      }
    } catch (cameraError) {
      releaseSource();
      setSource(null);
      setSourceName('');
      setError(cameraError instanceof Error ? cameraError.message : '无法打开摄像头');
    }
  };

  const toggleTranslation = async () => {
    if (isTranslating) {
      await webRequest('video.task.stop', {
        ...(activeSessionId ? { session_id: activeSessionId } : {}),
      });
      setIsTranslating(false);
      return;
    }
    stopMonitoring(false);
    const sourceId = source === 'screen' ? screens[0]?.id : source;
    if (!sourceId || (source === 'screen' && screens.length !== 1)) {
      setError('实时翻译 MVP 目前只支持单路视频源。');
      return;
    }
    setError('');
    setLiveSubtitle('');
    await webRequest('video.task.start', {
      ...(activeSessionId ? { session_id: activeSessionId } : {}),
      source_id: sourceId,
      target_language: targetLanguage,
    });
    setIsTranslating(true);
  };

  const openFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    stopMonitoring(true);

    if (isTranslating) {
      await webRequest('video.task.stop', {
        ...(activeSessionId ? { session_id: activeSessionId } : {}),
      }).catch(() => undefined);
      setIsTranslating(false);
    }

    cancelRecording();
    releaseScreens();
    releaseSource();
    setError('');
    const url = URL.createObjectURL(file);
    fileUrlRef.current = url;
    const video = videoRef.current;
    if (!video) return;
    video.srcObject = null;
    video.src = url;
    video.muted = false;
    await video.play().catch(() => undefined);
    const capturableVideo = video as HTMLVideoElement & {
      captureStream?: () => MediaStream;
      mozCaptureStream?: () => MediaStream;
    };
    const capturedStream = capturableVideo.captureStream?.() ?? capturableVideo.mozCaptureStream?.() ?? null;
    fileCaptureStreamRef.current = capturedStream;
    const hasAudio = capturedStream ? attachSourceAudio('file', `${file.name} 原音轨`, capturedStream) : false;
    setSource('file');
    setSourceName(file.name);
    setIsPlaying(!video.paused);
    if (!hasAudio) setError('视频已打开，但浏览器没有提供可读取的原音轨。');
  };

  const removeScreen = useCallback(
    (screenId: string) => {
      stopSourceAudio(screenId);
      const stream = screenStreamsRef.current.get(screenId);
      stream?.getTracks().forEach(track => track.stop());
      screenStreamsRef.current.delete(screenId);
      screenVideoRefs.current.delete(screenId);
      captureCanvasesRef.current.delete(screenId);
      capturePendingRef.current.delete(screenId);
      framesRef.current = framesRef.current.filter(frame => frame.source_id !== screenId);
      memoryFramesRef.current.delete(screenId);
      setFrameCount(framesRef.current.length);
      setScreens(current => {
        const next = current.filter(screen => screen.id !== screenId);
        if (next.length === 0) {
          setSource(null);
          setSourceName('');
          setIsPlaying(false);
        } else {
          setSourceName(`${next.length} 个屏幕`);
        }
        return next;
      });
    },
    [stopSourceAudio]
  );

  const startScreen = async () => {
    if (isAskingRef.current) return;
    if (screens.length >= MAX_SCREENS) {
      setError(`最多同时读取 ${MAX_SCREENS} 个屏幕。`);
      return;
    }
    if (!navigator.mediaDevices?.getDisplayMedia) {
      setError('当前运行环境不支持屏幕共享。');
      return;
    }

    if (source !== 'screen') stopMonitoring(true);

    if (isTranslating) {
      await webRequest('video.task.stop', {
        ...(activeSessionId ? { session_id: activeSessionId } : {}),
      }).catch(() => undefined);
      setIsTranslating(false);
    }

    cancelRecording();
    setError('');
    try {
      const stream = await navigator.mediaDevices.getDisplayMedia({
        video: { frameRate: { ideal: 30, max: 30 } },
        audio: true,
      });
      const track = stream.getVideoTracks()[0];
      if (!track) {
        stream.getTracks().forEach(item => item.stop());
        throw new Error('没有获得屏幕画面。');
      }

      if (source !== 'screen') {
        releaseSource();
        releaseScreens();
      }

      const screenId = typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : `screen-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      const name = track.label.trim() || `屏幕 ${screens.length + 1}`;
      screenStreamsRef.current.set(screenId, stream);
      setScreens(current => {
        setSourceName(`${current.length + 1} 个屏幕`);
        return [...current, { id: screenId, name, stream }];
      });
      const hasAudio = attachSourceAudio(screenId, `${name} 系统音频`, stream);
      setSource('screen');
      setIsPlaying(true);
      if (!hasAudio) setError('画面已共享，但没有系统音频；请在共享窗口中勾选“共享音频”。');
      track.addEventListener('ended', () => removeScreen(screenId), { once: true });
    } catch (screenError) {
      if (screenError instanceof DOMException && screenError.name === 'NotAllowedError') {
        setError('已取消选择屏幕。');
      } else {
        setError(screenError instanceof Error ? screenError.message : '无法读取屏幕。');
      }
    }
  };

  const evaluateMonitor = useCallback(async () => {
    if (!isMonitoringRef.current) return;
    const monitorRunId = monitorRunIdRef.current;
    if (!monitorRunId) return;
    const currentTime = Date.now();
    if (monitorRetryAtRef.current > currentTime) {
      const cutoff = currentTime - MONITOR_BUSY_FRAME_WINDOW_MS;
      const retainedFrames = monitorFramesRef.current.filter(frame => frame.captured_at >= cutoff);
      monitorBufferDroppedRef.current += monitorFramesRef.current.length - retainedFrames.length;
      monitorFramesRef.current = retainedFrames;
      setMonitorRetrySeconds(Math.max(1, Math.ceil((monitorRetryAtRef.current - currentTime) / 1_000)));
      return;
    }
    if (monitorRetryAtRef.current > 0) {
      monitorRetryAtRef.current = 0;
      setMonitorRetrySeconds(0);
    }
    if (monitorInFlightRef.current) {
      monitorSkippedIntervalsRef.current += 1;
      return;
    }
    const instruction = monitorInstructionRef.current.trim();
    const translationMonitoring = prefersLatestMonitorFrames(instruction);
    const historyCutoff = currentTime - (translationMonitoring ? TRANSLATION_MONITOR_HISTORY_MS : MONITOR_HISTORY_MS);
    const bufferedFrames = monitorFramesRef.current
      .filter(frame => frame.captured_at >= historyCutoff)
      .sort((left, right) => left.captured_at - right.captured_at);
    if (!instruction || bufferedFrames.length === 0) return;
    monitorFramesRef.current = [...bufferedFrames];
    const frames = selectMonitorFrames(bufferedFrames, translationMonitoring ? TRANSLATION_MONITOR_FRAME_COUNT : MONITOR_FRAME_COUNT, translationMonitoring);
    const coalescedFrameCount = bufferedFrames.length - frames.length;
    const bufferOverflowDropped = monitorBufferDroppedRef.current;
    monitorBufferDroppedRef.current = 0;

    const requestStartedAt = Date.now();
    monitorLastRequestStartedAtRef.current = requestStartedAt;
    const sequence = monitorSequenceRef.current + 1;
    monitorSequenceRef.current = sequence;
    const traceId = `monitor-${requestStartedAt}-${sequence}`;
    const skippedIntervals = monitorSkippedIntervalsRef.current;
    monitorSkippedIntervalsRef.current = 0;
    const newestFrameAt = Math.max(...frames.map(frame => frame.captured_at));
    console.info('[VideoMonitor]', {
      event: 'request_started',
      trace_id: traceId,
      skipped_intervals: skippedIntervals,
      buffered_frame_count: bufferedFrames.length,
      sampled_frame_count: frames.length,
      coalesced_frame_count: coalescedFrameCount,
      buffer_overflow_dropped: bufferOverflowDropped,
      frame_count: frames.length,
      newest_frame_age_ms: requestStartedAt - newestFrameAt,
      frame_span_ms: Math.max(...frames.map(frame => frame.captured_at)) - Math.min(...frames.map(frame => frame.captured_at)),
      frames: frames.map(frame => ({
        source_id: frame.source_id,
        frame_seq: frame.frame_seq,
        age_ms: requestStartedAt - frame.captured_at,
        width: frame.width,
        height: frame.height,
        data_url_chars: frame.data_url.length,
      })),
    });

    monitorInFlightRef.current = true;
    setMonitorPhase('evaluating');
    const controller = new AbortController();
    monitorAbortRef.current = controller;
    let requestSucceeded = false;
    try {
      const audioInputs = await snapshotSourceAudio();
      if (!isMonitoringRef.current || controller.signal.aborted || monitorRunIdRef.current !== monitorRunId) return;
      const monitorHistory = monitorEventsRef.current.slice(-MONITOR_EVENT_HISTORY_LIMIT);
      monitorEventsRef.current = monitorHistory;
      const result = await webRequest<MonitorEvaluateResponse>(
        'video.monitor.evaluate',
        {
          ...(monitorOwnerSessionIdRef.current
            ? { session_id: monitorOwnerSessionIdRef.current }
            : {}),
          monitor_run_id: monitorRunId,
          monitor_trace_id: traceId,
          client_started_at: requestStartedAt,
          skipped_intervals: skippedIntervals,
          buffered_frame_count: bufferedFrames.length,
          sampled_frame_count: frames.length,
          coalesced_frame_count: coalescedFrameCount,
          buffer_overflow_dropped: bufferOverflowDropped,
          instruction,
          frames,
          ...(audioInputs.length > 0 ? { audio_inputs: audioInputs } : {}),
          recent_events: monitorHistory,
        },
        { timeoutMs: 50_000, signal: controller.signal }
      );
      if (
        !isMonitoringRef.current ||
        controller.signal.aborted ||
        monitorRunIdRef.current !== monitorRunId ||
        (result.monitor_run_id !== undefined && result.monitor_run_id !== monitorRunId)
      )
        return;

      requestSucceeded = true;
      monitorBusyAttemptRef.current = 0;
      monitorRetryAtRef.current = 0;
      setMonitorRetrySeconds(0);
      setError('');
      setMonitorEvaluationCount(count => count + 1);
      if (result.latency_ms !== undefined) setLatencyMs(result.latency_ms);
      if (result.model) setModel(result.model);
      const completedAt = Date.now();
      console.info('[VideoMonitor]', {
        event: 'request_finished',
        trace_id: result.trace_id || traceId,
        request_ms: completedAt - requestStartedAt,
        display_lag_ms: completedAt - newestFrameAt,
        decision: result.decision,
        event_key: result.event_key,
      });
      if (result.decision === 'uncertain') {
        setMonitorPhase('uncertain');
        return;
      }
      if (result.decision !== 'emit') {
        setMonitorPhase('watching');
        return;
      }

      const eventKey = result.event_key?.trim();
      const occurrenceId = result.occurrence_id?.trim() || eventKey;
      const response = result.response?.trim();
      if (!eventKey || !response) {
        throw new Error('监控模型返回了不完整的回应。');
      }
      const emittedAt = Date.now();
      const nextEvent = {
        event_key: eventKey,
        occurrence_id: occurrenceId,
        response,
        emitted_at: emittedAt,
      };
      const replaceIndex =
        result.display_action === 'replace' ? lastMatchingIndex(monitorHistory, event => (event.occurrence_id || event.event_key) === occurrenceId) : -1;
      if (replaceIndex >= 0) {
        const updatedHistory = [...monitorHistory];
        updatedHistory[replaceIndex] = {
          ...updatedHistory[replaceIndex],
          event_key: eventKey,
          occurrence_id: occurrenceId,
          response,
        };
        monitorEventsRef.current = updatedHistory;
      } else {
        monitorEventsRef.current = [...monitorHistory, nextEvent].slice(-MONITOR_EVENT_HISTORY_LIMIT);
      }
      setMonitorResponse(response);
      setAnswer(response);
      if (replaceIndex < 0) setMonitorResponseCount(count => count + 1);
      console.info('[VideoMonitor]', {
        event: 'response_displayed',
        trace_id: result.trace_id || traceId,
        event_key: eventKey,
        display_lag_ms: Date.now() - newestFrameAt,
      });
      const turnId = `monitor-${occurrenceId}`;
      setConversationTurns(turns => {
        const existingIndex = result.display_action === 'replace' ? lastMatchingIndex(turns, turn => turn.id === turnId) : -1;
        if (existingIndex >= 0) {
          const updatedTurns = [...turns];
          updatedTurns[existingIndex] = { ...updatedTurns[existingIndex], answer: response };
          return updatedTurns;
        }
        return [
          ...turns,
          {
            id: turnId,
            question: '监控触发',
            answer: response,
          },
        ].slice(-30);
      });
      setMonitorPhase('watching');
    } catch (monitorError) {
      if (controller.signal.aborted || !isMonitoringRef.current) return;
      const providerBusy = !requestSucceeded && isMonitorProviderBusyError(monitorError);
      if (!requestSucceeded) {
        if (providerBusy) {
          const cutoff = Date.now() - MONITOR_BUSY_FRAME_WINDOW_MS;
          const retainedFrames = monitorFramesRef.current.filter(frame => frame.captured_at >= cutoff);
          monitorBufferDroppedRef.current += bufferedFrames.length + bufferOverflowDropped;
          monitorBufferDroppedRef.current += monitorFramesRef.current.length - retainedFrames.length;
          monitorFramesRef.current = retainedFrames;
        } else {
          const restoredFrames = Array.from(
            new Map(
              [...bufferedFrames, ...monitorFramesRef.current].map(frame => [frame.client_frame_id, frame])
            ).values()
          ).sort((left, right) => left.captured_at - right.captured_at);
          monitorBufferDroppedRef.current += bufferOverflowDropped;
          const overflow = restoredFrames.length - MONITOR_BUFFER_FRAME_COUNT;
          if (overflow > 0) {
            restoredFrames.splice(0, overflow);
            monitorBufferDroppedRef.current += overflow;
          }
          monitorFramesRef.current = restoredFrames;
        }
      }
      const message = monitorError instanceof Error ? monitorError.message : String(monitorError);
      if (providerBusy) {
        const retryDelayMs = monitorProviderRetryDelayMs(monitorError, monitorBusyAttemptRef.current);
        monitorBusyAttemptRef.current += 1;
        monitorRetryAtRef.current = Date.now() + retryDelayMs;
        const retrySeconds = Math.ceil(retryDelayMs / 1_000);
        setMonitorRetrySeconds(retrySeconds);
        setMonitorPhase('backoff');
        setError(`模型服务繁忙，${retrySeconds} 秒后自动重试。`);
        console.warn('[VideoMonitor]', {
          event: 'provider_busy_backoff',
          trace_id: traceId,
          request_ms: Date.now() - requestStartedAt,
          retry_delay_ms: retryDelayMs,
          busy_attempt: monitorBusyAttemptRef.current,
          error: message,
        });
        return;
      }
      setMonitorPhase('error');
      setError(message || '监控评估失败。');
      console.error('[VideoMonitor]', {
        event: 'request_failed',
        trace_id: traceId,
        request_ms: Date.now() - requestStartedAt,
        error: message,
      });
    } finally {
      if (monitorAbortRef.current === controller) {
        monitorAbortRef.current = null;
        monitorInFlightRef.current = false;
      }
      if (requestSucceeded && isMonitoringRef.current && monitorRunIdRef.current === monitorRunId && monitorFramesRef.current.length > 0) {
        const nextDelay = Math.max(0, MONITOR_INTERVAL_MS - (Date.now() - monitorLastRequestStartedAtRef.current));
        window.setTimeout(() => void evaluateMonitorRef.current(), nextDelay);
      }
    }
  }, [snapshotSourceAudio]);

  useEffect(() => {
    evaluateMonitorRef.current = evaluateMonitor;
  }, [evaluateMonitor]);

  useEffect(() => {
    if (!isMonitoring || !isPlaying || !source) return;
    void evaluateMonitor();
    const timer = window.setInterval(() => void evaluateMonitor(), MONITOR_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [evaluateMonitor, isMonitoring, isPlaying, source]);

  const startMonitoringInstruction = async (
    rawInstruction: string,
    requestedSessionId: string,
  ): Promise<VideoMonitorStartResult> => {
    const instruction = rawInstruction.trim();
    const ownerSessionId = requestedSessionId || activeSessionIdRef.current || '';
    if (!instruction) {
      setError('请先填写监控指令。');
      return {
        outcome: 'failed',
        state: 'idle',
        instruction: '',
        sessionId: ownerSessionId,
        runId: '',
        error: '监控指令不能为空。',
      };
    }
    if (isMonitoringRef.current || pendingMonitorStartRef.current) {
      const pending = pendingMonitorStartRef.current;
      return {
        outcome: 'already_active',
        state: isMonitoringRef.current ? 'active' : 'awaiting_source',
        instruction: monitorInstructionRef.current || pending?.instruction || instruction,
        sessionId: monitorOwnerSessionIdRef.current || pending?.sessionId || ownerSessionId,
        runId: monitorRunIdRef.current,
      };
    }
    if (!source || framesRef.current.length === 0) {
      pendingMonitorStartRef.current = { instruction, sessionId: ownerSessionId };
      setMonitorInstruction(instruction);
      setError('已识别监控任务，请选择摄像头、视频或共享屏幕。获取首帧后将自动开始。');
      return {
        outcome: 'needs_source',
        state: 'awaiting_source',
        instruction,
        sessionId: ownerSessionId,
        runId: '',
      };
    }
    if (isTranslating) {
      await webRequest('video.task.stop', {
        ...(ownerSessionId ? { session_id: ownerSessionId } : {}),
      }).catch(() => undefined);
      setIsTranslating(false);
    }
    setMonitorInstruction(instruction);
    monitorInstructionRef.current = instruction;
    monitorOwnerSessionIdRef.current = ownerSessionId;
    monitorRunIdRef.current =
      typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : `monitor-run-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    monitorEventsRef.current = [];
    monitorSequenceRef.current = 0;
    monitorSkippedIntervalsRef.current = 0;
    const newestFramesBySource = new Map<string, CapturedFrame>();
    framesRef.current.forEach(frame => {
      newestFramesBySource.set(frame.source_id, frame);
    });
    monitorFramesRef.current = Array.from(newestFramesBySource.values());
    monitorBufferDroppedRef.current = 0;
    monitorLastRequestStartedAtRef.current = 0;
    monitorBusyAttemptRef.current = 0;
    monitorRetryAtRef.current = 0;
    setMonitorRetrySeconds(0);
    isMonitoringRef.current = true;
    setMonitorResponse('');
    setMonitorEvaluationCount(0);
    setMonitorResponseCount(0);
    setMonitorPhase('watching');
    setError('');
    setIsMonitoring(true);
    return {
      outcome: 'started',
      state: 'active',
      instruction,
      sessionId: ownerSessionId,
      runId: monitorRunIdRef.current,
    };
  };

  startMonitorRequestRef.current = startMonitoringInstruction;

  const startMonitoring = async () => {
    await startMonitoringInstruction(
      monitorInstruction,
      activeSessionIdRef.current || '',
    );
  };

  useEffect(() => {
    const pending = pendingMonitorStartRef.current;
    if (!pending || !source || !isPlaying || frameCount === 0 || isMonitoringRef.current) return;
    pendingMonitorStartRef.current = null;
    void startMonitorRequestRef.current(pending.instruction, pending.sessionId);
  }, [frameCount, isPlaying, source]);

  const runOmniRequest = async (
    prompt: string,
    questionAudioDataUrl?: string,
    verifiedVoiceQuestion = false,
  ) => {
    if ((!prompt && !questionAudioDataUrl) || isAskingRef.current) return;
    const turnId = beginConversationTurn(prompt || '🎙️ 语音提问');
    setIsAsking(true);
    isAskingRef.current = true;

    if (prompt.trim()) {
      const recentContext = conversationTurns
        .slice(-2)
        .flatMap(turn => [
          { role: 'user', content: turn.question.slice(0, 1_000) },
          ...(turn.answer.trim()
            ? [{ role: 'assistant', content: turn.answer.slice(0, 1_000) }]
            : []),
        ])
        .slice(-4);
      try {
        const intent = await webRequest<MonitorIntentResponse>(
          'video.monitor.intent',
          {
            ...(activeSessionIdRef.current
              ? { session_id: activeSessionIdRef.current }
              : {}),
            content: prompt,
            recent_context: recentContext,
          },
          { timeoutMs: 6_000 },
        );
        const confidence = typeof intent.confidence === 'number' ? intent.confidence : 0;
        const instruction = intent.instruction?.trim() || prompt.trim();
        if (
          intent.action === 'start_monitor'
          && confidence >= 0.72
          && instruction
        ) {
          const startResult = await startMonitoringInstruction(
            instruction,
            activeSessionIdRef.current || '',
          );
          if (startResult.outcome !== 'failed') {
            const acknowledgement = startResult.outcome === 'started'
              ? `已开始监控：${startResult.instruction}`
              : startResult.outcome === 'needs_source'
                ? `已识别监控任务：${startResult.instruction}\n\n请选择摄像头、视频或共享屏幕，获取画面后会自动开始。`
                : `当前已有监控任务：${startResult.instruction}\n\n请先停止当前监控任务。`;
            replaceTurnAnswer(turnId, acknowledgement);
            if (intent.model) setModel(intent.model);
            setQuestion('');
            activeTurnIdRef.current = null;
            isAskingRef.current = false;
            setIsAsking(false);
            if (voiceConversationRef.current) {
              window.setTimeout(() => void startVoiceListeningRef.current(), 120);
            }
            return;
          }
        }
      } catch (intentError) {
        console.warn(
          '[VideoMonitor] intent classification failed; continuing as one-time question.',
          intentError,
        );
      }
    }

    if (framesRef.current.length === 0) {
      const message = verifiedVoiceQuestion
        ? '语音已识别，但尚未捕获到可用画面，请等待画面播放后重试。'
        : '请先打开视频并等待画面开始播放。';
      setError(message);
      replaceTurnAnswer(turnId, message);
      activeTurnIdRef.current = null;
      isAskingRef.current = false;
      setIsAsking(false);
      if (voiceConversationRef.current) {
        window.setTimeout(() => void startVoiceListeningRef.current(), 180);
      }
      return;
    }

    stopSpeechPlayback();
    streamedAnswerRef.current = '';
    nativeAudioExpectedRef.current = Boolean(
      (questionAudioDataUrl || verifiedVoiceQuestion) && isSpeechEnabled,
    );
    setError('');
    setToolStatus('');
    setElapsedMs(0);
    const startedAt = performance.now();
    requestStartedAtRef.current = startedAt;
    activeRequestIdRef.current = null;
    streamFirstTokenMsRef.current = null;
    const controller = new AbortController();
    requestAbortRef.current = controller;
    const timeoutId = window.setTimeout(() => {
      if (requestAbortRef.current === controller) {
        abortQuestion('timeout');
      }
    }, REQUEST_TIMEOUT_MS);
    try {
      // A microphone question must not be polluted by screen/video audio.
      const audioInputs = questionAudioDataUrl || verifiedVoiceQuestion
        ? []
        : await snapshotSourceAudio();
      if (questionAudioDataUrl) {
        audioInputs.push({
          data_url: questionAudioDataUrl,
          source_label: '用户麦克风提问',
        });
      }
      const request = webRequest<VideoAskResponse>(
        'video.ask',
        {
          ...(activeSessionId ? { session_id: activeSessionId } : {}),
          question: prompt,
          source,
          frames: framesRef.current.slice(-MAX_FRAMES),
          ...(audioInputs.length > 0 ? { audio_inputs: audioInputs } : {}),
          ...(verifiedVoiceQuestion ? { voice_question: true } : {}),
          ...(questionAudioDataUrl && assistantSpeechTextRef.current
            ? { assistant_speech_text: assistantSpeechTextRef.current }
            : {}),
        },
        {
          timeoutMs: REQUEST_TIMEOUT_MS + 1_000,
          signal: controller.signal,
          onRequestId: requestId => {
            if (requestAbortRef.current === controller) {
              activeRequestIdRef.current = requestId;
            }
          },
        }
      );
      const result = await request;
      // The model request is complete. Audio playback may be longer than the
      // request timeout and must not be mistaken for a hung model call.
      window.clearTimeout(timeoutId);
      if (result.outcome === 'voice_rejected' || (result.ignored && !result.outcome)) {
        if (result.transcript) replaceTurnQuestion(turnId, result.transcript);
        const rejectionMessage = '语音已收到，但未通过有效问题确认，请重试。';
        setError(rejectionMessage);
        replaceTurnAnswer(turnId, rejectionMessage);
      } else {
        if (result.transcript) replaceTurnQuestion(turnId, result.transcript);
        const finalAnswer = result.answer?.trim()
          || (result.outcome === 'empty_model_answer'
            ? '模型没有返回可显示的文本，请重试。'
            : '模型没有返回文本。');
        assistantSpeechTextRef.current = finalAnswer;
        replaceTurnAnswer(turnId, finalAnswer);
        if (!result.native_audio_emitted) {
          if (!streamedAnswerRef.current || nativeAudioExpectedRef.current) {
            queueSpeechText(finalAnswer);
          }
          queueSpeechText('', true);
        }
      }
      setLatencyMs(result.latency_ms ?? Math.round(performance.now() - startedAt));
      setFirstTokenMs(result.first_token_ms ?? null);
      if (result.model) setModel(result.model);
      setRequestCount(count => count + 1);
      if (!questionAudioDataUrl) setQuestion('');
    } catch (requestError) {
      const abortReason = requestAbortReasonsRef.current.get(controller);
      if (abortReason === 'timeout') {
        setError('超过 45 秒仍未收到结果，已停止等待，请重试。');
        replaceTurnAnswer(turnId, '请求超时，请重试。');
      } else if (abortReason === 'manual') {
        setError('已取消本次问答。');
        replaceTurnAnswer(turnId, '本次问答已取消。');
      } else if (abortReason === 'barge-in') {
        replaceTurnAnswer(turnId, '已被新的语音提问打断。');
      } else if (abortReason !== 'source' && abortReason !== 'barge-in') {
        const code = (requestError as { code?: unknown })?.code;
        const message =
          code === 'WS_DISCONNECTED' || code === 'WS_CLOSED'
            ? '视频连接已断开，请重新提问。'
            : requestError instanceof Error
              ? requestError.message
              : 'Jiuwen 音视频问答接口调用失败。';
        setError(message);
        replaceTurnAnswer(turnId, message);
      }
    } finally {
      window.clearTimeout(timeoutId);
      requestAbortReasonsRef.current.delete(controller);
      if (requestAbortRef.current === controller) {
        requestAbortRef.current = null;
        activeRequestIdRef.current = null;
        if (activeTurnIdRef.current === turnId) activeTurnIdRef.current = null;
        isAskingRef.current = false;
        setIsAsking(false);
        setToolStatus('');
        if (voiceConversationRef.current) {
          window.setTimeout(() => void startVoiceListeningRef.current(), 120);
        }
      }
    }
  };

  const askVideo = async (event: FormEvent) => {
    event.preventDefault();
    await runOmniRequest(question.trim());
  };

  const stopRecording = () => {
    const recorder = mediaRecorderRef.current;
    if (recorder?.state === 'recording') recorder.stop();
  };

  const startRecording = async () => {
    if (
      mediaRecorderRef.current
      || (isAskingRef.current && !voiceConversationRef.current)
    ) return;
    if (framesRef.current.length === 0) {
      setError('请先打开视频并等待画面开始播放。');
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      setError('当前运行环境不支持麦克风录音。');
      return;
    }

    setError('');
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
        video: false,
      });
      microphoneStreamRef.current = stream;
      audioChunksRef.current = [];
      const mimeType = AUDIO_MIME_TYPES.find(type => MediaRecorder.isTypeSupported(type));
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
      let heardSpeech = false;
      let consecutiveSpeechFrames = 0;
      let voicedFrames = 0;
      let noiseFloor = 0.006;
      const calibrationStartedAt = performance.now();
      let lastSpeechAt = performance.now();
      mediaRecorderRef.current = recorder;
      recorder.ondataavailable = recordedEvent => {
        if (recordedEvent.data.size > 0) audioChunksRef.current.push(recordedEvent.data);
      };
      recorder.onerror = () => {
        setError('麦克风录音失败，请重试。');
        cancelRecording();
      };
      recorder.onstop = () => {
        const chunks = audioChunksRef.current.slice();
        const recordedMimeType = recorder.mimeType || 'audio/webm';
        mediaRecorderRef.current = null;
        audioChunksRef.current = [];
        releaseMicrophone();
        setIsRecording(false);
        if (!heardSpeech || voicedFrames < 4) {
          if (voiceConversationRef.current) {
            window.setTimeout(() => void startVoiceListeningRef.current(), 300);
          }
          return;
        }
        const audioBlob = new Blob(chunks, { type: recordedMimeType });
        if (audioBlob.size === 0) {
          setError('没有录到声音，请重试。');
          if (voiceConversationRef.current) {
            window.setTimeout(() => void startVoiceListeningRef.current(), 180);
          }
          return;
        }
        void audioBlobToWavDataUrl(audioBlob)
          .then(async (audioDataUrl) => {
            const verification = await webRequest<{
              transcript?: string;
              accepted?: boolean;
            }>('video.transcribe', {
              audio_inputs: [{
                data_url: audioDataUrl,
                source_label: '用户麦克风提问',
              }],
              ...(assistantSpeechTextRef.current
                ? { assistant_speech_text: assistantSpeechTextRef.current }
                : {}),
            }, { timeoutMs: ASR_REQUEST_TIMEOUT_MS });
            const transcript = verification.transcript?.trim() || '';
            if (!verification.accepted || !transcript) {
              resumeSpeechPlayback();
              if (voiceConversationRef.current && !assistantAudioRef.current) {
                window.setTimeout(() => void startVoiceListeningRef.current(), 180);
              }
              return;
            }
            if (isAskingRef.current) {
              abortQuestion('barge-in');
              await new Promise((resolve) => window.setTimeout(resolve, 80));
            }
            stopSpeechPlayback();
            await runOmniRequest(transcript, undefined, true);
          })
          .catch((recordingError) => {
            setError(recordingError instanceof Error ? recordingError.message : '读取录音失败。');
          });
      };
      recorder.start(250);
      setIsRecording(true);
      const audioContext = new AudioContext();
      voiceAudioContextRef.current = audioContext;
      const analyser = audioContext.createAnalyser();
      analyser.fftSize = 1024;
      audioContext.createMediaStreamSource(stream).connect(analyser);
      const levels = new Float32Array(analyser.fftSize);
      voiceVadTimerRef.current = window.setInterval(() => {
        if (recorder.state !== 'recording') return;
        analyser.getFloatTimeDomainData(levels);
        let energy = 0;
        for (const level of levels) energy += level * level;
        const rms = Math.sqrt(energy / levels.length);
        const now = performance.now();
        if (now - calibrationStartedAt < 200 && rms < 0.02) {
          noiseFloor = noiseFloor * 0.85 + rms * 0.15;
        }
        const outputIsPlaying = assistantAudioRef.current !== null;
        const speechThreshold = Math.max(
          outputIsPlaying ? 0.024 : 0.018,
          noiseFloor * (outputIsPlaying ? 2.8 : 2.2),
        );
        if (rms >= speechThreshold) {
          consecutiveSpeechFrames += 1;
          voicedFrames += 1;
          const requiredFrames = outputIsPlaying ? 4 : 2;
          if (!heardSpeech && consecutiveSpeechFrames >= requiredFrames) {
            heardSpeech = true;
            if (outputIsPlaying) {
              setToolStatus('检测到插话，已停止播报');
              pauseSpeechPlayback();
            }
          }
          lastSpeechAt = performance.now();
        } else {
          consecutiveSpeechFrames = 0;
          noiseFloor = noiseFloor * 0.98 + rms * 0.02;
        }
        if (heardSpeech && now - lastSpeechAt >= 900) {
          stopRecording();
        }
      }, 80);
      recordingTimeoutRef.current = window.setTimeout(stopRecording, RECORDING_LIMIT_MS);
    } catch (microphoneError) {
      cancelRecording();
      setError(microphoneError instanceof Error ? microphoneError.message : '无法打开麦克风。');
    }
  };

  startVoiceListeningRef.current = startRecording;

  const toggleVoiceConversation = () => {
    if (voiceConversationRef.current) {
      voiceConversationRef.current = false;
      setIsVoiceConversation(false);
      cancelRecording();
      return;
    }
    voiceConversationRef.current = true;
    setIsVoiceConversation(true);
    setQuestion('');
    void startRecording();
  };

  return (
    <section className="video-live">
      <header className="video-live__header">
        <div className="video-live__brand">
          <span className="video-live__brand-icon">
            <Video aria-hidden />
          </span>
          <div>
            <h1>Jiuwen Omni Live</h1>
            <p>多屏音视频实时问答</p>
          </div>
        </div>
        <div className={`video-live__status ${isPlaying ? 'is-live' : ''}`}>
          <span />
          {isPlaying ? 'STREAMING' : 'IDLE'}
        </div>
      </header>

      <div className="video-live__grid">
        <div className="video-live__viewer-card">
          <div className="video-live__viewer">
            <video
              ref={videoRef}
              className={source === 'screen' ? 'is-hidden' : ''}
              controls={source === 'file'}
              playsInline
              onPlay={() => setIsPlaying(true)}
              onPause={() => setIsPlaying(false)}
              onEnded={() => setIsPlaying(false)}
            />
            {source === 'screen' && (
              <div className={`video-live__screen-grid video-live__screen-grid--${Math.min(screens.length, 4)}`}>
                {screens.map(screen => (
                  <div className="video-live__screen-tile" key={screen.id}>
                    <video
                      ref={node => {
                        if (!node) {
                          screenVideoRefs.current.delete(screen.id);
                          return;
                        }
                        screenVideoRefs.current.set(screen.id, node);
                        if (node.srcObject !== screen.stream) node.srcObject = screen.stream;
                        void node.play().catch(() => undefined);
                      }}
                      autoPlay
                      muted
                      playsInline
                    />
                    <div className="video-live__screen-label">
                      <span className="is-live" />
                      {screen.name}
                    </div>
                    <button className="video-live__screen-close" type="button" onClick={() => removeScreen(screen.id)} aria-label={`关闭${screen.name}`}>
                      <X aria-hidden />
                    </button>
                  </div>
                ))}
              </div>
            )}
            {!source && (
              <div className="video-live__empty">
                <span className="video-live__empty-icon">
                  <Video aria-hidden />
                </span>
                <strong>打开一个实时画面</strong>
                <p>使用摄像头、本地视频，或添加多个屏幕</p>
              </div>
            )}
            {source && source !== 'screen' && (
              <div className="video-live__source-chip">
                <span className={isPlaying ? 'is-live' : ''} />
                {sourceName}
              </div>
            )}
            {liveSubtitle && <div className="video-live__subtitle">{liveSubtitle}</div>}
            {monitorResponse && (
              <div className="video-live__monitor-response" role="status">
                <Radar aria-hidden />
                <span ref={monitorResponseTextRef}>{monitorResponse}</span>
                <button type="button" onClick={() => setMonitorResponse('')} aria-label="关闭监控回应">
                  <X aria-hidden />
                </button>
              </div>
            )}
            {source && source !== 'screen' && (
              <button className="video-live__close" type="button" onClick={closeSource} aria-label="关闭视频">
                <X aria-hidden />
              </button>
            )}
          </div>

          <div className="video-live__source-actions">
            <button type="button" className="video-live__source-button" onClick={() => void startCamera()}>
              <Camera aria-hidden />
              摄像头
            </button>
            <label className="video-live__source-button">
              <FileVideo aria-hidden />
              本地视频
              <input type="file" accept="video/*" onChange={event => void openFile(event)} />
            </label>
            <button
              type="button"
              className="video-live__source-button"
              disabled={source === 'screen' && screens.length >= MAX_SCREENS}
              onClick={() => void startScreen()}
            >
              <Monitor aria-hidden />
              {source === 'screen' ? '添加屏幕' : '共享屏幕'}
            </button>
            {source && (
              <select
                className="video-live__language"
                value={targetLanguage}
                disabled={isTranslating}
                onChange={event => setTargetLanguage(event.target.value)}
                aria-label="翻译目标语言"
              >
                <option value="中文">中文</option>
                <option value="English">English</option>
                <option value="日本語">日本語</option>
              </select>
            )}
            {source && (
              <button type="button" className="video-live__source-button video-live__source-button--stop" onClick={closeSource}>
                <X aria-hidden />
                {source === 'camera' ? '停止摄像头' : source === 'screen' ? '停止全部屏幕' : '关闭视频'}
              </button>
            )}
            {source && (
              <button
                type="button"
                className={`video-live__source-button${isTranslating ? ' video-live__source-button--active' : ''}`}
                onClick={() =>
                  void toggleTranslation().catch(taskError => {
                    setError(taskError instanceof Error ? taskError.message : '实时翻译启动失败');
                  })
                }
              >
                {isTranslating ? '停止翻译' : '实时翻译'}
              </button>
            )}
            <button
              type="button"
              className={`video-live__source-button${isSpeechEnabled ? ' video-live__source-button--active' : ''}`}
              onClick={() => {
                if (isSpeechEnabled) stopSpeechPlayback();
                setIsSpeechEnabled((enabled) => !enabled);
              }}
              title={isSpeechEnabled ? '关闭模型语音' : '开启模型语音'}
            >
              {isSpeechEnabled ? <Volume2 aria-hidden /> : <VolumeX aria-hidden />}
              {isSpeaking ? '正在播报' : '语音播报'}
            </button>
            <span className="video-live__frame-count">
              滚动窗口：{frameCount}/{MAX_FRAMES} 帧{source === 'screen' ? ` · ${screens.length}/${MAX_SCREENS} 屏` : ''}
              {` · 音频 ${audioSourceCount} 路`}
            </span>
          </div>
          <div className="video-live__monitor-panel">
            <div className="video-live__monitor-title">
              <span>
                <Radar aria-hidden />
                当前任务
              </span>
              <span className={`video-live__monitor-state is-${monitorPhase}`}>
                {monitorPhase === 'evaluating'
                  ? '正在判断'
                  : monitorPhase === 'backoff'
                    ? `模型繁忙 · ${monitorRetrySeconds} 秒后重试`
                    : monitorPhase === 'uncertain'
                      ? '证据不足'
                      : monitorPhase === 'error'
                        ? '检查失败'
                        : isMonitoring
                          ? '监控中'
                          : '未启动'}
              </span>
            </div>
            <div className="video-live__monitor-controls">
              <input
                value={monitorInstruction}
                disabled={isMonitoring}
                onChange={event => setMonitorInstruction(event.target.value)}
                placeholder="输入持续监控指令"
                aria-label="监控指令"
              />
              <button
                type="button"
                className={isMonitoring ? 'is-active' : ''}
                disabled={!isMonitoring && (!source || !isPlaying || !monitorInstruction.trim())}
                onClick={() => (isMonitoring ? stopMonitoring(false) : void startMonitoring())}
              >
                {isMonitoring ? <Square aria-hidden /> : <Play aria-hidden />}
                {isMonitoring ? '停止' : '启动'}
              </button>
            </div>
            <div className="video-live__monitor-meta">
              <span>检查 {monitorEvaluationCount}</span>
              <span>回应 {monitorResponseCount}</span>
            </div>
          </div>
        </div>

        <div className="video-live__output-card">
          <div className="video-live__output-head">
            <div>
              <span className="video-live__eyebrow">VLM OUTPUT</span>
              <strong>{model}</strong>
            </div>
            <div className="video-live__metrics">
              <span>
                First <b>{firstTokenMs === null ? '—' : `${firstTokenMs} ms`}</b>
              </span>
              <span>
                Total <b>{latencyMs === null ? '—' : `${latencyMs} ms`}</b>
              </span>
              <span>
                Count <b>{requestCount}</b>
              </span>
              <span>
                字幕 <b>{translationCount}</b>
              </span>
            </div>
          </div>

          <div className="video-live__prompt-banner">
            <span>当前模式</span>
            {isVoiceConversation && isRecording
              ? isSpeaking
                ? '持续语音 · 正在播报并监听，识别到插话后自动打断'
                : '持续语音 · 正在监听，停顿后自动发送'
              : isVoiceConversation
                ? '持续语音 · 正在恢复监听'
                : toolStatus
                  ? toolStatus
                  : isMonitoring
                    ? monitorPhase === 'evaluating'
                      ? '当前任务 · 模型正在判断最新画面'
                      : monitorPhase === 'backoff'
                        ? `当前任务 · 模型服务繁忙，${monitorRetrySeconds} 秒后自动重试`
                        : monitorPhase === 'uncertain'
                          ? '当前任务 · 当前证据不足，继续观察'
                          : '当前任务 · 等待触发条件'
                    : isTranslating
                      ? '实时翻译运行中 · 每秒处理最新画面'
                      : `当前窗口最多 ${MAX_FRAMES} 帧${source === 'screen' ? ` · ${screens.length} 个屏幕` : ''} · 长/中期记忆 · 必要时 memory/free_search${isSpeechEnabled ? ' · 语音播报' : ''}`}
          </div>

          <div className="video-live__answer">
            {conversationTurns.length > 0 ? (
              <div className="video-live__history">
                {conversationTurns.map((turn, index) => (
                  <div className="video-live__turn" key={turn.id}>
                    <div className="video-live__question">{turn.question}</div>
                    <div className="video-live__turn-answer">
                      {turn.answer ? (
                        <MarkdownRenderer content={turn.answer} className="chat-markdown" />
                      ) : isAsking && index === conversationTurns.length - 1 ? (
                        <span className="video-live__turn-loading">
                          <LoaderCircle className="is-spinning" aria-hidden />
                          {toolStatus || 'Omni 正在处理'}
                        </span>
                      ) : null}
                    </div>
                  </div>
                ))}
              </div>
            ) : answer ? (
              <MarkdownRenderer content={answer} className="video-live__answer-content chat-markdown" />
            ) : isAsking ? (
              <div className="video-live__answer-empty">
                <LoaderCircle className="is-spinning" aria-hidden />
                <strong>{toolStatus || 'Jiuwen Agent 正在处理'}</strong>
                <span>{(elapsedMs / 1000).toFixed(1)} 秒 · 最长等待 120 秒</span>
              </div>
            ) : (
              <div className="video-live__answer-empty">
                <Video aria-hidden />
                <strong>等待提问</strong>
                <span>例如：刚才谁进入了画面？</span>
              </div>
            )}
          </div>

          {error && <div className="video-live__error">{error}</div>}

          <form className="video-live__composer" onSubmit={event => void askVideo(event)}>
            <button
              type="button"
              className={`video-live__mic${isVoiceConversation ? ' is-recording' : ''}`}
              disabled={isAsking && !isVoiceConversation}
              onClick={toggleVoiceConversation}
              aria-label={isVoiceConversation ? '停止持续语音' : '开始持续语音'}
              title={isVoiceConversation ? '停止持续语音' : '开始持续语音'}
            >
              {isVoiceConversation ? <Square aria-hidden /> : <Mic aria-hidden />}
            </button>
            <input
              value={question}
              onChange={event => setQuestion(event.target.value)}
              placeholder={isVoiceConversation ? '持续语音已开启……' : '询问当前画面，必要时自动调用工具……'}
              disabled={isAsking || isVoiceConversation}
            />
            <button
              type={isAsking ? 'button' : 'submit'}
              disabled={!isAsking && (!question.trim() || isVoiceConversation)}
              onClick={isAsking ? () => abortQuestion('manual') : undefined}
              aria-label={isAsking ? '取消问答' : '发送问题'}
              title={isAsking ? '取消问答' : '发送问题'}
            >
              {isAsking ? <X aria-hidden /> : <Send aria-hidden />}
            </button>
          </form>
        </div>
      </div>
    </section>
  );
}
