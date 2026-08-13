import { useCallback, useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, ApiError } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { pcm16LeToFloat32, splitSpeechText } from '../../lib/speech';
import { Icon } from './Icon';
import { toast } from './Toast';

type PlayerState = 'idle' | 'connecting' | 'playing' | 'paused';

// Keep one GPU reservation short. Long 500-character requests can monopolize
// the single CosyVoice runtime for tens of seconds and make another click wait.
const PLAYBACK_CHUNK_CHARS = 80;
const START_BUFFER_SECONDS = 0.04;
const STREAM_TIMEOUT_MS = 15_000;

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
  if (error instanceof Error && error.message === 'TTS_STREAM_TIMEOUT') {
    return tr('语音响应超时，请重试', 'Speech response timed out; try again');
  }
  return error instanceof Error ? error.message : String(error);
}

async function withStreamTimeout<T>(promise: Promise<T>): Promise<T> {
  let timer: number | undefined;
  try {
    return await Promise.race([
      promise,
      new Promise<T>((_resolve, reject) => {
        timer = window.setTimeout(
          () => reject(new Error('TTS_STREAM_TIMEOUT')),
          STREAM_TIMEOUT_MS,
        );
      }),
    ]);
  } finally {
    if (timer !== undefined) window.clearTimeout(timer);
  }
}

