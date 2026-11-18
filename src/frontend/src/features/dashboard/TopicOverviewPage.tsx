import { Link, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { topicPath, useProject } from '../../app/project';
import { api, type DirectionLibrarySummary } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { saveDraft } from '../deepresearch/DirectionBox';
import '../deepresearch/deepresearch.css';

/* ============================================================
   /t/:id —— 没有研究进展的课题的概览。

   从「研究」首页开始的课题，概览就是它的时间线（/research/:id）。这一页只给之前按
   老办法建的课题用：它们没有时间线，但想法、实验、稿件都在。这里说清楚两件事——
   这个课题里现在有什么（各一行，点了就到）、以及怎么让 Polaris 替它跑一次研究。
   ============================================================ */

function count(n: number | undefined, zh: string, enOne: string, enMany = `${enOne}s`): string | null {
  if (n === undefined) return null;
  return tr(`${n} ${zh}`, `${n} ${n === 1 ? enOne : enMany}`);
}

export function TopicOverviewPage() {
  const navigate = useNavigate();
  const { currentProject, currentProjectId: pid } = useProject();

  const statsQuery = useQuery({
    queryKey: ['stats', pid],
    queryFn: () => api.getStats(pid!),
    enabled: !!pid,
    retry: false,
  });
  // 想法 / 实验的总数：stats 只数其中几种状态，这里要的是「一共有多少」（缓存与各页共用）
  const ideasQuery = useQuery({
    queryKey: ['ideas', pid, 'all'],
    queryFn: () => api.listIdeas(pid!),
    enabled: !!pid,
    retry: false,
  });
  const experimentsQuery = useQuery({
    queryKey: ['experiments', pid],
    queryFn: () => api.listExperiments(pid!),
    enabled: !!pid,
    retry: false,
  });
  const librariesQuery = useQuery<DirectionLibrarySummary[]>({
    queryKey: ['sourceLibraries', pid],
    queryFn: () => api.getSourceLibraries(pid!),
    enabled: !!pid,
    retry: false,
  });

  const stats = statsQuery.data;
  const libraries = librariesQuery.data;
  const ideas = ideasQuery.data;
  const picked = ideas?.filter((i) => i.status === 'promoted').length ?? 0;
  const statement = currentProject?.statement?.trim() ?? '';
  // 「开始研究」用的那句方向：有说明用说明，没有就用课题名
  const direction = statement || currentProject?.name || '';

  const firstLibrary = libraries?.[0];
  const libraryHref =
    libraries && libraries.length === 1 && firstLibrary ? `/libraries/${firstLibrary.id}` : topicPath(pid, 'settings');
  const papersText = libraries
    ? libraries.length === 0
      ? tr('还没有关联文献库', 'No library linked yet')
      : [
          libraries.length === 1 && firstLibrary ? firstLibrary.name : count(libraries.length, '个文献库', 'library', 'libraries'),
          count(stats?.papers_total, '篇论文', 'paper'),
        ]
          .filter(Boolean)
          .join(' · ')
    : '';

  const rows: { key: string; name: string; text: string; to: string; action: string }[] = [
    {
      key: 'papers',
      name: tr('文献', 'Papers'),
      text: papersText,
      to: libraryHref,
      action: libraries && libraries.length === 1 ? tr('打开文献库', 'Open library') : tr('关联文献库', 'Link a library'),
    },
    {
      key: 'ideas',
      name: tr('想法', 'Ideas'),
      text: ideas
        ? ideas.length === 0
          ? tr('还没有想法', 'No ideas yet')
          : [count(ideas.length, '个想法', 'idea'), picked > 0 ? tr(`已选定 ${picked} 个`, `${picked} picked`) : null]
              .filter(Boolean)
              .join(' · ')
        : '',
      to: topicPath(pid, 'forge'),
      action: tr('打开', 'Open'),
    },
    {
      key: 'experiments',
      name: tr('实验', 'Experiments'),
      text: experimentsQuery.data
        ? experimentsQuery.data.length === 0
          ? tr('还没有实验', 'No experiments yet')
          : (count(experimentsQuery.data.length, '个实验', 'experiment') ?? '')
        : '',
      to: topicPath(pid, 'experiment'),
      action: tr('打开', 'Open'),
    },
    {
      key: 'writing',
      name: tr('写作', 'Writing'),
      text: stats
        ? stats.manuscripts_total === 0
          ? tr('还没有稿件', 'No manuscripts yet')
          : (count(stats.manuscripts_total, '篇稿件', 'manuscript') ?? '')
        : '',
      to: topicPath(pid, 'writer'),
      action: tr('打开', 'Open'),
    },
  ];

  return (
    <div className="page fadeup">
      <div className="dr-run-body">
        {statement && <p className="dr-run-direction" style={{ margin: '0 0 var(--sp-5)' }}>{statement}</p>}

        <div className="dr-attn" style={{ marginTop: 0 }}>
          <div className="dr-attn-text">{tr('这个课题还没有研究进展', 'This topic has no research run yet')}</div>
          <div className="dr-attn-sub">
            {tr(
              '用它的说明开始一次研究：Polaris 检索文献、寻找有文献依据的假设，再列出想法。会新建一个课题，这里的内容都保留。',
              'Start a research run from its description: Polaris surveys the literature, looks for supported hypotheses and lists ideas. This creates a new topic; everything here stays.',
            )}
          </div>
          <div className="dr-attn-actions">
            <button
              type="button"
              className="btn btn-primary sm"
              disabled={!direction}
              onClick={() => {
                // 方向先放进首页那个框里，由用户确认后再开始（可以改一改、调范围）
                saveDraft(direction);
                navigate('/research');
              }}
            >
              {tr('用这个方向开始研究', 'Start research on this')}
            </button>
          </div>
        </div>

        <div className="group-label" style={{ margin: 'var(--sp-6) 0 var(--sp-2)' }}>
          {tr('课题里现有的内容', 'In this topic')}
        </div>
        <div className="dr-next">
          {rows.map((r) => (
            <div key={r.key} className="dr-next-row">
              <div className="dr-next-text">
                <div className="dr-next-name">{r.name}</div>
                <div className="dr-next-hint">{r.text || tr('加载中…', 'Loading…')}</div>
              </div>
              <Link className="btn btn-ghost sm" to={r.to}>
                {r.action}
              </Link>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
