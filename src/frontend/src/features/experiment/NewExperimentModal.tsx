import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Modal } from '../../components/ui/Modal';
import { FormField } from '../../components/ui/FormField';
import { toast } from '../../components/ui/Toast';
import { SelectMenu } from '../../components/ui/SelectMenu';
import { Switch } from '../../components/ui/Switch';
import { api, type ExperimentIntakeQuestion, type RunnerBackendSummary } from '../../lib/api';
import { errorText } from '../../lib/errors';
import { tr } from '../../lib/i18n';
import { selectableIdeaOptions } from '../../lib/ideaOptions';
import { topicPath } from '../../app/project';
import { ConfigRow } from '../voyages/shared/ConfigRow';
import { usableBackends } from './experimentView';
import '../voyages/task.css';
import './experiment.css';

/* ============================================================
   新建实验：只问两件必须的事——为哪个想法做、在哪台机器上做。
   选定想法后 AI 会提几个开始前想确认的问题（可以不答，过程中它还会再问）。
   轮数和时间上限、运行方式、流程模板收在「高级」里，都有默认值。

   运行方式只列真的能跑的（通过 SSH 在远程机器上运行）；清单里不要 SSH 的那几种
   选了一定会在运行时报错，所以不出现。
   ============================================================ */

export interface NewExperimentModalProps {
  open: boolean;
  onClose: () => void;
  pid: string;
  /** 深链 /experiment?new=<idea_id> 预选的 idea。 */
  initialIdeaId?: string | null;
}

/**
 * 这个后端要不要 SSH 凭据。
 *
 * 容器类后端（ngspice/openfoam/fmu）的 credential_kinds 是空的——后端 #716 已按
 * manifest 放宽，前端这道硬门是把它们挡在外面的最后一道。
 *
 * **清单还没加载出来时按「需要」处理**：那是今天的行为。反过来默认「不需要」的话，
 * 列表慢一拍就会放行一个没选凭据的 python-ml 实验，提交之后才发现跑不起来——而那
 * 时想法、开题问答都已经填完了。
 */
export function backendNeedsSsh(
  backends: RunnerBackendSummary[],
  selected: string,
): boolean {
  const chosen =
    backends.find((b) => b.backend === selected) ?? backends.find((b) => b.is_default);
  return chosen ? chosen.credential_kinds.includes('ssh') : true;
}

