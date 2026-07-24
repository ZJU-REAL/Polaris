import { useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Icon, type IconName } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { PipelineFlow, type PipelineStage } from '../../components/ui/PipelineFlow';
import { StatCard, type StatCardProps } from '../../components/ui/StatCard';
import { StatusPill } from '../../components/ui/StatusPill';
import { ScoreRing } from '../../components/ui/ScoreRing';
import { Delta } from '../../components/ui/Delta';
import { gateTitle, gateDesc, gateKindLabel } from '../../components/ui/GateCard';
import { useShell } from '../../app/AppShell';
import { topicPath, useProject } from '../../app/project';
import { fmtTime } from '../../lib/format';
import { api, type ActivityRead, type DirectionLibrarySummary, type GateRead, type StatsRead } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { compositeOf } from '../forge/ideaShared';

/** 端到端流水线各阶段的真实计数（stats 未就绪时显示 —）；path 带当前课题前缀；
    stuckKey = 当前卡住（还没有产出）的阶段，进度漏斗高亮它。 */
function buildPipelineStages(
  stats: StatsRead | undefined,
  pid: string | null,
  stuckKey: string | null,
  litPath: string,
): PipelineStage[] {
  const stages: PipelineStage[] = [
    { key: 'wiki', path: litPath, no: '00', icon: 'book', zh: '文献', en: 'Literature', count: stats?.papers_total ?? null },
    { key: 'forge', path: topicPath(pid, 'forge'), no: '01', icon: 'bulb', zh: '想法生成', en: 'Idea Forge', count: stats?.ideas_candidate ?? null },
    { key: 'review', path: topicPath(pid, 'review'), no: '02', icon: 'scale', zh: '想法评审', en: 'Idea Review', count: stats?.ideas_under_review ?? null },
    {
      key: 'experiment',
      path: topicPath(pid, 'experiment'),
      no: '03',
      icon: 'flask',
      zh: '实验搭建',
      en: 'Experiment Lab',
      count: stats?.experiments_active ?? null,
      running: (stats?.experiments_running ?? 0) > 0,
    },
    { key: 'writer', path: topicPath(pid, 'writer'), no: '04', icon: 'pen', zh: '论文撰写', en: 'Paper Writer', count: stats?.manuscripts_total ?? null },
    { key: 'paper-review', path: topicPath(pid, 'paper-review'), no: '05', icon: 'shield', zh: '论文评审', en: 'Paper Review', count: stats?.manuscripts_under_review ?? null },
  ];
  return stages.map((s) => (s.key === stuckKey ? { ...s, stuck: true } : s));
}

// ============================================================
// 「下一步」checklist：纯前端按产出计数推导（不加后端端点）。
// 各阶段完成度做级联兜底：晚期产物存在即视为前置步骤已完成
// （如实验只能由晋级想法而来），避免 stats 只统计部分状态时误判。
// ============================================================

interface NextStep {
  key: string;
  done: boolean;
  /** 一句话说明（zh/en 在渲染处 tr） */
  zh: string;
  en: string;
  /** 跳转按钮文案 */
  actionZh: string;
  actionEn: string;
  path: string;
  /** 可选次链接（如「浏览全部文献库」）：仅当前步骤渲染 */
  altActionZh?: string;
  altActionEn?: string;
  altPath?: string;
}

interface PipelineProgress {
  hasPapers: boolean;
  hasIdeas: boolean;
  hasExperiments: boolean;
  hasManuscripts: boolean;
}

