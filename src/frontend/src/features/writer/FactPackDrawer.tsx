import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Drawer } from '../../components/ui/Drawer';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { api, type ManuscriptDetail } from '../../lib/api';
import { fmtRelative } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { HypChip } from '../experiment/shared';

/* ============================================================
   事实包抽屉 — fact_pack 分区展示（idea / 假设 / 指标 /
   图表 / 引文）+ 刷新按钮。AI 起草只允许引用这里的引文、
   图表与数字，用来防幻觉。
   ============================================================ */

export interface FactPackDrawerProps {
  open: boolean;
  onClose: () => void;
  manuscript: ManuscriptDetail;
  /** 当前编辑器可插入（有 view 且当前文件可写）时为 true。 */
  canInsert?: boolean;
  onInsertCite?: (bibkey: string) => void;
  onInsertFigure?: (figId: string, caption?: string | null) => void;
}

function SectionTitle({ zh, count }: { zh: string; count?: number }) {
  return (
    <div className="row gap8" style={{ margin: '18px 0 8px' }}>
      <span style={{ fontSize: 13, fontWeight: 600 }}>{zh}</span>
      {count !== undefined && (
        <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>{count}</span>
      )}
    </div>
  );
}

export function FactPackDrawer({ open, onClose, manuscript, canInsert, onInsertCite, onInsertFigure }: FactPackDrawerProps) {
  const queryClient = useQueryClient();
  const fp = manuscript.fact_pack;

  const refreshMutation = useMutation({
    mutationFn: () => api.refreshFactPack(manuscript.id),
    onSuccess: () => {
      toast(tr('已更新事实包', 'Fact pack updated'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['manuscript', manuscript.id] });
    },
    onError: (e) => toast(`${tr('无法更新事实包：', 'Couldn’t update the fact pack: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const hypotheses = fp?.hypotheses ?? [];
  const metrics = fp?.metrics ?? [];
  const figures = fp?.figures ?? [];
  const citations = fp?.citations ?? [];

  return (
    <Drawer
      open={open}
      onClose={onClose}
      title={
        <>
          <Icon name="layers" size={17} style={{ color: 'var(--accent)' }} />
          <span style={{ fontSize: 15, fontWeight: 600 }}>{tr('事实包', 'Fact pack')}</span>
        </>
      }
      sub={tr('AI 起草时只使用这里的文献、图表和数字。', 'AI drafts use only the papers, figures and numbers listed here.')}
    >
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 4 }}>
        <span style={{ fontSize: 12, color: 'var(--text-3)' }}>
          {fp?.generated_at ? `${tr('更新于 ', 'Updated ')}${fmtRelative(fp.generated_at)}` : tr('尚未生成', 'Not created yet')}
        </span>
        <button
          className="btn btn-soft sm"
          disabled={refreshMutation.isPending}
          onClick={() => refreshMutation.mutate()}
        >
          <Icon name="refresh" size={12} style={refreshMutation.isPending ? { animation: 'spin 1s linear infinite' } : undefined} />
          {refreshMutation.isPending ? tr('更新中…', 'Updating…') : tr('更新', 'Update')}
        </button>
      </div>

      {!fp ? (
        <div className="empty" style={{ padding: 40 }}>
          {tr('还没有事实包。点击「更新」，从实验结果和文献库生成。', 'No fact pack yet. Click Update to build one from your experiments and library.')}
        </div>
      ) : (
        <>
          {/* —— Idea —— */}
          <SectionTitle zh={tr('想法', 'Idea')} />
          {fp.idea ? (
            <div className="list-row">
              <div style={{ fontSize: 13, fontWeight: 600 }}>{fp.idea.title ?? '—'}</div>
              {fp.idea.summary && (
                <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 4, lineHeight: 1.6 }}>
                  {fp.idea.summary}
                </div>
              )}
            </div>
          ) : (
            <div style={{ fontSize: 12, color: 'var(--text-3)' }}>{tr('未关联想法', 'No linked idea')}</div>
          )}

          {/* —— 假设 —— */}
          <SectionTitle zh={tr('实验假设', 'Hypotheses')} count={hypotheses.length} />
          {hypotheses.length === 0 ? (
            <div style={{ fontSize: 12, color: 'var(--text-3)' }}>{tr('暂无，需关联已出结论的实验', 'None yet. Link an experiment with results.')}</div>
          ) : (
            <div className="col gap6">
              {hypotheses.map((h, i) => (
                <div key={i} className="list-row row gap8" style={{ alignItems: 'flex-start' }}>
                  <span style={{ flex: 1, fontSize: 12, lineHeight: 1.55 }}>{h.text}</span>
                  <HypChip status={h.status} title={h.evidence ?? undefined} />
                </div>
              ))}
            </div>
          )}

          {/* —— 指标 —— */}
          <SectionTitle zh={tr('指标', 'Metrics')} count={metrics.length} />
          {metrics.length === 0 ? (
            <div style={{ fontSize: 12, color: 'var(--text-3)' }}>{tr('暂无指标', 'No metrics yet')}</div>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>{tr('指标', 'Metric')}</th>
                    <th style={{ textAlign: 'right' }}>{tr('最优值', 'Best')}</th>
                    <th style={{ textAlign: 'right' }}>{tr('运行次数', 'Runs')}</th>
                  </tr>
                </thead>
                <tbody>
                  {metrics.map((m) => (
                    <tr key={m.name}>
                      <td className="mono" style={{ fontSize: 12 }}>{m.name}</td>
                      <td className="mono" style={{ textAlign: 'right', fontWeight: 600 }}>
                        {m.best ?? '—'}
                      </td>
                      <td className="mono" style={{ textAlign: 'right', color: 'var(--text-3)' }}>
                        {m.runs?.length ?? 0}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* —— 图表 —— */}
          <SectionTitle zh={tr('图表', 'Figures')} count={figures.length} />
          {figures.length === 0 ? (
            <div style={{ fontSize: 12, color: 'var(--text-3)' }}>{tr('暂无图表', 'No figures yet')}</div>
          ) : (
            <div className="col gap6">
              {figures.map((f) => (
                <div key={f.fig_id} className="list-row">
                  <div className="row gap8">
                    <span className="pill sm mono" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
                      {f.fig_id}
                    </span>
                    {f.source && <span style={{ fontSize: 11, color: 'var(--text-3)' }}>{f.source === 'experiment' ? tr('来自实验', 'From experiment') : f.source}</span>}
                    {onInsertFigure && (
                      <button
                        className="btn btn-soft sm"
                        style={{ marginLeft: 'auto', height: 22, fontSize: 11, padding: '0 8px' }}
                        disabled={!canInsert}
                        title={canInsert ? tr('在光标处插入图表', 'Insert the figure at the cursor') : tr('请先打开一个可编辑的 .tex 文件', 'Open an editable .tex file first')}
                        onClick={() => onInsertFigure(f.fig_id, f.caption)}
                      >
                        <Icon name="plus" size={11} />
                        {tr('插入', 'Insert')}
                      </button>
                    )}
                  </div>
                  {f.caption && (
                    <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 4, lineHeight: 1.55 }}>{f.caption}</div>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* —— 引文 —— */}
          <SectionTitle zh={tr('文献', 'Citations')} count={citations.length} />
          {citations.length === 0 ? (
            <div style={{ fontSize: 12, color: 'var(--text-3)' }}>
              {tr('暂无文献，先在文献库中收录几篇论文', 'No papers yet. Add some to your library first.')}
            </div>
          ) : (
            <div className="col gap6">
              {citations.map((c) => (
                <div key={c.bibkey} className="list-row row gap8" style={{ alignItems: 'flex-start' }}>
                  <span className="mono" style={{ fontSize: 11, color: 'var(--accent-text)', flexShrink: 0, paddingTop: 1 }}>
                    {c.bibkey}
                  </span>
                  <span style={{ flex: 1, fontSize: 12, lineHeight: 1.5 }}>
                    {c.title}
                    {c.year != null && <span style={{ color: 'var(--text-3)' }}> · {c.year}</span>}
                  </span>
                  {onInsertCite && (
                    <button
                      className="btn btn-soft sm"
                      style={{ height: 22, fontSize: 11, padding: '0 8px', flexShrink: 0 }}
                      disabled={!canInsert}
                      title={canInsert ? tr(`在光标处插入 \\cite{${c.bibkey}}`, `Insert \\cite{${c.bibkey}} at the cursor`) : tr('请先打开一个可编辑的 .tex 文件', 'Open an editable .tex file first')}
                      onClick={() => onInsertCite(c.bibkey)}
                    >
                      <Icon name="plus" size={11} />
                      {tr('插入', 'Insert')}
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}

          <div style={{ fontSize: 11, color: 'var(--text-3)', lineHeight: 1.6, marginTop: 20 }}>
            {tr(
              '实验或文献库有变化后，点击「更新」同步到这里。',
              'Click Update after your experiments or library change.',
            )}
          </div>
        </>
      )}
    </Drawer>
  );
}
