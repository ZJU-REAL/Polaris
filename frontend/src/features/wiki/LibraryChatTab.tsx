import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { Markdown, type WikiLinkHandler } from '../../lib/markdown';
import { api, type ChatTurn, type LibraryChatSource } from '../../lib/api';
import { chatLibrarySse } from '../../lib/sse';
import { RelevanceBar } from '../../components/ui/RelevanceBar';
import { tr } from '../../lib/i18n';

/* ============================================================
   文献库对话 Tab：对整个文献库做跨文献分析与综合梳理。
   气泡对话 + 流式回答，回答里嵌入文献库的可交互元素：
   - [n] 引用角标 → 点击打开对应论文详情；
   - [[概念名]] 双链 → 点击跳概念库；
   - 来源卡：标题/年份/相关度 + 概念 chips +「详情 / 阅读」操作。
   ============================================================ */

interface ChatMsg {
  role: 'user' | 'assistant';
  content: string;
  /** assistant：本次回答检索到的引用来源 */
  sources?: LibraryChatSource[];
  done?: boolean;
  failed?: boolean;
}

const MAX_HISTORY_TURNS = 10;

// 模块级常量存 zh/en 两份，渲染处再 tr（import 时求值不会随语言切换更新）
const SUGGESTIONS: { zh: string; en: string }[] = [
  {
    zh: '这个方向目前的主流方法可以分成哪几类？各自的代表工作是什么？',
    en: 'What are the main families of methods in this direction, and the representative work of each?',
  },
  {
    zh: '综合文献库，当前还有哪些没有解决好的问题？',
    en: 'Across the library, which problems remain unsolved?',
  },
  {
    zh: '对比一下库里方法们使用的评测基准和指标。',
    en: 'Compare the benchmarks and metrics used by the methods in the library.',
  },
];

function SourceCard({
  s,
  onOpenPaper,
  onWikiLink,
}: {
  s: LibraryChatSource;
  onOpenPaper: (id: string) => void;
  onWikiLink?: WikiLinkHandler;
}) {
  const navigate = useNavigate();
  return (
    <div
      className="row gap8"
      style={{
        padding: '6px 8px',
        borderRadius: 8,
        background: 'var(--surface)',
        border: '0.5px solid var(--border)',
        minWidth: 0,
        alignItems: 'flex-start',
      }}
    >
      <span className="mono" style={{ color: 'var(--accent-text)', fontSize: 11, flexShrink: 0, marginTop: 1 }}>
        [{s.index}]
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="row gap6" style={{ minWidth: 0 }}>
          <span
            style={{
              fontSize: 11.5,
              fontWeight: 600,
              cursor: 'pointer',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              minWidth: 0,
            }}
            title={`${s.title} · ${tr('点击打开详情', 'click to open detail')}`}
            onClick={() => onOpenPaper(s.paper_id)}
          >
            {s.title}
          </span>
          {s.year !== null && (
            <span className="mono" style={{ fontSize: 10, color: 'var(--text-4)', flexShrink: 0 }}>{s.year}</span>
          )}
          {typeof s.relevance === 'number' && (
            <span style={{ flexShrink: 0 }}>
              <RelevanceBar value={s.relevance} width={40} />
            </span>
          )}
        </div>
        {(s.concepts?.length ?? 0) > 0 && (
          <div className="row gap4 wrap" style={{ marginTop: 3 }}>
            {(s.concepts ?? []).slice(0, 6).map((name) => (
              <span
                key={name}
                className="tag"
                style={{ fontSize: 9.5, height: 15, lineHeight: '15px', padding: '0 5px', cursor: 'pointer' }}
                title={`${tr('打开概念：', 'Open concept: ')}${name}`}
                onClick={() => onWikiLink?.(name)}
              >
                {name}
              </span>
            ))}
          </div>
        )}
      </div>
      <div className="row gap4" style={{ flexShrink: 0 }}>
        <button
          className="icon-btn"
          style={{ width: 22, height: 22, border: 'none', background: 'transparent' }}
          title={tr('打开论文详情', 'Open paper detail')}
          onClick={() => onOpenPaper(s.paper_id)}
        >
          <Icon name="layers" size={12} />
        </button>
        <button
          className="icon-btn"
          style={{ width: 22, height: 22, border: 'none', background: 'transparent' }}
          title={tr('去阅读（PDF + AI 伴读）', 'Read (PDF + AI companion)')}
          onClick={() => navigate(`/papers/${s.paper_id}/read`)}
        >
          <Icon name="book" size={12} />
        </button>
      </div>
    </div>
  );
}

