import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useMutation } from '@tanstack/react-query';
import { toast } from '../../components/ui/Toast';
import { api } from '../../lib/api';
import { errorText } from '../../lib/errors';
import { tr } from '../../lib/i18n';
import type { ResearchRunDetail } from '../../lib/researchApi';
import { useCapabilities } from './hooks';
import { PendingLine, Section } from './parts';
import type { SectionState } from './phase';

/* ============================================================
   下一步：选定想法之后往哪走。三条出口各一行——写成研究方案、做实验、写论文；
   后两条需要这台机器上有的东西（远程机器、LaTeX），没有就在这一行说清楚。
   选定了多个想法时，先选这一步要做哪一个。
   ============================================================ */

export function NextSection({ run, state }: { run: ResearchRunDetail; state: SectionState }) {
  const navigate = useNavigate();
  const picked = run.ideas.filter((i) => i.status === 'promoted');
  const [ideaId, setIdeaId] = useState<string>('');
  // 选中的那个被取消选定后，退回第一个
  useEffect(() => {
    if (!picked.some((i) => i.id === ideaId)) setIdeaId(picked[0]?.id ?? '');
  }, [picked, ideaId]);
  const idea = picked.find((i) => i.id === ideaId) ?? picked[0];

  const expand = useMutation({
    mutationFn: (id: string) => api.startDeepIdea(run.project_id, { seed: { type: 'idea', value: id } }),
    onSuccess: (voyage) => {
      toast(tr('已开始写研究方案', 'Started writing the proposal'), 'ok');
      navigate(`/voyages/${voyage.id}`);
    },
    onError: (e) => toast(errorText(e), 'error'),
  });

  const ready = state === 'ready' && !!idea;
  // 能力问引擎现在的状态（刚在设置里加了机器，回来就该变）；取不到时用这次研究读数里的那份
  const caps = useCapabilities().data ?? run.capabilities;
  const { remote_machine: hasMachine, latex: hasLatex } = caps;

  return (
    <Section title={tr('下一步', 'Next')} state={state} last>
      {!ready ? (
        <PendingLine>
          {tr('选定想法后，可以从这里把它写成研究方案、做实验、写成论文。', 'After you pick an idea, expand it into a proposal, run an experiment or write the paper from here.')}
        </PendingLine>
      ) : (
        <>
          {picked.length > 1 && (
            <label className="dr-next-pick">
              <span>{tr('接下来做', 'Continue with')}</span>
              <select className="input" value={idea.id} onChange={(e) => setIdeaId(e.target.value)}>
                {picked.map((i) => (
                  <option key={i.id} value={i.id}>
                    {i.title}
                  </option>
                ))}
              </select>
            </label>
          )}
          {picked.length === 1 && (
            <div className="dr-next-one" title={idea.title}>
              {idea.title}
            </div>
          )}
          <div className="dr-next">
            <div className="dr-next-row">
              <div className="dr-next-text">
                <div className="dr-next-name">{tr('展开为研究方案', 'Expand into a proposal')}</div>
                <div className="dr-next-hint">{tr('把这个想法写成完整方案：目标、方法、实验设计和风险。', 'Turns the idea into a full plan: goals, method, experiments and risks.')}</div>
              </div>
              <button type="button" className="btn btn-primary sm" disabled={expand.isPending} onClick={() => expand.mutate(idea.id)}>
                {expand.isPending ? tr('正在开始…', 'Starting…') : tr('展开', 'Expand')}
              </button>
            </div>
            <div className="dr-next-row">
              <div className="dr-next-text">
                <div className="dr-next-name">{tr('开始实验', 'Start experiment')}</div>
                <div className="dr-next-hint">
                  {hasMachine
                    ? tr('在你的远程机器上搭建并运行对照实验。', 'Builds and runs a controlled experiment on your remote machine.')
                    : tr('需要先添加远程机器。', 'Add a remote machine first.')}
                </div>
              </div>
              {hasMachine ? (
                <Link className="btn btn-ghost sm" to={`/t/${run.project_id}/experiment?new=${idea.id}`}>
                  {tr('开始', 'Start')}
                </Link>
              ) : (
                <Link className="btn btn-ghost sm" to="/settings?tab=ssh">
                  {tr('添加远程机器', 'Add a machine')}
                </Link>
              )}
            </div>
            <div className="dr-next-row">
              <div className="dr-next-text">
                <div className="dr-next-name">{tr('写成论文', 'Write the paper')}</div>
                <div className="dr-next-hint">
                  {hasLatex
                    ? tr('起草稿件并导出 PDF。', 'Draft the manuscript and export a PDF.')
                    : tr('可以先起草；导出 PDF 需要安装 LaTeX。', 'You can draft now; PDF export needs LaTeX installed.')}
                </div>
              </div>
              <Link className="btn btn-ghost sm" to={`/t/${run.project_id}/writer`}>
                {tr('打开写作', 'Open Writing')}
              </Link>
            </div>
          </div>
        </>
      )}
    </Section>
  );
}
