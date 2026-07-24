import { Fragment, useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Avatar } from '../../components/ui/Avatar';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { Segmented } from '../../components/ui/Segmented';
import { Switch } from '../../components/ui/Switch';
import { Modal } from '../../components/ui/Modal';
import { FormField } from '../../components/ui/FormField';
import { toast } from '../../components/ui/Toast';
import { DropdownList, SelectMenu, useClickOutside } from '../../components/ui/SelectMenu';
import { fmtTime } from '../../lib/format';
import { SysinfoPanel } from '../../components/ui/SysinfoPanel';
import { FeedbackTab } from '../feedback/FeedbackTab';
import { McpToolsContent } from '../mcp/McpToolsPage';
import { AcademicIdentitySection } from './AcademicIdentitySection';
import { tr } from '../../lib/i18n';
import { setTaskLogHistory, useTaskLogHistory } from '../../lib/prefs';
import {
  ApiError,
  LLM_STAGES,
  api,
  isAdmin,
  type AdminUserRead,
  type AffiliationMode,
  type EffectiveTestResult,
  type LlmCallLogRow,
  type LlmProviderInput,
  type LlmProviderKind,
  type LlmProviderRead,
  type LlmRoute,
  type LlmSelfConfig,
  type LlmTestCapability,
  type LlmTestModelInput,
  type LlmTestResult,
  type RegistrationCodeRead,
  type SshCredentialInput,
} from '../../lib/api';

/* ============================================================
   /settings — ①个人（只读）②SSH 凭据（所有人）
   ③LLM 管理（admin）④用量（admin）
   ============================================================ */

const KINDS: LlmProviderKind[] = ['openai_compat', 'anthropic'];

// ---------------- 个人 ----------------