function SourceList({
  sources,
  onOpenPaper,
  onWikiLink,
}: {
  sources: LibraryChatSource[];
  onOpenPaper: (id: string) => void;
  onWikiLink?: WikiLinkHandler;
}) {
  const [open, setOpen] = useState(false);
  if (sources.length === 0) return null;
  const shown = open ? sources : sources.slice(0, 3);
  return (
    <div className="col" style={{ gap: 4, marginTop: 10, paddingTop: 8, borderTop: '0.5px solid var(--border)' }}>
      <span className="mono" style={{ fontSize: 10, color: 'var(--text-4)' }}>
        {tr(`引用来源 · ${sources.length} 篇`, `Sources · ${sources.length}`)}
      </span>
      {shown.map((s) => (
        <SourceCard key={s.index} s={s} onOpenPaper={onOpenPaper} onWikiLink={onWikiLink} />
      ))}
      {sources.length > 3 && (
        <button
          onClick={() => setOpen(!open)}
          style={{ border: 'none', background: 'transparent', cursor: 'pointer', padding: 0, fontSize: 11, color: 'var(--accent-text)', textAlign: 'left' }}
        >
          {open ? tr('收起', 'Collapse') : tr(`展开全部 ${sources.length} 篇来源`, `Show all ${sources.length} sources`)}
        </button>
      )}
    </div>
  );
}

export interface LibraryChatTabProps {
  pid: string;
  onOpenPaper: (id: string) => void;
  /** [[概念名]] 双链点击 → 按名称跳概念库 */
  onWikiLink?: WikiLinkHandler;
}

