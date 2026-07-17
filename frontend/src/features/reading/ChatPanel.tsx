import { useEffect, useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { Markdown } from '../../lib/markdown';
import { api, type ChatTurn } from '../../lib/api';
import { chatPaperSse } from '../../lib/sse';
import { tr } from '../../lib/i18n';

/* ============================================================
   阅读工作台 · AI 伴读面板：
   气泡对话（user 右蓝 / assistant 左灰）+ 流式回答（SSE delta）
   + 每条回答可一键「存为笔记」。history 由组件 state 维护，
   请求时最多携带最近 10 轮（20 条）。
   ============================================================ */

interface ChatMsg {
  role: 'user' | 'assistant';
  content: string;
  /** assistant：流已结束（done/关流） */
  done?: boolean;
  /** assistant：出错中断 */
  failed?: boolean;
}

const MAX_HISTORY_TURNS = 10;

export function ChatPanel({ paperId, pid }: { paperId: string; pid: string }) {
  const queryClient = useQueryClient();
  const [msgs, setMsgs] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const stopRef = useRef<(() => void) | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // 卸载时断开流
  useEffect(() => () => stopRef.current?.(), []);

  // 新消息自动滚到底
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [msgs]);

  const saveNoteMutation = useMutation({
    mutationFn: (content: string) => api.createPaperNote(paperId, content),
    onSuccess: () => {
      toast(tr('已存为笔记', 'Saved as note'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['paper-notes', paperId] });
      void queryClient.invalidateQueries({ queryKey: ['project-notes', pid] });
      void queryClient.invalidateQueries({ queryKey: ['paper', paperId] });
      void queryClient.invalidateQueries({ queryKey: ['papers', pid] });
    },
    onError: (e) => toast(`${tr('保存失败：', 'Save failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const finishStream = (failed: boolean, fallbackText?: string) => {
    setStreaming(false);
    stopRef.current = null;
    setMsgs((m) => {
      const next = [...m];
      const last = next[next.length - 1];
      if (last && last.role === 'assistant') {
        next[next.length - 1] = {
          ...last,
          done: true,
          failed: failed || undefined,
          content: last.content || fallbackText || '',
        };
      }
      return next;
    });
  };

  const send = () => {
    const q = input.trim();
    if (!q || streaming) return;

    // 携带最近 10 轮完整历史（不含本次提问）
    const history: ChatTurn[] = msgs
      .filter((m) => m.content && !m.failed)
      .slice(-MAX_HISTORY_TURNS * 2)
      .map((m) => ({ role: m.role, content: m.content }));

    setMsgs((m) => [...m, { role: 'user', content: q }, { role: 'assistant', content: '' }]);
    setInput('');
    setStreaming(true);

    stopRef.current = chatPaperSse(paperId, { question: q, history }, {
      onEvent: (event, dataStr) => {
        if (event === 'delta') {
          let text = '';
          try {
            text = (JSON.parse(dataStr) as { text?: string }).text ?? '';
          } catch {
            return;
          }
          if (!text) return;
          setMsgs((m) => {
            const next = [...m];
            const last = next[next.length - 1];
            if (last && last.role === 'assistant' && !last.done) {
              next[next.length - 1] = { ...last, content: last.content + text };
            }
            return next;
          });
        } else if (event === 'done') {
          finishStream(false);
        } else if (event === 'error') {
          let detail = tr('服务端出错', 'Server error');
          try {
            detail = (JSON.parse(dataStr) as { detail?: string }).detail ?? detail;
          } catch {
            /* keep default */
          }
          toast(`${tr('AI 伴读出错：', 'AI chat error: ')}${detail}`, 'error');
          finishStream(true, tr('（回答中断了，请重试）', '(The answer was interrupted — please retry)'));
        }
      },
      onClose: () => finishStream(false),
      onError: (err) => {
        toast(`${tr('AI 伴读连接失败：', 'AI chat connection failed: ')}${err instanceof Error ? err.message : String(err)}`, 'error');
        finishStream(true, tr('（连接失败，请稍后重试）', '(Connection failed — try again later)'));
      },
    });
  };

  const stop = () => {
    stopRef.current?.();
    finishStream(false);
  };

  /** 找到某条 assistant 回答对应的提问（前一条 user 消息）。 */
  const questionFor = (idx: number): string => {
    for (let i = idx - 1; i >= 0; i--) {
      const m = msgs[i];
      if (m && m.role === 'user') return m.content;
    }
    return '';
  };

  return (
    <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      {/* —— 提示条 —— */}
      <div
        style={{
          flexShrink: 0,
          margin: '10px 14px 0',
          padding: '6px 10px',
          borderRadius: 8,
          background: 'var(--surface-2)',
          fontSize: 11,
          color: 'var(--text-3)',
          lineHeight: 1.5,
        }}
      >
        {tr('回答基于论文全文，仅供参考——关键结论请回到原文核对。', 'Answers are based on the full paper text and are for reference only — verify key conclusions against the original.')}
      </div>

      {/* —— 对话区 —— */}
      <div ref={scrollRef} className="scroll" style={{ flex: 1, overflowY: 'auto', padding: '12px 14px' }}>
        {msgs.length === 0 ? (
          <EmptyState
            compact
            icon="chat"
            title={tr('问问 AI 这篇论文', 'Ask AI about this paper')}
            desc={tr('比如：这篇论文解决了什么问题？方法核心是什么？和之前的工作差别在哪？', 'e.g. What problem does this paper solve? What is the core of the method? How does it differ from prior work?')}
          />
        ) : (
          msgs.map((m, i) => {
            if (m.role === 'user') {
              return (
                <div key={i} style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 10 }}>
                  <div
                    style={{
                      maxWidth: '86%',
                      padding: '8px 12px',
                      borderRadius: '12px 12px 3px 12px',
                      background: 'var(--accent)',
                      color: '#fff',
                      fontSize: 12.5,
                      lineHeight: 1.6,
                      whiteSpace: 'pre-wrap',
                      overflowWrap: 'break-word',
                    }}
                  >
                    {m.content}
                  </div>
                </div>
              );
            }
            const thinking = !m.done && !m.content;
            return (
              <div key={i} style={{ display: 'flex', justifyContent: 'flex-start', marginBottom: 10 }}>
                <div
                  style={{
                    maxWidth: '92%',
                    minWidth: 0,
                    padding: '8px 12px',
                    borderRadius: '12px 12px 12px 3px',
                    background: 'var(--surface-2)',
                    border: '0.5px solid var(--border)',
                    fontSize: 12.5,
                  }}
                >
                  {thinking ? (
                    <span className="row gap6 muted" style={{ fontSize: 12 }}>
                      <Icon name="refresh" size={12} style={{ animation: 'spin 1s linear infinite' }} />
                      {tr('正在阅读论文并思考…', 'Reading the paper and thinking…')}
                    </span>
                  ) : (
                    <Markdown source={m.content} style={{ fontSize: 12.5 }} />
                  )}
                  {m.done && !m.failed && m.content && (
                    <div className="row" style={{ marginTop: 8, justifyContent: 'flex-end' }}>
                      <button
                        className="btn btn-soft sm"
                        style={{ height: 24, fontSize: 11 }}
                        disabled={saveNoteMutation.isPending}
                        onClick={() =>
                          saveNoteMutation.mutate(`**问**：${questionFor(i)}\n\n**AI**：${m.content}`)
                        }
                      >
                        <Icon name="pen" size={11} />
                        {tr('存为笔记', 'Save as note')}
                      </button>
                    </div>
                  )}
                </div>
              </div>
            );
          })
        )}
      </div>

      {/* —— 输入区 —— */}
      <div
        className="row gap8"
        style={{ borderTop: '0.5px solid var(--border)', padding: '10px 14px', flexShrink: 0 }}
      >
        <input
          className="input"
          style={{ flex: 1, minWidth: 0, height: 34, fontSize: 12.5 }}
          placeholder={tr('针对这篇论文提问…', 'Ask about this paper…')}
          value={input}
          disabled={streaming}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.nativeEvent.isComposing) send();
          }}
        />
        {streaming ? (
          <button className="btn btn-ghost sm" style={{ height: 34 }} onClick={stop}>
            <Icon name="pause" size={13} />
            {tr('停止', 'Stop')}
          </button>
        ) : (
          <button
            className="btn btn-primary sm"
            style={{ height: 34 }}
            disabled={!input.trim()}
            onClick={send}
          >
            <Icon name="arrow" size={13} />
            {tr('发送', 'Send')}
          </button>
        )}
      </div>
    </div>
  );
}
