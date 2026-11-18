import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Modal } from '../../components/ui/Modal';
import { toast } from '../../components/ui/Toast';
import { api, ApiError } from '../../lib/api';
import { errorText } from '../../lib/errors';
import { tr } from '../../lib/i18n';
import { researchApi } from '../../lib/researchApi';
import { ConfigRow, RangeControl } from '../voyages/shared/ConfigRow';
import { researchKeys } from '../deepresearch/hooks';

/* ============================================================
   「再想一些」：这一页唯一的生成入口。两种方式选一种——

   - 快速：直接从文献库的论文里提出几个想法（几分钟）；
   - 深入：先提出假设，逐条对照文献核对，留下站得住的（几十分钟）。
     有研究进展的课题接着那次研究做；没有的课题在它关联的文献库上做。

   数量、轮数这些旋钮收在「高级」里；「从一段描述 / 一个概念 / 一篇论文写研究方案」
   也在那里（原来的「深度生成」）。开始后留在想法页，页面顶上有一行进度。
   ============================================================ */

export type GenerateMode = 'quick' | 'thorough';

export interface GenerateIdeasModalProps {
  open: boolean;
  onClose: () => void;
  pid: string;
  /** 这个课题的研究（有时间线的课题）；没有为 null */
  runId: string | null;
  /** 没有研究进展时，深入生成用的方向和文献库 */
  direction: string;
  libraryId: string | null;
  /** 文献库里的论文数；0 时两种方式都做不了 */
  paperCount: number | null;
  onStarted: () => void;
  /** 打开「写研究方案」抽屉 */
  onOpenProposal: () => void;
}

