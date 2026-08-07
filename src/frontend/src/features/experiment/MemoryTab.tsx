import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import { Markdown } from '../../lib/markdown';
import { tr } from '../../lib/i18n';
import { api, EXPERIMENT_TERMINAL, type ExperimentDetail } from '../../lib/api';

/* ============================================================
   记忆 Tab：实验的跨轮记忆文件（workdir/MEMORY.md）。
   平台在关键事件写入（计划/环境/每轮结论/用户决策/终止），AI 经
   reflection.memory_note 自主补笔记；这里做人类友好的审查视图。
   数据走既有 /experiments/{id}/code/file 端点（服务器实时优先、
   checkpoint 镜像回退），零新后端接口。
   ============================================================ */

export function MemoryTab({ exp }: { exp: ExperimentDetail }) {
  const [raw, setRaw] = useState(false);
  const active = !EXPERIMENT_TERMINAL.has(exp.status);
  const { data, isLoading } = useQuery({
    queryKey: ['experiment', exp.id, 'memory'],
    queryFn: () => api.getExperimentCodeFile(exp.id, 'MEMORY.md'),
    enabled: !!exp.voyage_id,
    retry: false,
    refetchInterval: active ? 15_000 : false,
  });

  if (!exp.voyage_id) {
    return (
      <div className="card">
        <EmptyState
          compact
          icon="book"
          title={tr('该实验没有关联 AI 任务', 'This experiment has no linked AI task')}
        />
      </div>
    );
  }
  if (isLoading) {
    return <div className="empty" style={{ padding: 40 }}>{tr('加载实验记忆…', 'Loading memory…')}</div>;
  }
  const content = data?.content ?? '';
  if (!content.trim()) {
    return (
      <div className="card">
        <EmptyState
          compact
          icon="book"
          title={tr('记忆还没建立', 'No memory yet')}
          desc={tr(
            '实验计划定稿后，平台与 AI 会把关键决策、环境事实、每轮结论写进这里。',
            'Once the plan is finalized, the platform and the AI record key decisions, environment facts and per-round conclusions here.',
          )}
        />
      </div>
    );
  }

  return (
    <div className="fadeup" style={{ maxWidth: 860 }}>
      <div className="row gap8" style={{ marginBottom: 12, flexWrap: 'wrap' }}>
        <span className="section-h">
          <Icon name="book" size={15} style={{ color: 'var(--accent)' }} />
          {tr('实验记忆', 'Experiment memory')}
          <span className="en-label mono" style={{ fontSize: 11 }}>MEMORY.md</span>
        </span>
        <div className="row gap8" style={{ marginLeft: 'auto' }}>
          {data?.source && (
            <span
              className="pill sm"
              style={
                data.source === 'ssh'
                  ? { background: 'var(--ok-bg)', color: 'var(--ok-tx)' }
                  : { background: 'var(--surface-3)', color: 'var(--text-2)' }
              }
              title={
                data.source === 'ssh'
                  ? tr('从实验服务器实时读取', 'Read live from the experiment server')
                  : tr('服务器不可达，显示平台镜像', 'Server unreachable — showing the platform mirror')
              }
            >
              {data.source === 'ssh' ? tr('服务器实时', 'live') : tr('平台镜像', 'mirror')}
            </span>
          )}
          <button className="btn btn-ghost sm" onClick={() => setRaw((r) => !r)}>
            <Icon name="file" size={12} />
            {raw ? tr('渲染视图', 'Rendered') : tr('查看原文', 'Raw')}
          </button>
        </div>
      </div>
      <div className="card card-pad">
        {raw ? (
          <pre
            className="mono scroll"
            style={{ fontSize: 11.5, lineHeight: 1.6, whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: 0 }}
          >
            {content}
          </pre>
        ) : (
          <Markdown source={content} />
        )}
      </div>
    </div>
  );
}
