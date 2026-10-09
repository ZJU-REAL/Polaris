import { useMemo, useState, type CSSProperties } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Segmented } from '../../components/ui/Segmented';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { tr } from '../../lib/i18n';
import { engineOrigin, localOrigin } from '../../lib/endpoint';
import { copyText } from '../../lib/clipboard';
import { useProject } from '../../app/project';
import { api, getToken, type McpToolCheck, type McpToolInfo } from '../../lib/api';
import { ToolRunner } from './ToolRunner';
import { McpPlayground } from './McpPlayground';
import { SettingsGroup, SettingsRow, SettingsSection, SettingsStack, StatusDot } from '../settings/settingsUi';

function copy(text: string) {
  void copyText(text).then((ok) =>
    ok ? toast(tr('已复制', 'Copied'), 'ok') : toast(tr('无法复制，请手动复制', 'Couldn’t copy. Copy it manually.'), 'error'),
  );
}

const CODE_BOX: CSSProperties = {
  fontFamily: 'var(--mono)',
  fontSize: 12.5,
  background: 'var(--surface-3)',
  border: '0.5px solid var(--border)',
  borderRadius: 'var(--radius-sm)',
  padding: '8px 10px',
  color: 'var(--text)',
};

/** 参数 chip：长枚举必须能换行，否则会顶破卡片（.tag 本身是定高 inline-flex + 不换行）。 */
const PARAM_CHIP: CSSProperties = {
  display: 'inline-block',
  height: 'auto',
  maxWidth: '100%',
  padding: '2px 8px',
  lineHeight: 1.5,
  whiteSpace: 'normal',
  overflowWrap: 'anywhere',
  fontFamily: 'var(--mono)',
  fontSize: 11,
};

function checkLabel(check: McpToolCheck): string {
  if (check.status === 'ok') return tr('通过', 'Passed');
  if (check.status === 'error') return tr('失败', 'Failed');
  return tr('未测试', 'Not tested');
}

