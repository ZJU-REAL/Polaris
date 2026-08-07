import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { FormField } from '../../components/ui/FormField';
import { toast } from '../../components/ui/Toast';
import { SelectMenu } from '../../components/ui/SelectMenu';
import { api, type ExperimentIntakeQuestion } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { topicPath } from '../../app/project';

/* ============================================================
   新建实验 Modal：选 promoted idea + SSH 凭据 + 预算 →
   AI 按 idea 生成 ≤5 个开题问题（代替静态 GPU 提示/高级选项表单），
   用户作答（可跳过）→ POST /projects/{pid}/experiments。
   其余不确定点实验过程中经提问机制动态交互——用户决定权更大、更灵活。
   ============================================================ */

export interface NewExperimentModalProps {
  open: boolean;
  onClose: () => void;
  pid: string;
  /** 深链 /experiment?new=<idea_id> 预选的 idea。 */
  initialIdeaId?: string | null;
}

export function NewExperimentModal({ open, onClose, pid, initialIdeaId }: NewExperimentModalProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [ideaId, setIdeaId] = useState('');
  const [credentialId, setCredentialId] = useState('');
  const [maxHours, setMaxHours] = useState('');
  const [maxRuns, setMaxRuns] = useState('10');
  const [questions, setQuestions] = useState<ExperimentIntakeQuestion[]>([]);
  const [answers, setAnswers] = useState<string[]>([]);
  const [intakeState, setIntakeState] = useState<'idle' | 'loading' | 'ready' | 'skipped'>('idle');

  const ideasQuery = useQuery({
    queryKey: ['ideas', pid, 'promoted'],
    queryFn: () => api.listIdeas(pid, { status: 'promoted' }),
    enabled: open && !!pid,
    retry: false,
  });
  const credsQuery = useQuery({
    queryKey: ['ssh-credentials'],
    queryFn: () => api.listSshCredentials(),
    enabled: open,
    retry: false,
  });
  const ideas = ideasQuery.data ?? [];
  const creds = credsQuery.data ?? [];

  // 打开时重置表单 + 应用深链预选
  useEffect(() => {
    if (!open) return;
    setIdeaId(initialIdeaId ?? '');
    setMaxHours('');
    setMaxRuns('10');
    setQuestions([]);
    setAnswers([]);
    setIntakeState('idle');
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
        credential_id: credentialId,
        params: {
          ...(intake.length > 0 ? { intake } : {}),
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
      toast(tr('实验已创建并入队', 'Experiment created and queued'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['experiments', pid] });
      void queryClient.invalidateQueries({ queryKey: ['voyages'] });
      onClose();
      navigate(`/experiment/${exp.id}`);
    },
    onError: (e) => toast(`${tr('创建失败：', 'Create failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const noIdeas = !ideasQuery.isLoading && ideas.length === 0;
  const noCreds = !credsQuery.isLoading && creds.length === 0;
  const canSubmit = !!ideaId && !!credentialId && !mutation.isPending;

  return (
    <Modal
      open={open}
      onClose={onClose}
      width={560}
      title={
        <>
          <Icon name="flask" size={16} style={{ color: 'var(--accent)' }} />
          {tr('新建实验', 'New experiment')}
        </>
      }
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>{tr('取消', 'Cancel')}</button>
          <button className="btn btn-primary" disabled={!canSubmit} onClick={() => mutation.mutate()}>
            {mutation.isPending ? (
              <>
                <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
                {tr('创建中…', 'Creating…')}
              </>
            ) : (
              <>
                <Icon name="play" size={14} />
                {tr('创建实验', 'Create experiment')}
              </>
            )}
          </button>
        </>
      }
    >
      <FormField
        label={tr('想法', 'Idea')}
        en="promoted idea"
        hint={noIdeas ? undefined : tr('仅列出已晋级的想法。', 'Only promoted ideas are listed.')}
        error={noIdeas ? tr('当前课题还没有已晋级的想法，先在想法评审页晋级一个。', 'No promoted ideas in this topic yet — promote one in Idea Review first.') : null}
      >
        <SelectMenu
          value={ideaId}
          disabled={noIdeas}
          placeholder={ideasQuery.isLoading ? tr('加载中…', 'Loading…') : ideasQuery.isError ? tr('（无法加载想法列表）', '(could not load ideas)') : tr('— 选择已晋级的想法 —', '— pick a promoted idea —')}
          options={ideas.map((i) => ({ value: i.id, label: i.title }))}
          onChange={setIdeaId}
        />
      </FormField>
      {noIdeas && (
        <div style={{ marginTop: -6, marginBottom: 14 }}>
          <button className="btn btn-soft sm" onClick={() => { onClose(); navigate(topicPath(pid, 'review')); }}>
            <Icon name="scale" size={13} />
            {tr('前往想法评审', 'Go to Idea Review')}
          </button>
        </div>
      )}

      <FormField
        label={tr('SSH 凭据', 'SSH credential')}
        en="ssh credential"
        hint={noCreds ? undefined : tr('实验将在该服务器的 ~/polaris_runs/ 下建隔离环境运行。', 'The experiment runs in an isolated environment under ~/polaris_runs/ on that server.')}
        error={noCreds ? tr('还没有 SSH 凭据，请先到设置页添加。', 'No SSH credentials yet — add one in Settings first.') : null}
      >
        <SelectMenu
          value={credentialId}
          disabled={noCreds}
          placeholder={credsQuery.isLoading ? tr('加载中…', 'Loading…') : credsQuery.isError ? tr('（无法加载凭据列表）', '(could not load credentials)') : tr('— 选择凭据 —', '— pick a credential —')}
          options={creds.map((c) => ({ value: c.id, label: `${c.name}（${c.username}@${c.host}:${c.port}）` }))}
          onChange={setCredentialId}
        />
      </FormField>
      {noCreds && (
        <div style={{ marginTop: -6, marginBottom: 14 }}>
          <button className="btn btn-soft sm" onClick={() => { onClose(); navigate('/settings?tab=ssh'); }}>
            <Icon name="settings" size={13} />
            {tr('去设置页添加 SSH 凭据', 'Add an SSH credential in Settings')}
          </button>
        </div>
      )}

      <div className="row gap12" style={{ alignItems: 'flex-start' }}>
        <FormField
          label={tr('时间预算（小时）', 'Time budget (hours)')}
          en="max_hours"
          style={{ flex: 1, minWidth: 0 }}
          hint={tr('留空 = 不限时。这是唯一的自动修复刹车：超时会暂停并问你怎么办。', 'Empty = unlimited. The only brake on auto-fixing: on timeout it pauses and asks you.')}
        >
          <input className="input mono" style={{ width: '100%' }} inputMode="decimal" value={maxHours} onChange={(e) => setMaxHours(e.target.value)} placeholder={tr('不限', 'unlimited')} />
        </FormField>
        <FormField label={tr('最多运行轮数', 'Max runs')} en="max_runs" style={{ flex: 1, minWidth: 0 }}>
          <input className="input mono" style={{ width: '100%' }} inputMode="numeric" value={maxRuns} onChange={(e) => setMaxRuns(e.target.value)} placeholder="10" />
        </FormField>
        <FormField
          label={tr('无提升自动停', 'Auto stop')}
          en="no_improve_stop"
          style={{ flex: 1, minWidth: 0 }}
          hint={tr('连续 2 轮主指标无提升自动收尾。', 'Wraps up after 2 runs in a row without metric gain.')}
        >
          <input className="input mono" style={{ width: '100%' }} value={tr('2 轮', '2 runs')} disabled />
        </FormField>
      </div>

      {/* 开题问答：选定 idea 后 AI 生成 ≤5 个整体性问题（可不答；实验中还能随时对话） */}
      {ideaId && intakeState === 'loading' && (
        <div className="row gap8" style={{ padding: '14px 4px', fontSize: 12.5, color: 'var(--text-3)' }}>
          <Icon name="sparkle" size={14} style={{ color: 'var(--accent)', animation: 'ai-dot-pulse 1.2s ease-in-out infinite' }} />
          {tr('AI 正在根据这个想法准备几个开题问题…', 'The AI is preparing a few intake questions for this idea…')}
        </div>
      )}
      {ideaId && intakeState === 'ready' && (
        <div style={{ marginBottom: 6 }}>
          <div className="row gap6" style={{ marginBottom: 10 }}>
            <Icon name="sparkle" size={14} style={{ color: 'var(--accent)' }} />
            <span style={{ fontSize: 13, fontWeight: 650 }}>
              {tr('AI 想先确认几件事', 'The AI wants to confirm a few things first')}
            </span>
            <span style={{ fontSize: 11.5, color: 'var(--text-4)' }}>
              {tr('（不答也行，实验中还能随时对话补充）', '(optional — you can also chat during the run)')}
            </span>
          </div>
          {questions.map((q, i) => (
            <FormField key={i} label={`${i + 1}. ${q.question}`} hint={q.hint ?? undefined}>
              {(q.options ?? []).length > 0 && (
                <div className="row gap6 wrap" style={{ marginBottom: 8 }}>
                  {(q.options ?? []).map((opt) => {
                    const active = (answers[i] ?? '') === opt;
                    return (
                      <button
                        key={opt}
                        type="button"
                        className="pill sm"
                        style={{
                          cursor: 'pointer',
                          border: active ? '1.5px solid var(--accent)' : '0.5px solid var(--border)',
                          background: active ? 'var(--accent-soft)' : 'var(--surface-2)',
                          color: active ? 'var(--accent-text)' : 'var(--text-2)',
                          fontWeight: active ? 700 : 500,
                          maxWidth: '100%',
                        }}
                        onClick={() =>
                          setAnswers((prev) => prev.map((a, j) => (j === i ? (active ? '' : opt) : a)))
                        }
                        title={opt}
                      >
                        {opt}
                      </button>
                    );
                  })}
                </div>
              )}
              <textarea
                className="textarea"
                rows={2}
                value={answers[i] ?? ''}
                onChange={(e) =>
                  setAnswers((prev) => prev.map((a, j) => (j === i ? e.target.value : a)))
                }
                placeholder={
                  (q.options ?? []).length > 0
                    ? tr('其他（手动输入，或点上方候选）', 'Other — type your own, or pick above')
                    : tr('（可留空，交给 AI 判断）', '(leave empty to let the AI decide)')
                }
                style={{ minHeight: 44, width: '100%' }}
              />
            </FormField>
          ))}
        </div>
      )}

      <div style={{ fontSize: 11, color: 'var(--text-4)', lineHeight: 1.6 }}>
        {tr(
          '消耗真实算力前会提交算力预算审批等待人工确认；实验中 AI 拿不准会随时暂停问你。',
          'Before real compute is spent, a budget approval awaits human sign-off; whenever the AI is unsure mid-run it pauses and asks you.',
        )}
      </div>
    </Modal>
  );
}
