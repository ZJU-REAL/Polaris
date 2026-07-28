import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type CollectingLibrary } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { Icon } from './Icon';
import { Modal } from './Modal';

/* ============================================================
   「被哪些文献库收录」：行内只显示前两个 + 计数，点开看完整列表与相关度。

   行内不铺开是有原因的：一篇热门论文可能同时进十几个库，全列出来会把详情页顶部
   挤没。相关度分也只在弹窗里给——它是每个库各自打的分，并排堆在一行里没法读。
   ============================================================ */

function scoreText(v: number | null): string {
  return v === null ? tr('未打分', 'not scored') : v.toFixed(2);
}

export function CollectingLibraries({ paperId }: { paperId: string }) {
  const [open, setOpen] = useState(false);
  const { data } = useQuery({
    queryKey: ['paper-libraries', paperId],
    queryFn: () => api.getCollectingLibraries(paperId),
    retry: false,
  });

  const libs: CollectingLibrary[] = data ?? [];
  if (libs.length === 0) return null;

  const shown = libs.slice(0, 2);
  const rest = libs.length - shown.length;

  return (
    <>
      <span className="row gap6 wrap" style={{ alignItems: 'center' }}>
        <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>
          {tr('已收录', 'In')}
        </span>
        {shown.map((l) => (
          <span
            key={l.library_id}
            className="pill sm"
            style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}
            title={tr(`相关度 ${scoreText(l.relevance_score)}`, `relevance ${scoreText(l.relevance_score)}`)}
          >
            {l.name}
          </span>
        ))}
        <button
          type="button"
          className="btn btn-ghost sm"
          style={{ padding: '0 6px', height: 22, fontSize: 11 }}
          onClick={() => setOpen(true)}
        >
          {rest > 0 ? tr(`还有 ${rest} 个`, `+${rest} more`) : tr('详情', 'Details')}
        </button>
      </span>

      {open && (
        <Modal
          open
          title={tr(`收录了这篇论文的文献库（${libs.length}）`, `Libraries holding this paper (${libs.length})`)}
          onClose={() => setOpen(false)}
        >
          <div className="col gap8" style={{ minWidth: 320 }}>
            {libs.map((l) => (
              <div
                key={l.library_id}
                className="row"
                style={{ justifyContent: 'space-between', alignItems: 'center', gap: 12 }}
              >
                <span className="row gap6" style={{ minWidth: 0 }}>
                  <Icon name="layers" size={12} style={{ color: 'var(--text-3)' }} />
                  <span className="ellipsis" style={{ fontSize: 13 }}>{l.name}</span>
                  {!l.is_public && (
                    <span className="pill sm" style={{ background: 'var(--surface-3)', fontSize: 10 }}>
                      {tr('个人', 'personal')}
                    </span>
                  )}
                </span>
                <span className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)' }}>
                  {tr('相关度', 'relevance')} {scoreText(l.relevance_score)}
                </span>
              </div>
            ))}
          </div>
        </Modal>
      )}
    </>
  );
}