export function LibraryChatTab({ pid, onOpenPaper, onWikiLink }: LibraryChatTabProps) {
  const [msgs, setMsgs] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const stopRef = useRef<(() => void) | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // 切换方向清空对话；卸载时断流
  useEffect(() => {
    setMsgs([]);
    return () => stopRef.current?.();
  }, [pid]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [msgs]);

  // 老数据补建全文分段索引（新入库论文由任务自动处理）
  const rebuildMutation = useMutation({
    mutationFn: () => api.rebuildFulltextIndex(pid),
    onSuccess: (r) => {
      if (r.papers_indexed === 0) {
        toast(tr('全文索引已是最新', 'Full-text index is up to date'), 'info');
      } else {
        toast(
          tr(
            `已为 ${r.papers_indexed} 篇论文建好全文索引（${r.chunks_created} 段）`,
            `Indexed ${r.papers_indexed} papers (${r.chunks_created} chunks)`,
          ),
          'ok',
        );
      }
    },
    onError: (e) =>
      toast(`${tr('索引重建失败：', 'Index rebuild failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
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

  const send = (question?: string) => {
    const q = (question ?? input).trim();
    if (!q || streaming) return;

    const history: ChatTurn[] = msgs
      .filter((m) => m.content && !m.failed)
      .slice(-MAX_HISTORY_TURNS * 2)
      .map((m) => ({ role: m.role, content: m.content }));

    setMsgs((m) => [...m, { role: 'user', content: q }, { role: 'assistant', content: '' }]);
    setInput('');
    setStreaming(true);

    stopRef.current = chatLibrarySse(pid, { question: q, history }, {
      onEvent: (event, dataStr) => {
        if (event === 'sources') {
          let items: LibraryChatSource[] = [];
          try {
            items = (JSON.parse(dataStr) as { items?: LibraryChatSource[] }).items ?? [];
          } catch {
            return;
          }
          setMsgs((m) => {
            const next = [...m];
            const last = next[next.length - 1];
            if (last && last.role === 'assistant' && !last.done) {
              next[next.length - 1] = { ...last, sources: items };
            }
            return next;
          });
        } else if (event === 'delta') {
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
          toast(`${tr('文献对话出错：', 'Library chat error: ')}${detail}`, 'error');
          finishStream(true, tr('（回答中断了，请重试）', '(Answer interrupted — please retry)'));
        }
      },
      onClose: () => finishStream(false),
      onError: (err) => {
        toast(`${tr('连接失败：', 'Connection failed: ')}${err instanceof Error ? err.message : String(err)}`, 'error');
        finishStream(true, tr('（连接失败，请稍后重试）', '(Connection failed — try again later)'));
      },
    });
  };

  const stop = () => {
    stopRef.current?.();
    finishStream(false);
  };

  // [n] 引用角标：可点击，悬停显示论文标题；编号不在来源清单里则按原文渲染
  const citationRenderer = useCallback(
    (sources?: LibraryChatSource[]) => (n: number) => {
      const src = sources?.find((s) => s.index === n);
      if (!src) return null;
      return (
        <span
          role="link"
          tabIndex={0}
          title={`${src.title} · ${tr('点击打开', 'click to open')}`}
          onClick={() => onOpenPaper(src.paper_id)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') onOpenPaper(src.paper_id);
          }}
          style={{
            display: 'inline-block',
            padding: '0 4px',
            margin: '0 1px',
            borderRadius: 5,
            background: 'var(--accent-soft)',
            color: 'var(--accent-text)',
            fontSize: '0.82em',
            fontWeight: 650,
            cursor: 'pointer',
            verticalAlign: '0.15em',
            lineHeight: 1.5,
          }}
        >
          {n}
        </span>
      );
    },
    [onOpenPaper],
  );

  return (
    <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      {/* —— 提示条 —— */}
      <div
        className="row gap8"
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
        <span style={{ flex: 1 }}>
          {tr(
            '回答基于从整个文献库检索到的全文片段，可做跨文献对比与综合梳理；[n] 为引用来源编号。',
            'Answers are grounded in full-text passages retrieved from the whole library, so cross-paper comparison and synthesis work; [n] marks a source number.',
          )}
        </span>
        <button
          className="btn btn-ghost sm"
          style={{ height: 22, fontSize: 10.5, flexShrink: 0 }}
          title={tr(
            '给较早入库、还没建全文索引的论文补索引（新论文由任务自动处理）',
            'Backfill the full-text index for older papers (new papers are indexed automatically)',
          )}
          disabled={rebuildMutation.isPending}
          onClick={() => rebuildMutation.mutate()}
        >
          {rebuildMutation.isPending ? (
            <Icon name="refresh" size={11} style={{ animation: 'spin 1s linear infinite' }} />
          ) : (
            <Icon name="refresh" size={11} />
          )}
          {tr('补建全文索引', 'Rebuild full-text index')}
        </button>
      </div>

      {/* —— 对话区 —— */}
      <div ref={scrollRef} className="scroll" style={{ flex: 1, overflowY: 'auto', padding: '12px 14px' }}>
        {msgs.length === 0 ? (
          <div style={{ maxWidth: 560, margin: '40px auto 0' }}>
            <EmptyState
              compact
              icon="chat"
              title={tr('和整个文献库对话', 'Chat with the whole library')}
              desc={tr(
                '跨文献的分析、比较、综合梳理都可以问，AI 会先检索相关论文片段再回答。',
                'Ask cross-paper analysis, comparison, or synthesis questions — the AI retrieves relevant passages before answering.',
              )}
            />
            <div className="col gap8" style={{ marginTop: 18 }}>
              {SUGGESTIONS.map((s) => (
                <button
                  key={s.zh}
                  className="card"
                  style={{
                    textAlign: 'left',
                    padding: '9px 12px',
                    fontSize: 12,
                    color: 'var(--text-2)',
                    cursor: 'pointer',
                    border: '0.5px solid var(--border)',
                    background: 'var(--surface)',
                    lineHeight: 1.5,
                  }}
                  onClick={() => send(tr(s.zh, s.en))}
                >
                  {tr(s.zh, s.en)}
                </button>
              ))}
            </div>
          </div>
        ) : (
          msgs.map((m, i) => {
            if (m.role === 'user') {
              return (
                <div key={i} style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 10 }}>
                  <div
                    style={{
                      maxWidth: '78%',
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
                    maxWidth: '86%',
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
                      {tr('正在检索文献库并思考…', 'Searching the library and thinking…')}
                    </span>
                  ) : (
                    <Markdown
                      source={m.content}
                      style={{ fontSize: 12.5 }}
                      onWikiLink={onWikiLink}
                      renderCitation={citationRenderer(m.sources)}
                    />
                  )}
                  {(m.sources?.length ?? 0) > 0 && (m.done || m.content) && (
                    <SourceList sources={m.sources ?? []} onOpenPaper={onOpenPaper} onWikiLink={onWikiLink} />
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
          placeholder={tr(
            '向整个文献库提问，比如：这些方法的共同局限是什么？',
            'Ask the whole library, e.g. what limitations do these methods share?',
          )}
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
            onClick={() => send()}
          >
            <Icon name="arrow" size={13} />
            {tr('发送', 'Send')}
          </button>
        )}
      </div>
    </div>
  );
}
