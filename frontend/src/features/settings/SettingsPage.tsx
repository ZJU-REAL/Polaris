import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Avatar } from '../../components/ui/Avatar';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { Segmented } from '../../components/ui/Segmented';
import { Modal } from '../../components/ui/Modal';
import { FormField } from '../../components/ui/FormField';
import { toast } from '../../components/ui/Toast';
import { fmtTime } from '../../lib/format';
import {
  LLM_STAGES,
  api,
  isAdmin,
  type AdminUserRead,
  type LlmProviderInput,
  type LlmProviderKind,
  type LlmProviderRead,
  type LlmRoute,
  type SshCredentialInput,
} from '../../lib/api';

/* ============================================================
   /settings — ①个人（只读）②SSH 凭据（所有人）
   ③LLM 管理（admin）④用量（admin）
   ============================================================ */

const KINDS: LlmProviderKind[] = ['openai_compat', 'anthropic', 'fake'];

// ---------------- 个人 ----------------

function PersonalTab() {
  const queryClient = useQueryClient();
  const { data: me, isLoading, isError } = useQuery({ queryKey: ['me'], queryFn: () => api.me(), retry: false });
  const { data: usage } = useQuery({ queryKey: ['my-usage'], queryFn: () => api.myUsage(), retry: false });
  const [name, setName] = useState('');
  const [avatarVersion, setAvatarVersion] = useState(0);
  const avatarInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (me) setName(me.display_name ?? '');
  }, [me]);

  const saveMutation = useMutation({
    mutationFn: () => api.updateMe({ display_name: name.trim() }),
    onSuccess: () => {
      toast('个人资料已保存', 'ok');
      void queryClient.invalidateQueries({ queryKey: ['me'] });
    },
    onError: (e) => toast(`保存失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  const avatarMutation = useMutation({
    mutationFn: (file: File) => api.uploadAvatar(file),
    onSuccess: () => {
      toast('头像已更新', 'ok');
      setAvatarVersion((v) => v + 1);
      void queryClient.invalidateQueries({ queryKey: ['me'] });
      void queryClient.invalidateQueries({ queryKey: ['avatar'] });
    },
    onError: (e) => {
      const msg = e instanceof Error ? e.message : String(e);
      toast(msg === 'AVATAR_TOO_LARGE' ? '图片超过 2MB' : msg === 'AVATAR_NOT_IMAGE' ? '不是有效的图片文件' : `上传失败：${msg}`, 'error');
    },
  });

  if (isLoading) return <div className="empty">加载中…</div>;
  if (isError || !me) return <div className="empty">无法加载用户信息（后端不可用）</div>;

  return (
    <div className="card card-pad" style={{ maxWidth: 560 }}>
      <div className="row gap16" style={{ marginBottom: 20 }}>
        <Avatar userId={me.id} hasAvatar={!!me.has_avatar} name={me.display_name || me.email} size={64} version={avatarVersion} />
        <div>
          <input
            ref={avatarInputRef}
            type="file"
            accept="image/png,image/jpeg,image/webp"
            style={{ display: 'none' }}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) avatarMutation.mutate(f);
              e.target.value = '';
            }}
          />
          <button className="btn btn-soft" disabled={avatarMutation.isPending} onClick={() => avatarInputRef.current?.click()}>
            {avatarMutation.isPending ? '上传中…' : '更换头像'}
          </button>
          <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 6 }}>PNG / JPEG / WebP，2MB 以内</div>
        </div>
      </div>
      <FormField label="显示名" en="Display name">
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="如：王小明" />
      </FormField>
      <FormField label="邮箱" en="Email">
        <input className="input" value={me.email} disabled />
      </FormField>
      <div className="row gap12" style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 12.5, color: 'var(--text-2)' }}>
          角色：{me.role === 'admin' ? '管理员' : '成员'}
        </div>
        {usage && (
          <div style={{ fontSize: 12.5, color: 'var(--text-2)' }}>
            AI 用量：{usage.tokens_used.toLocaleString()} tokens
            {usage.token_quota != null && ` / 配额 ${usage.token_quota.toLocaleString()}`}
          </div>
        )}
      </div>
      <div className="row" style={{ justifyContent: 'flex-end' }}>
        <button
          className="btn btn-primary"
          disabled={saveMutation.isPending || (me.display_name ?? '') === name.trim()}
          onClick={() => saveMutation.mutate()}
        >
          保存
        </button>
      </div>
    </div>
  );
}

// ---------------- SSH 凭据（M4） ----------------

interface SshDraft {
  name: string;
  host: string;
  port: string;
  username: string;
  private_key: string;
  passphrase: string;
  proxy_url: string;
}

function emptySshDraft(): SshDraft {
  return { name: '', host: '', port: '22', username: '', private_key: '', passphrase: '', proxy_url: '' };
}

function toSshInput(d: SshDraft): SshCredentialInput {
  const port = Number(d.port);
  return {
    name: d.name.trim(),
    host: d.host.trim(),
    ...(Number.isInteger(port) && port > 0 ? { port } : {}),
    username: d.username.trim(),
    private_key: d.private_key,
    ...(d.passphrase ? { passphrase: d.passphrase } : {}),
    ...(d.proxy_url.trim() ? { proxy_url: d.proxy_url.trim() } : {}),
  };
}

function SshTab() {
  const queryClient = useQueryClient();
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['ssh-credentials'],
    queryFn: () => api.listSshCredentials(),
    retry: false,
  });
  const creds = data ?? [];

  const [modalOpen, setModalOpen] = useState(false);
  const [draft, setDraft] = useState<SshDraft>(emptySshDraft());

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ['ssh-credentials'] });

  const createMutation = useMutation({
    mutationFn: () => api.createSshCredential(toSshInput(draft)),
    onSuccess: () => {
      toast('SSH 凭据已添加（私钥加密存储）', 'ok');
      setModalOpen(false);
      invalidate();
    },
    onError: (e) => toast(`添加失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteSshCredential(id),
    onSuccess: () => {
      toast('凭据已删除', 'ok');
      invalidate();
    },
    onError: (e) => toast(`删除失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  const testMutation = useMutation({
    mutationFn: (id: string) => api.testSshCredential(id),
    onSuccess: (r) => {
      toast(r.ok ? `连接成功：${r.detail}` : `连接失败：${r.detail}`, r.ok ? 'ok' : 'error');
      if (r.ok) invalidate(); // 后端更新 last_verified_at
    },
    onError: (e) => toast(`测试失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const canSave =
    draft.name.trim() !== '' &&
    draft.host.trim() !== '' &&
    draft.username.trim() !== '' &&
    draft.private_key.trim() !== '' &&
    !createMutation.isPending;

  return (
    <div className="card card-pad">
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 6 }}>
        <span className="section-h">
          <Icon name="server" size={15} style={{ color: 'var(--accent)' }} />
          SSH 凭据 <span className="en-label" style={{ fontSize: 11 }}>实验用远程服务器</span>
        </span>
        <button className="btn btn-primary sm" onClick={() => { setDraft(emptySshDraft()); setModalOpen(true); }}>
          <Icon name="plus" size={13} />
          添加凭据
        </button>
      </div>
      <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginBottom: 14, lineHeight: 1.5 }}>
        私钥加密存储（Fernet），仅用于你自己的实验任务，绝不回传前端；实验工作目录限定 ~/polaris_runs/。
      </div>

      {isLoading ? (
        <div className="empty" style={{ padding: 24 }}>加载中…</div>
      ) : isError ? (
        <div className="empty" style={{ padding: 24 }}>
          无法加载（后端不可用或接口尚未就绪）
          <div style={{ marginTop: 10 }}>
            <button className="btn btn-soft sm" onClick={() => void refetch()}>重试</button>
          </div>
        </div>
      ) : creds.length === 0 ? (
        <div className="empty" style={{ padding: 24 }}>
          还没有 SSH 凭据 — 添加一台 GPU 服务器后即可在 Experiment Lab 发起实验
        </div>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>名称</th>
              <th>host</th>
              <th style={{ width: 60 }}>port</th>
              <th>username</th>
              <th style={{ width: 130 }}>最近验证</th>
              <th style={{ width: 150 }} />
            </tr>
          </thead>
          <tbody>
            {creds.map((c) => (
              <tr key={c.id}>
                <td style={{ fontWeight: 600 }}>{c.name}</td>
                <td className="mono" style={{ fontSize: 11.5 }}>{c.host}</td>
                <td className="mono" style={{ fontSize: 11.5 }}>{c.port}</td>
                <td className="mono" style={{ fontSize: 11.5 }}>{c.username}</td>
                <td className="mono" style={{ fontSize: 11, color: c.last_verified_at ? 'var(--ok-tx)' : 'var(--text-4)' }}>
                  {c.last_verified_at ? fmtTime(c.last_verified_at) : '从未验证'}
                </td>
                <td>
                  <div className="row gap6" style={{ justifyContent: 'flex-end' }}>
                    <button
                      className="btn btn-soft sm"
                      disabled={testMutation.isPending}
                      onClick={() => testMutation.mutate(c.id)}
                    >
                      {testMutation.isPending && testMutation.variables === c.id ? '连接中…' : '测试连接'}
                    </button>
                    <button
                      className="icon-btn"
                      style={{ width: 26, height: 26 }}
                      title="删除"
                      disabled={deleteMutation.isPending}
                      onClick={() => {
                        if (window.confirm(`确定删除凭据 “${c.name}” ？使用中的实验将无法再连接该服务器。`)) {
                          deleteMutation.mutate(c.id);
                        }
                      }}
                    >
                      <Icon name="trash" size={13} />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <Modal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        width={560}
        title="添加 SSH 凭据"
        sub="私钥加密存储，仅用于你自己的实验任务"
        footer={
          <>
            <button className="btn btn-ghost" onClick={() => setModalOpen(false)}>取消</button>
            <button className="btn btn-primary" disabled={!canSave} onClick={() => createMutation.mutate()}>
              {createMutation.isPending ? '保存中…' : '保存'}
            </button>
          </>
        }
      >
        <FormField label="名称" en="name">
          <input className="input" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            placeholder="如 lab-gpu-1" />
        </FormField>
        <div className="row gap12" style={{ alignItems: 'flex-start' }}>
          <FormField label="主机" en="host" style={{ flex: 1 }}>
            <input className="input mono" value={draft.host} onChange={(e) => setDraft({ ...draft, host: e.target.value })}
              placeholder="gpu.example.edu" />
          </FormField>
          <FormField label="端口" en="port" style={{ width: 100 }}>
            <input className="input mono" inputMode="numeric" value={draft.port}
              onChange={(e) => setDraft({ ...draft, port: e.target.value })} placeholder="22" />
          </FormField>
        </div>
        <FormField label="用户名" en="username">
          <input className="input mono" value={draft.username} onChange={(e) => setDraft({ ...draft, username: e.target.value })}
            placeholder="ubuntu" autoComplete="off" />
        </FormField>
        <FormField label="私钥" en="private_key（PEM）" hint="粘贴完整 PEM 文本；后端 Fernet 加密入库，只写不读。">
          <textarea
            className="textarea mono"
            style={{ minHeight: 130, fontSize: 11 }}
            value={draft.private_key}
            onChange={(e) => setDraft({ ...draft, private_key: e.target.value })}
            placeholder={'-----BEGIN OPENSSH PRIVATE KEY-----\n…\n-----END OPENSSH PRIVATE KEY-----'}
            autoComplete="off"
            spellCheck={false}
          />
        </FormField>
        <FormField label="密钥口令（可选）" en="passphrase">
          <input className="input mono" type="password" autoComplete="new-password" value={draft.passphrase}
            onChange={(e) => setDraft({ ...draft, passphrase: e.target.value })} placeholder="私钥无口令则留空" />
        </FormField>
        <FormField label="出外网代理（可选）" en="proxy">
          <input className="input mono" value={draft.proxy_url}
            onChange={(e) => setDraft({ ...draft, proxy_url: e.target.value })}
            placeholder="如 http://10.205.70.120:7899，服务器直连外网则留空" />
          <div className="muted" style={{ fontSize: 11.5, marginTop: 4 }}>
            实验装依赖、下载模型数据时自动走该代理；内网 LLM 接口不受影响
          </div>
        </FormField>
      </Modal>
    </div>
  );
}

// ---------------- LLM Providers ----------------

interface ProviderDraft {
  name: string;
  kind: LlmProviderKind;
  base_url: string;
  api_key: string;
  enabled: boolean;
}

function emptyDraft(): ProviderDraft {
  return { name: '', kind: 'openai_compat', base_url: '', api_key: '', enabled: true };
}

function draftFrom(p: LlmProviderRead): ProviderDraft {
  return { name: p.name, kind: p.kind, base_url: p.base_url ?? '', api_key: '', enabled: p.enabled };
}

function toInput(d: ProviderDraft): LlmProviderInput {
  return {
    name: d.name.trim(),
    kind: d.kind,
    base_url: d.base_url.trim() || undefined,
    api_key: d.api_key, // 空字符串 = 不变（PATCH）；POST 时后端忽略空 key
    enabled: d.enabled,
  };
}

function ProviderForm({ draft, setDraft, isNew }: {
  draft: ProviderDraft;
  setDraft: (d: ProviderDraft) => void;
  isNew: boolean;
}) {
  return (
    <>
      <FormField label="名称" en="Name">
        <input className="input" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })}
          placeholder="如 deepseek / claude / local-fake" />
      </FormField>
      <div className="row gap12" style={{ alignItems: 'flex-start' }}>
        <FormField label="类型" en="Kind" style={{ width: 180 }}>
          <select className="input" value={draft.kind}
            onChange={(e) => setDraft({ ...draft, kind: e.target.value as LlmProviderKind })}>
            {KINDS.map((k) => (
              <option key={k} value={k}>{k}</option>
            ))}
          </select>
        </FormField>
        <FormField label="Base URL" en="base_url" style={{ flex: 1 }}>
          <input className="input mono" value={draft.base_url} onChange={(e) => setDraft({ ...draft, base_url: e.target.value })}
            placeholder="https://api.example.com/v1" disabled={draft.kind === 'fake'} />
        </FormField>
      </div>
      <FormField label="API Key" en="api_key"
        hint={isNew ? 'fake provider 无需 key' : '留空 = 保持不变；后端只写不读，展示为 masked'}>
        <input className="input mono" type="password" autoComplete="new-password" value={draft.api_key}
          onChange={(e) => setDraft({ ...draft, api_key: e.target.value })}
          placeholder={isNew ? 'sk-…' : '••••••（留空不变）'} disabled={draft.kind === 'fake'} />
      </FormField>
      <label className="row gap8" style={{ fontSize: 13, cursor: 'pointer', userSelect: 'none' }}>
        <input type="checkbox" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />
        启用 enabled
      </label>
    </>
  );
}

function ProvidersSection() {
  const queryClient = useQueryClient();
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['llm', 'providers'],
    queryFn: () => api.listLlmProviders(),
    retry: false,
  });
  const providers = data ?? [];

  const [modal, setModal] = useState<'closed' | 'create' | string>('closed'); // string = 编辑中的 provider id
  const [draft, setDraft] = useState<ProviderDraft>(emptyDraft());

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ['llm'] });

  const createMutation = useMutation({
    mutationFn: () => api.createLlmProvider(toInput(draft)),
    onSuccess: () => {
      toast('Provider 已创建', 'ok');
      setModal('closed');
      invalidate();
    },
    onError: (err) => toast(`创建失败：${err instanceof Error ? err.message : String(err)}`, 'error'),
  });
  const patchMutation = useMutation({
    mutationFn: (id: string) => api.patchLlmProvider(id, toInput(draft)),
    onSuccess: () => {
      toast('Provider 已更新', 'ok');
      setModal('closed');
      invalidate();
    },
    onError: (err) => toast(`更新失败：${err instanceof Error ? err.message : String(err)}`, 'error'),
  });
  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteLlmProvider(id),
    onSuccess: () => {
      toast('Provider 已删除', 'ok');
      invalidate();
    },
    onError: (err) => toast(`删除失败：${err instanceof Error ? err.message : String(err)}`, 'error'),
  });

  const isNew = modal === 'create';
  const busy = createMutation.isPending || patchMutation.isPending;

  return (
    <div className="card card-pad" style={{ marginBottom: 20 }}>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 14 }}>
        <span className="section-h">
          <Icon name="server" size={15} style={{ color: 'var(--accent)' }} />
          Providers <span className="en-label" style={{ fontSize: 11 }}>LLM 供应商</span>
        </span>
        <button className="btn btn-primary sm" onClick={() => { setDraft(emptyDraft()); setModal('create'); }}>
          <Icon name="plus" size={13} />
          新增 Provider
        </button>
      </div>

      {isLoading ? (
        <div className="empty" style={{ padding: 24 }}>加载中…</div>
      ) : isError ? (
        <div className="empty" style={{ padding: 24 }}>
          无法加载（后端不可用或无权限）
          <div style={{ marginTop: 10 }}>
            <button className="btn btn-soft sm" onClick={() => void refetch()}>重试</button>
          </div>
        </div>
      ) : providers.length === 0 ? (
        <div className="empty" style={{ padding: 24 }}>还没有 provider，先添加一个（kind=fake 可无 key 演示）</div>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>名称</th>
              <th>kind</th>
              <th>base_url</th>
              <th>api_key</th>
              <th style={{ width: 60 }}>状态</th>
              <th style={{ width: 90 }} />
            </tr>
          </thead>
          <tbody>
            {providers.map((p) => (
              <tr key={p.id}>
                <td style={{ fontWeight: 600 }}>{p.name}</td>
                <td className="mono" style={{ fontSize: 11.5 }}>{p.kind}</td>
                <td className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)', maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {p.base_url ?? '—'}
                </td>
                <td className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)' }}>{p.api_key_masked ?? '—'}</td>
                <td>
                  <span className="pill sm" style={p.enabled ? { background: 'var(--ok-bg)', color: 'var(--ok-tx)' } : {}}>
                    {p.enabled ? '启用' : '停用'}
                  </span>
                </td>
                <td>
                  <div className="row gap6" style={{ justifyContent: 'flex-end' }}>
                    <button className="icon-btn" style={{ width: 26, height: 26 }} title="编辑"
                      onClick={() => { setDraft(draftFrom(p)); setModal(p.id); }}>
                      <Icon name="pen" size={13} />
                    </button>
                    <button className="icon-btn" style={{ width: 26, height: 26 }} title="删除"
                      disabled={deleteMutation.isPending}
                      onClick={() => deleteMutation.mutate(p.id)}>
                      <Icon name="trash" size={13} />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <Modal
        open={modal !== 'closed'}
        onClose={() => setModal('closed')}
        title={isNew ? '新增 Provider' : '编辑 Provider'}
        sub="api_key 后端只写不读；kind=fake 用于测试与无 key 演示"
        footer={
          <>
            <button className="btn btn-ghost" onClick={() => setModal('closed')}>取消</button>
            <button className="btn btn-primary" disabled={!draft.name.trim() || busy}
              onClick={() => (isNew ? createMutation.mutate() : patchMutation.mutate(modal))}>
              {busy ? '保存中…' : '保存'}
            </button>
          </>
        }
      >
        <ProviderForm draft={draft} setDraft={setDraft} isNew={isNew} />
      </Modal>
    </div>
  );
}

// ---------------- 模型路由表 ----------------

/** stage 中文标签（代码标识符照常显示在下方）。 */
const STAGE_ZH: Record<string, string> = {
  default: '默认',
  navigator: '任务规划',
  sextant: '自动校验',
  interview: '方向访谈',
  relevance: '相关度打分',
  librarian: '文献抓取',
  reading: '精读编译',
  embedding: '向量嵌入',
  forge: '想法生成',
  forge_signal: '信号摘要',
  goal_explore: '目标构建',
  proposal: '方案起草',
  proposal_review: '方案评审',
  debate: '辩论评审',
  experiment: '实验',
  writing: '论文撰写',
  review: '论文评审',
};

interface RouteDraft {
  provider_id: string;
  model: string;
  temperature: string;
}

function RoutesSection() {
  const queryClient = useQueryClient();
  const providersQuery = useQuery({ queryKey: ['llm', 'providers'], queryFn: () => api.listLlmProviders(), retry: false });
  const routesQuery = useQuery({ queryKey: ['llm', 'routes'], queryFn: () => api.getLlmRoutes(), retry: false });
  const providers = providersQuery.data ?? [];

  const [rows, setRows] = useState<Record<string, RouteDraft>>({});
  useEffect(() => {
    if (!routesQuery.data) return;
    const next: Record<string, RouteDraft> = {};
    for (const r of routesQuery.data) {
      next[r.stage] = {
        provider_id: r.provider_id,
        model: r.model,
        temperature: r.temperature === null || r.temperature === undefined ? '' : String(r.temperature),
      };
    }
    setRows(next);
  }, [routesQuery.data]);

  const saveMutation = useMutation({
    mutationFn: () => {
      const routes: LlmRoute[] = [];
      for (const stage of LLM_STAGES) {
        const r = rows[stage];
        if (!r || !r.provider_id || !r.model.trim()) continue;
        const t = r.temperature.trim();
        routes.push({
          stage,
          provider_id: r.provider_id,
          model: r.model.trim(),
          ...(t !== '' && Number.isFinite(Number(t)) ? { temperature: Number(t) } : {}),
        });
      }
      return api.putLlmRoutes(routes);
    },
    onSuccess: () => {
      toast('模型路由表已保存', 'ok');
      void queryClient.invalidateQueries({ queryKey: ['llm', 'routes'] });
    },
    onError: (err) => toast(`保存失败：${err instanceof Error ? err.message : String(err)}`, 'error'),
  });

  const setRow = (stage: string, patch: Partial<RouteDraft>) =>
    setRows((prev) => ({
      ...prev,
      [stage]: { provider_id: '', model: '', temperature: '', ...prev[stage], ...patch },
    }));

  return (
    <div className="card card-pad">
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 14 }}>
        <span className="section-h">
          <Icon name="git" size={15} style={{ color: 'var(--accent)' }} />
          模型路由表 <span className="en-label" style={{ fontSize: 11 }}>Model routes（整表保存）</span>
        </span>
        <button className="btn btn-primary sm" disabled={saveMutation.isPending} onClick={() => saveMutation.mutate()}>
          <Icon name="check" size={13} />
          {saveMutation.isPending ? '保存中…' : '保存路由表'}
        </button>
      </div>
      {routesQuery.isError && (
        <div className="field-hint" style={{ marginBottom: 10, color: 'var(--warn-tx)' }}>
          路由表加载失败（后端不可用），保存将覆盖整表。
        </div>
      )}
      <table className="table">
        <thead>
          <tr>
            <th style={{ width: 130 }}>stage</th>
            <th style={{ width: 180 }}>provider</th>
            <th>model</th>
            <th style={{ width: 110 }}>temperature</th>
          </tr>
        </thead>
        <tbody>
          {LLM_STAGES.map((stage) => {
            const r = rows[stage] ?? { provider_id: '', model: '', temperature: '' };
            return (
              <tr key={stage}>
                <td>
                  <div style={{ fontSize: 12, fontWeight: 650 }}>{STAGE_ZH[stage] ?? stage}</div>
                  <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>{stage}</div>
                </td>
                <td>
                  <select className="input" style={{ height: 32, width: '100%' }} value={r.provider_id}
                    onChange={(e) => setRow(stage, { provider_id: e.target.value })}>
                    <option value="">（未配置）</option>
                    {providers.map((p) => (
                      <option key={p.id} value={p.id}>{p.name}</option>
                    ))}
                  </select>
                </td>
                <td>
                  <input className="input mono" style={{ height: 32, width: '100%', fontSize: 12 }} value={r.model}
                    placeholder="如 deepseek-chat" onChange={(e) => setRow(stage, { model: e.target.value })} />
                </td>
                <td>
                  <input className="input mono" style={{ height: 32, width: '100%', fontSize: 12 }} value={r.temperature}
                    placeholder="默认" inputMode="decimal" onChange={(e) => setRow(stage, { temperature: e.target.value })} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function LlmTab() {
  return (
    <>
      <ProvidersSection />
      <RoutesSection />
    </>
  );
}

// ---------------- 用量 ----------------

function UsageTab() {
  const [days, setDays] = useState<'7' | '30' | '90'>('30');
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['llm', 'usage', days],
    queryFn: () => api.getLlmUsage({ days: Number(days) }),
    retry: false,
  });
  const rows = useMemo(() => data ?? [], [data]);
  const totals = useMemo(
    () =>
      rows.reduce(
        (acc, r) => ({
          prompt: acc.prompt + r.prompt_tokens,
          completion: acc.completion + r.completion_tokens,
          calls: acc.calls + r.calls,
        }),
        { prompt: 0, completion: 0, calls: 0 },
      ),
    [rows],
  );

  return (
    <div className="card card-pad">
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 14 }}>
        <span className="section-h">
          <Icon name="chart" size={15} style={{ color: 'var(--accent)' }} />
          LLM 用量 <span className="en-label" style={{ fontSize: 11 }}>按天 × stage</span>
        </span>
        <Segmented options={[{ v: '7' as const, label: '7 天' }, { v: '30' as const, label: '30 天' }, { v: '90' as const, label: '90 天' }]}
          value={days} onChange={setDays} />
      </div>
      {isLoading ? (
        <div className="empty" style={{ padding: 24 }}>加载中…</div>
      ) : isError ? (
        <div className="empty" style={{ padding: 24 }}>
          无法加载用量数据（后端不可用或无权限）
          <div style={{ marginTop: 10 }}>
            <button className="btn btn-soft sm" onClick={() => void refetch()}>重试</button>
          </div>
        </div>
      ) : rows.length === 0 ? (
        <div className="empty" style={{ padding: 24 }}>近 {days} 天暂无用量记录</div>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>日期</th>
              <th>stage</th>
              <th>model</th>
              <th style={{ textAlign: 'right' }}>prompt tok</th>
              <th style={{ textAlign: 'right' }}>completion tok</th>
              <th style={{ textAlign: 'right' }}>calls</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                <td className="mono" style={{ fontSize: 11.5 }}>{r.date}</td>
                <td className="mono" style={{ fontSize: 11.5 }}>{r.stage}</td>
                <td className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)' }}>{r.model}</td>
                <td className="mono" style={{ fontSize: 11.5, textAlign: 'right' }}>{r.prompt_tokens.toLocaleString()}</td>
                <td className="mono" style={{ fontSize: 11.5, textAlign: 'right' }}>{r.completion_tokens.toLocaleString()}</td>
                <td className="mono" style={{ fontSize: 11.5, textAlign: 'right' }}>{r.calls.toLocaleString()}</td>
              </tr>
            ))}
            <tr>
              <td colSpan={3} style={{ fontWeight: 650 }}>合计</td>
              <td className="mono" style={{ fontSize: 11.5, textAlign: 'right', fontWeight: 650 }}>{totals.prompt.toLocaleString()}</td>
              <td className="mono" style={{ fontSize: 11.5, textAlign: 'right', fontWeight: 650 }}>{totals.completion.toLocaleString()}</td>
              <td className="mono" style={{ fontSize: 11.5, textAlign: 'right', fontWeight: 650 }}>{totals.calls.toLocaleString()}</td>
            </tr>
          </tbody>
        </table>
      )}
    </div>
  );
}

