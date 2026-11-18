import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { toast } from '../../components/ui/Toast';
import { api, ApiError, type IdeaRead, type ReviewPersona } from '../../lib/api';
import { errorText } from '../../lib/errors';
import { tr } from '../../lib/i18n';
import { ConfigRow, RangeControl } from '../voyages/shared/ConfigRow';

/* ============================================================
   「对比排序」：AI 审稿人把想法两两对比，按胜负排出名次。
   默认什么都不用填；每场讨论几轮、审稿人是谁收在「高级」里。
   开始后留在想法页，名次出来后直接显示在列表上。
   ============================================================ */

/** 默认的三位审稿人（渲染时求值，随语言切换）。 */
function defaultPersonas(): ReviewPersona[] {
  return [
    { name: tr('方法论审稿人', 'Methods reviewer'), stance: tr('找方法和实验设计的漏洞', 'Looks for flaws in method and experiment design') },
    { name: tr('前沿视角审稿人', 'Frontier reviewer'), stance: tr('看新颖性和潜在影响', 'Judges novelty and potential impact') },
    { name: tr('工程审稿人', 'Engineering reviewer'), stance: tr('看可行性和实现成本', 'Judges feasibility and cost to build') },
  ];
}

/** 参加对比的想法：没被淘汰的都算。草案只和草案比、研究方案只和研究方案比。 */
export function comparePlan(ideas: Pick<IdeaRead, 'id' | 'status' | 'depth'>[]): { ids: string[]; matches: number } {
  const usable = ideas.filter((i) => i.status !== 'rejected');
  const sketches = usable.filter((i) => i.depth !== 'proposal').length;
  const proposals = usable.length - sketches;
  return { ids: usable.map((i) => i.id), matches: Math.floor(sketches / 2) + Math.floor(proposals / 2) };
}

export interface CompareModalProps {
  open: boolean;
  onClose: () => void;
  pid: string;
  ideas: Pick<IdeaRead, 'id' | 'status' | 'depth'>[];
  onStarted: () => void;
}

export function CompareModal({ open, onClose, pid, ideas, onStarted }: CompareModalProps) {
  const queryClient = useQueryClient();
  const [advanced, setAdvanced] = useState(false);
  const [rounds, setRounds] = useState(2);
  const [personas, setPersonas] = useState<ReviewPersona[]>(defaultPersonas);
  useEffect(() => {
    if (open) setAdvanced(false);
  }, [open]);

  const plan = comparePlan(ideas);

  const start = useMutation({
    mutationFn: () =>
      api.startTournament(pid, {
        idea_ids: plan.ids,
        rounds,
        personas: advanced ? personas.filter((p) => p.name.trim() !== '') : null,
      }),
    onSuccess: () => {
      toast(tr('已开始对比，名次出来后会显示在列表上', 'Comparison started. The ranking will show on the list.'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['forge-state', pid] });
      void queryClient.invalidateQueries({ queryKey: ['voyages'] });
      void queryClient.invalidateQueries({ queryKey: ['latest-tournament', pid] });
      onStarted();
      onClose();
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) {
        toast(tr('这个课题已有想法任务在运行，请等它完成', 'An idea task is already running for this topic. Wait for it to finish.'), 'error');
        void queryClient.invalidateQueries({ queryKey: ['forge-state', pid] });
        void queryClient.invalidateQueries({ queryKey: ['voyages'] });
      } else {
        toast(errorText(e), 'error');
      }
    },
  });

  function setPersona(i: number, patch: Partial<ReviewPersona>) {
    setPersonas((ps) => ps.map((p, pi) => (pi === i ? { ...p, ...patch } : p)));
  }

  const enough = plan.matches > 0;

  return (
    <Modal
      open={open}
      onClose={onClose}
      width={540}
      title={tr('对比排序', 'Compare ideas')}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>
            {tr('取消', 'Cancel')}
          </button>
          <button className="btn btn-primary" disabled={!enough || start.isPending} onClick={() => start.mutate()}>
            {start.isPending ? tr('正在开始…', 'Starting…') : tr('开始对比', 'Start comparing')}
          </button>
        </>
      }
    >
      <p className="ideas-modal-lead">
        {tr(
          'AI 审稿人把想法两两放在一起讨论，判出哪个更值得做，再按胜负排出名次。',
          'AI reviewers discuss the ideas two at a time, decide which is more worth pursuing, and rank them by the results.',
        )}
      </p>
      <p className="ideas-modal-note">
        {enough
          ? tr(
              `这次有 ${plan.ids.length} 个想法参加，共 ${plan.matches} 场对比。你在想法讨论里写的评论，审稿人也会看到。`,
              `${plan.ids.length} ideas take part, in ${plan.matches} ${plan.matches === 1 ? 'comparison' : 'comparisons'}. Reviewers also read your comments on each idea.`,
            )
          : tr('至少要有两个同类的想法才能对比。', 'You need at least two ideas of the same kind to compare.')}
      </p>

      <button type="button" className="step-toggle" aria-expanded={advanced} onClick={() => setAdvanced((v) => !v)} style={{ marginTop: 10 }}>
        {tr('高级', 'Advanced')}
        <span aria-hidden="true">{advanced ? ' −' : ' +'}</span>
      </button>
      {advanced && (
        <>
          <div className="settings-list" style={{ marginTop: 4 }}>
            <ConfigRow label={tr('每场讨论轮数', 'Rounds per comparison')} hint={tr('轮数越多越仔细，用量也越大', 'More rounds, more careful, more usage')}>
              <RangeControl ariaLabel={tr('每场讨论轮数', 'Rounds per comparison')} value={rounds} min={1} max={5} step={1} onChange={setRounds} />
            </ConfigRow>
          </div>
          <div className="ideas-modal-label">{tr('AI 审稿人', 'AI reviewers')}</div>
          <div className="col gap8">
            {personas.map((p, i) => (
              <div key={i} className="row gap8">
                <input
                  className="input"
                  style={{ width: 150, flexShrink: 0 }}
                  placeholder={tr('名字', 'Name')}
                  aria-label={tr('审稿人名字', 'Reviewer name')}
                  value={p.name}
                  onChange={(e) => setPersona(i, { name: e.target.value })}
                />
                <input
                  className="input"
                  style={{ flex: 1, minWidth: 0 }}
                  placeholder={tr('关注点', 'Focus')}
                  aria-label={tr('审稿人关注点', 'Reviewer focus')}
                  value={p.stance}
                  onChange={(e) => setPersona(i, { stance: e.target.value })}
                />
                <button className="icon-btn" title={tr('移除', 'Remove')} aria-label={tr('移除', 'Remove')} onClick={() => setPersonas((ps) => ps.filter((_, pi) => pi !== i))}>
                  <Icon name="x" size={14} />
                </button>
              </div>
            ))}
            <button className="btn btn-plain sm" style={{ alignSelf: 'flex-start' }} onClick={() => setPersonas((ps) => [...ps, { name: '', stance: '' }])}>
              <Icon name="plus" size={13} />
              {tr('添加审稿人', 'Add reviewer')}
            </button>
          </div>
        </>
      )}
    </Modal>
  );
}
