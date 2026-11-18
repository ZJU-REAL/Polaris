import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { ErrorState, LoadingState } from '../../components/ui/EmptyState';
import { api, type IdeaEvidence as IdeaEvidenceItem } from '../../lib/api';
import { errorText } from '../../lib/errors';
import { tr } from '../../lib/i18n';

/* ============================================================
   一个想法的文献依据，按立场分两组：支持 / 反对。每条是「哪句论断 + 哪篇论文」，
   论文点开就是阅读页。尚无依据的部分写在想法正文里，这里只给一个去看的链接。
   展开时才取数（想法详情那份缓存）。
   ============================================================ */

/** 假设探索写入的依据比通用结构多两个字段。 */
type StancedEvidence = IdeaEvidenceItem & { stance?: string | null; snippet?: string | null };

export function IdeaEvidence({ ideaId, unsupported }: { ideaId: string; unsupported: number }) {
  const query = useQuery({
    queryKey: ['idea', ideaId],
    queryFn: () => api.getIdea(ideaId),
    staleTime: 60_000,
    retry: false,
  });

  if (query.isLoading) return <LoadingState compact rows={2} />;
  if (query.isError) {
    return (
      <ErrorState
        compact
        title={tr('无法加载依据，请重试', 'Couldn’t load the evidence. Try again.')}
        detail={errorText(query.error)}
        onRetry={() => void query.refetch()}
      />
    );
  }

  const items = (query.data?.evidence ?? []) as StancedEvidence[];
  const support = items.filter((e) => e.stance !== 'refute');
  const refute = items.filter((e) => e.stance === 'refute');

  const group = (label: string, list: StancedEvidence[]) =>
    list.length > 0 && (
      <div className="dr-ev-group">
        <div className="dr-ev-label">{label}</div>
        <ul>
          {list.map((e, i) => (
            <li key={`${e.paper_id ?? e.title}-${i}`}>
              <span className="dr-ev-why">{e.why}</span>
              {e.paper_id ? (
                <Link to={`/papers/${e.paper_id}/read`} className="dr-ev-paper" title={e.title}>
                  {e.title}
                </Link>
              ) : (
                <span className="dr-ev-paper" title={e.title}>
                  {e.title}
                </span>
              )}
            </li>
          ))}
        </ul>
      </div>
    );

  return (
    <div className="dr-ev">
      {group(tr('支持', 'Supporting'), support)}
      {group(tr('反对', 'Against'), refute)}
      {items.length === 0 && <div className="dr-ev-empty">{tr('没有文献依据', 'No evidence from the papers')}</div>}
      {unsupported > 0 && (
        <div className="dr-ev-more">
          {tr(`另有 ${unsupported} 处论断尚无文献依据，`, `${unsupported} ${unsupported === 1 ? 'claim has' : 'claims have'} no evidence yet. `)}
          <Link to={`/ideas/${ideaId}`}>{tr('在想法详情中查看', 'See them in the idea')}</Link>
        </div>
      )}
    </div>
  );
}