export function GenerateIdeasModal({
  open,
  onClose,
  pid,
  runId,
  direction,
  libraryId,
  paperCount,
  onStarted,
  onOpenProposal,
}: GenerateIdeasModalProps) {
  const queryClient = useQueryClient();
  const thoroughReady = !!runId || (!!libraryId && direction.trim() !== '');
  const [mode, setMode] = useState<GenerateMode>('quick');
  const [advanced, setAdvanced] = useState(false);
  const [numIdeas, setNumIdeas] = useState(8);
  const [maxContextPapers, setMaxContextPapers] = useState(20);
  const [dedupThreshold, setDedupThreshold] = useState(0.85);
  const [rounds, setRounds] = useState(3);

  useEffect(() => {
    if (!open) return;
    setMode(thoroughReady ? 'thorough' : 'quick');
    setAdvanced(false);
  }, [open, thoroughReady]);

  const start = useMutation({
    mutationFn: async () => {
      if (mode === 'quick') {
        await api.startForge(pid, { num_ideas: numIdeas, dedup_threshold: dedupThreshold, max_context_papers: maxContextPapers });
      } else if (runId) {
        await researchApi.moreIdeas(runId, advanced ? rounds : undefined);
      } else {
        await api.createVoyage({
          kind: 'discovery',
          project_id: pid,
          goal: direction.trim(),
          params: { direction: direction.trim(), library_id: libraryId, max_expansions: rounds },
        });
      }
    },
    onSuccess: () => {
      toast(tr('已开始，完成后新想法会出现在这里', 'Started. New ideas will appear here when it’s done.'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['forge-state', pid] });
      void queryClient.invalidateQueries({ queryKey: ['voyages'] });
      void queryClient.invalidateQueries({ queryKey: researchKeys.list });
      if (runId) void queryClient.invalidateQueries({ queryKey: researchKeys.run(runId) });
      onStarted();
      onClose();
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409 && !/LIBRARY_TOO_SMALL/.test(String(e.message))) {
        toast(tr('这个课题已有想法任务在运行，请等它完成', 'An idea task is already running for this topic. Wait for it to finish.'), 'error');
        void queryClient.invalidateQueries({ queryKey: ['forge-state', pid] });
        void queryClient.invalidateQueries({ queryKey: ['voyages'] });
      } else {
        toast(errorText(e), 'error');
      }
    },
  });

  const noPapers = paperCount === 0;
  const canStart = !start.isPending && !noPapers && (mode === 'quick' || thoroughReady);

  const option = (m: GenerateMode, name: string, time: string, desc: string, disabledWhy?: string) => {
    const disabled = !!disabledWhy;
    return (
      <label className={'ideas-choice' + (mode === m ? ' is-on' : '') + (disabled ? ' is-disabled' : '')}>
        <input type="radio" name="generate-mode" checked={mode === m} disabled={disabled} onChange={() => setMode(m)} />
        <span className="ideas-choice-body">
          <span className="ideas-choice-head">
            <span className="ideas-choice-name">{name}</span>
            <span className="ideas-choice-time">{time}</span>
          </span>
          <span className="ideas-choice-desc">{disabledWhy ?? desc}</span>
        </span>
      </label>
    );
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      width={520}
      title={tr('再想一些', 'Generate more ideas')}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>
            {tr('取消', 'Cancel')}
          </button>
          <button className="btn btn-primary" disabled={!canStart} onClick={() => start.mutate()}>
            {start.isPending ? tr('正在开始…', 'Starting…') : tr('开始', 'Start')}
          </button>
        </>
      }
    >
      <div className="ideas-choices" role="radiogroup" aria-label={tr('生成方式', 'How to generate')}>
        {option(
          'quick',
          tr('快速', 'Quick'),
          tr('几分钟', 'A few minutes'),
          tr('直接从文献库的论文里提出想法，不逐条核对依据。', 'Proposes ideas straight from the papers in your library, without checking each claim.'),
        )}
        {option(
          'thorough',
          tr('深入', 'Thorough'),
          tr('通常几十分钟', 'Usually tens of minutes'),
          tr('先提出假设，逐条对照文献核对，只留下站得住的。', 'Proposes hypotheses, checks each against the papers and keeps the ones that hold.'),
          thoroughReady ? undefined : tr('这个课题还没有关联文献库，暂时只能快速生成。', 'This topic has no library linked yet, so only Quick is available.'),
        )}
      </div>

      {noPapers && (
        <div className="ideas-modal-note is-warn">
          {tr('文献库里还没有论文。先在「概览」里开始研究，找到论文后再来。', 'The library has no papers yet. Start the research from Overview first.')}
        </div>
      )}

      <button type="button" className="step-toggle" aria-expanded={advanced} onClick={() => setAdvanced((v) => !v)} style={{ marginTop: 14 }}>
        {tr('高级', 'Advanced')}
        <span aria-hidden="true">{advanced ? ' −' : ' +'}</span>
      </button>
      {advanced && (
        <div className="settings-list" style={{ marginTop: 4 }}>
          {mode === 'quick' ? (
            <>
              <ConfigRow label={tr('想法数量', 'Number of ideas')} hint={tr('合并相似想法之前的数量', 'Before similar ideas are merged')}>
                <RangeControl ariaLabel={tr('想法数量', 'Number of ideas')} value={numIdeas} min={3} max={20} step={1} onChange={setNumIdeas} />
              </ConfigRow>
              <ConfigRow label={tr('参考论文数', 'Papers to draw on')} hint={tr('越多越全面，用量也越大', 'More papers, broader ideas, more usage')}>
                <RangeControl ariaLabel={tr('参考论文数', 'Papers to draw on')} value={maxContextPapers} min={5} max={50} step={5} onChange={setMaxContextPapers} />
              </ConfigRow>
              <ConfigRow label={tr('合并相似想法', 'Merge similar ideas')} hint={tr('相似度高于这个值的算同一个', 'Ideas more similar than this count as one')}>
                <RangeControl
                  ariaLabel={tr('合并相似想法', 'Merge similar ideas')}
                  value={dedupThreshold}
                  min={0.5}
                  max={0.95}
                  step={0.05}
                  format={(v) => v.toFixed(2)}
                  onChange={setDedupThreshold}
                />
              </ConfigRow>
            </>
          ) : (
            <ConfigRow label={tr('轮数', 'Rounds')} hint={tr('每一轮在最有希望的假设上再展开', 'Each round expands the most promising hypotheses')}>
              <RangeControl ariaLabel={tr('轮数', 'Rounds')} value={rounds} min={1} max={10} step={1} onChange={setRounds} />
            </ConfigRow>
          )}
          <div className="settings-row" style={{ minHeight: 40, padding: '10px 0' }}>
            <div className="settings-row-text">
              <div style={{ fontSize: 13, fontWeight: 500, color: 'var(--text)' }}>{tr('从你自己的想法出发', 'Start from your own idea')}</div>
              <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 2 }}>
                {tr('把一段描述、一个概念或一篇论文写成研究方案', 'Turn a description, a concept or a paper into a proposal')}
              </div>
            </div>
            <button
              type="button"
              className="btn btn-ghost sm"
              onClick={() => {
                onClose();
                onOpenProposal();
              }}
            >
              {tr('写研究方案', 'Write a proposal')}
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}
