interface AudioBufferLike {
  numberOfChannels: number;
  length: number;
  sampleRate: number;
  getChannelData(channel: number): Float32Array;
}

const TARGET_SAMPLE_RATE = 16_000;

function writeAscii(view: DataView, offset: number, value: string): void {
  for (let index = 0; index < value.length; index += 1) {
    view.setUint8(offset + index, value.charCodeAt(index));
  }
}

export function encodePcm16MonoWav(audio: AudioBufferLike): Blob {
  if (audio.numberOfChannels < 1 || audio.length < 1 || audio.sampleRate < 1) {
    throw new Error('录音数据无效。');
  }

  const sampleRate = Math.min(TARGET_SAMPLE_RATE, audio.sampleRate);
  const outputLength = Math.max(1, Math.floor((audio.length * sampleRate) / audio.sampleRate));
  const bytesPerSample = 2;
  const dataSize = outputLength * bytesPerSample;
  const buffer = new ArrayBuffer(44 + dataSize);
  const view = new DataView(buffer);

  writeAscii(view, 0, 'RIFF');
  view.setUint32(4, 36 + dataSize, true);
  writeAscii(view, 8, 'WAVE');
  writeAscii(view, 12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * bytesPerSample, true);
  view.setUint16(32, bytesPerSample, true);
  view.setUint16(34, 16, true);
  writeAscii(view, 36, 'data');
  view.setUint32(40, dataSize, true);

  const channels = Array.from({ length: audio.numberOfChannels }, (_, channel) => audio.getChannelData(channel));
  const sourceStep = audio.sampleRate / sampleRate;
  for (let outputIndex = 0; outputIndex < outputLength; outputIndex += 1) {
    const sourcePosition = outputIndex * sourceStep;
    const lowerIndex = Math.min(Math.floor(sourcePosition), audio.length - 1);
    const upperIndex = Math.min(lowerIndex + 1, audio.length - 1);
    const weight = sourcePosition - lowerIndex;
    let sample = 0;
    for (const channel of channels) {
      sample += channel[lowerIndex] + (channel[upperIndex] - channel[lowerIndex]) * weight;
    }
    sample = Math.max(-1, Math.min(1, sample / channels.length));
    view.setInt16(44 + outputIndex * bytesPerSample, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
  }

  return new Blob([buffer], { type: 'audio/wav' });
}

function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(reader.error || new Error('读取录音失败'));
    reader.readAsDataURL(blob);
  });
}

export async function transcodeAudioBlobToWavDataUrl(blob: Blob): Promise<string> {
  if (typeof AudioContext === 'undefined') {
    throw new Error('当前运行环境不支持音频转换。');
  }

  const context = new AudioContext();
  try {
    const decoded = await context.decodeAudioData(await blob.arrayBuffer());
    return await blobToDataUrl(encodePcm16MonoWav(decoded));
  } finally {
    await context.close();
  }
}
