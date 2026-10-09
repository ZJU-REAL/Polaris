import { Fragment, useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { Navigate, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Avatar } from '../../components/ui/Avatar';
import { Icon } from '../../components/ui/Icon';
import { Segmented } from '../../components/ui/Segmented';
import { Switch } from '../../components/ui/Switch';
import { Modal } from '../../components/ui/Modal';
import { FormField } from '../../components/ui/FormField';
import { toast } from '../../components/ui/Toast';
import { DropdownList, SelectMenu, useClickOutside } from '../../components/ui/SelectMenu';
import { fmtTime } from '../../lib/format';
import { SysinfoPanel } from '../../components/ui/SysinfoPanel';
import { McpToolsContent } from '../mcp/McpToolsPage';
import { AcademicIdentitySection } from './AcademicIdentitySection';
import { tr } from '../../lib/i18n';
import { stageLabel } from '../../lib/stageLabels';
import { setTaskLogHistory, useTaskLogHistory } from '../../lib/prefs';
import {
  ApiError,
  LLM_STAGES,
  api,
  isPluginStage,
  type AffiliationMode,
  type ChatBotPlatform,
  type DailySyncScope,
  LLM_EFFORT_LEVELS,
  type LlmProviderInput,
  type LlmProviderKind,
  type LlmProviderRead,
  type LlmRoute,
  type LlmTestCapability,
  type LlmTestModelInput,
  type LlmTestResult,
  type SshCredentialInput,
} from '../../lib/api';
import { BuddySettings } from './BuddySettings';
import { SettingsLayout, SettingsTabs } from './SettingsTabs';
import { SettingsActions, SettingsGroup, SettingsStack, SettingsRow, SettingsSection, StatusDot } from './settingsUi';
import { DailySubscriptionsSection } from './DailySubscriptionsSection';
import {
  budgetLabel,
  budgetsPayload,
  effectiveBudget,
  fmtChars,
  parsePositiveInt,
  specsByStage,
} from './inputBudgets';
import { ExtensionApiKeySettings } from './ExtensionApiKeySettings';
import { AGENTS_KEY, AcpAgentsSettings } from './AcpAgentsSettings';
import {
  CAPABILITY_STAGES,
  agentAllowedFor,
  agentName,
  answerStatus,
  buildRoute,
  draftComplete,
  draftsFromRoutes,
  parseTargetValue,
  rebaseDrafts,
  resolveTabParam,
  takeoverAgent,
  targetValueOf,
  type RouteDraft,
} from './llmRoutingModel';
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
   数据导出 / 插件，加上原「管理」页的六项：模型与路由 / 文献检索 /
   文档处理 / 实验 / 每日论文 / 用量总览。

   曾经分成 /settings 与 /admin 两页，是实验室时代「管理员 vs 成员」的
   残留；平台面向个人之后两边是同一个人，找同一类配置却要猜在哪一页。
   /admin 现在只是一条指向这里的重定向（见 routes.tsx）。
   ============================================================ */

const KINDS: LlmProviderKind[] = ['openai_compat', 'openai_responses', 'anthropic'];

/**
 * 类型下拉框里显示协议名，而不是内部 id（#809）。
 *
 * 选的是**协议**，不是厂商：同一个模型在不同网关上可能暴露不同接口，所以由配置的人
 * 按网关实际提供的接口来选。显示 openai_compat / openai_responses 这种 id，就是让人
 * 去猜两者差在哪。协议名是专有名词，不走 tr。
 */
export const KIND_LABELS: Record<LlmProviderKind, string> = {
  openai_compat: 'OpenAI Chat Completions',
  openai_responses: 'OpenAI Responses',
  anthropic: 'Anthropic Messages',
  fake: 'fake',
};

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

// ---------------- LLM Providers ----------------

interface ProviderDraft {
  name: string;
  kind: LlmProviderKind;
  base_url: string;
  user_agent: string;
  api_key: string;
  enabled: boolean;
  /** 可用模型列表原始输入（逗号/换行分隔），保存时解析为数组 */
  models: string;
  /** rerank 端点路径原始输入；空 = 默认 /rerank */
  rerank_path: string;
}

/** 默认 rerank 路径。各家不统一（#810），但默认值不能变：改了会把现在能用的
    LiteLLM / Cohere 风格服务一起弄坏。 */
const DEFAULT_RERANK_PATH = '/rerank';

/**
 * 输入 → 要发给后端的 rerank 路径。
 *
 * 空 = 默认；漏写开头的斜杠就补上——后端要求以 / 开头，是因为不带斜杠会拼成
 * ``https://host/v1rerank`` 这种既不报错也永远打不通的地址。与其让人收到一个
 * 422 再回来改，不如直接替他补上。
 */
export function rerankPathValue(raw: string): string {
  const v = raw.trim();
  if (!v) return DEFAULT_RERANK_PATH;
  return v.startsWith('/') ? v : `/${v}`;
}

/** 逗号/换行分隔的模型输入 → 去空白、去重后的数组。 */
function parseModels(raw: string): string[] {
  return [...new Set(raw.split(/[\n,，]/).map((s) => s.trim()).filter(Boolean))];
}

function emptyDraft(): ProviderDraft {
  return {
    name: '',
    kind: 'openai_compat',
    base_url: '',
    user_agent: '',
    api_key: '',
    enabled: true,
    models: '',
    rerank_path: '',
  };
}

function draftFrom(p: LlmProviderRead): ProviderDraft {
  return {
    name: p.name,
    kind: p.kind,
    base_url: p.base_url ?? '',
    user_agent: p.user_agent ?? '',
    api_key: '',
    enabled: p.enabled,
    models: (p.models ?? []).join('\n'),
    rerank_path: p.rerank_path ?? '',
  };
}

function toInput(d: ProviderDraft): LlmProviderInput {
  return {
    name: d.name.trim(),
    kind: d.kind,
    base_url: d.base_url.trim() || undefined,
    user_agent: d.kind === 'anthropic' ? d.user_agent.trim() : '',
    api_key: d.api_key, // 空字符串 = 不变（PATCH）；POST 时后端忽略空 key
    enabled: d.enabled,
    models: parseModels(d.models), // 整体替换（清空 = []）
    // 只有 openai_compat 会 rerank；别的类型不发，免得存进一个永远用不上的值
    rerank_path: d.kind === 'openai_compat' ? rerankPathValue(d.rerank_path) : undefined,
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
        <FormField label={tr('协议', 'Protocol')} style={{ width: 240 }}>
          <SelectMenu
            value={draft.kind}
            options={KINDS.map((k) => ({ value: k, label: KIND_LABELS[k] }))}
            onChange={(v) => setDraft({ ...draft, kind: v as LlmProviderKind })}
          />
        </FormField>
        <FormField label={tr('API 地址', 'API URL')} style={{ flex: 1 }}>
          <input className="input mono" value={draft.base_url} onChange={(e) => setDraft({ ...draft, base_url: e.target.value })}
            placeholder="https://api.example.com/v1" disabled={draft.kind === 'fake'} />
        </FormField>
      </div>
      {draft.kind === 'anthropic' && (
        <FormField label={tr('User-Agent（可选）', 'User-Agent (optional)')}
          hint={tr('留空则使用 HTTP 客户端默认值', 'Leave empty to use the HTTP client default')}>
          <input className="input mono" value={draft.user_agent}
            onChange={(e) => setDraft({ ...draft, user_agent: e.target.value })}
            placeholder="claude-cli/2.1.226 (external, sdk-cli)" />
        </FormField>
      )}
      {draft.kind === 'openai_compat' && (
        <FormField label={tr('Rerank 路径（可选）', 'Rerank path (optional)')}
          hint={tr('接在 API 地址之后，留空则用 /rerank', 'Added after the API URL. Leave empty to use /rerank.')}>
          <input className="input mono" value={draft.rerank_path}
            onChange={(e) => setDraft({ ...draft, rerank_path: e.target.value })}
            placeholder={DEFAULT_RERANK_PATH} />
        </FormField>
      )}
      <FormField label="API Key"
        hint={isNew ? undefined : tr('留空则不修改', 'Leave empty to keep the current key')}>
        <input className="input mono" type="password" autoComplete="new-password" value={draft.api_key}
          onChange={(e) => setDraft({ ...draft, api_key: e.target.value })}
          placeholder={isNew ? 'sk-…' : '••••••'} disabled={draft.kind === 'fake'} />
      </FormField>
      <FormField label={tr('可用模型', 'Models')}
        hint={tr('用逗号或换行分隔，选择模型时会列出', 'Separate with commas or new lines. They’re offered when you pick a model.')}>
        <textarea className="input mono" rows={3} style={{ resize: 'vertical', fontSize: 12 }}
          value={draft.models} onChange={(e) => setDraft({ ...draft, models: e.target.value })}
          placeholder={tr('例如 gpt-5.5, text-embedding-3-large', 'e.g. gpt-5.5, text-embedding-3-large')} />
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

/** 智能体的测试结果按 agent+model 共享。 */
const agentTestKeyOf = (agentId: string, model: string) => `agent:${agentId}|${model}`;

const testKeyOfInput = (input: LlmTestModelInput) =>
  'acp_agent_id' in input
    ? agentTestKeyOf(input.acp_agent_id, input.model ?? '')
    : testKeyOf(input.provider_id, input.model, input.capability);

/**
 * 供应商表里「模型状态」该按什么能力测（#819）。
 *
 * 以前一律按 chat 测：嵌入模型的服务恰好也应答 chat，于是显示「正常」；重排序服务不应答
 * chat，于是显示「失败」——与下面路由表按真实能力测出的结果正好相反。现在看路由表里这个
 * provider+model 被用在哪个环节：用作 embedding / rerank 就按那个能力测，否则按 chat。
 */
export function providerTestCapability(
  providerId: string,
  model: string,
  routes: readonly { stage: string; provider_id: string | null; model: string }[],
): LlmTestCapability {
  const uses = routes.filter((r) => r.provider_id === providerId && r.model.trim() === model);
  if (uses.some((r) => r.stage === 'embedding')) return 'embedding';
  if (uses.some((r) => r.stage === 'rerank')) return 'rerank';
  return 'chat';
}

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
      const key = testKeyOfInput(input);
      if (!combos.has(key)) combos.set(key, input);
    }
    if (combos.size === 0) return false;
    setTesting(true);
    setResults((prev) => {
      const next = { ...prev };
      for (const key of combos.keys()) next[key] = { status: 'testing' };
      return next;
    });
    const one = async ([key, input]: [string, LlmTestModelInput]) => {
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
    };
    const entries = [...combos.entries()];
    // 模型 API 并发测；智能体是真跑一个本机进程，一次一个
    const agentEntries = entries.filter(([, input]) => 'acp_agent_id' in input);
    try {
      await Promise.all([
        ...entries.filter(([, input]) => !('acp_agent_id' in input)).map(one),
        (async () => {
          for (const e of agentEntries) await one(e);
        })(),
      ]);
    } finally {
      setTesting(false);
    }
    return true;
  };

  return { results, testing, run };
}

