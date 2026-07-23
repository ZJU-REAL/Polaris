import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { RelevanceBar } from '../../components/ui/RelevanceBar';
import { api, type LibraryChatSource } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   文献对话的来源渲染件（库对话 / 相关研究对话 / 个人库对话共用）。
   scoped 场景下 relevance / status 可能为 null——渲染要容忍：
   没有相关度就不画相关度条，没有状态就不画状态徽标，绝不因缺字段崩。
   ============================================================ */

/** 回答里 [[fig:论文id:图号]] 标记 → 内联配图（blob→objectURL，点击开论文）。 */
export function ChatFigure({
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

function SourceCard({ s, onOpenPaper }: { s: LibraryChatSource; onOpenPaper: (id: string) => void }) {
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
            title={`${s.title} · ${tr('点击去阅读', 'click to read')}`}
            onClick={() => onOpenPaper(s.paper_id)}
          >
            {s.title}
          </span>
          {s.year !== null && (
            <span className="mono" style={{ fontSize: 10, color: 'var(--text-4)', flexShrink: 0 }}>{s.year}</span>
          )}
          {/* scoped 场景相关度可能为 null——有才画相关度条 */}
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
                style={{ fontSize: 9.5, height: 15, lineHeight: '15px', padding: '0 5px' }}
              >
                {name}
              </span>
            ))}
          </div>
        )}
      </div>
      <button
        className="icon-btn"
        style={{ width: 22, height: 22, border: 'none', background: 'transparent', flexShrink: 0 }}
        title={tr('去阅读（PDF + AI 伴读）', 'Read (PDF + AI companion)')}
        onClick={() => onOpenPaper(s.paper_id)}
      >
        <Icon name="book" size={12} />
      </button>
    </div>
  );
}

export function SourceList({
  sources,
  onOpenPaper,
}: {
  sources: LibraryChatSource[];
  onOpenPaper: (id: string) => void;
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
        <SourceCard key={s.index} s={s} onOpenPaper={onOpenPaper} />
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

/** [n] 引用角标：可点击跳到对应来源论文。编号不在来源清单里则按原文渲染。 */
export function makeCitationRenderer(onOpenPaper: (id: string) => void) {
  return (sources?: LibraryChatSource[]) =>
    (n: number) => {
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
    };
}
