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
import { setLang, tr, useLang } from '../../lib/i18n';
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
      toast(tr('个人资料已保存', 'Profile saved'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['me'] });
    },
    onError: (e) => toast(`${tr('保存失败', 'Save failed')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  const avatarMutation = useMutation({
    mutationFn: (file: File) => api.uploadAvatar(file),
    onSuccess: () => {
      toast(tr('头像已更新', 'Avatar updated'), 'ok');
      setAvatarVersion((v) => v + 1);
      void queryClient.invalidateQueries({ queryKey: ['me'] });
      void queryClient.invalidateQueries({ queryKey: ['avatar'] });
    },
    onError: (e) => {
      const msg = e instanceof Error ? e.message : String(e);
      toast(msg === 'AVATAR_TOO_LARGE' ? tr('图片超过 2MB', 'Image exceeds 2MB') : msg === 'AVATAR_NOT_IMAGE' ? tr('不是有效的图片文件', 'Not a valid image file') : `${tr('上传失败', 'Upload failed')}：${msg}`, 'error');
    },
  });

  if (isLoading) return <div className="empty">{tr('加载中…', 'Loading…')}</div>;
  if (isError || !me) return <div className="empty">{tr('无法加载用户信息（后端不可用）', 'Failed to load user info (backend unavailable)')}</div>;

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
            {avatarMutation.isPending ? tr('上传中…', 'Uploading…') : tr('更换头像', 'Change avatar')}
          </button>
          <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 6 }}>{tr('PNG / JPEG / WebP，2MB 以内', 'PNG / JPEG / WebP, up to 2MB')}</div>
        </div>
      </div>
      <FormField label={tr('显示名', 'Display name')}>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder={tr('如：王小明', 'e.g. Alice Wang')} />
      </FormField>
      <FormField label={tr('邮箱', 'Email')}>
        <input className="input" value={me.email} disabled />
      </FormField>
      <div className="row gap12" style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 12.5, color: 'var(--text-2)' }}>
          {tr('角色', 'Role')}：{me.role === 'admin' ? tr('管理员', 'Admin') : tr('成员', 'Member')}
        </div>
        {usage && (
          <div style={{ fontSize: 12.5, color: 'var(--text-2)' }}>
            {tr('AI 用量', 'AI usage')}：{usage.tokens_used.toLocaleString()} tokens
            {usage.token_quota != null && ` / ${tr('配额', 'quota')} ${usage.token_quota.toLocaleString()}`}
          </div>
        )}
      </div>
      <div className="row" style={{ justifyContent: 'flex-end' }}>
        <button
          className="btn btn-primary"
          disabled={saveMutation.isPending || (me.display_name ?? '') === name.trim()}
          onClick={() => saveMutation.mutate()}
        >
          {tr('保存', 'Save')}
        </button>
      </div>
    </div>
  );
}

// ---------------- 界面语言 ----------------

function LanguageCard() {
  const lang = useLang();
  return (
    <div className="card card-pad" style={{ maxWidth: 560, marginTop: 16 }}>
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <div>
          <div style={{ fontSize: 13, fontWeight: 650 }}>{tr('界面语言', 'Language')}</div>
          <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 4 }}>
            {tr('只影响界面文字，不影响 AI 生成内容的语言', 'Only affects the UI, not AI-generated content')}
          </div>
        </div>
        <Segmented
          options={[
            { v: 'zh' as const, label: '中文' },
            { v: 'en' as const, label: 'English' },
          ]}
          value={lang}
          onChange={setLang}
        />
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
      toast(tr('SSH 凭据已添加（私钥加密存储）', 'SSH credential added (private key stored encrypted)'), 'ok');
      setModalOpen(false);
      invalidate();
    },
    onError: (e) => toast(`${tr('添加失败', 'Add failed')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteSshCredential(id),
    onSuccess: () => {
      toast(tr('凭据已删除', 'Credential deleted'), 'ok');
      invalidate();
    },
    onError: (e) => toast(`${tr('删除失败', 'Delete failed')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  const testMutation = useMutation({
    mutationFn: (id: string) => api.testSshCredential(id),
    onSuccess: (r) => {
      toast(r.ok ? `${tr('连接成功', 'Connected')}：${r.detail}` : `${tr('连接失败', 'Connection failed')}：${r.detail}`, r.ok ? 'ok' : 'error');
      if (r.ok) invalidate(); // 后端更新 last_verified_at
    },
    onError: (e) => toast(`${tr('测试失败', 'Test failed')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
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
          {tr('SSH 凭据', 'SSH credentials')} <span className="en-label" style={{ fontSize: 11 }}>{tr('实验用远程服务器', 'remote servers for experiments')}</span>
        </span>
        <button className="btn btn-primary sm" onClick={() => { setDraft(emptySshDraft()); setModalOpen(true); }}>
          <Icon name="plus" size={13} />
          {tr('添加凭据', 'Add credential')}
        </button>
      </div>
      <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginBottom: 14, lineHeight: 1.5 }}>
        {tr(
          '私钥加密存储（Fernet），仅用于你自己的实验任务，绝不回传前端；实验工作目录限定 ~/polaris_runs/。',
          'Private keys are stored encrypted (Fernet), used only for your own experiment jobs and never sent back to the browser; the experiment working dir is limited to ~/polaris_runs/.',
        )}
      </div>

      {isLoading ? (
        <div className="empty" style={{ padding: 24 }}>{tr('加载中…', 'Loading…')}</div>
      ) : isError ? (
        <div className="empty" style={{ padding: 24 }}>
          {tr('无法加载（后端不可用或接口尚未就绪）', 'Failed to load (backend unavailable or API not ready)')}
          <div style={{ marginTop: 10 }}>
            <button className="btn btn-soft sm" onClick={() => void refetch()}>{tr('重试', 'Retry')}</button>
          </div>
        </div>
      ) : creds.length === 0 ? (
        <div className="empty" style={{ padding: 24 }}>
          {tr('还没有 SSH 凭据 — 添加一台 GPU 服务器后即可在 Experiment Lab 发起实验', 'No SSH credentials yet — add a GPU server to run experiments in Experiment Lab')}
        </div>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>{tr('名称', 'Name')}</th>
              <th>host</th>
              <th style={{ width: 60 }}>port</th>
              <th>username</th>
              <th style={{ width: 130 }}>{tr('最近验证', 'Last verified')}</th>
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
                  {c.last_verified_at ? fmtTime(c.last_verified_at) : tr('从未验证', 'Never verified')}
                </td>
                <td>
                  <div className="row gap6" style={{ justifyContent: 'flex-end' }}>
                    <button
                      className="btn btn-soft sm"
                      disabled={testMutation.isPending}
                      onClick={() => testMutation.mutate(c.id)}
                    >
                      {testMutation.isPending && testMutation.variables === c.id ? tr('连接中…', 'Connecting…') : tr('测试连接', 'Test connection')}
                    </button>
                    <button
                      className="icon-btn"
                      style={{ width: 26, height: 26 }}
                      title={tr('删除', 'Delete')}
                      disabled={deleteMutation.isPending}
                      onClick={() => {
                        if (window.confirm(`${tr('确定删除凭据', 'Delete credential')} “${c.name}”？${tr('使用中的实验将无法再连接该服务器。', 'Experiments using it will no longer be able to reach this server.')}`)) {
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
        title={tr('添加 SSH 凭据', 'Add SSH credential')}
        sub={tr('私钥加密存储，仅用于你自己的实验任务', 'Private key stored encrypted; used only for your own experiments')}
        footer={
          <>
            <button className="btn btn-ghost" onClick={() => setModalOpen(false)}>{tr('取消', 'Cancel')}</button>
            <button className="btn btn-primary" disabled={!canSave} onClick={() => createMutation.mutate()}>
              {createMutation.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
            </button>
          </>
        }
      >
        <FormField label={tr('名称', 'Name')}>
          <input className="input" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            placeholder={tr('如 lab-gpu-1', 'e.g. lab-gpu-1')} />
        </FormField>
        <div className="row gap12" style={{ alignItems: 'flex-start' }}>
          <FormField label={tr('主机', 'Host')} style={{ flex: 1 }}>
            <input className="input mono" value={draft.host} onChange={(e) => setDraft({ ...draft, host: e.target.value })}
              placeholder="gpu.example.edu" />
          </FormField>
          <FormField label={tr('端口', 'Port')} style={{ width: 100 }}>
            <input className="input mono" inputMode="numeric" value={draft.port}
              onChange={(e) => setDraft({ ...draft, port: e.target.value })} placeholder="22" />
          </FormField>
        </div>
        <FormField label={tr('用户名', 'Username')}>
          <input className="input mono" value={draft.username} onChange={(e) => setDraft({ ...draft, username: e.target.value })}
            placeholder="ubuntu" autoComplete="off" />
        </FormField>
        <FormField label={tr('私钥（PEM）', 'Private key (PEM)')} hint={tr('粘贴完整 PEM 文本；后端 Fernet 加密入库，只写不读。', 'Paste the full PEM text; stored encrypted on the backend, write-only.')}>
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
        <FormField label={tr('密钥口令（可选）', 'Passphrase (optional)')}>
          <input className="input mono" type="password" autoComplete="new-password" value={draft.passphrase}
            onChange={(e) => setDraft({ ...draft, passphrase: e.target.value })} placeholder={tr('私钥无口令则留空', 'Leave empty if the key has no passphrase')} />
        </FormField>
        <FormField label={tr('出外网代理（可选）', 'Outbound proxy (optional)')}>
          <input className="input mono" value={draft.proxy_url}
            onChange={(e) => setDraft({ ...draft, proxy_url: e.target.value })}
            placeholder={tr('如 http://10.205.70.120:7899，服务器直连外网则留空', 'e.g. http://10.205.70.120:7899; leave empty if the server has direct internet access')} />
          <div className="muted" style={{ fontSize: 11.5, marginTop: 4 }}>
            {tr('实验装依赖、下载模型数据时自动走该代理；内网 LLM 接口不受影响', 'Used when experiments install dependencies or download models; internal LLM endpoints are unaffected')}
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
      <FormField label={tr('名称', 'Name')}>
        <input className="input" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })}
          placeholder={tr('如 deepseek / claude / local-fake', 'e.g. deepseek / claude / local-fake')} />
      </FormField>
      <div className="row gap12" style={{ alignItems: 'flex-start' }}>
        <FormField label={tr('类型', 'Kind')} style={{ width: 180 }}>
          <select className="input" value={draft.kind}
            onChange={(e) => setDraft({ ...draft, kind: e.target.value as LlmProviderKind })}>
            {KINDS.map((k) => (
              <option key={k} value={k}>{k}</option>
            ))}
          </select>
        </FormField>
        <FormField label="Base URL" style={{ flex: 1 }}>
          <input className="input mono" value={draft.base_url} onChange={(e) => setDraft({ ...draft, base_url: e.target.value })}
            placeholder="https://api.example.com/v1" disabled={draft.kind === 'fake'} />
        </FormField>
      </div>
      <FormField label="API Key"
        hint={isNew ? tr('fake provider 无需 key', 'fake providers need no key') : tr('留空 = 保持不变；后端只写不读，展示为 masked', 'Leave empty to keep unchanged; write-only on the backend, shown masked')}>
        <input className="input mono" type="password" autoComplete="new-password" value={draft.api_key}
          onChange={(e) => setDraft({ ...draft, api_key: e.target.value })}
          placeholder={isNew ? 'sk-…' : tr('••••••（留空不变）', '•••••• (empty = unchanged)')} disabled={draft.kind === 'fake'} />
      </FormField>
      <label className="row gap8" style={{ fontSize: 13, cursor: 'pointer', userSelect: 'none' }}>
        <input type="checkbox" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />
        {tr('启用', 'Enabled')}
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
      toast(tr('Provider 已创建', 'Provider created'), 'ok');
      setModal('closed');
      invalidate();
    },
    onError: (err) => toast(`${tr('创建失败', 'Create failed')}：${err instanceof Error ? err.message : String(err)}`, 'error'),
  });
  const patchMutation = useMutation({
    mutationFn: (id: string) => api.patchLlmProvider(id, toInput(draft)),
    onSuccess: () => {
      toast(tr('Provider 已更新', 'Provider updated'), 'ok');
      setModal('closed');
      invalidate();
    },
    onError: (err) => toast(`${tr('更新失败', 'Update failed')}：${err instanceof Error ? err.message : String(err)}`, 'error'),
  });
  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteLlmProvider(id),
    onSuccess: () => {
      toast(tr('Provider 已删除', 'Provider deleted'), 'ok');
      invalidate();
    },
    onError: (err) => toast(`${tr('删除失败', 'Delete failed')}：${err instanceof Error ? err.message : String(err)}`, 'error'),
  });

  const isNew = modal === 'create';
  const busy = createMutation.isPending || patchMutation.isPending;

  return (
    <div className="card card-pad" style={{ marginBottom: 20 }}>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 14 }}>
        <span className="section-h">
          <Icon name="server" size={15} style={{ color: 'var(--accent)' }} />
          {tr('LLM 供应商', 'Providers')}
        </span>
        <button className="btn btn-primary sm" onClick={() => { setDraft(emptyDraft()); setModal('create'); }}>
          <Icon name="plus" size={13} />
          {tr('新增 Provider', 'Add provider')}
        </button>
      </div>

      {isLoading ? (
        <div className="empty" style={{ padding: 24 }}>{tr('加载中…', 'Loading…')}</div>
      ) : isError ? (
        <div className="empty" style={{ padding: 24 }}>
          {tr('无法加载（后端不可用或无权限）', 'Failed to load (backend unavailable or no permission)')}
          <div style={{ marginTop: 10 }}>
            <button className="btn btn-soft sm" onClick={() => void refetch()}>{tr('重试', 'Retry')}</button>
          </div>
        </div>
      ) : providers.length === 0 ? (
        <div className="empty" style={{ padding: 24 }}>{tr('还没有 provider，先添加一个（kind=fake 可无 key 演示）', 'No providers yet — add one (kind=fake works without a key for demos)')}</div>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>{tr('名称', 'Name')}</th>
              <th>kind</th>
              <th>base_url</th>
              <th>api_key</th>
              <th style={{ width: 60 }}>{tr('状态', 'Status')}</th>
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
                    {p.enabled ? tr('启用', 'Enabled') : tr('停用', 'Disabled')}
                  </span>
                </td>
                <td>
                  <div className="row gap6" style={{ justifyContent: 'flex-end' }}>
                    <button className="icon-btn" style={{ width: 26, height: 26 }} title={tr('编辑', 'Edit')}
                      onClick={() => { setDraft(draftFrom(p)); setModal(p.id); }}>
                      <Icon name="pen" size={13} />
                    </button>
                    <button className="icon-btn" style={{ width: 26, height: 26 }} title={tr('删除', 'Delete')}
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
        title={isNew ? tr('新增 Provider', 'Add provider') : tr('编辑 Provider', 'Edit provider')}
        sub={tr('api_key 后端只写不读；kind=fake 用于测试与无 key 演示', 'api_key is write-only on the backend; kind=fake is for tests and keyless demos')}
        footer={
          <>
            <button className="btn btn-ghost" onClick={() => setModal('closed')}>{tr('取消', 'Cancel')}</button>
            <button className="btn btn-primary" disabled={!draft.name.trim() || busy}
              onClick={() => (isNew ? createMutation.mutate() : patchMutation.mutate(modal))}>
              {busy ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
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

/** stage 标签（渲染处再 tr；代码标识符照常显示在下方）。 */
const STAGE_LABELS: Record<string, { zh: string; en: string }> = {
  default: { zh: '默认', en: 'Default' },
  navigator: { zh: '任务规划', en: 'Task planning' },
  sextant: { zh: '自动校验', en: 'Auto verification' },
  interview: { zh: '方向访谈', en: 'Direction interview' },
  relevance: { zh: '相关度打分', en: 'Relevance scoring' },
  librarian: { zh: '文献抓取', en: 'Paper fetching' },
  reading: { zh: '精读编译', en: 'Deep reading' },
  embedding: { zh: '向量嵌入', en: 'Embeddings' },
  forge: { zh: '想法生成', en: 'Idea generation' },
  forge_signal: { zh: '信号摘要', en: 'Signal digest' },
  goal_explore: { zh: '目标构建', en: 'Goal building' },
  proposal: { zh: '方案起草', en: 'Proposal drafting' },
  proposal_review: { zh: '方案评审', en: 'Proposal review' },
  debate: { zh: '辩论评审', en: 'Debate review' },
  experiment: { zh: '实验', en: 'Experiments' },
  writing: { zh: '论文撰写', en: 'Paper writing' },
  review: { zh: '论文评审', en: 'Paper review' },
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
      toast(tr('模型路由表已保存', 'Model routing saved'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['llm', 'routes'] });
    },
    onError: (err) => toast(`${tr('保存失败', 'Save failed')}：${err instanceof Error ? err.message : String(err)}`, 'error'),
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
          {tr('模型路由表', 'Model routing')} <span className="en-label" style={{ fontSize: 11 }}>{tr('整表保存', 'saved as one table')}</span>
        </span>
        <button className="btn btn-primary sm" disabled={saveMutation.isPending} onClick={() => saveMutation.mutate()}>
          <Icon name="check" size={13} />
          {saveMutation.isPending ? tr('保存中…', 'Saving…') : tr('保存路由表', 'Save routing')}
        </button>
      </div>
      {routesQuery.isError && (
        <div className="field-hint" style={{ marginBottom: 10, color: 'var(--warn-tx)' }}>
          {tr('路由表加载失败（后端不可用），保存将覆盖整表。', 'Failed to load routes (backend unavailable); saving will overwrite the whole table.')}
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
            const label = STAGE_LABELS[stage];
            return (
              <tr key={stage}>
                <td>
                  <div style={{ fontSize: 12, fontWeight: 650 }}>{label ? tr(label.zh, label.en) : stage}</div>
                  <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>{stage}</div>
                </td>
                <td>
                  <select className="input" style={{ height: 32, width: '100%' }} value={r.provider_id}
                    onChange={(e) => setRow(stage, { provider_id: e.target.value })}>
                    <option value="">{tr('（未配置）', '(not set)')}</option>
                    {providers.map((p) => (
                      <option key={p.id} value={p.id}>{p.name}</option>
                    ))}
                  </select>
                </td>
                <td>
                  <input className="input mono" style={{ height: 32, width: '100%', fontSize: 12 }} value={r.model}
                    placeholder={tr('如 deepseek-chat', 'e.g. deepseek-chat')} onChange={(e) => setRow(stage, { model: e.target.value })} />
                </td>
                <td>
                  <input className="input mono" style={{ height: 32, width: '100%', fontSize: 12 }} value={r.temperature}
                    placeholder={tr('默认', 'default')} inputMode="decimal" onChange={(e) => setRow(stage, { temperature: e.target.value })} />
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
          {tr('LLM 用量', 'LLM usage')} <span className="en-label" style={{ fontSize: 11 }}>{tr('按天 × stage', 'per day × stage')}</span>
        </span>
        <Segmented options={[{ v: '7' as const, label: tr('7 天', '7 days') }, { v: '30' as const, label: tr('30 天', '30 days') }, { v: '90' as const, label: tr('90 天', '90 days') }]}
          value={days} onChange={setDays} />
      </div>
      {isLoading ? (
        <div className="empty" style={{ padding: 24 }}>{tr('加载中…', 'Loading…')}</div>
      ) : isError ? (
        <div className="empty" style={{ padding: 24 }}>
          {tr('无法加载用量数据（后端不可用或无权限）', 'Failed to load usage data (backend unavailable or no permission)')}
          <div style={{ marginTop: 10 }}>
            <button className="btn btn-soft sm" onClick={() => void refetch()}>{tr('重试', 'Retry')}</button>
          </div>
        </div>
      ) : rows.length === 0 ? (
        <div className="empty" style={{ padding: 24 }}>{tr(`近 ${days} 天暂无用量记录`, `No usage records in the last ${days} days`)}</div>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>{tr('日期', 'Date')}</th>
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
              <td colSpan={3} style={{ fontWeight: 650 }}>{tr('合计', 'Total')}</td>
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

const FEATURE_LABELS: [string, string, string][] = [
  ['forge', '想法生成', 'Idea generation'],
  ['review', '想法评审', 'Idea review'],
  ['experiment', '实验搭建', 'Experiment lab'],
  ['writer', '论文撰写', 'Paper writing'],
  ['paper_review', '论文评审', 'Paper review'],
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
      toast(tr('用户已更新', 'User updated'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['admin-users'] });
      onClose();
    },
    onError: (e) => {
      const msg = e instanceof Error ? e.message : String(e);
      toast(msg === 'CANNOT_MODIFY_SELF_ROLE' ? tr('不能修改自己的角色或停用自己', 'You cannot change your own role or deactivate yourself') : `${tr('保存失败', 'Save failed')}：${msg}`, 'error');
    },
  });

  const quotaInvalid = quota.trim() !== '' && (!Number.isFinite(Number(quota)) || Number(quota) < 0);

  return (
    <Modal
      open
      onClose={onClose}
      width={520}
      title={`${tr('编辑用户', 'Edit user')}：${u.display_name || u.email}`}
      sub={u.email}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>{tr('取消', 'Cancel')}</button>
          <button className="btn btn-primary" disabled={saveMutation.isPending || quotaInvalid} onClick={() => saveMutation.mutate()}>
            {tr('保存', 'Save')}
          </button>
        </>
      }
    >
      <div className="row gap10" style={{ marginBottom: 16 }}>
        <FormField label={tr('角色', 'Role')} style={{ flex: 1, marginBottom: 0 }}>
          <select className="input" value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="member">{tr('成员', 'Member')}</option>
            <option value="admin">{tr('管理员', 'Admin')}</option>
          </select>
        </FormField>
        <FormField label={tr('状态', 'Status')} style={{ flex: 1, marginBottom: 0 }}>
          <select className="input" value={active ? '1' : '0'} onChange={(e) => setActive(e.target.value === '1')}>
            <option value="1">{tr('启用', 'Active')}</option>
            <option value="0">{tr('停用', 'Disabled')}</option>
          </select>
        </FormField>
      </div>
      <FormField label={tr('大模型使用', 'LLM access')} hint={tr('限制该用户能否调用大模型', 'Controls whether this user can call LLMs')}>
        <select className="input" value={llmAccess} onChange={(e) => setLlmAccess(e.target.value)}>
          <option value="full">{tr('不限（全部功能）', 'Unrestricted (all features)')}</option>
          <option value="chat_only">{tr('仅文献对话与 AI 伴读', 'Paper chat and reading assistant only')}</option>
          <option value="blocked">{tr('锁定（禁止使用大模型）', 'Blocked (no LLM use)')}</option>
        </select>
      </FormField>
      <FormField label={tr('AI token 配额', 'Token quota')} hint={`${tr('已用', 'Used')} ${u.tokens_used.toLocaleString()} tokens${tr('；留空 = 不限。达到配额后不能再发起 AI 任务', '; empty = unlimited. Once the quota is reached, no new AI tasks can start')}`}>
        <input className="input mono" value={quota} onChange={(e) => setQuota(e.target.value)} placeholder={tr('不限', 'Unlimited')} />
      </FormField>
      <FormField label={tr('功能权限', 'Feature toggles')} hint={tr('取消勾选后该用户不能发起对应环节的 AI 任务', 'Unchecked features block this user from starting those AI tasks')}>
        <div className="row gap8" style={{ flexWrap: 'wrap' }}>
          {FEATURE_LABELS.map(([k, zh, en]) => (
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
              {tr(zh, en)}
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
      toast(tr(`已分配：新增 ${r.added} 个成员关系`, `Assigned — ${r.added} memberships added`), 'ok');
      onDone();
      onClose();
    },
    onError: (e) => toast(`${tr('分配失败', 'Assign failed')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  return (
    <Modal
      open
      onClose={onClose}
      width={480}
      title={tr('批量分配研究方向', 'Assign directions in bulk')}
      sub={tr(`已选 ${userIds.length} 个用户，将以成员身份加入所选方向`, `${userIds.length} users selected; they will join the chosen directions as members`)}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>{tr('取消', 'Cancel')}</button>
          <button className="btn btn-primary" disabled={selected.size === 0 || assignMutation.isPending} onClick={() => assignMutation.mutate()}>
            {tr('分配', 'Assign')}
          </button>
        </>
      }
    >
      {!projects ? (
        <div className="empty">{tr('加载方向列表…', 'Loading directions…')}</div>
      ) : projects.length === 0 ? (
        <div className="empty">{tr('还没有研究方向', 'No research directions yet')}</div>
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

  if (isLoading) return <div className="empty">{tr('加载中…', 'Loading…')}</div>;
  if (isError || !users) return <div className="empty">{tr('无法加载用户列表', 'Failed to load users')}</div>;

  const toggle = (id: string) =>
    setChecked((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const allChecked = users.length > 0 && checked.size === users.length;

  const featureSummary = (u: AdminUserRead): string => {
    const disabled = FEATURE_LABELS.filter(([k]) => u.features?.[k] === false).map(([, zh, en]) => tr(zh, en));
    return disabled.length === 0 ? tr('全部', 'All') : `${tr('禁用', 'Disabled')}：${disabled.join(tr('、', ', '))}`;
  };

  return (
    <div>
      <div className="row gap10" style={{ marginBottom: 14 }}>
        <span style={{ fontSize: 12.5, color: 'var(--text-2)' }}>{tr(`${users.length} 个用户`, `${users.length} users`)}</span>
        <button className="btn btn-soft" style={{ marginLeft: 'auto' }} disabled={checked.size === 0} onClick={() => setAssignOpen(true)}>
          {tr('批量分配方向', 'Assign directions')}{checked.size > 0 ? `（${checked.size}）` : ''}
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
              <th>{tr('用户', 'User')}</th>
              <th>{tr('角色', 'Role')}</th>
              <th>{tr('状态', 'Status')}</th>
              <th>{tr('AI 用量 / 配额', 'AI usage / quota')}</th>
              <th>{tr('大模型', 'LLM')}</th>
              <th>{tr('功能权限', 'Features')}</th>
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
                      <div style={{ fontSize: 12.5, fontWeight: 600 }}>
                        {u.display_name || '—'}
                        {u.username && <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', fontWeight: 400, marginLeft: 6 }}>@{u.username}</span>}
                      </div>
                      <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>{u.email}</div>
                    </div>
                  </div>
                </td>
                <td>
                  <span className="pill sm" style={u.role === 'admin' ? { background: 'var(--accent-soft)', color: 'var(--accent-text)' } : undefined}>
                    {u.role === 'admin' ? tr('管理员', 'Admin') : tr('成员', 'Member')}
                  </span>
                </td>
                <td>
                  <span className="pill sm" style={{ background: u.is_active ? 'var(--ok-bg)' : 'var(--surface-3)', color: u.is_active ? 'var(--ok-tx)' : 'var(--text-3)' }}>
                    {u.is_active ? tr('启用', 'Active') : tr('停用', 'Disabled')}
                  </span>
                </td>
                <td className="mono" style={{ fontSize: 11.5 }}>
                  {u.tokens_used.toLocaleString()}
                  <span style={{ color: 'var(--text-3)' }}> / {u.token_quota != null ? u.token_quota.toLocaleString() : tr('不限', 'unlimited')}</span>
                </td>
                <td style={{ fontSize: 11.5, color: 'var(--text-2)' }}>
                  {u.llm_access === 'blocked' ? tr('锁定', 'Blocked') : u.llm_access === 'chat_only' ? tr('仅对话', 'Chat only') : tr('不限', 'Unrestricted')}
                </td>
                <td style={{ fontSize: 11.5, color: 'var(--text-2)' }}>{featureSummary(u)}</td>
                <td>
                  <button className="btn btn-ghost sm" onClick={() => setEditing(u)}>{tr('编辑', 'Edit')}</button>
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
    { v: 'personal', label: tr('个人', 'Profile') },
    { v: 'ssh', label: tr('SSH 凭据', 'SSH credentials') },
    ...(admin
      ? [
          { v: 'llm' as Tab, label: tr('LLM 管理', 'LLM admin') },
          { v: 'usage' as Tab, label: tr('用量', 'Usage') },
          { v: 'users' as Tab, label: tr('用户管理', 'Users') },
        ]
      : []),
  ];
  const effectiveTab: Tab = !admin && (tab === 'llm' || tab === 'usage' || tab === 'users') ? 'personal' : tab;

  return (
    <div className="page fadeup">
      <PageHead
        eyebrow="Polaris · Settings"
        title={tr('设置', 'Settings')}
        sub={tr('个人资料、界面语言、SSH 凭据、LLM 服务与模型路由、用量统计、用户管理。', 'Profile, language, SSH credentials, LLM providers and model routing, usage stats, user management.')}
      />
      <div style={{ marginBottom: 20 }}>
        <Segmented options={tabs} value={effectiveTab} onChange={setTab} />
      </div>
      {effectiveTab === 'personal' && (
        <>
          <PersonalTab />
          <LanguageCard />
        </>
      )}
      {effectiveTab === 'ssh' && <SshTab />}
      {effectiveTab === 'llm' && admin && <LlmTab />}
      {effectiveTab === 'usage' && admin && <UsageTab />}
      {effectiveTab === 'users' && admin && <UsersTab />}
    </div>
  );
}
