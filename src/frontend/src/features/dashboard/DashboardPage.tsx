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
import { api, type ActivityRead, type GateRead, type StatsRead } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { compositeOf } from '../forge/ideaShared';

/** 端到端流水线各阶段的真实计数（stats 未就绪时显示 —）；path 带当前课题前缀；
    stuckKey = 当前卡住（还没有产出）的阶段，进度漏斗高亮它。 */
function buildPipelineStages(stats: StatsRead | undefined, pid: string | null, stuckKey: string | null): PipelineStage[] {
  const stages: PipelineStage[] = [
    { key: 'wiki', path: topicPath(pid, 'wiki'), no: '00', icon: 'book', zh: '文献追踪', en: 'Research Wiki', count: stats?.papers_total ?? null },
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
}

interface PipelineProgress {
  hasPapers: boolean;
  hasIdeas: boolean;
  hasExperiments: boolean;
  hasManuscripts: boolean;
}

function buildNextSteps(progress: PipelineProgress, pid: string | null, hasKeywords: boolean): NextStep[] {
  const { hasPapers, hasIdeas, hasExperiments, hasManuscripts } = progress;
  return [
    {
      key: 'papers',
      done: hasPapers,
      zh: '配置文献追踪并启动初始建库，建立课题知识库',
      en: 'Set up literature tracking and run the initial library build',
      actionZh: hasKeywords ? '去启动初始建库' : '先去课题设置里配置关键词',
      actionEn: hasKeywords ? 'Run the initial build' : 'Configure terms in topic settings first',
      path: hasKeywords ? topicPath(pid, 'wiki?tab=ingest') : `/projects/${pid ?? ''}`,
    },
    {
      key: 'ideas',
      done: hasIdeas,
      zh: '基于知识库生成第一批研究想法',
      en: 'Generate your first batch of research ideas from the library',
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
                <button className="btn btn-primary sm" style={{ flexShrink: 0 }} onClick={() => onNavigate(s.path)}>
                  {tr(s.actionZh, s.actionEn)}
                  <Icon name="arrow" size={12} />
                </button>
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
          <div className="empty" style={{ padding: 18 }}>{tr('暂无活动 — 运行一次文献初始建库试试', 'No activity yet — try running an initial library build')}</div>
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

/** GET /projects/{pid}/stats → 4 张指标卡（后端不可用时显示 —）。 */
function buildStatCards(stats: StatsRead | undefined, pendingGatesCount: number): StatCardProps[] {
  return [
    {
      icon: 'book',
      label: tr('知识库论文', 'Papers in vault'),
      en: 'Papers in vault',
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
  const checklistReady = !!stats && !ideasQuery.isLoading && !experimentsQuery.isLoading;

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

  const includeTerms = currentProject?.definition?.keywords?.include ?? [];
  const nextSteps = buildNextSteps(progress, currentProjectId, includeTerms.length > 0);
  // 全流程都有产出 → 整卡隐藏
  const showChecklist = checklistReady && nextSteps.some((s) => !s.done);

  const statCards = buildStatCards(stats, pendingGates.length);
  const stages = buildPipelineStages(stats, currentProjectId, stuckKey);

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
