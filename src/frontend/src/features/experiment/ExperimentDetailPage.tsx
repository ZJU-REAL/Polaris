import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { DotsMenu, type DotsMenuItem } from '../../components/ui/DotsMenu';
import { ErrorState, LoadingState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { Markdown } from '../../lib/markdown';
import { useShell } from '../../app/AppShell';
import { topicPath, useProject, useSyncTopic } from '../../app/project';
import { errorText } from '../../lib/errors';
import {
  api,
  ApiError,
  EXPERIMENT_TERMINAL,
  VOYAGE_TERMINAL,
  type EvalProtocol,
  type ExperimentCondition,
  type ExperimentContainer,
  type ExperimentDataset,
  type ExperimentDetail,
  type ExperimentPlan,
  type GateDecision,
} from '../../lib/api';
import { tr } from '../../lib/i18n';
import { useNow } from '../deepresearch/hooks';
import { InlineQuestion } from '../deepresearch/InlineQuestion';
import { elapsedText } from '../deepresearch/phase';
import { ParkedRunNotice } from '../voyages/shared/ParkedRunNotice';
import { byListOrder } from '../voyages/shared/stepUtils';
import { taskStatusLine, taskTone } from '../voyages/shared/taskText';
import { useVoyageChannel } from '../voyages/shared/useVoyageChannel';
import { openAskOf, useVoyageMessages } from '../voyages/shared/useVoyageMessages';
import { budgetText, HypChip } from './shared';
import { experimentSentence, experimentTone, fmtMetric, summarize, summaryLine } from './experimentView';
import { MemoryTab } from './MemoryTab';
import { ProgressTab } from './ProgressTab';
import { RunTab } from './RunTab';
import { CodeTab } from './CodeTab';
import { ExperimentFigures } from './ExperimentFigures';
import '../../app/DirectionNav.css';
import '../deepresearch/deepresearch.css';
import '../voyages/task.css';
import './experiment.css';

/* ============================================================
   /experiment/:id —— 一个实验的页面。和任务页同一种读法，不是控制台：

   · 顶上一句话：现在在做什么 / 在等你什么 / 结果如何，用了多久、跑到第几轮；
   · 需要你的时候（AI 提问、确认预算、出错停下），要做的事就在这句话下面；
   · 做完了先看结果：结论的几个数、假设成立没有、图表、报告、各轮记录；
   · 还在做就看进展：步骤一行一个，日志和机器状态默认收起；
   · 计划、代码、实验笔记各一个页签。

   以前默认页是「运行台」：模式徽章、节点地图、一直开着的深色终端，
   AI 的提问夹在日志里。
   ============================================================ */

type TabKey = 'progress' | 'results' | 'plan' | 'code' | 'notes';

/* 文案在渲染处 tr()，避免模块级求值不随语言切换 */
const TABS: { k: TabKey; zh: string; en: string }[] = [
  { k: 'progress', zh: '进展', en: 'Progress' },
  { k: 'results', zh: '结果', en: 'Results' },
  { k: 'plan', zh: '计划', en: 'Plan' },
  { k: 'code', zh: '代码', en: 'Code' },
  { k: 'notes', zh: '实验笔记', en: 'Notes' },
];

/* ---------------- 计划 ---------------- */

function planStepText(s: NonNullable<ExperimentPlan['steps']>[number]): string {
  if (typeof s === 'string') return s;
  return s.title ?? s.desc ?? s.description ?? JSON.stringify(s);
}

/* 实验类型 / 对照角色 → 大白话（模块级常量只存 zh/en，渲染处再 tr） */
const EXP_KIND_INFO: Record<string, { zh: string; en: string }> = {
  eval: { zh: '评测', en: 'Evaluation' },
  training: { zh: '训练', en: 'Training' },
  agent: { zh: '智能体', en: 'Agent' },
  analysis: { zh: '分析', en: 'Analysis' },
  other: { zh: '其他', en: 'Other' },
};

/** 在哪里运行：有镜像名 = 在容器里；没有 = 直接在机器上。 */
function environmentText(container: ExperimentContainer | null | undefined): string {
  const image = container && typeof container === 'object' && typeof container.image === 'string' ? container.image.trim() : '';
  if (!image) return tr('直接在机器上运行，不用容器', 'Directly on the machine, no container');
  const rawGpus = container?.gpus;
  const gpus = typeof rawGpus === 'string' ? rawGpus.trim() : typeof rawGpus === 'number' && Number.isFinite(rawGpus) ? String(rawGpus) : '';
  const shm = typeof container?.shm_size === 'string' ? container.shm_size.trim() : '';
  return [tr(`容器 ${image}`, `Container ${image}`), gpus ? `GPU ${gpus}` : '', shm ? tr(`共享内存 ${shm}`, `shared memory ${shm}`) : ''].filter(Boolean).join(' · ');
}

function evalRows(protocol: EvalProtocol): string[] {
  const out: string[] = [];
  if (typeof protocol.dataset === 'string' && protocol.dataset.trim()) out.push(tr(`数据集 ${protocol.dataset.trim()}`, `Dataset ${protocol.dataset.trim()}`));
  if (typeof protocol.split === 'string' && protocol.split.trim()) out.push(tr(`划分 ${protocol.split.trim()}`, `Split ${protocol.split.trim()}`));
  if (typeof protocol.metric === 'string' && protocol.metric.trim()) out.push(tr(`指标 ${protocol.metric.trim()}`, `Metric ${protocol.metric.trim()}`));
  if (typeof protocol.n_examples === 'number' && Number.isFinite(protocol.n_examples)) out.push(tr(`${protocol.n_examples} 个样本`, `${protocol.n_examples} examples`));
  if (typeof protocol.n_samples === 'number' && Number.isFinite(protocol.n_samples)) out.push(tr(`采样 ${protocol.n_samples} 次`, `${protocol.n_samples} samples each`));
  return out;
}

/** 假设判定依据：plan 回写的 evidence 优先，缺失时从各轮反思里取最近一轮的。 */
function hypothesisEvidence(exp: ExperimentDetail): Map<number, string> {
  const map = new Map<number, string>();
  const runs = [...(exp.runs ?? [])].sort((a, b) => a.seq - b.seq);
  for (const run of runs) {
    for (const u of run.reflection?.hypothesis_updates ?? []) {
      if (typeof u.index === 'number' && u.evidence && u.evidence.trim() !== '') map.set(u.index, u.evidence);
    }
  }
  (exp.plan?.hypotheses ?? []).forEach((h, i) => {
    if (h.evidence && h.evidence.trim() !== '') map.set(i, h.evidence);
  });
  return map;
}

function PlanTab({ exp }: { exp: ExperimentDetail }) {
  const plan = exp.plan;
  if (!plan) {
    return (
      <div className="task-empty" data-exp-tab="plan">
        {exp.status === 'planning' ? tr('AI 正在写实验计划…', 'The AI is writing the plan…') : tr('这个实验没有计划。', 'This experiment has no plan.')}
      </div>
    );
  }
  const pm = plan.primary_metric;
  const kind = plan.kind ? EXP_KIND_INFO[plan.kind] : undefined;
  const hyps = plan.hypotheses ?? [];
  const conditions: ExperimentCondition[] = plan.conditions ?? [];
  const datasets: ExperimentDataset[] = plan.datasets ?? [];
  const steps = plan.steps ?? [];
  const evalBits = plan.eval_protocol ? evalRows(plan.eval_protocol) : [];
  const estimate = plan.budget_estimate
    ? typeof plan.budget_estimate === 'string'
      ? plan.budget_estimate
      : Object.entries(plan.budget_estimate as Record<string, unknown>)
          .map(([k, v]) => `${k} ${String(v)}`)
          .join(' · ')
    : '';
  return (
    <dl className="exp-plan" data-exp-tab="plan">
      {(kind || plan.kind) && (
        <>
          <dt>{tr('实验类型', 'Type')}</dt>
          <dd>{kind ? tr(kind.zh, kind.en) : plan.kind}</dd>
        </>
      )}
      {pm?.name && (
        <>
          <dt>{tr('主指标', 'Primary metric')}</dt>
          <dd>
            {pm.name}
            {pm.direction === 'minimize' ? tr('，越低越好', ', lower is better') : pm.direction === 'maximize' ? tr('，越高越好', ', higher is better') : ''}
          </dd>
        </>
      )}
      {hyps.length > 0 && (
        <>
          <dt>{tr('要检验的假设', 'Hypotheses')}</dt>
          <dd>
            <ol>
              {hyps.map((h, i) => (
                <li key={i}>{h.text}</li>
              ))}
            </ol>
          </dd>
        </>
      )}
      {conditions.length > 0 && (
        <>
          <dt>{tr('对照设置', 'Conditions')}</dt>
          <dd>
            <ul>
              {conditions.map((c, i) => (
                <li key={`${c.name}-${i}`}>
                  {c.role === 'baseline' ? tr('对照组', 'Baseline') : tr('实验组', 'Treatment')}：{c.name}
                  {c.description && c.description.trim() !== '' && <div className="exp-plan-sub">{c.description}</div>}
                </li>
              ))}
            </ul>
          </dd>
        </>
      )}
      {evalBits.length > 0 && (
        <>
          <dt>{tr('评测设置', 'Evaluation')}</dt>
          <dd>{evalBits.join(' · ')}</dd>
        </>
      )}
      {datasets.length > 0 && (
        <>
          <dt>{tr('数据集', 'Datasets')}</dt>
          <dd>
            <ul>
              {datasets.map((d, i) => (
                <li key={`${d.name}-${i}`}>
                  {d.name}
                  {d.size_hint && d.size_hint.trim() !== '' ? tr(`（${d.size_hint}）`, ` (${d.size_hint})`) : ''}
                  {d.purpose && d.purpose.trim() !== '' && <div className="exp-plan-sub">{d.purpose}</div>}
                </li>
              ))}
            </ul>
          </dd>
        </>
      )}
      {plan.repro_strategy && (
        <>
          <dt>{tr('做法', 'Approach')}</dt>
          <dd>{plan.repro_strategy}</dd>
        </>
      )}
      {steps.length > 0 && (
        <>
          <dt>{tr('步骤', 'Steps')}</dt>
          <dd>
            <ol>
              {steps.map((s, i) => (
                <li key={i}>{planStepText(s)}</li>
              ))}
            </ol>
          </dd>
        </>
      )}
      <dt>{tr('运行环境', 'Environment')}</dt>
      <dd>{environmentText(plan.container)}</dd>
      <dt>{tr('预算', 'Budget')}</dt>
      <dd>
        {tr(`上限 ${budgetText(exp.budget)}，到了自动停止`, `Limit ${budgetText(exp.budget)}; it stops when reached`)}
        {estimate && <div className="exp-plan-sub">{tr(`AI 的预估：${estimate}`, `AI’s estimate: ${estimate}`)}</div>}
      </dd>
    </dl>
  );
}

/* ---------------- 结果 ---------------- */

function ResultsTab({ exp }: { exp: ExperimentDetail }) {
  const figures = exp.figures ?? [];
  const s = summarize(exp);
  const hyps = exp.plan?.hypotheses ?? [];
  const evidence = hypothesisEvidence(exp);
  const finished = EXPERIMENT_TERMINAL.has(exp.status);
  const nothing = !exp.report && figures.length === 0 && s.rounds.total === 0;

  if (nothing) {
    return (
      <div className="task-empty" data-exp-tab="results">
        {finished
          ? tr('这个实验没有留下结果。', 'This experiment left no results.')
          : tr('还没有结果。试运行通过后，每一轮的指标会出现在这里；结束时会有图表和报告。', 'No results yet. Once the trial run passes, each round’s metrics appear here; figures and a report come at the end.')}
      </div>
    );
  }

  return (
    <div data-exp-tab="results">
      <dl className="exp-facts">
        {s.best && (
          <>
            <dt>{tr('主指标', 'Primary metric')}</dt>
            <dd>
              <b>{fmtMetric(s.best.value)}</b>
              <span>
                {s.metric ?? ''}
                {tr(`，第 ${s.best.round} 轮最好`, `, best in round ${s.best.round}`)}
                {s.direction === 'minimize' ? tr('（越低越好）', ' (lower is better)') : tr('（越高越好）', ' (higher is better)')}
              </span>
            </dd>
          </>
        )}
        <dt>{tr('轮次', 'Rounds')}</dt>
        <dd>
          <b>{s.rounds.total}</b>
          <span>
            {[
              s.rounds.failed > 0 ? tr(`其中 ${s.rounds.failed} 轮失败`, `${s.rounds.failed} failed`) : '',
              exp.budget?.max_runs ? tr(`上限 ${exp.budget.max_runs} 轮`, `limit ${exp.budget.max_runs}`) : '',
            ]
              .filter(Boolean)
              .join(tr('，', ', '))}
          </span>
        </dd>
      </dl>

      {hyps.length > 0 && (
        <>
          <div className="task-sec-head">
            <h2 className="task-sec-title">{tr('假设', 'Hypotheses')}</h2>
          </div>
          <ul className="exp-hyps">
            {hyps.map((h, i) => (
              <li key={i}>
                <div className="exp-hyp-row">
                  <span className="exp-hyp-text">{h.text}</span>
                  <HypChip status={h.status} />
                </div>
                {evidence.get(i) && <div className="exp-hyp-why">{evidence.get(i)}</div>}
              </li>
            ))}
          </ul>
        </>
      )}

      {figures.length > 0 && (
        <>
          <div className="task-sec-head">
            <h2 className="task-sec-title">{tr('图表', 'Figures')}</h2>
          </div>
          <ExperimentFigures expId={exp.id} figures={figures} />
        </>
      )}

      <div className="task-sec-head">
        <h2 className="task-sec-title">{tr('报告', 'Report')}</h2>
      </div>
      {exp.report ? (
        <div className="exp-report">
          <Markdown source={exp.report} />
        </div>
      ) : (
        <div className="task-empty">
          {finished ? tr('这个实验没有生成报告。', 'This experiment produced no report.') : tr('实验结束后会写一份报告。', 'A report is written when the experiment finishes.')}
        </div>
      )}

      {s.rounds.total > 0 && <RunTab exp={exp} />}
    </div>
  );
}

/* ---------------- 页面 ---------------- */

export function ExperimentDetailPage() {
  const { id = '' } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const { openGates } = useShell();
  const { currentProjectId, projects } = useProject();
  const [chosenTab, setChosenTab] = useState<TabKey | null>(null);
  const [showObsolete, setShowObsolete] = useState(false);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [confirmTrash, setConfirmTrash] = useState(false);

  const { data: exp, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['experiment', id],
    queryFn: () => api.getExperiment(id),
    enabled: !!id,
    retry: false,
    refetchInterval: (q) => (q.state.data && !EXPERIMENT_TERMINAL.has(q.state.data.status) ? 5_000 : false),
  });
  useSyncTopic(exp?.project_id);

  const vid = exp?.voyage_id ?? null;
  const { data: voyage } = useQuery({
    queryKey: ['voyage', vid, showObsolete],
    queryFn: () => api.getVoyage(vid!, { includeObsolete: showObsolete }),
    enabled: !!vid,
    retry: false,
  });
  const voyageActive = !!voyage && !VOYAGE_TERMINAL.has(voyage.status);
  const { messages, handleExtraEvent } = useVoyageMessages(vid);
  const { terminal, live, clearTerminal, historyLoading, historyError } = useVoyageChannel(vid ?? '', voyageActive, { onExtraEvent: handleExtraEvent });
  const openAsk = openAskOf(voyage, messages);

  // 取消是先停任务、再改实验的状态：任务已经结束时就不再当它「还在做」
  const active = !!exp && !EXPERIMENT_TERMINAL.has(exp.status) && !(voyage && VOYAGE_TERMINAL.has(voyage.status));
  const now = useNow(30_000, active);

  // 等确认预算时：就地给出那张确认卡
  const waitingBudget = !!exp && active && (exp.status === 'awaiting_gate' || voyage?.status === 'paused_gate');
  const gates = useQuery({
    queryKey: ['gates', 'pending', exp?.project_id],
    queryFn: () => api.listGates('pending', exp!.project_id),
    enabled: waitingBudget,
    retry: false,
    refetchInterval: 10_000,
  });
  const gate = (gates.data ?? []).find((g) => g.payload?.voyage_id === vid);

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['experiment', id] });
    void queryClient.invalidateQueries({ queryKey: ['experiments'] });
    void queryClient.invalidateQueries({ queryKey: ['voyage', vid] });
    void queryClient.invalidateQueries({ queryKey: ['voyage-messages', vid] });
    void queryClient.invalidateQueries({ queryKey: ['voyages'] });
    void queryClient.invalidateQueries({ queryKey: ['gates'] });
  };
  const decide = useMutation({
    mutationFn: (v: { id: string; decision: GateDecision; comment?: string }) => api.decideGate(v.id, v.decision, v.comment),
    onSuccess: () => {
      toast(tr('已提交', 'Saved'), 'ok');
      refresh();
    },
    onError: (e) => toast(errorText(e), 'error'),
  });
  const resume = useMutation({
    mutationFn: () => api.resumeVoyage(vid!),
    onSuccess: () => {
      toast(tr('已从出错的步骤继续', 'Resumed from the failed step'), 'ok');
      refresh();
    },
    onError: (e) => toast(`${tr('无法重试：', 'Couldn’t retry: ')}${errorText(e)}`, 'error'),
  });
  const cancel = useMutation({
    mutationFn: () => api.cancelExperiment(id),
    onSuccess: () => {
      toast(tr('实验已取消', 'Experiment cancelled'), 'ok');
      refresh();
    },
    onError: (e) => toast(errorText(e), 'error'),
    onSettled: () => setConfirmCancel(false),
  });
  const trash = useMutation({
    mutationFn: () => api.trashExperiment(id),
    onSuccess: () => {
      toast(tr('已移入回收站', 'Moved to trash'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['experiments'] });
      navigate(topicPath(exp?.project_id ?? currentProjectId, 'experiment'));
    },
    onError: (e) => toast(errorText(e), 'error'),
    onSettled: () => setConfirmTrash(false),
  });

  const liveSteps = useMemo(() => [...(voyage?.steps ?? [])].sort(byListOrder).filter((s) => s.status !== 'obsolete'), [voyage?.steps]);
  const summary = useMemo(() => (exp ? summarize(exp) : null), [exp]);

  // 默认页签：做完且有结果先看结果，否则看进展；地址里的 ?tab= 优先
  const hasResults = !!exp && (!!exp.report || (exp.runs ?? []).length > 0 || (exp.figures ?? []).length > 0);
  const urlTab = TABS.find((t) => t.k === searchParams.get('tab'))?.k ?? null;
  const defaultTab: TabKey = exp && !active && hasResults ? 'results' : 'progress';
  const tab: TabKey = chosenTab ?? urlTab ?? defaultTab;
  // 实验刚做完：还停在「进展」上的话，换到「结果」
  const wasActive = useRef<boolean | null>(null);
  useEffect(() => {
    if (!exp) return;
    if (wasActive.current === true && !active && hasResults && chosenTab === 'progress') setChosenTab(null);
    wasActive.current = active;
  }, [exp, active, hasResults, chosenTab]);
  const pickTab = (k: TabKey) => {
    setChosenTab(k);
    if (searchParams.has('tab')) {
      const next = new URLSearchParams(searchParams);
      next.delete('tab');
      setSearchParams(next, { replace: true });
    }
  };

  if (isLoading) {
    return (
      <div className="page fadeup task-page">
        <LoadingState rows={5} />
      </div>
    );
  }
  if (isError || !exp) {
    const notFound = error instanceof ApiError && error.status === 404;
    return (
      <div className="page fadeup task-page">
        <ErrorState
          title={
            notFound
              ? tr('找不到这个实验，可能已被删除', 'This experiment no longer exists.')
              : tr('无法加载实验，请确认本机引擎正在运行', 'Couldn’t load this experiment. Check that the local engine is running.')
          }
          onRetry={notFound ? undefined : () => void refetch()}
          action={
            <Link className="btn btn-ghost sm" to={topicPath(currentProjectId, 'experiment')}>
              {tr('返回实验', 'Back to experiments')}
            </Link>
          }
        />
      </div>
    );
  }

  const topicName = projects.find((p) => p.id === exp.project_id)?.name ?? '';
  // 状态句：有任务就用任务的那句（它知道正在做哪一步）；等确认预算和已结束的用实验自己的
  const ownLine = exp.status === 'awaiting_gate' || !voyage || EXPERIMENT_TERMINAL.has(exp.status) || waitingBudget;
  const statusLine = waitingBudget ? experimentSentence('awaiting_gate') : ownLine ? experimentSentence(exp.status) : taskStatusLine(voyage, liveSteps);
  const tone = waitingBudget ? 'warn' : ownLine ? experimentTone(exp.status) : taskTone(voyage.status);
  const rounds = (exp.runs ?? []).length;
  const meta = [
    elapsedText(exp.created_at, active ? null : exp.updated_at, now),
    exp.server_host ? tr(`在 ${exp.server_host} 上`, `on ${exp.server_host}`) : '',
    active && rounds > 0 ? (exp.budget?.max_runs ? tr(`第 ${rounds}/${exp.budget.max_runs} 轮`, `round ${rounds} of ${exp.budget.max_runs}`) : tr(`第 ${rounds} 轮`, `round ${rounds}`)) : '',
  ]
    .filter(Boolean)
    .join(' · ');
  const result = !active && summary ? summaryLine(summary) : null;
  const stuck = voyage?.status === 'paused_error';

  const menu: DotsMenuItem[] = [
    ...(exp.idea_id ? [{ key: 'idea', label: tr('查看想法', 'View the idea'), icon: 'bulb', onSelect: () => navigate(`/ideas/${exp.idea_id}`) } as DotsMenuItem] : []),
    ...(vid ? [{ key: 'task', label: tr('在任务页查看', 'Open as a task'), icon: 'clock', onSelect: () => navigate(`/voyages/${vid}`) } as DotsMenuItem] : []),
    ...(!active ? [{ key: 'trash', label: tr('移入回收站', 'Move to trash'), icon: 'trash', danger: true, separated: true, onSelect: () => setConfirmTrash(true) } as DotsMenuItem] : []),
  ];

  return (
    <div className={'page fadeup task-page' + (tab === 'code' ? ' exp-wide' : '')}>
      <Link className="task-back" to={topicPath(exp.project_id, 'experiment')}>
        ← {topicName ? tr(`${topicName} · 实验`, `${topicName} · Experiments`) : tr('实验', 'Experiments')}
      </Link>
      <div className="task-head">
        <h1 className="task-title" title={exp.idea_title}>
          {exp.idea_title}
        </h1>
        <div className="task-actions">
          {active && (
            <button type="button" className="btn btn-ghost sm" disabled={cancel.isPending} onClick={() => setConfirmCancel(true)}>
              {tr('取消实验', 'Cancel experiment')}
            </button>
          )}
          <DotsMenu items={menu} />
        </div>
      </div>

      <div className="task-status" data-exp-status={exp.status}>
        <span className={`task-dot is-${tone}`} aria-hidden="true" />
        <span className="task-status-text">{statusLine}</span>
        {meta && <span className="task-status-meta">{meta}</span>}
      </div>
      {result && (
        <div className="task-result" data-testid="exp-result">
          {result}
        </div>
      )}

      {/* —— 需要你的时候：要做的事就在这里 —— */}
      {openAsk && voyageActive && vid && <InlineQuestion voyageId={vid} ask={openAsk} onAnswered={refresh} />}
      {waitingBudget &&
        (gate ? (
          <div className="dr-attn" data-testid="exp-gate">
            <div className="dr-attn-text">{tr('计划写好了，等你确认后再上机器运行', 'The plan is ready. Confirm it before anything runs on the machine.')}</div>
            <div className="dr-attn-sub">
              {[
                (exp.plan?.hypotheses ?? []).length > 0 ? tr(`${(exp.plan?.hypotheses ?? []).length} 条假设`, `${(exp.plan?.hypotheses ?? []).length} hypotheses`) : '',
                (exp.plan?.steps ?? []).length > 0 ? tr(`${(exp.plan?.steps ?? []).length} 个步骤`, `${(exp.plan?.steps ?? []).length} steps`) : '',
                tr(`上限 ${budgetText(exp.budget)}`, `limit ${budgetText(exp.budget)}`),
                exp.server_host ? tr(`在 ${exp.server_host} 上运行`, `runs on ${exp.server_host}`) : '',
              ]
                .filter(Boolean)
                .join(tr('，', ', '))}
              {tr('。', '.')}
            </div>
            <div className="dr-attn-actions">
              <button type="button" className="btn btn-primary sm" disabled={decide.isPending} onClick={() => decide.mutate({ id: gate.id, decision: 'approve' })}>
                {decide.isPending ? tr('正在提交…', 'Saving…') : tr('确认，开始运行', 'Confirm and run')}
              </button>
              <button type="button" className="btn btn-ghost sm" onClick={() => pickTab('plan')}>
                {tr('先看计划', 'Read the plan first')}
              </button>
              <button type="button" className="btn btn-plain sm" disabled={decide.isPending} onClick={() => decide.mutate({ id: gate.id, decision: 'reject' })}>
                {tr('不运行', 'Don’t run')}
              </button>
            </div>
          </div>
        ) : (
          <div className="dr-attn" data-testid="exp-gate">
            <div className="dr-attn-text">{tr('开始在机器上运行之前，需要你确认预算', 'Confirm the budget before anything runs on the machine')}</div>
            <div className="dr-attn-sub">{tr(`上限：${budgetText(exp.budget)}。确认后从这里继续。`, `Limit: ${budgetText(exp.budget)}. It continues from here once you confirm.`)}</div>
            <div className="dr-attn-actions">
              <button type="button" className="btn btn-primary sm" onClick={() => openGates(null)}>
                {tr('查看并确认', 'Review and confirm')}
              </button>
            </div>
          </div>
        ))}
      {stuck && voyage && (
        <div className="task-parked">
          <ParkedRunNotice error={voyage.last_error} retryAt={voyage.retry_at} retryAttempt={voyage.retry_attempt} onRetry={() => resume.mutate()} retrying={resume.isPending} />
        </div>
      )}

      <nav className="exp-tabs" aria-label={tr('实验的页面', 'Pages of this experiment')}>
        {TABS.map((t) => (
          <button key={t.k} type="button" className={'dn-tab' + (tab === t.k ? ' is-active' : '')} aria-current={tab === t.k ? 'page' : undefined} data-tab={t.k} onClick={() => pickTab(t.k)}>
            {tr(t.zh, t.en)}
          </button>
        ))}
      </nav>

      {tab === 'progress' && (
        <ProgressTab
          exp={exp}
          voyage={voyage}
          messages={messages}
          terminal={terminal}
          live={live}
          clearTerminal={clearTerminal}
          historyLoading={historyLoading}
          historyError={historyError}
          showObsolete={showObsolete}
          onShowObsolete={setShowObsolete}
        />
      )}
      {tab === 'results' && <ResultsTab exp={exp} />}
      {tab === 'plan' && <PlanTab exp={exp} />}
      {tab === 'code' && <CodeTab exp={exp} active={active} />}
      {tab === 'notes' && <MemoryTab exp={exp} />}

      <ConfirmModal
        open={confirmCancel}
        onClose={() => setConfirmCancel(false)}
        title={tr('取消这个实验？', 'Cancel this experiment?')}
        message={tr('它的任务和机器上的进程都会停止，已有的代码和结果保留。', 'Its task and the processes on the machine stop. Code and results so far are kept.')}
        confirmText={tr('取消实验', 'Cancel experiment')}
        danger
        busy={cancel.isPending}
        onConfirm={() => cancel.mutate()}
      />
      <ConfirmModal
        open={confirmTrash}
        onClose={() => setConfirmTrash(false)}
        title={tr('把这个实验移入回收站？', 'Move this experiment to the trash?')}
        message={tr('之后可以在实验页的回收站里恢复。', 'You can restore it from the trash on the Experiments page.')}
        confirmText={tr('移入回收站', 'Move to trash')}
        danger
        busy={trash.isPending}
        onConfirm={() => trash.mutate()}
      />
    </div>
  );
}
