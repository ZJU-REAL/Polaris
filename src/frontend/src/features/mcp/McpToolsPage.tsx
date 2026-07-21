import { useMemo, useState, type CSSProperties } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { Segmented } from '../../components/ui/Segmented';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { tr } from '../../lib/i18n';
import { api, getToken, type McpToolInfo } from '../../lib/api';

function copy(text: string) {
  navigator.clipboard
    .writeText(text)
    .then(() => toast(tr('已复制', 'Copied'), 'ok'))
    .catch(() => toast(tr('复制失败', 'Copy failed'), 'error'));
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

function ToolCard({ t }: { t: McpToolInfo }) {
  return (
    <div className="card" style={{ padding: '13px 15px', display: 'grid', gap: 8 }}>
      <div className="row gap8" style={{ alignItems: 'center' }}>
        <code style={{ fontFamily: 'var(--mono)', fontSize: 13, fontWeight: 600, color: 'var(--text)' }}>
          {t.name}
        </code>
        <span
          className="pill sm"
          style={
            t.network
              ? { background: 'var(--warn-bg)', color: 'var(--warn-tx)' }
              : { background: 'var(--info-bg)', color: 'var(--info-tx)' }
          }
        >
          {t.network ? tr('联网', 'Network') : tr('库内', 'Library')}
        </span>
        <span className="pill sm" style={{ marginLeft: 'auto', background: 'var(--surface-3)', color: 'var(--text-3)' }}>
          {tr('只读', 'read-only')}
        </span>
      </div>
      <div style={{ fontSize: 13, color: 'var(--text-2)', lineHeight: 1.5 }}>{t.description}</div>
      {t.params.length > 0 && (
        <div className="row" style={{ flexWrap: 'wrap', gap: 6 }}>
          {t.params.map((p) => (
            <span
              key={p.name}
              className="tag"
              title={p.description ?? undefined}
              style={{ fontFamily: 'var(--mono)', fontSize: 11, fontWeight: p.required ? 600 : 500 }}
            >
              {p.name}
              {p.required ? '*' : ''}
              <span style={{ color: 'var(--text-3)', marginLeft: 3 }}>
                {p.enum ? p.enum.join('|') : p.type}
              </span>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

export function McpToolsPage() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['mcp-tools'],
    queryFn: () => api.listMcpTools(),
    retry: false,
  });
  const [filter, setFilter] = useState<'all' | 'library' | 'network'>('all');

  const origin = typeof window !== 'undefined' ? window.location.origin : '';
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

  const tools = data?.tools ?? [];
  const shown = tools.filter((t) =>
    filter === 'all' ? true : filter === 'network' ? t.network : !t.network,
  );

  return (
    <div className="page fadeup">
      <PageHead
        eyebrow="Polaris"
        title={tr('MCP 工具', 'MCP Tools')}
        sub={tr(
          '把平台的只读检索能力（文献 / 概念 / 知识 / 项目状态）暴露为 MCP 工具，供 Claude Desktop、Cursor 等外部客户端调用。',
          'Expose the platform’s read-only retrieval (papers / concepts / knowledge / project state) as MCP tools for external clients like Claude Desktop and Cursor.',
        )}
      />

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
            '调用工具时需在参数里带 project_id（目标项目 uuid），服务端会校验你是否为该项目成员。本地桌面客户端也可用 stdio：python -m app.mcp（详见 docs/api-mcp.md）。',
            'Each tool call takes a project_id (target project uuid); the server verifies your membership. Local desktop clients can also use stdio: python -m app.mcp (see docs/api-mcp.md).',
          )}
        </div>
      </div>

      {/* 工具目录 */}
      <div className="row gap8" style={{ marginBottom: 14, alignItems: 'center' }}>
        <strong style={{ fontSize: 14 }}>{tr('工具目录', 'Tool catalog')}</strong>
        {data && <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-3)' }}>{tools.length}</span>}
        <div style={{ marginLeft: 'auto' }}>
          <Segmented
            options={[
              { v: 'all', label: tr('全部', 'All') },
              { v: 'library', label: tr('库内', 'Library') },
              { v: 'network', label: tr('联网', 'Network') },
            ]}
            value={filter}
            onChange={setFilter}
          />
        </div>
      </div>

      {isLoading && <EmptyState icon="server" title={tr('加载中…', 'Loading…')} />}
      {isError && <EmptyState icon="server" title={tr('加载失败', 'Failed to load')} />}
      {data && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))', gap: 12 }}>
          {shown.map((t) => (
            <ToolCard key={t.name} t={t} />
          ))}
        </div>
      )}
    </div>
  );
}
