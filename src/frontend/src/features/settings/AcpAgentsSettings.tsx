import { useState } from 'react';
import { useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { FormField } from '../../components/ui/FormField';
import { Switch } from '../../components/ui/Switch';
import { SelectMenu } from '../../components/ui/SelectMenu';
import { toast } from '../../components/ui/Toast';
import { copyText } from '../../lib/clipboard';
import { fmtTime } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { SettingsGroup, SettingsRow, SettingsSection, StatusDot } from './settingsUi';
import {
  ApiError,
  api,
  type AcpAgentCreate,
  type AcpAgentRead,
  type AcpAgentTemplate,
  type AcpAgentUpdate,
  type AcpPermissionPolicy,
} from '../../lib/api';
import {
  DEFAULT_POLICY,
  POLICY_OPTIONS,
  SLUG_RE,
  TEMPLATE_DESCRIPTIONS,
  acpErrorText,
  envRowIssue,
  envRowsPayload,
  freeSlug,
  joinCommand,
  probeCapabilities,
  probeTitle,
  slugify,
  splitArgs,
  type EnvRow,
} from './acpAgentsModel';
import { takeoverAgent, withDefaultAgent } from './llmRoutingModel';

/* ============================================================
   设置 → 模型与智能体 → 智能体（#836，#840 起并入模型页且排第一）

   用户已经在用的智能体（Claude Code、Codex、Gemini CLI……）走 Agent Client Protocol
   接进来。它在这台机器上、用它自己的登录跑，Polaris 碰不到它的密钥。除了在助手里
   直接选它，它也能当模型用：设为默认模型，或没有默认模型时自动接管对话类环节。
   这里做：从模板一键登记、登记自定义的、管理已登记的、设为默认模型。
   ============================================================ */

export const AGENTS_KEY = ['acp-agents'] as const;
const TEMPLATES_KEY = ['acp-templates'] as const;

/** 智能体增删、启停之后要跟着刷新的：列表、助手里的「谁来答」、模型路由（后端删智能体时
    会连带删掉指向它的路由行；启停会改变谁来接管）。 */
export function invalidateAfterAgentChange(queryClient: QueryClient): void {
  void queryClient.invalidateQueries({ queryKey: AGENTS_KEY });
  void queryClient.invalidateQueries({ queryKey: ['chat-backends'] });
  void queryClient.invalidateQueries({ queryKey: ['llm'] });
}

function errText(e: unknown): string {
  const msg = e instanceof Error ? e.message : String(e);
  const known = acpErrorText(msg);
  return known ? tr(known.zh, known.en) : msg;
}

function policyOptions() {
  return POLICY_OPTIONS.map((o) => ({ value: o.value, label: tr(o.label.zh, o.label.en) }));
}

/** 选中的策略自带的一句说明（目前只有「每次都问我」有） */
function PolicyHint({ policy }: { policy?: (typeof POLICY_OPTIONS)[number] }) {
  if (!policy?.hint) return null;
  return (
    <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 4 }}>{tr(policy.hint.zh, policy.hint.en)}</div>
  );
}

/** 撞了 slug 就换 -2、-3 再试；别的错误原样抛。 */
async function createDeduped(input: AcpAgentCreate, taken: string[]): Promise<AcpAgentRead> {
  const used = [...taken];
  let slug = freeSlug(input.slug, used);
  for (let attempt = 0; attempt < 5; attempt += 1) {
    try {
      return await api.createAcpAgent({ ...input, slug });
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 409)) throw e;
      used.push(slug);
      slug = freeSlug(input.slug, used);
    }
  }
  return api.createAcpAgent({ ...input, slug });
}