function PersonalTab() {
  const queryClient = useQueryClient();
  const { data: me, isLoading, isError } = useQuery({ queryKey: ['me'], queryFn: () => api.me(), retry: false });
  const { data: usage } = useQuery({ queryKey: ['my-usage'], queryFn: () => api.myUsage(), retry: false });
  const [name, setName] = useState('');
  const [username, setUsername] = useState('');
  const [avatarVersion, setAvatarVersion] = useState(0);
  const avatarInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (me) {
      setName(me.display_name ?? '');
      setUsername(me.username ?? '');
    }
  }, [me]);

  const usernameValid = /^[a-z0-9_]{3,32}$/.test(username);
  const usernameLocked = !!me?.username_locked;

  const usernameMutation = useMutation({
    mutationFn: () => api.setUsername(username),
    onSuccess: () => {
      toast(tr('用户名已设置', 'Username set'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['me'] });
    },
    onError: (e) => {
      const msg = e instanceof Error ? e.message : String(e);
      toast(
        msg === 'USERNAME_TAKEN'
          ? tr('用户名已被占用，换一个吧', 'Username already taken')
          : msg === 'USERNAME_LOCKED'
            ? tr('用户名已锁定，不能再改', 'Username is locked')
            : `${tr('设置失败', 'Failed')}：${msg}`,
        'error',
      );
    },
  });

  const saveMutation = useMutation({
    mutationFn: () => api.updateMe({ display_name: name.trim() }),
    onSuccess: () => {
      toast(tr('个人资料已保存', 'Profile saved'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['me'] });
    },
    onError: (e) => toast(`${tr('保存失败', 'Save failed')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  const chatIndexMutation = useMutation({
    mutationFn: (v: boolean) => api.updateMySettings({ chat_fulltext_index: v }),
    onSuccess: (_, v) => {
      toast(v ? tr('已开启全文索引', 'Full-text index enabled') : tr('已关闭全文索引', 'Full-text index disabled'), 'ok');
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
    <>
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
      <FormField label={tr('姓名', 'Name')}>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder={tr('你的真实姓名', 'Your name')} />
      </FormField>
      <FormField label={tr('用户名', 'Username')}>
        {usernameLocked ? (
          <div className="row gap8" style={{ alignItems: 'center' }}>
            <input className="input mono" value={me.username ?? ''} disabled style={{ flex: 1 }} />
            <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-3)' }}>
              {tr('已锁定', 'Locked')}
            </span>
          </div>
        ) : (
          <>
            <div className="row gap8" style={{ alignItems: 'center' }}>
              <input
                className="input mono"
                value={username}
                onChange={(e) => setUsername(e.target.value.toLowerCase())}
                placeholder="e.g. zhang_san"
                style={{ flex: 1 }}
              />
              <button
                className="btn btn-soft"
                disabled={!usernameValid || usernameMutation.isPending || username === (me.username ?? '')}
                onClick={() => usernameMutation.mutate()}
              >
                {tr('保存', 'Save')}
              </button>
            </div>
            <div style={{ fontSize: 11, color: username && !usernameValid ? 'var(--danger-tx)' : 'var(--text-4)', marginTop: 4 }}>
              {tr('小写字母、数字、下划线 3-32 位；全局唯一。只能设置/修改一次', 'lowercase letters, digits, underscore; 3-32 chars; unique. Can only be set once')}
            </div>
          </>
        )}
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
      <div className="card" style={{ maxWidth: 560, marginTop: 16, padding: '14px 18px' }}>
        <div className="section-h">{tr('文献对话', 'Literature chat')}</div>
        <div className="row" style={{ gap: 16, alignItems: 'center', marginTop: 10 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div id="pref-chat-fulltext" style={{ fontSize: 13, lineHeight: 1.4 }}>
              {tr('为论文建立全文索引', 'Build a full-text index for papers')}
            </div>
            <div style={{ fontSize: 12, color: 'var(--text-3)', lineHeight: 1.45, marginTop: 2 }}>
              {tr(
                '打开后可为你的相关研究/个人文献库的论文建立全文索引，让 AI 文献对话检索得更准（会消耗你的模型额度抓取和嵌入全文）。',
                'When on, you can build a full-text index for the papers in your related work and personal library, so AI literature chat retrieves more accurately (it uses your model quota to fetch and embed the full text).',
              )}
            </div>
          </div>
          <Switch
            checked={me.settings?.chat_fulltext_index ?? false}
            onChange={(v) => chatIndexMutation.mutate(v)}
            disabled={chatIndexMutation.isPending}
            aria-labelledby="pref-chat-fulltext"
          />
        </div>
      </div>
      <AcademicIdentitySection />
      <PreferencesSection />
    </>
  );
}

// ---------------- 界面偏好（本地，存 localStorage） ----------------

function PreferencesSection() {
  const showHistory = useTaskLogHistory();
  return (
    <div className="card" style={{ maxWidth: 560, marginTop: 16, padding: '14px 18px' }}>
      <div className="section-h">
        {tr('界面偏好', 'Interface preferences')}
        <span style={{ fontSize: 11.5, fontWeight: 400, color: 'var(--text-4)' }}>
          {tr('只保存在本浏览器。', 'Saved in this browser only.')}
        </span>
      </div>
      <div className="row" style={{ gap: 16, alignItems: 'center', marginTop: 10 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div id="pref-task-log-history" style={{ fontSize: 13, lineHeight: 1.4 }}>
            {tr('任务终端展示历史日志', 'Show past logs in the task terminal')}
          </div>
          <div style={{ fontSize: 12, color: 'var(--text-3)', lineHeight: 1.45, marginTop: 2 }}>
            {tr(
              '打开任务详情时加载已保存的日志与大模型输出，刷新页面或任务结束后仍可回看。',
              'Loads saved logs and model output when you open a task, so they survive a refresh or task completion.',
            )}
          </div>
        </div>
        <Switch
          checked={showHistory}
          onChange={setTaskLogHistory}
          aria-labelledby="pref-task-log-history"
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
  const [sysinfoId, setSysinfoId] = useState<string | null>(null);

  // 服务器系统状态（展开时拉取，30s 自动刷新）
  const sysinfoQuery = useQuery({
    queryKey: ['ssh-credentials', sysinfoId, 'sysinfo'],
    queryFn: () => api.getSshCredentialSysinfo(sysinfoId!),
    enabled: sysinfoId != null,
    retry: false,
    refetchInterval: sysinfoId != null ? 30_000 : false,
  });

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
              <Fragment key={c.id}>
                <tr>
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
                        onClick={() => setSysinfoId(sysinfoId === c.id ? null : c.id)}
                      >
                        <Icon name="cpu" size={12} />
                        {sysinfoId === c.id ? tr('收起状态', 'Hide status') : tr('系统状态', 'System status')}
                      </button>
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
                {sysinfoId === c.id && (
                  <tr>
                    <td colSpan={6} style={{ background: 'var(--surface-2)', padding: '12px 16px' }}>
                      <SysinfoPanel
                        loading={sysinfoQuery.isLoading}
                        error={sysinfoQuery.isError}
                        info={sysinfoQuery.data}
                        onRefresh={() => void sysinfoQuery.refetch()}
                      />
                    </td>
                  </tr>
                )}
              </Fragment>
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
  /** 可用模型列表原始输入（逗号/换行分隔），保存时解析为数组 */
  models: string;
}

/** 逗号/换行分隔的模型输入 → 去空白、去重后的数组。 */
function parseModels(raw: string): string[] {
  return [...new Set(raw.split(/[\n,，]/).map((s) => s.trim()).filter(Boolean))];
}

function emptyDraft(): ProviderDraft {
  return { name: '', kind: 'openai_compat', base_url: '', api_key: '', enabled: true, models: '' };
}

function draftFrom(p: LlmProviderRead): ProviderDraft {
  return {
    name: p.name,
    kind: p.kind,
    base_url: p.base_url ?? '',
    api_key: '',
    enabled: p.enabled,
    models: (p.models ?? []).join('\n'),
  };
}

function toInput(d: ProviderDraft): LlmProviderInput {
  return {
    name: d.name.trim(),
    kind: d.kind,
    base_url: d.base_url.trim() || undefined,
    api_key: d.api_key, // 空字符串 = 不变（PATCH）；POST 时后端忽略空 key
    enabled: d.enabled,
    models: parseModels(d.models), // 整体替换（清空 = []）
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
          placeholder={tr('如 deepseek / claude', 'e.g. deepseek / claude')} />
      </FormField>
      <div className="row gap12" style={{ alignItems: 'flex-start' }}>
        <FormField label={tr('类型', 'Kind')} style={{ width: 180 }}>
          <SelectMenu
            value={draft.kind}
            options={KINDS.map((k) => ({ value: k, label: k }))}
            onChange={(v) => setDraft({ ...draft, kind: v as LlmProviderKind })}
          />
        </FormField>
        <FormField label="Base URL" style={{ flex: 1 }}>
          <input className="input mono" value={draft.base_url} onChange={(e) => setDraft({ ...draft, base_url: e.target.value })}
            placeholder="https://api.example.com/v1" disabled={draft.kind === 'fake'} />
        </FormField>
      </div>
      <FormField label="API Key"
        hint={isNew ? undefined : tr('留空 = 保持不变；后端只写不读，展示为 masked', 'Leave empty to keep unchanged; write-only on the backend, shown masked')}>
        <input className="input mono" type="password" autoComplete="new-password" value={draft.api_key}
          onChange={(e) => setDraft({ ...draft, api_key: e.target.value })}
          placeholder={isNew ? 'sk-…' : tr('••••••（留空不变）', '•••••• (empty = unchanged)')} disabled={draft.kind === 'fake'} />
      </FormField>
      <FormField label={tr('可用模型', 'Available models')}
        hint={tr('逗号或换行分隔；作为路由表 model 输入框的候选', 'Comma or newline separated; used as suggestions in the routing table model field')}>
        <textarea className="input mono" rows={3} style={{ resize: 'vertical', fontSize: 12 }}
          value={draft.models} onChange={(e) => setDraft({ ...draft, models: e.target.value })}
          placeholder={tr('如 gpt-5.6-sol, gpt-5.5（可留空）', 'e.g. gpt-5.6-sol, gpt-5.5 (optional)')} />
      </FormField>
      <label className="row gap8" style={{ fontSize: 13, cursor: 'pointer', userSelect: 'none' }}>
        <input type="checkbox" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />
        {tr('启用', 'Enabled')}
      </label>
    </>
  );
}

// ---- 模型连通性测试（Provider 区与路由表共用） ----

type TestState =
  | { status: 'idle' }
  | { status: 'testing' }
  | { status: 'ok'; latencyMs: number }
  | { status: 'error'; error: string };

/** 测试结果按 provider+model+capability 去重共享（跟随默认的行直接复用 default 的结果）。 */
const testKeyOf = (providerId: string, model: string, capability: LlmTestCapability) =>
  `${providerId}|${model}|${capability}`;

/** 模型连通性测试：相同 provider+model+capability 只实测一次，结果共享。 */
function useModelTests(testModel: (input: LlmTestModelInput) => Promise<LlmTestResult>) {
  const [results, setResults] = useState<Record<string, TestState>>({});
  const [testing, setTesting] = useState(false);

  const setOne = (key: string, state: TestState) =>
    setResults((prev) => ({ ...prev, [key]: state }));

  /** 返回 false 表示没有可测试的组合（由调用方决定提示文案）。 */
  const run = async (inputs: LlmTestModelInput[]): Promise<boolean> => {
    const combos = new Map<string, LlmTestModelInput>();
    for (const input of inputs) {
      const key = testKeyOf(input.provider_id, input.model, input.capability);
      if (!combos.has(key)) combos.set(key, input);
    }
    if (combos.size === 0) return false;
    setTesting(true);
    setResults((prev) => {
      const next = { ...prev };
      for (const key of combos.keys()) next[key] = { status: 'testing' };
      return next;
    });
    try {
      await Promise.all(
        [...combos.entries()].map(async ([key, input]) => {
          try {
            const r = await testModel(input);
            setOne(
              key,
              r.ok
                ? { status: 'ok', latencyMs: r.latency_ms }
                : { status: 'error', error: r.error || tr('测试失败', 'Test failed') },
            );
          } catch (e) {
            setOne(key, { status: 'error', error: e instanceof Error ? e.message : String(e) });
          }
        }),
      );
    } finally {
      setTesting(false);
    }
    return true;
  };

  return { results, testing, run };
}

function ModelStatusBadge({ state, onTest, idleHint }: {
  state: TestState;
  onTest?: () => void;
  /** 不可测试（onTest 未提供）时 idle 徽标的提示文案 */
  idleHint?: string;
}) {
  const clickable = onTest !== undefined && state.status !== 'testing';
  const base: CSSProperties = clickable ? { cursor: 'pointer' } : {};
  if (state.status === 'testing') {
    return (
      <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-2)' }}>
        <Icon name="refresh" size={11} style={{ animation: 'spin 1s linear infinite' }} />
        {tr('测试中…', 'Testing…')}
      </span>
    );
  }
  if (state.status === 'ok') {
    return (
      <span className="pill sm" style={{ ...base, background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}
        title={tr('点击重新测试', 'Click to retest')} onClick={onTest}>
        <Icon name="check" size={11} />
        {tr('正常', 'OK')} · {state.latencyMs.toLocaleString()}ms
      </span>
    );
  }
  if (state.status === 'error') {
    return (
      <span className="pill sm" style={{ ...base, background: 'var(--danger-bg)', color: 'var(--danger-tx)' }}
        title={state.error} onClick={onTest}>
        <Icon name="x" size={11} />
        {tr('失败', 'Failed')}
      </span>
    );
  }
  return (
    <span className="pill sm" style={{ ...base, background: 'var(--surface-3)', color: 'var(--text-3)' }}
      title={onTest ? tr('点击测试该模型', 'Click to test this model') : idleHint} onClick={onTest}>
      {tr('未测试', 'Untested')}
    </span>
  );
}

/** 「可用模型」列收起时最多展示的 chips 数。 */
const MODELS_COLLAPSED = 3;

/**
 * LLM 配置的两层各自的 api 方法组：admin 全局配置指向 /admin/llm，
 * 用户自管配置指向 /me/llm。ProvidersSection / RoutesSection 复用同一套 UI，
 * 只换 adapter（keyRoot 用于隔离 TanStack Query 缓存）。
 */
interface LlmAdapter {
  /** query 缓存命名空间：admin 全局 'llm'，用户自管 'my-llm' */
  keyRoot: string;
  listProviders: () => Promise<LlmProviderRead[]>;
  createProvider: (input: LlmProviderInput) => Promise<LlmProviderRead>;
  patchProvider: (id: string, input: Partial<LlmProviderInput>) => Promise<LlmProviderRead>;
  deleteProvider: (id: string) => Promise<void>;
  getRoutes: () => Promise<LlmRoute[]>;
  putRoutes: (routes: LlmRoute[]) => Promise<LlmRoute[]>;
  testModel: (input: LlmTestModelInput) => Promise<LlmTestResult>;
}

const adminLlmAdapter: LlmAdapter = {
  keyRoot: 'llm',
  listProviders: () => api.listLlmProviders(),
  createProvider: (input) => api.createLlmProvider(input),
  patchProvider: (id, input) => api.patchLlmProvider(id, input),
  deleteProvider: (id) => api.deleteLlmProvider(id),
  getRoutes: () => api.getLlmRoutes(),
  putRoutes: (routes) => api.putLlmRoutes(routes),
  testModel: (input) => api.testLlmModel(input),
};

const myLlmAdapter: LlmAdapter = {
  keyRoot: 'my-llm',
  listProviders: () => api.myLlmProviders(),
  createProvider: (input) => api.createMyLlmProvider(input),
  patchProvider: (id, input) => api.updateMyLlmProvider(id, input),
  deleteProvider: (id) => api.deleteMyLlmProvider(id),
  getRoutes: () => api.myLlmRoutes(),
  putRoutes: (routes) => api.replaceMyLlmRoutes(routes),
  testModel: (input) => api.testMyLlmModel(input),
};

function ProvidersSection({ adapter }: { adapter: LlmAdapter }) {
  const queryClient = useQueryClient();
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: [adapter.keyRoot, 'providers'],
    queryFn: () => adapter.listProviders(),
    retry: false,
  });
  const providers = data ?? [];

  const [modal, setModal] = useState<'closed' | 'create' | string>('closed'); // string = 编辑中的 provider id
  const [draft, setDraft] = useState<ProviderDraft>(emptyDraft());
  const [expandedModels, setExpandedModels] = useState<Set<string>>(new Set());
  const tests = useModelTests(adapter.testModel);

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: [adapter.keyRoot] });

  const createMutation = useMutation({
    mutationFn: () => adapter.createProvider(toInput(draft)),
    onSuccess: () => {
      toast(tr('Provider 已创建', 'Provider created'), 'ok');
      setModal('closed');
      invalidate();
    },
    onError: (err) => toast(`${tr('创建失败', 'Create failed')}：${err instanceof Error ? err.message : String(err)}`, 'error'),
  });
  const patchMutation = useMutation({
    mutationFn: (id: string) => adapter.patchProvider(id, toInput(draft)),
    onSuccess: () => {
      toast(tr('Provider 已更新', 'Provider updated'), 'ok');
      setModal('closed');
      invalidate();
    },
    onError: (err) => toast(`${tr('更新失败', 'Update failed')}：${err instanceof Error ? err.message : String(err)}`, 'error'),
  });
  const deleteMutation = useMutation({
    mutationFn: (id: string) => adapter.deleteProvider(id),
    onSuccess: () => {
      toast(tr('Provider 已删除', 'Provider deleted'), 'ok');
      invalidate();
    },
    onError: (err) => toast(`${tr('删除失败', 'Delete failed')}：${err instanceof Error ? err.message : String(err)}`, 'error'),
  });
  const toggleMutation = useMutation({
    mutationFn: (p: LlmProviderRead) => adapter.patchProvider(p.id, { enabled: !p.enabled }),
    onSuccess: (p) => {
      toast(p.enabled ? tr('Provider 已启用', 'Provider enabled') : tr('Provider 已停用', 'Provider disabled'), 'ok');
      invalidate();
    },
    onError: (err) => toast(`${tr('操作失败', 'Failed')}：${err instanceof Error ? err.message : String(err)}`, 'error'),
  });

  /** 每个 provider 用其 models 的第一个模型测 chat 连通性。 */
  const firstModelOf = (p: LlmProviderRead): string | null => (p.models ?? [])[0]?.trim() || null;
  const runProviderTests = async (list: LlmProviderRead[]) => {
    const inputs: LlmTestModelInput[] = [];
    for (const p of list) {
      const model = firstModelOf(p);
      if (model) inputs.push({ provider_id: p.id, model, capability: 'chat' });
    }
    if (!(await tests.run(inputs))) {
      toast(tr('没有可测试的 provider — 先在编辑里填写可用模型', 'Nothing to test — add models to a provider first'), 'error');
    }
  };

  const toggleModelsExpanded = (id: string) =>
    setExpandedModels((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const isNew = modal === 'create';
  const busy = createMutation.isPending || patchMutation.isPending;

  return (
    <div className="card card-pad" style={{ marginBottom: 20 }}>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 14 }}>
        <span className="section-h">
          <Icon name="server" size={15} style={{ color: 'var(--accent)' }} />
          {tr('LLM 供应商', 'Providers')}{' '}
          <span className="en-label" style={{ fontSize: 11 }}>
            {tr('测试用各 provider 的第一个可用模型', "tests use each provider's first model")}
          </span>
        </span>
        <div className="row gap8">
          <button className="btn btn-soft sm" disabled={tests.testing || providers.length === 0}
            onClick={() => void runProviderTests(providers)}>
            <Icon name="play" size={12} />
            {tests.testing ? tr('测试中…', 'Testing…') : tr('批量测试', 'Test all')}
          </button>
          <button className="btn btn-primary sm" onClick={() => { setDraft(emptyDraft()); setModal('create'); }}>
            <Icon name="plus" size={13} />
            {tr('新增 Provider', 'Add provider')}
          </button>
        </div>
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
        <div className="empty" style={{ padding: 24 }}>{tr('还没有 provider，先添加一个；配置好前 AI 功能不可用', 'No providers yet — add one; AI features stay unavailable until configured')}</div>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th style={{ width: 230 }}>{tr('名称', 'Name')}</th>
              <th style={{ width: 130 }}>api_key</th>
              <th>{tr('可用模型', 'Models')}</th>
              <th style={{ width: 80 }}>{tr('状态', 'Status')}</th>
              <th style={{ width: 130 }}>{tr('模型状态', 'Model status')}</th>
              <th style={{ width: 70 }} />
            </tr>
          </thead>
          <tbody>
            {providers.map((p) => {
              const models = p.models ?? [];
              const firstModel = firstModelOf(p);
              const expanded = expandedModels.has(p.id);
              const shownModels = expanded ? models : models.slice(0, MODELS_COLLAPSED);
              const hiddenCount = models.length - shownModels.length;
              const state: TestState = firstModel
                ? tests.results[testKeyOf(p.id, firstModel, 'chat')] ?? { status: 'idle' }
                : { status: 'idle' };
              return (
                <tr key={p.id}>
                  <td>
                    <div className="row gap6" style={{ alignItems: 'center' }}>
                      <span style={{ fontSize: 12, fontWeight: 650 }}>{p.name}</span>
                      <span className="pill sm mono" style={{ background: 'var(--surface-3)', color: 'var(--text-3)' }}>
                        {p.kind}
                      </span>
                    </div>
                    <div className="mono" title={p.base_url ?? undefined}
                      style={{ fontSize: 10.5, color: 'var(--text-3)', maxWidth: 210, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {p.base_url ?? '—'}
                    </div>
                  </td>
                  <td className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)' }}>{p.api_key_masked ?? '—'}</td>
                  <td>
                    {models.length === 0 ? (
                      <span style={{ fontSize: 11.5, color: 'var(--text-4)' }}>{tr('（未填写）', '(none)')}</span>
                    ) : (
                      <div className="row gap6" style={{ flexWrap: 'wrap' }}>
                        {shownModels.map((m) => (
                          <span key={m} className="tag mono" style={{ fontSize: 10.5 }}>{m}</span>
                        ))}
                        {hiddenCount > 0 && (
                          <span className="tag mono" role="button" title={models.join(', ')}
                            style={{ fontSize: 10.5, cursor: 'pointer', color: 'var(--accent-text)' }}
                            onClick={() => toggleModelsExpanded(p.id)}>
                            +{hiddenCount}
                          </span>
                        )}
                        {expanded && models.length > MODELS_COLLAPSED && (
                          <span className="tag" role="button"
                            style={{ fontSize: 10.5, cursor: 'pointer', color: 'var(--text-3)' }}
                            onClick={() => toggleModelsExpanded(p.id)}>
                            {tr('收起', 'Collapse')}
                          </span>
                        )}
                      </div>
                    )}
                  </td>
                  <td>
                    <span
                      className="pill sm"
                      role="button"
                      title={p.enabled ? tr('点击停用', 'Click to disable') : tr('点击启用', 'Click to enable')}
                      style={{
                        cursor: toggleMutation.isPending ? 'default' : 'pointer',
                        ...(p.enabled
                          ? { background: 'var(--ok-bg)', color: 'var(--ok-tx)' }
                          : { background: 'var(--surface-3)', color: 'var(--text-3)' }),
                      }}
                      onClick={() => { if (!toggleMutation.isPending) toggleMutation.mutate(p); }}
                    >
                      {p.enabled ? tr('启用', 'Enabled') : tr('停用', 'Disabled')}
                    </span>
                  </td>
                  <td>
                    <ModelStatusBadge
                      state={state}
                      onTest={firstModel ? () => void runProviderTests([p]) : undefined}
                      idleHint={firstModel ? undefined : tr('先填写可用模型才能测试', 'Add models first to enable testing')}
                    />
                  </td>
                  <td>
                    <div className="row gap6" style={{ justifyContent: 'flex-end' }}>
                      <button className="icon-btn" style={{ width: 26, height: 26 }} title={tr('编辑', 'Edit')}
                        onClick={() => { setDraft(draftFrom(p)); setModal(p.id); }}>
                        <Icon name="pen" size={13} />
                      </button>
                      <button className="icon-btn" style={{ width: 26, height: 26 }} title={tr('删除', 'Delete')}
                        disabled={deleteMutation.isPending}
                        onClick={() => {
                          if (window.confirm(`${tr('确定删除 Provider', 'Delete provider')} “${p.name}”？${tr('模型路由表里引用它的环节将失效。', 'Routing rows that reference it will stop working.')}`)) {
                            deleteMutation.mutate(p.id);
                          }
                        }}>
                        <Icon name="trash" size={13} />
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      <Modal
        open={modal !== 'closed'}
        onClose={() => setModal('closed')}
        title={isNew ? tr('新增 Provider', 'Add provider') : tr('编辑 Provider', 'Edit provider')}
        sub={tr('api_key 后端只写不读，展示为 masked', 'api_key is write-only on the backend and shown masked')}
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
  interview: { zh: '课题访谈', en: 'Topic interview' },
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
  rerank: { zh: '重排序', en: 'Reranking' },
};

/** 常驻顶层的行：默认 + 两个能力型环节；其余环节收进展开区。 */
const PRIMARY_STAGES: string[] = ['default', 'embedding', 'rerank'];

/** 能力型环节：不跟随「默认」（对话模型没有嵌入/重排能力），未设置即为「未设置」。 */
const CAPABILITY_STAGES = new Set(['embedding', 'rerank']);

/** 按环节推断测试能力：embedding → embedding，rerank → rerank，其余 chat。 */
function capabilityOf(stage: string): LlmTestCapability {
  if (stage === 'embedding') return 'embedding';
  if (stage === 'rerank') return 'rerank';
  return 'chat';
}

interface RouteDraft {
  provider_id: string;
  model: string;
  temperature: string;
}

// ---- 模型组合框：自由输入 + 候选下拉（面板视觉复用 components/ui/SelectMenu） ----

function ModelCombobox({ value, options, placeholder, muted, onChange }: {
  value: string;
  options: string[];
  placeholder?: string;
  /** 「跟随默认」行的弱化样式 */
  muted?: boolean;
  onChange: (v: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [hi, setHi] = useState(0);
  const wrapRef = useRef<HTMLDivElement>(null);
  useClickOutside(wrapRef, open, () => setOpen(false));

  const query = value.trim().toLowerCase();
  // 输入值精确等于某候选（刚点选完）时展示全部候选，否则按输入过滤
  const filtered = useMemo(() => {
    if (!query || options.some((m) => m.toLowerCase() === query)) return options;
    return options.filter((m) => m.toLowerCase().includes(query));
  }, [options, query]);

  const pick = (m: string) => {
    onChange(m);
    setOpen(false);
  };
  const openList = () => {
    if (options.length > 0) {
      setOpen(true);
      setHi(0);
    }
  };
  const onKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (!open) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        openList();
        e.preventDefault();
      }
      return;
    }
    if (e.key === 'ArrowDown') {
      setHi((h) => Math.min(h + 1, filtered.length - 1));
      e.preventDefault();
    } else if (e.key === 'ArrowUp') {
      setHi((h) => Math.max(h - 1, 0));
      e.preventDefault();
    } else if (e.key === 'Enter') {
      if (filtered[hi] !== undefined) {
        pick(filtered[hi]);
        e.preventDefault();
      }
    } else if (e.key === 'Escape') {
      setOpen(false);
    }
  };

  return (
    <div ref={wrapRef} style={{ position: 'relative' }}>
      <input
        className="input mono"
        style={{ height: 32, width: '100%', fontSize: 12, ...(muted ? { color: 'var(--text-3)' } : {}) }}
        value={value}
        placeholder={placeholder}
        onFocus={openList}
        onClick={openList}
        onChange={(e) => {
          onChange(e.target.value);
          if (options.length > 0) {
            setOpen(true);
            setHi(0);
          }
        }}
        onKeyDown={onKeyDown}
      />
      {open && filtered.length > 0 && (
        <DropdownList
          items={filtered.map((m) => ({ key: m, label: m }))}
          hi={hi}
          mono
          onHover={setHi}
          onPick={(i) => {
            const m = filtered[i];
            if (m !== undefined) pick(m);
          }}
        />
      )}
    </div>
  );
}

function RoutesSection({ adapter }: { adapter: LlmAdapter }) {
  const queryClient = useQueryClient();
  const providersQuery = useQuery({ queryKey: [adapter.keyRoot, 'providers'], queryFn: () => adapter.listProviders(), retry: false });
  const routesQuery = useQuery({ queryKey: [adapter.keyRoot, 'routes'], queryFn: () => adapter.getRoutes(), retry: false });
  const providers = providersQuery.data ?? [];

  // 只有显式设置过的 stage 才有行；其余环节运行时回退 default 路由
  const [rows, setRows] = useState<Record<string, RouteDraft>>({});
  const [showAll, setShowAll] = useState(false);
  const tests = useModelTests(adapter.testModel);

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
      return adapter.putRoutes(routes);
    },
    onSuccess: () => {
      toast(tr('模型路由表已保存', 'Model routing saved'), 'ok');
      void queryClient.invalidateQueries({ queryKey: [adapter.keyRoot, 'routes'] });
    },
    onError: (err) => toast(`${tr('保存失败', 'Save failed')}：${err instanceof Error ? err.message : String(err)}`, 'error'),
  });

  const defaultRow = rows['default'];
  const emptyDraftRow: RouteDraft = { provider_id: '', model: '', temperature: '' };

  // 编辑「跟随默认」的行时，以 default 的值为底稿转成显式设置；
  // 能力型环节不跟随默认，底稿从空开始
  const setRow = (stage: string, patch: Partial<RouteDraft>) =>
    setRows((prev) => {
      const seed = prev[stage]
        ?? (stage !== 'default' && !CAPABILITY_STAGES.has(stage) && prev['default']
          ? { ...prev['default'] }
          : emptyDraftRow);
      return { ...prev, [stage]: { ...seed, ...patch } };
    });
  const clearRow = (stage: string) =>
    setRows((prev) => {
      const next = { ...prev };
      delete next[stage];
      return next;
    });

  /** 行的生效路由：显式设置优先，否则跟随 default（能力型环节不跟随；不完整则 null）。 */
  const effectiveOf = (stage: string): RouteDraft | null => {
    const r = rows[stage]
      ?? (stage !== 'default' && !CAPABILITY_STAGES.has(stage) ? defaultRow : undefined);
    if (r && r.provider_id && r.model.trim()) return r;
    return null;
  };

  /** 测试一组 stage；去重与结果共享由 useModelTests 处理。 */
  const runTests = async (stages: string[]) => {
    const inputs: LlmTestModelInput[] = [];
    for (const stage of stages) {
      const eff = effectiveOf(stage);
      if (!eff) continue;
      inputs.push({ provider_id: eff.provider_id, model: eff.model.trim(), capability: capabilityOf(stage) });
    }
    if (!(await tests.run(inputs))) {
      toast(tr('没有可测试的行，先配置 provider 和模型', 'Nothing to test — set a provider and model first'), 'error');
    }
  };

  // 常驻行固定在顶部；展开区只包含其余环节（embedding/rerank 不重复出现）
  const visibleStages: string[] = showAll
    ? [...PRIMARY_STAGES, ...LLM_STAGES.filter((s) => !PRIMARY_STAGES.includes(s))]
    : PRIMARY_STAGES;
  // 收起态下有显式设置的隐藏行数（提示用）
  const hiddenExplicitCount = LLM_STAGES.filter((s) => !PRIMARY_STAGES.includes(s) && rows[s]).length;

  return (
    <div className="card card-pad">
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 14 }}>
        <span className="section-h">
          <Icon name="git" size={15} style={{ color: 'var(--accent)' }} />
          {tr('模型路由表', 'Model routing')}{' '}
          <span className="en-label" style={{ fontSize: 11 }}>
            {tr('未单独设置的环节自动跟随「默认」；向量嵌入/重排序需单独配置', 'stages without their own row follow "Default"; embeddings/reranking need their own config')}
          </span>
        </span>
        <div className="row gap8">
          <button className="btn btn-soft sm" disabled={tests.testing} onClick={() => void runTests(visibleStages)}>
            <Icon name="play" size={12} />
            {tests.testing ? tr('测试中…', 'Testing…') : tr('批量测试', 'Test all')}
          </button>
          <button className="btn btn-primary sm" disabled={saveMutation.isPending} onClick={() => saveMutation.mutate()}>
            <Icon name="check" size={13} />
            {saveMutation.isPending ? tr('保存中…', 'Saving…') : tr('保存路由表', 'Save routing')}
          </button>
        </div>
      </div>
      {routesQuery.isError && (
        <div className="field-hint" style={{ marginBottom: 10, color: 'var(--warn-tx)' }}>
          {tr('路由表加载失败（后端不可用），保存将覆盖整表。', 'Failed to load routes (backend unavailable); saving will overwrite the whole table.')}
        </div>
      )}
      <table className="table">
        <thead>
          <tr>
            <th style={{ width: 150 }}>{tr('环节', 'Stage')}</th>
            <th style={{ width: 170 }}>provider</th>
            <th>model</th>
            <th style={{ width: 90 }}>temperature</th>
            <th style={{ width: 130 }}>{tr('模型状态', 'Model status')}</th>
          </tr>
        </thead>
        <tbody>
          {visibleStages.map((stage) => {
            const explicit = rows[stage] !== undefined;
            const capability = CAPABILITY_STAGES.has(stage);
            const follows = !explicit && stage !== 'default' && !capability;
            const unset = capability && !explicit; // 能力型环节未设置：不跟随默认，运行时降级
            // 展示值：显式行用自己的；跟随默认的行弱化展示 default 的 provider/模型
            const shown = rows[stage] ?? (follows ? defaultRow : undefined) ?? emptyDraftRow;
            const label = STAGE_LABELS[stage];
            const eff = effectiveOf(stage);
            const state: TestState = eff
              ? tests.results[testKeyOf(eff.provider_id, eff.model.trim(), capabilityOf(stage))] ?? { status: 'idle' }
              : { status: 'idle' };
            const providerModels = providers.find((p) => p.id === shown.provider_id)?.models ?? [];
            return (
              <tr key={stage}>
                <td>
                  <div className="row gap6" style={{ alignItems: 'center' }}>
                    <span style={{ fontSize: 12, fontWeight: 650 }}>{label ? tr(label.zh, label.en) : stage}</span>
                    {follows && (
                      <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-3)' }}>
                        {tr('跟随默认', 'Follows default')}
                      </span>
                    )}
                    {unset && (
                      <span
                        className="pill sm"
                        style={{ background: 'var(--warn-bg)', color: 'var(--warn-tx)' }}
                        title={tr('该环节需要专用模型，不跟随默认；未配置时相关功能自动降级', 'This stage needs a dedicated model and never follows Default; features degrade while unset')}
                      >
                        {tr('未设置', 'Not set')}
                      </span>
                    )}
                    {explicit && stage !== 'default' && (
                      <button
                        className="icon-btn"
                        style={{ width: 20, height: 20 }}
                        title={capability
                          ? tr('清除设置，恢复「未设置」', 'Clear — back to "Not set"')
                          : tr('清除单独设置，恢复跟随默认', 'Clear this override and follow default again')}
                        onClick={() => clearRow(stage)}
                      >
                        <Icon name="x" size={11} />
                      </button>
                    )}
                  </div>
                  <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>{stage}</div>
                </td>
                <td>
                  <SelectMenu
                    style={{ height: 32 }}
                    muted={follows}
                    value={shown.provider_id}
                    options={[
                      { value: '', label: tr('（未配置）', '(not set)') },
                      ...providers.map((p) => ({ value: p.id, label: p.name })),
                    ]}
                    onChange={(v) => setRow(stage, { provider_id: v, model: '' })}
                  />
                </td>
                <td>
                  <ModelCombobox
                    value={shown.model}
                    options={providerModels}
                    muted={follows}
                    placeholder={unset
                      ? tr('未配置，相关功能将降级', 'Not set — related features degrade')
                      : tr('如 deepseek-chat', 'e.g. deepseek-chat')}
                    onChange={(v) => setRow(stage, { model: v })}
                  />
                </td>
                <td>
                  <input
                    className="input mono"
                    style={{ height: 32, width: '100%', fontSize: 12, ...(follows ? { color: 'var(--text-3)' } : {}) }}
                    value={shown.temperature}
                    placeholder={tr('默认', 'default')}
                    inputMode="decimal"
                    onChange={(e) => setRow(stage, { temperature: e.target.value })}
                  />
                </td>
                <td>
                  <ModelStatusBadge
                    state={state}
                    onTest={eff ? () => void runTests([stage]) : undefined}
                    idleHint={unset ? tr('未配置，批量测试将跳过该环节', 'Not set; batch tests skip this stage') : undefined}
                  />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="row" style={{ justifyContent: 'center', marginTop: 10 }}>
        <button className="btn btn-ghost sm" onClick={() => setShowAll((v) => !v)}>
          <Icon name="chevDown" size={12} style={showAll ? { transform: 'rotate(180deg)' } : undefined} />
          {showAll
            ? tr('收起，只看默认、向量嵌入与重排序', 'Collapse to Default, Embeddings and Reranking')
            : tr(
                `查看所有环节设置（共 ${LLM_STAGES.length} 个${hiddenExplicitCount > 0 ? `，${hiddenExplicitCount} 个已单独设置` : ''}）`,
                `Show all stages (${LLM_STAGES.length}${hiddenExplicitCount > 0 ? `, ${hiddenExplicitCount} overridden` : ''})`,
              )}
        </button>
      </div>
    </div>
  );
}

// ---------------- 调用日志 ----------------

const CALL_LOG_PAGE_SIZE = 50;

/** 单条日志的展开详情：request messages 逐条 + response 全文。 */
function CallLogDetailPanel({ id }: { id: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['llm', 'call-logs', 'detail', id],
    queryFn: () => api.getLlmCallLog(id),
    retry: false,
  });
  if (isLoading) return <div className="empty" style={{ padding: 16 }}>{tr('加载中…', 'Loading…')}</div>;
  if (isError || !data) return <div className="empty" style={{ padding: 16 }}>{tr('无法加载详情', 'Failed to load detail')}</div>;

  const messages = data.request?.messages;
  const images = data.request?.images;
  const preStyle: CSSProperties = {
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
    fontSize: 11.5,
    lineHeight: 1.55,
    maxHeight: 260,
    overflow: 'auto',
    margin: 0,
    padding: '8px 10px',
    background: 'var(--surface)',
    border: '1px solid var(--border)',
    borderRadius: 6,
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div>
        <div style={{ fontSize: 12, fontWeight: 650, marginBottom: 6 }}>{tr('输入', 'Request')}</div>
        {messages && messages.length > 0 ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {messages.map((m, i) => (
              <div key={i}>
                <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)', marginBottom: 3 }}>{m.role}</div>
                <pre className="mono" style={preStyle}>{m.content}</pre>
              </div>
            ))}
            {images && images.length > 0 && (
              <div className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>
                {tr('图片（不存原图）', 'Images (originals not stored)')}：{images.join(' ')}
              </div>
            )}
          </div>
        ) : data.request ? (
          <pre className="mono" style={preStyle}>{JSON.stringify(data.request, null, 2)}</pre>
        ) : (
          <div className="muted" style={{ fontSize: 11.5 }}>—</div>
        )}
      </div>
      <div>
        <div style={{ fontSize: 12, fontWeight: 650, marginBottom: 6 }}>{tr('输出', 'Response')}</div>
        {data.response != null && data.response !== '' ? (
          <pre className="mono" style={preStyle}>{data.response}</pre>
        ) : (
          <div className="muted" style={{ fontSize: 11.5 }}>—</div>
        )}
        {data.error && (
          <div style={{ marginTop: 8 }}>
            <div style={{ fontSize: 12, fontWeight: 650, marginBottom: 6, color: 'var(--danger-tx)' }}>{tr('错误', 'Error')}</div>
            <pre className="mono" style={{ ...preStyle, color: 'var(--danger-tx)' }}>{data.error}</pre>
          </div>
        )}
      </div>
    </div>
  );
}

function CallLogsSection() {
  const queryClient = useQueryClient();
  const [page, setPage] = useState(0);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const settingsQuery = useQuery({
    queryKey: ['llm', 'call-log-settings'],
    queryFn: () => api.getLlmCallLogSettings(),
    retry: false,
  });
  const enabled = settingsQuery.data?.enabled ?? false;

  const logsQuery = useQuery({
    queryKey: ['llm', 'call-logs', page],
    queryFn: () => api.listLlmCallLogs({ limit: CALL_LOG_PAGE_SIZE, offset: page * CALL_LOG_PAGE_SIZE }),
    retry: false,
  });
  const total = logsQuery.data?.total ?? 0;
  const items = logsQuery.data?.items ?? [];
  const pageCount = Math.max(1, Math.ceil(total / CALL_LOG_PAGE_SIZE));

  const toggleMutation = useMutation({
    mutationFn: (next: boolean) => api.putLlmCallLogSettings(next),
    onSuccess: (r) => {
      toast(r.enabled ? tr('调用日志已开启', 'Call logging enabled') : tr('调用日志已关闭', 'Call logging disabled'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['llm', 'call-log-settings'] });
    },
    onError: (e) => toast(`${tr('设置失败', 'Failed')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  const clearMutation = useMutation({
    mutationFn: () => api.clearLlmCallLogs(),
    onSuccess: (r) => {
      toast(tr(`已清空 ${r.deleted} 条日志`, `Cleared ${r.deleted} log entries`), 'ok');
      setExpandedId(null);
      setPage(0);
      void queryClient.invalidateQueries({ queryKey: ['llm', 'call-logs'] });
    },
    onError: (e) => toast(`${tr('清空失败', 'Clear failed')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const statusPill = (row: LlmCallLogRow) =>
    row.status === 'ok'
      ? { background: 'var(--ok-bg)', color: 'var(--ok-tx)' }
      : { background: 'var(--danger-bg)', color: 'var(--danger-tx)' };

  return (
    <div className="card card-pad" style={{ marginTop: 20 }}>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 6 }}>
        <span className="section-h">
          <Icon name="file" size={15} style={{ color: 'var(--accent)' }} />
          {tr('调用日志', 'Call logs')} <span className="en-label" style={{ fontSize: 11 }}>{tr('每次 LLM API 调用的输入/输出', 'full input/output of each LLM API call')}</span>
        </span>
        <div className="row gap8">
          <button
            className="btn btn-soft sm"
            disabled={clearMutation.isPending || total === 0}
            onClick={() => {
              if (window.confirm(tr('确定清空全部调用日志？此操作不可恢复。', 'Clear all call logs? This cannot be undone.'))) {
                clearMutation.mutate();
              }
            }}
          >
            <Icon name="trash" size={12} />
            {tr('清空日志', 'Clear logs')}
          </button>
          <button
            className={`btn sm ${enabled ? 'btn-primary' : 'btn-soft'}`}
            disabled={settingsQuery.isLoading || toggleMutation.isPending}
            onClick={() => toggleMutation.mutate(!enabled)}
          >
            {enabled ? tr('已开启 — 点击关闭', 'On — click to disable') : tr('已关闭 — 点击开启', 'Off — click to enable')}
          </button>
        </div>
      </div>
      <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginBottom: 14, lineHeight: 1.5 }}>
        {tr(
          '开启后会记录每次 LLM API 调用的完整输入与输出（图片只存大小占位，不存原图），用于排查问题；注意存储占用，日志只保留最近 7 天。',
          'When enabled, the full input and output of every LLM API call is recorded (images are stored as size placeholders only) for debugging. Mind the storage cost; logs are kept for 7 days only.',
        )}
      </div>

      {logsQuery.isLoading ? (
        <div className="empty" style={{ padding: 24 }}>{tr('加载中…', 'Loading…')}</div>
      ) : logsQuery.isError ? (
        <div className="empty" style={{ padding: 24 }}>
          {tr('无法加载日志（后端不可用或无权限）', 'Failed to load logs (backend unavailable or no permission)')}
          <div style={{ marginTop: 10 }}>
            <button className="btn btn-soft sm" onClick={() => void logsQuery.refetch()}>{tr('重试', 'Retry')}</button>
          </div>
        </div>
      ) : items.length === 0 ? (
        <div className="empty" style={{ padding: 24 }}>
          {enabled
            ? tr('还没有日志记录 — 发起一次 AI 任务后这里会出现记录', 'No log entries yet — run an AI task and entries will appear here')
            : tr('日志已关闭 — 打开开关后开始记录', 'Logging is off — turn it on to start recording')}
        </div>
      ) : (
        <>
          <table className="table">
            <thead>
              <tr>
                <th style={{ width: 140 }}>{tr('时间', 'Time')}</th>
                <th style={{ width: 110 }}>{tr('环节', 'Stage')}</th>
                <th>{tr('模型', 'Model')}</th>
                <th style={{ width: 90, textAlign: 'right' }}>{tr('时延', 'Latency')} (ms)</th>
                <th style={{ width: 120, textAlign: 'right' }}>tokens</th>
                <th style={{ width: 70 }}>{tr('状态', 'Status')}</th>
              </tr>
            </thead>
            <tbody>
              {items.map((row) => (
                <Fragment key={row.id}>
                  <tr style={{ cursor: 'pointer' }} onClick={() => setExpandedId(expandedId === row.id ? null : row.id)}>
                    <td className="mono" style={{ fontSize: 11 }}>{fmtTime(row.created_at)}</td>
                    <td className="mono" style={{ fontSize: 11.5 }}>{row.stage}</td>
                    <td className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)' }}>
                      {row.model}
                      <span style={{ color: 'var(--text-4)' }}> · {row.provider_name}</span>
                    </td>
                    <td className="mono" style={{ fontSize: 11.5, textAlign: 'right' }}>{row.duration_ms.toLocaleString()}</td>
                    <td className="mono" style={{ fontSize: 11.5, textAlign: 'right' }}>
                      {row.prompt_tokens.toLocaleString()} + {row.completion_tokens.toLocaleString()}
                    </td>
                    <td>
                      <span className="pill sm" style={statusPill(row)}>{row.status === 'ok' ? 'ok' : tr('出错', 'error')}</span>
                    </td>
                  </tr>
                  {expandedId === row.id && (
                    <tr>
                      <td colSpan={6} style={{ background: 'var(--surface-2)', padding: '12px 16px' }}>
                        <CallLogDetailPanel id={row.id} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
          <div className="row gap8" style={{ justifyContent: 'flex-end', marginTop: 12, alignItems: 'center' }}>
            <span style={{ fontSize: 11.5, color: 'var(--text-3)' }}>
              {tr(`共 ${total} 条 · 第 ${page + 1} / ${pageCount} 页`, `${total} entries · page ${page + 1} / ${pageCount}`)}
            </span>
            <button className="btn btn-soft sm" disabled={page === 0} onClick={() => { setExpandedId(null); setPage(page - 1); }}>
              {tr('上一页', 'Prev')}
            </button>
            <button className="btn btn-soft sm" disabled={page + 1 >= pageCount} onClick={() => { setExpandedId(null); setPage(page + 1); }}>
              {tr('下一页', 'Next')}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

function AffiliationModeSection() {
  const queryClient = useQueryClient();
  const { data, isLoading, isError } = useQuery({
    queryKey: ['affiliation-mode'],
    queryFn: () => api.getAffiliationMode(),
    retry: false,
  });
  const mode: AffiliationMode = data?.mode ?? 'on_add';

  const setMutation = useMutation({
    mutationFn: (m: AffiliationMode) => api.setAffiliationMode(m),
    onSuccess: (r) => {
      toast(tr('已保存机构抽取时机', 'Affiliation extraction timing saved'), 'ok');
      queryClient.setQueryData(['affiliation-mode'], r);
    },
    onError: (e) => toast(`${tr('保存失败', 'Save failed')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  return (
    <div className="card card-pad" style={{ marginTop: 20 }}>
      <div className="section-h" style={{ marginBottom: 6 }}>
        <Icon name="users" size={15} style={{ color: 'var(--accent)' }} />
        {tr('作者机构抽取', 'Author affiliation extraction')}
      </div>
      <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginBottom: 14, lineHeight: 1.5 }}>
        {tr(
          '大模型从论文标题页解析每位作者所属机构的时机。选「编译 wiki 时抽取」会把机构解析折叠进精读编译那一次调用里，省下一次单独的大模型调用。（DOI 论文的机构由 OpenAlex 结构化数据直接给出，不受此设置影响。）',
          "When the model parses each author's affiliation from the title page. \"Extract while compiling\" folds it into the deep-reading compile call, saving a separate LLM call. (Affiliations for DOI papers come straight from OpenAlex and are unaffected.)",
        )}
      </div>
      {isLoading ? (
        <div className="empty" style={{ padding: 16 }}>{tr('加载中…', 'Loading…')}</div>
      ) : isError ? (
        <div className="empty" style={{ padding: 16 }}>
          {tr('无法加载（后端不可用或无权限）', 'Failed to load (backend unavailable or no permission)')}
        </div>
      ) : (
        <Segmented
          options={[
            { v: 'on_add' as AffiliationMode, label: tr('添加时抽取', 'Extract on add') },
            {
              v: 'on_compile' as AffiliationMode,
              label: tr('编译 wiki 时抽取（省一次调用）', 'Extract while compiling (saves a call)'),
            },
          ]}
          value={mode}
          onChange={(v) => { if (v !== mode) setMutation.mutate(v); }}
        />
      )}
    </div>
  );
}

function LlmTab() {
  return (
    <>
      <ProvidersSection adapter={adminLlmAdapter} />
      <RoutesSection adapter={adminLlmAdapter} />
      <AffiliationModeSection />
      <CallLogsSection />
    </>
  );
}

// ---------------- 我的模型（每个用户自管那一层） ----------------

/** 被接管时的只读展示：把生效的 provider 与路由表原样列出。 */
function ManagedEffectiveView({ effective }: { effective: LlmSelfConfig }) {
  const providerName = (id: string) => effective.providers.find((p) => p.id === id)?.name ?? id;
  return (
    <>
      <div className="card card-pad" style={{ marginBottom: 20 }}>
        <div className="section-h" style={{ marginBottom: 12 }}>
          <Icon name="server" size={15} style={{ color: 'var(--accent)' }} />
          {tr('生效的供应商', 'Effective providers')}{' '}
          <span className="en-label" style={{ fontSize: 11 }}>{tr('平台默认，只读', 'platform defaults, read-only')}</span>
        </div>
        {effective.providers.length === 0 ? (
          <div className="empty" style={{ padding: 24 }}>{tr('管理员尚未配置任何 provider', 'The admin has not configured any provider yet')}</div>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th style={{ width: 230 }}>{tr('名称', 'Name')}</th>
                <th style={{ width: 130 }}>api_key</th>
                <th>{tr('可用模型', 'Models')}</th>
                <th style={{ width: 80 }}>{tr('状态', 'Status')}</th>
              </tr>
            </thead>
            <tbody>
              {effective.providers.map((p) => {
                const models = p.models ?? [];
                return (
                  <tr key={p.id}>
                    <td>
                      <div className="row gap6" style={{ alignItems: 'center' }}>
                        <span style={{ fontSize: 12, fontWeight: 650 }}>{p.name}</span>
                        <span className="pill sm mono" style={{ background: 'var(--surface-3)', color: 'var(--text-3)' }}>{p.kind}</span>
                      </div>
                      <div className="mono" title={p.base_url ?? undefined}
                        style={{ fontSize: 10.5, color: 'var(--text-3)', maxWidth: 210, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {p.base_url ?? '—'}
                      </div>
                    </td>
                    <td className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)' }}>{p.api_key_masked ?? '—'}</td>
                    <td>
                      {models.length === 0 ? (
                        <span style={{ fontSize: 11.5, color: 'var(--text-4)' }}>{tr('（未填写）', '(none)')}</span>
                      ) : (
                        <div className="row gap6" style={{ flexWrap: 'wrap' }}>
                          {models.map((m) => (
                            <span key={m} className="tag mono" style={{ fontSize: 10.5 }}>{m}</span>
                          ))}
                        </div>
                      )}
                    </td>
                    <td>
                      <span className="pill sm" style={p.enabled
                        ? { background: 'var(--ok-bg)', color: 'var(--ok-tx)' }
                        : { background: 'var(--surface-3)', color: 'var(--text-3)' }}>
                        {p.enabled ? tr('启用', 'Enabled') : tr('停用', 'Disabled')}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="card card-pad">
        <div className="section-h" style={{ marginBottom: 12 }}>
          <Icon name="git" size={15} style={{ color: 'var(--accent)' }} />
          {tr('生效的模型路由表', 'Effective model routing')}{' '}
          <span className="en-label" style={{ fontSize: 11 }}>{tr('平台默认，只读', 'platform defaults, read-only')}</span>
        </div>
        {effective.routes.length === 0 ? (
          <div className="empty" style={{ padding: 24 }}>{tr('管理员尚未配置路由表', 'The admin has not configured any routing yet')}</div>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th style={{ width: 180 }}>{tr('环节', 'Stage')}</th>
                <th style={{ width: 170 }}>provider</th>
                <th>model</th>
                <th style={{ width: 100 }}>temperature</th>
              </tr>
            </thead>
            <tbody>
              {effective.routes.map((r) => {
                const label = STAGE_LABELS[r.stage];
                return (
                  <tr key={r.stage}>
                    <td>
                      <div style={{ fontSize: 12, fontWeight: 650 }}>{label ? tr(label.zh, label.en) : r.stage}</div>
                      <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>{r.stage}</div>
                    </td>
                    <td style={{ fontSize: 12 }}>{providerName(r.provider_id)}</td>
                    <td className="mono" style={{ fontSize: 11.5 }}>{r.model}</td>
                    <td className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)' }}>
                      {r.temperature === null || r.temperature === undefined ? tr('默认', 'default') : r.temperature}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

// ---- 生效模型状态测试（被接管 / 自管两种状态都用） ----

type EffState =
  | { status: 'idle' }
  | { status: 'testing' }
  | { status: 'done'; result: EffectiveTestResult };

/**
 * 「模型状态」区：列出当前**生效**的每条 stage 路由，逐条测试。
 * 测的是用户实际生效的配置（被接管测全局、自管测自己），与是否自管无关，
 * 因此两种状态下都展示。
 */
function MyModelStatusSection({ config }: { config: LlmSelfConfig }) {
  const [results, setResults] = useState<Record<string, EffState>>({});
  const [testingAll, setTestingAll] = useState(false);

  const providerName = (id: string) => config.providers.find((p) => p.id === id)?.name ?? id;
  const routes = config.routes;

  const testOne = async (stage: string) => {
    setResults((prev) => ({ ...prev, [stage]: { status: 'testing' } }));
    try {
      const result = await api.testMyLlmEffective({ stage });
      setResults((prev) => ({ ...prev, [stage]: { status: 'done', result } }));
    } catch (e) {
      setResults((prev) => ({
        ...prev,
        [stage]: {
          status: 'done',
          result: {
            ok: false,
            latency_ms: 0,
            error: e instanceof Error ? e.message : String(e),
            model: '',
            provider_name: '',
            is_fake: false,
          },
        },
      }));
    }
  };

  const testAll = async () => {
    setTestingAll(true);
    try {
      for (const r of routes) await testOne(r.stage);
    } finally {
      setTestingAll(false);
    }
  };

  const renderResult = (state: EffState | undefined) => {
    if (!state || state.status === 'idle') return null;
    if (state.status === 'testing') {
      return (
        <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-2)' }}>
          <Icon name="refresh" size={11} style={{ animation: 'spin 1s linear infinite' }} />
          {tr('测试中…', 'Testing…')}
        </span>
      );
    }
    const r = state.result;
    if (r.is_fake) {
      return <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-3)' }}>{tr('未配置真实模型', 'No real model configured')}</span>;
    }
    if (r.ok) {
      return (
        <span className="pill sm" style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>
          <Icon name="check" size={11} />
          {r.latency_ms.toLocaleString()}ms
        </span>
      );
    }
    return (
      <span className="pill sm" style={{ background: 'var(--danger-bg)', color: 'var(--danger-tx)' }} title={r.error ?? undefined}>
        <Icon name="x" size={11} />
        {r.error || tr('失败', 'Failed')}
      </span>
    );
  };

  return (
    <div className="card card-pad" style={{ marginBottom: 20 }}>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 14 }}>
        <span className="section-h">
          <Icon name="server" size={15} style={{ color: 'var(--accent)' }} />
          {tr('模型状态', 'Model status')}{' '}
          <span className="en-label" style={{ fontSize: 11 }}>
            {tr('测试你当前生效的每条路由', 'test each route currently in effect for you')}
          </span>
        </span>
        <button className="btn btn-soft sm" disabled={testingAll || routes.length === 0} onClick={() => void testAll()}>
          <Icon name="play" size={12} />
          {testingAll ? tr('测试中…', 'Testing…') : tr('测试全部', 'Test all')}
        </button>
      </div>
      {routes.length === 0 ? (
        <div className="empty" style={{ padding: 24 }}>{tr('还没有生效的模型路由', 'No routes in effect yet')}</div>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th style={{ width: 180 }}>{tr('环节', 'Stage')}</th>
              <th>provider · model</th>
              <th style={{ width: 220 }}>{tr('状态', 'Status')}</th>
            </tr>
          </thead>
          <tbody>
            {routes.map((r) => {
              const label = STAGE_LABELS[r.stage];
              const state = results[r.stage];
              return (
                <tr key={r.stage}>
                  <td>
                    <div style={{ fontSize: 12, fontWeight: 650 }}>{label ? tr(label.zh, label.en) : r.stage}</div>
                    <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>{r.stage}</div>
                  </td>
                  <td className="mono" style={{ fontSize: 11.5 }}>
                    {providerName(r.provider_id)}
                    <span style={{ color: 'var(--text-3)' }}> · {r.model}</span>
                  </td>
                  <td>
                    <div className="row gap8" style={{ alignItems: 'center' }}>
                      <button
                        className="btn btn-soft sm"
                        disabled={testingAll || state?.status === 'testing'}
                        onClick={() => void testOne(r.stage)}
                      >
                        {tr('测试', 'Test')}
                      </button>
                      {renderResult(state)}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

function MyLlmTab() {
  const queryClient = useQueryClient();
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['my-llm', 'effective'],
    queryFn: () => api.myLlmEffective(),
    retry: false,
  });
  const [confirmOpen, setConfirmOpen] = useState(false);

  const invalidateAll = () => {
    void queryClient.invalidateQueries({ queryKey: ['my-llm'] });
    void queryClient.invalidateQueries({ queryKey: ['me'] });
  };

  const selfManageMutation = useMutation({
    mutationFn: () => api.llmSelfManage(),
    onSuccess: () => {
      toast(tr('已取消接管，现在使用你自己的配置', 'Takeover removed — now using your own configuration'), 'ok');
      setConfirmOpen(false);
      invalidateAll();
    },
    onError: (e) => toast(`${tr('操作失败', 'Failed')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  const managedMutation = useMutation({
    mutationFn: () => api.llmManaged(),
    onSuccess: () => {
      toast(tr('已切回管理员接管', 'Switched back to admin-managed'), 'ok');
      invalidateAll();
    },
    onError: (e) => toast(`${tr('操作失败', 'Failed')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  if (isLoading) return <div className="empty" style={{ padding: 24 }}>{tr('加载中…', 'Loading…')}</div>;
  if (isError || !data) {
    return (
      <div className="empty" style={{ padding: 24 }}>
        {tr('无法加载（后端不可用）', 'Failed to load (backend unavailable)')}
        <div style={{ marginTop: 10 }}>
          <button className="btn btn-soft sm" onClick={() => void refetch()}>{tr('重试', 'Retry')}</button>
        </div>
      </div>
    );
  }

  if (!data.self_managed) {
    return (
      <>
        <div className="card card-pad" style={{ marginBottom: 20, borderColor: 'var(--accent)', background: 'var(--accent-soft)' }}>
          <div className="row gap10" style={{ alignItems: 'flex-start' }}>
            <Icon name="shield" size={18} style={{ color: 'var(--accent)', flexShrink: 0, marginTop: 1 }} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 13, fontWeight: 650, color: 'var(--accent-text)' }}>
                {tr('你的模型配置由管理员统一接管', 'Your model configuration is managed by the admin')}
              </div>
              <div style={{ fontSize: 12, color: 'var(--text-2)', marginTop: 4, lineHeight: 1.5 }}>
                {tr(
                  '当前使用平台默认的 Provider 与路由表（见下方，只读）。你也可以取消接管，改用自己的 Provider 与密钥。',
                  'You are using the platform default providers and routing (shown below, read-only). You may remove the takeover and use your own providers and keys instead.',
                )}
              </div>
              <div style={{ marginTop: 12 }}>
                <button className="btn btn-primary sm" disabled={selfManageMutation.isPending} onClick={() => setConfirmOpen(true)}>
                  <Icon name="sliders" size={13} />
                  {tr('取消接管，自行设置', 'Remove takeover, configure my own')}
                </button>
              </div>
            </div>
          </div>
        </div>

        <MyModelStatusSection config={data} />

        <ManagedEffectiveView effective={data} />

        <Modal
          open={confirmOpen}
          onClose={() => setConfirmOpen(false)}
          width={440}
          title={tr('取消管理员接管？', 'Remove admin takeover?')}
          footer={
            <>
              <button className="btn btn-ghost" onClick={() => setConfirmOpen(false)}>{tr('取消', 'Cancel')}</button>
              <button className="btn btn-primary" disabled={selfManageMutation.isPending} onClick={() => selfManageMutation.mutate()}>
                {selfManageMutation.isPending ? tr('处理中…', 'Working…') : tr('确认取消接管', 'Confirm')}
              </button>
            </>
          }
        >
          <div style={{ fontSize: 13, lineHeight: 1.6, color: 'var(--text-2)' }}>
            {tr(
              '取消后将改用你自己的配置，管理员的默认配置对你失效。你需要自行填好 Provider 与路由表；在配好之前无法调用真实模型。随时可以切回接管。',
              'After this you will use your own configuration and the admin defaults no longer apply to you. You must set up your own providers and routing; until then no real model can be called. You can switch back to admin-managed at any time.',
            )}
          </div>
        </Modal>
      </>
    );
  }

  return (
    <>
      <div className="card card-pad" style={{ marginBottom: 20 }}>
        <div className="row gap10" style={{ alignItems: 'flex-start' }}>
          <Icon name="sliders" size={18} style={{ color: 'var(--accent)', flexShrink: 0, marginTop: 1 }} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 13, fontWeight: 650 }}>
              {tr('你正在使用自己的模型配置', 'You are using your own model configuration')}
            </div>
            <div style={{ fontSize: 12, color: 'var(--text-2)', marginTop: 4, lineHeight: 1.5 }}>
              {tr(
                '下面的 Provider 与路由表只对你自己生效（BYO key，后端只写不读）。配好后即可用自己的密钥调用模型。',
                'The providers and routing below apply only to you (BYO key, write-only on the backend). Once set up, models are called with your own keys.',
              )}
            </div>
          </div>
          <button className="btn btn-soft sm" style={{ flexShrink: 0 }} disabled={managedMutation.isPending} onClick={() => managedMutation.mutate()}>
            <Icon name="refresh" size={13} />
            {managedMutation.isPending ? tr('处理中…', 'Working…') : tr('切回管理员接管', 'Switch back to admin-managed')}
          </button>
        </div>
      </div>

      <MyModelStatusSection config={data} />

      <ProvidersSection adapter={myLlmAdapter} />
      <RoutesSection adapter={myLlmAdapter} />
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

// ---------------- 我的用量（个人） ----------------

function MyUsageTab() {
  const [days, setDays] = useState<'7' | '30' | '90'>('30');
  const { data: summary } = useQuery({ queryKey: ['my-usage'], queryFn: () => api.myUsage(), retry: false });
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['my-usage-history', days],
    queryFn: () => api.myUsageHistory({ days: Number(days) }),
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

  const used = summary?.tokens_used ?? 0;
  const quota = summary?.token_quota ?? null;
  const pct = quota != null && quota > 0 ? Math.min(100, Math.round((used / quota) * 100)) : null;

  return (
    <>
      <div className="card card-pad" style={{ marginBottom: 20, maxWidth: 560 }}>
        <div className="section-h" style={{ marginBottom: 12 }}>
          <Icon name="chart" size={15} style={{ color: 'var(--accent)' }} />
          {tr('我的 AI 用量', 'My AI usage')}{' '}
          <span className="en-label" style={{ fontSize: 11 }}>{tr('累计消耗', 'total consumed')}</span>
        </div>
        <div className="row gap12" style={{ alignItems: 'baseline' }}>
          <span className="mono" style={{ fontSize: 22, fontWeight: 700 }}>{used.toLocaleString()}</span>
          <span style={{ fontSize: 12.5, color: 'var(--text-3)' }}>
            tokens{quota != null ? ` / ${tr('配额', 'quota')} ${quota.toLocaleString()}` : ` · ${tr('不限配额', 'unlimited quota')}`}
          </span>
        </div>
        {pct != null && (
          <div style={{ marginTop: 10 }}>
            <div style={{ height: 8, borderRadius: 999, background: 'var(--surface-3)', overflow: 'hidden' }}>
              <div style={{ width: `${pct}%`, height: '100%', borderRadius: 999, background: pct >= 100 ? 'var(--danger-tx)' : 'var(--accent)' }} />
            </div>
            <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 4 }}>
              {tr(`已用 ${pct}%`, `${pct}% used`)}
            </div>
          </div>
        )}
      </div>

      <div className="card card-pad">
        <div className="row" style={{ justifyContent: 'space-between', marginBottom: 14 }}>
          <span className="section-h">
            <Icon name="chart" size={15} style={{ color: 'var(--accent)' }} />
            {tr('用量明细', 'Usage details')} <span className="en-label" style={{ fontSize: 11 }}>{tr('按天 × stage', 'per day × stage')}</span>
          </span>
          <Segmented options={[{ v: '7' as const, label: tr('7 天', '7 days') }, { v: '30' as const, label: tr('30 天', '30 days') }, { v: '90' as const, label: tr('90 天', '90 days') }]}
            value={days} onChange={setDays} />
        </div>
        {isLoading ? (
          <div className="empty" style={{ padding: 24 }}>{tr('加载中…', 'Loading…')}</div>
        ) : isError ? (
          <div className="empty" style={{ padding: 24 }}>
            {tr('无法加载用量数据（后端不可用）', 'Failed to load usage data (backend unavailable)')}
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
    </>
  );
}

// ---------------- 页面 ----------------

type Tab = 'personal' | 'ssh' | 'mymodels' | 'myusage' | 'mcp' | 'llm' | 'daily' | 'usage' | 'users' | 'codes' | 'feedback';

// ---------------- 每日新论文订阅分类（admin） ----------------

/** arxiv 分类的大致格式：如 cs.AI / stat.ML / hep-th。 */
const DAILY_CATEGORY_RE = /^[a-z][a-z-]+(\.[A-Za-z]{2,10})?$/;

function DailyCategoriesTab() {
  const queryClient = useQueryClient();
  const { data, isLoading, isError } = useQuery({
    queryKey: ['daily-categories'],
    queryFn: () => api.getDailyCategories(),
    retry: false,
  });
  // 本地编辑副本：首次拿到数据后接管，避免 refetch 覆盖未保存的改动
  const [cats, setCats] = useState<string[] | null>(null);
  const [input, setInput] = useState('');
  useEffect(() => {
    if (data && cats === null) setCats(data.categories);
  }, [data, cats]);

  const shown = cats ?? data?.categories ?? [];
  const dirty = !!data && JSON.stringify(shown) !== JSON.stringify(data.categories);

  const saveMutation = useMutation({
    mutationFn: () => api.setDailyCategories(shown),
    onSuccess: (res) => {
      toast(tr('订阅分类已保存', 'Subscribed categories saved'), 'ok');
      setCats(res.categories);
      void queryClient.invalidateQueries({ queryKey: ['daily-categories'] });
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 422) {
        toast(tr('分类格式不对', 'Invalid category format'), 'error');
      } else {
        toast(`${tr('保存失败', 'Save failed')}：${e instanceof Error ? e.message : String(e)}`, 'error');
      }
    },
  });

  const add = () => {
    const v = input.trim();
    if (!v) return;
    if (!DAILY_CATEGORY_RE.test(v)) {
      toast(tr('分类格式不对，应形如 cs.AI / stat.ML', 'Invalid format — expected e.g. cs.AI / stat.ML'), 'error');
      return;
    }
    if (!shown.includes(v)) setCats([...shown, v]);
    setInput('');
  };

  if (isLoading) return <div className="empty">{tr('加载中…', 'Loading…')}</div>;
  if (isError) {
    return <div className="empty">{tr('无法加载订阅分类（后端不可用）', 'Failed to load categories (backend unavailable)')}</div>;
  }

  return (
    <div className="card card-pad" style={{ maxWidth: 640 }}>
      <div className="section-h" style={{ marginBottom: 6 }}>
        <Icon name="book" size={15} style={{ color: 'var(--accent)' }} />
        {tr('每日新论文订阅分类', 'Daily papers subscribed categories')}
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 14 }}>
        {tr(
          '每天从 arxiv 抓取这些分类下的新提交，全实验室共用一份。改动从下一次抓取开始生效。',
          'New arxiv submissions in these categories are fetched daily, shared lab-wide. Changes apply from the next fetch.',
        )}
      </div>

      <div className="row gap6 wrap" style={{ marginBottom: 12 }}>
        {shown.length === 0 ? (
          <span style={{ fontSize: 12, color: 'var(--text-4)' }}>
            {tr('还没有订阅分类，先添加一个。', 'No categories yet — add one below.')}
          </span>
        ) : (
          shown.map((c) => (
            <span
              key={c}
              className="pill sm mono"
              style={{ background: 'var(--surface-3)', gap: 4, paddingRight: 5 }}
            >
              {c}
              <button
                title={tr('移除', 'Remove')}
                onClick={() => setCats(shown.filter((x) => x !== c))}
                style={{
                  border: 'none',
                  background: 'transparent',
                  cursor: 'pointer',
                  color: 'var(--text-3)',
                  display: 'inline-flex',
                  padding: 1,
                }}
              >
                <Icon name="x" size={10} />
              </button>
            </span>
          ))
        )}
      </div>

      <div className="row gap8">
        <input
          className="input mono"
          style={{ width: 200 }}
          placeholder={tr('如 cs.AI，回车添加', 'e.g. cs.AI, Enter to add')}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
              e.preventDefault();
              add();
            }
          }}
        />
        <button className="btn btn-soft sm" disabled={!input.trim()} onClick={add}>
          <Icon name="plus" size={12} />
          {tr('添加', 'Add')}
        </button>
        <div style={{ flex: 1 }} />
        <button
          className="btn btn-primary sm"
          disabled={!dirty || saveMutation.isPending}
          onClick={() => saveMutation.mutate()}
        >
          {saveMutation.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
        </button>
      </div>
    </div>
  );
}


// ---------------- 用户管理（admin） ----------------

const FEATURE_LABELS: [string, string, string][] = [
  ['forge', '想法生成', 'Idea generation'],
  ['review', '想法评审', 'Idea review'],
  ['experiment', '实验搭建', 'Experiment lab'],
  ['writer', '论文撰写', 'Paper writing'],
  ['paper_review', '论文评审', 'Paper review'],
];

const USERNAME_RE = /^[a-z0-9_]{3,32}$/;

/** 把后端用户 CRUD 的错误码翻成友好文案。 */
function userCrudErrorMessage(e: unknown): string {
  let code = '';
  let reason = '';
  if (e instanceof ApiError) {
    const detail = (e.body as { detail?: unknown } | undefined)?.detail;
    if (typeof detail === 'string') {
      code = detail;
    } else if (detail && typeof detail === 'object') {
      const d = detail as { code?: unknown; reason?: unknown };
      code = typeof d.code === 'string' ? d.code : '';
      reason = typeof d.reason === 'string' ? d.reason : '';
    } else {
      code = e.message;
    }
  } else if (e instanceof Error) {
    code = e.message;
  } else {
    code = String(e);
  }
  switch (code) {
    case 'USERNAME_TAKEN':
      return tr('用户名已被占用，请换一个', 'Username already taken');
    case 'EMAIL_TAKEN':
      return tr('该邮箱已注册', 'Email already registered');
    case 'INVALID_PASSWORD':
      return reason
        ? `${tr('密码不符合要求', 'Password does not meet requirements')}：${reason}`
        : tr('密码不符合要求（至少 8 位）', 'Password does not meet requirements (at least 8 characters)');
    case 'CANNOT_DELETE_SELF':
      return tr('不能删除自己的账号', 'You cannot delete your own account');
    case 'USER_NOT_FOUND':
      return tr('用户不存在（可能已被删除）', 'User not found (may already be deleted)');
    case 'CANNOT_MODIFY_SELF_ROLE':
      return tr('不能修改自己的角色或停用自己', 'You cannot change your own role or deactivate yourself');
    default:
      return `${tr('操作失败', 'Failed')}：${code || tr('未知错误', 'unknown error')}`;
  }
}

function CreateUserModal({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const [email, setEmail] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [role, setRole] = useState<'member' | 'admin'>('member');
  const [llmAccess, setLlmAccess] = useState<'full' | 'chat_only' | 'blocked'>('full');
  const [quota, setQuota] = useState('');

  const emailOk = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim());
  const usernameOk = USERNAME_RE.test(username.trim());
  const passwordOk = password.length >= 8;
  const quotaInvalid = quota.trim() !== '' && (!Number.isFinite(Number(quota)) || Number(quota) < 0);
  const canSubmit = emailOk && usernameOk && passwordOk && displayName.trim() !== '' && !quotaInvalid;

  const createMutation = useMutation({
    mutationFn: () =>
      api.adminCreateUser({
        email: email.trim(),
        password,
        display_name: displayName.trim(),
        username: username.trim(),
        role,
        llm_access: llmAccess,
        token_quota: quota.trim() === '' ? null : Math.max(0, Number(quota)),
      }),
    onSuccess: () => {
      toast(tr('已添加用户', 'User created'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['admin-users'] });
      onClose();
    },
    onError: (e) => toast(userCrudErrorMessage(e), 'error'),
  });

  return (
    <Modal
      open
      onClose={onClose}
      width={520}
      title={tr('添加用户', 'Add user')}
      sub={tr('创建一个新账号并设定初始密码', 'Create a new account with an initial password')}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>{tr('取消', 'Cancel')}</button>
          <button className="btn btn-primary" disabled={!canSubmit || createMutation.isPending} onClick={() => createMutation.mutate()}>
            {tr('创建', 'Create')}
          </button>
        </>
      }
    >
      <FormField label={tr('邮箱', 'Email')} error={email.trim() !== '' && !emailOk ? tr('邮箱格式不正确', 'Invalid email format') : null}>
        <input className="input" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="name@example.com" autoComplete="off" />
      </FormField>
      <FormField label={tr('姓名', 'Name')}>
        <input className="input" value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder={tr('显示名称', 'Display name')} autoComplete="off" />
      </FormField>
      <FormField
        label={tr('用户名', 'Username')}
        hint={tr('3–32 位小写字母、数字或下划线', '3–32 lowercase letters, digits or underscores')}
        error={username.trim() !== '' && !usernameOk ? tr('只能用小写字母、数字、下划线，长度 3–32', 'Only lowercase letters, digits, underscores; length 3–32') : null}
      >
        <input className="input mono" value={username} onChange={(e) => setUsername(e.target.value.toLowerCase())} placeholder="e.g. li_ming" autoComplete="off" />
      </FormField>
      <FormField
        label={tr('初始密码', 'Initial password')}
        hint={tr('至少 8 位；用户登录后可自行修改', 'At least 8 characters; the user can change it after signing in')}
        error={password !== '' && !passwordOk ? tr('密码至少 8 位', 'Password must be at least 8 characters') : null}
      >
        <input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
      </FormField>
      <div className="row gap10" style={{ marginBottom: 16 }}>
        <FormField label={tr('角色', 'Role')} style={{ flex: 1, marginBottom: 0 }}>
          <SelectMenu
            value={role}
            options={[
              { value: 'member', label: tr('成员', 'Member') },
              { value: 'admin', label: tr('管理员', 'Admin') },
            ]}
            onChange={(v) => setRole(v as 'member' | 'admin')}
          />
        </FormField>
        <FormField label={tr('大模型使用', 'LLM access')} style={{ flex: 1, marginBottom: 0 }}>
          <SelectMenu
            value={llmAccess}
            options={[
              { value: 'full', label: tr('不限', 'Unrestricted') },
              { value: 'chat_only', label: tr('仅对话', 'Chat only') },
              { value: 'blocked', label: tr('锁定', 'Blocked') },
            ]}
            onChange={(v) => setLlmAccess(v as 'full' | 'chat_only' | 'blocked')}
          />
        </FormField>
      </div>
      <FormField
        label={tr('AI token 配额', 'Token quota')}
        hint={tr('留空 = 不限。达到配额后不能再发起 AI 任务', 'Empty = unlimited. Once reached, no new AI tasks can start')}
        error={quotaInvalid ? tr('请输入非负整数', 'Enter a non-negative number') : null}
      >
        <input className="input mono" value={quota} onChange={(e) => setQuota(e.target.value)} placeholder={tr('不限', 'Unlimited')} />
      </FormField>
    </Modal>
  );
}

function DeleteUserModal({ u, onClose }: { u: AdminUserRead; onClose: () => void }) {
  const queryClient = useQueryClient();
  const deleteMutation = useMutation({
    mutationFn: () => api.adminDeleteUser(u.id),
    onSuccess: () => {
      toast(tr('已删除用户', 'User deleted'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['admin-users'] });
      onClose();
    },
    onError: (e) => toast(userCrudErrorMessage(e), 'error'),
  });
  return (
    <Modal
      open
      onClose={onClose}
      width={440}
      title={tr('删除用户', 'Delete user')}
      sub={u.display_name || u.email}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>{tr('取消', 'Cancel')}</button>
          <button className="btn btn-danger" disabled={deleteMutation.isPending} onClick={() => deleteMutation.mutate()}>
            {tr('删除', 'Delete')}
          </button>
        </>
      }
    >
      <div style={{ fontSize: 13, color: 'var(--text-2)', lineHeight: 1.7 }}>
        {tr('确定要删除该用户吗？此操作不可恢复，该用户的账号将被永久移除。', 'Delete this user? This cannot be undone — the account will be permanently removed.')}
      </div>
    </Modal>
  );
}

function BatchDeleteUsersModal({ userIds, onClose, onDone }: { userIds: string[]; onClose: () => void; onDone: () => void }) {
  const deleteMutation = useMutation({
    mutationFn: () => api.adminBatchDeleteUsers(userIds),
    onSuccess: (r) => {
      toast(tr(`已删除 ${r.deleted} 个用户`, `Deleted ${r.deleted} users`), 'ok');
      onDone();
      onClose();
    },
    onError: (e) => toast(userCrudErrorMessage(e), 'error'),
  });
  return (
    <Modal
      open
      onClose={onClose}
      width={440}
      title={tr('批量删除用户', 'Delete users in bulk')}
      sub={tr(`已选 ${userIds.length} 个用户`, `${userIds.length} users selected`)}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>{tr('取消', 'Cancel')}</button>
          <button className="btn btn-danger" disabled={deleteMutation.isPending} onClick={() => deleteMutation.mutate()}>
            {tr('删除', 'Delete')}
          </button>
        </>
      }
    >
      <div style={{ fontSize: 13, color: 'var(--text-2)', lineHeight: 1.7 }}>
        {tr('确定要删除选中的用户吗？此操作不可恢复。如果其中包含你自己的账号，会自动跳过。', 'Delete the selected users? This cannot be undone. Your own account, if selected, will be skipped automatically.')}
      </div>
    </Modal>
  );
}

function UserEditModal({ u, onClose }: { u: AdminUserRead; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [displayName, setDisplayName] = useState(u.display_name || '');
  const [username, setUsername] = useState(u.username || '');
  const [password, setPassword] = useState('');
  const [role, setRole] = useState(u.role);
  const [active, setActive] = useState(u.is_active);
  const [quota, setQuota] = useState(u.token_quota != null ? String(u.token_quota) : '');
  const [llmAccess, setLlmAccess] = useState(u.llm_access || 'full');
  const [features, setFeatures] = useState<Record<string, boolean>>(() => {
    const f: Record<string, boolean> = {};
    for (const [k] of FEATURE_LABELS) f[k] = u.features?.[k] !== false;
    return f;
  });

  const usernameOk = username.trim() === '' || USERNAME_RE.test(username.trim());
  const passwordOk = password === '' || password.length >= 8;

  const saveMutation = useMutation({
    mutationFn: () =>
      api.adminUpdateUser(u.id, {
        display_name: displayName.trim(),
        username: username.trim(),
        ...(password !== '' ? { password } : {}),
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
    onError: (e) => toast(userCrudErrorMessage(e), 'error'),
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
          <button className="btn btn-primary" disabled={saveMutation.isPending || quotaInvalid || !usernameOk || !passwordOk} onClick={() => saveMutation.mutate()}>
            {tr('保存', 'Save')}
          </button>
        </>
      }
    >
      <div className="row gap10" style={{ marginBottom: 16 }}>
        <FormField label={tr('姓名', 'Name')} style={{ flex: 1, marginBottom: 0 }}>
          <input className="input" value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder={tr('显示名称', 'Display name')} />
        </FormField>
        <FormField
          label={tr('用户名', 'Username')}
          style={{ flex: 1, marginBottom: 0 }}
          error={!usernameOk ? tr('只能用小写字母、数字、下划线，长度 3–32', 'Only lowercase letters, digits, underscores; length 3–32') : null}
        >
          <input className="input mono" value={username} onChange={(e) => setUsername(e.target.value.toLowerCase())} placeholder="—" />
        </FormField>
      </div>
      <FormField
        label={tr('重置密码', 'Reset password')}
        hint={tr('留空则不修改；填写则至少 8 位', 'Leave blank to keep unchanged; at least 8 characters if set')}
        error={!passwordOk ? tr('密码至少 8 位', 'Password must be at least 8 characters') : null}
      >
        <input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder={tr('不修改', 'Unchanged')} autoComplete="new-password" />
      </FormField>
      <div className="row gap10" style={{ marginBottom: 16 }}>
        <FormField label={tr('角色', 'Role')} style={{ flex: 1, marginBottom: 0 }}>
          <SelectMenu
            value={role}
            options={[
              { value: 'member', label: tr('成员', 'Member') },
              { value: 'admin', label: tr('管理员', 'Admin') },
            ]}
            onChange={setRole}
          />
        </FormField>
        <FormField label={tr('状态', 'Status')} style={{ flex: 1, marginBottom: 0 }}>
          <SelectMenu
            value={active ? '1' : '0'}
            options={[
              { value: '1', label: tr('启用', 'Active') },
              { value: '0', label: tr('停用', 'Disabled') },
            ]}
            onChange={(v) => setActive(v === '1')}
          />
        </FormField>
      </div>
      <FormField label={tr('大模型使用', 'LLM access')} hint={tr('限制该用户能否调用大模型', 'Controls whether this user can call LLMs')}>
        <SelectMenu
          value={llmAccess}
          options={[
            { value: 'full', label: tr('不限（全部功能）', 'Unrestricted (all features)') },
            { value: 'chat_only', label: tr('仅文献对话与 AI 伴读', 'Paper chat and reading assistant only') },
            { value: 'blocked', label: tr('锁定（禁止使用大模型）', 'Blocked (no LLM use)') },
          ]}
          onChange={setLlmAccess}
        />
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
      title={tr('批量分配课题', 'Assign topics in bulk')}
      sub={tr(`已选 ${userIds.length} 个用户，将以成员身份加入所选课题`, `${userIds.length} users selected; they will join the chosen topics as members`)}
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
        <div className="empty">{tr('加载课题列表…', 'Loading topics…')}</div>
      ) : projects.length === 0 ? (
        <div className="empty">{tr('还没有课题', 'No topics yet')}</div>
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
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: () => api.me(), retry: false });
  const [editing, setEditing] = useState<AdminUserRead | null>(null);
  const [deleting, setDeleting] = useState<AdminUserRead | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [batchDeleteOpen, setBatchDeleteOpen] = useState(false);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [assignOpen, setAssignOpen] = useState(false);

  const takeoverMutation = useMutation({
    mutationFn: (u: AdminUserRead) => api.adminUpdateUser(u.id, { llm_self_managed: !u.llm_self_managed }),
    onSuccess: (u) => {
      toast(u.llm_self_managed ? tr('已释放：该用户自管 LLM', 'Released — the user manages their own LLM') : tr('已接管：使用平台默认配置', 'Taken over — using platform defaults'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['admin-users'] });
    },
    onError: (e) => toast(`${tr('操作失败', 'Failed')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

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
        <div className="row gap8" style={{ marginLeft: 'auto' }}>
          {checked.size > 0 && (
            <button className="btn btn-danger" onClick={() => setBatchDeleteOpen(true)}>
              {tr('删除选中', 'Delete selected')}（{checked.size}）
            </button>
          )}
          <button className="btn btn-soft" disabled={checked.size === 0} onClick={() => setAssignOpen(true)}>
            {tr('批量分配课题', 'Assign topics')}{checked.size > 0 ? `（${checked.size}）` : ''}
          </button>
          <button className="btn btn-primary row gap6" onClick={() => setCreateOpen(true)}>
            <Icon name="plus" size={14} />
            {tr('添加用户', 'Add user')}
          </button>
        </div>
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
              <th>{tr('LLM 接管', 'LLM takeover')}</th>
              <th>{tr('功能权限', 'Features')}</th>
              <th style={{ width: 108 }}></th>
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
                <td>
                  <span
                    className="pill sm"
                    role="button"
                    title={u.llm_self_managed
                      ? tr('该用户自管 LLM，点击改为管理员接管', 'User manages their own LLM; click to take over')
                      : tr('由管理员接管，点击释放给用户自管', 'Managed by admin; click to release to the user')}
                    style={{
                      cursor: takeoverMutation.isPending ? 'default' : 'pointer',
                      ...(u.llm_self_managed
                        ? { background: 'var(--surface-3)', color: 'var(--text-3)' }
                        : { background: 'var(--accent-soft)', color: 'var(--accent-text)' }),
                    }}
                    onClick={() => { if (!takeoverMutation.isPending) takeoverMutation.mutate(u); }}
                  >
                    {u.llm_self_managed ? tr('已释放', 'Released') : tr('接管中', 'Managed')}
                  </span>
                </td>
                <td style={{ fontSize: 11.5, color: 'var(--text-2)' }}>{featureSummary(u)}</td>
                <td>
                  <div className="row gap6" style={{ justifyContent: 'flex-end' }}>
                    <button className="btn btn-ghost sm" onClick={() => setEditing(u)}>{tr('编辑', 'Edit')}</button>
                    {me?.id === u.id ? (
                      <button className="icon-btn" disabled title={tr('不能删除自己的账号', 'You cannot delete your own account')} style={{ opacity: 0.4 }}>
                        <Icon name="trash" size={15} />
                      </button>
                    ) : (
                      <button className="icon-btn" title={tr('删除用户', 'Delete user')} style={{ color: 'var(--danger-tx)' }} onClick={() => setDeleting(u)}>
                        <Icon name="trash" size={15} />
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {editing && <UserEditModal u={editing} onClose={() => setEditing(null)} />}
      {deleting && <DeleteUserModal u={deleting} onClose={() => setDeleting(null)} />}
      {createOpen && <CreateUserModal onClose={() => setCreateOpen(false)} />}
      {batchDeleteOpen && (
        <BatchDeleteUsersModal
          userIds={[...checked]}
          onClose={() => setBatchDeleteOpen(false)}
          onDone={() => {
            setChecked(new Set());
            void queryClient.invalidateQueries({ queryKey: ['admin-users'] });
          }}
        />
      )}
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

// ---------------- 注册码管理（admin） ----------------

const CODE_STATUS_LABEL: Record<string, [string, string, string]> = {
  active: ['有效', 'Active', 'var(--ok)'],
  exhausted: ['已用尽', 'Used up', 'var(--text-3)'],
  expired: ['已过期', 'Expired', 'var(--text-3)'],
  revoked: ['已停用', 'Revoked', 'var(--danger-tx)'],
};

function CreateCodeModal({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const [note, setNote] = useState('');
  const [expiresDays, setExpiresDays] = useState<string>(''); // '' = 永久
  const [maxUses, setMaxUses] = useState<string>(''); // '' = 不限
  const [directionsText, setDirectionsText] = useState('');

  const createMutation = useMutation({
    mutationFn: () =>
      api.adminCreateRegistrationCode({
        note: note.trim(),
        expires_days: expiresDays ? Number(expiresDays) : null,
        max_uses: maxUses ? Number(maxUses) : null,
        preset_directions: directionsText
          .split('\n')
          .map((s) => s.trim())
          .filter(Boolean)
          .slice(0, 10),
      }),
    onSuccess: (rc) => {
      void queryClient.invalidateQueries({ queryKey: ['admin-reg-codes'] });
      void navigator.clipboard?.writeText(rc.code).catch(() => {});
      toast(tr(`已生成 ${rc.code}，已复制到剪贴板`, `Created ${rc.code} — copied to clipboard`), 'ok');
      onClose();
    },
    onError: (e) => toast(`${tr('生成失败', 'Create failed')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  return (
    <Modal
      open
      onClose={onClose}
      width={440}
      title={tr('生成注册码', 'Generate registration code')}
      sub={tr('把生成的码发给需要注册的人。可设置有效期与使用次数上限。', 'Share the code with people who need to register. You can cap its lifetime and number of uses.')}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>{tr('取消', 'Cancel')}</button>
          <button className="btn btn-primary" disabled={createMutation.isPending} onClick={() => createMutation.mutate()}>
            {tr('生成', 'Generate')}
          </button>
        </>
      }
    >
      <FormField label={tr('备注', 'Note')} hint={tr('可选，例如「2026 级新生」', 'Optional, e.g. "2026 cohort"')}>
        <input className="input" value={note} maxLength={255} onChange={(e) => setNote(e.target.value)} placeholder={tr('这批码发给谁 / 什么用途', 'Who / what it is for')} />
      </FormField>
      <FormField label={tr('有效期', 'Expiry')} hint={tr('过期后自动失效', 'Auto-expires when reached')}>
        <SelectMenu
          value={expiresDays}
          options={[
            { value: '', label: tr('永久', 'Never') },
            { value: '7', label: tr('7 天', '7 days') },
            { value: '30', label: tr('30 天', '30 days') },
            { value: '90', label: tr('90 天', '90 days') },
            { value: '180', label: tr('180 天', '180 days') },
          ]}
          onChange={setExpiresDays}
        />
      </FormField>
      <FormField label={tr('使用次数上限', 'Max uses')} hint={tr('留空 = 不限。达到次数后自动失效', 'Empty = unlimited. Auto-expires once reached')}>
        <input
          className="input"
          type="number"
          min={1}
          max={10000}
          value={maxUses}
          onChange={(e) => setMaxUses(e.target.value)}
          placeholder={tr('不限', 'unlimited')}
        />
      </FormField>
      <FormField
        label={tr('预设课题', 'Preset topics')}
        hint={tr('可选，每行一个（最多 10 个）。用此码注册的同学会自动获得这些课题', 'Optional, one per line (max 10). New users registering with this code automatically get a topic for each line')}
      >
        <textarea
          className="input"
          rows={3}
          value={directionsText}
          onChange={(e) => setDirectionsText(e.target.value)}
          placeholder={tr('例如：大模型智能体的长程规划', 'e.g. Long-horizon planning for LLM agents')}
        />
      </FormField>
    </Modal>
  );
}

function CodesTab() {
  const queryClient = useQueryClient();
  const { data: codes, isLoading, isError } = useQuery({ queryKey: ['admin-reg-codes'], queryFn: () => api.adminListRegistrationCodes(), retry: false });
  const [createOpen, setCreateOpen] = useState(false);

  const revokeMutation = useMutation({
    mutationFn: (id: string) => api.adminRevokeRegistrationCode(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['admin-reg-codes'] });
      toast(tr('已停用', 'Revoked'), 'ok');
    },
    onError: (e) => toast(`${tr('停用失败', 'Revoke failed')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const copy = (code: string) => {
    void navigator.clipboard?.writeText(code).catch(() => {});
    toast(tr(`已复制 ${code}`, `Copied ${code}`), 'ok');
  };

  return (
    <div className="card card-pad">
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 16 }}>
        <div>
          <div className="section-h">{tr('注册码', 'Registration codes')}</div>
          <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 3 }}>
            {tr('注册时填对任一有效注册码即可加入。用尽 / 过期 / 停用即失效。', 'Anyone with a valid code can register. Codes stop working once used up, expired, or revoked.')}
          </div>
        </div>
        <button className="btn btn-primary" onClick={() => setCreateOpen(true)}>
          <Icon name="plus" size={14} />
          {tr('生成注册码', 'Generate code')}
        </button>
      </div>

      {isLoading ? (
        <div className="empty">{tr('加载中…', 'Loading…')}</div>
      ) : isError || !codes ? (
        <div className="empty">{tr('无法加载注册码', 'Failed to load codes')}</div>
      ) : codes.length === 0 ? (
        <div className="empty">{tr('还没有注册码，点右上角生成一个', 'No codes yet — generate one from the top right')}</div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table className="table">
            <thead>
              <tr>
                <th>{tr('注册码', 'Code')}</th>
                <th>{tr('备注', 'Note')}</th>
                <th>{tr('预设课题', 'Topics')}</th>
                <th>{tr('使用', 'Uses')}</th>
                <th>{tr('有效期', 'Expiry')}</th>
                <th>{tr('状态', 'Status')}</th>
                <th style={{ textAlign: 'right' }}>{tr('操作', 'Actions')}</th>
              </tr>
            </thead>
            <tbody>
              {codes.map((c: RegistrationCodeRead) => {
                const [zh, en, color] = CODE_STATUS_LABEL[c.status] ?? [c.status, c.status, 'var(--text-3)'];
                return (
                  <tr key={c.id}>
                    <td>
                      <button
                        className="btn btn-ghost sm"
                        style={{ fontFamily: 'var(--mono)', fontWeight: 600 }}
                        title={tr('点击复制', 'Click to copy')}
                        onClick={() => copy(c.code)}
                      >
                        {c.code} <Icon name="link" size={12} style={{ opacity: 0.6 }} />
                      </button>
                    </td>
                    <td style={{ color: c.note ? 'var(--text)' : 'var(--text-4)', maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {c.note || '—'}
                    </td>
                    <td
                      style={{ color: c.preset_directions.length ? 'var(--text)' : 'var(--text-4)', maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                      title={c.preset_directions.join('\n')}
                    >
                      {c.preset_directions.length ? c.preset_directions.join('；') : '—'}
                    </td>
                    <td style={{ fontFamily: 'var(--mono)' }}>
                      {c.used_count}
                      {c.max_uses != null ? ` / ${c.max_uses}` : tr(' / 不限', ' / ∞')}
                    </td>
                    <td style={{ fontSize: 12, color: 'var(--text-3)' }}>
                      {c.expires_at ? fmtTime(c.expires_at) : tr('永久', 'Never')}
                    </td>
                    <td>
                      <span className="pill" style={{ color, borderColor: 'var(--border)' }}>
                        <span className="dot" style={{ background: color }} />
                        {tr(zh, en)}
                      </span>
                    </td>
                    <td style={{ textAlign: 'right' }}>
                      {!c.revoked && (
                        <button
                          className="btn btn-ghost sm"
                          disabled={revokeMutation.isPending}
                          onClick={() => revokeMutation.mutate(c.id)}
                        >
                          {tr('停用', 'Revoke')}
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {createOpen && <CreateCodeModal onClose={() => setCreateOpen(false)} />}
    </div>
  );
}

const PERSONAL_TABS: Tab[] = ['personal', 'ssh', 'mymodels', 'myusage', 'mcp'];
const ALL_TABS: Tab[] = [...PERSONAL_TABS, 'llm', 'daily', 'usage', 'users', 'codes', 'feedback'];

export function SettingsPage() {
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: () => api.me(), retry: false });
  const admin = isAdmin(me);
  // 支持 /settings?tab=mcp 这类深链（如旧 /mcp-tools 路由的重定向）
  const [searchParams] = useSearchParams();
  const [tab, setTab] = useState<Tab>(() => {
    const t = searchParams.get('tab');
    return t !== null && ALL_TABS.includes(t as Tab) ? (t as Tab) : 'personal';
  });

  const personalItems: { v: Tab; label: string }[] = [
    { v: 'personal', label: tr('个人信息', 'Profile') },
    { v: 'ssh', label: tr('SSH 凭据', 'SSH credentials') },
    { v: 'mymodels', label: tr('我的模型', 'My LLM') },
    { v: 'myusage', label: tr('用量', 'Usage') },
    { v: 'mcp', label: tr('MCP 接入', 'MCP access') },
  ];
  const adminItems: { v: Tab; label: string }[] = [
    { v: 'llm', label: tr('LLM 管理', 'LLM admin') },
    { v: 'daily', label: tr('每日论文', 'Daily papers') },
    { v: 'users', label: tr('用户管理', 'Users') },
    { v: 'codes', label: tr('注册码', 'Codes') },
    { v: 'feedback', label: tr('反馈', 'Feedback') },
    { v: 'usage', label: tr('用量总览', 'Usage overview') },
  ];

  // 非 admin 只能停留在个人设置组
  const effectiveTab: Tab = !admin && !PERSONAL_TABS.includes(tab) ? 'personal' : tab;

  return (
    <div className="page fadeup">
      <PageHead eyebrow="Polaris · Settings" title={tr('设置', 'Settings')} />
      {/* 个人 tab 一块；管理员 tab 紧随其后另成一块，前面标「管理员设置」 */}
      <div className="row" style={{ gap: 12, marginBottom: 22, flexWrap: 'wrap', alignItems: 'center' }}>
        <Segmented options={personalItems} value={effectiveTab} onChange={setTab} />
        {admin && (
          <>
            <span
              style={{
                marginLeft: 8,
                fontSize: 10.5,
                fontWeight: 700,
                letterSpacing: '0.06em',
                textTransform: 'uppercase',
                color: 'var(--text-3)',
                flexShrink: 0,
              }}
            >
              {tr('管理员设置', 'Admin')}
            </span>
            <Segmented options={adminItems} value={effectiveTab} onChange={setTab} />
          </>
        )}
      </div>
      {effectiveTab === 'personal' && <PersonalTab />}
      {effectiveTab === 'ssh' && <SshTab />}
      {effectiveTab === 'mymodels' && <MyLlmTab />}
      {effectiveTab === 'myusage' && <MyUsageTab />}
      {effectiveTab === 'mcp' && <McpToolsContent />}
      {effectiveTab === 'llm' && admin && <LlmTab />}
      {effectiveTab === 'daily' && admin && <DailyCategoriesTab />}
      {effectiveTab === 'usage' && admin && <UsageTab />}
      {effectiveTab === 'users' && admin && <UsersTab />}
      {effectiveTab === 'codes' && admin && <CodesTab />}
      {effectiveTab === 'feedback' && admin && <FeedbackTab />}
    </div>
  );
}
