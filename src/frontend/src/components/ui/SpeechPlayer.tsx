import { useCallback, useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, ApiError } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { Icon } from './Icon';
import { toast } from './Toast';

type PlayerState = 'idle' | 'loading' | 'playing' | 'paused';

let stopActivePlayer: (() => void) | null = null;

function formatTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
  const whole = Math.floor(seconds);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`;
}

function speechError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 413) return tr('内容太长，请缩短后再播放', 'This content is too long to read aloud');
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

/** 按需生成并播放语音。Blob 只在第一次点击时请求，同一控件内可反复播放。 */
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
  const [state, setState] = useState<PlayerState>('idle');
  const [elapsed, setElapsed] = useState(0);
  const [duration, setDuration] = useState(0);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const urlRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const stopRef = useRef<(() => void) | null>(null);

  const release = useCallback(() => {
    if (stopActivePlayer === stopRef.current) stopActivePlayer = null;
    abortRef.current?.abort();
    abortRef.current = null;
    const audio = audioRef.current;
    if (audio) {
      audio.pause();
      audio.removeAttribute('src');
      audio.load();
    }
    audioRef.current = null;
    if (urlRef.current) URL.revokeObjectURL(urlRef.current);
    urlRef.current = null;
    setElapsed(0);
    setDuration(0);
    setState('idle');
  }, []);

  useEffect(() => release, [release]);
  useEffect(() => {
    release();
  }, [text, context, release]);

  const stop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    const audio = audioRef.current;
    if (audio) {
      audio.pause();
      audio.currentTime = 0;
    }
    setElapsed(0);
    setState('idle');
  }, []);
  stopRef.current = stop;

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
      const blob = await api.synthesizeSpeech(cleanText, context, controller.signal);
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      urlRef.current = url;
      audioRef.current = audio;
      audio.addEventListener('loadedmetadata', () => setDuration(audio.duration));
      audio.addEventListener('timeupdate', () => setElapsed(audio.currentTime));
      audio.addEventListener('ended', () => {
        setElapsed(0);
        setState('idle');
      });
      await audio.play();
      setState('playing');
    } catch (error) {
      if (!controller.signal.aborted) {
        release();
        toast(`${tr('播放失败', 'Playback failed')}：${speechError(error)}`, 'error');
      }
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
    }
  }, [context, release, state, stop, text]);

  const settings = settingsQuery.data;
  if (!settings || !settings.available || !settings.enabled || !text.trim()) return null;

  const active = state === 'playing' || state === 'paused';
  const title = state === 'loading'
    ? tr('取消生成语音', 'Cancel speech generation')
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
        <span>{state === 'loading' ? tr('正在生成语音…', 'Generating speech…') : title}</span>
      </button>
      <div className="speech-player-track" aria-hidden="true">
        <span style={{ width: `${progress}%` }} />
      </div>
      {active && <span className="speech-player-time">{formatTime(elapsed)} / {formatTime(duration)}</span>}
    </div>
  );
}
