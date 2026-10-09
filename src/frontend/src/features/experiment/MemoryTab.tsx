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
          title={tr('这个实验没有关联任务', 'This experiment has no linked task')}
        />
      </div>
    );
  }
  if (isLoading) {
    return <div className="empty" style={{ padding: 40 }}>{tr('加载中…', 'Loading…')}</div>;
  }
  const content = data?.content ?? '';
  if (!content.trim()) {
    return (
      <div className="card">
        <EmptyState
          compact
          icon="book"
          title={tr('还没有实验记忆', 'No memory yet')}
          desc={tr('计划确定后，关键决策和每轮结论会记在这里。', 'Key decisions and round results are recorded here once the plan is set.')}
        />
      </div>
    );
  }

  return (
    <div className="fadeup" style={{ maxWidth: 860 }}>
      <div className="row gap8" style={{ marginBottom: 12, flexWrap: 'wrap' }}>
        <span className="section-h">
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
                  ? tr('从实验机器实时读取', 'Read live from the experiment machine')
                  : tr('连不上实验机器，显示上次保存的版本', 'Can’t reach the experiment machine; showing the last saved copy')
              }
            >
              {data.source === 'ssh' ? tr('实时', 'Live') : tr('已保存', 'Saved copy')}
            </span>
          )}
          <button className="btn btn-ghost sm" onClick={() => setRaw((r) => !r)}>
            <Icon name="file" size={12} />
            {raw ? tr('预览', 'Preview') : tr('查看原文', 'Source')}
          </button>
        </div>
      </div>
      <div className="card card-pad">
        {raw ? (
          <pre
            className="mono scroll"
            style={{ fontSize: 12, lineHeight: 1.6, whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: 0 }}
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