function CopyLine({ text, label }: { text: string; label: string }) {
  return (
    <div className="row gap6" style={{ alignItems: 'center', minWidth: 0 }}>
      <span style={{ fontSize: 11.5, color: 'var(--text-3)', flexShrink: 0 }}>{label}</span>
      <code
        className="mono"
        style={{
          flex: 1,
          minWidth: 0,
          fontSize: 11.5,
          padding: '3px 8px',
          borderRadius: 6,
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
        className="btn btn-ghost sm"
        title={tr('复制', 'Copy')}
        aria-label={tr('复制', 'Copy')}
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
                className="btn btn-ghost sm"
                title={tr('删除', 'Remove')}
                onClick={() => onChange(rows.filter((_, j) => j !== i))}
              >
                <Icon name="x" size={12} />
              </button>
            </div>
            {issue && <div className="field-error">{tr(issue.zh, issue.en)}</div>}
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

/** 一个已登记的智能体：命令、开关、权限、环境变量、检查连接、删除、设为默认模型。 */
function AgentRow({
  agent,
  modelRole,
  onMakeDefault,
  makingDefault,
}: {
  agent: AcpAgentRead;
  /** 它在模型路由里的位置：默认模型 / 没有默认时接管 / 都不是 */
  modelRole: 'default' | 'takeover' | null;
  /** 取不到路由表时不给这个按钮 */
  onMakeDefault?: () => void;
  makingDefault?: boolean;
}) {
  const queryClient = useQueryClient();
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
    onError: (e) => toast(`${tr('保存失败', 'Couldn’t save')}：${errText(e)}`, 'error'),
  });
  const probe = useMutation({
    mutationFn: () => api.probeAcpAgent(agent.id),
    onSuccess: (row) => {
      refresh(row);
      if (row.last_error) toast(tr('连接失败，原因见下方', 'Couldn’t connect. See the details below.'), 'error');
      else toast(tr('连接正常', 'Connected'), 'ok');
    },
    onError: (e) => toast(`${tr('检查失败', 'Couldn’t check')}：${errText(e)}`, 'error'),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteAcpAgent(agent.id),
    onSuccess: () => {
      queryClient.setQueryData<AcpAgentRead[]>(AGENTS_KEY, (old) => old?.filter((a) => a.id !== agent.id));
      refresh();
      toast(tr('已删除', 'Removed'), 'ok');
    },
    onError: (e) => toast(`${tr('删除失败', 'Couldn’t remove')}：${errText(e)}`, 'error'),
  });

  const policy = POLICY_OPTIONS.find((o) => o.value === agent.permission_policy);
  const envIssue = envRows.some((r) => envRowIssue(r, envRows));
  const probeInfo = agent.last_probe;
  const caps = probeInfo ? probeCapabilities(probeInfo) : [];

  return (
    <div className="st-row st-row-stack" style={{ gap: 4 }}>
      <div className="row gap8" style={{ alignItems: 'center', flexWrap: 'wrap' }}>
        <span className="st-row-label" style={agent.enabled ? undefined : { color: 'var(--text-3)' }}>{agent.name || agent.slug}</span>
        {!agent.enabled && <StatusDot tone="idle">{tr('已停用', 'Off')}</StatusDot>}
        {modelRole === 'default' && <StatusDot tone="ok">{tr('默认', 'Default')}</StatusDot>}
        {modelRole === 'takeover' && (
          <StatusDot tone="ok" title={tr('未设置默认模型，由它回答', 'No default model is set, so it answers')}>
            {tr('正在回答', 'Answering')}
          </StatusDot>
        )}
        <span style={{ flex: 1 }} />
        {onMakeDefault && agent.enabled && modelRole !== 'default' && (
          <button
            className="btn btn-ghost sm"
            disabled={makingDefault}
            title={tr('未单独设置的环节都由它回答', 'It answers every stage you haven’t set separately')}
            onClick={onMakeDefault}
          >
            {tr('设为默认', 'Make default')}
          </button>
        )}
        <button className="btn btn-ghost sm" disabled={probe.isPending} onClick={() => probe.mutate()}>
          {probe.isPending ? tr('检查中…', 'Checking…') : tr('检查连接', 'Check connection')}
        </button>
        {confirmDelete ? (
          <>
            <span style={{ fontSize: 12, color: 'var(--danger-tx)' }}>{tr(`删除「${agent.name || agent.slug}」？`, `Remove “${agent.name || agent.slug}”?`)}</span>
            <button className="btn btn-danger sm" disabled={remove.isPending} onClick={() => remove.mutate()}>
              {tr('删除', 'Remove')}
            </button>
            <button className="btn btn-ghost sm" onClick={() => setConfirmDelete(false)}>
              {tr('取消', 'Cancel')}
            </button>
          </>
        ) : (
          <button className="icon-btn st-quiet-danger" title={tr('删除', 'Remove')} aria-label={tr('删除', 'Remove')} onClick={() => setConfirmDelete(true)}>
            <Icon name="trash" size={12} />
          </button>
        )}
      </div>

      <div className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 6, overflowWrap: 'anywhere' }}>
        {joinCommand(agent.command, agent.args)}
      </div>
      {!agent.command_found && (
        <div style={{ fontSize: 12, color: 'var(--warn-tx)', marginTop: 4 }}>
          {tr(
            `本机找不到命令「${agent.command}」，请先安装`,
            `Can’t find “${agent.command}” on this computer. Install it first.`,
          )}
        </div>
      )}

      {/* 检查结果：成功给名称、版本和能力；失败原样给原因（通常是没装或没登录） */}
      {agent.last_error ? (
        <div
          style={{
            fontSize: 12,
            color: 'var(--warn-tx)',
            background: 'var(--warn-bg)',
            borderRadius: 7,
            padding: '7px 10px',
            marginTop: 8,
            whiteSpace: 'pre-wrap',
            overflowWrap: 'anywhere',
          }}
        >
          {agent.last_error}
        </div>
      ) : probeInfo ? (
        <div className="row gap6" style={{ marginTop: 8, flexWrap: 'wrap', alignItems: 'center', fontSize: 12 }}>
          <Icon name="check" size={12} style={{ color: 'var(--ok-tx)' }} />
          <span style={{ color: 'var(--text-2)' }}>{probeTitle(probeInfo) || tr('连接正常', 'Connected')}</span>
          {caps.map((c) => (
            <span key={c.en} className="pill sm">
              {tr(c.zh, c.en)}
            </span>
          ))}
          {agent.last_probed_at && (
            <span style={{ color: 'var(--text-4)', fontSize: 11 }}>{fmtTime(agent.last_probed_at)}</span>
          )}
        </div>
      ) : (
        <div style={{ fontSize: 12, color: 'var(--text-4)', marginTop: 8 }}>
          {tr('未检查连接', 'Not checked yet')}
        </div>
      )}

      <div className="settings-fields" style={{ marginTop: 12 }}>
        <FormField
          label={tr('权限', 'Permissions')}
          hint={tr('智能体要读写文件或运行命令时如何处理', 'What happens when the agent wants to read, edit or run something')}
        >
          <SelectMenu
            value={agent.permission_policy}
            options={policyOptions()}
            disabled={update.isPending}
            onChange={(v) => update.mutate({ permission_policy: v as AcpPermissionPolicy })}
          />
          {policy?.warn && (
            <div style={{ fontSize: 12, color: 'var(--warn-tx)', marginTop: 4 }}>
              {tr('它会直接改文件、运行命令而不先询问，只在可信的环境中使用。', 'It edits files and runs commands without asking. Use only where you trust it.')}
            </div>
          )}
          <PolicyHint policy={policy} />
        </FormField>
        <div className="col" style={{ gap: 10 }}>
          <label className="row gap8" style={{ alignItems: 'center', fontSize: 13 }}>
            <Switch
              checked={agent.enabled}
              disabled={update.isPending}
              onChange={(v) => update.mutate({ enabled: v })}
              aria-label={tr('启用', 'On')}
            />
            {tr('启用', 'On')}
          </label>
        </div>
      </div>

      <div className="field" style={{ marginTop: 4 }}>
        <label className="field-label">{tr('环境变量', 'Environment variables')}</label>
        {envEditing ? (
          <>
            <EnvRowsEditor rows={envRows} onChange={setEnvRows} />
            <div className="field-hint">
              {tr(
                '保存后替换全部原有变量；删光再保存即清空。',
                'Saving replaces all existing variables. Remove every row and save to clear them.',
              )}
            </div>
            <div className="row gap8" style={{ marginTop: 8 }}>
              <button
                className="btn btn-primary sm"
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
              <button className="btn btn-ghost sm" onClick={() => setEnvEditing(false)}>
                {tr('取消', 'Cancel')}
              </button>
            </div>
          </>
        ) : (
          <div className="row gap6" style={{ alignItems: 'center', flexWrap: 'wrap' }}>
            {agent.env_keys.length === 0 ? (
              <span style={{ fontSize: 12, color: 'var(--text-4)' }}>{tr('未设置', 'None')}</span>
            ) : (
              agent.env_keys.map((k) => (
                <span key={k} className="chip mono" style={{ cursor: 'default' }}>
                  {k}
                </span>
              ))
            )}
            <button
              className="btn btn-ghost sm"
              onClick={() => {
                // 值读不回来：重填时只预置名字
                setEnvRows(agent.env_keys.map((k) => ({ key: k, value: '' })));
                setEnvEditing(true);
              }}
            >
              <Icon name="pen" size={12} />
              {agent.env_keys.length ? tr('重新设置', 'Replace') : tr('添加', 'Add')}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

/** 一个内置模板：装没装、怎么装、怎么登录、一键添加。 */
function TemplateRow({
  template,
  adding,
  onAdd,
}: {
  template: AcpAgentTemplate;
  adding: boolean;
  onAdd: () => void;
}) {
  const desc = TEMPLATE_DESCRIPTIONS[template.id];
  // 安装、登录命令默认收起：六七个模板全展开时，这一节会比整页其余部分还长
  const [howOpen, setHowOpen] = useState(false);
  return (
    <div className="st-row st-row-stack" style={{ gap: 4 }}>
      <div className="row gap8" style={{ alignItems: 'center', flexWrap: 'wrap' }}>
        <span className="st-row-label" style={template.installed ? undefined : { color: 'var(--text-3)' }}>{template.name}</span>
        <StatusDot tone={template.installed ? 'ok' : 'idle'}>
          {template.installed ? tr('已安装', 'Installed') : tr('未安装', 'Not installed')}
        </StatusDot>
        <span style={{ flex: 1 }} />
        {!template.installed && (template.install || template.login) && (
          <button className="btn btn-ghost sm" onClick={() => setHowOpen((o) => !o)}>
            {howOpen ? tr('收起', 'Hide') : tr('安装方法', 'How to install')}
          </button>
        )}
        <button className="btn btn-ghost sm" disabled={adding} onClick={onAdd}>
          <Icon name="plus" size={12} />
          {tr('添加', 'Add')}
        </button>
      </div>
      <div className="st-row-hint">
        {desc ? tr(desc.zh, desc.en) : template.description}
      </div>
      {!template.installed && howOpen && (
        <div className="col" style={{ gap: 6, marginTop: 8 }}>
          {template.install && <CopyLine label={tr('安装', 'Install')} text={template.install} />}
          {template.login && <CopyLine label={tr('登录', 'Sign in')} text={template.login} />}
        </div>
      )}
    </div>
  );
}

/** 自定义智能体：任何说 ACP 的命令都行。 */
function CustomAgentForm({ taken, onDone }: { taken: string[]; onDone: () => void }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState('');
  const [slug, setSlug] = useState('');
  const [slugTouched, setSlugTouched] = useState(false);
  const [command, setCommand] = useState('');
  const [args, setArgs] = useState('');
  const [envRows, setEnvRows] = useState<EnvRow[]>([]);
  const [policy, setPolicy] = useState<AcpPermissionPolicy>(DEFAULT_POLICY);

  const effectiveSlug = slugTouched ? slug.trim() : slugify(name);
  const slugBad = !SLUG_RE.test(effectiveSlug);
  const envIssue = envRows.some((r) => envRowIssue(r, envRows));

  const create = useMutation({
    mutationFn: () =>
      api.createAcpAgent({
        slug: effectiveSlug,
        name: name.trim() || undefined,
        template: 'custom',
        command: command.trim(),
        args: splitArgs(args),
        env: envRowsPayload(envRows),
        permission_policy: policy,
      }),
    onSuccess: () => {
      invalidateAfterAgentChange(queryClient);
      toast(tr('已添加', 'Added'), 'ok');
      onDone();
    },
    onError: (e) => toast(`${tr('添加失败', 'Couldn’t add')}：${errText(e)}`, 'error'),
  });

  return (
    <div style={{ marginTop: 12 }}>
      <div className="settings-fields">
        <FormField label={tr('名称', 'Name')}>
          <input className="input" value={name} placeholder="My agent" onChange={(e) => setName(e.target.value)} />
        </FormField>
        <FormField
          label={tr('标识', 'ID')}
          hint={tr('小写字母、数字、- 和 _', 'Lowercase letters, digits, - and _')}
          error={
            effectiveSlug && slugBad
              ? tr('格式不对', 'Invalid format')
              : taken.includes(effectiveSlug)
                ? tr('已被使用', 'Already taken')
                : null
          }
        >
          <input
            className="input mono"
            value={effectiveSlug}
            placeholder="my-agent"
            spellCheck={false}
            onChange={(e) => {
              setSlugTouched(true);
              setSlug(e.target.value);
            }}
          />
        </FormField>
        <FormField label={tr('启动命令', 'Command')} hint={tr('命令名或完整路径', 'Command name or full path')}>
          <input
            className="input mono"
            value={command}
            placeholder="my-agent-acp"
            spellCheck={false}
            onChange={(e) => setCommand(e.target.value)}
          />
        </FormField>
        <FormField label={tr('参数', 'Arguments')} hint={tr('用空格分隔', 'Separate with spaces')}>
          <input
            className="input mono"
            value={args}
            placeholder="--acp"
            spellCheck={false}
            onChange={(e) => setArgs(e.target.value)}
          />
        </FormField>
        <FormField label={tr('权限', 'Permissions')}>
          <SelectMenu value={policy} options={policyOptions()} onChange={(v) => setPolicy(v as AcpPermissionPolicy)} />
          <PolicyHint policy={POLICY_OPTIONS.find((o) => o.value === policy)} />
        </FormField>
      </div>
      <div className="field">
        <label className="field-label">{tr('环境变量', 'Environment variables')}</label>
        <EnvRowsEditor rows={envRows} onChange={setEnvRows} />
        <div className="field-hint">
          {tr('保存后只显示变量名，不再显示值。', 'After saving, only names are shown.')}
        </div>
      </div>
      <div className="row" style={{ justifyContent: 'flex-end', marginTop: 10 }}>
        <button
          className="btn btn-primary"
          disabled={!command.trim() || slugBad || taken.includes(effectiveSlug) || envIssue || create.isPending}
          onClick={() => create.mutate()}
        >
          {create.isPending ? tr('添加中…', 'Adding…') : tr('添加', 'Add')}
        </button>
      </div>
    </div>
  );
}

export function AcpAgentsSettings() {
  const queryClient = useQueryClient();
  const agentsQ = useQuery({ queryKey: AGENTS_KEY, queryFn: () => api.listAcpAgents(), retry: false });
  const templatesQ = useQuery({ queryKey: TEMPLATES_KEY, queryFn: () => api.listAcpTemplates(), retry: false });
  const [customOpen, setCustomOpen] = useState(false);
  const agents = agentsQ.data ?? [];
  const taken = agents.map((a) => a.slug);
  // 路由表：标出谁是默认模型 / 谁在接管；取不到就不标、不给按钮
  const routesQ = useQuery({ queryKey: ['llm', 'routes'], queryFn: () => api.getLlmRoutes(), retry: false });
  const routes = routesQ.data;
  const defaultRoute = routes?.find((r) => r.stage === 'default');
  const takeover = routes ? takeoverAgent(!!defaultRoute, agents) : null;
  const roleOf = (a: AcpAgentRead): 'default' | 'takeover' | null =>
    defaultRoute?.acp_agent_id === a.id ? 'default' : takeover?.id === a.id ? 'takeover' : null;

  const makeDefault = useMutation({
    // PUT 是整表覆盖：拿当前整表，只换掉「默认」那一行
    mutationFn: (a: AcpAgentRead) => api.putLlmRoutes(withDefaultAgent(routes ?? [], a.id)),
    onSuccess: (_, a) => {
      // 路由表那边没保存的改动不会被冲掉：它按行合并服务端的新表（rebaseDrafts）
      void queryClient.invalidateQueries({ queryKey: ['llm'] });
      toast(tr(`已将「${a.name || a.slug}」设为默认`, `“${a.name || a.slug}” is now the default`), 'ok');
    },
    onError: (e) => toast(`${tr('设置失败', 'Couldn’t set the default')}：${errText(e)}`, 'error'),
  });

  const addTemplate = useMutation({
    mutationFn: (t: AcpAgentTemplate) => createDeduped({ slug: t.id, template: t.id }, taken),
    onSuccess: (row) => {
      invalidateAfterAgentChange(queryClient);
      toast(tr(`已添加 ${row.name}`, `Added ${row.name}`), 'ok');
    },
    onError: (e) => toast(`${tr('添加失败', 'Couldn’t add')}：${errText(e)}`, 'error'),
  });

  return (
    <>
      <SettingsSection
        title={tr('智能体', 'Agents')}
        desc={tr(
          '让你已安装的 Claude Code、Codex 等智能体来回答，它用你自己的登录在本机运行。',
          'Let an agent you already use, like Claude Code or Codex, answer. It runs on this computer with your own sign-in.',
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
          <SettingsRow label={tr('还没有智能体', 'No agents yet')} hint={tr('在下方添加一个', 'Add one below')} />
        ) : (
          agents.map((a) => (
            <AgentRow
              key={a.id}
              agent={a}
              modelRole={roleOf(a)}
              onMakeDefault={routes ? () => makeDefault.mutate(a) : undefined}
              makingDefault={makeDefault.isPending}
            />
          ))
        )}
        </SettingsGroup>
      </SettingsSection>

      <SettingsSection
        title={tr('添加智能体', 'Add an agent')}
        desc={tr('未安装的请先在终端里安装并登录一次。', 'If it isn’t installed, install it and sign in once in a terminal first.')}
      >
        <SettingsGroup>
        {templatesQ.isLoading ? (
          <div className="st-row"><span className="st-row-hint">{tr('加载中…', 'Loading…')}</span></div>
        ) : templatesQ.isError ? (
          <div className="st-row"><span className="st-row-hint">{tr('无法加载可添加的智能体', 'Couldn’t load the agent list')}</span></div>
        ) : (
          (templatesQ.data ?? []).map((t) => (
            <TemplateRow
              key={t.id}
              template={t}
              adding={addTemplate.isPending && addTemplate.variables?.id === t.id}
              onAdd={() => addTemplate.mutate(t)}
            />
          ))
        )}

        <div className="st-row st-row-stack" style={{ gap: 4 }}>
          <div className="row gap8" style={{ alignItems: 'center' }}>
            <span className="st-row-label">{tr('自定义智能体', 'Custom agent')}</span>
            <span style={{ flex: 1, fontSize: 12, color: 'var(--text-3)' }}>
              {tr('任何支持 ACP 协议的命令', 'Any command that supports ACP')}
            </span>
            <button className="btn btn-ghost sm" onClick={() => setCustomOpen((o) => !o)}>
              <Icon name={customOpen ? 'minus' : 'plus'} size={12} />
              {customOpen ? tr('收起', 'Hide') : tr('填写', 'Set up')}
            </button>
          </div>
          {customOpen && <CustomAgentForm taken={taken} onDone={() => setCustomOpen(false)} />}
        </div>
        </SettingsGroup>
      </SettingsSection>
    </>
  );
}
