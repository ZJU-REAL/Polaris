import { useMemo, useState, type CSSProperties } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Segmented } from '../../components/ui/Segmented';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { tr } from '../../lib/i18n';
import { portalUrl } from '../../lib/endpoint';
import { copyText } from '../../lib/clipboard';
import { useProject } from '../../app/project';
import { api, getToken, type McpToolCheck, type McpToolInfo } from '../../lib/api';
import { ToolRunner } from './ToolRunner';
import { McpPlayground } from './McpPlayground';

function copy(text: string) {
  void copyText(text).then((ok) =>
    ok ? toast(tr('已复制', 'Copied'), 'ok') : toast(tr('复制失败', 'Copy failed'), 'error'),
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

const CHECK_STYLE: Record<string, CSSProperties> = {
  ok: { background: 'var(--ok-bg)', color: 'var(--ok-tx)' },
  error: { background: 'var(--danger-bg)', color: 'var(--danger-tx)' },
  skipped: { background: 'var(--surface-3)', color: 'var(--text-3)' },
};

function checkLabel(check: McpToolCheck): string {
  if (check.status === 'ok') return `${tr('正常', 'OK')} · ${check.duration_ms ?? 0} ms`;
  if (check.status === 'error') return tr('失效', 'Broken');
  return tr('未测', 'Not tested');
}

/** 标签 + 可复制的等宽值行；secret 时可隐藏（token）。 */
function CopyRow({ label, value, secret }: { label: string; value: string; secret?: boolean }) {
  const [shown, setShown] = useState(!secret);
  const display = shown ? value || '—' : '•'.repeat(Math.min(44, value.length || 8));
  return (
    <div>
      <div className="h-eyebrow">{label}</div>
      <div className="row gap8" style={{ marginTop: 6 }}>
        <code
          style={{
            ...CODE_BOX,
            flex: 1,
            minWidth: 0,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {display}
        </code>
        {secret && (
          <button className="btn btn-ghost sm" onClick={() => setShown((s) => !s)}>
            {shown ? tr('隐藏', 'Hide') : tr('显示', 'Show')}
          </button>
        )}
        <button className="btn btn-soft sm" onClick={() => copy(value)} disabled={!value}>
          <Icon name="file" /> {tr('复制', 'Copy')}
        </button>
      </div>
    </div>
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
    <div className="card" style={{ padding: '13px 15px', display: 'grid', gap: 8, minWidth: 0 }}>
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
          className="pill sm"
          title={
            t.network
              ? tr('会请求库外的文献接口', 'Calls external literature APIs')
              : tr('只查本平台库内数据', 'Reads only this platform’s own data')
          }
          style={
            t.network
              ? { background: 'var(--warn-bg)', color: 'var(--warn-tx)' }
              : { background: 'var(--info-bg)', color: 'var(--info-tx)' }
          }
        >
          {t.network ? tr('联网', 'Network') : tr('库内', 'Library')}
        </span>
        {check && (
          <span className="pill sm" style={CHECK_STYLE[check.status]} title={check.detail ?? undefined}>
            {checkLabel(check)}
          </span>
        )}
        <span
          className="pill sm"
          style={{ marginLeft: 'auto', background: 'var(--surface-3)', color: 'var(--text-3)' }}
        >
          {tr('只读', 'read-only')}
        </span>
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
          <Icon name={open ? 'chevDown' : 'play'} />
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

  const origin = portalUrl();
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
    onError: (e: Error) => toast(tr('自检失败：', 'Self-check failed: ') + e.message, 'error'),
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
    <div>
      <div style={{ fontSize: 12.5, color: 'var(--text-3)', lineHeight: 1.55, marginBottom: 14, maxWidth: 720 }}>
        {tr(
          '把平台的只读检索能力（文献 / 概念 / 知识 / 课题状态）暴露为 MCP 工具，供 Claude Desktop、Cursor 等外部客户端调用。',
          'Expose the platform’s read-only retrieval (papers / concepts / knowledge / topic state) as MCP tools for external clients like Claude Desktop and Cursor.',
        )}
      </div>

      {/* 接入信息 */}
      <div
        className="card"
        style={{
          padding: '16px 18px',
          display: 'grid',
          gridTemplateColumns: 'minmax(0, 1fr)',
          gap: 14,
          marginBottom: 18,
        }}
      >
        <div className="row gap8" style={{ alignItems: 'center' }}>
          <Icon name="server" />
          <strong style={{ fontSize: 14 }}>{tr('接入信息', 'Connection')}</strong>
          {data && (
            <span className="pill sm" style={{ marginLeft: 'auto', background: 'var(--surface-3)', color: 'var(--text-3)' }}>
              {data.server.name} · MCP {data.protocol_version}
            </span>
          )}
        </div>
        <CopyRow label={tr('HTTP 端点（Streamable HTTP）', 'HTTP endpoint (Streamable HTTP)')} value={httpUrl} />
        <CopyRow label={tr('访问令牌（你的登录 Bearer token）', 'Access token (your bearer token)')} value={token} secret />
        <div style={{ minWidth: 0 }}>
          <div className="h-eyebrow">{tr('客户端配置（Cursor / 支持 HTTP 的客户端）', 'Client config (Cursor / HTTP clients)')}</div>
          <pre
            style={{
              ...CODE_BOX,
              margin: '6px 0 0',
              padding: 12,
              maxWidth: '100%',
              overflowX: 'auto',
              lineHeight: 1.55,
            }}
          >
            {httpConfig}
          </pre>
          <div style={{ marginTop: 8 }}>
            <button className="btn btn-soft sm" onClick={() => copy(httpConfig)}>
              <Icon name="file" /> {tr('复制配置', 'Copy config')}
            </button>
          </div>
        </div>
        <div style={{ fontSize: 12.5, color: 'var(--text-3)', lineHeight: 1.55 }}>
          {tr(
            '调用工具时需在参数里带 project_id（目标课题 uuid），服务端会校验你是否为该课题成员。本地桌面客户端也可用 stdio：python -m app.mcp（详见 docs/concepts.md）。',
            'Each tool call takes a project_id (target topic uuid); the server verifies your membership. Local desktop clients can also use stdio: python -m app.mcp (see docs/concepts.md).',
          )}
        </div>
      </div>

      {/* 工具测试：选课题 → 一键自检（用课题里的真实数据把每个工具跑一遍） */}
      <div className="card" style={{ padding: '14px 16px', display: 'grid', gap: 10, marginBottom: 18 }}>
        <div className="row wrap gap8" style={{ alignItems: 'center' }}>
          <Icon name="shield" />
          <strong style={{ fontSize: 14 }}>{tr('工具测试', 'Tool test')}</strong>
          <span style={{ fontSize: 12, color: 'var(--text-3)' }}>
            {tr(
              '用课题里的真实数据跑一遍，看哪些工具还能用',
              'Runs every tool against real data from a topic to see what still works',
            )}
          </span>
        </div>
        <div className="row wrap gap8" style={{ alignItems: 'center' }}>
          <select
            className="input"
            style={{ height: 34, maxWidth: 280 }}
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
          <label className="row gap6" style={{ fontSize: 12.5, color: 'var(--text-2)', cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={includeNetwork}
              onChange={(e) => setIncludeNetwork(e.target.checked)}
            />
            {tr(
              '连联网工具一起测（会真的请求外部接口，慢一些）',
              'Include network tools (really calls external APIs, slower)',
            )}
          </label>
          <button
            className="btn btn-primary sm"
            onClick={() => selfcheck.mutate()}
            disabled={!projectId || selfcheck.isPending}
          >
            <Icon name="refresh" />
            {selfcheck.isPending ? tr('自检中…', 'Checking…') : tr('一键自检', 'Run self-check')}
          </button>
        </div>
        {report && (
          <div className="row wrap gap8" style={{ alignItems: 'center', fontSize: 12.5 }}>
            <span className="pill sm" style={CHECK_STYLE.ok}>
              {tr('正常', 'OK')} {report.summary.ok}
            </span>
            <span className="pill sm" style={report.summary.error ? CHECK_STYLE.error : CHECK_STYLE.skipped}>
              {tr('失效', 'Broken')} {report.summary.error}
            </span>
            <span className="pill sm" style={CHECK_STYLE.skipped}>
              {tr('未测', 'Not tested')} {report.summary.skipped}
            </span>
            <span style={{ color: 'var(--text-3)' }}>
              {tr('共', 'of')} {report.summary.total} {tr('个工具', 'tools')}
            </span>
          </div>
        )}
        {report && report.summary.skipped > 0 && (
          <div style={{ fontSize: 11.5, color: 'var(--text-3)', lineHeight: 1.55 }}>
            {tr(
              '「未测」= 课题里缺少对应的样本数据（比如还没有稿件），或该工具需要联网而没有勾选，不代表工具有问题。',
              '“Not tested” means the topic lacks sample data (e.g. no manuscript yet) or the tool needs network access you didn’t enable — not a failure.',
            )}
          </div>
        )}
      </div>

      {/* 工具目录 / 调试台 */}
      <div className="row wrap gap8" style={{ marginBottom: 14, alignItems: 'center' }}>
        <Segmented
          options={[
            { v: 'catalog', label: tr('工具目录', 'Tool catalog') },
            { v: 'playground', label: tr('调试台', 'Playground') },
          ]}
          value={view}
          onChange={setView}
        />
        {data && (
          <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-3)' }}>
            {tools.length}
          </span>
        )}
        {view === 'catalog' && (
          <div style={{ marginLeft: 'auto' }}>
            <Segmented
              options={[
                { v: 'all', label: tr('全部', 'All') },
                { v: 'library', label: tr('库内', 'Library') },
                { v: 'network', label: tr('联网', 'Network') },
                ...(report && report.summary.error > 0
                  ? [{ v: 'failed' as const, label: tr('失效', 'Broken') }]
                  : []),
              ]}
              value={filter}
              onChange={setFilter}
            />
          </div>
        )}
      </div>

      {isLoading && <EmptyState icon="server" title={tr('加载中…', 'Loading…')} />}
      {isError && <EmptyState icon="server" title={tr('加载失败', 'Failed to load')} />}
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
    </div>
  );
}
