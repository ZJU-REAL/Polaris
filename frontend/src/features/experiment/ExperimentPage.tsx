import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { StatusPill } from '../../components/ui/StatusPill';
import { EmptyState } from '../../components/ui/EmptyState';
import { useProject } from '../../app/project';
import { fmtDuration, fmtTime } from '../../lib/format';
import { api, EXPERIMENT_TERMINAL, type ExperimentRead } from '../../lib/api';
import { budgetText, expProgress } from './shared';
import { NewExperimentModal } from './NewExperimentModal';

/* ============================================================
   /experiment — Stage 03 · Experiment Lab（M4）列表视图。
   实验卡（idea 标题 + status + 服务器 + 耗时 + 进度）+ 新建实验。
   深链 ?new=<idea_id>（Review 页「发起实验」）自动开 Modal。
   ============================================================ */

function ExperimentCard({ exp, onClick }: { exp: ExperimentRead; onClick: () => void }) {
  const terminal = EXPERIMENT_TERMINAL.has(exp.status);
  const pct = expProgress(exp.status);
  const barColor =
    exp.status === 'failed' ? 'var(--danger)' : exp.status === 'cancelled' ? 'var(--text-4)' : exp.status === 'done' ? 'var(--ok)' : 'var(--accent)';
  return (
    <div className="card card-pad hoverable" onClick={onClick} style={{ cursor: 'pointer' }}>
      <div className="row gap10" style={{ alignItems: 'flex-start' }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13.5, fontWeight: 650, lineHeight: 1.4, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {exp.idea_title}
          </div>
          <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', marginTop: 3 }}>
            {exp.id.slice(0, 8)} · 创建 {fmtTime(exp.created_at)}
          </div>
        </div>
        <StatusPill status={exp.status} sm />
      </div>
      <div className="row gap10" style={{ marginTop: 12, flexWrap: 'wrap' }}>
        <span className="pill sm">
          <Icon name="server" size={11} />
          {exp.server_host ?? '未分配'}
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
}

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
          title="实验搭建 Experiment Lab"
          sub="从计划、审批、建环境到运行与报告。"
        />
        <div className="card">
          <EmptyState
            icon="flask"
            title="还没有研究方向"
            desc="先创建研究方向、生成并晋级想法，再发起实验。"
            action={
              <button className="btn btn-primary" onClick={() => navigate('/projects/new')}>
                <Icon name="plus" size={14} />
                新建研究方向 · New direction
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
        title="实验搭建 Experiment Lab"
        sub={
          currentProject
            ? `当前方向：${currentProject.name}`
            : projectsLoading
              ? '加载研究方向…'
              : '选择一个研究方向'
        }
        en="plan · gate · setup · run · report"
        right={
          <button className="btn btn-primary" disabled={!pid} onClick={() => setModalOpen(true)}>
            <Icon name="plus" size={14} />
            新建实验
          </button>
        }
      />

      {!pid ? (
        <div className="card">
          <div className="empty" style={{ padding: 60 }}>
            {projectsLoading ? '加载研究方向…' : '请先选择研究方向'}
          </div>
        </div>
      ) : isLoading ? (
        <div className="card">
          <div className="empty" style={{ padding: 60 }}>加载实验列表…</div>
        </div>
      ) : isError ? (
        <div className="card">
          <EmptyState
            compact
            icon="x"
            title="无法加载实验列表"
            desc="后端不可用或接口尚未就绪。"
            action={<button className="btn btn-soft sm" onClick={() => void refetch()}>重试 retry</button>}
          />
        </div>
      ) : experiments.length === 0 ? (
        <div className="card">
          <EmptyState
            icon="flask"
            title="还没有实验"
            desc="先在想法评审页晋级一个想法，再回到这里发起实验。"
            action={
              <div className="row gap8">
                <button className="btn btn-ghost" onClick={() => navigate('/review')}>
                  <Icon name="scale" size={14} />
                  前往想法评审
                </button>
                <button className="btn btn-primary" onClick={() => setModalOpen(true)}>
                  <Icon name="plus" size={14} />
                  新建实验
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