function ModelStatusBadge({ state, onTest, idleHint, slow }: {
  state: TestState;
  onTest?: () => void;
  /** 不可测试（onTest 未提供）时 idle 徽标的提示文案 */
  idleHint?: string;
  /** 智能体：测试是真跑一次，可能要一分钟 */
  slow?: boolean;
}) {
  const clickable = onTest !== undefined && state.status !== 'testing';
  const base: CSSProperties = clickable ? { cursor: 'pointer' } : {};
  if (state.status === 'testing') {
    return (
      <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-2)' }}
        title={slow ? tr('智能体测试最长约需 1 分钟', 'Testing an agent can take up to a minute') : undefined}>
        <Icon name="refresh" size={11} style={{ animation: 'spin 1s linear infinite' }} />
        {tr('测试中…', 'Testing…')}
      </span>
    );
  }
  if (state.status === 'ok') {
    return (
      <span className="pill sm" style={{ ...base, background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}
        title={tr('点击重新测试', 'Click to test again')} onClick={onTest}>
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
      title={onTest ? tr('点击测试', 'Click to test') : idleHint} onClick={onTest}>
      {tr('未测试', 'Not tested')}
    </span>
  );
}

/** 窄列里的长徽标：单行省略号截断（全文放 title）。 */
const ellipsisPill: CSSProperties = {
  display: 'inline-block', maxWidth: '100%', overflow: 'hidden', textOverflow: 'ellipsis', lineHeight: '19px',
};

/** 「可用模型」列收起时最多展示的 chips 数。 */
const MODELS_COLLAPSED = 3;

// 曾经有一层 LlmAdapter：同一套 Providers/Routes UI 在 /admin/llm（全局）和
// /me/llm（用户自管）之间切换。自管轨并入平台配置后（#621）只剩一份配置，
// 适配层随之拆掉，两个 Section 直接打 /admin/llm。

function ProvidersSection() {
  const queryClient = useQueryClient();
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['llm', 'providers'],
    queryFn: () => api.listLlmProviders(),
    retry: false,
  });
  const providers = data ?? [];
  // 与路由表区共用同一个查询缓存：测试能力要按路由表里的用途来定
  const routesQuery = useQuery({ queryKey: ['llm', 'routes'], queryFn: () => api.getLlmRoutes(), retry: false });
  const routes = routesQuery.data ?? [];

  const [modal, setModal] = useState<'closed' | 'create' | string>('closed'); // string = 编辑中的 provider id
  const [draft, setDraft] = useState<ProviderDraft>(emptyDraft());
  const [expandedModels, setExpandedModels] = useState<Set<string>>(new Set());
  const tests = useModelTests(api.testLlmModel);

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ['llm'] });

  const createMutation = useMutation({
    mutationFn: () => api.createLlmProvider(toInput(draft)),
    onSuccess: () => {
      toast(tr('已添加', 'Added'), 'ok');
      setModal('closed');
      invalidate();
    },
    onError: (err) => toast(`${tr('添加失败', 'Couldn’t add')}：${err instanceof Error ? err.message : String(err)}`, 'error'),
  });
  const patchMutation = useMutation({
    mutationFn: (id: string) => api.patchLlmProvider(id, toInput(draft)),
    onSuccess: () => {
      toast(tr('已保存', 'Saved'), 'ok');
      setModal('closed');
      invalidate();
    },
    onError: (err) => toast(`${tr('保存失败', 'Couldn’t save')}：${err instanceof Error ? err.message : String(err)}`, 'error'),
  });
  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteLlmProvider(id),
    onSuccess: () => {
      toast(tr('已删除', 'Deleted'), 'ok');
      invalidate();
    },
    onError: (err) => toast(`${tr('删除失败', 'Delete failed')}：${err instanceof Error ? err.message : String(err)}`, 'error'),
  });
  const toggleMutation = useMutation({
    mutationFn: (p: LlmProviderRead) => api.patchLlmProvider(p.id, { enabled: !p.enabled }),
    onSuccess: (p) => {
      toast(p.enabled ? tr('已启用', 'Turned on') : tr('已停用', 'Turned off'), 'ok');
      invalidate();
    },
    onError: (err) => toast(`${tr('操作失败', 'Failed')}：${err instanceof Error ? err.message : String(err)}`, 'error'),
  });

  /** 每个 provider 测其 models 的第一个模型，按它在路由表里的用途测（见 providerTestCapability）。 */
  const firstModelOf = (p: LlmProviderRead): string | null => (p.models ?? [])[0]?.trim() || null;
  const runProviderTests = async (list: LlmProviderRead[]) => {
    const inputs: LlmTestModelInput[] = [];
    for (const p of list) {
      const model = firstModelOf(p);
      if (model) inputs.push({ provider_id: p.id, model, capability: providerTestCapability(p.id, model, routes) });
    }
    if (!(await tests.run(inputs))) {
      toast(tr('没有可测试的模型，请先在模型服务里填写可用模型', 'Nothing to test. Add models to a provider first.'), 'error');
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
    <SettingsSection
      title={tr('模型服务', 'Model providers')}
      desc={tr('向量嵌入和重排序需要模型服务，其余环节也可交给智能体。', 'Embeddings and reranking need a model provider. An agent can handle everything else.')}
      actions={
        <>
          <button className="btn btn-ghost sm" disabled={tests.testing || providers.length === 0}
            onClick={() => void runProviderTests(providers)}>
            {tests.testing ? tr('测试中…', 'Testing…') : tr('全部测试', 'Test all')}
          </button>
          <button className="btn btn-soft sm" onClick={() => { setDraft(emptyDraft()); setModal('create'); }}>
            <Icon name="plus" size={13} />
            {tr('添加模型服务', 'Add provider')}
          </button>
        </>
      }
    >
      {isLoading ? (
        <SettingsGroup pad><div className="st-row-hint">{tr('加载中…', 'Loading…')}</div></SettingsGroup>
      ) : isError ? (
        <SettingsGroup>
          <SettingsRow label={tr('无法加载模型服务', 'Couldn’t load model providers')}>
            <button className="btn btn-ghost sm" onClick={() => void refetch()}>{tr('重试', 'Retry')}</button>
          </SettingsRow>
        </SettingsGroup>
      ) : providers.length === 0 ? (
        <SettingsGroup>
          <SettingsRow label={tr('还没有模型服务', 'No model providers yet')} hint={tr('向量嵌入和重排序需要添加一个', 'Add one for embeddings and reranking')} />
        </SettingsGroup>
      ) : (
        <SettingsGroup>
        <div className="table-wrap">
          {/* 定宽列 + 最小宽度：主区窄时整表横滚，而不是把模型列压到几十像素、chips 叠在一起 */}
          <table className="table" style={{ tableLayout: 'fixed', minWidth: 900 }}>
            <thead>
              <tr>
                <th style={{ width: 190 }}>{tr('名称', 'Name')}</th>
                <th style={{ width: 120 }}>API Key</th>
                <th style={{ width: 280 }}>{tr('可用模型', 'Models')}</th>
                <th style={{ width: 80 }}>{tr('启用', 'On')}</th>
                <th style={{ width: 150 }}>{tr('连接', 'Connection')}</th>
                <th style={{ width: 80 }} />
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
                  ? tests.results[testKeyOf(p.id, firstModel, providerTestCapability(p.id, firstModel, routes))]
                    ?? { status: 'idle' }
                  : { status: 'idle' };
                return (
                  <tr key={p.id}>
                    <td>
                      <div title={p.name} style={{ fontSize: 12, fontWeight: 650, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{p.name}</div>
                      <div title={p.base_url ?? undefined}
                        style={{ fontSize: 10.5, color: 'var(--text-3)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {KIND_LABELS[p.kind] ?? p.kind}{p.base_url ? ` · ${p.base_url}` : ''}
                      </div>
                    </td>
                    <td className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)' }}>{p.api_key_masked ?? '—'}</td>
                    <td>
                      {models.length === 0 ? (
                        <span style={{ fontSize: 11.5, color: 'var(--text-4)' }}>{tr('未填写', 'None')}</span>
                      ) : (
                        <div className="row gap6" style={{ flexWrap: 'wrap' }}>
                          {shownModels.map((m) => (
                            <span key={m} className="tag mono" title={m}
                              style={{ fontSize: 10.5, whiteSpace: 'nowrap', maxWidth: '100%', overflow: 'hidden', textOverflow: 'ellipsis', display: 'inline-block', lineHeight: '19px' }}>
                              {m}
                            </span>
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
                      <Switch
                        checked={p.enabled}
                        disabled={toggleMutation.isPending}
                        onChange={() => toggleMutation.mutate(p)}
                        aria-label={tr(`启用 ${p.name}`, `Turn on ${p.name}`)}
                      />
                    </td>
                    <td>
                      <ModelStatusBadge
                        state={state}
                        onTest={firstModel ? () => void runProviderTests([p]) : undefined}
                        idleHint={firstModel ? undefined : tr('填写可用模型后才能测试', 'Add models to test')}
                      />
                    </td>
                    <td>
                      <div className="row gap6" style={{ justifyContent: 'flex-end' }}>
                        <button className="icon-btn" style={{ width: 26, height: 26 }} title={tr('编辑', 'Edit')}
                          onClick={() => { setDraft(draftFrom(p)); setModal(p.id); }}>
                          <Icon name="pen" size={13} />
                        </button>
                        <button className="icon-btn st-quiet-danger" style={{ width: 26, height: 26 }} title={tr('删除', 'Delete')}
                          disabled={deleteMutation.isPending}
                          onClick={() => {
                            if (window.confirm(tr(`删除模型服务「${p.name}」？用到它的环节将无法调用模型。`, `Delete provider “${p.name}”? Stages that use it will stop working.`))) {
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
        </div>
        </SettingsGroup>
      )}

      <Modal
        open={modal !== 'closed'}
        onClose={() => setModal('closed')}
        title={isNew ? tr('添加模型服务', 'Add provider') : tr('编辑模型服务', 'Edit provider')}
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
    </SettingsSection>
  );
}

// ---------------- 模型路由表 ----------------

/** 常驻顶层的行：默认 + 两个能力型环节；其余环节收进展开区。 */
const PRIMARY_STAGES: string[] = ['default', 'embedding', 'rerank'];

// 能力型环节（CAPABILITY_STAGES，见 llmRoutingModel）：不跟随「默认」（对话模型没有
// 嵌入/重排能力），也不能交给智能体，未设置即为「未设置」。

/**
 * 只能由管理员统一设置的环节。向量嵌入在此：论文向量是全平台共享的一份数据，
 * 每个人各用各的模型建向量，池子里就会混进互不可比的向量，检索排序会悄悄变乱
 * （维度恰好一样时连报错都没有）。个人配置里直接不显示这一行，后端也会拒收。
 */

/** 按环节推断测试能力：embedding → embedding，rerank → rerank，其余 chat。 */
function capabilityOf(stage: string): LlmTestCapability {
  if (stage === 'embedding') return 'embedding';
  if (stage === 'rerank') return 'rerank';
  return 'chat';
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

function RoutesSection() {
  const queryClient = useQueryClient();
  const providersQuery = useQuery({ queryKey: ['llm', 'providers'], queryFn: () => api.listLlmProviders(), retry: false });
  const routesQuery = useQuery({ queryKey: ['llm', 'routes'], queryFn: () => api.getLlmRoutes(), retry: false });
  const providers = providersQuery.data ?? [];
  // 智能体也能当路由目标（#840）；取不到时就只列模型 API
  const agentsQuery = useQuery({ queryKey: AGENTS_KEY, queryFn: () => api.listAcpAgents(), retry: false });
  const agents = agentsQuery.data ?? [];
  // 可调输入预算的登记表来自后端；老后端没有这个接口时为空，界面就不画预算输入框
  const budgetSpecsQuery = useQuery({
    queryKey: ['llm', 'input-budgets'],
    queryFn: () => api.getLlmInputBudgets(),
    retry: false,
    staleTime: Infinity,
  });
  const budgetSpecs = useMemo(() => specsByStage(budgetSpecsQuery.data ?? []), [budgetSpecsQuery.data]);

  // 只有显式设置过的 stage 才有行；其余环节运行时回退 default 路由
  const [rows, setRows] = useState<Record<string, RouteDraft>>({});
  const [showAll, setShowAll] = useState(false);
  const tests = useModelTests(api.testLlmModel);

  // 上次从服务端拿到的那份：用来分辨草稿里哪些行用户动过
  const baseRef = useRef<Record<string, RouteDraft>>({});
  useEffect(() => {
    if (!routesQuery.data) return;
    const server = draftsFromRoutes(routesQuery.data);
    // 服务端变了（「设为默认模型」、删了智能体……）：没动过的行跟上，动过的保留
    const base = baseRef.current;
    baseRef.current = server;
    setRows((prev) => rebaseDrafts(base, prev, server));
  }, [routesQuery.data]);

  const saveMutation = useMutation({
    mutationFn: () => {
      const routes: LlmRoute[] = [];
      // PUT 是整表覆盖：插件环节的行也要一并提交，否则每次保存都会把它们静默删光
      for (const stage of [...LLM_STAGES, ...pluginStages]) {
        const r = rows[stage];
        if (!r) continue;
        // 窗口与预算也得每次都带上：PUT 是整表覆盖，漏了就等于把它们清空
        const route = buildRoute(stage, r, {
          contextWindow: parsePositiveInt(r.context_window),
          budgets: budgetsPayload(r.budgets, budgetSpecs[stage] ?? []),
        });
        if (route) routes.push(route);
      }
      return api.putLlmRoutes(routes);
    },
    onSuccess: () => {
      toast(tr('已保存', 'Saved'), 'ok');
      // 刚存下的就是新的底稿：服务端回来的（规整过写法的）表整行替换，不算用户改动
      baseRef.current = rows;
      void queryClient.invalidateQueries({ queryKey: ['llm', 'routes'] });
    },
    onError: (err) => toast(`${tr('保存失败', 'Couldn’t save')}：${err instanceof Error ? err.message : String(err)}`, 'error'),
  });

  // 插件命名空间环节（#736）：不在内置清单里，路由表里有记录才显示一行。
  // 从 rows 派生而不是另存状态：清掉行（clearRow）它就消失，保存后路由随之删除，
  // 该环节回到插件注册时声明的回退链（fallback 环节 → 默认）。
  const pluginStages = useMemo(
    () =>
      Object.keys(rows)
        .filter((s) => !(LLM_STAGES as readonly string[]).includes(s) && isPluginStage(s))
        .sort(),
    [rows],
  );

  const defaultRow = rows['default'];
  const emptyDraftRow: RouteDraft = {
    provider_id: '',
    acp_agent_id: '',
    model: '',
    temperature: '',
    effort: '',
    context_window: '',
    budgets: {},
  };

  // 编辑「跟随默认」的行时，以 default 的值为底稿转成显式设置；
  // 能力型环节不跟随默认，底稿从空开始
  // 预算不从 default 抄：default 那行存的是它自己环节的键，与这个环节无关
  const seedOf = (prev: Record<string, RouteDraft>, stage: string): RouteDraft =>
    prev[stage]
      ?? (stage !== 'default' && !CAPABILITY_STAGES.has(stage) && prev['default']
        ? { ...prev['default'], budgets: {} }
        : emptyDraftRow);
  const setRow = (stage: string, patch: Partial<RouteDraft>) =>
    setRows((prev) => ({ ...prev, [stage]: { ...seedOf(prev, stage), ...patch } }));
  const setBudget = (stage: string, key: string, value: string) =>
    setRows((prev) => {
      const seed = seedOf(prev, stage);
      return { ...prev, [stage]: { ...seed, budgets: { ...seed.budgets, [key]: value } } };
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
    if (r && draftComplete(r) && !(r.acp_agent_id && !agentAllowedFor(stage))) return r;
    return null;
  };

  // 草稿里没有完整的「默认」时，保存后对话类环节由第一个启用的智能体接管
  const takeover = takeoverAgent(!!defaultRow && draftComplete(defaultRow), agents);
  /** 一行目标的显示名：智能体名或模型 API 名 + 模型。 */
  const targetLabel = (d: RouteDraft): string => {
    if (d.acp_agent_id) {
      const a = agents.find((x) => x.id === d.acp_agent_id);
      const name = a ? agentName(a) : '?';
      return d.model.trim() ? `${name} · ${d.model.trim()}` : name;
    }
    const p = providers.find((x) => x.id === d.provider_id);
    return `${p?.name ?? '?'} · ${d.model.trim()}`;
  };

  /** 测试一组 stage；去重与结果共享由 useModelTests 处理。 */
  const runTests = async (stages: string[]) => {
    const inputs: LlmTestModelInput[] = [];
    for (const stage of stages) {
      const eff = effectiveOf(stage);
      if (eff?.acp_agent_id) {
        inputs.push({ acp_agent_id: eff.acp_agent_id, model: eff.model.trim() });
      } else if (eff) {
        inputs.push({ provider_id: eff.provider_id, model: eff.model.trim(), capability: capabilityOf(stage) });
      } else if (takeover && agentAllowedFor(stage)) {
        // 跟随默认、默认又空着：实际回答的是接管的智能体，就测它
        inputs.push({ acp_agent_id: takeover.id, model: '' });
      }
    }
    if (!(await tests.run(inputs))) {
      toast(tr('没有可测试的环节，请先选择智能体或模型服务', 'Nothing to test. Pick an agent or a model provider first.'), 'error');
    }
  };

  // 常驻行固定在顶部；展开区包含其余内置环节 + 有路由记录的插件环节。
  const visibleStages: string[] = showAll
    ? [...PRIMARY_STAGES, ...LLM_STAGES.filter((s) => !PRIMARY_STAGES.includes(s)), ...pluginStages]
    : PRIMARY_STAGES;
  // 收起态下有显式设置的隐藏行数（提示用）；插件行必然是显式设置，全算上
  const hiddenExplicitCount =
    LLM_STAGES.filter((s) => !PRIMARY_STAGES.includes(s) && rows[s]).length + pluginStages.length;

  return (
    <SettingsSection
      title={tr('各环节使用的模型', 'Models by stage')}
      desc={routesQuery.isError
        ? <span style={{ color: 'var(--warn-tx)' }}>{tr('无法加载当前设置，保存会覆盖全部环节。', 'Couldn’t load the current settings. Saving will replace every stage.')}</span>
        : tr('未单独设置的环节使用「默认」，向量嵌入和重排序需单独设置。', 'Stages you don’t set use Default. Embeddings and reranking must be set separately.')}
      actions={
        <button className="btn btn-ghost sm" disabled={tests.testing} onClick={() => void runTests(visibleStages)}>
          {tests.testing ? tr('测试中…', 'Testing…') : tr('全部测试', 'Test all')}
        </button>
      }
    >
      <SettingsGroup>
      <div className="table-wrap">
        {/* 定宽列 + 最小宽度：主区窄时整表横滚；model 列至少 ~200px 才看得清 */}
        <table className="table" style={{ tableLayout: 'fixed', minWidth: 1010 }}>
          <thead>
            <tr>
              <th style={{ width: 190 }}>{tr('环节', 'Stage')}</th>
              <th style={{ width: 170 }}>{tr('由谁回答', 'Answered by')}</th>
              <th style={{ width: 210 }}>{tr('模型', 'Model')}</th>
              <th style={{ width: 90 }}>{tr('温度', 'Temperature')}</th>
              <th
                style={{ width: 110 }}
                title={tr('模型思考的深度，默认使用模型自身设置', 'How hard the model thinks. Defaults to the model’s own setting.')}
              >
                {tr('思考强度', 'Effort')}
              </th>
              <th
                style={{ width: 100, whiteSpace: 'nowrap' }}
                title={tr('模型一次能读入的 token 数，例如 128000', 'How many tokens the model can read at once, e.g. 128000')}
              >
                {tr('上下文窗口', 'Context')}
              </th>
              <th style={{ width: 140 }}>{tr('连接', 'Connection')}</th>
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
              const plugin = isPluginStage(stage);
              const label = stageLabel(stage);
              const eff = effectiveOf(stage);
              // 跟随默认、默认空着：由接管的智能体回答
              const viaTakeover = !eff && takeover && agentAllowedFor(stage) && (stage === 'default' || follows)
                ? takeover
                : null;
              const testKey = eff
                ? eff.acp_agent_id
                  ? agentTestKeyOf(eff.acp_agent_id, eff.model.trim())
                  : testKeyOf(eff.provider_id, eff.model.trim(), capabilityOf(stage))
                : viaTakeover
                  ? agentTestKeyOf(viaTakeover.id, '')
                  : null;
              const state: TestState = (testKey && tests.results[testKey]) || { status: 'idle' };
              const isAgent = !!shown.acp_agent_id;
              const agentOk = agentAllowedFor(stage);
              const providerModels = isAgent ? [] : providers.find((p) => p.id === shown.provider_id)?.models ?? [];
              const agentHint = tr('智能体不用这项设置', 'Agents ignore this setting');
              const stageBudgets = budgetSpecs[stage] ?? [];
              // 预算只认这个环节自己的行；窗口看实际会被调用的那一行（跟随默认时是 default 的）
              const ownBudgets = rows[stage]?.budgets ?? {};
              const windowTokens = parsePositiveInt(shown.context_window);
              return (
                <Fragment key={stage}>
                <tr>
                  <td>
                    {/* 标签不折行；徽标放不下就整块换到下一行，长徽标省略号截断 */}
                    <div className="row gap6" style={{ alignItems: 'center', flexWrap: 'wrap', rowGap: 4, minWidth: 0 }}>
                      <span style={{ fontSize: 12, fontWeight: 650, whiteSpace: 'nowrap', ...(plugin ? { fontFamily: 'var(--mono, monospace)', whiteSpace: 'normal', overflowWrap: 'anywhere' } : {}) }}>{tr(label.zh, label.en)}</span>
                      {plugin && (
                        <span
                          className="pill sm"
                          style={{ background: 'var(--surface-3)', color: 'var(--text-3)' }}
                          title={tr('由插件添加的环节', 'Added by a plugin')}
                        >
                          {tr('插件', 'Plugin')}
                        </span>
                      )}
                      {follows && (() => {
                        const text = defaultRow && draftComplete(defaultRow)
                          ? tr(`使用默认：${targetLabel(defaultRow)}`, `Default: ${targetLabel(defaultRow)}`)
                          : takeover
                            ? tr(`使用默认：${agentName(takeover)}`, `Default: ${agentName(takeover)}`)
                            : tr('使用默认', 'Uses default');
                        return (
                          <span title={text} style={{ ...ellipsisPill, fontSize: 12, color: 'var(--text-3)' }}>
                            {text}
                          </span>
                        );
                      })()}
                      {stage === 'default' && viaTakeover && (
                        <span
                          style={{ ...ellipsisPill, fontSize: 12, color: 'var(--text-3)' }}
                          title={tr('未设置默认模型时，由第一个启用的智能体回答', 'With no default set, the first enabled agent answers')}
                        >
                          {tr(`由 ${agentName(viaTakeover)} 回答`, `Answered by ${agentName(viaTakeover)}`)}
                        </span>
                      )}
                      {unset && (
                        <StatusDot tone="warn" title={tr('需要单独设置，未设置时相关功能不可用', 'Must be set separately. Related features are off until then.')}>
                          {tr('未设置', 'Not set')}
                        </StatusDot>
                      )}
                      {explicit && stage !== 'default' && (
                        <button
                          className="icon-btn"
                          style={{ width: 20, height: 20 }}
                          title={capability
                            ? tr('清除设置', 'Clear')
                            : plugin
                              ? tr('清除设置，使用插件的默认环节', 'Clear and use the plugin’s fallback')
                              : tr('清除设置，改用默认', 'Clear and use Default')}
                          onClick={() => clearRow(stage)}
                        >
                          <Icon name="x" size={11} />
                        </button>
                      )}
                    </div>
                  </td>
                  <td>
                    <SelectMenu
                      style={{ height: 32 }}
                      muted={follows}
                      value={targetValueOf(shown)}
                      options={[
                        { value: '', label: tr('未设置', 'Not set') },
                        // 智能体在前：推荐用法；向量嵌入/重排序这类能力型环节不列
                        ...(agentOk
                          ? agents
                              .filter((a) => a.enabled || a.id === shown.acp_agent_id)
                              .map((a) => ({
                                value: targetValueOf({ provider_id: '', acp_agent_id: a.id }),
                                label: `${tr('智能体', 'Agent')} · ${agentName(a)}`,
                              }))
                          : []),
                        ...providers.map((p) => ({ value: p.id, label: p.name })),
                      ]}
                      onChange={(v) => setRow(stage, { ...parseTargetValue(v), model: '' })}
                    />
                  </td>
                  <td>
                    <ModelCombobox
                      value={shown.model}
                      options={providerModels}
                      muted={follows}
                      placeholder={isAgent
                        ? tr('智能体默认', 'Agent default')
                        : unset
                          ? tr('未设置', 'Not set')
                          : tr('例如 deepseek-chat', 'e.g. deepseek-chat')}
                      onChange={(v) => setRow(stage, { model: v })}
                    />
                  </td>
                  <td title={isAgent ? agentHint : undefined}>
                    <input
                      className="input mono"
                      style={{ height: 32, width: '100%', fontSize: 12, ...(follows ? { color: 'var(--text-3)' } : {}) }}
                      value={isAgent ? '' : shown.temperature}
                      disabled={isAgent}
                      placeholder={isAgent ? '—' : tr('默认', 'Default')}
                      inputMode="decimal"
                      onChange={(e) => setRow(stage, { temperature: e.target.value })}
                    />
                  </td>
                  <td title={isAgent ? agentHint : undefined}>
                    <SelectMenu
                      style={{ height: 32 }}
                      muted={follows}
                      disabled={capability || isAgent}
                      value={capability || isAgent ? '' : shown.effort}
                      options={[
                        { value: '', label: tr('模型默认', 'Model default') },
                        ...LLM_EFFORT_LEVELS.map((e) => ({ value: e, label: e })),
                      ]}
                      onChange={(v) => setRow(stage, { effort: v })}
                    />
                  </td>
                  <td>
                    <input
                      className="input mono"
                      style={{ height: 32, width: '100%', fontSize: 12, ...(follows ? { color: 'var(--text-3)' } : {}) }}
                      value={capability ? '' : shown.context_window}
                      disabled={capability}
                      placeholder={tr('未知', 'Unknown')}
                      inputMode="numeric"
                      onChange={(e) => setRow(stage, { context_window: e.target.value })}
                    />
                  </td>
                  <td>
                    <ModelStatusBadge
                      state={state}
                      slow={!!eff?.acp_agent_id || !!viaTakeover}
                      onTest={eff || viaTakeover ? () => void runTests([stage]) : undefined}
                      idleHint={unset ? tr('未设置', 'Not set') : undefined}
                    />
                  </td>
                </tr>
                {stageBudgets.length > 0 && (
                  <tr>
                    <td colSpan={7} style={{ paddingTop: 0, borderTop: 'none' }}>
                      <div className="row gap12" style={{ flexWrap: 'wrap', alignItems: 'center', fontSize: 12 }}>
                        <span
                          style={{ color: 'var(--text-3)' }}
                          title={tr('每次调用最多读入的字符数，超出部分会被截掉', 'Most characters read per call. Anything longer is cut.')}
                        >
                          {tr('输入上限', 'Input limit')}
                        </span>
                        {stageBudgets.map((spec) => {
                          const raw = ownBudgets[spec.key] ?? '';
                          const effective = effectiveBudget(spec, raw, windowTokens);
                          return (
                            <span key={spec.key} className="row gap6" style={{ alignItems: 'center' }}>
                              <span>{budgetLabel(spec.key)}</span>
                              <input
                                className="input mono"
                                style={{ height: 28, width: 110, fontSize: 12 }}
                                value={raw}
                                placeholder={fmtChars(spec.default)}
                                inputMode="numeric"
                                title={`${tr('范围', 'Range')} ${fmtChars(spec.minimum)} – ${fmtChars(spec.maximum)}`}
                                onChange={(e) => setBudget(stage, spec.key, e.target.value)}
                              />
                              {effective.problem ? (
                                <span style={{ color: 'var(--warn-tx)' }}>
                                  {effective.problem.kind === 'range'
                                    ? tr(
                                        `须在 ${fmtChars(effective.problem.min)}–${fmtChars(effective.problem.max)} 之间`,
                                        `Must be ${fmtChars(effective.problem.min)}–${fmtChars(effective.problem.max)}`,
                                      )
                                    : tr(
                                        `超出上下文窗口，最多 ${fmtChars(effective.problem.max)}`,
                                        `Too large for the context window. Max ${fmtChars(effective.problem.max)}.`,
                                      )}
                                </span>
                              ) : (
                                <span style={{ color: 'var(--text-3)' }}>
                                  {tr('字符，实际', 'chars, using')} <span className="mono">{fmtChars(effective.value)}</span>
                                  {effective.cappedByWindow && tr('（受上下文窗口限制）', ' (limited by context window)')}
                                </span>
                              )}
                            </span>
                          );
                        })}
                      </div>
                    </td>
                  </tr>
                )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
      </SettingsGroup>
      <SettingsActions
        note={
          <button className="btn btn-ghost sm" onClick={() => setShowAll((v) => !v)}>
          <Icon name="chevDown" size={12} style={showAll ? { transform: 'rotate(180deg)' } : undefined} />
          {showAll
            ? tr('收起', 'Show less')
            : tr(
                `显示全部 ${LLM_STAGES.length} 个环节${hiddenExplicitCount > 0 ? `（${hiddenExplicitCount} 个已单独设置）` : ''}`,
                `Show all ${LLM_STAGES.length} stages${hiddenExplicitCount > 0 ? ` (${hiddenExplicitCount} set separately)` : ''}`,
              )}
          </button>
        }
      >
        <button className="btn btn-primary sm" disabled={saveMutation.isPending} onClick={() => saveMutation.mutate()}>
          {saveMutation.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
        </button>
      </SettingsActions>
    </SettingsSection>
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

/**
 * 向量模型现状 + 换模型后的确认入口（admin）。
 *
 * 不同模型建出来的向量互相不能比，所以全平台同一时刻只认一批向量。换了模型之后
 * 必须在这里确认一次：确认前新向量一律不写（不能把两个模型的向量混进一个池子），
 * 确认后检索改认新的一批，旧的留在库里，随时可以切回去。
 */
function EmbeddingSpaceSection() {
  const queryClient = useQueryClient();
  const { data } = useQuery({
    queryKey: ['admin', 'embedding-space'],
    queryFn: () => api.getEmbeddingSpace(),
    retry: false,
  });

  const adoptMutation = useMutation({
    mutationFn: () => api.adoptEmbeddingSpace(),
    onSuccess: (r) => {
      toast(
        tr(`已改用 ${r.active.model}`, `Now using ${r.active.model}`),
        'ok',
      );
      void queryClient.invalidateQueries({ queryKey: ['admin', 'embedding-space'] });
    },
    onError: (e) => toast(`${tr('切换失败', 'Couldn’t switch')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const active = data?.active ?? null;
  const others = (data?.spaces ?? []).filter((s) => !s.active);

  return (
    <SettingsSection
      title={tr('搜索索引', 'Search index')}
      desc={tr('更换向量模型后需在这里确认，再重建索引。', 'After changing the embedding model, confirm it here and rebuild the index.')}
    >
      {data?.mismatched && (
        <p className="st-section-desc" style={{ color: 'var(--warn-tx)' }}>
          {tr(
            `已设置 ${data.routed_model}，但索引来自 ${active?.model}。确认前不会建立新索引。`,
            `${data.routed_model} is set, but the index was built with ${active?.model}. Nothing new is indexed until you confirm.`,
          )}
        </p>
      )}

      <SettingsGroup>
      <div className="st-row">
        <div className="st-row-text" style={{ fontSize: 13 }}>
          {active ? (
            <>
              <span className="mono">{active.model}</span>
              <span style={{ color: 'var(--text-3)' }}>
                {tr(
                  ` · ${active.papers} 篇论文 · ${active.ideas} 个想法`,
                  ` · ${active.papers} papers · ${active.ideas} ideas`,
                )}
              </span>
            </>
          ) : (
            <span style={{ color: 'var(--text-3)' }}>
              {tr('还没有建立索引', 'Nothing indexed yet')}
            </span>
          )}
        </div>
        <button
          className={data?.mismatched ? 'btn btn-primary sm' : 'btn btn-soft sm'}
          disabled={adoptMutation.isPending || (!data?.routed_model)}
          title={tr('旧索引会保留，可以再切回来', 'The old index is kept, so you can switch back')}
          onClick={() => adoptMutation.mutate()}
        >
          <Icon name="check" size={12} />
          {adoptMutation.isPending ? tr('切换中…', 'Switching…') : tr('改用当前模型', 'Use current model')}
        </button>
      </div>
      {others.length > 0 && (
        <div className="st-row">
          <span className="st-row-hint">
            {tr('已保留的旧索引：', 'Kept: ')}
            {others.map((s) => `${s.model}（${s.papers}）`).join(tr('、', ', '))}
          </span>
        </div>
      )}
      </SettingsGroup>
    </SettingsSection>
  );
}

/** 页首一句话：此刻谁在回答模型调用（默认路由 → 接管的智能体 → 没有）。 */
function AnswerStatusHeader() {
  const routesQuery = useQuery({ queryKey: ['llm', 'routes'], queryFn: () => api.getLlmRoutes(), retry: false });
  const providersQuery = useQuery({ queryKey: ['llm', 'providers'], queryFn: () => api.listLlmProviders(), retry: false });
  const agentsQuery = useQuery({ queryKey: AGENTS_KEY, queryFn: () => api.listAcpAgents(), retry: false });
  // 三样都到了才下结论，免得先闪一句「没有可用的模型」
  if (!routesQuery.data || !providersQuery.data || (!agentsQuery.data && !agentsQuery.isError)) return null;
  const line = answerStatus(routesQuery.data, agentsQuery.data ?? [], providersQuery.data);
  const warn = line.tone === 'warn';
  return (
    <div className="st-status-line" role="status" style={warn ? { color: 'var(--warn-tx)' } : undefined}>
      <span className={`st-dot ${warn ? 'st-dot-warn' : 'st-dot-ok'}`} style={{ marginTop: 6 }} aria-hidden />
      <span>{tr(line.zh, line.en)}</span>
    </div>
  );
}

/**
 * 模型与智能体（#840）：智能体是首选——有一个就能对话；模型 API 变成可选，
 * 只有向量嵌入、重排序非它不可。focus='agents' 来自旧深链 ?tab=agents。
 */
export function LlmTab({ focus }: { focus?: 'agents' | null }) {
  const agentsRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (focus === 'agents') agentsRef.current?.scrollIntoView({ block: 'start' });
  }, [focus]);
  return (
    <SettingsStack>
      <AnswerStatusHeader />
      <div ref={agentsRef} id="agents" className="st-page" style={{ scrollMarginTop: 12 }}>
        <AcpAgentsSettings />
      </div>
      <ProvidersSection />
      <RoutesSection />
      <div className="st-page">
        <EmbeddingSpaceSection />
        <AdminSpeechSettings />
        <AffiliationModeSection />
      </div>
      <CallLogsSection />
    </SettingsStack>
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
  // 'llm' 即「模型与智能体」：原「智能体后端」（#836）并进来了（#840），旧 ?tab=agents 落到这里
  | 'llm' | 'literature' | 'processing' | 'experiment' | 'daily' | 'usage';

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
 * 深链 ?tab= 认得的全部取值。以前只认个人那几项：?tab=llm / ?tab=daily 之类的工作区
 * 项一律落到「个人信息」——连本页自己的旧链接重定向（mymodels → ?tab=llm）都打不开。
 * 不可用的项（非桌面的「关于」、没有插件能力的「插件」）照样由 effectiveTab 回落。
 */
export const ALL_TABS: Tab[] = [
  'personal', 'prefs', 'buddy', 'speech', 'bots', 'ssh', 'myusage', 'extension', 'mcp', 'export', 'plugins',
  'llm', 'literature', 'processing', 'experiment', 'daily', 'usage', 'about',
];

/** 深链 ?tab= → 初始标签页；认不出的落「个人信息」，旧的 agents 落「模型与智能体」。 */
export function initialTabOf(param: string | null): Tab {
  const { tab } = resolveTabParam(param);
  return tab !== null && ALL_TABS.includes(tab as Tab) ? (tab as Tab) : 'personal';
}

export function SettingsPage() {
  // 支持 /settings?tab=mcp 这类深链（如旧 /mcp-tools 路由的重定向）
  const [searchParams, setSearchParams] = useSearchParams();
  const param = searchParams.get('tab');
  const [tab, setTabState] = useState<Tab>(() => initialTabOf(param));
  // 只在落地那一次定位到智能体区；切走再切回来不再跳
  const [focusAgents, setFocusAgents] = useState(() => resolveTabParam(param).focus === 'agents');
  // 切换时同步到地址栏（replace，不堆历史）：刷新、复制链接都停在同一节
  const setTab = (next: Tab) => {
    setTabState(next);
    // 一切换标签页，落地那次的定位就用掉了（LlmTab 重新挂载时不再跳）
    setFocusAgents(false);
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
  // 「我的模型」自管轨并入平台配置后（#621），旧深链落到模型与路由这一块。
  if (param === 'mymodels') {
    return <Navigate to="/settings?tab=llm" replace />;
  }

  const generalItems: { v: Tab; label: string }[] = [
    { v: 'personal', label: tr('个人资料', 'Profile') },
    { v: 'prefs', label: tr('界面', 'Interface') },
    { v: 'buddy', label: tr('助手', 'Assistant') },
    { v: 'speech', label: tr('语音', 'Speech') },
    { v: 'myusage', label: tr('用量', 'Usage') },
    { v: 'export', label: tr('数据导出', 'Export') },
    ...(desktop ? [{ v: 'about' as Tab, label: tr('关于', 'About') }] : []),
  ];
  const researchItems: { v: Tab; label: string }[] = [
    { v: 'llm', label: tr('模型与智能体', 'Models & agents') },
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
      {effectiveTab === 'speech' && <PersonalSpeechSettings />}
      {effectiveTab === 'bots' && <ChatBotsTab />}
      {effectiveTab === 'ssh' && <SshTab />}
      {effectiveTab === 'myusage' && <MyUsageTab />}
      {effectiveTab === 'extension' && <ExtensionApiKeySettings />}
      {effectiveTab === 'mcp' && <McpToolsContent />}
      {effectiveTab === 'export' && <FullExportSettings />}
      {effectiveTab === 'plugins' && <PluginsSettings />}
      {effectiveTab === 'about' && <AboutSettings />}
      {effectiveTab === 'llm' && <LlmTab focus={focusAgents ? 'agents' : null} />}
      {effectiveTab === 'literature' && <LiteratureSearchSettingsPanel />}
      {effectiveTab === 'processing' && <DocumentProcessingSettingsPanel />}
      {effectiveTab === 'experiment' && <ExperimentSettings />}
      {effectiveTab === 'daily' && <DailyCategoriesTab />}
      </SettingsLayout>
    </div>
  );
}
