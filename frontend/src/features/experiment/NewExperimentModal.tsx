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
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [evalModel, setEvalModel] = useState('');
  const [hfMirror, setHfMirror] = useState(false);
  const [extraNotes, setExtraNotes] = useState('');

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
    setShowAdvanced(false);
    setEvalModel('');
    setHfMirror(false);
    setExtraNotes('');
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
          ...(evalModel.trim() ? { eval_model: evalModel.trim() } : {}),
          ...(hfMirror ? { hf_mirror: true } : {}),
          ...(extraNotes.trim() ? { extra_notes: extraNotes.trim() } : {}),
          budget: {
            ...(Number.isFinite(hours) && hours > 0 ? { max_hours: hours } : {}),
            ...(Number.isFinite(runs) && runs > 0 ? { max_runs: runs } : {}),
            // M5-A 固定值：连续 2 轮主指标无提升自动停止
            no_improve_stop: 2,
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
      sub="从已晋级 idea 发起：计划 → 算力预算审批 → SSH 建环境 → 冒烟 → 自动迭代 → 图表 → 报告"
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
        <FormField label="预算 · 最多运行轮数" en="max_runs" style={{ flex: 1 }}>
          <input className="input mono" inputMode="numeric" value={maxRuns} onChange={(e) => setMaxRuns(e.target.value)} placeholder="10" />
        </FormField>
        <FormField
          label="预算 · 无提升自动停"
          en="no_improve_stop"
          style={{ flex: 1 }}
          hint="固定值：连续 2 轮主指标无提升自动停止。"
        >
          <input className="input mono" value="2 轮" disabled />
        </FormField>
      </div>
      <FormField label="GPU 提示（可选）" en="gpu_hint" hint="如 A100 / cuda:0，供计划阶段参考。">
        <input className="input mono" value={gpuHint} onChange={(e) => setGpuHint(e.target.value)} placeholder="如 1×A100" />
      </FormField>

      <button
        type="button"
        className="btn btn-ghost sm"
        style={{ marginBottom: showAdvanced ? 10 : 14, paddingLeft: 0 }}
        onClick={() => setShowAdvanced((v) => !v)}
      >
        <Icon name={showAdvanced ? 'chevDown' : 'chevron'} size={13} />
        高级选项 <span style={{ color: 'var(--text-4)', fontSize: 11 }}>advanced</span>
      </button>
      {showAdvanced && (
        <>
          <FormField
            label="评测模型（可选）"
            en="eval_model"
            hint="实验代码将获得该模型的 API 访问（平台把接入点与密钥写入工作目录 llm_config.json），用于 ReAct 等 training-free 的 agentic 评测。"
          >
            <input
              className="input mono"
              value={evalModel}
              onChange={(e) => setEvalModel(e.target.value)}
              placeholder="qwen36-35b-a3b"
            />
          </FormField>
          <FormField
            label="HuggingFace 镜像"
            en="hf_mirror"
            hint="训练类实验从 hf-mirror.com 拉取模型与数据集（大陆网络推荐勾选）。"
          >
            <label className="row gap8" style={{ fontSize: 12.5, cursor: 'pointer' }}>
              <input type="checkbox" checked={hfMirror} onChange={(e) => setHfMirror(e.target.checked)} />
              启用 HF 镜像（注入 HF_ENDPOINT）
            </label>
          </FormField>
          <FormField
            label="补充说明（可选）"
            en="extra_notes"
            hint="对实验的额外要求，会原文提供给计划与代码生成的 AI，比如指定数据集子集、评测协议、对比基线。"
          >
            <textarea
              className="textarea"
              rows={3}
              value={extraNotes}
              onChange={(e) => setExtraNotes(e.target.value)}
              placeholder="如：只评测 ALFWorld 前 30 个任务；必须对比 ReAct 基线"
            />
          </FormField>
        </>
      )}

      <div style={{ fontSize: 11, color: 'var(--text-4)', lineHeight: 1.6 }}>
        消耗真实算力前会提交算力预算审批等待人工确认；超时/超预算自动 kill 并置 failed。
        进入自动迭代后，AI 每轮跑完会分析结果并决定继续改进、修错重试或停止；连续 2 轮主指标无提升也会自动停止。
      </div>
    </Modal>
  );
}
