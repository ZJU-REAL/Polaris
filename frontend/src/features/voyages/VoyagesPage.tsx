import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { StatusPill } from '../../components/ui/StatusPill';
import { Segmented } from '../../components/ui/Segmented';
import { Modal } from '../../components/ui/Modal';
import { FormField } from '../../components/ui/FormField';
import { toast } from '../../components/ui/Toast';
import { useProject } from '../../app/project';
import { api, VOYAGE_TERMINAL, type VoyageRead } from '../../lib/api';
import { fmtDuration, fmtTime } from '../../lib/format';

/* ============================================================
   /voyages — 航程列表：状态过滤 + 行（kind/goal/status/进度/耗时）
   + 「新建演示航程」（POST /voyages {kind:"demo"}）。
   ============================================================ */

type Filter = 'all' | 'active' | 'paused' | 'done' | 'failed';

const FILTERS: { v: Filter; label: string }[] = [
  { v: 'all', label: '全部' },
  { v: 'active', label: '进行中' },
  { v: 'paused', label: '等待中' },
  { v: 'done', label: '已完成' },
  { v: 'failed', label: '失败/取消' },
];

function matchFilter(v: VoyageRead, f: Filter): boolean {
  switch (f) {
    case 'all':
      return true;
    case 'active':
      return ['planning', 'executing', 'verifying', 'replanning'].includes(v.status);
    case 'paused':
      return v.status === 'paused_gate' || v.status === 'paused_error';
    case 'done':
      return v.status === 'done';
    case 'failed':
      return v.status === 'failed' || v.status === 'cancelled';
  }
}

/** 进度描述：cursor / plan 长度（plan 为数组时）。 */
function progressText(v: VoyageRead): string {
  const total = Array.isArray(v.plan) ? v.plan.length : null;
  const cur = v.cursor ?? 0;
  return total ? `step ${Math.min(cur + 1, total)}/${total}` : `cursor ${cur}`;
}

