import { memo, useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { StatusPill } from '../../components/ui/StatusPill';
import { EmptyState } from '../../components/ui/EmptyState';
import { useProject } from '../../app/project';
import { fmtDuration, fmtTime } from '../../lib/format';
import { api, EXPERIMENT_TERMINAL, type ExperimentRead } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { clickable } from '../../lib/a11y';
import { budgetText, expProgress } from './shared';
import { NewExperimentModal } from './NewExperimentModal';

/* ============================================================
   /experiment — Stage 03 · Experiment Lab（M4）列表视图。
   实验卡（idea 标题 + status + 服务器 + 耗时 + 进度）+ 新建实验。
   深链 ?new=<idea_id>（Review 页「发起实验」）自动开 Modal。
   ============================================================ */

/* memo：列表页轮询刷新时避免未变卡片重渲染（onClick 只捕获稳定的 navigate 与 id） */
const ExperimentCard = memo(function ExperimentCard({ exp, onClick }: { exp: ExperimentRead; onClick: () => void }) {
  const terminal = EXPERIMENT_TERMINAL.has(exp.status);
  const pct = expProgress(exp.status);
  const barColor =
    exp.status === 'failed' ? 'var(--danger)' : exp.status === 'cancelled' ? 'var(--text-4)' : exp.status === 'done' ? 'var(--ok)' : 'var(--accent)';
  return (
    <div className="card card-pad hoverable" {...clickable(onClick)} style={{ cursor: 'pointer' }}>
      <div className="row gap10" style={{ alignItems: 'flex-start' }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13.5, fontWeight: 650, lineHeight: 1.4, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {exp.idea_title}
          </div>
          <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', marginTop: 3 }}>
            {exp.id.slice(0, 8)} · {tr('创建', 'Created')} {fmtTime(exp.created_at)}
          </div>
        </div>
        <StatusPill status={exp.status} sm />
      </div>
      <div className="row gap10" style={{ marginTop: 12, flexWrap: 'wrap' }}>
        <span className="pill sm">
          <Icon name="server" size={11} />
          {exp.server_host ?? tr('未分配', 'Unassigned')}
        </span>
        <span className="pill sm mono" style={{ background: 'var(--surface-3)' }}>{budgetText(exp.budget)}</span>
        <span className="mono muted" style={{ fontSize: 11, marginLeft: 'auto' }}>
          <Icon name="clock" size={11} style={{ display: 'inline-block', verticalAlign: '-1.5px', marginRight: 4 }} />
          {fmtDuration(exp.created_at, terminal ? exp.updated_at : null)}
        </span>
      </div>
      <div className="bar" style={{ marginTop: 12 }}>
        <i style={{ width: `${pct}%`, background: barColor }} />
      </div>
    </div>
  );
}, (prev, next) => prev.exp === next.exp);

export function ExperimentPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { projects, isLoading: projectsLoading, currentProject, currentProjectId } = useProject();
  const pid = currentProjectId;

  const newIdeaId = searchParams.get('new');
  const [modalOpen, setModalOpen] = useState(false);

  // 深链 ?new=<idea_id> 自动开 Modal
  useEffect(() => {
    if (newIdeaId) setModalOpen(true);
  }, [newIdeaId]);

  function closeModal() {
    setModalOpen(false);
    if (newIdeaId) {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.delete('new');
          return next;
        },
        { replace: true },
      );
    }
  }

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['experiments', pid],
    queryFn: () => api.listExperiments(pid!),
    enabled: !!pid,
    retry: false,
    refetchInterval: (q) =>
      (q.state.data ?? []).some((e) => !EXPERIMENT_TERMINAL.has(e.status)) ? 10_000 : false,
  });
  const experiments = data ?? [];

  if (!projectsLoading && projects.length === 0) {
    return (
      <div className="page fadeup">
        <PageHead
          eyebrow="Stage 03 · Experiment Lab"
          title={tr('实验搭建', 'Experiment Lab')}
          sub={tr('从计划、审批、建环境到运行与报告。', 'From plan, approval and environment setup to runs and report.')}
        />
        <div className="card">
          <EmptyState
            icon="flask"
            title={tr('还没有研究方向', 'No research direction yet')}
            desc={tr('先创建研究方向、生成并晋级想法，再发起实验。', 'Create a direction, generate and promote an idea, then start an experiment.')}
            action={
              <button className="btn btn-primary" onClick={() => navigate('/projects/new')}>
                <Icon name="plus" size={14} />
                {tr('新建研究方向', 'New direction')}
              </button>
            }
          />
        </div>
      </div>
    );
  }

  return (
    <div className="page fadeup" style={{ maxWidth: 1180 }}>
      <PageHead
        eyebrow="Stage 03 · Experiment Lab"
        title={tr('实验搭建', 'Experiment Lab')}
        sub={
          currentProject
            ? tr(`当前方向：${currentProject.name}`, `Current direction: ${currentProject.name}`)
            : projectsLoading
              ? tr('加载研究方向…', 'Loading directions…')
              : tr('选择一个研究方向', 'Pick a research direction')
        }
        right={
          <button className="btn btn-primary" disabled={!pid} onClick={() => setModalOpen(true)}>
            <Icon name="plus" size={14} />
            {tr('新建实验', 'New experiment')}
          </button>
        }
      />

      {!pid ? (
        <div className="card">
          <div className="empty" style={{ padding: 60 }}>
            {projectsLoading ? tr('加载研究方向…', 'Loading directions…') : tr('请先选择研究方向', 'Pick a research direction first')}
          </div>
        </div>
      ) : isLoading ? (
        <div className="card">
          <div className="empty" style={{ padding: 60 }}>{tr('加载实验列表…', 'Loading experiments…')}</div>
        </div>
      ) : isError ? (
        <div className="card">
          <EmptyState
            compact
            icon="x"
            title={tr('无法加载实验列表', 'Could not load experiments')}
            desc={tr('后端不可用或接口尚未就绪。', 'Backend unavailable or the API is not ready yet.')}
            action={<button className="btn btn-soft sm" onClick={() => void refetch()}>{tr('重试', 'Retry')}</button>}
          />
        </div>
      ) : experiments.length === 0 ? (
        <div className="card">
          <EmptyState
            icon="flask"
            title={tr('还没有实验', 'No experiments yet')}
            desc={tr('先在想法评审页晋级一个想法，再回到这里发起实验。', 'Promote an idea in Idea Review first, then come back to start an experiment.')}
            action={
              <div className="row gap8">
                <button className="btn btn-ghost" onClick={() => navigate('/review')}>
                  <Icon name="scale" size={14} />
                  {tr('前往想法评审', 'Go to Idea Review')}
                </button>
                <button className="btn btn-primary" onClick={() => setModalOpen(true)}>
                  <Icon name="plus" size={14} />
                  {tr('新建实验', 'New experiment')}
                </button>
              </div>
            }
          />
        </div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(340px, 1fr))', gap: 14 }}>
          {experiments.map((e) => (
            <ExperimentCard key={e.id} exp={e} onClick={() => navigate(`/experiment/${e.id}`)} />
          ))}
        </div>
      )}

      {pid && (
        <NewExperimentModal open={modalOpen} onClose={closeModal} pid={pid} initialIdeaId={newIdeaId} />
      )}
    </div>
  );
}