function buildNextSteps(
  progress: PipelineProgress,
  pid: string | null,
  linkedCount: number,
): NextStep[] {
  const { hasPapers, hasIdeas, hasExperiments, hasManuscripts } = progress;
  // 第 1 步「让课题有文献可用」：先按关联库数量分岔引导，最终以「并集里真有论文」为完成。
  //   关联 0 库 → 去关联/新建文献库（次链接浏览全部库）
  //   已关联但并集空 → 去「相关研究」从关联库里挑论文
  //   并集有论文 → 完成
  const linkStep: NextStep =
    linkedCount > 0
      ? {
          key: 'papers',
          done: hasPapers,
          zh: '从关联文献库里挑选论文，充实课题的「相关研究」',
          en: "Pick papers from the linked libraries to fill the topic's related work",
          actionZh: '去相关研究挑选论文',
          actionEn: 'Pick related work',
          path: topicPath(pid, 'research'),
        }
      : {
          key: 'papers',
          done: hasPapers,
          zh: '关联一个文献库作为课题的文献来源（也可以新建一个）',
          en: "Link a literature library as the topic's source (or create a new one)",
          actionZh: '去关联文献库',
          actionEn: 'Link a library',
          path: `/projects/${pid ?? ''}`,
          altActionZh: '浏览全部文献库',
          altActionEn: 'Browse all libraries',
          altPath: '/libraries',
        };
  return [
    linkStep,
    {
      key: 'ideas',
      done: hasIdeas,
      zh: '基于关联文献生成第一批研究想法',
      en: 'Generate your first batch of research ideas from the linked literature',
      actionZh: '去生成想法',
      actionEn: 'Generate ideas',
      path: topicPath(pid, 'forge'),
    },
    {
      key: 'experiments',
      done: hasExperiments,
      zh: '评审想法，把优胜者晋级为实验',
      en: 'Review ideas and promote the winners to experiments',
      actionZh: '去评审想法',
      actionEn: 'Review ideas',
      path: topicPath(pid, 'review'),
    },
    {
      key: 'manuscripts',
      done: hasManuscripts,
      zh: '根据实验结果开始撰写论文',
      en: 'Start writing the paper from your experiment results',
      actionZh: '去写论文',
      actionEn: 'Start writing',
      path: topicPath(pid, 'writer'),
    },
  ];
}

