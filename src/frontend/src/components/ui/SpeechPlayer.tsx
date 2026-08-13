import { useCallback, useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, ApiError } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { splitSpeechText } from '../../lib/speech';
import { Icon } from './Icon';
import { toast } from './Toast';

type PlayerState = 'idle' | 'loading' | 'playing' | 'paused';
type ChunkLoadResult = { ok: true; blob: Blob } | { ok: false; error: unknown };

const PLAYBACK_CHUNK_CHARS = 500;

let stopActivePlayer: (() => void) | null = null;

function formatTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
  const whole = Math.floor(seconds);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`;
}

function speechError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 413) return tr('语音分段失败，请重试', 'Speech segmentation failed; try again');
    if (error.status === 503) return tr('语音服务暂时不可用', 'Speech service is temporarily unavailable');
  }
  return error instanceof Error ? error.message : String(error);
}

export interface SpeechPlayerProps {
  text: string;
  context?: 'assistant' | 'digest';
  compact?: boolean;
  className?: string;
}

/** 按需生成并播放语音；长文本按服务端限制分段并自动连续播放。 */
export function SpeechPlayer({
  text,
  context = 'assistant',
  compact = true,
  className = '',
}: SpeechPlayerProps) {
  const settingsQuery = useQuery({
    queryKey: ['tts-settings'],
    queryFn: () => api.getTtsSettings(),
    retry: false,
    staleTime: 60_000,
  });
  const settings = settingsQuery.data;
  const maxChars = settings?.max_chars ?? 20_000;
  const [state, setState] = useState<PlayerState>('idle');
  const [elapsed, setElapsed] = useState(0);
  const [duration, setDuration] = useState(0);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const urlRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const stopRef = useRef<(() => void) | null>(null);
  const chunksRef = useRef<string[]>([]);
  const chunkLoadsRef = useRef(new Map<number, Promise<ChunkLoadResult>>());
  const startChunkRef = useRef<(index: number, controller: AbortController) => Promise<void>>(
    async () => undefined,
  );
  const [chunkIndex, setChunkIndex] = useState(0);

  const clearAudio = useCallback(() => {
    const audio = audioRef.current;
    if (audio) {
      audio.pause();
      audio.removeAttribute('src');
      audio.load();
    }
    audioRef.current = null;
    if (urlRef.current) URL.revokeObjectURL(urlRef.current);
    urlRef.current = null;
  }, []);

  const release = useCallback(() => {
    if (stopActivePlayer === stopRef.current) stopActivePlayer = null;
    abortRef.current?.abort();
    abortRef.current = null;
    clearAudio();
    chunksRef.current = [];
    chunkLoadsRef.current.clear();
    setChunkIndex(0);
    setElapsed(0);
    setDuration(0);
    setState('idle');
  }, [clearAudio]);

  useEffect(() => release, [release]);
  useEffect(() => {
    release();
  }, [text, context, release]);

  const stop = release;
  stopRef.current = stop;

  const loadChunk = useCallback((index: number, controller: AbortController) => {
    const existing = chunkLoadsRef.current.get(index);
    if (existing) return existing;
    const chunk = chunksRef.current[index];
    const request = chunk
      ? api.synthesizeSpeech(chunk, context, controller.signal).then<ChunkLoadResult, ChunkLoadResult>(
          (blob) => ({ ok: true, blob }),
          (error: unknown) => ({ ok: false, error }),
        )
      : Promise.resolve<ChunkLoadResult>({ ok: false, error: new Error('TTS_EMPTY_CHUNK') });
    chunkLoadsRef.current.set(index, request);
    return request;
  }, [context]);

  const startChunk = useCallback(async (index: number, controller: AbortController) => {
    const chunk = chunksRef.current[index];
    if (!chunk || controller.signal.aborted) return;
    setChunkIndex(index);
    setElapsed(0);
    setDuration(0);
    setState('loading');
    try {
      const result = await loadChunk(index, controller);
      chunkLoadsRef.current.delete(index);
      if (!result.ok) throw result.error;
      if (controller.signal.aborted) return;
      clearAudio();
      const url = URL.createObjectURL(result.blob);
      const audio = new Audio(url);
      urlRef.current = url;
      audioRef.current = audio;
      audio.addEventListener('loadedmetadata', () => setDuration(audio.duration));
      audio.addEventListener('timeupdate', () => setElapsed(audio.currentTime));
      audio.addEventListener('ended', () => {
        if (controller.signal.aborted) return;
        clearAudio();
        const next = index + 1;
        if (next < chunksRef.current.length) {
          void startChunkRef.current(next, controller);
        } else {
          release();
        }
      });
      await audio.play();
      if (!controller.signal.aborted) {
        setState('playing');
        if (index + 1 < chunksRef.current.length) {
          // CosyVoice runs slightly faster than real time. Preparing one part
          // while the current part plays normally removes the segment gap.
          void loadChunk(index + 1, controller);
        }
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        release();
        toast(`${tr('播放失败', 'Playback failed')}：${speechError(error)}`, 'error');
      }
    }
  }, [clearAudio, loadChunk, release]);
  startChunkRef.current = startChunk;

  const play = useCallback(async () => {
    const cleanText = text.trim();
    if (!cleanText) return;

    if (state === 'loading') {
      release();
      return;
    }
    if (state === 'playing') {
      audioRef.current?.pause();
      setState('paused');
      return;
    }

    if (audioRef.current) {
      if (stopActivePlayer !== stop) stopActivePlayer?.();
      stopActivePlayer = stop;
      try {
        await audioRef.current.play();
        setState('playing');
      } catch (error) {
        toast(`${tr('播放失败', 'Playback failed')}：${speechError(error)}`, 'error');
      }
      return;
    }

    const controller = new AbortController();
    abortRef.current = controller;
    if (stopActivePlayer !== stop) stopActivePlayer?.();
    stopActivePlayer = stop;
    setState('loading');
    try {
      // Leave a small normalization margin because the API replaces omitted
      // Markdown code blocks with a short spoken marker.
      const providerLimit = Math.max(1, maxChars - 64);
      chunksRef.current = splitSpeechText(
        cleanText,
        Math.min(PLAYBACK_CHUNK_CHARS, providerLimit),
      );
      await startChunk(0, controller);
    } catch (error) {
      if (!controller.signal.aborted) {
        release();
        toast(`${tr('播放失败', 'Playback failed')}：${speechError(error)}`, 'error');
      }
    }
  }, [maxChars, release, startChunk, state, stop, text]);

  if (!settings || !settings.available || !settings.enabled || !text.trim()) return null;

  const active = state === 'playing' || state === 'paused';
  const chunkCount = chunksRef.current.length;
  const chunkProgress = chunkCount > 1 ? `${chunkIndex + 1}/${chunkCount}` : '';
  const title = state === 'loading'
    ? `${tr('取消生成语音', 'Cancel speech generation')}${chunkProgress ? ` · ${chunkProgress}` : ''}`
    : state === 'playing'
      ? tr('暂停', 'Pause')
      : state === 'paused'
        ? tr('继续播放', 'Resume')
        : tr('朗读', 'Read aloud');

  if (compact) {
    return (
      <button
        type="button"
        className={`icon-btn speech-player-compact ${active ? 'active' : ''} ${className}`.trim()}
        title={title}
        aria-label={title}
        onClick={() => void play()}
        style={{ width: 24, height: 24 }}
      >
        <Icon
          name={state === 'playing' ? 'pause' : state === 'loading' ? 'refresh' : 'play'}
          size={12}
          style={state === 'loading' ? { animation: 'spin 1s linear infinite' } : undefined}
        />
      </button>
    );
  }

  const progress = duration > 0 ? Math.min(100, (elapsed / duration) * 100) : 0;
  return (
    <div className={`speech-player ${active ? 'active' : ''} ${className}`.trim()}>
      <button type="button" className="speech-player-button" onClick={() => void play()} aria-label={title}>
        <span className="speech-player-icon">
          <Icon
            name={state === 'playing' ? 'pause' : state === 'loading' ? 'refresh' : 'play'}
            size={14}
            style={state === 'loading' ? { animation: 'spin 1s linear infinite' } : undefined}
          />
        </span>
        <span>
          {state === 'loading'
            ? `${tr('正在生成语音…', 'Generating speech…')}${chunkProgress ? ` ${chunkProgress}` : ''}`
            : title}
        </span>
      </button>
      <div className="speech-player-track" aria-hidden="true">
        <span style={{ width: `${progress}%` }} />
      </div>
      {active && (
        <span className="speech-player-time">
          {chunkProgress ? `${chunkProgress} · ` : ''}{formatTime(elapsed)} / {formatTime(duration)}
        </span>
      )}
    </div>
  );
}