/** 可复制的等宽值；secret 时可隐藏（token）。 */
function CopyValue({ value, secret }: { value: string; secret?: boolean }) {
  const [shown, setShown] = useState(!secret);
  const display = shown ? value || '—' : '•'.repeat(Math.min(24, value.length || 8));
  return (
    <>
      <code
        style={{
          ...CODE_BOX,
          minWidth: 0,
          maxWidth: 320,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
        title={shown ? value : undefined}
      >
        {display}
      </code>
      {secret && (
        <button className="btn btn-ghost sm" onClick={() => setShown((s) => !s)}>
          {shown ? tr('隐藏', 'Hide') : tr('显示', 'Show')}
        </button>
      )}
      <button className="btn btn-ghost sm" onClick={() => copy(value)} disabled={!value}>
        {tr('复制', 'Copy')}
      </button>
    </>
  );
}

function ToolCard({
  t,
  check,
  projectId,
}: {
  t: McpToolInfo;
  check?: McpToolCheck;
  projectId: string | null;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="st-card" style={{ padding: '12px 14px', display: 'grid', gap: 8, minWidth: 0 }}>
      <div className="row wrap gap8" style={{ alignItems: 'center', minWidth: 0 }}>
        <code
          style={{
            fontFamily: 'var(--mono)',
            fontSize: 13,
            fontWeight: 600,
            color: 'var(--text)',
            overflowWrap: 'anywhere',
          }}
        >
          {t.name}
        </code>
        <span
          style={{ fontSize: 12, color: 'var(--text-3)' }}
          title={
            t.network
              ? tr('会联网查询外部文献库', 'Queries external literature sources online')
              : tr('只读取本机数据', 'Reads only data on this computer')
          }
        >
          {t.network ? tr('联网', 'Online') : tr('本地', 'Local')}
        </span>
        {check && (
          <span style={{ marginLeft: 'auto' }}>
            <StatusDot tone={check.status === 'ok' ? 'ok' : check.status === 'error' ? 'err' : 'idle'} title={check.detail ?? undefined}>
              {checkLabel(check)}
            </StatusDot>
          </span>
        )}
      </div>
      <div style={{ fontSize: 13, color: 'var(--text-2)', lineHeight: 1.5, overflowWrap: 'anywhere' }}>
        {t.description}
      </div>
      {t.params.length > 0 && (
        <div className="row wrap" style={{ gap: 6, minWidth: 0 }}>
          {t.params.map((p) => (
            <span
              key={p.name}
              className="tag"
              title={p.description ?? undefined}
              style={{ ...PARAM_CHIP, fontWeight: p.required ? 600 : 500 }}
            >
              {p.name}
              {p.required ? '*' : ''}
              <span style={{ color: 'var(--text-3)', marginLeft: 3 }}>
                {p.enum ? p.enum.join(' | ') : p.type}
              </span>
            </span>
          ))}
        </div>
      )}
      {check?.status === 'error' && check.detail && (
        <div
          style={{
            fontSize: 11.5,
            color: 'var(--danger-tx)',
            background: 'var(--danger-bg)',
            borderRadius: 'var(--radius-sm)',
            padding: '6px 8px',
            overflowWrap: 'anywhere',
          }}
        >
          {check.detail}
        </div>
      )}
      {check?.status === 'skipped' && check.detail && (
        <div style={{ fontSize: 11.5, color: 'var(--text-3)', overflowWrap: 'anywhere' }}>
          {check.detail}
        </div>
      )}
      <div>
        <button className="btn btn-ghost sm" onClick={() => setOpen((v) => !v)}>
          {open ? tr('收起', 'Hide') : tr('试运行', 'Try it')}
        </button>
      </div>
      {open && <ToolRunner tool={t} projectId={projectId} sampleArgs={check?.arguments} />}
    </div>
  );
}

/** MCP 接入说明主体（无页头），由设置页的「MCP 接入」页签复用。 */
export function McpToolsContent() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['mcp-tools'],
    queryFn: () => api.listMcpTools(),
    retry: false,
  });
  const [filter, setFilter] = useState<'all' | 'library' | 'network' | 'failed'>('all');
  const [view, setView] = useState<'catalog' | 'playground'>('catalog');

  const { projects, currentProjectId } = useProject();
  const [pickedProjectId, setPickedProjectId] = useState<string | null>(null);
  const projectId = pickedProjectId ?? currentProjectId;
  const [includeNetwork, setIncludeNetwork] = useState(false);

  // 端点给出本机引擎的真实地址（外部 MCP 客户端与桌面端同机，127.0.0.1
  // 恰好就是对的），并附一行「仅本机可访问」说明。
  const isLocalEndpoint = localOrigin() != null;
  const origin = engineOrigin();
  const httpUrl = `${origin}${data?.endpoint ?? '/mcp'}`;
  const token = getToken() ?? '';

  const httpConfig = useMemo(
    () =>
      JSON.stringify(
        {
          mcpServers: {
            polaris: { url: httpUrl, headers: { Authorization: `Bearer ${token || '<TOKEN>'}` } },
          },
        },
        null,
        2,
      ),
    [httpUrl, token],
  );

  const selfcheck = useMutation({
    mutationFn: () =>
      api.selfCheckMcpTools({ project_id: projectId as string, include_network: includeNetwork }),
    onError: (e: Error) => toast(tr('测试失败：', 'Test failed: ') + e.message, 'error'),
  });
  const report = selfcheck.data;
  const checks = useMemo(() => {
    const map = new Map<string, McpToolCheck>();
    for (const r of report?.results ?? []) map.set(r.name, r);
    return map;
  }, [report]);

  const tools = data?.tools ?? [];
  const shown = tools.filter((t) => {
    if (filter === 'network') return t.network;
    if (filter === 'library') return !t.network;
    if (filter === 'failed') return checks.get(t.name)?.status === 'error';
    return true;
  });

  return (
    <SettingsStack>
      <div className="st-page">
      <SettingsSection
        title={tr('连接', 'Connection')}
        desc={tr(
          '让 Claude Desktop、Cursor 等 MCP 客户端检索你的文献、概念和课题（只读）。',
          'Let MCP clients like Claude Desktop and Cursor search your papers, concepts and topics (read-only).',
        )}
      >
        <SettingsGroup>
          <SettingsRow
            label={tr('地址', 'URL')}
            hint={isLocalEndpoint ? tr('仅这台电脑上的程序可以访问', 'Only apps on this computer can reach it') : undefined}
          >
            <CopyValue value={httpUrl} />
          </SettingsRow>
          <SettingsRow label={tr('访问令牌', 'Access token')}>
            <CopyValue value={token} secret />
          </SettingsRow>
          <SettingsRow stack label={tr('客户端配置', 'Client config')} hint={tr('粘贴到 Cursor 等支持 HTTP 的客户端', 'Paste into Cursor or another client that supports HTTP')}>
            <div style={{ minWidth: 0 }}>
              <pre
                style={{
                  ...CODE_BOX,
                  margin: 0,
                  padding: 12,
                  maxWidth: '100%',
                  overflowX: 'auto',
                  lineHeight: 1.55,
                }}
              >
                {httpConfig}
              </pre>
              <div style={{ marginTop: 8 }}>
                <button className="btn btn-ghost sm" onClick={() => copy(httpConfig)}>
                  {tr('复制配置', 'Copy config')}
                </button>
              </div>
            </div>
          </SettingsRow>
        </SettingsGroup>
      </SettingsSection>

      <SettingsSection
        title={tr('工具测试', 'Tool test')}
        desc={tr('用课题里的数据把每个工具运行一遍，看哪些能用。', 'Runs every tool on a topic’s data to see which work.')}
      >
        <SettingsGroup>
          <SettingsRow label={tr('课题', 'Topic')}>
            <select
              className="input"
              value={projectId ?? ''}
              onChange={(e) => setPickedProjectId(e.target.value || null)}
            >
              <option value="">{tr('选择课题…', 'Pick a topic…')}</option>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </SettingsRow>
          <SettingsRow
            labelId="mcp-include-network"
            label={tr('包括联网工具', 'Include online tools')}
            hint={tr('会查询外部文献库，较慢', 'Queries external sources, so it’s slower')}
          >
            <input
              type="checkbox"
              aria-labelledby="mcp-include-network"
              checked={includeNetwork}
              onChange={(e) => setIncludeNetwork(e.target.checked)}
            />
          </SettingsRow>
          {report && (
            <SettingsRow
              label={tr(`共 ${report.summary.total} 个工具`, `${report.summary.total} tools`)}
              hint={report.summary.skipped > 0
                ? tr('「未测试」表示课题缺少相应数据或未包括联网工具', '“Not tested” means the topic lacks data or online tools were left out')
                : undefined}
            >
              <StatusDot tone="ok">{tr(`${report.summary.ok} 个通过`, `${report.summary.ok} passed`)}</StatusDot>
              <StatusDot tone={report.summary.error ? 'err' : 'idle'}>{tr(`${report.summary.error} 个失败`, `${report.summary.error} failed`)}</StatusDot>
              <StatusDot tone="idle">{tr(`${report.summary.skipped} 个未测试`, `${report.summary.skipped} not tested`)}</StatusDot>
            </SettingsRow>
          )}
        </SettingsGroup>
        <div className="st-actions">
          <button
            className="btn btn-primary sm"
            onClick={() => selfcheck.mutate()}
            disabled={!projectId || selfcheck.isPending}
          >
            {selfcheck.isPending ? tr('测试中…', 'Testing…') : tr('开始测试', 'Run test')}
          </button>
        </div>
      </SettingsSection>
      </div>

      <SettingsSection
        title={data ? tr(`工具（${tools.length}）`, `Tools (${tools.length})`) : tr('工具', 'Tools')}
        actions={
          <>
            {view === 'catalog' && (
              <Segmented
                options={[
                  { v: 'all', label: tr('全部', 'All') },
                  { v: 'library', label: tr('本地', 'Local') },
                  { v: 'network', label: tr('联网', 'Online') },
                  ...(report && report.summary.error > 0
                    ? [{ v: 'failed' as const, label: tr('失败', 'Failed') }]
                    : []),
                ]}
                value={filter}
                onChange={setFilter}
              />
            )}
            <Segmented
              options={[
                { v: 'catalog', label: tr('列表', 'List') },
                { v: 'playground', label: tr('调试', 'Playground') },
              ]}
              value={view}
              onChange={setView}
            />
          </>
        }
      >
      {isLoading && <EmptyState icon="server" title={tr('加载中…', 'Loading…')} />}
      {isError && <EmptyState icon="server" title={tr('无法加载工具', 'Couldn’t load tools')} />}
      {data && view === 'playground' && (
        <McpPlayground tools={tools} projectId={projectId} checks={checks} />
      )}
      {data && view === 'catalog' && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(min(320px, 100%), 1fr))', gap: 12 }}>
          {shown.map((t) => (
            <ToolCard key={t.name} t={t} check={checks.get(t.name)} projectId={projectId} />
          ))}
        </div>
      )}
      </SettingsSection>
    </SettingsStack>
  );
}