// ---------------- 页面 ----------------

type Tab = 'personal' | 'ssh' | 'llm' | 'usage' | 'users';


// ---------------- 用户管理（admin） ----------------

const FEATURE_LABELS: [string, string][] = [
  ['forge', '想法生成'],
  ['review', '想法评审'],
  ['experiment', '实验搭建'],
  ['writer', '论文撰写'],
  ['paper_review', '论文评审'],
];

function UserEditModal({ u, onClose }: { u: AdminUserRead; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [role, setRole] = useState(u.role);
  const [active, setActive] = useState(u.is_active);
  const [quota, setQuota] = useState(u.token_quota != null ? String(u.token_quota) : '');
  const [llmAccess, setLlmAccess] = useState(u.llm_access || 'full');
  const [features, setFeatures] = useState<Record<string, boolean>>(() => {
    const f: Record<string, boolean> = {};
    for (const [k] of FEATURE_LABELS) f[k] = u.features?.[k] !== false;
    return f;
  });

  const saveMutation = useMutation({
    mutationFn: () =>
      api.adminUpdateUser(u.id, {
        role,
        is_active: active,
        token_quota: quota.trim() === '' ? -1 : Math.max(0, Number(quota)),
        features,
        llm_access: llmAccess,
      }),
    onSuccess: () => {
      toast('用户已更新', 'ok');
      void queryClient.invalidateQueries({ queryKey: ['admin-users'] });
      onClose();
    },
    onError: (e) => {
      const msg = e instanceof Error ? e.message : String(e);
      toast(msg === 'CANNOT_MODIFY_SELF_ROLE' ? '不能修改自己的角色或停用自己' : `保存失败：${msg}`, 'error');
    },
  });

  const quotaInvalid = quota.trim() !== '' && (!Number.isFinite(Number(quota)) || Number(quota) < 0);

  return (
    <Modal
      open
      onClose={onClose}
      width={520}
      title={`编辑用户：${u.display_name || u.email}`}
      sub={u.email}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>取消</button>
          <button className="btn btn-primary" disabled={saveMutation.isPending || quotaInvalid} onClick={() => saveMutation.mutate()}>
            保存
          </button>
        </>
      }
    >
      <div className="row gap10" style={{ marginBottom: 16 }}>
        <FormField label="角色" en="Role" style={{ flex: 1, marginBottom: 0 }}>
          <select className="input" value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="member">成员</option>
            <option value="admin">管理员</option>
          </select>
        </FormField>
        <FormField label="状态" en="Active" style={{ flex: 1, marginBottom: 0 }}>
          <select className="input" value={active ? '1' : '0'} onChange={(e) => setActive(e.target.value === '1')}>
            <option value="1">启用</option>
            <option value="0">停用</option>
          </select>
        </FormField>
      </div>
      <FormField label="大模型使用" en="LLM access" hint="限制该用户能否调用大模型">
        <select className="input" value={llmAccess} onChange={(e) => setLlmAccess(e.target.value)}>
          <option value="full">不限（全部功能）</option>
          <option value="chat_only">仅文献对话与 AI 伴读</option>
          <option value="blocked">锁定（禁止使用大模型）</option>
        </select>
      </FormField>
      <FormField label="AI token 配额" en="Token quota" hint={`已用 ${u.tokens_used.toLocaleString()} tokens；留空 = 不限。达到配额后不能再发起 AI 任务`}>
        <input className="input mono" value={quota} onChange={(e) => setQuota(e.target.value)} placeholder="不限" />
      </FormField>
      <FormField label="功能权限" en="Features" hint="取消勾选后该用户不能发起对应环节的 AI 任务">
        <div className="row gap8" style={{ flexWrap: 'wrap' }}>
          {FEATURE_LABELS.map(([k, label]) => (
            <button
              key={k}
              className="pill sm"
              style={{
                cursor: 'pointer',
                background: features[k] ? 'var(--accent-soft)' : 'var(--surface-3)',
                color: features[k] ? 'var(--accent-text)' : 'var(--text-3)',
                border: features[k] ? '1px solid var(--accent)' : '1px solid transparent',
              }}
              onClick={() => setFeatures((f) => ({ ...f, [k]: !f[k] }))}
            >
              {label}
            </button>
          ))}
        </div>
      </FormField>
    </Modal>
  );
}