export function VoyagesPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { projects, currentProjectId, isLoading: projectsLoading } = useProject();

  const [filter, setFilter] = useState<Filter>('all');
  const [scope, setScope] = useState<'current' | 'all'>('current');

  const projectId = scope === 'current' ? (currentProjectId ?? undefined) : undefined;
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['voyages', projectId ?? 'all'],
    queryFn: () => api.listVoyages(projectId),
    retry: false,
    refetchInterval: 30_000,
  });
  const voyages = useMemo(
    () => (data ?? []).filter((v) => matchFilter(v, filter)),
    [data, filter],
  );

  // —— 新建演示航程 ——
  const [createOpen, setCreateOpen] = useState(false);
  const [goal, setGoal] = useState('演示：分析目标 → 生成产物 → 自检');
  const [createProjectId, setCreateProjectId] = useState<string>('');
  const effectiveProjectId = createProjectId || currentProjectId || projects[0]?.id || '';

  const createMutation = useMutation({
    mutationFn: () => api.createVoyage({ kind: 'demo', project_id: effectiveProjectId, goal: goal.trim() }),
    onSuccess: (v) => {
      toast('演示任务已入队', 'ok');
      setCreateOpen(false);
      void queryClient.invalidateQueries({ queryKey: ['voyages'] });
      navigate(`/voyages/${v.id}`);
    },
    onError: (err) => toast(`创建失败：${err instanceof Error ? err.message : String(err)}`, 'error'),
  });

  const noProjects = !projectsLoading && projects.length === 0;

  return (
    <div className="page fadeup">
      <PageHead
        eyebrow="Polaris · Voyages"
        title="AI 任务 Tasks"
        sub="长时程 agent 任务：先规划 → 再执行 → 自动校验，需要人工审批时会自动暂停。"
        right={
          <button className="btn btn-primary" disabled={noProjects} onClick={() => setCreateOpen(true)}>
            <Icon name="play" size={14} />
            新建演示任务
          </button>
        }
      />

      {noProjects ? (
        <div className="card card-pad" style={{ textAlign: 'center', padding: 60 }}>
          <Icon name="compass" size={36} style={{ margin: '0 auto 14px', color: 'var(--text-4)' }} />
          <div style={{ fontSize: 15, fontWeight: 650, marginBottom: 6 }}>还没有研究方向</div>
          <div style={{ fontSize: 12.5, color: 'var(--text-3)', marginBottom: 18 }}>
            任务隶属于研究方向。先创建一个方向，再启动演示任务。
          </div>
          <button className="btn btn-primary" onClick={() => navigate('/projects/new')}>
            <Icon name="plus" size={14} />
            新建研究方向
          </button>
        </div>
      ) : (
        <>
          <div className="row gap10" style={{ marginBottom: 16, flexWrap: 'wrap' }}>
            <Segmented options={FILTERS.map((f) => ({ v: f.v, label: f.label }))} value={filter} onChange={setFilter} />
            <div style={{ flex: 1 }} />
            <Segmented
              options={[
                { v: 'current' as const, label: '当前方向' },
                { v: 'all' as const, label: '全部方向' },
              ]}
              value={scope}
              onChange={setScope}
            />
          </div>

          {isLoading ? (
            <div className="empty" style={{ padding: 60 }}>加载中…</div>
          ) : isError ? (
            <div className="card card-pad" style={{ textAlign: 'center', padding: 48 }}>
              <div style={{ fontSize: 14, fontWeight: 650, marginBottom: 6 }}>无法加载任务列表</div>
              <div style={{ fontSize: 12.5, color: 'var(--text-3)', marginBottom: 16 }}>后端不可用或 M1 接口尚未就绪</div>
              <button className="btn btn-soft" onClick={() => void refetch()}>重试 retry</button>
            </div>
          ) : voyages.length === 0 ? (
            <div className="card card-pad" style={{ textAlign: 'center', padding: 48 }}>
              <div style={{ fontSize: 14, fontWeight: 650, marginBottom: 6 }}>暂无任务</div>
              <div style={{ fontSize: 12.5, color: 'var(--text-3)', marginBottom: 16 }}>
                点击右上角「新建演示任务」体验 AI 任务的规划-执行-自检循环。
              </div>
            </div>
          ) : (
            <div className="card" style={{ overflow: 'hidden' }}>
              {voyages.map((v, i) => {
                const active = !VOYAGE_TERMINAL.has(v.status);
                return (
                  <div
                    key={v.id}
                    className="hoverable row gap12"
                    onClick={() => navigate(`/voyages/${v.id}`)}
                    style={{ padding: '14px 18px', borderTop: i > 0 ? '0.5px solid var(--border)' : 'none', alignItems: 'center' }}
                  >
                    <span className="pill sm mono" style={{ background: 'var(--surface-3)', flexShrink: 0 }}>{v.kind}</span>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: 13.5, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {v.goal}
                      </div>
                      <div className="row gap8" style={{ marginTop: 4 }}>
                        <span className="mono muted" style={{ fontSize: 10.5 }}>{v.id.slice(0, 8)}</span>
                        <span className="mono muted" style={{ fontSize: 10.5 }}>· {fmtTime(v.created_at)}</span>
                      </div>
                    </div>
                    <span className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)', flexShrink: 0 }}>
                      {progressText(v)}
                    </span>
                    <span className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)', width: 72, textAlign: 'right', flexShrink: 0 }}>
                      <Icon name="clock" size={11} style={{ display: 'inline-block', verticalAlign: '-1px', marginRight: 4 }} />
                      {fmtDuration(v.created_at, active ? null : v.updated_at)}
                    </span>
                    <span style={{ flexShrink: 0 }}>
                      <StatusPill status={v.status} sm />
                    </span>
                    <Icon name="chevron" size={14} style={{ color: 'var(--text-4)', flexShrink: 0 }} />
                  </div>
                );
              })}
            </div>
          )}
        </>
      )}

      {/* 新建演示航程 */}
      <Modal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        title={
          <>
            <Icon name="play" size={16} style={{ color: 'var(--accent)' }} />
            新建演示任务
          </>
        }
        sub="kind=demo：分析目标 → 生成产物（含算力预算审批）→ 自检"
        footer={
          <>
            <button className="btn btn-ghost" onClick={() => setCreateOpen(false)}>取消</button>
            <button
              className="btn btn-primary"
              disabled={!goal.trim() || !effectiveProjectId || createMutation.isPending}
              onClick={() => createMutation.mutate()}
            >
              {createMutation.isPending ? '入队中…' : '启动任务'}
            </button>
          </>
        }
      >
        <FormField label="所属方向" en="Project">
          <select className="input" value={effectiveProjectId} onChange={(e) => setCreateProjectId(e.target.value)}>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        </FormField>
        <FormField label="目标" en="Goal" hint="系统将围绕该目标自动生成三步计划">
          <textarea className="textarea" rows={3} value={goal} onChange={(e) => setGoal(e.target.value)} />
        </FormField>
      </Modal>
    </div>
  );
}
