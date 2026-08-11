export function selectTemporalFrames<T>(frames: readonly T[], limit: number): T[] {
  if (limit <= 0 || frames.length === 0) return [];
  if (frames.length <= limit) return [...frames];
  if (limit === 1) return [frames[frames.length - 1]];

  const selected: T[] = [];
  const lastIndex = frames.length - 1;
  for (let position = 0; position < limit; position += 1) {
    const index = Math.round((position * lastIndex) / (limit - 1));
    selected.push(frames[index]);
  }
  return selected;
}

export function selectMonitorFrames<T>(frames: readonly T[], limit: number, preferLatest: boolean): T[] {
  if (!preferLatest) return selectTemporalFrames(frames, limit);
  if (limit <= 0 || frames.length === 0) return [];
  return selectTemporalFrames(frames, limit);
}