function BatchAssignModal({ userIds, onClose, onDone }: { userIds: string[]; onClose: () => void; onDone: () => void }) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const { data: projects } = useQuery({ queryKey: ['admin-projects'], queryFn: () => api.adminListProjects(), retry: false });

  const assignMutation = useMutation({
    mutationFn: () => api.adminBatchAssign({ user_ids: userIds, project_ids: [...selected] }),
    onSuccess: (r) => {
      toast(`已分配：新增 ${r.added} 个成员关系`, 'ok');
      onDone();
      onClose();
    },
    onError: (e) => toast(`分配失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  return (
    <Modal
      open
      onClose={onClose}
      width={480}
      title="批量分配研究方向"
      sub={`已选 ${userIds.length} 个用户，将以成员身份加入所选方向`}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>取消</button>
          <button className="btn btn-primary" disabled={selected.size === 0 || assignMutation.isPending} onClick={() => assignMutation.mutate()}>
            分配
          </button>
        </>
      }
    >
      {!projects ? (
        <div className="empty">加载方向列表…</div>
      ) : projects.length === 0 ? (
        <div className="empty">还没有研究方向</div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {projects.map((p) => {
            const on = selected.has(p.id);
            return (
              <label key={p.id} className="list-row row gap10" style={{ cursor: 'pointer' }}>
                <input
                  type="checkbox"
                  checked={on}
                  onChange={() =>
                    setSelected((s) => {
                      const next = new Set(s);
                      if (on) next.delete(p.id);
                      else next.add(p.id);
                      return next;
                    })
                  }
                />
                <span style={{ fontSize: 13, fontWeight: 600 }}>{p.name}</span>
              </label>
            );
          })}
        </div>
      )}
    </Modal>
  );
}

function UsersTab() {
  const queryClient = useQueryClient();
  const { data: users, isLoading, isError } = useQuery({ queryKey: ['admin-users'], queryFn: () => api.adminListUsers(), retry: false });
  const [editing, setEditing] = useState<AdminUserRead | null>(null);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [assignOpen, setAssignOpen] = useState(false);

  if (isLoading) return <div className="empty">加载中…</div>;
  if (isError || !users) return <div className="empty">无法加载用户列表</div>;

  const toggle = (id: string) =>
    setChecked((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const allChecked = users.length > 0 && checked.size === users.length;

  const featureSummary = (u: AdminUserRead): string => {
    const disabled = FEATURE_LABELS.filter(([k]) => u.features?.[k] === false).map(([, l]) => l);
    return disabled.length === 0 ? '全部' : `禁用：${disabled.join('、')}`;
  };

  return (
    <div>
      <div className="row gap10" style={{ marginBottom: 14 }}>
        <span style={{ fontSize: 12.5, color: 'var(--text-2)' }}>{users.length} 个用户</span>
        <button className="btn btn-soft" style={{ marginLeft: 'auto' }} disabled={checked.size === 0} onClick={() => setAssignOpen(true)}>
          批量分配方向{checked.size > 0 ? `（${checked.size}）` : ''}
        </button>
      </div>
      <div className="card" style={{ overflow: 'hidden' }}>
        <table className="table">
          <thead>
            <tr>
              <th style={{ width: 34 }}>
                <input
                  type="checkbox"
                  checked={allChecked}
                  onChange={() => setChecked(allChecked ? new Set() : new Set(users.map((u) => u.id)))}
                />
              </th>
              <th>用户</th>
              <th>角色</th>
              <th>状态</th>
              <th>AI 用量 / 配额</th>
              <th>大模型</th>
              <th>功能权限</th>
              <th style={{ width: 70 }}></th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id}>
                <td>
                  <input type="checkbox" checked={checked.has(u.id)} onChange={() => toggle(u.id)} />
                </td>
                <td>
                  <div className="row gap10">
                    <Avatar userId={u.id} hasAvatar={u.has_avatar} name={u.display_name || u.email} size={26} />
                    <div>
                      <div style={{ fontSize: 12.5, fontWeight: 600 }}>{u.display_name || '—'}</div>
                      <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>{u.email}</div>
                    </div>
                  </div>
                </td>
                <td>
                  <span className="pill sm" style={u.role === 'admin' ? { background: 'var(--accent-soft)', color: 'var(--accent-text)' } : undefined}>
                    {u.role === 'admin' ? '管理员' : '成员'}
                  </span>
                </td>
                <td>
                  <span className="pill sm" style={{ background: u.is_active ? 'var(--ok-bg)' : 'var(--surface-3)', color: u.is_active ? 'var(--ok-tx)' : 'var(--text-3)' }}>
                    {u.is_active ? '启用' : '停用'}
                  </span>
                </td>
                <td className="mono" style={{ fontSize: 11.5 }}>
                  {u.tokens_used.toLocaleString()}
                  <span style={{ color: 'var(--text-3)' }}> / {u.token_quota != null ? u.token_quota.toLocaleString() : '不限'}</span>
                </td>
                <td style={{ fontSize: 11.5, color: 'var(--text-2)' }}>
                  {u.llm_access === 'blocked' ? '锁定' : u.llm_access === 'chat_only' ? '仅对话' : '不限'}
                </td>
                <td style={{ fontSize: 11.5, color: 'var(--text-2)' }}>{featureSummary(u)}</td>
                <td>
                  <button className="btn btn-ghost sm" onClick={() => setEditing(u)}>编辑</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {editing && <UserEditModal u={editing} onClose={() => setEditing(null)} />}
      {assignOpen && (
        <BatchAssignModal
          userIds={[...checked]}
          onClose={() => setAssignOpen(false)}
          onDone={() => {
            setChecked(new Set());
            void queryClient.invalidateQueries({ queryKey: ['admin-users'] });
          }}
        />
      )}
    </div>
  );
}

export function SettingsPage() {
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: () => api.me(), retry: false });
  const admin = isAdmin(me);
  const [tab, setTab] = useState<Tab>('personal');

  const tabs: { v: Tab; label: string }[] = [
    { v: 'personal', label: '个人 Profile' },
    { v: 'ssh', label: 'SSH 凭据' },
    ...(admin
      ? [
          { v: 'llm' as Tab, label: 'LLM 管理' },
          { v: 'usage' as Tab, label: '用量 Usage' },
          { v: 'users' as Tab, label: '用户管理' },
        ]
      : []),
  ];
  const effectiveTab: Tab = !admin && (tab === 'llm' || tab === 'usage' || tab === 'users') ? 'personal' : tab;

  return (
    <div className="page fadeup">
      <PageHead
        eyebrow="Polaris · Settings"
        title="设置 Settings"
        sub="个人资料、SSH 凭据、LLM 服务与模型路由、用量统计、用户管理。"
      />
      <div style={{ marginBottom: 20 }}>
        <Segmented options={tabs} value={effectiveTab} onChange={setTab} />
      </div>
      {effectiveTab === 'personal' && <PersonalTab />}
      {effectiveTab === 'ssh' && <SshTab />}
      {effectiveTab === 'llm' && admin && <LlmTab />}
      {effectiveTab === 'usage' && admin && <UsageTab />}
      {effectiveTab === 'users' && admin && <UsersTab />}
    </div>
  );
}
