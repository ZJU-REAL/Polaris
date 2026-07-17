import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Drawer } from '../../components/ui/Drawer';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { api, type ManuscriptDetail } from '../../lib/api';
import { fmtRelative } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { HypChip } from '../experiment/shared';

/* ============================================================
   「事实包」抽屉 — fact_pack 分区展示（idea / 假设 / 指标 /
   图表 / 引文）+ 刷新按钮。AI 起草只允许引用这里的引文、
   图表与数字，用来防幻觉。
   ============================================================ */

export interface FactPackDrawerProps {
  open: boolean;
  onClose: () => void;
  manuscript: ManuscriptDetail;
}

function SectionTitle({ zh, count }: { zh: string; count?: number }) {
  return (
    <div className="row gap8" style={{ margin: '18px 0 8px' }}>
      <span style={{ fontSize: 12.5, fontWeight: 660 }}>{zh}</span>
      {count !== undefined && (
        <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>{count}</span>
      )}
    </div>
  );
}

export function FactPackDrawer({ open, onClose, manuscript }: FactPackDrawerProps) {
  const queryClient = useQueryClient();
  const fp = manuscript.fact_pack;

  const refreshMutation = useMutation({
    mutationFn: () => api.refreshFactPack(manuscript.id),
    onSuccess: () => {
      toast(tr('事实包已重新组装', 'Fact pack rebuilt'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['manuscript', manuscript.id] });
    },
    onError: (e) => toast(`${tr('刷新失败：', 'Refresh failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
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
          <span style={{ fontSize: 14.5, fontWeight: 660 }}>{tr('事实包', 'Fact pack')}</span>
        </>
      }
      sub={tr('AI 起草只能引用这里的引文、图表和实验数字，防止编造。', 'AI drafting may only cite the references, figures and numbers listed here, to prevent fabrication.')}
    >
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 4 }}>
        <span style={{ fontSize: 11.5, color: 'var(--text-3)' }}>
          {fp?.generated_at ? `${tr('组装于 ', 'Assembled ')}${fmtRelative(fp.generated_at)}` : tr('还没有组装过', 'Not assembled yet')}
        </span>
        <button
          className="btn btn-soft sm"
          disabled={refreshMutation.isPending}
          onClick={() => refreshMutation.mutate()}
        >
          <Icon name="refresh" size={12} style={refreshMutation.isPending ? { animation: 'spin 1s linear infinite' } : undefined} />
          {refreshMutation.isPending ? tr('正在重新组装…', 'Rebuilding…') : tr('刷新', 'Refresh')}
        </button>
      </div>

      {!fp ? (
        <div className="empty" style={{ padding: 40 }}>
          {tr('还没有事实包。点上方的刷新按钮，从实验结果和文献库重新组装一份。', 'No fact pack yet. Click refresh above to assemble one from experiment results and the library.')}
        </div>
      ) : (
        <>
          {/* —— Idea —— */}
          <SectionTitle zh={tr('研究想法', 'Idea')} />
          {fp.idea ? (
            <div className="list-row">
              <div style={{ fontSize: 12.5, fontWeight: 620 }}>{fp.idea.title ?? '—'}</div>
              {fp.idea.summary && (
                <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 4, lineHeight: 1.6 }}>
                  {fp.idea.summary}
                </div>
              )}
            </div>
          ) : (
            <div style={{ fontSize: 11.5, color: 'var(--text-4)' }}>{tr('未关联想法', 'No linked idea')}</div>
          )}

          {/* —— 假设 —— */}
          <SectionTitle zh={tr('实验假设', 'Hypotheses')} count={hypotheses.length} />
          {hypotheses.length === 0 ? (
            <div style={{ fontSize: 11.5, color: 'var(--text-4)' }}>{tr('暂无（未关联实验或实验还没出结论）', 'None (no linked experiment, or no conclusions yet)')}</div>
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
          <SectionTitle zh={tr('实验指标', 'Metrics')} count={metrics.length} />
          {metrics.length === 0 ? (
            <div style={{ fontSize: 11.5, color: 'var(--text-4)' }}>{tr('暂无实验指标', 'No experiment metrics')}</div>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>{tr('指标', 'Metric')}</th>
                  <th style={{ textAlign: 'right' }}>{tr('最优值', 'Best')}</th>
                  <th style={{ textAlign: 'right' }}>{tr('轮数', 'Runs')}</th>
                </tr>
              </thead>
              <tbody>
                {metrics.map((m) => (
                  <tr key={m.name}>
                    <td className="mono" style={{ fontSize: 11.5 }}>{m.name}</td>
                    <td className="mono" style={{ textAlign: 'right', fontWeight: 650 }}>
                      {m.best ?? '—'}
                    </td>
                    <td className="mono" style={{ textAlign: 'right', color: 'var(--text-3)' }}>
                      {m.runs?.length ?? 0}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {/* —— 图表 —— */}
          <SectionTitle zh={tr('可用图表', 'Figures')} count={figures.length} />
          {figures.length === 0 ? (
            <div style={{ fontSize: 11.5, color: 'var(--text-4)' }}>{tr('暂无实验图表', 'No experiment figures')}</div>
          ) : (
            <div className="col gap6">
              {figures.map((f) => (
                <div key={f.fig_id} className="list-row">
                  <div className="row gap8">
                    <span className="pill sm mono" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
                      {f.fig_id}
                    </span>
                    {f.source && <span style={{ fontSize: 10.5, color: 'var(--text-4)' }}>{tr('来自', 'from ')}{f.source === 'experiment' ? tr('实验', 'experiment') : f.source}</span>}
                  </div>
                  {f.caption && (
                    <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 4, lineHeight: 1.55 }}>{f.caption}</div>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* —— 引文 —— */}
          <SectionTitle zh={tr('可引用文献', 'Citations')} count={citations.length} />
          {citations.length === 0 ? (
            <div style={{ fontSize: 11.5, color: 'var(--text-4)' }}>
              {tr('暂无可引用文献（先在文献库里精读/纳入几篇论文）', 'No citable papers yet (read or include some papers in the library first)')}
            </div>
          ) : (
            <div className="col gap6">
              {citations.map((c) => (
                <div key={c.bibkey} className="list-row row gap8" style={{ alignItems: 'flex-start' }}>
                  <span className="mono" style={{ fontSize: 10.5, color: 'var(--accent-text)', flexShrink: 0, paddingTop: 1 }}>
                    {c.bibkey}
                  </span>
                  <span style={{ flex: 1, fontSize: 11.5, lineHeight: 1.5 }}>
                    {c.title}
                    {c.year != null && <span style={{ color: 'var(--text-4)' }}>（{c.year}）</span>}
                  </span>
                </div>
              ))}
            </div>
          )}

          <div style={{ fontSize: 11, color: 'var(--text-4)', lineHeight: 1.6, marginTop: 20 }}>
            {tr(
              '编译时会按这里的文献自动生成 references.bib，实验图表自动复制到 figures/ 目录（均为只读文件）。实验或文献库更新后，点刷新重新组装。',
              'On compile, references.bib is generated from these papers and experiment figures are copied into figures/ (both read-only). After experiments or the library change, click refresh to rebuild.',
            )}
          </div>
        </>
      )}
    </Drawer>
  );
}
