import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { FormField } from '../../components/ui/FormField';
import { toast } from '../../components/ui/Toast';
import { api } from '../../lib/api';

/* ============================================================
   新建实验 Modal：选 promoted idea + SSH 凭据 + 预算 →
   POST /projects/{pid}/experiments（后端同时创建 experiment voyage）。
   无凭据时提示去设置页添加。
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
  const [maxHours, setMaxHours] = useState('4');
  const [maxRuns, setMaxRuns] = useState('10');
  const [gpuHint, setGpuHint] = useState('');

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
    setMaxHours('4');
    setMaxRuns('10');
    setGpuHint('');
  }, [open, initialIdeaId]);

  // 凭据加载后默认选第一个
  useEffect(() => {
    if (!open) return;
    if (creds.length > 0 && !creds.some((c) => c.id === credentialId)) {
      setCredentialId(creds[0]!.id);
    }
    if (creds.length === 0) setCredentialId('');
  }, [open, creds, credentialId]);

  const mutation = useMutation({
    mutationFn: () => {
      const hours = Number(maxHours);
      const runs = Number(maxRuns);
      return api.createExperiment(pid, {
        idea_id: ideaId,
        credential_id: credentialId,
        params: {
          ...(gpuHint.trim() ? { gpu_hint: gpuHint.trim() } : {}),
          budget: {
            ...(Number.isFinite(hours) && hours > 0 ? { max_hours: hours } : {}),
            ...(Number.isFinite(runs) && runs > 0 ? { max_runs: runs } : {}),
          },
        },
      });
    },
    onSuccess: (exp) => {
      toast('实验已创建并入队 · experiment queued', 'ok');
      void queryClient.invalidateQueries({ queryKey: ['experiments', pid] });
      void queryClient.invalidateQueries({ queryKey: ['voyages'] });
      onClose();
      navigate(`/experiment/${exp.id}`);
    },
    onError: (e) => toast(`创建失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
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
          新建实验
        </>
      }
      sub="从已晋级 idea 发起：计划 → 预算闸门 → SSH 建环境 → 冒烟 → 正式运行 → 报告"
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>取消</button>
          <button className="btn btn-primary" disabled={!canSubmit} onClick={() => mutation.mutate()}>
            {mutation.isPending ? (
              <>
                <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
                创建中…
              </>
            ) : (
              <>
                <Icon name="play" size={14} />
                创建实验
              </>
            )}
          </button>
        </>
      }
    >
      <FormField
        label="Idea"
        en="promoted idea"
        hint={noIdeas ? undefined : '仅列出 status=promoted 的 idea（评审页晋级并审批通过）。'}
        error={noIdeas ? '当前方向还没有已晋级的 idea，先在评审页晋级一个。' : null}
      >
        <select className="input" value={ideaId} onChange={(e) => setIdeaId(e.target.value)} disabled={noIdeas}>
          <option value="" disabled>
            {ideasQuery.isLoading ? '加载中…' : ideasQuery.isError ? '（无法加载 idea 列表）' : '— 选择已晋级 idea —'}
          </option>
          {ideas.map((i) => (
            <option key={i.id} value={i.id}>{i.title}</option>
          ))}
        </select>
      </FormField>
      {noIdeas && (
        <div style={{ marginTop: -6, marginBottom: 14 }}>
          <button className="btn btn-soft sm" onClick={() => { onClose(); navigate('/review'); }}>
            <Icon name="scale" size={13} />
            前往 Idea 评审
          </button>
        </div>
      )}

      <FormField
        label="SSH 凭据"
        en="ssh credential"
        hint={noCreds ? undefined : '实验将在该服务器的 ~/polaris_runs/ 下建隔离环境运行。'}
        error={noCreds ? '还没有 SSH 凭据，请先到设置页添加。' : null}
      >
        <select className="input" value={credentialId} onChange={(e) => setCredentialId(e.target.value)} disabled={noCreds}>
          <option value="" disabled>
            {credsQuery.isLoading ? '加载中…' : credsQuery.isError ? '（无法加载凭据列表）' : '— 选择凭据 —'}
          </option>
          {creds.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}（{c.username}@{c.host}:{c.port}）
            </option>
          ))}
        </select>
      </FormField>
      {noCreds && (
        <div style={{ marginTop: -6, marginBottom: 14 }}>
          <button className="btn btn-soft sm" onClick={() => { onClose(); navigate('/settings'); }}>
            <Icon name="settings" size={13} />
            去设置页添加 SSH 凭据
          </button>
        </div>
      )}

      <div className="row gap12" style={{ alignItems: 'flex-start' }}>
        <FormField label="预算 · 最长时数" en="max_hours" style={{ flex: 1 }}>
          <input className="input mono" inputMode="decimal" value={maxHours} onChange={(e) => setMaxHours(e.target.value)} placeholder="4" />
        </FormField>
        <FormField label="预算 · 最多 run 数" en="max_runs" style={{ flex: 1 }}>
          <input className="input mono" inputMode="numeric" value={maxRuns} onChange={(e) => setMaxRuns(e.target.value)} placeholder="10" />
        </FormField>
      </div>
      <FormField label="GPU 提示（可选）" en="gpu_hint" hint="如 A100 / cuda:0，供计划阶段参考。">
        <input className="input mono" value={gpuHint} onChange={(e) => setGpuHint(e.target.value)} placeholder="如 1×A100" />
      </FormField>
      <div style={{ fontSize: 11, color: 'var(--text-4)', lineHeight: 1.6 }}>
        消耗真实算力前会创建 compute_budget 闸门等待人工确认；超时/超预算自动 kill 并置 failed。
      </div>
    </Modal>
  );
}