export function NewExperimentModal({ open, onClose, pid, initialIdeaId }: NewExperimentModalProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [ideaId, setIdeaId] = useState('');
  const [credentialId, setCredentialId] = useState('');
  const [maxHours, setMaxHours] = useState('');
  const [maxRuns, setMaxRuns] = useState('10');
  // 空串 = 用缺省后端/不选流程包，params 里就不带这两个键——存量行为逐字节不变
  const [backend, setBackend] = useState('');
  const [processPack, setProcessPack] = useState('');
  const [questions, setQuestions] = useState<ExperimentIntakeQuestion[]>([]);
  const [answers, setAnswers] = useState<string[]>([]);
  const [intakeState, setIntakeState] = useState<'idle' | 'loading' | 'ready' | 'skipped'>('idle');
  const [advanced, setAdvanced] = useState(false);
  const [confirmFirst, setConfirmFirst] = useState(false);

  const ideasQuery = useQuery({
    // 不按状态筛：后端允许从任意想法开始实验（创建时顺手把它选中）
    queryKey: ['ideas', pid, 'selectable'],
    queryFn: () => api.listIdeas(pid),
    enabled: open && !!pid,
    retry: false,
  });
  const credsQuery = useQuery({
    queryKey: ['ssh-credentials'],
    queryFn: () => api.listSshCredentials(),
    enabled: open,
    retry: false,
  });
  // 后端与流程包都问服务端要：装一个后端就该自动可选，用户自己写的流程包也一样
  const backendsQuery = useQuery({
    queryKey: ['experiment-backends'],
    queryFn: () => api.listExperimentBackends(),
    enabled: open,
    retry: false,
  });
  const packsQuery = useQuery({
    queryKey: ['process-packs'],
    queryFn: () => api.listProcessPacks(),
    enabled: open,
    retry: false,
  });
  const ideaOptions = selectableIdeaOptions(ideasQuery.data ?? []);
  const creds = credsQuery.data ?? [];
  // 只列真的能跑的运行方式（见 usableBackends）；只剩一种时不必让人选
  const backends = usableBackends(backendsQuery.data ?? []);
  const packs = packsQuery.data ?? [];
  const needsSsh = backendNeedsSsh(backends, backend);

  // 打开时重置表单 + 应用深链预选
  useEffect(() => {
    if (!open) return;
    setIdeaId(initialIdeaId ?? '');
    setMaxHours('');
    setMaxRuns('10');
    setBackend('');
    setProcessPack('');
    setQuestions([]);
    setAnswers([]);
    setIntakeState('idle');
    setAdvanced(false);
    setConfirmFirst(false);
  }, [open, initialIdeaId]);

  // 凭据加载后默认选第一个
  useEffect(() => {
    if (!open) return;
    if (creds.length > 0 && !creds.some((c) => c.id === credentialId)) {
      setCredentialId(creds[0]!.id);
    }
    if (creds.length === 0) setCredentialId('');
  }, [open, creds, credentialId]);

  // 选定 idea → AI 生成开题问题（失败/为空则静默降级：直接创建）
  useEffect(() => {
    if (!open || !ideaId) return;
    let cancelled = false;
    setIntakeState('loading');
    setQuestions([]);
    setAnswers([]);
    api
      .getExperimentIntakeQuestions(pid, ideaId)
      .then((r) => {
        if (cancelled) return;
        const qs = (r.questions ?? []).slice(0, 5);
        setQuestions(qs);
        setAnswers(qs.map(() => ''));
        setIntakeState(qs.length > 0 ? 'ready' : 'skipped');
      })
      .catch(() => {
        if (!cancelled) setIntakeState('skipped');
      });
    return () => {
      cancelled = true;
    };
  }, [open, pid, ideaId]);

  const mutation = useMutation({
    mutationFn: () => {
      const hours = Number(maxHours);
      const runs = Number(maxRuns);
      const intake = questions
        .map((q, i) => ({ question: q.question, answer: (answers[i] ?? '').trim() }))
        .filter((qa) => qa.answer !== '');
      return api.createExperiment(pid, {
        idea_id: ideaId,
        // 不需要凭据的后端这里是空串，而后端的 credential_id 是 UUID|None——
        // 发空串会 422。不选就整个不发这个键
        ...(credentialId ? { credential_id: credentialId } : {}),
        params: {
          ...(intake.length > 0 ? { intake } : {}),
          // 不选就不带键：缺省后端 / 原计划路径，与这个选择器出现之前完全一致
          ...(backend ? { backend } : {}),
          ...(processPack ? { process_pack: processPack } : {}),
          ...(confirmFirst ? { confirm_budget: true } : {}),
          budget: {
            // 留空 = 无限时（后端 0 = 不设时限）
            max_hours: Number.isFinite(hours) && hours > 0 ? hours : 0,
            ...(Number.isFinite(runs) && runs > 0 ? { max_runs: runs } : {}),
            no_improve_stop: 2,
          },
        },
      });
    },
    onSuccess: (exp) => {
      toast(tr('实验已开始', 'Experiment started'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['experiments', pid] });
      void queryClient.invalidateQueries({ queryKey: ['voyages'] });
      onClose();
      navigate(`/experiment/${exp.id}`);
    },
    onError: (e) => toast(errorText(e), 'error'),
  });

  const noIdeas = !ideasQuery.isLoading && !ideasQuery.isError && ideaOptions.length === 0;
  const noCreds = !credsQuery.isLoading && !credsQuery.isError && creds.length === 0;
  const canSubmit = !!ideaId && (!needsSsh || !!credentialId) && !mutation.isPending;
  const showBackendPicker = backends.length > 1;
  const showPackPicker = packs.length > 0;

  return (
    <Modal
      open={open}
      onClose={onClose}
      width={540}
      title={tr('新建实验', 'New experiment')}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>
            {tr('取消', 'Cancel')}
          </button>
          <button className="btn btn-primary" disabled={!canSubmit} onClick={() => mutation.mutate()}>
            {mutation.isPending ? tr('正在开始…', 'Starting…') : tr('开始实验', 'Start experiment')}
          </button>
        </>
      }
    >
      {noIdeas && (
        <div className="exp-new-missing" data-missing="idea">
          <span>{tr('这个课题还没有想法。实验是为一个想法做的，先去生成想法。', 'This topic has no ideas yet. An experiment tests an idea, so generate some first.')}</span>
          <button
            className="btn btn-ghost sm"
            onClick={() => {
              onClose();
              navigate(topicPath(pid, 'forge'));
            }}
          >
            {tr('去想法页', 'Go to Ideas')}
          </button>
        </div>
      )}
      {noCreds && (
        <div className="exp-new-missing" data-missing="machine">
          <span>{tr('还没有远程机器。实验在一台能用 SSH 连接的服务器上运行，目前不能在本机上运行。', 'No remote machine yet. Experiments run on a server you can reach over SSH; they can’t run on this computer yet.')}</span>
          <button
            className="btn btn-ghost sm"
            onClick={() => {
              onClose();
              navigate('/settings?tab=ssh');
            }}
          >
            {tr('添加远程机器', 'Add a machine')}
          </button>
        </div>
      )}

      <FormField label={tr('想法', 'Idea')}>
        <SelectMenu
          value={ideaId}
          disabled={noIdeas}
          placeholder={ideasQuery.isLoading ? tr('加载中…', 'Loading…') : ideasQuery.isError ? tr('无法加载想法', 'Couldn’t load ideas') : tr('选择想法', 'Choose an idea')}
          options={ideaOptions}
          onChange={setIdeaId}
        />
      </FormField>

      <FormField
        label={tr('远程机器', 'Remote machine')}
        hint={noCreds ? undefined : tr('实验文件放在这台机器的 ~/polaris_runs/ 下', 'Experiment files go under ~/polaris_runs/ on this machine')}
      >
        <SelectMenu
          value={credentialId}
          disabled={noCreds || !needsSsh}
          placeholder={credsQuery.isLoading ? tr('加载中…', 'Loading…') : credsQuery.isError ? tr('无法加载远程机器', 'Couldn’t load machines') : noCreds ? tr('还没有远程机器', 'No machines yet') : tr('选择机器', 'Choose a machine')}
          options={creds.map((c) => ({
            value: c.id,
            label: tr(`${c.name}（${c.username}@${c.host}:${c.port}）`, `${c.name} (${c.username}@${c.host}:${c.port})`),
          }))}
          onChange={setCredentialId}
        />
      </FormField>

      {/* 开始前的几个问题：选定想法后 AI 提出（可不答；实验中还会再问） */}
      {ideaId && intakeState === 'loading' && <div className="exp-new-foot">{tr('正在看这个想法，准备几个开始前想确认的问题…', 'Reading the idea and preparing a few questions…')}</div>}
      {ideaId && intakeState === 'ready' && (
        <div className="exp-q" data-testid="exp-intake">
          <div className="exp-q-head">
            {tr('开始前确认几件事', 'A few questions before starting')}
            <span>{tr('可以不答，AI 会自己决定', 'Optional. The AI decides anything you leave empty.')}</span>
          </div>
          {questions.map((q, i) => (
            <FormField key={i} label={`${i + 1}. ${q.question}`} hint={q.hint ?? undefined}>
              {(q.options ?? []).length > 0 && (
                <div className="exp-q-opts">
                  {(q.options ?? []).map((opt) => {
                    const on = (answers[i] ?? '') === opt;
                    return (
                      <button
                        key={opt}
                        type="button"
                        className={'btn sm ' + (on ? 'btn-soft' : 'btn-ghost')}
                        aria-pressed={on}
                        style={{ maxWidth: '100%', height: 'auto', minHeight: 28, whiteSpace: 'normal', textAlign: 'left', padding: '4px 10px' }}
                        onClick={() => setAnswers((prev) => prev.map((a, j) => (j === i ? (on ? '' : opt) : a)))}
                      >
                        {opt}
                      </button>
                    );
                  })}
                </div>
              )}
              <textarea
                className="textarea"
                rows={1}
                value={answers[i] ?? ''}
                onChange={(e) => setAnswers((prev) => prev.map((a, j) => (j === i ? e.target.value : a)))}
                placeholder={(q.options ?? []).length > 0 ? tr('或者自己写', 'Or write your own') : tr('留空则由 AI 决定', 'Leave empty to let the AI decide')}
                style={{ minHeight: 36, width: '100%' }}
              />
            </FormField>
          ))}
        </div>
      )}

      <button type="button" className="step-toggle" aria-expanded={advanced} onClick={() => setAdvanced((v) => !v)} style={{ marginTop: 10 }}>
        {tr('高级', 'Advanced')}
        <span aria-hidden="true">{advanced ? ' −' : ' +'}</span>
      </button>
      {advanced && (
        <div className="settings-list" style={{ marginTop: 4 }} data-testid="exp-advanced">
          <ConfigRow label={tr('运行前先让我确认', 'Let me confirm before it runs')} hint={tr('计划写好后停下来，等你看过计划和预算再上机器', 'Pauses once the plan is written, until you have checked the plan and budget')}>
            <Switch checked={confirmFirst} onChange={setConfirmFirst} aria-label={tr('运行前先让我确认', 'Let me confirm before it runs')} />
          </ConfigRow>
          <ConfigRow label={tr('最多轮数', 'Max rounds')} hint={tr('主指标连续 2 轮没有提升也会提前结束', 'Also ends early after 2 rounds without improvement')}>
            <input
              className="input"
              aria-label={tr('最多轮数', 'Max rounds')}
              style={{ width: 96, textAlign: 'right' }}
              inputMode="numeric"
              value={maxRuns}
              onChange={(e) => setMaxRuns(e.target.value)}
              placeholder="10"
            />
          </ConfigRow>
          <ConfigRow label={tr('时间上限（小时）', 'Time limit (hours)')} hint={tr('到时会停下来问你要不要继续', 'When time is up it stops and asks whether to continue')}>
            <input
              className="input"
              aria-label={tr('时间上限（小时）', 'Time limit (hours)')}
              style={{ width: 96, textAlign: 'right' }}
              inputMode="decimal"
              value={maxHours}
              onChange={(e) => setMaxHours(e.target.value)}
              placeholder={tr('不限', 'No limit')}
            />
          </ConfigRow>
          {showBackendPicker && (
            <ConfigRow label={tr('运行方式', 'Runner')} hint={tr('都在上面选的机器上运行', 'All of them run on the machine chosen above')}>
              <div style={{ width: 200 }}>
                <SelectMenu
                  value={backend}
                  placeholder={tr('默认', 'Default')}
                  options={backends.map((b) => ({ value: b.backend, label: b.is_default ? tr(`${b.backend}（默认）`, `${b.backend} (default)`) : b.backend }))}
                  onChange={setBackend}
                />
              </div>
            </ConfigRow>
          )}
          {showPackPicker && (
            <ConfigRow
              label={tr('流程模板', 'Workflow template')}
              hint={
                processPack
                  ? tr(`步骤：${(packs.find((p) => p.name === processPack)?.phases ?? []).join(' → ')}`, `Steps: ${(packs.find((p) => p.name === processPack)?.phases ?? []).join(' → ')}`)
                  : tr('不选就按常规方式安排步骤', 'Leave unset for the usual steps')
              }
            >
              <div style={{ width: 200 }}>
                <SelectMenu value={processPack} placeholder={tr('不使用', 'None')} options={[{ value: '', label: tr('不使用', 'None') }, ...packs.map((p) => ({ value: p.name, label: p.name }))]} onChange={setProcessPack} />
              </div>
            </ConfigRow>
          )}
        </div>
      )}

      <div className="exp-new-foot">
        {confirmFirst
          ? tr('开始后 AI 先写实验计划，然后停下来等你确认，再在这台机器上搭环境、运行。', 'The AI writes a plan, waits for you to confirm it, then sets up and runs on this machine.')
          : tr('开始后 AI 先写实验计划，接着就在这台机器上搭环境、运行。拿不准的地方它会停下来问你。', 'The AI writes a plan, then sets up and runs on this machine. It stops to ask when it is unsure.')}
      </div>
    </Modal>
  );
}
