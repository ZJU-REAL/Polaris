import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
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
  const { data: me, isLoading, isError } = useQuery({ queryKey: ['me'], queryFn: () => api.me(), retry: false });
  if (isLoading) return <div className="empty">加载中…</div>;
  if (isError || !me) return <div className="empty">无法加载用户信息（后端不可用）</div>;
  const rows: [string, string, string][] = [
    ['邮箱', 'email', me.email],
    ['显示名', 'display_name', me.display_name ?? '—'],
    ['角色', 'role', me.role ?? (me.is_superuser ? 'admin' : 'member')],
  ];
  return (
    <div className="card" style={{ overflow: 'hidden', maxWidth: 560 }}>
      {rows.map(([zh, en, v], i) => (
        <div key={en} className="row gap12" style={{ padding: '13px 20px', borderTop: i > 0 ? '0.5px solid var(--border)' : 'none' }}>
          <div style={{ width: 130 }}>
            <div style={{ fontSize: 13, fontWeight: 600 }}>{zh}</div>
            <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>{en}</div>
          </div>
          <div style={{ fontSize: 13, color: 'var(--text-2)' }}>{v}</div>
        </div>
      ))}
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
                        if (window.confirm(`确定删除凭据「${c.name}」？使用中的实验将无法再连接该服务器。`)) {
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
            <th style={{ width: 110 }}>stage</th>
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
                <td className="mono" style={{ fontSize: 11.5, fontWeight: 600 }}>{stage}</td>
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

type Tab = 'personal' | 'ssh' | 'llm' | 'usage';

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
        ]
      : []),
  ];
  const effectiveTab: Tab = !admin && (tab === 'llm' || tab === 'usage') ? 'personal' : tab;

  return (
    <div className="page fadeup">
      <PageHead
        eyebrow="Polaris · Settings"
        title="设置 Settings"
        sub="个人信息、SSH 凭据、LLM provider 与模型路由、用量统计。"
      />
      <div style={{ marginBottom: 20 }}>
        <Segmented options={tabs} value={effectiveTab} onChange={setTab} />
      </div>
      {effectiveTab === 'personal' && <PersonalTab />}
      {effectiveTab === 'ssh' && <SshTab />}
      {effectiveTab === 'llm' && admin && <LlmTab />}
      {effectiveTab === 'usage' && admin && <UsageTab />}
    </div>
  );
}
