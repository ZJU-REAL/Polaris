import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { Icon } from '../../components/ui/Icon';
import { api } from '../../lib/api';
import { clampLines } from '../../lib/clamp';
import { tr } from '../../lib/i18n';

/* ============================================================
   最近 7 天的文献库 / 论文热度。

   只出计数，不出「谁看的」——实验室里「某人在读什么」是敏感信息，而热度需要的
   只是总数。同一个人一小时内重复打开只算一次（后端去重），否则这张榜衡量的是
   谁刷新得最勤。

   没有数据时**整块不显示**：一张全是 0 的榜既没信息也占地方，还会让人以为统计坏了。
   ============================================================ */

function HotCard({
  title,
  rows,
  onOpen,
}: {
  title: string;
  rows: { id: string; label: string; views: number }[];
  onOpen: (id: string) => void;
}) {
  const max = Math.max(...rows.map((r) => r.views), 1);
  return (
    <div className="card card-pad" style={{ flex: 1, minWidth: 0 }}>
      <div className="section-h" style={{ marginBottom: 10 }}>
        <Icon name="chart" size={15} style={{ color: 'var(--accent)' }} />
        {title}
      </div>
      <div className="col gap8">
        {rows.map((row, i) => (
          <div
            key={row.id}
            className="hoverable"
            onClick={() => onOpen(row.id)}
            style={{ cursor: 'pointer', padding: '2px 4px', borderRadius: 6 }}
          >
            <div className="row gap8" style={{ alignItems: 'baseline' }}>
              <span className="mono" style={{ fontSize: 11, color: 'var(--text-4)', width: 14 }}>
                {i + 1}
              </span>
              <span style={{ flex: 1, minWidth: 0, fontSize: 12.5, ...clampLines(1) }} title={row.label}>
                {row.label}
              </span>
              <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>
                {row.views}
              </span>
            </div>
            {/* 一条细条：数字之间的差距比数字本身更说明问题 */}
            <div style={{ height: 3, background: 'var(--surface-3)', borderRadius: 2, marginTop: 4 }}>
              <div
                style={{
                  height: 3,
                  width: `${Math.round((row.views / max) * 100)}%`,
                  background: 'var(--accent)',
                  borderRadius: 2,
                }}
              />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export function HotLists() {
  const navigate = useNavigate();
  const { data } = useQuery({
    queryKey: ['lab-hot'],
    queryFn: () => api.getLabHot(7, 5),
    retry: false,
    staleTime: 60_000,
  });

  const libraries = data?.libraries ?? [];
  const papers = data?.papers ?? [];
  if (libraries.length === 0 && papers.length === 0) return null;

  return (
    <div className="row gap16 wrap" style={{ marginBottom: 20, alignItems: 'stretch' }}>
      {libraries.length > 0 && (
        <HotCard
          title={tr('7 天文献库热度', 'Libraries · last 7 days')}
          rows={libraries.map((l) => ({ id: l.id, label: l.name, views: l.views }))}
          onOpen={(id) => navigate(`/libraries/${id}`)}
        />
      )}
      {papers.length > 0 && (
        <HotCard
          title={tr('7 天论文热度', 'Papers · last 7 days')}
          rows={papers.map((p) => ({ id: p.id, label: p.title, views: p.views }))}
          onOpen={(id) => navigate(`/papers/${id}/read`)}
        />
      )}
    </div>
  );
}
