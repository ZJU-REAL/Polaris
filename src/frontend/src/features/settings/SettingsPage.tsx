import { Fragment, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { Navigate, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Avatar } from '../../components/ui/Avatar';
import { Icon } from '../../components/ui/Icon';
import { Segmented } from '../../components/ui/Segmented';
import { Switch } from '../../components/ui/Switch';
import { Modal } from '../../components/ui/Modal';
import { FormField } from '../../components/ui/FormField';
import { toast } from '../../components/ui/Toast';
import { fmtTime } from '../../lib/format';
import { SysinfoPanel } from '../../components/ui/SysinfoPanel';
import { McpToolsContent } from '../mcp/McpToolsPage';
import { AcademicIdentitySection } from './AcademicIdentitySection';
import { tr } from '../../lib/i18n';
import { stageLabel } from '../../lib/stageLabels';
import { setTaskLogHistory, useTaskLogHistory } from '../../lib/prefs';
import {
  ApiError,
  api,
  type AffiliationMode,
  type ChatBotPlatform,
  type DailySyncScope,
  type SshCredentialInput,
} from '../../lib/api';
import { BuddySettings } from './BuddySettings';
import { SettingsLayout, SettingsTabs } from './SettingsTabs';
import { SettingsActions, SettingsGroup, SettingsStack, SettingsRow, SettingsSection, StatusDot } from './settingsUi';
import { DailySubscriptionsSection } from './DailySubscriptionsSection';
import { ExtensionApiKeySettings } from './ExtensionApiKeySettings';
import { ModelSettings } from './ModelSettings';
import { FullExportSettings } from './FullExportSettings';
import { PluginsSettings } from './PluginsSettings';
import {
  CAPABILITY_PLUGINS_MANAGE,
  hasHost,
  isCapabilityAvailable,
  loadCapabilities,
} from '../../lib/host';
import { AboutSettings } from './AboutSettings';
import { AdminSpeechSettings, PersonalSpeechSettings } from './SpeechSettings';
// 原「管理」页的三块（#755）：入口合一后直接在同一页渲染
import { ExperimentSettings } from './ExperimentSettings';
import { LiteratureSearchSettingsPanel } from './LiteratureSearchSettings';
import { DocumentProcessingSettingsPanel } from './DocumentProcessingSettings';

/* ============================================================
   /settings — 全平台唯一的设置入口（#755）：个人信息 / 界面偏好 /
   PolarisBuddy / 语音 / 群机器人 / SSH 凭据 / 用量 / 扩展 / MCP 接入 /
   数据导出 / 插件，加上原「管理」页的几项：模型 / 文献检索 /
   文档处理 / 实验 / 每日论文 / 用量总览。

   曾经分成 /settings 与 /admin 两页，是实验室时代「管理员 vs 成员」的
   残留；平台面向个人之后两边是同一个人，找同一类配置却要猜在哪一页。
   /admin 现在只是一条指向这里的重定向（见 routes.tsx）。
   ============================================================ */

// ---------------- 个人 ----------------

function PersonalTab() {
  const queryClient = useQueryClient();
  const { data: me, isLoading, isError } = useQuery({ queryKey: ['me'], queryFn: () => api.me(), retry: false });
  const [name, setName] = useState('');
  const [avatarVersion, setAvatarVersion] = useState(0);
  const avatarInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (me) setName(me.display_name ?? '');
  }, [me]);

  const saveMutation = useMutation({
    mutationFn: () => api.updateMe({ display_name: name.trim() }),
    onSuccess: () => {
      toast(tr('已保存', 'Saved'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['me'] });
    },
    onError: (e) => toast(`${tr('保存失败', 'Couldn’t save')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  const avatarMutation = useMutation({
    mutationFn: (file: File) => api.uploadAvatar(file),
    onSuccess: () => {
      toast(tr('头像已更新', 'Photo updated'), 'ok');
      setAvatarVersion((v) => v + 1);
      void queryClient.invalidateQueries({ queryKey: ['me'] });
      void queryClient.invalidateQueries({ queryKey: ['avatar'] });
    },
    onError: (e) => {
      const msg = e instanceof Error ? e.message : String(e);
      toast(
        msg === 'AVATAR_TOO_LARGE'
          ? tr('图片不能超过 2 MB', 'The image must be 2 MB or smaller')
          : msg === 'AVATAR_NOT_IMAGE'
            ? tr('请选择 PNG、JPEG 或 WebP 图片', 'Choose a PNG, JPEG or WebP image')
            : `${tr('上传失败', 'Upload failed')}：${msg}`,
        'error',
      );
    },
  });

  if (isLoading) return <div className="empty">{tr('加载中…', 'Loading…')}</div>;
  if (isError || !me) return <div className="empty">{tr('无法加载个人资料，请确认本机引擎正在运行', 'Couldn’t load your profile. Check that the local engine is running.')}</div>;

  return (
    <SettingsStack>
      <SettingsSection title={tr('个人资料', 'Profile')}>
        <SettingsGroup>
          <SettingsRow label={tr('头像', 'Photo')} hint={tr('PNG、JPEG 或 WebP，不超过 2 MB', 'PNG, JPEG or WebP, up to 2 MB')}>
            <Avatar userId={me.id} hasAvatar={!!me.has_avatar} name={me.display_name || ''} size={36} version={avatarVersion} />
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
            <button className="btn btn-ghost sm" disabled={avatarMutation.isPending} onClick={() => avatarInputRef.current?.click()}>
              {avatarMutation.isPending ? tr('上传中…', 'Uploading…') : tr('更换', 'Change')}
            </button>
          </SettingsRow>
          <SettingsRow label={tr('姓名', 'Name')}>
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder={tr('例如 张三', 'e.g. Jane Smith')} />
            <button
              className="btn btn-primary sm"
              disabled={saveMutation.isPending || (me.display_name ?? '') === name.trim()}
              onClick={() => saveMutation.mutate()}
            >
              {saveMutation.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
            </button>
          </SettingsRow>
        </SettingsGroup>
      </SettingsSection>

      <AcademicIdentitySection />
    </SettingsStack>
  );
}

// ---------------- 界面偏好（本地，存 localStorage） ----------------

function PreferencesTab() {
  const showHistory = useTaskLogHistory();
  const queryClient = useQueryClient();
  const watchdog = useQuery({
    queryKey: ['managed-command-watchdog', 'user'],
    queryFn: () => api.getMyManagedCommandWatchdog(),
    retry: false,
  });
  const [watchdogMinutes, setWatchdogMinutes] = useState<number | null>(null);
  const shownMinutes = watchdogMinutes ?? watchdog.data?.unanswered_minutes ?? 120;
  const saveWatchdog = useMutation({
    mutationFn: () => api.setMyManagedCommandWatchdog(shownMinutes),
    onSuccess: (saved) => {
      setWatchdogMinutes(saved.unanswered_minutes);
      queryClient.setQueryData(['managed-command-watchdog', 'user'], saved);
      toast(tr('已保存', 'Saved'), 'ok');
    },
    onError: (error) => toast(
      `${tr('保存失败', 'Couldn’t save')}：${error instanceof Error ? error.message : String(error)}`,
      'error',
    ),
  });
  return (
    <SettingsStack>
      <SettingsSection title={tr('界面', 'Interface')}>
        <SettingsGroup>
          <SettingsRow
            labelId="pref-task-log-history"
            label={tr('任务终端显示历史日志', 'Show earlier logs in the task terminal')}
            hint={tr('关闭后只显示本次运行的日志', 'When off, only this run’s logs are shown')}
          >
            <Switch checked={showHistory} onChange={setTaskLogHistory} aria-labelledby="pref-task-log-history" />
          </SettingsRow>
        </SettingsGroup>
      </SettingsSection>

      <SettingsSection title={tr('远程命令超时', 'Remote command timeout')}>
        <SettingsGroup>
          <SettingsRow
            label={tr('等待回复（分钟）', 'Wait for reply (minutes)')}
            hint={watchdog.isError
              ? tr('无法加载此设置', 'Couldn’t load this setting')
              : watchdog.data && watchdog.data.effective_unanswered_minutes < watchdog.data.unanswered_minutes
                ? tr(`上限为 ${watchdog.data.effective_unanswered_minutes} 分钟`, `Capped at ${watchdog.data.effective_unanswered_minutes} minutes`)
                : tr('超时后仍占用 GPU 的命令会被终止', 'Commands still using a GPU are then stopped')}
          >
            <input
              className="input mono st-num"
              type="number"
              min={15}
              max={10080}
              value={shownMinutes}
              disabled={watchdog.isError}
              onChange={(event) => setWatchdogMinutes(Number(event.target.value))}
            />
            <button
              className="btn btn-primary sm"
              disabled={watchdog.isLoading || shownMinutes < 15 || shownMinutes > 10080 || saveWatchdog.isPending || shownMinutes === watchdog.data?.unanswered_minutes}
              onClick={() => saveWatchdog.mutate()}
            >
              {saveWatchdog.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
            </button>
          </SettingsRow>
        </SettingsGroup>
      </SettingsSection>
    </SettingsStack>
  );
}

// ---------------- 群机器人（单向 Webhook 推送） ----------------

const CHAT_BOT_PLATFORMS: ChatBotPlatform[] = ['dingtalk', 'feishu'];
const CHAT_BOT_META: Record<ChatBotPlatform, {
  zh: string;
  en: string;
  idZh: string;
  idEn: string;
  placeholder: string;
  docs: string;
}> = {
  dingtalk: {
    zh: '钉钉机器人',
    en: 'DingTalk bot',
    idZh: '机器人 ID / Webhook',
    idEn: 'Bot ID / Webhook',
    placeholder: 'https://oapi.dingtalk.com/robot/send?access_token=…',
    docs: 'https://open.dingtalk.com/document/orgapp/custom-robot-access',
  },
  feishu: {
    zh: '飞书机器人',
    en: 'Feishu bot',
    idZh: '机器人 ID / Webhook',
    idEn: 'Bot ID / Webhook',
    placeholder: 'https://open.feishu.cn/open-apis/bot/v2/hook/…',
    docs: 'https://open.feishu.cn/document/ukTMukTMukTM/ucTM5YjL3ETO24yNxkjN',
  },
};

interface ChatBotDraft {
  robot_id: string;
  secret: string;
}

const EMPTY_CHAT_BOT_DRAFTS: Record<ChatBotPlatform, ChatBotDraft> = {
  dingtalk: { robot_id: '', secret: '' },
  feishu: { robot_id: '', secret: '' },
};

function chatBotError(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.message === 'INVALID_CHAT_BOT_ID') {
      return tr('Webhook 格式不对，请粘贴钉钉或飞书提供的完整地址', 'That Webhook isn’t valid. Paste the full URL from DingTalk or Feishu.');
    }
    if (e.message === 'CHAT_BOT_NOT_CONFIGURED') {
      return tr('还没有设置这个机器人', 'This bot isn’t set up yet');
    }
    if (e.message.startsWith('CHAT_BOT_DELIVERY_FAILED')) {
      return tr('发送失败，请检查 Webhook、签名密钥和群的安全设置', 'Couldn’t send. Check the Webhook, signing secret and the group’s security settings.');
    }
  }
  return e instanceof Error ? e.message : String(e);
}

function ChatBotsTab() {
  const queryClient = useQueryClient();
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['chat-bots'],
    queryFn: () => api.listChatBotConfigs(),
    retry: false,
  });
  const [drafts, setDrafts] = useState<Record<ChatBotPlatform, ChatBotDraft>>(
    EMPTY_CHAT_BOT_DRAFTS,
  );

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ['chat-bots'] });
  const saveMutation = useMutation({
    mutationFn: (platform: ChatBotPlatform) => {
      const draft = drafts[platform];
      return api.saveChatBotConfig(platform, {
        robot_id: draft.robot_id.trim(),
        ...(draft.secret.trim() ? { secret: draft.secret.trim() } : {}),
      });
    },
    onSuccess: (_, platform) => {
      setDrafts((current) => ({ ...current, [platform]: { robot_id: '', secret: '' } }));
      invalidate();
      toast(tr('已保存', 'Saved'), 'ok');
    },
    onError: (e) => toast(`${tr('保存失败', 'Couldn’t save')}：${chatBotError(e)}`, 'error'),
  });
  const testMutation = useMutation({
    mutationFn: (platform: ChatBotPlatform) => api.testChatBotConfig(platform),
    onSuccess: () => {
      invalidate();
      toast(tr('测试消息已发送', 'Test message sent'), 'ok');
    },
    onError: (e) => toast(chatBotError(e), 'error'),
  });
  const deleteMutation = useMutation({
    mutationFn: (platform: ChatBotPlatform) => api.deleteChatBotConfig(platform),
    onSuccess: () => {
      invalidate();
      toast(tr('已删除', 'Deleted'), 'ok');
    },
    onError: (e) => toast(`${tr('删除失败', 'Couldn’t delete')}：${chatBotError(e)}`, 'error'),
  });

  if (isLoading) return <div className="empty">{tr('加载中…', 'Loading…')}</div>;
  if (isError || !data) {
    return (
      <div className="empty">
        {tr('无法加载群机器人', 'Couldn’t load group bots')}
        <div style={{ marginTop: 10 }}>
          <button className="btn btn-soft sm" onClick={() => void refetch()}>{tr('重试', 'Retry')}</button>
        </div>
      </div>
    );
  }

  return (
    <SettingsStack>
      {CHAT_BOT_PLATFORMS.map((platform, index) => {
        const meta = CHAT_BOT_META[platform];
        const config = data.find((item) => item.platform === platform);
        const draft = drafts[platform];
        const busy =
          (saveMutation.isPending && saveMutation.variables === platform) ||
          (testMutation.isPending && testMutation.variables === platform) ||
          (deleteMutation.isPending && deleteMutation.variables === platform);
        return (
          <SettingsSection
            key={platform}
            title={tr(meta.zh, meta.en)}
            desc={index === 0
              ? tr(
                  '在群里添加自定义机器人并填到这里，之后在对话中 @ 它即可把回答发到群里。',
                  'Add a custom bot to the group and enter it here. Mention it with @ in a chat to send the answer there.',
                )
              : undefined}
            actions={
              <>
                <StatusDot tone={config?.configured ? 'ok' : 'idle'}>
                  {config?.configured ? tr('已设置', 'Set up') : tr('未设置', 'Not set up')}
                </StatusDot>
                <a href={meta.docs} target="_blank" rel="noreferrer noopener" className="btn btn-ghost sm">
                  {tr('设置说明', 'Setup guide')}
                </a>
              </>
            }
          >
            <SettingsGroup>
              <SettingsRow stack label="Webhook" hint={tr('粘贴完整的 Webhook 地址', 'Paste the full Webhook URL')}>
                <input
                  className="input mono"
                  value={draft.robot_id}
                  onChange={(e) => setDrafts((current) => ({
                    ...current,
                    [platform]: { ...current[platform], robot_id: e.target.value },
                  }))}
                  placeholder={config?.configured ? tr('已保存，填写新值即可替换', 'Saved. Enter a new value to replace it.') : meta.placeholder}
                  autoComplete="off"
                  spellCheck={false}
                />
              </SettingsRow>
              <SettingsRow
                label={tr('签名密钥', 'Signing secret')}
                hint={config?.configured && !config.has_secret
                  ? tr('建议在机器人安全设置中开启加签', 'Turn on signing in the bot’s security settings')
                  : tr('机器人未开启加签时可留空', 'Leave empty if signing is off')}
              >
                <input
                  className="input mono"
                  type="password"
                  value={draft.secret}
                  onChange={(e) => setDrafts((current) => ({
                    ...current,
                    [platform]: { ...current[platform], secret: e.target.value },
                  }))}
                  placeholder={config?.has_secret ? tr('已保存', 'Saved') : tr('可选', 'Optional')}
                  autoComplete="new-password"
                />
              </SettingsRow>
            </SettingsGroup>
            <SettingsActions
              note={config?.last_delivered_at
                ? tr(`上次发送 ${fmtTime(config.last_delivered_at)}`, `Last sent ${fmtTime(config.last_delivered_at)}`)
                : undefined}
            >
              {config?.configured && (
                <>
                  <button
                    className="btn btn-ghost sm st-quiet-danger"
                    disabled={busy}
                    onClick={() => {
                      if (window.confirm(tr(`删除${meta.zh}？之后无法再向这个群发送消息。`, `Delete the ${meta.en}? Polaris will stop sending to that group.`))) {
                        deleteMutation.mutate(platform);
                      }
                    }}
                  >
                    {tr('删除', 'Delete')}
                  </button>
                  <button
                    className="btn btn-ghost sm"
                    disabled={busy}
                    title={tr('向群里发送一条测试消息', 'Sends a test message to the group')}
                    onClick={() => testMutation.mutate(platform)}
                  >
                    {testMutation.isPending && testMutation.variables === platform ? tr('发送中…', 'Sending…') : tr('发送测试消息', 'Send test message')}
                  </button>
                </>
              )}
              <button
                className="btn btn-primary sm"
                disabled={busy || draft.robot_id.trim() === ''}
                onClick={() => saveMutation.mutate(platform)}
              >
                {saveMutation.isPending && saveMutation.variables === platform ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
              </button>
            </SettingsActions>
          </SettingsSection>
        );
      })}
    </SettingsStack>
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
      toast(tr('已添加服务器', 'Server added'), 'ok');
      setModalOpen(false);
      invalidate();
    },
    onError: (e) => toast(`${tr('添加失败', 'Couldn’t add')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteSshCredential(id),
    onSuccess: () => {
      toast(tr('已删除', 'Deleted'), 'ok');
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
    onError: (e) => toast(`${tr('连接失败', 'Connection failed')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const canSave =
    draft.name.trim() !== '' &&
    draft.host.trim() !== '' &&
    draft.username.trim() !== '' &&
    draft.private_key.trim() !== '' &&
    !createMutation.isPending;

  return (
    <SettingsStack>
    <SettingsSection
      title={tr('远程服务器', 'Remote servers')}
      desc={tr('实验通过 SSH 在这些服务器上运行。', 'Experiments run on these servers over SSH.')}
      actions={
        <button className="btn btn-primary sm" onClick={() => { setDraft(emptySshDraft()); setModalOpen(true); }}>
          <Icon name="plus" size={13} />
          {tr('添加服务器', 'Add server')}
        </button>
      }
    >
      {isLoading ? (
        <SettingsGroup pad><div className="st-row-hint">{tr('加载中…', 'Loading…')}</div></SettingsGroup>
      ) : isError ? (
        <SettingsGroup>
          <SettingsRow label={tr('无法加载远程服务器', 'Couldn’t load remote servers')}>
            <button className="btn btn-ghost sm" onClick={() => void refetch()}>{tr('重试', 'Retry')}</button>
          </SettingsRow>
        </SettingsGroup>
      ) : creds.length === 0 ? (
        <SettingsGroup>
          <SettingsRow label={tr('还没有远程服务器', 'No remote servers yet')} hint={tr('添加一台 GPU 服务器即可运行实验', 'Add a GPU server to run experiments')} />
        </SettingsGroup>
      ) : (
        <SettingsGroup>
              {creds.map((c) => (
                <Fragment key={c.id}>
                  <SettingsRow
                    label={<span className="row gap8" style={{ alignItems: 'center', flexWrap: 'wrap' }}>
                      {c.name}
                      {c.last_verified_at ? (
                        <StatusDot tone="ok" title={fmtTime(c.last_verified_at)}>{tr('连接正常', 'Connected')}</StatusDot>
                      ) : (
                        <StatusDot tone="idle">{tr('未测试', 'Not tested')}</StatusDot>
                      )}
                    </span>}
                    hint={<span className="mono" style={{ overflowWrap: 'anywhere' }}>{`${c.username}@${c.host}${c.port && c.port !== 22 ? `:${c.port}` : ''}`}</span>}
                  >
                        <button
                          className="btn btn-ghost sm"
                          onClick={() => setSysinfoId(sysinfoId === c.id ? null : c.id)}
                        >
                          {sysinfoId === c.id ? tr('收起', 'Hide') : tr('查看状态', 'Status')}
                        </button>
                        <button
                          className="btn btn-ghost sm"
                          disabled={testMutation.isPending}
                          onClick={() => testMutation.mutate(c.id)}
                        >
                          {testMutation.isPending && testMutation.variables === c.id ? tr('连接中…', 'Connecting…') : tr('测试连接', 'Test connection')}
                        </button>
                        <button
                          className="icon-btn st-quiet-danger"
                          style={{ width: 26, height: 26 }}
                          title={tr('删除', 'Delete')}
                          disabled={deleteMutation.isPending}
                          onClick={() => {
                            if (window.confirm(tr(`删除服务器「${c.name}」？用到它的实验将无法再连接。`, `Delete server “${c.name}”? Experiments that use it can no longer connect.`))) {
                              deleteMutation.mutate(c.id);
                            }
                          }}
                        >
                          <Icon name="trash" size={13} />
                        </button>
                  </SettingsRow>
                  {sysinfoId === c.id && (
                    <div style={{ background: 'var(--surface-2)', padding: '12px 14px' }}>
                      <SysinfoPanel
                        loading={sysinfoQuery.isLoading}
                        error={sysinfoQuery.isError}
                        info={sysinfoQuery.data}
                        onRefresh={() => void sysinfoQuery.refetch()}
                      />
                    </div>
                  )}
                </Fragment>
              ))}
        </SettingsGroup>
      )}
    </SettingsSection>

      <Modal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        width={560}
        title={tr('添加远程服务器', 'Add remote server')}
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
            placeholder={tr('例如 lab-gpu-1', 'e.g. lab-gpu-1')} />
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
        <FormField label={tr('私钥', 'Private key')} hint={tr('粘贴完整的私钥文本', 'Paste the whole private key')}>
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
        <FormField label={tr('私钥密码（可选）', 'Passphrase (optional)')}>
          <input className="input mono" type="password" autoComplete="new-password" value={draft.passphrase}
            onChange={(e) => setDraft({ ...draft, passphrase: e.target.value })} placeholder={tr('私钥没有密码则留空', 'Leave empty if the key has none')} />
        </FormField>
        <FormField
          label={tr('外网代理（可选）', 'Internet proxy (optional)')}
          hint={tr('服务器安装依赖、下载模型时使用；能直接上网则留空', 'Used to install packages and download models. Leave empty if the server has direct access.')}
        >
          <input className="input mono" value={draft.proxy_url}
            onChange={(e) => setDraft({ ...draft, proxy_url: e.target.value })}
            placeholder={tr('例如 http://10.0.0.1:7890', 'e.g. http://10.0.0.1:7890')} />
        </FormField>
      </Modal>
    </SettingsStack>
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
  if (isError || !data) return <div className="empty" style={{ padding: 16 }}>{tr('无法加载详情', 'Couldn’t load details')}</div>;

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
                {tr('图片（未保存原图）', 'Images (not saved)')}：{images.join(' ')}
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
      toast(r.enabled ? tr('已开始记录', 'Logging on') : tr('已停止记录', 'Logging off'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['llm', 'call-log-settings'] });
    },
    onError: (e) => toast(`${tr('保存失败', 'Couldn’t save')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  const clearMutation = useMutation({
    mutationFn: () => api.clearLlmCallLogs(),
    onSuccess: (r) => {
      toast(tr(`已清空 ${r.deleted} 条记录`, r.deleted === 1 ? 'Cleared 1 entry' : `Cleared ${r.deleted} entries`), 'ok');
      setExpandedId(null);
      setPage(0);
      void queryClient.invalidateQueries({ queryKey: ['llm', 'call-logs'] });
    },
    onError: (e) => toast(`${tr('清空失败', 'Couldn’t clear')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  return (
    <SettingsSection
      title={tr('调用记录', 'Call log')}
      actions={
        <>
          <button
            className="btn btn-ghost sm st-quiet-danger"
            disabled={clearMutation.isPending || total === 0}
            onClick={() => {
              if (window.confirm(tr('清空全部调用记录？清空后无法恢复。', 'Clear the whole call log? This can’t be undone.'))) {
                clearMutation.mutate();
              }
            }}
          >
            {tr('清空', 'Clear')}
          </button>
        </>
      }
    >
      <SettingsGroup>
        <SettingsRow
          labelId="call-log-enabled"
          label={tr('记录模型调用', 'Log model calls')}
          hint={tr('保存每次调用的输入和输出，保留 7 天', 'Keeps each call’s input and output for 7 days')}
        >
          <Switch
            checked={enabled}
            disabled={settingsQuery.isLoading || toggleMutation.isPending}
            onChange={(next) => toggleMutation.mutate(next)}
            aria-labelledby="call-log-enabled"
          />
        </SettingsRow>
      {logsQuery.isLoading ? (
        <div className="st-row"><span className="st-row-hint">{tr('加载中…', 'Loading…')}</span></div>
      ) : logsQuery.isError ? (
        <SettingsRow label={tr('无法加载调用记录', 'Couldn’t load the call log')}>
          <button className="btn btn-ghost sm" onClick={() => void logsQuery.refetch()}>{tr('重试', 'Retry')}</button>
        </SettingsRow>
      ) : items.length === 0 ? (
        <div className="st-row">
          <span className="st-row-hint">
            {enabled
              ? tr('还没有调用记录', 'No calls logged yet')
              : tr('记录已关闭', 'Logging is off')}
          </span>
        </div>
      ) : (
        <div>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th style={{ width: 140 }}>{tr('时间', 'Time')}</th>
                  <th style={{ width: 110 }}>{tr('环节', 'Stage')}</th>
                  <th>{tr('模型', 'Model')}</th>
                  <th style={{ width: 90, textAlign: 'right' }}>{tr('耗时', 'Time')} (ms)</th>
                  <th style={{ width: 120, textAlign: 'right' }}>tokens</th>
                  <th style={{ width: 70 }}>{tr('状态', 'Status')}</th>
                </tr>
              </thead>
              <tbody>
                {items.map((row) => (
                  <Fragment key={row.id}>
                    <tr style={{ cursor: 'pointer' }} onClick={() => setExpandedId(expandedId === row.id ? null : row.id)}>
                      <td className="mono" style={{ fontSize: 12, whiteSpace: 'nowrap' }}>{fmtTime(row.created_at)}</td>
                      <td style={{ fontSize: 12, whiteSpace: 'nowrap' }} title={row.stage}>{tr(stageLabel(row.stage).zh, stageLabel(row.stage).en)}</td>
                      <td className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)' }}>
                        {row.model}
                        <span style={{ color: 'var(--text-4)' }}> · {row.provider_name}</span>
                      </td>
                      <td className="mono" style={{ fontSize: 11.5, textAlign: 'right' }}>{row.duration_ms.toLocaleString()}</td>
                      <td className="mono" style={{ fontSize: 11.5, textAlign: 'right' }}>
                        {row.prompt_tokens.toLocaleString()} + {row.completion_tokens.toLocaleString()}
                      </td>
                      <td>
                        <StatusDot tone={row.status === 'ok' ? 'ok' : 'err'}>{row.status === 'ok' ? tr('成功', 'OK') : tr('失败', 'Failed')}</StatusDot>
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
          </div>
          <div className="row gap8" style={{ justifyContent: 'flex-end', padding: '10px 14px', alignItems: 'center' }}>
            <span style={{ fontSize: 12, color: 'var(--text-3)' }}>
              {tr(`共 ${total} 条 · 第 ${page + 1}/${pageCount} 页`, `${total} entries · Page ${page + 1} of ${pageCount}`)}
            </span>
            <button className="btn btn-soft sm" disabled={page === 0} onClick={() => { setExpandedId(null); setPage(page - 1); }}>
              {tr('上一页', 'Previous')}
            </button>
            <button className="btn btn-soft sm" disabled={page + 1 >= pageCount} onClick={() => { setExpandedId(null); setPage(page + 1); }}>
              {tr('下一页', 'Next')}
            </button>
          </div>
        </div>
      )}
      </SettingsGroup>
    </SettingsSection>
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
      toast(tr('已保存', 'Saved'), 'ok');
      queryClient.setQueryData(['affiliation-mode'], r);
    },
    onError: (e) => toast(`${tr('保存失败', 'Couldn’t save')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  return (
    <SettingsSection title={tr('作者机构', 'Author affiliations')}>
      <SettingsGroup>
        <SettingsRow
          label={tr('识别时机', 'When to detect')}
          hint={isError
            ? tr('无法加载此设置', 'Couldn’t load this setting')
            : tr('从论文首页识别作者机构，有 DOI 的论文不受影响', 'Reads affiliations from the first page. Papers with a DOI aren’t affected.')}
        >
      {isLoading || isError ? null : (
        <Segmented
          options={[
            { v: 'on_add' as AffiliationMode, label: tr('添加论文时', 'When adding') },
            {
              v: 'on_compile' as AffiliationMode,
              label: tr('生成解读时', 'With the summary'),
            },
          ]}
          value={mode}
          onChange={(v) => { if (v !== mode) setMutation.mutate(v); }}
        />
      )}
        </SettingsRow>
      </SettingsGroup>
    </SettingsSection>
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

  return (
    <SettingsStack>
      <SettingsSection
        title={tr('用量', 'Usage')}
        actions={
          <Segmented options={[{ v: '7' as const, label: tr('7 天', '7 days') }, { v: '30' as const, label: tr('30 天', '30 days') }, { v: '90' as const, label: tr('90 天', '90 days') }]}
            value={days} onChange={setDays} />
        }
      >
        <SettingsGroup pad>
          <div className="settings-stats">
            {[
              { zh: '累计', en: 'All time', v: used, unit: 'tokens' },
              { zh: `近 ${days} 天输入`, en: `Input, last ${days} days`, v: totals.prompt, unit: 'tokens' },
              { zh: `近 ${days} 天输出`, en: `Output, last ${days} days`, v: totals.completion, unit: 'tokens' },
              { zh: `近 ${days} 天调用`, en: `Calls, last ${days} days`, v: totals.calls, unit: tr('次', 'calls') },
            ].map((s) => (
              <div key={s.en}>
                <div style={{ fontSize: 12, color: 'var(--text-3)' }}>{tr(s.zh, s.en)}</div>
                <div className="mono" style={{ fontSize: 20, fontWeight: 600, marginTop: 4, fontVariantNumeric: 'tabular-nums' }}>
                  {s.v.toLocaleString()}
                  <span style={{ fontSize: 12, fontWeight: 400, color: 'var(--text-3)', marginLeft: 4 }}>{s.unit}</span>
                </div>
              </div>
            ))}
          </div>
        </SettingsGroup>
      </SettingsSection>

      <SettingsSection title={tr('每日明细', 'By day')}>
        <SettingsGroup>
        {isLoading ? (
          <div className="st-row"><span className="st-row-hint">{tr('加载中…', 'Loading…')}</span></div>
        ) : isError ? (
          <SettingsRow label={tr('无法加载用量', 'Couldn’t load usage')}>
            <button className="btn btn-ghost sm" onClick={() => void refetch()}>{tr('重试', 'Retry')}</button>
          </SettingsRow>
        ) : rows.length === 0 ? (
          <div className="st-row"><span className="st-row-hint">{tr(`近 ${days} 天没有用量`, `No usage in the last ${days} days`)}</span></div>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>{tr('日期', 'Date')}</th>
                  <th>{tr('环节', 'Stage')}</th>
                  <th>{tr('模型', 'Model')}</th>
                  <th style={{ textAlign: 'right' }}>{tr('输入 tokens', 'Input tokens')}</th>
                  <th style={{ textAlign: 'right' }}>{tr('输出 tokens', 'Output tokens')}</th>
                  <th style={{ textAlign: 'right' }}>{tr('调用次数', 'Calls')}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={i}>
                    <td className="mono" style={{ fontSize: 12, whiteSpace: 'nowrap' }}>{r.date}</td>
                    <td style={{ fontSize: 12, whiteSpace: 'nowrap' }} title={r.stage}>{tr(stageLabel(r.stage).zh, stageLabel(r.stage).en)}</td>
                    <td className="mono" style={{ fontSize: 12, color: 'var(--text-3)', whiteSpace: 'nowrap' }}>{r.model}</td>
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
          </div>
        )}
        </SettingsGroup>
      </SettingsSection>

      <CallLogsSection />
    </SettingsStack>
  );
}

// ---------------- 页面 ----------------

/** 设置页的标签页。原「管理」那六项自 #755 起也在这里。 */
type Tab =
  | 'personal' | 'prefs' | 'buddy' | 'speech' | 'bots' | 'ssh' | 'myusage'
  | 'extension' | 'mcp' | 'export' | 'plugins' | 'about'
  // 原 /admin 的六项（#755）：平台只剩一个使用者，另开一个「管理」入口只是
  // 实验室时代的残留——同一个人要在两个页面之间找同一类配置
  // 'model' 是只读的模型状态；旧的 ?tab=llm / ?tab=agents / ?tab=mymodels 都落到这里
  | 'model' | 'literature' | 'processing' | 'experiment' | 'daily' | 'usage';

/** 给最近 7 天缺向量的每日论文补建向量（新论文同步时已自动建，这里只补历史）。 */
function DailyEmbedSection() {
  const backfillMutation = useMutation({
    mutationFn: () => api.backfillDailyEmbeddings(),
    onSuccess: (r) => {
      const base = tr(`已为 ${r.embedded} 篇建立索引`, r.embedded === 1 ? 'Indexed 1 paper' : `Indexed ${r.embedded} papers`);
      if (r.failed > 0) {
        toast(`${base}${tr(`，${r.failed} 篇失败`, `. ${r.failed} failed.`)}`, 'info');
      } else {
        toast(base, 'ok');
      }
    },
    onError: (e) => toast(`${tr('建立索引失败', 'Couldn’t index')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  return (
    <SettingsSection title={tr('索引', 'Index')}>
      <SettingsGroup>
      <SettingsRow
        label={tr('补建索引', 'Index older papers')}
        hint={tr('新论文会自动建立索引，较早的论文可在这里补建', 'New papers are indexed automatically')}
      >
        <button
          className="btn btn-ghost sm"
          disabled={backfillMutation.isPending}
          title={tr('为近 7 天未建索引的论文建立索引，约需几十秒', 'Indexes papers from the last 7 days. Takes about a minute.')}
          onClick={() => backfillMutation.mutate()}
        >
          <Icon
            name="refresh"
            size={12}
            style={backfillMutation.isPending ? { animation: 'spin 1s linear infinite' } : undefined}
          />
          {backfillMutation.isPending ? tr('正在建立索引…', 'Indexing…') : tr('开始', 'Start')}
        </button>
      </SettingsRow>
      </SettingsGroup>
    </SettingsSection>
  );
}

/** 抓取与同步：抓取时刻、池子保留期、库同步每次扫多大范围。 */
function DailySyncSection() {
  const queryClient = useQueryClient();
  const scopeQuery = useQuery({
    queryKey: ['daily-sync-scope'],
    queryFn: () => api.getDailySyncScope(),
    retry: false,
  });
  const timeQuery = useQuery({
    queryKey: ['daily-sync-time'],
    queryFn: () => api.getDailySyncTime(),
    retry: false,
  });
  const retentionQuery = useQuery({
    queryKey: ['daily-retention'],
    queryFn: () => api.getDailyRetention(),
    retry: false,
  });
  const probeQuery = useQuery({
    queryKey: ['daily-probe-attempts'],
    queryFn: () => api.getDailyProbeAttempts(),
    retry: false,
  });

  const [days, setDays] = useState('');
  const [clock, setClock] = useState('');
  const [probes, setProbes] = useState('');
  useEffect(() => {
    if (retentionQuery.data && !days) setDays(String(retentionQuery.data.days));
  }, [retentionQuery.data, days]);
  useEffect(() => {
    if (probeQuery.data && !probes) setProbes(String(probeQuery.data.attempts));
  }, [probeQuery.data, probes]);
  useEffect(() => {
    if (timeQuery.data && !clock) {
      const { hour, minute } = timeQuery.data;
      setClock(`${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`);
    }
  }, [timeQuery.data, clock]);

  const fail = (e: unknown) =>
    toast(`${tr('保存失败', 'Couldn’t save')}：${e instanceof Error ? e.message : String(e)}`, 'error');

  const scopeMutation = useMutation({
    mutationFn: (scope: DailySyncScope) => api.setDailySyncScope(scope),
    onSuccess: () => {
      toast(tr('已保存', 'Saved'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['daily-sync-scope'] });
    },
    onError: fail,
  });
  const timeMutation = useMutation({
    mutationFn: () => {
      const [h, m] = clock.split(':');
      return api.setDailySyncTime(Number(h), Number(m));
    },
    onSuccess: () => {
      toast(tr('已保存', 'Saved'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['daily-sync-time'] });
    },
    onError: fail,
  });
  const probeMutation = useMutation({
    mutationFn: () => api.setDailyProbeAttempts(Number(probes)),
    onSuccess: () => {
      toast(tr('已保存', 'Saved'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['daily-probe-attempts'] });
    },
    onError: fail,
  });
  const retentionMutation = useMutation({
    mutationFn: () => api.setDailyRetention(Number(days)),
    onSuccess: () => {
      toast(tr('已保存', 'Saved'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['daily-retention'] });
    },
    onError: fail,
  });

  const scope = scopeQuery.data?.scope ?? 'since_last';
  const SCOPES: { v: DailySyncScope; zh: string; en: string; noteZh: string; noteEn: string }[] = [
    {
      v: 'since_last',
      zh: '上次同步以来',
      en: 'Since last sync',
      noteZh: '通常只看当天，漏掉的日子会补上。',
      noteEn: 'Usually just today. Missed days are caught up.',
    },
    {
      v: 'daily',
      zh: '只看当天',
      en: 'Today only',
      noteZh: '用量最少，但漏掉的日子不会补。',
      noteEn: 'Uses the least, but missed days aren’t caught up.',
    },
    {
      v: 'full',
      zh: '全部每日论文',
      en: 'All daily papers',
      noteZh: '最完整，但每次都会重新筛选所有论文，用量较大。',
      noteEn: 'Most complete, but rechecks every paper each time and uses more.',
    },
  ];
  const current = SCOPES.find((x) => x.v === scope);

  return (
    <SettingsSection
      title={tr('获取与同步', 'Fetch and sync')}
      desc={tr('每天获取新论文后，各文献库会随即同步。', 'Libraries sync right after new papers are fetched each day.')}
    >
      <SettingsGroup>
        <SettingsRow label={tr('同步范围', 'Sync scope')} hint={current ? tr(current.noteZh, current.noteEn) : undefined}>
          <select
            className="input"
            value={scope}
            disabled={scopeQuery.isLoading || scopeMutation.isPending}
            onChange={(e) => scopeMutation.mutate(e.target.value as DailySyncScope)}
          >
            {SCOPES.map((x) => (
              <option key={x.v} value={x.v}>
                {tr(x.zh, x.en)}
              </option>
            ))}
          </select>
        </SettingsRow>

        <SettingsRow
          label={tr('获取时间（UTC）', 'Fetch time (UTC)')}
          hint={tr('北京时间减 8 小时；之后每 15 分钟检查一次更新', 'arXiv is then checked every 15 minutes')}
        >
          <input
            className="input mono st-num"
            type="time"
            style={{ width: 140 }}
            value={clock}
            onChange={(e) => setClock(e.target.value)}
          />
          <button
            className="btn btn-ghost sm"
            disabled={!/^\d{2}:\d{2}$/.test(clock) || timeMutation.isPending}
            onClick={() => timeMutation.mutate()}
          >
            {tr('保存', 'Save')}
          </button>
        </SettingsRow>

        <SettingsRow
          label={tr('每天最多检查次数', 'Checks per day')}
          hint={tr('用完仍无更新，当天就不再检查；默认 10 次', 'Stops for the day after this many. Default 10.')}
        >
          <input
            className="input mono st-num"
            type="number"
            min={1}
            max={96}
            value={probes}
            onChange={(e) => setProbes(e.target.value)}
          />
          <button
            className="btn btn-ghost sm"
            disabled={!probes || Number(probes) < 1 || Number(probes) > 96 || probeMutation.isPending}
            onClick={() => probeMutation.mutate()}
          >
            {tr('保存', 'Save')}
          </button>
        </SettingsRow>

        <SettingsRow
          label={tr('保留天数', 'Keep for (days)')}
          hint={tr('过期的每日论文会被清除，未同步的也会丢失', 'Older daily papers are removed, synced or not')}
        >
          <input
            className="input mono st-num"
            type="number"
            min={1}
            max={90}
            value={days}
            onChange={(e) => setDays(e.target.value)}
          />
          <button
            className="btn btn-ghost sm"
            disabled={!days || Number(days) < 1 || Number(days) > 90 || retentionMutation.isPending}
            onClick={() => retentionMutation.mutate()}
          >
            {tr('保存', 'Save')}
          </button>
        </SettingsRow>
      </SettingsGroup>
    </SettingsSection>
  );
}

export function DailyCategoriesTab() {
  return (
    <SettingsStack>
      <DailySubscriptionsSection />
      <DailySyncSection />
      <DailyEmbedSection />
    </SettingsStack>
  );
}

/**
 * 深链 ?tab= 认得的全部取值。以前只认个人那几项：?tab=daily 之类的工作区
 * 项一律落到「个人信息」。
 * 不可用的项（非桌面的「关于」、没有插件能力的「插件」）照样由 effectiveTab 回落。
 */
export const ALL_TABS: Tab[] = [
  'personal', 'prefs', 'buddy', 'speech', 'bots', 'ssh', 'myusage', 'extension', 'mcp', 'export', 'plugins',
  'model', 'literature', 'processing', 'experiment', 'daily', 'usage', 'about',
];

/**
 * 已经撤掉的标签页 → 现在的落点。模型固定为一个之后，「模型与智能体」（llm）、
 * 更早的「智能体后端」（agents）和「我的模型」（mymodels）都只剩「模型」这一块可看。
 */
export const LEGACY_TABS: Readonly<Record<string, Tab>> = {
  llm: 'model',
  agents: 'model',
  mymodels: 'model',
};

/** 深链 ?tab= → 初始标签页；旧标签按 LEGACY_TABS 换算，认不出的落「个人信息」。 */
export function initialTabOf(param: string | null): Tab {
  const tab = param !== null && Object.hasOwn(LEGACY_TABS, param) ? LEGACY_TABS[param] : param;
  return tab != null && ALL_TABS.includes(tab as Tab) ? (tab as Tab) : 'personal';
}

export function SettingsPage() {
  // 支持 /settings?tab=mcp 这类深链（如旧 /mcp-tools 路由的重定向）
  const [searchParams, setSearchParams] = useSearchParams();
  const param = searchParams.get('tab');
  const [tab, setTabState] = useState<Tab>(() => initialTabOf(param));
  // 切换时同步到地址栏（replace，不堆历史）：刷新、复制链接都停在同一节
  const setTab = (next: Tab) => {
    setTabState(next);
    setSearchParams(
      (prev) => {
        const out = new URLSearchParams(prev);
        out.set('tab', next);
        return out;
      },
      { replace: true },
    );
  };

  // 「插件」tab 在 plugins.manage 能力可用时出现（看桌面主进程的能力清单）。能力
  // 清单由 App.tsx 启动时异步拉取，本页可能先于它渲染完，这里再取一次并在拿到结果
  // 后重读，避免首次进设置页时 tab 闪失。浏览器里开发调试没有宿主，就没有这个 tab。
  const [pluginsAvailable, setPluginsAvailable] = useState(() => isCapabilityAvailable(CAPABILITY_PLUGINS_MANAGE));
  useEffect(() => {
    if (pluginsAvailable) return;
    let alive = true;
    void loadCapabilities().then(() => {
      if (alive) setPluginsAvailable(isCapabilityAvailable(CAPABILITY_PLUGINS_MANAGE));
    });
    return () => {
      alive = false;
    };
  }, [pluginsAvailable]);
  // 深链 ?tab=plugins 在能力缺失（web 端、清单未就绪）时回落默认 tab，不崩也不留空白；
  // 清单稍后就绪且能力在，effectiveTab 自动切回 plugins。
  // 「关于」（检查更新）只在桌面端有意义（#812）
  const desktop = hasHost();
  // 只有一个使用者，「用量总览」与「用量」是同一份数据：旧深链 ?tab=usage 落到「用量」
  const effectiveTab: Tab =
    (tab === 'plugins' && !pluginsAvailable) || (tab === 'about' && !desktop)
      ? 'personal'
      : tab === 'usage'
        ? 'myusage'
        : tab;

  // 管理页并入本页后（#755），这些标签**就在这里**，不能再往 /admin 跳——
  // /admin 已经反向重定向到 /settings，两边对跳就是一个死循环。
  // 旧深链（llm / agents / mymodels）换成现在的地址，刷新、复制链接都不再带着旧名字。
  const legacyTarget = param !== null && Object.hasOwn(LEGACY_TABS, param) ? LEGACY_TABS[param] : undefined;
  if (legacyTarget) {
    return <Navigate to={`/settings?tab=${legacyTarget}`} replace />;
  }

  const generalItems: { v: Tab; label: string }[] = [
    { v: 'personal', label: tr('个人资料', 'Profile') },
    { v: 'prefs', label: tr('界面', 'Interface') },
    { v: 'buddy', label: tr('助手', 'Assistant') },
    { v: 'model', label: tr('模型', 'Model') },
    { v: 'speech', label: tr('语音', 'Speech') },
    { v: 'myusage', label: tr('用量', 'Usage') },
    { v: 'export', label: tr('数据导出', 'Export') },
    ...(desktop ? [{ v: 'about' as Tab, label: tr('关于', 'About') }] : []),
  ];
  const researchItems: { v: Tab; label: string }[] = [
    { v: 'literature', label: tr('文献检索', 'Literature search') },
    { v: 'processing', label: tr('文档处理', 'Documents') },
    { v: 'daily', label: tr('每日论文', 'Daily papers') },
    { v: 'experiment', label: tr('实验', 'Experiments') },
    { v: 'ssh', label: tr('远程服务器', 'Remote servers') },
  ];
  const connectionItems: { v: Tab; label: string }[] = [
    { v: 'bots', label: tr('群机器人', 'Group bots') },
    { v: 'extension', label: tr('浏览器扩展', 'Browser extension') },
    { v: 'mcp', label: 'MCP' },
    ...(pluginsAvailable ? [{ v: 'plugins' as Tab, label: tr('插件', 'Plugins') }] : []),
  ];

  return (
    <div className="page fadeup">
            <SettingsLayout
        nav={
          <SettingsTabs
            groups={[
              { label: tr('通用', 'General'), items: generalItems },
              { label: tr('研究', 'Research'), items: researchItems },
              { label: tr('连接', 'Connections'), items: connectionItems },
            ]}
            value={effectiveTab}
            onChange={setTab}
          />
        }
      >
      {effectiveTab === 'personal' && <PersonalTab />}
      {effectiveTab === 'prefs' && <PreferencesTab />}
      {effectiveTab === 'buddy' && <BuddySettings />}
      {effectiveTab === 'speech' && (
        <SettingsStack>
          <PersonalSpeechSettings />
          <AdminSpeechSettings />
        </SettingsStack>
      )}
      {effectiveTab === 'bots' && <ChatBotsTab />}
      {effectiveTab === 'ssh' && <SshTab />}
      {effectiveTab === 'myusage' && <MyUsageTab />}
      {effectiveTab === 'extension' && <ExtensionApiKeySettings />}
      {effectiveTab === 'mcp' && <McpToolsContent />}
      {effectiveTab === 'export' && <FullExportSettings />}
      {effectiveTab === 'plugins' && <PluginsSettings />}
      {effectiveTab === 'about' && <AboutSettings />}
      {effectiveTab === 'model' && <ModelSettings />}
      {effectiveTab === 'literature' && <LiteratureSearchSettingsPanel />}
      {effectiveTab === 'processing' && (
        <SettingsStack>
          <DocumentProcessingSettingsPanel />
          <AffiliationModeSection />
        </SettingsStack>
      )}
      {effectiveTab === 'experiment' && <ExperimentSettings />}
      {effectiveTab === 'daily' && <DailyCategoriesTab />}
      </SettingsLayout>
    </div>
  );
}
