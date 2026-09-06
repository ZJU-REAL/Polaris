import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { EmptyState } from '../../components/ui/EmptyState';
import { Icon } from '../../components/ui/Icon';
import { api, type GapKind, type LibraryGapEntry } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   研究缺口台账（#665）：库内论文里「别人还没解决的问题 / 相互矛盾的
   结论 / 失败的尝试」的聚合视图。每条都带逐字原文摘录（引用块），
   点论文名跳论文库详情。顶部先摆疑似矛盾对——启发式匹配（共享概念
   词 + 一侧否定措辞），明确标注请读者对照双方原文自行核对。
   ============================================================ */

const KIND_META: Record<GapKind, { zh: string; en: string; tone: string }> = {
  gap: { zh: '没人解决的问题', en: 'Open problem', tone: 'var(--accent)' },
  contradiction: { zh: '矛盾的结论', en: 'Contradiction', tone: 'var(--warn-tx)' },
  uncertainty: { zh: '尚不确定', en: 'Uncertain', tone: 'var(--text-3)' },
  negative_result: { zh: '失败的尝试', en: 'Negative result', tone: 'var(--danger-tx)' },
  limitation: { zh: '作者自述局限', en: 'Limitation', tone: 'var(--text-3)' },
};

const KIND_FILTERS: (GapKind | 'all')[] = [
  'all',
  'gap',
  'contradiction',
  'negative_result',
  'uncertainty',
  'limitation',
];

function KindBadge({ kind }: { kind: GapKind }) {
  const meta = KIND_META[kind];
  if (!meta) return null;
  return (
    <span className="pill" style={{ color: meta.tone, fontSize: 11, flexShrink: 0 }}>
      {tr(meta.zh, meta.en)}
    </span>
  );
}

function GapCard({ entry, onOpenPaper }: { entry: LibraryGapEntry; onOpenPaper: (id: string) => void }) {
  return (
    <div className="card" style={{ padding: '12px 14px' }}>
      <div className="row gap8" style={{ alignItems: 'center', flexWrap: 'wrap' }}>
        <KindBadge kind={entry.kind} />
        <span
          style={{ fontSize: 12, fontWeight: 600, cursor: 'pointer', color: 'var(--accent)', minWidth: 0 }}
          title={tr('点击打开论文', 'Click to open the paper')}
          onClick={() => onOpenPaper(entry.paper_id)}
        >
          {entry.paper_title}
        </span>
        {entry.year != null && (
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>{entry.year}</span>
        )}
      </div>
      <div style={{ fontSize: 13, lineHeight: 1.7, marginTop: 6 }}>{entry.statement}</div>
      {/* 原文摘录：台账的锚点——AI 归纳可疑时，读者靠这段核对出处 */}
      <blockquote
        style={{
          margin: '8px 0 0',
          padding: '4px 10px',
          borderLeft: '3px solid var(--border-2)',
          fontSize: 12,
          color: 'var(--text-3)',
          lineHeight: 1.6,
        }}
      >
        {entry.source_span}
      </blockquote>
    </div>
  );
}

export function GapsTab({
  libraryId,
  onOpenPaper,
}: {
  libraryId: string;
  onOpenPaper: (id: string) => void;
}) {
  const [kind, setKind] = useState<GapKind | 'all'>('all');

  const { data, isLoading, isError } = useQuery({
    queryKey: ['library-gaps', libraryId, kind],
    queryFn: () => api.getLibraryGaps(libraryId, kind === 'all' ? {} : { kind }),
    retry: false,
  });

  const entries = data?.entries ?? [];
  const pairs = data?.pairs ?? [];

  return (
    <div className="col" style={{ flex: 1, minHeight: 0 }}>
      <div style={{ padding: '14px 16px 10px', borderBottom: '1px solid var(--border)' }}>
        <div style={{ fontSize: 13, color: 'var(--text-2)' }}>
          {tr(
            '从库内论文全文里自动整理：别人还没解决的问题、相互矛盾的结论、失败的尝试。每条都附原文出处。',
            'Auto-collected from full texts in this library: open problems, contradictory findings, failed attempts — each anchored to a verbatim quote.',
          )}
        </div>
        <div className="row gap6 wrap" style={{ marginTop: 10 }}>
          {KIND_FILTERS.map((f) => (
            <span key={f} className={`chip${kind === f ? ' on' : ''}`} onClick={() => setKind(f)}>
              {f === 'all' ? tr('全部', 'All') : tr(KIND_META[f].zh, KIND_META[f].en)}
            </span>
          ))}
        </div>
      </div>

      <div className="scroll" style={{ overflowY: 'auto', flex: 1, padding: 16 }}>
        {isLoading ? (
          <div className="empty">{tr('整理研究缺口…', 'Collecting research gaps…')}</div>
        ) : isError ? (
          <EmptyState
            compact
            icon="x"
            title={tr('无法加载研究缺口', 'Failed to load research gaps')}
            desc={tr('后端不可用或接口尚未就绪，稍后重试。', 'Backend unavailable or API not ready — try again later.')}
          />
        ) : entries.length === 0 ? (
          <EmptyState
            compact
            icon="bulb"
            title={tr('还没有条目', 'Nothing here yet')}
            desc={tr(
              '论文全文就位后会自动整理；也可能这个筛选下确实没有。',
              'Entries appear automatically once full texts are processed; or this filter simply has none.',
            )}
          />
        ) : (
          <div className="col gap10">
            {pairs.length > 0 && (
              <div className="card" style={{ padding: '12px 14px', background: 'var(--warn-bg)' }}>
                <div className="row gap8" style={{ alignItems: 'center', flexWrap: 'wrap' }}>
                  <Icon name="scale" size={14} />
                  <b style={{ fontSize: 13 }}>{tr('疑似矛盾的结论', 'Possible contradictions')}</b>
                  <span style={{ fontSize: 11, color: 'var(--text-3)' }}>
                    {tr('按共同关键词自动匹配，请对照双方原文核实', 'Matched by shared keywords — verify against both quotes')}
                  </span>
                </div>
                <div className="col gap10" style={{ marginTop: 10 }}>
                  {pairs.map((pair, i) => (
                    <div key={i} className="col gap6">
                      <GapCard entry={pair.a} onOpenPaper={onOpenPaper} />
                      <div style={{ fontSize: 11, color: 'var(--text-3)', textAlign: 'center' }}>
                        {tr('↕ 说法对不上', '↕ These disagree')}
                        {pair.shared_terms.length > 0 && ` · ${pair.shared_terms.join(' / ')}`}
                      </div>
                      <GapCard entry={pair.b} onOpenPaper={onOpenPaper} />
                    </div>
                  ))}
                </div>
              </div>
            )}
            {entries.map((entry, i) => (
              <GapCard key={i} entry={entry} onOpenPaper={onOpenPaper} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