/** 新课题引导卡：全部完成时整卡隐藏（由调用方判断）。 */
function NextStepsCard({ steps, onNavigate }: { steps: NextStep[]; onNavigate: (path: string) => void }) {
  const currentKey = steps.find((s) => !s.done)?.key ?? null;
  const doneCount = steps.filter((s) => s.done).length;
  return (
    <div className="card card-pad" style={{ marginBottom: 24, borderColor: 'var(--accent-soft-2)' }}>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 14 }}>
        <span className="section-h">
          <Icon name="sparkle" size={15} style={{ color: 'var(--accent)' }} />
          {tr('下一步', 'Next steps')}
        </span>
        <span className="pill" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
          {doneCount}/{steps.length} {tr('已完成', 'done')}
        </span>
      </div>
      <div className="col gap8">
        {steps.map((s) => {
          const isCurrent = s.key === currentKey;
          return (
            <div
              key={s.key}
              className="row gap10"
              style={{
                padding: '9px 12px',
                borderRadius: 10,
                background: isCurrent ? 'var(--accent-soft)' : 'transparent',
                alignItems: 'center',
              }}
            >
              <span
                style={{
                  width: 20,
                  height: 20,
                  borderRadius: '50%',
                  flexShrink: 0,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  background: s.done ? 'var(--ok-bg)' : isCurrent ? 'var(--accent)' : 'var(--surface-2)',
                  color: s.done ? 'var(--ok-tx)' : isCurrent ? '#fff' : 'var(--text-4)',
                  border: s.done || isCurrent ? 'none' : '1px solid var(--border-strong)',
                }}
              >
                {s.done ? <Icon name="check" size={12} /> : isCurrent ? <Icon name="arrow" size={11} /> : null}
              </span>
              <span
                style={{
                  flex: 1,
                  fontSize: 13,
                  lineHeight: 1.5,
                  color: s.done ? 'var(--text-3)' : isCurrent ? 'var(--text)' : 'var(--text-3)',
                  fontWeight: isCurrent ? 650 : 450,
                }}
              >
                {tr(s.zh, s.en)}
              </span>
              {isCurrent && (
                <div className="row gap8" style={{ flexShrink: 0 }}>
                  {s.altPath && (
                    <button className="btn btn-ghost sm" onClick={() => onNavigate(s.altPath!)}>
                      {tr(s.altActionZh ?? '', s.altActionEn ?? '')}
                    </button>
                  )}
                  <button className="btn btn-primary sm" onClick={() => onNavigate(s.path)}>
                    {tr(s.actionZh, s.actionEn)}
                    <Icon name="arrow" size={12} />
                  </button>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** 当前重点想法：取 leaderboard 第一名（真实数据）；无则空状态引导。 */
function FeaturedIdeaCard({ pid }: { pid: string | null }) {
  const navigate = useNavigate();
  const leaderboardQuery = useQuery({
    queryKey: ['leaderboard', pid],
    queryFn: () => api.getLeaderboard(pid!),
    enabled: !!pid,
    retry: false,
  });
  const idea = leaderboardQuery.data?.[0];

  if (!idea) {
    return (
      <div className="card card-pad" style={{ background: 'linear-gradient(135deg, var(--surface) 60%, var(--accent-soft) 200%)' }}>
        <div className="row" style={{ justifyContent: 'space-between', marginBottom: 12 }}>
          <span className="pill" style={{ background: 'var(--accent)', color: '#fff' }}>
            <Icon name="sparkle" size={12} />
            {tr('当前重点想法', 'Featured idea')}
          </span>
        </div>
        <div style={{ fontSize: 13, color: 'var(--text-2)', lineHeight: 1.6 }}>
          {leaderboardQuery.isLoading
            ? tr('加载想法排行榜…', 'Loading idea leaderboard…')
            : leaderboardQuery.isError
              ? tr('暂时无法加载想法排行榜。', 'Idea leaderboard is unavailable right now.')
              : tr('候选池还是空的，先运行一次想法生成。', 'The candidate pool is empty — run idea generation first.')}
        </div>
        {!leaderboardQuery.isLoading && (
          <button className="btn btn-ghost sm" style={{ marginTop: 14 }} onClick={() => navigate(topicPath(pid, 'forge'))}>
            <Icon name="bulb" size={13} />
            {tr('前往想法生成', 'Go to Idea Forge')}
          </button>
        )}
      </div>
    );
  }

  const composite = compositeOf(idea.scores);
  return (
    <div
      className="card card-pad hoverable"
      onClick={() => navigate(`/ideas/${idea.id}`)}
      style={{ background: 'linear-gradient(135deg, var(--surface) 60%, var(--accent-soft) 200%)' }}
    >
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 12 }}>
        <span className="pill" style={{ background: 'var(--accent)', color: '#fff' }}>
          <Icon name="sparkle" size={12} />
          {tr('当前重点想法 · Elo 榜首', 'Featured idea · Elo leader')}
        </span>
        <StatusPill status={idea.status} sm />
      </div>
      <div style={{ fontSize: 16, fontWeight: 680, letterSpacing: '-0.01em', lineHeight: 1.35 }}>{idea.title}</div>
      <div
        style={{
          fontSize: 12,
          color: 'var(--text-3)',
          marginTop: 4,
          display: '-webkit-box',
          WebkitLineClamp: 2,
          WebkitBoxOrient: 'vertical',
          overflow: 'hidden',
        }}
      >
        {idea.summary}
      </div>
      <div className="row gap16" style={{ marginTop: 16, alignItems: 'center' }}>
        {composite !== null && <ScoreRing value={composite} label="composite" />}
        <div>
          <div style={{ fontSize: 11, color: 'var(--text-3)' }}>Elo rating</div>
          <div className="row" style={{ alignItems: 'baseline', gap: 8 }}>
            <span className="mono" style={{ fontSize: 22, fontWeight: 700 }}>{Math.round(idea.elo_rating)}</span>
            <Delta>{tr(`${idea.wins}/${idea.matches} 胜`, `${idea.wins}/${idea.matches} wins`)}</Delta>
          </div>
        </div>
        <div style={{ marginLeft: 'auto', textAlign: 'right' }}>
          <div style={{ fontSize: 11, color: 'var(--text-3)' }}>{tr('查看详情与讨论 →', 'View details & discussion →')}</div>
        </div>
      </div>
    </div>
  );
}

/** activity kind → icon（后端 kind 为自由字符串，按关键字归类）。 */
function activityIcon(kind: string): IconName {
  const k = kind.toLowerCase();
  if (k.includes('gate')) return 'gate';
  if (k.includes('ingest') || k.includes('wiki') || k.includes('paper')) return 'book';
  if (k.includes('idea') || k.includes('forge')) return 'bulb';
  if (k.includes('experiment') || k.includes('run')) return 'flask';
  if (k.includes('voyage')) return 'compass';
  return 'bell';
}

/** 今天 → HH:mm，否则 MM-DD（活动流左栏窄）。 */
function fmtClock(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const p = (n: number) => String(n).padStart(2, '0');
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  return sameDay ? `${p(d.getHours())}:${p(d.getMinutes())}` : `${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function ActivityFeed({ activities, error }: { activities: ActivityRead[]; error: boolean }) {
  return (
    <div className="card" style={{ overflow: 'hidden' }}>
      <div className="card-pad row" style={{ paddingBottom: 12, justifyContent: 'space-between' }}>
        <span className="section-h">
          <Icon name="clock" size={15} style={{ color: 'var(--accent)' }} />
          {tr('近期活动', 'Activity')}
        </span>
      </div>
      <div style={{ padding: '0 6px 8px' }}>
        {error ? (
          <div className="empty" style={{ padding: 18 }}>{tr('无法加载活动（后端不可用）', 'Failed to load activity (backend unavailable)')}</div>
        ) : activities.length === 0 ? (
          <div className="empty" style={{ padding: 18 }}>{tr('暂无活动 — 先关联文献库或生成想法', 'No activity yet — link a library or generate ideas')}</div>
        ) : (
          activities.map((a) => {
            const gate = a.kind.toLowerCase().includes('gate');
            return (
              <div key={a.id} className="row gap12" style={{ padding: '9px 16px', alignItems: 'flex-start' }}>
                <div
                  style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-3)', width: 36, flexShrink: 0, paddingTop: 1 }}
                  title={fmtTime(a.created_at)}
                >
                  {fmtClock(a.created_at)}
                </div>
                <div
                  style={{
                    width: 24,
                    height: 24,
                    borderRadius: 7,
                    flexShrink: 0,
                    background: gate ? 'var(--accent-soft)' : 'var(--surface-2)',
                    color: gate ? 'var(--accent)' : 'var(--text-3)',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                  }}
                >
                  <Icon name={activityIcon(a.kind)} size={13} />
                </div>
                <div style={{ flex: 1, fontSize: 12.5, color: 'var(--text)', lineHeight: 1.45, paddingTop: 2 }}>
                  {a.message}
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

function GatePreview({ gates, gatesError, openGates }: {
  gates: GateRead[];
  gatesError: boolean;
  openGates: (id?: string | null) => void;
}) {
  return (
    <div className="card" style={{ overflow: 'hidden', borderColor: gates.length ? 'var(--accent-soft-2)' : 'var(--border)' }}>
      <div className="card-pad row" style={{ justifyContent: 'space-between', paddingBottom: 14 }}>
        <span className="section-h">
          <Icon name="gate" size={15} style={{ color: 'var(--accent)' }} />
          {tr('人工审批 · 审批中心', 'Approvals')}
        </span>
        <span className="pill" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
          {gates.length} {tr('待处理', 'pending')}
        </span>
      </div>
      <div style={{ padding: '0 14px 14px', display: 'flex', flexDirection: 'column', gap: 8 }}>
        {gatesError ? (
          <div className="empty" style={{ padding: 18 }}>{tr('无法加载审批列表（后端不可用）', 'Failed to load approvals (backend unavailable)')}</div>
        ) : gates.length === 0 ? (
          <div className="empty" style={{ padding: 18 }}>{tr('没有待处理的审批', 'No pending approvals')}</div>
        ) : (
          gates.map((g) => {
            const desc = gateDesc(g);
            return (
              <div
                key={g.id}
                className="hoverable"
                onClick={() => openGates(g.id)}
                style={{
                  border: '0.5px solid var(--border)',
                  borderRadius: 10,
                  padding: '12px 14px',
                  background: 'var(--surface-2)',
                }}
              >
                <div className="row" style={{ justifyContent: 'space-between' }}>
                  <span style={{ fontSize: 13, fontWeight: 650 }}>{gateTitle(g)}</span>
                  <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>
                    {gateKindLabel(g.kind)}
                  </span>
                </div>
                {desc && <div style={{ fontSize: 11.5, color: 'var(--text-2)', marginTop: 5, lineHeight: 1.45 }}>{desc}</div>}
                <div className="row gap8" style={{ marginTop: 10 }}>
                  <button
                    className="btn btn-primary sm"
                    onClick={(e) => {
                      e.stopPropagation();
                      openGates(g.id);
                    }}
                  >
                    <Icon name="check" size={13} />
                    {tr('审批', 'Review')}
                  </button>
                  <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', marginLeft: 'auto' }}>
                    {fmtTime(g.created_at)}
                  </span>
                </div>
              </div>
            );
          })
        )}
        <button className="btn btn-soft" onClick={() => openGates(null)} style={{ justifyContent: 'center' }}>
          {tr('查看全部审批记录', 'View all approval records')}
        </button>
      </div>
    </div>
  );
}

/** 课题文献概览：关联文献库数 + 相关研究书架篇数，入口→相关研究 / 关联库管理。
    shelfCount 无接口时传 null（只显示关联库数 + 入口）。 */
function LiteratureCard({
  pid,
  linkedCount,
  shelfCount,
  onNavigate,
}: {
  pid: string | null;
  linkedCount: number;
  shelfCount: number | null;
  onNavigate: (path: string) => void;
}) {
  const hasLinked = linkedCount > 0;
  return (
    <div className="card card-pad" style={{ marginBottom: 24 }}>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 12 }}>
        <span className="section-h">
          <Icon name="book" size={15} style={{ color: 'var(--accent)' }} />
          {tr('相关研究', 'Related work')}
        </span>
        <button className="btn btn-ghost sm" onClick={() => onNavigate(`/projects/${pid ?? ''}`)}>
          <Icon name="link" size={12} />
          {tr('管理关联库', 'Linked libraries')}
        </button>
      </div>
      <div className="row gap16" style={{ alignItems: 'baseline', marginBottom: 14 }}>
        <div>
          <span className="mono" style={{ fontSize: 24, fontWeight: 700, letterSpacing: '-0.02em' }}>{linkedCount}</span>
          <span style={{ fontSize: 12, color: 'var(--text-3)', marginLeft: 6 }}>{tr('个关联文献库', 'linked libraries')}</span>
        </div>
        {shelfCount !== null && (
          <div>
            <span className="mono" style={{ fontSize: 24, fontWeight: 700, letterSpacing: '-0.02em' }}>{shelfCount}</span>
            <span style={{ fontSize: 12, color: 'var(--text-3)', marginLeft: 6 }}>{tr('篇相关研究', 'in related work')}</span>
          </div>
        )}
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-3)', lineHeight: 1.5, marginBottom: 14 }}>
        {hasLinked
          ? tr('课题语料 = 关联文献库的并集；抓取论文在文献库里进行。', 'The topic corpus is the union of its linked libraries — papers are fetched inside the libraries.')
          : tr('课题还没关联文献库；关联后即可从库里挑论文进「相关研究」。', 'No linked libraries yet — link one to pick papers into related work.')}
      </div>
      <div className="row gap8">
        {hasLinked ? (
          <button className="btn btn-primary sm" onClick={() => onNavigate(topicPath(pid, 'research'))}>
            <Icon name="book" size={13} />
            {tr('去相关研究', 'Open related work')}
          </button>
        ) : (
          <>
            <button className="btn btn-primary sm" onClick={() => onNavigate(`/projects/${pid ?? ''}`)}>
              <Icon name="link" size={13} />
              {tr('去关联文献库', 'Link a library')}
            </button>
            <button className="btn btn-ghost sm" onClick={() => onNavigate('/libraries')}>
              {tr('浏览全部文献库', 'Browse all libraries')}
            </button>
          </>
        )}
      </div>
    </div>
  );
}

/** GET /projects/{pid}/stats → 4 张指标卡（后端不可用时显示 —）。 */
function buildStatCards(stats: StatsRead | undefined, pendingGatesCount: number): StatCardProps[] {
  return [
    {
      icon: 'book',
      label: tr('关联文献', 'Linked papers'),
      en: 'Linked papers',
      value: stats ? stats.papers_total : '—',
      sub: stats ? `+${stats.papers_today} ${tr('今日', 'today')}` : undefined,
    },
    {
      icon: 'refresh',
      label: tr('今日新增', 'New today'),
      en: 'New today',
      value: stats ? stats.papers_today : '—',
      sub: tr('篇论文', 'papers'),
    },
    {
      icon: 'bulb',
      label: tr('候选想法', 'Idea candidates'),
      en: 'Idea candidates',
      value: stats ? stats.ideas_candidate : '—',
      sub: tr('想法池', 'in the pool'),
    },
    {
      icon: 'gate',
      label: tr('待处理审批', 'Pending approvals'),
      en: 'Pending approvals',
      value: stats ? stats.gates_pending : pendingGatesCount,
      sub: tr('人工审批', 'need review'),
      accent: true,
    },
  ];
}

export function DashboardPage() {
  const navigate = useNavigate();
  const { pendingGates, gatesError, openGates } = useShell();
  const { currentProject, currentProjectId } = useProject();

  const statsQuery = useQuery({
    queryKey: ['stats', currentProjectId],
    queryFn: () => api.getStats(currentProjectId!),
    enabled: !!currentProjectId,
    retry: false,
    refetchInterval: 60_000,
  });

  // stats 只统计部分状态（想法只算 candidate/under_review、实验只算未终态），
  // checklist 需要「有没有任何产出」——用既有列表接口补齐口径（缓存与各阶段页共享）
  const ideasQuery = useQuery({
    queryKey: ['ideas', currentProjectId, 'all'],
    queryFn: () => api.listIdeas(currentProjectId!),
    enabled: !!currentProjectId,
    retry: false,
  });
  const experimentsQuery = useQuery({
    queryKey: ['experiments', currentProjectId],
    queryFn: () => api.listExperiments(currentProjectId!),
    enabled: !!currentProjectId,
    retry: false,
  });
  // 课题关联的文献库（P7）：语料 = 关联库并集；键与课题设置页共享缓存
  const sourceLibrariesQuery = useQuery<DirectionLibrarySummary[]>({
    queryKey: ['sourceLibraries', currentProjectId],
    queryFn: () => api.getSourceLibraries(currentProjectId!),
    enabled: !!currentProjectId,
    retry: false,
  });
  // 「相关研究」书架条目数（P5a）：键与相关研究页共享缓存
  const shelfIdsQuery = useQuery({
    queryKey: ['shelf-ids', currentProjectId],
    queryFn: () => api.listShelfIds(currentProjectId!),
    enabled: !!currentProjectId,
    retry: false,
  });
  const linkedCount = sourceLibrariesQuery.data?.length ?? 0;
  const shelfCount = shelfIdsQuery.data?.paper_ids.length ?? null;

  const stats = statsQuery.data;
  // 级联兜底：晚期产物存在 ⇒ 前置阶段必然完成（实验只能由晋级想法而来）
  const hasManuscripts = (stats?.manuscripts_total ?? 0) > 0;
  const hasExperiments =
    (experimentsQuery.data?.length ?? 0) > 0 || (stats?.experiments_active ?? 0) > 0 || hasManuscripts;
  const hasIdeas =
    (ideasQuery.data?.length ?? 0) > 0 ||
    (stats ? stats.ideas_candidate + stats.ideas_under_review > 0 : false) ||
    hasExperiments;
  const hasPapers = (stats?.papers_total ?? 0) > 0;
  const progress: PipelineProgress = { hasPapers, hasIdeas, hasExperiments, hasManuscripts };

  // stats/列表还没就绪时不渲染 checklist、不高亮漏斗（避免误判闪烁）
  const checklistReady = !!stats && !ideasQuery.isLoading && !experimentsQuery.isLoading && !sourceLibrariesQuery.isLoading;

  // 流水线漏斗高亮：第一个还没有产出的阶段（全流程有产出但暂无稿件送审时提示论文评审）
  const stuckKey = !checklistReady || !stats
    ? null
    : !hasPapers
      ? 'wiki'
      : !hasIdeas
        ? 'forge'
        : !hasExperiments
          ? 'review'
          : !hasManuscripts
            ? 'writer'
            : stats.manuscripts_under_review === 0
              ? 'paper-review'
              : null;

  const nextSteps = buildNextSteps(progress, currentProjectId, linkedCount);
  // 全流程都有产出 → 整卡隐藏
  const showChecklist = checklistReady && nextSteps.some((s) => !s.done);

  const statCards = buildStatCards(stats, pendingGates.length);
  const stages = buildPipelineStages(
    stats,
    currentProjectId,
    stuckKey,
    linkedCount > 0 ? topicPath(currentProjectId, 'research') : `/projects/${currentProjectId ?? ''}`,
  );

  return (
    <div className="page fadeup">
      <PageHead
        eyebrow="Polaris · Autonomous Research"
        title={tr('工作台', 'Workbench')}
        right={
          <>
            <button className="btn btn-ghost" onClick={() => navigate(topicPath(currentProjectId, 'voyages'))}>
              <Icon name="compass" size={15} />
              {tr('任务', 'Tasks')}
            </button>
            <button className="btn btn-primary" onClick={() => navigate(topicPath(currentProjectId, 'voyages'))}>
              <Icon name="play" size={14} />
              {tr('运行今日循环', "Run today's loop")}
            </button>
          </>
        }
      />

      {showChecklist && <NextStepsCard steps={nextSteps} onNavigate={navigate} />}

      <PipelineFlow
        stages={stages}
        directionLabel={currentProject?.name ?? 'Polaris'}
        onNavigate={navigate}
      />

      <LiteratureCard
        pid={currentProjectId}
        linkedCount={linkedCount}
        shelfCount={shelfCount}
        onNavigate={navigate}
      />

      <div className="row gap16" style={{ marginBottom: 24 }}>
        {statCards.map((s) => (
          <StatCard key={s.label} {...s} />
        ))}
      </div>

      <div className="row gap20" style={{ alignItems: 'flex-start' }}>
        <div className="col gap20" style={{ flex: 1.5, minWidth: 0 }}>
          <FeaturedIdeaCard pid={currentProjectId} />
          <ActivityFeed
            activities={statsQuery.data?.recent_activities ?? []}
            error={statsQuery.isError}
          />
        </div>
        <div style={{ flex: 1, minWidth: 0, position: 'sticky', top: 0 }}>
          <GatePreview gates={pendingGates} gatesError={gatesError} openGates={openGates} />
        </div>
      </div>
    </div>
  );
}
