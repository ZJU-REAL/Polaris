import { useState } from 'react';
import { useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Switch } from '../../components/ui/Switch';
import { SelectMenu } from '../../components/ui/SelectMenu';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { toast } from '../../components/ui/Toast';
import { copyText } from '../../lib/clipboard';
import { errorText } from '../../lib/errors';
import { fmtRelative } from '../../lib/format';
import { tr } from '../../lib/i18n';
import {
  api,
  type AcpAgentCreate,
  type AcpAgentRead,
  type AcpAgentTemplate,
  type AcpAgentUpdate,
  type AcpPermissionPolicy,
} from '../../lib/api';
import { SettingsGroup, SettingsRow, SettingsSection, SettingsStack, StatusDot } from './settingsUi';
import {
  DEFAULT_POLICY,
  NO_PROMPTS_NOTE,
  POLICY_OPTIONS,
  acpErrorText,
  agentFix,
  agentStatus,
  envRowIssue,
  envRowsPayload,
  freeSlug,
  joinCommand,
  policyOption,
  probeCapabilities,
  probeTitle,
  slugify,
  splitArgs,
  templateDescription,
  type EnvRow,
  type ZhEn,
} from './localAgentsModel';

/* ============================================================
   设置 → 本机智能体

   把用户已经在用的编程智能体（Claude Code、Codex、pi……）经 Agent Client Protocol
   接进来，在助手里为某一场对话选它来回答。它在这台机器上、用它自己的登录运行，
   Polaris 碰不到它的凭据；Polaris 自己的模型调用不经过它。

   一页两节：已添加的（每个一行：名称、状态点、检查连接；展开看权限、环境变量、
   启停和删除），以及可以添加的（内置模板 + 自定义命令；安装和登录命令默认收起）。
   ============================================================ */

export const AGENTS_KEY = ['acp-agents'] as const;
const TEMPLATES_KEY = ['acp-templates'] as const;

/** 增删改之后要跟着刷新的：列表、模板（「已添加」标记）、助手里的「由谁回答」。 */
export function invalidateAfterAgentChange(queryClient: QueryClient): void {
  void queryClient.invalidateQueries({ queryKey: AGENTS_KEY });
  void queryClient.invalidateQueries({ queryKey: ['chat-backends'] });
}

function t(text: ZhEn): string {
  return tr(text.zh, text.en);
}

function errText(e: unknown): string {
  const known = acpErrorText(e instanceof Error ? e.message : String(e));
  return known ? t(known) : errorText(e);
}

function policyOptions() {
  return POLICY_OPTIONS.map((o) => ({ value: o.value, label: t(o.label) }));
}

const quiet = { fontSize: 'var(--fs-sm)', color: 'var(--text-3)' } as const;

