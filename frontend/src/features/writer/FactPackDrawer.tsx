import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Drawer } from '../../components/ui/Drawer';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { api, type ManuscriptDetail } from '../../lib/api';
import { fmtRelative } from '../../lib/format';
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
  /** 当前编辑器可插入（有 view 且当前文件可写）时为 true。 */
  canInsert?: boolean;
  onInsertCite?: (bibkey: string) => void;
  onInsertFigure?: (figId: string, caption?: string | null) => void;
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

export function FactPackDrawer({ open, onClose, manuscript, canInsert, onInsertCite, onInsertFigure }: FactPackDrawerProps) {
  const queryClient = useQueryClient();
  const fp = manuscript.fact_pack;

  const refreshMutation = useMutation({
    mutationFn: () => api.refreshFactPack(manuscript.id),
    onSuccess: () => {
      toast('事实包已重新组装', 'ok');
      void queryClient.invalidateQueries({ queryKey: ['manuscript', manuscript.id] });
    },
    onError: (e) => toast(`刷新失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
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
          <span style={{ fontSize: 14.5, fontWeight: 660 }}>事实包 · Fact Pack</span>
        </>
      }
      sub="AI 起草只能引用这里的引文、图表和实验数字，防止编造。"
    >
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 4 }}>
        <span style={{ fontSize: 11.5, color: 'var(--text-3)' }}>
          {fp?.generated_at ? `组装于 ${fmtRelative(fp.generated_at)}` : '还没有组装过'}
        </span>
        <button
          className="btn btn-soft sm"
          disabled={refreshMutation.isPending}
          onClick={() => refreshMutation.mutate()}
        >
          <Icon name="refresh" size={12} style={refreshMutation.isPending ? { animation: 'spin 1s linear infinite' } : undefined} />
          {refreshMutation.isPending ? '正在重新组装…' : '刷新'}
        </button>
      </div>

      {!fp ? (
        <div className="empty" style={{ padding: 40 }}>
          还没有事实包。点上方的刷新按钮，从实验结果和文献库重新组装一份。
        </div>
      ) : (
        <>
          {/* —— Idea —— */}
          <SectionTitle zh="研究想法 · Idea" />
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
            <div style={{ fontSize: 11.5, color: 'var(--text-4)' }}>未关联想法</div>
          )}

          {/* —— 假设 —— */}
          <SectionTitle zh="实验假设 · Hypotheses" count={hypotheses.length} />
          {hypotheses.length === 0 ? (
            <div style={{ fontSize: 11.5, color: 'var(--text-4)' }}>暂无（未关联实验或实验还没出结论）</div>
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
          <SectionTitle zh="实验指标 · Metrics" count={metrics.length} />
          {metrics.length === 0 ? (
            <div style={{ fontSize: 11.5, color: 'var(--text-4)' }}>暂无实验指标</div>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>指标</th>
                  <th style={{ textAlign: 'right' }}>最优值</th>
                  <th style={{ textAlign: 'right' }}>轮数</th>
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
          <SectionTitle zh="可用图表 · Figures" count={figures.length} />
          {figures.length === 0 ? (
            <div style={{ fontSize: 11.5, color: 'var(--text-4)' }}>暂无实验图表</div>
          ) : (
            <div className="col gap6">
              {figures.map((f) => (
                <div key={f.fig_id} className="list-row">
                  <div className="row gap8">
                    <span className="pill sm mono" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
                      {f.fig_id}
                    </span>
                    {f.source && <span style={{ fontSize: 10.5, color: 'var(--text-4)' }}>来自{f.source === 'experiment' ? '实验' : f.source}</span>}
                    {onInsertFigure && (
                      <button
                        className="btn btn-soft sm"
                        style={{ marginLeft: 'auto', height: 22, fontSize: 10.5, padding: '0 8px' }}
                        disabled={!canInsert}
                        title={canInsert ? '在编辑器光标处插入 figure 环境' : '先在编辑器里打开一个可写的 .tex 文件'}
                        onClick={() => onInsertFigure(f.fig_id, f.caption)}
                      >
                        <Icon name="plus" size={11} />
                        插入
                      </button>
                    )}
                  </div>
                  {f.caption && (
                    <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 4, lineHeight: 1.55 }}>{f.caption}</div>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* —— 引文 —— */}
          <SectionTitle zh="可引用文献 · Citations" count={citations.length} />
          {citations.length === 0 ? (
            <div style={{ fontSize: 11.5, color: 'var(--text-4)' }}>
              暂无可引用文献（先在文献库里精读/纳入几篇论文）
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
                  {onInsertCite && (
                    <button
                      className="btn btn-soft sm"
                      style={{ height: 22, fontSize: 10.5, padding: '0 8px', flexShrink: 0 }}
                      disabled={!canInsert}
                      title={canInsert ? `在编辑器光标处插入 \\cite{${c.bibkey}}` : '先在编辑器里打开一个可写的 .tex 文件'}
                      onClick={() => onInsertCite(c.bibkey)}
                    >
                      <Icon name="plus" size={11} />
                      插入
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}

          <div style={{ fontSize: 11, color: 'var(--text-4)', lineHeight: 1.6, marginTop: 20 }}>
            编译时会按这里的文献自动生成 references.bib，实验图表自动复制到 figures/ 目录（均为只读文件）。
            实验或文献库更新后，点刷新重新组装。
          </div>
        </>
      )}
    </Drawer>
  );
}