function audioContextConstructor(): typeof AudioContext | null {
  if (typeof window === 'undefined') return null;
  return window.AudioContext
    ?? (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
    ?? null;
}

export interface SpeechPlayerProps {
  text: string;
  context?: 'assistant' | 'digest';
  compact?: boolean;
  className?: string;
}

/** Stream provider PCM directly into Web Audio while later text segments generate. */
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
  const [chunkIndex, setChunkIndex] = useState(0);
  const [chunkCount, setChunkCount] = useState(0);
  const contextRef = useRef<AudioContext | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const sourcesRef = useRef(new Set<AudioBufferSourceNode>());
  const nextStartRef = useRef(0);
  const playbackStartRef = useRef(0);
  const streamDoneRef = useRef(false);
  const stopRef = useRef<(() => void) | null>(null);

  const release = useCallback(() => {
    if (stopActivePlayer === stopRef.current) stopActivePlayer = null;
    abortRef.current?.abort();
    abortRef.current = null;
    const sources = [...sourcesRef.current];
    sourcesRef.current.clear();
    for (const source of sources) {
      source.onended = null;
      try {
        source.stop();
      } catch {
        /* already ended */
      }
    }
    const audioContext = contextRef.current;
    contextRef.current = null;
    if (audioContext && audioContext.state !== 'closed') void audioContext.close();
    nextStartRef.current = 0;
    playbackStartRef.current = 0;
    streamDoneRef.current = false;
    setChunkIndex(0);
    setChunkCount(0);
    setElapsed(0);
    setState('idle');
  }, []);
  stopRef.current = release;

  useEffect(() => release, [release]);
  useEffect(() => {
    release();
  }, [text, context, release]);

  useEffect(() => {
    if (state !== 'playing' && state !== 'paused') return undefined;
    const timer = window.setInterval(() => {
      const audioContext = contextRef.current;
      const start = playbackStartRef.current;
      if (audioContext && start > 0) setElapsed(Math.max(0, audioContext.currentTime - start));
    }, 200);
    return () => window.clearInterval(timer);
  }, [state]);

  const runPlayback = useCallback(async (
    chunks: string[],
    audioContext: AudioContext,
    controller: AbortController,
  ) => {
    let scheduledAudio = false;
    try {
      for (let index = 0; index < chunks.length; index += 1) {
        if (controller.signal.aborted) return;
        setChunkIndex(index);
        const chunk = chunks[index];
        if (!chunk) continue;
        const stream = await withStreamTimeout(
          api.streamSpeech(chunk, context, controller.signal),
        );
        const reader = stream.body.getReader();
        let trailingByte: number | null = null;
        try {
          while (!controller.signal.aborted) {
            const { done, value } = await withStreamTimeout(reader.read());
            if (done) break;
            const decoded = pcm16LeToFloat32(value, trailingByte);
            trailingByte = decoded.trailingByte;
            if (decoded.samples.length === 0) continue;

            const buffer = audioContext.createBuffer(
              1,
              decoded.samples.length,
              stream.sampleRate,
            );
            buffer.getChannelData(0).set(decoded.samples);
            const source = audioContext.createBufferSource();
            source.buffer = buffer;
            source.playbackRate.value = stream.playbackRate;
            source.connect(audioContext.destination);

            const startAt = scheduledAudio && nextStartRef.current >= audioContext.currentTime
              ? nextStartRef.current
              : audioContext.currentTime + START_BUFFER_SECONDS;
            if (!scheduledAudio) playbackStartRef.current = startAt;
            nextStartRef.current = startAt + buffer.duration / stream.playbackRate;
            sourcesRef.current.add(source);
            source.onended = () => {
              sourcesRef.current.delete(source);
              if (streamDoneRef.current && sourcesRef.current.size === 0) release();
            };
            source.start(startAt);
            if (!scheduledAudio) {
              scheduledAudio = true;
              setState('playing');
            }
          }
        } finally {
          if (controller.signal.aborted) {
            try {
              await reader.cancel();
            } catch {
              /* fetch abort already closed the body */
            }
          }
          reader.releaseLock();
        }
        if (trailingByte !== null) throw new Error('TTS_INVALID_PCM_STREAM');
      }
      if (!scheduledAudio) throw new Error('TTS_EMPTY_STREAM');
      streamDoneRef.current = true;
      if (sourcesRef.current.size === 0) release();
    } catch (error) {
      if (!controller.signal.aborted) {
        release();
        toast(`${tr('播放失败', 'Playback failed')}：${speechError(error)}`, 'error');
      }
    }
  }, [context, release]);

  const play = useCallback(async () => {
    const cleanText = text.trim();
    if (!cleanText) return;

    if (state === 'connecting') {
      release();
      return;
    }
    if (state === 'playing') {
      await contextRef.current?.suspend();
      setState('paused');
      return;
    }
    if (state === 'paused') {
      if (stopActivePlayer !== release) stopActivePlayer?.();
      stopActivePlayer = release;
      await contextRef.current?.resume();
      setState('playing');
      return;
    }

    const AudioContextClass = audioContextConstructor();
    if (!AudioContextClass) {
      toast(tr('当前浏览器不支持实时语音播放', 'This browser does not support live speech playback'), 'error');
      return;
    }
    if (stopActivePlayer !== release) stopActivePlayer?.();
    stopActivePlayer = release;
    const controller = new AbortController();
    const audioContext = new AudioContextClass({ latencyHint: 'interactive' });
    abortRef.current = controller;
    contextRef.current = audioContext;
    setState('connecting');
    try {
      await audioContext.resume();
      // Leave a normalization margin because omitted Markdown code blocks are
      // replaced by a short spoken marker on the server.
      const providerLimit = Math.max(1, maxChars - 64);
      const chunks = splitSpeechText(
        cleanText,
        Math.min(PLAYBACK_CHUNK_CHARS, providerLimit),
      );
      setChunkCount(chunks.length);
      void runPlayback(chunks, audioContext, controller);
    } catch (error) {
      if (!controller.signal.aborted) {
        release();
        toast(`${tr('播放失败', 'Playback failed')}：${speechError(error)}`, 'error');
      }
    }
  }, [maxChars, release, runPlayback, state, text]);

  if (!settings || !settings.available || !settings.enabled || !text.trim()) return null;

  const active = state !== 'idle';
  const chunkProgress = chunkCount > 1 ? `${chunkIndex + 1}/${chunkCount}` : '';
  const title = state === 'connecting'
    ? tr('取消实时语音', 'Cancel live speech')
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
          name={state === 'playing' ? 'pause' : state === 'connecting' ? 'refresh' : 'play'}
          size={12}
          style={state === 'connecting' ? { animation: 'spin 1s linear infinite' } : undefined}
        />
      </button>
    );
  }

  return (
    <div className={`speech-player ${active ? 'active' : ''} ${className}`.trim()}>
      <button type="button" className="speech-player-button" onClick={() => void play()} aria-label={title}>
        <span className="speech-player-icon">
          <Icon
            name={state === 'playing' ? 'pause' : state === 'connecting' ? 'refresh' : 'play'}
            size={14}
            style={state === 'connecting' ? { animation: 'spin 1s linear infinite' } : undefined}
          />
        </span>
        <span>
          {state === 'connecting'
            ? tr('正在连接实时语音…', 'Connecting live speech…')
            : title}
        </span>
      </button>
      <div className="speech-player-track" aria-hidden="true">
        <span style={{ width: active ? '100%' : '0%' }} />
      </div>
      {(state === 'playing' || state === 'paused') && (
        <span className="speech-player-time">
          {chunkProgress ? `${chunkProgress} · ` : ''}{tr('实时', 'Live')} · {formatTime(elapsed)}
        </span>
      )}
    </div>
  );
}