function CopyLine({ text, label }: { text: string; label: string }) {
  return (
    <div className="row gap6" style={{ alignItems: 'center', minWidth: 0 }}>
      <span style={{ ...quiet, flexShrink: 0, minWidth: 40 }}>{label}</span>
      <code
        className="mono"
        style={{
          flex: 1,
          minWidth: 0,
          fontSize: 'var(--fs-sm)',
          padding: '3px 8px',
          borderRadius: 'var(--radius-sm)',
          background: 'var(--surface-2)',
          color: 'var(--text-2)',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
        title={text}
      >
        {text}
      </code>
      <button
        className="btn btn-plain xs"
        title={tr('复制', 'Copy')}
        aria-label={`${tr('复制', 'Copy')} ${label}`}
        onClick={() =>
          void copyText(text).then((ok) =>
            toast(ok ? tr('已复制', 'Copied') : tr('无法复制，请手动复制', 'Couldn’t copy. Copy it manually.'), ok ? 'ok' : 'error'),
          )
        }
      >
        <Icon name="file" size={12} />
      </button>
    </div>
  );
}

function EnvRowsEditor({ rows, onChange }: { rows: EnvRow[]; onChange: (rows: EnvRow[]) => void }) {
  return (
    <div className="col" style={{ gap: 8 }}>
      {rows.map((r, i) => {
        const issue = envRowIssue(r, rows);
        return (
          <div key={i}>
            <div className="row gap8" style={{ alignItems: 'center' }}>
              <input
                className="input mono"
                style={{ flex: '0 1 200px', minWidth: 0 }}
                value={r.key}
                placeholder="NAME"
                spellCheck={false}
                autoComplete="off"
                aria-label={tr('变量名', 'Variable name')}
                onChange={(e) => onChange(rows.map((x, j) => (j === i ? { ...x, key: e.target.value } : x)))}
              />
              <span style={{ color: 'var(--text-3)' }}>=</span>
              <input
                className="input mono"
                type="password"
                style={{ flex: 1, minWidth: 0 }}
                value={r.value}
                placeholder={tr('值', 'Value')}
                spellCheck={false}
                autoComplete="new-password"
                aria-label={tr('变量值', 'Variable value')}
                onChange={(e) => onChange(rows.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))}
              />
              <button
                className="icon-btn"
                title={tr('移除这一行', 'Remove this row')}
                aria-label={tr('移除这一行', 'Remove this row')}
                onClick={() => onChange(rows.filter((_, j) => j !== i))}
              >
                <Icon name="x" size={12} />
              </button>
            </div>
            {issue && <div className="st-row-error">{t(issue)}</div>}
          </div>
        );
      })}
      <div>
        <button className="btn btn-soft sm" onClick={() => onChange([...rows, { key: '', value: '' }])}>
          <Icon name="plus" size={12} />
          {tr('添加变量', 'Add variable')}
        </button>
      </div>
    </div>
  );
}

/** 一行里的小标题 + 内容（展开区里用，不再套卡片）。 */
function Field({
  label,
  hint,
  text,
  children,
}: {
  label: string;
  hint?: React.ReactNode;
  /** 内容是一行字而不是控件：和标签按同一条基线对齐 */
  text?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className={'lagent-field' + (text ? ' lagent-field-text' : '')}>
      <div className="lagent-field-label">{label}</div>
      <div className="lagent-field-body">
        {children}
        {hint && <div className="st-row-hint">{hint}</div>}
      </div>
    </div>
  );
}

/** 一个已添加的智能体：名称、状态、检查连接；展开后是权限、环境变量、启停、删除。 */
export function AgentRow({ agent, template }: { agent: AcpAgentRead; template?: AcpAgentTemplate }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [envEditing, setEnvEditing] = useState(false);
  const [envRows, setEnvRows] = useState<EnvRow[]>([]);

  const refresh = (row?: AcpAgentRead) => {
    if (row) {
      queryClient.setQueryData<AcpAgentRead[]>(AGENTS_KEY, (old) => old?.map((a) => (a.id === row.id ? row : a)));
    }
    invalidateAfterAgentChange(queryClient);
  };

  const update = useMutation({
    mutationFn: (input: AcpAgentUpdate) => api.updateAcpAgent(agent.id, input),
    onSuccess: (row) => refresh(row),
    onError: (e) => toast(`${tr('无法保存', 'Couldn’t save')}：${errText(e)}`, 'error'),
  });
  const probe = useMutation({
    mutationFn: () => api.probeAcpAgent(agent.id),
    onSuccess: (row) => refresh(row),
    onError: (e) => toast(`${tr('无法检查连接', 'Couldn’t check the connection')}：${errText(e)}`, 'error'),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteAcpAgent(agent.id),
    onSuccess: () => {
      queryClient.setQueryData<AcpAgentRead[]>(AGENTS_KEY, (old) => old?.filter((a) => a.id !== agent.id));
      invalidateAfterAgentChange(queryClient);
      setConfirmDelete(false);
      toast(tr('已删除', 'Deleted'), 'ok');
    },
    onError: (e) => toast(`${tr('无法删除', 'Couldn’t delete')}：${errText(e)}`, 'error'),
  });

  const name = agent.name || agent.slug;
  const status = probe.isPending
    ? { tone: 'busy' as const, label: { zh: '正在检查…', en: 'Checking…' } }
    : agentStatus(agent);
  const fix = agentFix(agent, template);
  const policy = policyOption(agent.permission_policy);
  const envIssue = envRows.some((r) => envRowIssue(r, envRows));
  const info = agent.last_probe;
  const caps = info ? probeCapabilities(info) : [];

  // 收起时的那一行说明：出问题说该做什么，否则说检查到了什么，再否则给命令
  const summary = fix
    ? fix.kind === 'install'
      ? tr('本机找不到它。先安装，再检查连接。', 'It isn’t installed here. Install it, then check the connection.')
      : tr('它还没有可用的凭据。在终端里登录一次，再检查连接。', 'It isn’t signed in. Sign in once in a terminal, then check the connection.')
    : agent.last_error
      ? tr('检查连接没有通过，原因见展开后的详情。', 'The connection check failed. Expand for details.')
      : info
        ? [probeTitle(info), ...caps.map(t)].filter(Boolean).join(' · ')
        : joinCommand(agent.command, agent.args);

  return (
    <div className="st-row st-row-stack lagent-row">
      <div className="row gap8" style={{ alignItems: 'center', minWidth: 0 }}>
        <button
          className="lagent-toggle"
          aria-expanded={open}
          onClick={() => setOpen((o) => !o)}
          title={open ? tr('收起', 'Collapse') : tr('展开设置', 'Show settings')}
        >
          <Icon name={open ? 'chevDown' : 'chevron'} size={12} />
          <span className="st-row-label lagent-name" title={name}>{name}</span>
        </button>
        <StatusDot tone={status.tone} title={agent.last_probed_at ? fmtRelative(agent.last_probed_at) : undefined}>
          {t(status.label)}
        </StatusDot>
        <span style={{ flex: 1 }} />
        <button className="btn btn-ghost sm" disabled={probe.isPending || !agent.enabled} onClick={() => probe.mutate()}>
          {tr('检查连接', 'Check connection')}
        </button>
      </div>
      <div className="st-row-hint" style={{ paddingLeft: 18 }}>
        {info || fix || agent.last_error ? summary : <span className="mono" style={{ overflowWrap: 'anywhere' }}>{summary}</span>}
      </div>
      {fix && (
        <div style={{ paddingLeft: 18 }}>
          <CopyLine label={fix.kind === 'install' ? tr('安装', 'Install') : tr('登录', 'Sign in')} text={fix.command} />
        </div>
      )}

      {open && (
        <div className="lagent-detail">
          {agent.last_error && (
            <Field label={tr('检查结果', 'Last check')} text>
              <div className="mono lagent-error">{agent.last_error}</div>
            </Field>
          )}
          <Field label={tr('启动命令', 'Command')} text>
            <span className="mono" style={{ fontSize: 'var(--fs-sm)', color: 'var(--text-2)', overflowWrap: 'anywhere' }}>
              {joinCommand(agent.command, agent.args)}
            </span>
          </Field>
          <Field
            label={tr('权限', 'Permissions')}
            hint={
              <>
                {policy && <span style={policy.warn ? { color: 'var(--warn-tx)' } : undefined}>{t(policy.hint)}</span>}
                {!agent.permission_prompts && <span style={{ display: 'block', color: 'var(--warn-tx)' }}>{t(NO_PROMPTS_NOTE)}</span>}
              </>
            }
          >
            <SelectMenu
              value={agent.permission_policy}
              options={policyOptions()}
              disabled={update.isPending}
              wrapStyle={{ width: 220, maxWidth: '100%' }}
              onChange={(v) => update.mutate({ permission_policy: v as AcpPermissionPolicy })}
            />
          </Field>
          <Field
            label={tr('环境变量', 'Environment variables')}
            hint={
              envEditing
                ? tr('保存后替换全部原有变量，之后只显示名字。', 'Saving replaces all existing variables. Afterwards only names are shown.')
                : undefined
            }
          >
            {envEditing ? (
              <>
                <EnvRowsEditor rows={envRows} onChange={setEnvRows} />
                <div className="row gap8" style={{ marginTop: 8 }}>
                  <button
                    className="btn btn-soft sm"
                    disabled={envIssue || update.isPending}
                    onClick={() =>
                      update.mutate(
                        { env: envRowsPayload(envRows) },
                        {
                          onSuccess: () => {
                            setEnvEditing(false);
                            setEnvRows([]);
                            toast(tr('已保存', 'Saved'), 'ok');
                          },
                        },
                      )
                    }
                  >
                    {tr('保存', 'Save')}
                  </button>
                  <button className="btn btn-plain sm" onClick={() => setEnvEditing(false)}>
                    {tr('取消', 'Cancel')}
                  </button>
                </div>
              </>
            ) : (
              <div className="row gap6" style={{ alignItems: 'center', flexWrap: 'wrap' }}>
                {agent.env_keys.length === 0 ? (
                  <span style={quiet}>{tr('未设置', 'None')}</span>
                ) : (
                  agent.env_keys.map((k) => (
                    <span key={k} className="chip mono" style={{ cursor: 'default', fontSize: 'var(--fs-sm)' }}>
                      {k}
                    </span>
                  ))
                )}
                <button
                  className="btn btn-plain sm"
                  onClick={() => {
                    // 值读不回来：重填时只预置名字
                    setEnvRows(agent.env_keys.length ? agent.env_keys.map((k) => ({ key: k, value: '' })) : [{ key: '', value: '' }]);
                    setEnvEditing(true);
                  }}
                >
                  {agent.env_keys.length ? tr('重新设置', 'Replace') : tr('添加', 'Add')}
                </button>
              </div>
            )}
          </Field>
          <Field label={tr('在助手里可选', 'Available in the assistant')}>
            <Switch
              checked={agent.enabled}
              disabled={update.isPending}
              onChange={(v) => update.mutate({ enabled: v })}
              aria-label={tr('在助手里可选', 'Available in the assistant')}
            />
          </Field>
          <div className="row" style={{ justifyContent: 'flex-end' }}>
            <button className="btn btn-danger-soft sm" onClick={() => setConfirmDelete(true)}>
              {tr('删除', 'Delete')}
            </button>
          </div>
        </div>
      )}

      <ConfirmModal
        open={confirmDelete}
        onClose={() => setConfirmDelete(false)}
        title={tr(`删除智能体「${name}」？`, `Delete agent “${name}”?`)}
        message={tr(
          '选了它的对话会改回由 Polaris 回答，已有的消息不受影响。它本身仍装在这台电脑上。',
          'Conversations that used it go back to Polaris; their messages stay. The agent itself stays installed on this computer.',
        )}
        confirmText={tr('删除', 'Delete')}
        danger
        busy={remove.isPending}
        onConfirm={() => remove.mutate()}
      />
    </div>
  );
}

/** 一个内置模板：装没装、一句说明、添加；安装和登录命令默认收起。 */
function TemplateRow({
  template,
  adding,
  onAdd,
}: {
  template: AcpAgentTemplate;
  adding: boolean;
  onAdd: () => void;
}) {
  const [howOpen, setHowOpen] = useState(false);
  const desc = t(templateDescription(template));
  return (
    <div className="st-row st-row-stack lagent-row">
      <div className="row gap8" style={{ alignItems: 'center', minWidth: 0 }}>
        <span className="st-row-label lagent-name" title={template.name}>{template.name}</span>
        <StatusDot tone={template.installed ? 'ok' : 'idle'}>
          {template.installed ? tr('已安装', 'Installed') : tr('未安装', 'Not installed')}
        </StatusDot>
        <span style={{ flex: 1 }} />
        {(template.install || template.login) && (
          <button className="btn btn-plain sm" aria-expanded={howOpen} onClick={() => setHowOpen((o) => !o)}>
            {howOpen ? tr('收起', 'Hide') : template.installed ? tr('登录方法', 'How to sign in') : tr('安装方法', 'How to install')}
          </button>
        )}
        <button className="btn btn-ghost sm" disabled={adding} onClick={onAdd}>
          {tr('添加', 'Add')}
        </button>
      </div>
      <div className="st-row-hint">
        {desc}
        {!template.permission_prompts && <span style={{ display: 'block' }}>{t(NO_PROMPTS_NOTE)}</span>}
      </div>
      {howOpen && (
        <div className="col" style={{ gap: 6 }}>
          {template.install && !template.installed && <CopyLine label={tr('安装', 'Install')} text={template.install} />}
          {template.login && <CopyLine label={tr('登录', 'Sign in')} text={template.login} />}
          <div className="st-row-hint">
            {tr('在终端里运行。', 'Run these in a terminal.')}{' '}
            {template.homepage && (
              <a href={template.homepage} target="_blank" rel="noreferrer" style={{ color: 'var(--text-2)' }}>
                {tr('项目主页', 'Project page')}
              </a>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/** 自定义智能体：任何支持 ACP 的命令。 */
function CustomAgentForm({ taken, onDone }: { taken: string[]; onDone: () => void }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState('');
  const [command, setCommand] = useState('');
  const [args, setArgs] = useState('');
  const [envRows, setEnvRows] = useState<EnvRow[]>([]);
  const [policy, setPolicy] = useState<AcpPermissionPolicy>(DEFAULT_POLICY);
  const envIssue = envRows.some((r) => envRowIssue(r, envRows));
  const picked = policyOption(policy);

  const create = useMutation({
    mutationFn: () =>
      api.createAcpAgent({
        // 标识是内部的：从名称（没有就从命令）推出来，撞了自动加序号
        slug: freeSlug(slugify(name.trim() || command.trim().split('/').pop() || ''), taken),
        name: name.trim() || undefined,
        template: 'custom',
        command: command.trim(),
        args: splitArgs(args),
        env: envRowsPayload(envRows),
        permission_policy: policy,
      }),
    onSuccess: (row) => {
      invalidateAfterAgentChange(queryClient);
      toast(tr(`已添加 ${row.name}`, `Added ${row.name}`), 'ok');
      onDone();
    },
    onError: (e) => toast(`${tr('无法添加', 'Couldn’t add')}：${errText(e)}`, 'error'),
  });

  return (
    <div className="lagent-detail">
      <Field label={tr('名称', 'Name')}>
        <input className="input" value={name} placeholder={tr('例如 我的智能体', 'e.g. My agent')} onChange={(e) => setName(e.target.value)} />
      </Field>
      <Field label={tr('启动命令', 'Command')} hint={tr('命令名或完整路径', 'Command name or full path')}>
        <input
          className="input mono"
          value={command}
          placeholder="my-agent-acp"
          spellCheck={false}
          onChange={(e) => setCommand(e.target.value)}
        />
      </Field>
      <Field label={tr('参数', 'Arguments')} hint={tr('用空格分隔', 'Separate with spaces')}>
        <input
          className="input mono"
          value={args}
          placeholder="--acp"
          spellCheck={false}
          onChange={(e) => setArgs(e.target.value)}
        />
      </Field>
      <Field label={tr('权限', 'Permissions')} hint={picked ? <span style={picked.warn ? { color: 'var(--warn-tx)' } : undefined}>{t(picked.hint)}</span> : undefined}>
        <SelectMenu
          value={policy}
          options={policyOptions()}
          wrapStyle={{ width: 220, maxWidth: '100%' }}
          onChange={(v) => setPolicy(v as AcpPermissionPolicy)}
        />
      </Field>
      <Field label={tr('环境变量', 'Environment variables')} hint={tr('保存后只显示名字。', 'After saving, only names are shown.')}>
        <EnvRowsEditor rows={envRows} onChange={setEnvRows} />
      </Field>
      <div className="row gap8" style={{ justifyContent: 'flex-end' }}>
        <button className="btn btn-plain sm" onClick={onDone}>
          {tr('取消', 'Cancel')}
        </button>
        <button className="btn btn-soft sm" disabled={!command.trim() || envIssue || create.isPending} onClick={() => create.mutate()}>
          {create.isPending ? tr('正在添加…', 'Adding…') : tr('添加', 'Add')}
        </button>
      </div>
    </div>
  );
}

export function LocalAgentsSettings() {
  const queryClient = useQueryClient();
  const agentsQ = useQuery({ queryKey: AGENTS_KEY, queryFn: () => api.listAcpAgents(), retry: false });
  const templatesQ = useQuery({ queryKey: TEMPLATES_KEY, queryFn: () => api.listAcpTemplates(), retry: false });
  const [customOpen, setCustomOpen] = useState(false);
  const agents = agentsQ.data ?? [];
  const templates = templatesQ.data ?? [];
  const taken = agents.map((a) => a.slug);
  // 已经添加过的模板不再列在「添加」里：它的安装、登录提示在上面那一行
  const addedTemplates = new Set(agents.map((a) => a.template));
  const addable = templates.filter((tpl) => !addedTemplates.has(tpl.id));

  const addTemplate = useMutation({
    mutationFn: (tpl: AcpAgentTemplate) =>
      api.createAcpAgent({ slug: freeSlug(tpl.id, taken), template: tpl.id } satisfies AcpAgentCreate),
    onSuccess: (row) => {
      invalidateAfterAgentChange(queryClient);
      toast(tr(`已添加 ${row.name}，可以检查连接了`, `Added ${row.name}. You can check the connection now.`), 'ok');
    },
    onError: (e) => toast(`${tr('无法添加', 'Couldn’t add')}：${errText(e)}`, 'error'),
  });

  return (
    <SettingsStack>
      <SettingsSection
        title={tr('本机智能体', 'Local agents')}
        desc={tr(
          '接入你已在用的 Claude Code、Codex、pi，在助手里选它回答。',
          'Connect the agents you already use, like Claude Code, Codex or pi, and pick one to answer in the assistant.',
        )}
      >
        <SettingsGroup>
          {agentsQ.isLoading ? (
            <div className="st-row"><span className="st-row-hint">{tr('加载中…', 'Loading…')}</span></div>
          ) : agentsQ.isError ? (
            <SettingsRow label={tr('无法加载智能体', 'Couldn’t load agents')}>
              <button className="btn btn-ghost sm" onClick={() => void agentsQ.refetch()}>
                {tr('重试', 'Retry')}
              </button>
            </SettingsRow>
          ) : agents.length === 0 ? (
            <SettingsRow
              label={tr('还没有接入智能体', 'No agents connected yet')}
              hint={tr('从下方添加一个。', 'Add one below.')}
            />
          ) : (
            agents.map((a) => <AgentRow key={a.id} agent={a} template={templates.find((x) => x.id === a.template)} />)
          )}
        </SettingsGroup>
      </SettingsSection>

      <SettingsSection
        title={tr('添加智能体', 'Add an agent')}
        desc={tr(
          '它用你自己的登录在本机运行，Polaris 读不到它的凭据。',
          'It runs on this computer with your own sign-in. Polaris can’t read its credentials.',
        )}
      >
        <SettingsGroup>
          {templatesQ.isLoading ? (
            <div className="st-row"><span className="st-row-hint">{tr('加载中…', 'Loading…')}</span></div>
          ) : templatesQ.isError ? (
            <div className="st-row"><span className="st-row-hint">{tr('无法加载可添加的智能体', 'Couldn’t load the agent list')}</span></div>
          ) : (
            addable.map((tpl) => (
              <TemplateRow
                key={tpl.id}
                template={tpl}
                adding={addTemplate.isPending && addTemplate.variables?.id === tpl.id}
                onAdd={() => addTemplate.mutate(tpl)}
              />
            ))
          )}
          <div className="st-row st-row-stack lagent-row">
            <div className="row gap8" style={{ alignItems: 'center' }}>
              <span className="st-row-label">{tr('自定义命令', 'Custom command')}</span>
              <span style={{ flex: 1 }} />
              <button className="btn btn-plain sm" aria-expanded={customOpen} onClick={() => setCustomOpen((o) => !o)}>
                {customOpen ? tr('收起', 'Hide') : tr('填写', 'Set up')}
              </button>
            </div>
            <div className="st-row-hint">{tr('任何支持 ACP 协议的命令', 'Any command that speaks ACP')}</div>
            {customOpen && <CustomAgentForm taken={taken} onDone={() => setCustomOpen(false)} />}
          </div>
        </SettingsGroup>
      </SettingsSection>
    </SettingsStack>
  );
}
