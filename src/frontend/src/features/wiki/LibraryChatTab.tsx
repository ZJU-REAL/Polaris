import { useCallback, useEffect, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { Markdown, type WikiLinkHandler } from '../../lib/markdown';
import { api, type LibraryChatSource } from '../../lib/api';
import { chatLibrarySse } from '../../lib/sse';
import { RelevanceBar } from '../../components/ui/RelevanceBar';
import { tr } from '../../lib/i18n';
import { ChatSurface } from '../chat/ChatSurface';
import { readerFrom } from '../reading/shared';
import type { ChatMsg } from '../chat/types';

/* ============================================================
   文献库对话 Tab：对整个文献库做跨文献分析与综合梳理。
   壳（历史/技能条/输入/ 上下文/@ 分享）复用 ChatSurface；
   这里只提供库对话特有的：引用角标、来源卡、补建索引、建议。
   ============================================================ */

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

/** 回答里 [[fig:论文id:图号]] 标记 → 内联配图（blob→objectURL，点击开论文）。 */
function ChatFigure({
  paperId,
  index,
  onOpenPaper,
}: {
  paperId: string;
  index: number;
  onOpenPaper: (id: string) => void;
}) {
  const query = useQuery({
    queryKey: ['figure-image', paperId, index],
    queryFn: () => api.fetchFigureImage(paperId, index),
    staleTime: Infinity,
    retry: false,
  });
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    const blob = query.data;
    if (!blob) {
      setUrl(null);
      return;
    }
    const u = URL.createObjectURL(blob);
    setUrl(u);
    return () => URL.revokeObjectURL(u);
  }, [query.data]);

  if (query.isError) return null; // 图缺失静默跳过
  if (!url) {
    return <span style={{ color: 'var(--text-3)', fontSize: 12 }}>{tr('配图加载中…', 'loading figure…')}</span>;
  }
  return (
    <img
      src={url}
      alt={tr('论文配图', 'paper figure')}
      title={tr('点击打开论文', 'click to open paper')}
      onClick={() => onOpenPaper(paperId)}
      style={{
        display: 'block',
        maxWidth: '100%',
        maxHeight: 260,
        margin: '8px 0',
        borderRadius: 8,
        border: '0.5px solid var(--border)',
        cursor: 'pointer',
      }}
    />
  );
}

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
  const location = useLocation();
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
          onClick={() => navigate(`/papers/${s.paper_id}/read`, { state: readerFrom(location, 'wiki') })}
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

  const stream = useCallback(
    (
      args: { question: string; history: { role: 'user' | 'assistant'; content: string }[] },
      ctrl: {
        onDelta: (t: string) => void;
        onSources?: (s: string) => void;
        onDone: () => void;
        onError: (d: string) => void;
      },
    ) =>
      chatLibrarySse(pid, { question: args.question, history: args.history }, {
        onEvent: (event, dataStr) => {
          if (event === 'sources') ctrl.onSources?.(dataStr);
          else if (event === 'delta') {
            try {
              const t = (JSON.parse(dataStr) as { text?: string }).text ?? '';
              if (t) ctrl.onDelta(t);
            } catch {
              /* ignore */
            }
          } else if (event === 'done') ctrl.onDone();
          else if (event === 'error') {
            let detail = tr('服务端出错', 'Server error');
            try {
              detail = (JSON.parse(dataStr) as { detail?: string }).detail ?? detail;
            } catch {
              /* keep */
            }
            ctrl.onError(detail);
          }
        },
        onClose: () => ctrl.onDone(),
        onError: (err) => ctrl.onError(err instanceof Error ? err.message : String(err)),
      }),
    [pid],
  );

  return (
    <ChatSurface
      surfaceKey={`library:${pid}`}
      pid={pid}
      title={tr('文献对话', 'Library chat')}
      contextKinds={['paper', 'idea', 'experiment', 'concept']}
      hint={tr(
        '回答基于从整个文献库检索到的全文片段，可做跨文献对比与综合梳理；[n] 为引用来源编号。',
        'Answers are grounded in full-text passages from the whole library; [n] marks a source number.',
      )}
      headerAction={
        <button
          className="btn btn-ghost sm"
          style={{ height: 26, fontSize: 10.5, flexShrink: 0 }}
          title={tr('给较早入库、还没建全文索引的论文补索引', 'Backfill the full-text index for older papers')}
          disabled={rebuildMutation.isPending}
          onClick={() => rebuildMutation.mutate()}
        >
          <Icon name="refresh" size={11} style={rebuildMutation.isPending ? { animation: 'spin 1s linear infinite' } : undefined} />
          {tr('补建索引', 'Rebuild index')}
        </button>
      }
      emptyIcon="chat"
      emptyTitle={tr('和整个文献库对话', 'Chat with the whole library')}
      emptyDesc={tr(
        '跨文献的分析、比较、综合梳理都可以问；/ 放入指定论文，@ 分享推荐给同事或群。',
        'Ask cross-paper questions; use / to pin papers, @ to share or recommend to teammates.',
      )}
      suggestions={SUGGESTIONS}
      placeholder={tr('提问，或输入 / 放入上下文、@ 分享…', 'Ask, or type / for context, @ to share…')}
      renderAssistant={(m: ChatMsg) => (
        <Markdown
          source={m.content}
          style={{ fontSize: 12.5 }}
          onWikiLink={onWikiLink}
          renderCitation={citationRenderer(m.sources)}
          renderLibraryFigure={(paperId, index) => (
            <ChatFigure paperId={paperId} index={index} onOpenPaper={onOpenPaper} />
          )}
        />
      )}
      assistantExtras={(m: ChatMsg) =>
        (m.sources?.length ?? 0) > 0 && (m.done || m.content) ? (
          <SourceList sources={m.sources ?? []} onOpenPaper={onOpenPaper} onWikiLink={onWikiLink} />
        ) : null
      }
      stream={stream}
    />
  );
}
