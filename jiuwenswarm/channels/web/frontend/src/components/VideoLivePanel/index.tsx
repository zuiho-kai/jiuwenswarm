import { ChangeEvent, FormEvent, useCallback, useEffect, useRef, useState } from 'react';
import { Camera, FileVideo, LoaderCircle, Mic, Monitor, Send, Square, Video, Volume2, VolumeX, X } from 'lucide-react';
import { webClient, webRequest } from '../../services/webClient';
import { useChatStore } from '../../stores/chatStore';
import { fetchTtsAudio, sanitizeTtsText } from '../../utils';
import { MarkdownRenderer } from '../MarkdownRenderer';
import './VideoLivePanel.css';

type VideoSource = 'camera' | 'file' | 'screen' | null;
type AbortReason = 'manual' | 'timeout' | 'source';

interface CapturedFrame {
  client_frame_id: string;
  frame_seq: number;
  data_url: string;
  captured_at: number;
  source_id: string;
  source_label: string;
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
  latency_ms?: number;
  first_token_ms?: number;
  model?: string;
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
const MAX_FRAME_WIDTH = 768;
const REQUEST_TIMEOUT_MS = 45_000;
const RECORDING_LIMIT_MS = 15_000;
const SOURCE_AUDIO_SEGMENT_MS = 3_000;
const AUDIO_MIME_TYPES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
];

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
  const activeSessionId = useChatStore((state) => state.activeSessionId);
  const videoRef = useRef<HTMLVideoElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
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
  const requestAbortReasonRef = useRef<AbortReason | null>(null);
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
  const assistantSpeechTextRef = useRef('');
  const ttsAbortControllersRef = useRef<Set<AbortController>>(new Set());

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
  const [model, setModel] = useState('Qwen3-Omni');
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

  const beginConversationTurn = useCallback((prompt: string) => {
    const id = `turn-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    activeTurnIdRef.current = id;
    setAnswer('');
    setConversationTurns((turns) => [
      ...turns,
      { id, question: prompt || '🎙️ 语音提问', answer: '' },
    ].slice(-30));
  }, []);

  const appendActiveAnswer = useCallback((delta: string) => {
    if (!delta) return;
    setAnswer((current) => current + delta);
    const id = activeTurnIdRef.current;
    if (!id) return;
    setConversationTurns((turns) => turns.map((turn) => (
      turn.id === id ? { ...turn, answer: turn.answer + delta } : turn
    )));
  }, []);

  const replaceActiveAnswer = useCallback((value: string) => {
    setAnswer(value);
    const id = activeTurnIdRef.current;
    if (!id) return;
    setConversationTurns((turns) => turns.map((turn) => (
      turn.id === id ? { ...turn, answer: value } : turn
    )));
  }, []);

  const replaceActiveQuestion = useCallback((value: string) => {
    const id = activeTurnIdRef.current;
    if (!id || !value.trim()) return;
    setConversationTurns((turns) => turns.map((turn) => (
      turn.id === id ? { ...turn, question: value.trim() } : turn
    )));
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

  const removeActiveTurn = useCallback(() => {
    const id = activeTurnIdRef.current;
    if (!id) return;
    setConversationTurns((turns) => turns.filter((turn) => turn.id !== id));
    setAnswer('');
  }, []);

  useEffect(() => {
    activeSessionIdRef.current = activeSessionId;
  }, [activeSessionId]);

  const stopSourceAudio = useCallback((sourceId: string) => {
    const capture = sourceAudioCapturesRef.current.get(sourceId);
    if (!capture) return;
    capture.disposed = true;
    if (capture.timerId !== null) window.clearTimeout(capture.timerId);
    capture.timerId = null;
    capture.recorder && (capture.recorder.onstop = null);
    if (capture.recorder?.state === 'recording') capture.recorder.stop();
    capture.waiters.splice(0).forEach((resolve) => resolve());
    sourceAudioCapturesRef.current.delete(sourceId);
    setAudioSourceCount(sourceAudioCapturesRef.current.size);
  }, []);

  const attachSourceAudio = useCallback((sourceId: string, label: string, stream: MediaStream) => {
    stopSourceAudio(sourceId);
    const liveAudioTracks = stream.getAudioTracks().filter((track) => track.readyState === 'live');
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
    const mimeType = AUDIO_MIME_TYPES.find((type) => MediaRecorder.isTypeSupported(type));

    const startSegment = () => {
      if (capture.disposed) return;
      const tracks = capture.stream.getAudioTracks().filter((track) => track.readyState === 'live');
      if (tracks.length === 0) return;
      capture.chunks = [];
      const recorder = new MediaRecorder(
        new MediaStream(tracks),
        mimeType ? { mimeType } : undefined,
      );
      capture.recorder = recorder;
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) capture.chunks.push(event.data);
      };
      recorder.onstop = () => {
        if (capture.timerId !== null) window.clearTimeout(capture.timerId);
        capture.timerId = null;
        const blob = new Blob(capture.chunks, { type: recorder.mimeType || 'audio/webm' });
        if (blob.size > 0) capture.latestBlob = blob;
        capture.chunks = [];
        capture.recorder = null;
        capture.waiters.splice(0).forEach((resolve) => resolve());
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
  }, [stopSourceAudio]);

  const snapshotSourceAudio = useCallback(async (): Promise<AudioInput[]> => {
    const captures = Array.from(sourceAudioCapturesRef.current.values())
      .filter((capture) => !capture.disposed);
    await Promise.all(captures.map((capture) => new Promise<void>((resolve) => {
      const recorder = capture.recorder;
      if (!recorder || recorder.state !== 'recording') {
        resolve();
        return;
      }
      capture.waiters.push(resolve);
      if (capture.timerId !== null) window.clearTimeout(capture.timerId);
      capture.timerId = null;
      recorder.stop();
    })));
    const inputs = await Promise.all(captures.map(async (capture) => {
      if (!capture.latestBlob) return null;
      return {
        data_url: await audioBlobToWavDataUrl(capture.latestBlob),
        source_label: capture.label,
      };
    }));
    return inputs.filter((input): input is AudioInput => input !== null);
  }, []);

  const releaseSource = useCallback(() => {
    stopSourceAudio('camera');
    stopSourceAudio('file');
    cameraStreamRef.current?.getTracks().forEach((track) => track.stop());
    cameraStreamRef.current = null;
    fileCaptureStreamRef.current?.getTracks().forEach((track) => track.stop());
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
    lastMemoryFlushAtRef.current = 0;
    setFrameCount(0);
    setIsPlaying(false);
  }, [stopSourceAudio]);

  const releaseScreens = useCallback((updateState = true) => {
    screenStreamsRef.current.forEach((stream, screenId) => {
      stopSourceAudio(screenId);
      stream.getTracks().forEach((track) => track.stop());
    });
    screenStreamsRef.current.clear();
    screenVideoRefs.current.clear();
    if (updateState) setScreens([]);
  }, [stopSourceAudio]);

  const abortQuestion = useCallback((reason: AbortReason) => {
    requestAbortReasonRef.current = reason;
    requestAbortRef.current?.abort();
    requestAbortRef.current = null;
    activeRequestIdRef.current = null;
    isAskingRef.current = false;
    setIsAsking(false);
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
    microphoneStreamRef.current?.getTracks().forEach((track) => track.stop());
    microphoneStreamRef.current = null;
  }, []);

  const cancelRecording = useCallback((updateState = true) => {
    const recorder = mediaRecorderRef.current;
    if (recorder) {
      recorder.onstop = null;
      if (recorder.state !== 'inactive') recorder.stop();
    }
    mediaRecorderRef.current = null;
    audioChunksRef.current = [];
    releaseMicrophone();
    if (updateState) setIsRecording(false);
  }, [releaseMicrophone]);

  const closeSource = () => {
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
      if (isAskingRef.current && typeof requestId === 'string') {
        activeRequestIdRef.current = requestId;
        if (typeof event.payload.model === 'string' && event.payload.model) {
          setModel(event.payload.model);
        }
        setToolStatus('');
      }
    });
    const offDelta = webClient.on<{ request_id?: unknown; content?: unknown }>('video.delta', (event) => {
      if (event.payload.request_id !== activeRequestIdRef.current) return;
      const content = event.payload.content;
      if (typeof content === 'string' && content) {
        if (streamFirstTokenMsRef.current === null) {
          streamFirstTokenMsRef.current = Math.round(
            performance.now() - requestStartedAtRef.current,
          );
          setFirstTokenMs(streamFirstTokenMsRef.current);
        }
        streamedAnswerRef.current += content;
        queueSpeechText(content);
        appendActiveAnswer(content);
      }
    });
    const offTranscript = webClient.on<{
      request_id?: unknown;
      text?: unknown;
      accepted?: unknown;
    }>('video.transcript', (event) => {
      if (event.payload.request_id !== activeRequestIdRef.current) return;
      if (event.payload.accepted !== true) return;
      stopSpeechPlayback();
      if (typeof event.payload.text === 'string') {
        replaceActiveQuestion(event.payload.text);
      }
    });
    const offVideoToolStatus = webClient.on<{
      request_id?: unknown;
      status?: unknown;
    }>('video.tool_status', (event) => {
      if (event.payload.request_id !== activeRequestIdRef.current) return;
      if (typeof event.payload.status === 'string') {
        setToolStatus(event.payload.status);
      }
    });
    const offTaskResponse = webClient.on<{
      text?: unknown;
      source_id?: unknown;
      frame_seq?: unknown;
    }>('video.task.response', (event) => {
      const text = event.payload.text;
      if (typeof text !== 'string' || !text.trim()) return;
      setLiveSubtitle(text.trim());
      setAnswer(text.trim());
      setTranslationCount((count) => count + 1);
    });
    const offTaskError = webClient.on<{ error?: unknown }>('video.task.error', (event) => {
      const taskError = event.payload.error;
      if (typeof taskError === 'string' && taskError) setError(taskError);
    });
    const offMemoryError = webClient.on<{ error?: unknown }>('video.memory.error', (event) => {
      const memoryError = event.payload.error;
      if (typeof memoryError === 'string' && memoryError) setError(memoryError);
    });
    return () => {
      offStarted();
      offDelta();
      offTranscript();
      offVideoToolStatus();
      offTaskResponse();
      offTaskError();
      offMemoryError();
    };
  }, [appendActiveAnswer, queueSpeechText, replaceActiveAnswer, replaceActiveQuestion, stopSpeechPlayback]);

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
        await webRequest(
          'video.observe',
          { session_id: activeSessionId, frames },
          { timeoutMs: 30_000 },
        );
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
      const canvas = canvasRef.current;
      if (!canvas || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return;
      if (!video.videoWidth || !video.videoHeight) return;

      const scale = Math.min(1, MAX_FRAME_WIDTH / video.videoWidth);
      canvas.width = Math.round(video.videoWidth * scale);
      canvas.height = Math.round(video.videoHeight * scale);
      const context = canvas.getContext('2d');
      if (!context) return;

      context.drawImage(video, 0, 0, canvas.width, canvas.height);
      const nextFrameSeq = frameSequencesRef.current.get(sourceId) ?? 0;
      frameSequencesRef.current.set(sourceId, nextFrameSeq + 1);
      const frame: CapturedFrame = {
        client_frame_id: typeof crypto.randomUUID === 'function'
          ? crypto.randomUUID()
          : `${sourceId}-${Date.now()}-${nextFrameSeq}`,
        frame_seq: nextFrameSeq,
        data_url: canvas.toDataURL('image/jpeg', 0.72),
        captured_at: Date.now(),
        source_id: sourceId,
        source_label: sourceLabel,
      };
      framesRef.current.push(frame);
      memoryFramesRef.current.set(sourceId, frame);
    };

    const capture = () => {
      if (source === 'screen') {
        screens.forEach((screen) => {
          const video = screenVideoRefs.current.get(screen.id);
          if (video) captureVideo(video, screen.id, screen.name);
        });
      } else {
        const video = videoRef.current;
        if (video) {
          captureVideo(
            video,
            source || 'video',
            source === 'camera' ? '摄像头' : sourceName || '本地视频',
          );
        }
      }
      if (framesRef.current.length > MAX_FRAMES) {
        framesRef.current.splice(0, framesRef.current.length - MAX_FRAMES);
      }
      setFrameCount(framesRef.current.length);
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
        setError(
          playError instanceof Error
            ? `摄像头已连接，但画面播放失败：${playError.message}`
            : '摄像头已连接，但画面播放失败。',
        );
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
    const capturedStream = capturableVideo.captureStream?.()
      ?? capturableVideo.mozCaptureStream?.()
      ?? null;
    fileCaptureStreamRef.current = capturedStream;
    const hasAudio = capturedStream
      ? attachSourceAudio('file', `${file.name} 原音轨`, capturedStream)
      : false;
    setSource('file');
    setSourceName(file.name);
    setIsPlaying(!video.paused);
    if (!hasAudio) setError('视频已打开，但浏览器没有提供可读取的原音轨。');
  };

  const removeScreen = useCallback((screenId: string) => {
    stopSourceAudio(screenId);
    const stream = screenStreamsRef.current.get(screenId);
    stream?.getTracks().forEach((track) => track.stop());
    screenStreamsRef.current.delete(screenId);
    screenVideoRefs.current.delete(screenId);
    framesRef.current = framesRef.current.filter((frame) => frame.source_id !== screenId);
    memoryFramesRef.current.delete(screenId);
    setFrameCount(framesRef.current.length);
    setScreens((current) => {
      const next = current.filter((screen) => screen.id !== screenId);
      if (next.length === 0) {
        setSource(null);
        setSourceName('');
        setIsPlaying(false);
      } else {
        setSourceName(`${next.length} 个屏幕`);
      }
      return next;
    });
  }, [stopSourceAudio]);

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
        video: { frameRate: { ideal: 2, max: 5 } },
        audio: true,
      });
      const track = stream.getVideoTracks()[0];
      if (!track) {
        stream.getTracks().forEach((item) => item.stop());
        throw new Error('没有获得屏幕画面。');
      }

      if (source !== 'screen') {
        releaseSource();
        releaseScreens();
      }

      const screenId = typeof crypto.randomUUID === 'function'
        ? crypto.randomUUID()
        : `screen-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      const name = track.label.trim() || `屏幕 ${screens.length + 1}`;
      screenStreamsRef.current.set(screenId, stream);
      setScreens((current) => {
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

  const runOmniRequest = async (
    prompt: string,
    questionAudioDataUrl?: string,
    recordedAssistantPlayback = false,
  ) => {
    if ((!prompt && !questionAudioDataUrl) || isAskingRef.current) return;
    if (framesRef.current.length === 0) {
      setError('请先打开视频并等待画面开始播放。');
      return;
    }

    setIsAsking(true);
    isAskingRef.current = true;
    if (!recordedAssistantPlayback) stopSpeechPlayback();
    streamedAnswerRef.current = '';
    beginConversationTurn(prompt || '🎙️ 语音提问');
    setError('');
    setToolStatus('');
    setElapsedMs(0);
    const startedAt = performance.now();
    requestStartedAtRef.current = startedAt;
    requestAbortReasonRef.current = null;
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
      const audioInputs = questionAudioDataUrl ? [] : await snapshotSourceAudio();
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
          ...(questionAudioDataUrl && assistantSpeechTextRef.current
            ? { assistant_speech_text: assistantSpeechTextRef.current }
            : {}),
        },
        {
          timeoutMs: REQUEST_TIMEOUT_MS + 1_000,
          signal: controller.signal,
        },
      );
      // Full duplex: immediately reopen the microphone while the model is
      // thinking and speaking. A new utterance interrupts this request.
      if (voiceConversationRef.current) {
        window.setTimeout(() => void startVoiceListeningRef.current(), 120);
      }
      const result = await request;
      // The model request is complete. Audio playback may be longer than the
      // request timeout and must not be mistaken for a hung model call.
      window.clearTimeout(timeoutId);
      if (result.ignored) {
        removeActiveTurn();
      } else {
        if (result.transcript) replaceActiveQuestion(result.transcript);
        const finalAnswer = result.answer?.trim() || '模型没有返回文本。';
        assistantSpeechTextRef.current = finalAnswer;
        replaceActiveAnswer(finalAnswer);
        if (!streamedAnswerRef.current) queueSpeechText(finalAnswer);
        queueSpeechText('', true);
      }
      setLatencyMs(result.latency_ms ?? Math.round(performance.now() - startedAt));
      setFirstTokenMs(result.first_token_ms ?? null);
      if (result.model) setModel(result.model);
      setRequestCount((count) => count + 1);
      if (!questionAudioDataUrl) setQuestion('');
    } catch (requestError) {
      const abortReason = requestAbortReasonRef.current;
      if (abortReason === 'timeout') {
        setError('超过 45 秒仍未收到结果，已停止等待，请重试。');
      } else if (abortReason === 'manual') {
        setError('已取消本次问答。');
      } else if (abortReason !== 'source') {
        const code = (requestError as { code?: unknown })?.code;
        setError(
          code === 'WS_DISCONNECTED' || code === 'WS_CLOSED'
            ? '视频连接已断开，请重新提问。'
            : requestError instanceof Error
              ? requestError.message
              : 'Jiuwen 音视频问答接口调用失败。',
        );
      }
    } finally {
      window.clearTimeout(timeoutId);
      if (requestAbortRef.current === controller) {
        requestAbortRef.current = null;
      }
      activeRequestIdRef.current = null;
      activeTurnIdRef.current = null;
      requestAbortReasonRef.current = null;
      isAskingRef.current = false;
      setIsAsking(false);
      setToolStatus('');
      if (voiceConversationRef.current) {
        window.setTimeout(() => void startVoiceListeningRef.current(), 120);
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
      const mimeType = AUDIO_MIME_TYPES.find((type) => MediaRecorder.isTypeSupported(type));
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
      let heardSpeech = false;
      let consecutiveSpeechFrames = 0;
      let voicedFrames = 0;
      let noiseFloor = 0.006;
      let assistantPlayedDuringRecording = false;
      const calibrationStartedAt = performance.now();
      let lastSpeechAt = performance.now();
      mediaRecorderRef.current = recorder;
      recorder.ondataavailable = (recordedEvent) => {
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
          .then((audioDataUrl) => runOmniRequest(
            '',
            audioDataUrl,
            assistantPlayedDuringRecording,
          ))
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
        if (outputIsPlaying) assistantPlayedDuringRecording = true;
        const speechThreshold = Math.max(
          outputIsPlaying ? 0.032 : 0.018,
          noiseFloor * (outputIsPlaying ? 3.2 : 2.2),
        );
        if (rms >= speechThreshold) {
          consecutiveSpeechFrames += 1;
          voicedFrames += 1;
          const requiredFrames = outputIsPlaying ? 6 : 2;
          if (!heardSpeech && consecutiveSpeechFrames >= requiredFrames) {
            heardSpeech = true;
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
      setError(
        microphoneError instanceof Error
          ? microphoneError.message
          : '无法打开麦克风。',
      );
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
          <span className="video-live__brand-icon"><Video aria-hidden /></span>
          <div>
            <h1>Jiuwen Omni Live</h1>
            <p>Qwen3-Omni 多屏音视频问答</p>
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
                {screens.map((screen) => (
                  <div className="video-live__screen-tile" key={screen.id}>
                    <video
                      ref={(node) => {
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
                    <button
                      className="video-live__screen-close"
                      type="button"
                      onClick={() => removeScreen(screen.id)}
                      aria-label={`关闭${screen.name}`}
                    >
                      <X aria-hidden />
                    </button>
                  </div>
                ))}
              </div>
            )}
            {!source && (
              <div className="video-live__empty">
                <span className="video-live__empty-icon"><Video aria-hidden /></span>
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
            {liveSubtitle && (
              <div className="video-live__subtitle">{liveSubtitle}</div>
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
              <input type="file" accept="video/*" onChange={(event) => void openFile(event)} />
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
                onChange={(event) => setTargetLanguage(event.target.value)}
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
                onClick={() => void toggleTranslation().catch((taskError) => {
                  setError(taskError instanceof Error ? taskError.message : '实时翻译启动失败');
                })}
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
              滚动窗口：{frameCount}/{MAX_FRAMES} 帧
              {source === 'screen' ? ` · ${screens.length}/${MAX_SCREENS} 屏` : ''}
              {` · 音频 ${audioSourceCount} 路`}
            </span>
          </div>
        </div>

        <div className="video-live__output-card">
          <div className="video-live__output-head">
            <div>
              <span className="video-live__eyebrow">VLM OUTPUT</span>
              <strong>{model}</strong>
            </div>
            <div className="video-live__metrics">
              <span>First <b>{firstTokenMs === null ? '—' : `${firstTokenMs} ms`}</b></span>
              <span>Total <b>{latencyMs === null ? '—' : `${latencyMs} ms`}</b></span>
              <span>Count <b>{requestCount}</b></span>
              <span>字幕 <b>{translationCount}</b></span>
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
                        <MarkdownRenderer
                          content={turn.answer}
                          className="chat-markdown"
                        />
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
              <MarkdownRenderer
                content={answer}
                className="video-live__answer-content chat-markdown"
              />
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

          <form className="video-live__composer" onSubmit={(event) => void askVideo(event)}>
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
              onChange={(event) => setQuestion(event.target.value)}
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

      <canvas ref={canvasRef} className="video-live__capture-canvas" aria-hidden />
    </section>
  );
}
