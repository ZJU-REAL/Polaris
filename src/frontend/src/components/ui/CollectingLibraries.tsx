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

export function CollectingLibraries({
  paperId,
  standalone = false,
}: {
  paperId: string;
  /** 独立成一行（详情面板用）：占整行、多摆几个、空态也出提示。
   *  默认是挤在顶部徽章行里的紧凑形态。 */
  standalone?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const { data } = useQuery({
    queryKey: ['paper-libraries', paperId],
    queryFn: () => api.getCollectingLibraries(paperId),
    retry: false,
  });

  const libs: CollectingLibrary[] = data ?? [];
  // 独立成行时，「没有任何库收录」本身是有用的信息，要说出来；挤在徽章行里则不占位
  if (libs.length === 0) {
    return standalone ? (
      <div className="row gap6" style={{ alignItems: 'center', marginTop: 8, paddingLeft: 2 }}>
        <Icon name="book" size={12} style={{ color: 'var(--text-4)' }} />
        <span style={{ fontSize: 11.5, color: 'var(--text-4)' }}>
          {tr('还没有文献库收录这篇', 'No library has collected this paper yet')}
        </span>
      </div>
    ) : null;
  }

  // 独立成行有整行宽度，可以多摆几个；挤在徽章行里只放两个
  const shown = libs.slice(0, standalone ? 6 : 2);
  const rest = libs.length - shown.length;

  return (
    <>
      <span
        className="row gap6 wrap"
        style={
          standalone
            ? { alignItems: 'center', marginTop: 8, paddingLeft: 2 }
            : { alignItems: 'center' }
        }
      >
        <span
          className={standalone ? 'row gap6' : 'mono'}
          style={
            standalone
              ? { fontSize: 11.5, color: 'var(--text-3)', alignItems: 'center' }
              : { fontSize: 10.5, color: 'var(--text-4)' }
          }
        >
          {standalone && <Icon name="book" size={12} style={{ color: 'var(--text-4)' }} />}
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
