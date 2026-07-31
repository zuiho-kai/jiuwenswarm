import { ChangeEvent, FormEvent, useCallback, useEffect, useRef, useState } from 'react';
import { Camera, FileVideo, LoaderCircle, Mic, Send, Square, Video, X } from 'lucide-react';
import { webClient, webRequest } from '../../services/webClient';
import './VideoLivePanel.css';

type VideoSource = 'camera' | 'file' | null;
type AbortReason = 'manual' | 'timeout' | 'source';

interface CapturedFrame {
  data_url: string;
  captured_at: number;
}

interface VideoAskResponse {
  answer?: string;
  latency_ms?: number;
  first_token_ms?: number;
  model?: string;
}

const FRAME_INTERVAL_MS = 500;
const MAX_FRAMES = 6;
const MAX_FRAME_WIDTH = 768;
const REQUEST_TIMEOUT_MS = 15_000;
const RECORDING_LIMIT_MS = 15_000;

function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(reader.error || new Error('读取录音失败'));
    reader.readAsDataURL(blob);
  });
}

export function VideoLivePanel() {
  const videoRef = useRef<HTMLVideoElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const cameraStreamRef = useRef<MediaStream | null>(null);
  const fileUrlRef = useRef<string | null>(null);
  const framesRef = useRef<CapturedFrame[]>([]);
  const requestAbortRef = useRef<AbortController | null>(null);
  const requestStartedAtRef = useRef(0);
  const requestAbortReasonRef = useRef<AbortReason | null>(null);
  const activeRequestIdRef = useRef<string | null>(null);
  const isAskingRef = useRef(false);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const microphoneStreamRef = useRef<MediaStream | null>(null);
  const audioChunksRef = useRef<Blob[]>([]);
  const recordingTimeoutRef = useRef<number | null>(null);

  const [source, setSource] = useState<VideoSource>(null);
  const [sourceName, setSourceName] = useState('');
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

  const releaseSource = useCallback(() => {
    cameraStreamRef.current?.getTracks().forEach((track) => track.stop());
    cameraStreamRef.current = null;
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
    setFrameCount(0);
    setIsPlaying(false);
  }, []);

  const abortQuestion = useCallback((reason: AbortReason) => {
    requestAbortReasonRef.current = reason;
    requestAbortRef.current?.abort();
    requestAbortRef.current = null;
    activeRequestIdRef.current = null;
  }, []);

  const releaseMicrophone = useCallback(() => {
    if (recordingTimeoutRef.current !== null) {
      window.clearTimeout(recordingTimeoutRef.current);
      recordingTimeoutRef.current = null;
    }
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
    if (isAskingRef.current) abortQuestion('source');
    cancelRecording();
    releaseSource();
    setSource(null);
    setSourceName('');
    setAnswer('');
    setError('');
  };

  useEffect(() => () => {
    requestAbortRef.current?.abort();
    cancelRecording(false);
    releaseSource();
  }, [cancelRecording, releaseSource]);

  useEffect(() => {
    const offStarted = webClient.on<{ request_id?: unknown }>('video.started', (event) => {
      const requestId = event.payload.request_id;
      if (isAskingRef.current && typeof requestId === 'string') {
        activeRequestIdRef.current = requestId;
      }
    });
    const offDelta = webClient.on<{ request_id?: unknown; content?: unknown }>('video.delta', (event) => {
      if (event.payload.request_id !== activeRequestIdRef.current) return;
      const content = event.payload.content;
      if (typeof content === 'string' && content) {
        setAnswer((current) => current + content);
      }
    });
    return () => {
      offStarted();
      offDelta();
    };
  }, []);

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

  useEffect(() => {
    if (!isPlaying) return;

    const capture = () => {
      const video = videoRef.current;
      const canvas = canvasRef.current;
      if (!video || !canvas || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return;
      if (!video.videoWidth || !video.videoHeight) return;

      const scale = Math.min(1, MAX_FRAME_WIDTH / video.videoWidth);
      canvas.width = Math.round(video.videoWidth * scale);
      canvas.height = Math.round(video.videoHeight * scale);
      const context = canvas.getContext('2d');
      if (!context) return;

      context.drawImage(video, 0, 0, canvas.width, canvas.height);
      framesRef.current.push({
        data_url: canvas.toDataURL('image/jpeg', 0.72),
        captured_at: Date.now(),
      });
      if (framesRef.current.length > MAX_FRAMES) {
        framesRef.current.splice(0, framesRef.current.length - MAX_FRAMES);
      }
      setFrameCount(framesRef.current.length);
    };

    capture();
    const timer = window.setInterval(capture, FRAME_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [isPlaying]);

  const startCamera = async () => {
    cancelRecording();
    releaseSource();
    setSource(null);
    setSourceName('');
    setError('');
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 1280 }, height: { ideal: 720 } },
        audio: false,
      });
      cameraStreamRef.current = stream;
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

  const openFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;

    cancelRecording();
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
    setSource('file');
    setSourceName(file.name);
    setIsPlaying(!video.paused);
  };

  const runOmniRequest = async (prompt: string, audioDataUrl?: string) => {
    if ((!prompt && !audioDataUrl) || isAskingRef.current) return;
    if (framesRef.current.length === 0) {
      setError('请先打开视频并等待画面开始播放。');
      return;
    }

    setIsAsking(true);
    isAskingRef.current = true;
    setAnswer('');
    setError('');
    setElapsedMs(0);
    const startedAt = performance.now();
    requestStartedAtRef.current = startedAt;
    requestAbortReasonRef.current = null;
    activeRequestIdRef.current = null;
    const controller = new AbortController();
    requestAbortRef.current = controller;
    const timeoutId = window.setTimeout(() => {
      if (requestAbortRef.current === controller) {
        abortQuestion('timeout');
      }
    }, REQUEST_TIMEOUT_MS);
    try {
      const result = await webRequest<VideoAskResponse>(
        'video.ask',
        {
          question: prompt,
          source,
          frames: framesRef.current.slice(),
          ...(audioDataUrl ? { audio_data_url: audioDataUrl } : {}),
        },
        {
          timeoutMs: REQUEST_TIMEOUT_MS + 1_000,
          signal: controller.signal,
        },
      );
      setAnswer(result.answer?.trim() || '模型没有返回文本。');
      setLatencyMs(result.latency_ms ?? Math.round(performance.now() - startedAt));
      setFirstTokenMs(result.first_token_ms ?? null);
      if (result.model) setModel(result.model);
      setRequestCount((count) => count + 1);
      if (!audioDataUrl) setQuestion('');
    } catch (requestError) {
      const abortReason = requestAbortReasonRef.current;
      if (abortReason === 'timeout') {
        setError('超过 15 秒仍未收到结果，已停止等待，请重试。');
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
      requestAbortReasonRef.current = null;
      isAskingRef.current = false;
      setIsAsking(false);
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
    if (isAskingRef.current || isRecording) return;
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
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
      microphoneStreamRef.current = stream;
      audioChunksRef.current = [];
      const preferredMimeTypes = [
        'audio/webm;codecs=opus',
        'audio/webm',
        'audio/ogg;codecs=opus',
      ];
      const mimeType = preferredMimeTypes.find((type) => MediaRecorder.isTypeSupported(type));
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
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
        const audioBlob = new Blob(chunks, { type: recordedMimeType });
        if (audioBlob.size === 0) {
          setError('没有录到声音，请重试。');
          return;
        }
        void blobToDataUrl(audioBlob)
          .then((audioDataUrl) => runOmniRequest(question.trim(), audioDataUrl))
          .catch((recordingError) => {
            setError(recordingError instanceof Error ? recordingError.message : '读取录音失败。');
          });
      };
      recorder.start(250);
      setIsRecording(true);
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

  return (
    <section className="video-live">
      <header className="video-live__header">
        <div className="video-live__brand">
          <span className="video-live__brand-icon"><Video aria-hidden /></span>
          <div>
            <h1>Jiuwen Omni Live</h1>
            <p>Qwen3-Omni 实时音视频问答</p>
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
              controls={source === 'file'}
              playsInline
              onPlay={() => setIsPlaying(true)}
              onPause={() => setIsPlaying(false)}
              onEnded={() => setIsPlaying(false)}
            />
            {!source && (
              <div className="video-live__empty">
                <span className="video-live__empty-icon"><Video aria-hidden /></span>
                <strong>打开一个实时画面</strong>
                <p>使用摄像头，或选择本地视频文件</p>
              </div>
            )}
            {source && (
              <div className="video-live__source-chip">
                <span className={isPlaying ? 'is-live' : ''} />
                {sourceName}
              </div>
            )}
            {source && (
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
            {source && (
              <button type="button" className="video-live__source-button video-live__source-button--stop" onClick={closeSource}>
                <X aria-hidden />
                {source === 'camera' ? '停止摄像头' : '关闭视频'}
              </button>
            )}
            <span className="video-live__frame-count">滚动窗口：{frameCount}/{MAX_FRAMES} 帧</span>
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
            </div>
          </div>

          <div className="video-live__prompt-banner">
            <span>当前模式</span>
            {isRecording ? '正在录音，再点麦克风结束并提问' : '最近 3 秒画面 + 语音/文字问答'}
          </div>

          <div className="video-live__answer">
            {answer ? (
              <p>{answer}</p>
            ) : isAsking ? (
              <div className="video-live__answer-empty">
                <LoaderCircle className="is-spinning" aria-hidden />
                <strong>正在分析最近 3 秒画面</strong>
                <span>{(elapsedMs / 1000).toFixed(1)} 秒 · 最长等待 15 秒</span>
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
              className={`video-live__mic${isRecording ? ' is-recording' : ''}`}
              disabled={isAsking}
              onClick={isRecording ? stopRecording : () => void startRecording()}
              aria-label={isRecording ? '结束录音并提问' : '语音提问'}
              title={isRecording ? '结束录音并提问' : '语音提问'}
            >
              {isRecording ? <Square aria-hidden /> : <Mic aria-hidden />}
            </button>
            <input
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder={isRecording ? '正在录音……' : '向 Qwen3-Omni 询问当前视频……'}
              disabled={isAsking || isRecording}
            />
            <button
              type={isAsking ? 'button' : 'submit'}
              disabled={!isAsking && (!question.trim() || isRecording)}
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
