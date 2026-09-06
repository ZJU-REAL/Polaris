import { useMemo, useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import { api, type LibraryQaResponse } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   库级深度问答（agentic RAG，#644）：一问一答、证据先行。
   与「文献对话」互补：那边是多轮流式闲聊；这边跑完整检索流水线
   （查询扩展 → 引文补召回 → 重排 → 作答），回答里的每个引用都
   指向下方的证据卡，可点开对应论文。刻意做成最小问答框，不带历史。
   ============================================================ */

const VIA_LABELS: Record<string, { zh: string; en: string }> = {
  vector: { zh: '语义检索', en: 'retrieval' },
  expansion: { zh: '查询扩展', en: 'expansion' },
  citation: { zh: '引文关联', en: 'citation' },
  direct: { zh: '全库直供', en: 'whole library' },
};

/** 回答里的 [paper_id] 引用 → 可点击的 [n] 角标（n = 该论文在证据卡里的编号）。 */
function AnswerText({
  answer,
  paperIndex,
  onOpenPaper,
}: {
  answer: string;
  paperIndex: Map<string, number>;
  onOpenPaper: (id: string) => void;
}) {
  const parts = answer.split(/\[([0-9a-fA-F-]{36})\]/g);
  return (
    <div style={{ fontSize: 13, lineHeight: 1.7, whiteSpace: 'pre-wrap' }}>
      {parts.map((part, i) => {
        // split 捕获组：奇数位是 paper_id
        if (i % 2 === 0) return <span key={i}>{part}</span>;
        const n = paperIndex.get(part.toLowerCase());
        if (n === undefined) return <span key={i}>[{part}]</span>;
        return (
          <span
            key={i}
            role="link"
            tabIndex={0}
            title={tr('点击打开对应论文', 'Open the cited paper')}
            onClick={() => onOpenPaper(part)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') onOpenPaper(part);
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
            }}
          >
            {n}
          </span>
        );
      })}
    </div>
  );
}

export function LibraryQaTab({
  libraryId,
  onOpenPaper,
}: {
  libraryId: string;
  onOpenPaper: (id: string) => void;
}) {
  const [question, setQuestion] = useState('');
  const [result, setResult] = useState<LibraryQaResponse | null>(null);

  const mutation = useMutation({
    mutationFn: (q: string) => api.libraryQa(libraryId, q),
    onSuccess: setResult,
  });

  // 证据卡编号按论文去重（同一篇论文的多个片段共用一个编号，与回答角标一致）
  const paperIndex = useMemo(() => {
    const map = new Map<string, number>();
    for (const e of result?.evidence ?? []) {
      const key = e.paper_id.toLowerCase();
      if (!map.has(key)) map.set(key, map.size + 1);
    }
    return map;
  }, [result]);

  const ask = () => {
    const q = question.trim();
    if (!q || mutation.isPending) return;
    mutation.mutate(q);
  };

  return (
    <div className="col" style={{ flex: 1, minHeight: 0, padding: 16, gap: 12, overflowY: 'auto' }}>
      <div className="row gap8" style={{ alignItems: 'stretch' }}>
        <input
          className="input"
          style={{ flex: 1 }}
          value={question}
          placeholder={tr(
            '向整个文献库提问；回答只依据检索到的证据，每个引用都可回溯…',
            'Ask the whole library; answers cite retrieved evidence only…',
          )}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') ask();
          }}
        />
        <button className="btn btn-primary sm" disabled={mutation.isPending || !question.trim()} onClick={ask}>
          {mutation.isPending ? tr('检索作答中…', 'Answering…') : tr('提问', 'Ask')}
        </button>
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-4)' }}>
        {tr(
          '深度问答：查询扩展 + 引文关联补召回 + 重排后，只依据证据作答（不看证据外的内容）。',
          'Deep Q&A: query expansion + citation-graph recall + reranking; the answer is grounded in the evidence below.',
        )}
      </div>

      {mutation.isError && (
        <div className="card" style={{ padding: 12, color: 'var(--danger-tx, #c00)', fontSize: 12 }}>
          {tr('提问失败：', 'Request failed: ')}
          {mutation.error instanceof Error ? mutation.error.message : String(mutation.error)}
        </div>
      )}

      {!result && !mutation.isPending && !mutation.isError && (
        <EmptyState
          icon="chat"
          title={tr('问一个需要翻遍全库才能答的问题', 'Ask something that takes the whole library to answer')}
          desc={tr('例如：这个方向的方法可以分几类？谁和谁在互相引用？', 'e.g. What are the main method families? Who cites whom?')}
        />
      )}

      {result && (
        <>
          <div className="card" style={{ padding: 14 }}>
            <AnswerText answer={result.answer} paperIndex={paperIndex} onOpenPaper={onOpenPaper} />
            {result.queries.length > 0 && (
              <div style={{ marginTop: 10, fontSize: 10.5, color: 'var(--text-4)' }}>
                {tr('执行过的检索：', 'Queries run: ')}
                {result.queries.join(tr('；', '; '))}
              </div>
            )}
          </div>

          {result.evidence.length > 0 && (
            <div className="col" style={{ gap: 6 }}>
              <span className="mono" style={{ fontSize: 10, color: 'var(--text-4)' }}>
                {tr(`证据 · ${result.evidence.length} 条`, `Evidence · ${result.evidence.length}`)}
              </span>
              {result.evidence.map((e, i) => (
                <div
                  key={e.chunk_id ?? `${e.paper_id}-${i}`}
                  className="row gap8"
                  style={{
                    padding: '8px 10px',
                    borderRadius: 8,
                    background: 'var(--surface)',
                    border: '0.5px solid var(--border)',
                    alignItems: 'flex-start',
                  }}
                >
                  <span className="mono" style={{ color: 'var(--accent-text)', fontSize: 11, flexShrink: 0 }}>
                    [{paperIndex.get(e.paper_id.toLowerCase()) ?? '?'}]
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="row gap6" style={{ minWidth: 0 }}>
                      <span
                        style={{
                          fontSize: 12,
                          fontWeight: 600,
                          cursor: 'pointer',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                          minWidth: 0,
                        }}
                        title={`${e.title} · ${tr('点击打开论文', 'click to open paper')}`}
                        onClick={() => onOpenPaper(e.paper_id)}
                      >
                        {e.title || e.paper_id}
                      </span>
                      <span className="tag" style={{ fontSize: 9.5, flexShrink: 0 }}>
                        {tr(VIA_LABELS[e.via]?.zh ?? e.via, VIA_LABELS[e.via]?.en ?? e.via)}
                      </span>
                      {typeof e.score === 'number' && (
                        <span className="mono" style={{ fontSize: 10, color: 'var(--text-4)', flexShrink: 0 }}>
                          {e.score.toFixed(2)}
                        </span>
                      )}
                    </div>
                    <div style={{ marginTop: 3, fontSize: 11.5, color: 'var(--text-3)', lineHeight: 1.55 }}>
                      {e.snippet}
                    </div>
                  </div>
                  <button
                    className="icon-btn"
                    style={{ width: 22, height: 22, border: 'none', background: 'transparent', flexShrink: 0 }}
                    title={tr('打开论文详情', 'Open paper detail')}
                    onClick={() => onOpenPaper(e.paper_id)}
                  >
                    <Icon name="layers" size={12} />
                  </button>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
