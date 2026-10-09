import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { FormField } from '../../components/ui/FormField';
import { Switch } from '../../components/ui/Switch';
import { SelectMenu } from '../../components/ui/SelectMenu';
import { toast } from '../../components/ui/Toast';
import { copyText } from '../../lib/clipboard';
import { fmtTime } from '../../lib/format';
import { tr } from '../../lib/i18n';
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
            toast(ok ? tr('已复制', 'Copied') : tr('复制失败，请手动复制', 'Could not copy — copy it manually'), ok ? 'ok' : 'error'),
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
                placeholder={tr('值', 'value')}
                spellCheck={false}
                autoComplete="new-password"
                aria-label={tr('变量值', 'Variable value')}
                onChange={(e) => onChange(rows.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))}
              />
              <button
                className="btn btn-ghost sm"
                title={tr('删除这一行', 'Remove this row')}
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
    void queryClient.invalidateQueries({ queryKey: AGENTS_KEY });
    // 助手面板里的「谁来答」跟着变
    void queryClient.invalidateQueries({ queryKey: ['chat-backends'] });
  };

  const update = useMutation({
    mutationFn: (input: AcpAgentUpdate) => api.updateAcpAgent(agent.id, input),
    onSuccess: (row) => refresh(row),
    onError: (e) => toast(`${tr('保存失败', 'Save failed')}：${errText(e)}`, 'error'),
  });
  const probe = useMutation({
    mutationFn: () => api.probeAcpAgent(agent.id),
    onSuccess: (row) => {
      refresh(row);
      if (row.last_error) toast(tr('连不上，原因见下方', 'Could not connect — see below'), 'error');
      else toast(tr('连接正常', 'Connected'), 'ok');
    },
    onError: (e) => toast(`${tr('检查失败', 'Check failed')}：${errText(e)}`, 'error'),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteAcpAgent(agent.id),
    onSuccess: () => {
      queryClient.setQueryData<AcpAgentRead[]>(AGENTS_KEY, (old) => old?.filter((a) => a.id !== agent.id));
      refresh();
      toast(tr('已删除', 'Removed'), 'ok');
    },
    onError: (e) => toast(`${tr('删除失败', 'Delete failed')}：${errText(e)}`, 'error'),
  });

  const policy = POLICY_OPTIONS.find((o) => o.value === agent.permission_policy);
  const envIssue = envRows.some((r) => envRowIssue(r, envRows));
  const probeInfo = agent.last_probe;
  const caps = probeInfo ? probeCapabilities(probeInfo) : [];

  return (
    <div style={{ padding: '14px 0', borderTop: '0.5px solid var(--border)' }}>
      <div className="row gap8" style={{ alignItems: 'center', flexWrap: 'wrap' }}>
        <strong style={{ fontSize: 14 }}>{agent.name || agent.slug}</strong>
        <span className="pill sm">{agent.template === 'custom' ? tr('自定义', 'Custom') : agent.template}</span>
        {!agent.enabled && <span className="pill sm">{tr('已停用', 'Off')}</span>}
        {modelRole === 'default' && (
          <span className="pill sm" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
            {tr('默认模型', 'Default model')}
          </span>
        )}
        {modelRole === 'takeover' && (
          <span
            className="pill sm"
            style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}
            title={tr('没有设置默认模型，所有对话类环节都由它回答', 'No default model is set, so it answers every chat stage')}
          >
            {tr('正在接管所有模型调用', 'Answering all model calls')}
          </span>
        )}
        <span style={{ flex: 1 }} />
        {onMakeDefault && agent.enabled && modelRole !== 'default' && (
          <button
            className="btn btn-soft sm"
            disabled={makingDefault}
            title={tr('让没单独设置的对话类环节都交给它回答', 'Every chat stage without its own row will be answered by it')}
            onClick={onMakeDefault}
          >
            <Icon name="star" size={12} />
            {tr('设为默认模型', 'Use as default model')}
          </button>
        )}
        <button className="btn btn-soft sm" disabled={probe.isPending} onClick={() => probe.mutate()}>
          <Icon name="refresh" size={12} style={probe.isPending ? { animation: 'spin 1s linear infinite' } : undefined} />
          {probe.isPending ? tr('检查中…', 'Checking…') : tr('检查连接', 'Check connection')}
        </button>
        {confirmDelete ? (
          <>
            <span style={{ fontSize: 12, color: 'var(--danger-tx)' }}>{tr('确定删除？', 'Remove it?')}</span>
            <button className="btn btn-danger sm" disabled={remove.isPending} onClick={() => remove.mutate()}>
              {tr('删除', 'Remove')}
            </button>
            <button className="btn btn-ghost sm" onClick={() => setConfirmDelete(false)}>
              {tr('取消', 'Cancel')}
            </button>
          </>
        ) : (
          <button className="btn btn-ghost sm" title={tr('删除', 'Remove')} onClick={() => setConfirmDelete(true)}>
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
            `在这台机器上找不到命令「${agent.command}」——先装好再用。`,
            `Command "${agent.command}" was not found on this machine — install it first.`,
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
          {tr('还没检查过连接。', 'Not checked yet.')}
        </div>
      )}

      <div className="settings-fields" style={{ marginTop: 12 }}>
        <FormField
          label={tr('权限', 'Permissions')}
          hint={tr('智能体想读文件、改文件或跑命令时，按这一条回答它。', 'How to answer when the agent asks to read, edit or run something.')}
        >
          <SelectMenu
            value={agent.permission_policy}
            options={policyOptions()}
            disabled={update.isPending}
            onChange={(v) => update.mutate({ permission_policy: v as AcpPermissionPolicy })}
          />
          {policy?.warn && (
            <div style={{ fontSize: 12, color: 'var(--warn-tx)', marginTop: 4 }}>
              {tr('它会不经确认就改文件、跑命令。只在你信得过的环境里用。', 'It will edit files and run commands without asking. Only use this where you trust it.')}
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
              aria-label={tr('启用', 'Enabled')}
            />
            {tr('启用（在助手里可选）', 'Enabled (selectable in the assistant)')}
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
                '保存会整体替换原来的变量（旧值读不回来）。全部删掉再保存就是清空。',
                'Saving replaces all existing variables (old values cannot be read back). Remove every row and save to clear them.',
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
                        toast(tr('环境变量已更新', 'Environment updated'), 'ok');
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
              <span style={{ fontSize: 12, color: 'var(--text-4)' }}>{tr('没有设置', 'None set')}</span>
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
  return (
    <div style={{ padding: '12px 0', borderTop: '0.5px solid var(--border)' }}>
      <div className="row gap8" style={{ alignItems: 'center', flexWrap: 'wrap' }}>
        <strong style={{ fontSize: 13.5 }}>{template.name}</strong>
        {template.installed ? (
          <span className="pill sm st-implemented">{tr('已安装', 'Installed')}</span>
        ) : (
          <span className="pill sm st-candidate">{tr('未安装', 'Not installed')}</span>
        )}
        <span style={{ flex: 1 }} />
        <button className="btn btn-soft sm" disabled={adding} onClick={onAdd}>
          <Icon name="plus" size={12} />
          {tr('添加', 'Add')}
        </button>
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 4, lineHeight: 1.6 }}>
        {desc ? tr(desc.zh, desc.en) : template.description}
      </div>
      {!template.installed && (
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
      void queryClient.invalidateQueries({ queryKey: AGENTS_KEY });
      void queryClient.invalidateQueries({ queryKey: ['chat-backends'] });
      toast(tr('已添加', 'Added'), 'ok');
      onDone();
    },
    onError: (e) => toast(`${tr('添加失败', 'Could not add')}：${errText(e)}`, 'error'),
  });

  return (
    <div style={{ marginTop: 12 }}>
      <div className="settings-fields">
        <FormField label={tr('名称', 'Name')}>
          <input className="input" value={name} placeholder="My agent" onChange={(e) => setName(e.target.value)} />
        </FormField>
        <FormField
          label={tr('标识', 'ID')}
          hint={tr('小写字母、数字、- 和 _，不能重复。', 'Lowercase letters, digits, - and _; must be unique.')}
          error={
            effectiveSlug && slugBad
              ? tr('格式不对', 'Invalid format')
              : taken.includes(effectiveSlug)
                ? tr('已经被用了', 'Already taken')
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
        <FormField label={tr('启动命令', 'Command')} hint={tr('可执行文件名或完整路径。', 'Executable name or full path.')}>
          <input
            className="input mono"
            value={command}
            placeholder="my-agent-acp"
            spellCheck={false}
            onChange={(e) => setCommand(e.target.value)}
          />
        </FormField>
        <FormField label={tr('参数', 'Arguments')} hint={tr('用空格分隔。', 'Separated by spaces.')}>
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
          {tr('保存后只显示变量名，值不会再显示。', 'After saving only the names are shown; values are never displayed again.')}
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
      void queryClient.invalidateQueries({ queryKey: ['llm', 'routes'] });
      toast(tr(`默认模型已改为「${a.name || a.slug}」`, `Default model is now "${a.name || a.slug}"`), 'ok');
    },
    onError: (e) => toast(`${tr('设置失败', 'Could not set it')}：${errText(e)}`, 'error'),
  });

  const addTemplate = useMutation({
    mutationFn: (t: AcpAgentTemplate) => createDeduped({ slug: t.id, template: t.id }, taken),
    onSuccess: (row) => {
      void queryClient.invalidateQueries({ queryKey: AGENTS_KEY });
      void queryClient.invalidateQueries({ queryKey: ['chat-backends'] });
      toast(tr(`已添加 ${row.name}`, `Added ${row.name}`), 'ok');
    },
    onError: (e) => toast(`${tr('添加失败', 'Could not add')}：${errText(e)}`, 'error'),
  });

  return (
    <>
      <div className="card card-pad">
        <div className="section-h" style={{ marginBottom: 4 }}>
          <Icon name="cpu" size={15} style={{ color: 'var(--accent)' }} />
          {tr('智能体后端', 'Agent backends')}{' '}
          <span className="en-label" style={{ fontSize: 11 }}>{tr('推荐', 'recommended')}</span>
        </div>
        <div style={{ fontSize: 12, color: 'var(--text-3)', lineHeight: 1.6, marginBottom: 4 }}>
          {tr(
            '用你已经在用的智能体（Claude Code、Codex、Gemini CLI……）回答模型调用，通过 Agent Client Protocol 连接。它在这台机器上用你自己的登录运行，Polaris 拿不到它的密钥。有一个启用的智能体就够了：没设置默认模型时，它会回答所有对话类环节；在助手输入框上方也能直接选它。',
            'Use an agent you already have (Claude Code, Codex, Gemini CLI…) to answer model calls, connected through the Agent Client Protocol. It runs on this machine with your own sign-in; Polaris never sees its keys. One enabled agent is enough: with no default model set, it answers every chat stage, and you can also pick it just above the assistant’s input box.',
          )}
        </div>
        {agentsQ.isLoading ? (
          <div className="empty">{tr('加载中…', 'Loading…')}</div>
        ) : agentsQ.isError ? (
          <div className="empty">
            {tr('无法加载智能体列表', 'Could not load the agents')}
            <div style={{ marginTop: 10 }}>
              <button className="btn btn-soft sm" onClick={() => void agentsQ.refetch()}>
                {tr('重试', 'Retry')}
              </button>
            </div>
          </div>
        ) : agents.length === 0 ? (
          <div style={{ fontSize: 12.5, color: 'var(--text-4)', padding: '8px 0' }}>
            {tr('还没有。从下面挑一个添加。', 'None yet. Add one from below.')}
          </div>
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
      </div>

      <div className="card card-pad" style={{ marginTop: 16 }}>
        <div className="section-h" style={{ marginBottom: 4 }}>
          <Icon name="plus" size={15} style={{ color: 'var(--accent)' }} />
          {tr('添加智能体', 'Add an agent')}
        </div>
        <div style={{ fontSize: 12, color: 'var(--text-3)', lineHeight: 1.6, marginBottom: 4 }}>
          {tr(
            '没装的先在终端里装好并登录一次，再回来添加、检查连接。',
            'If it is not installed yet, install it and sign in once in a terminal, then add it here and check the connection.',
          )}
        </div>
        {templatesQ.isLoading ? (
          <div className="empty">{tr('加载中…', 'Loading…')}</div>
        ) : templatesQ.isError ? (
          <div className="empty">{tr('无法加载模板', 'Could not load templates')}</div>
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

        <div style={{ paddingTop: 12, borderTop: '0.5px solid var(--border)' }}>
          <div className="row gap8" style={{ alignItems: 'center' }}>
            <strong style={{ fontSize: 13.5 }}>{tr('自定义智能体', 'Custom agent')}</strong>
            <span style={{ flex: 1, fontSize: 12, color: 'var(--text-3)' }}>
              {tr('任何支持 ACP 的命令。', 'Any command that speaks ACP.')}
            </span>
            <button className="btn btn-ghost sm" onClick={() => setCustomOpen((o) => !o)}>
              <Icon name={customOpen ? 'minus' : 'plus'} size={12} />
              {customOpen ? tr('收起', 'Hide') : tr('填写', 'Set up')}
            </button>
          </div>
          {customOpen && <CustomAgentForm taken={taken} onDone={() => setCustomOpen(false)} />}
        </div>
      </div>
    </>
  );
}
