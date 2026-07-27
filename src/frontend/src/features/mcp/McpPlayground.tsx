import { useEffect, useMemo, useState, type CSSProperties } from 'react';
import { useMutation } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Segmented } from '../../components/ui/Segmented';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { tr } from '../../lib/i18n';
import { copyText } from '../../lib/clipboard';
import { api, type McpInvokeResult, type McpToolCheck, type McpToolInfo } from '../../lib/api';
import { ParamField, ResultView, RESULT_BOX, coerce, toFormValues } from './ToolRunner';

/* ============================================================
   MCP 调试台：挑一个工具 → 填参数（表单或直接写 JSON）→ 运行 → 看返回。

   和工具卡片里的「试运行」是同一条路径（POST /mcp/tools/{name}/invoke →
   dispatch.call_tool），这里多的是：工具搜索、原始 JSON 入参、可复制的
   JSON-RPC 请求体（拿去自己的客户端里对拍）、以及本次会话的调用历史。
   ============================================================ */

const LABEL: CSSProperties = { fontSize: 11.5, color: 'var(--text-3)', marginBottom: 5 };

interface HistoryEntry {
  id: number;
  tool: string;
  args: Record<string, unknown>;
  result: McpInvokeResult;
  at: string;
}

/** 工具返回的文本多半是 JSON：能解析就缩进美化，解析不了就原样。 */
function prettify(result: McpInvokeResult): McpInvokeResult {
  return {
    ...result,
    content: result.content.map((b) => {
      if (b.type !== 'text' || !b.text) return b;
      try {
        return { ...b, text: JSON.stringify(JSON.parse(b.text), null, 2) };
      } catch {
        return b;
      }
    }),
  };
}

/** 外部 MCP 客户端发的那条 tools/call 请求（project_id 由服务端注入，这里显式写出来）。 */
function rpcRequest(tool: string, projectId: string | null, args: Record<string, unknown>): string {
  return JSON.stringify(
    {
      jsonrpc: '2.0',
      id: 1,
      method: 'tools/call',
      params: { name: tool, arguments: { project_id: projectId ?? '<PROJECT_ID>', ...args } },
    },
    null,
    2,
  );
}

function ToolListItem({
  tool,
  check,
  active,
  onPick,
}: {
  tool: McpToolInfo;
  check?: McpToolCheck;
  active: boolean;
  onPick: () => void;
}) {
  const dot =
    check?.status === 'ok'
      ? 'var(--ok-tx)'
      : check?.status === 'error'
        ? 'var(--danger-tx)'
        : 'var(--text-4)';
  return (
    <button
      onClick={onPick}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 7,
        width: '100%',
        textAlign: 'left',
        padding: '6px 8px',
        borderRadius: 'var(--radius-sm)',
        border: 'none',
        cursor: 'pointer',
        background: active ? 'var(--accent-soft)' : 'transparent',
        color: active ? 'var(--accent-text)' : 'var(--text-2)',
        fontFamily: 'var(--mono)',
        fontSize: 12,
        fontWeight: active ? 600 : 500,
        minWidth: 0,
      }}
    >
      {check && (
        <span
          title={check.detail ?? undefined}
          style={{ width: 6, height: 6, borderRadius: '50%', background: dot, flexShrink: 0 }}
        />
      )}
      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {tool.name}
      </span>
      {tool.network && (
        <span
          className="pill sm"
          style={{ marginLeft: 'auto', background: 'var(--warn-bg)', color: 'var(--warn-tx)' }}
        >
          {tr('联网', 'Net')}
        </span>
      )}
    </button>
  );
}

export function McpPlayground({
  tools,
  projectId,
  checks,
}: {
  tools: McpToolInfo[];
  projectId: string | null;
  /** 自检结果：给工具列表打状态点，并给参数表单提供样例初值。 */
  checks: Map<string, McpToolCheck>;
}) {
  const [q, setQ] = useState('');
  const [picked, setPicked] = useState<string | null>(null);
  const [mode, setMode] = useState<'form' | 'json'>('form');
  const [values, setValues] = useState<Record<string, string>>({});
  const [jsonText, setJsonText] = useState('{}');
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [result, setResult] = useState<McpInvokeResult | null>(null);

  const tool = useMemo(
    () => tools.find((t) => t.name === picked) ?? tools[0] ?? null,
    [tools, picked],
  );
  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return tools;
    return tools.filter(
      (t) =>
        t.name.toLowerCase().includes(needle) || t.description.toLowerCase().includes(needle),
    );
  }, [tools, q]);

  // 换工具：用自检的样例参数当初值，清掉上一个工具的结果
  useEffect(() => {
    if (!tool) return;
    const sample = toFormValues(checks.get(tool.name)?.arguments);
    setValues(sample);
    setJsonText(JSON.stringify(coerce(tool.params, sample), null, 2));
    setJsonError(null);
    setResult(null);
  }, [tool?.name]); // eslint-disable-line react-hooks/exhaustive-deps

  /** 当前入参：表单模式按类型转换，JSON 模式解析文本框（写坏了给 null）。纯函数，渲染期可用。 */
  const args = useMemo<Record<string, unknown> | null>(() => {
    if (!tool) return null;
    if (mode === 'form') return coerce(tool.params, values);
    try {
      const parsed: unknown = JSON.parse(jsonText || '{}');
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
      return parsed as Record<string, unknown>;
    } catch {
      return null;
    }
  }, [tool, mode, values, jsonText]);

  const run = useMutation({
    mutationFn: async () => {
      if (!tool || args === null) {
        setJsonError(tr('入参必须是合法的 JSON 对象', 'Arguments must be a valid JSON object'));
        throw new Error(tr('入参有误', 'Invalid arguments'));
      }
      setJsonError(null);
      const raw = await api.invokeMcpTool(tool.name, {
        project_id: projectId as string,
        arguments: args,
      });
      return { raw: prettify(raw), args } as { raw: McpInvokeResult; args: Record<string, unknown> };
    },
    onSuccess: ({ raw, args }) => {
      setResult(raw);
      setHistory((prev) =>
        [
          {
            id: (prev[0]?.id ?? 0) + 1,
            tool: raw.name,
            args,
            result: raw,
            at: new Date().toLocaleTimeString(),
          },
          ...prev,
        ].slice(0, 20),
      );
    },
  });

  /** 从历史里回放一次调用：入参回填表单，结果直接显示。 */
  const replay = (entry: HistoryEntry) => {
    setPicked(entry.tool);
    setValues(toFormValues(entry.args));
    setJsonText(JSON.stringify(entry.args, null, 2));
    setResult(entry.result);
  };

  const copyRpc = () => {
    if (!tool || args === null) {
      setJsonError(tr('入参必须是合法的 JSON 对象', 'Arguments must be a valid JSON object'));
      return;
    }
    void copyText(rpcRequest(tool.name, projectId, args)).then((ok) =>
      toast(ok ? tr('已复制请求', 'Request copied') : tr('复制失败', 'Copy failed'), ok ? 'ok' : 'error'),
    );
  };

  if (!tool) return <EmptyState icon="server" title={tr('没有可用的工具', 'No tools available')} />;

  const check = checks.get(tool.name);

  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: 'minmax(0, 220px) minmax(0, 1fr)',
        gap: 14,
        alignItems: 'start',
      }}
      className="mcp-playground"
    >
      {/* 左：工具清单 */}
      <div className="card" style={{ padding: 10, display: 'grid', gap: 8, minWidth: 0 }}>
        <input
          className="input"
          style={{ height: 30, fontSize: 12 }}
          placeholder={tr('搜工具…', 'Search tools…')}
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <div style={{ display: 'grid', gap: 2, maxHeight: 420, overflowY: 'auto' }} className="scroll">
          {shown.map((t) => (
            <ToolListItem
              key={t.name}
              tool={t}
              check={checks.get(t.name)}
              active={t.name === tool.name}
              onPick={() => setPicked(t.name)}
            />
          ))}
          {shown.length === 0 && (
            <div style={{ fontSize: 11.5, color: 'var(--text-3)', padding: '6px 8px' }}>
              {tr('没有匹配的工具', 'No matching tool')}
            </div>
          )}
        </div>
      </div>

      {/* 右：入参 → 运行 → 结果 */}
      <div style={{ display: 'grid', gap: 14, minWidth: 0 }}>
        <div className="card" style={{ padding: '14px 16px', display: 'grid', gap: 12, minWidth: 0 }}>
          <div className="row wrap gap8" style={{ alignItems: 'center', minWidth: 0 }}>
            <code style={{ fontFamily: 'var(--mono)', fontSize: 14, fontWeight: 600 }}>{tool.name}</code>
            <span
              className="pill sm"
              style={
                tool.network
                  ? { background: 'var(--warn-bg)', color: 'var(--warn-tx)' }
                  : { background: 'var(--info-bg)', color: 'var(--info-tx)' }
              }
            >
              {tool.network ? tr('联网', 'Network') : tr('库内', 'Library')}
            </span>
            {check?.status === 'ok' && (
              <span className="pill sm" style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>
                {tr('自检正常', 'Self-check OK')}
              </span>
            )}
            {check?.status === 'error' && (
              <span className="pill sm" style={{ background: 'var(--danger-bg)', color: 'var(--danger-tx)' }}>
                {tr('自检失效', 'Self-check broken')}
              </span>
            )}
            <div style={{ marginLeft: 'auto' }}>
              <Segmented
                options={[
                  { v: 'form', label: tr('表单', 'Form') },
                  { v: 'json', label: 'JSON' },
                ]}
                value={mode}
                onChange={(v) => {
                  // 表单 → JSON：把当前表单值序列化过去，两边始终看到同一组入参
                  if (v === 'json') setJsonText(JSON.stringify(coerce(tool.params, values), null, 2));
                  else {
                    try {
                      setValues(toFormValues(JSON.parse(jsonText || '{}')));
                    } catch {
                      /* JSON 写坏了就保留表单原值 */
                    }
                  }
                  setMode(v);
                }}
              />
            </div>
          </div>
          <div style={{ fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.5 }}>{tool.description}</div>

          {mode === 'form' ? (
            tool.params.length ? (
              <div style={{ display: 'grid', gap: 8, minWidth: 0 }}>
                {tool.params.map((p) => (
                  <ParamField
                    key={p.name}
                    param={p}
                    value={values[p.name] ?? ''}
                    onChange={(v) => setValues((prev) => ({ ...prev, [p.name]: v }))}
                  />
                ))}
              </div>
            ) : (
              <div style={{ fontSize: 12, color: 'var(--text-3)' }}>
                {tr('这个工具不需要参数。', 'This tool takes no arguments.')}
              </div>
            )
          ) : (
            <div style={{ minWidth: 0 }}>
              <div style={LABEL}>
                {tr('入参 JSON（project_id 由服务端注入，不用写）', 'Arguments JSON (project_id is injected server-side)')}
              </div>
              <textarea
                className="textarea mono"
                style={{ width: '100%', minHeight: 130, fontSize: 12 }}
                value={jsonText}
                spellCheck={false}
                onChange={(e) => {
                  setJsonText(e.target.value);
                  setJsonError(null);
                }}
              />
              {jsonError && (
                <div style={{ fontSize: 11.5, color: 'var(--danger-tx)', marginTop: 4 }}>{jsonError}</div>
              )}
            </div>
          )}

          <div className="row wrap gap8">
            <button
              className="btn btn-primary sm"
              onClick={() => run.mutate()}
              disabled={!projectId || run.isPending}
            >
              <Icon name="play" /> {run.isPending ? tr('运行中…', 'Running…') : tr('运行', 'Run')}
            </button>
            <button className="btn btn-soft sm" onClick={copyRpc}>
              <Icon name="file" /> {tr('复制 JSON-RPC 请求', 'Copy JSON-RPC request')}
            </button>
            {tool.network && (
              <span style={{ fontSize: 11.5, color: 'var(--warn-tx)', alignSelf: 'center' }}>
                {tr('会真的请求外部文献接口', 'Really calls the external literature API')}
              </span>
            )}
            {!projectId && (
              <span style={{ fontSize: 11.5, color: 'var(--text-3)', alignSelf: 'center' }}>
                {tr('先在上方选一个课题', 'Pick a topic above first')}
              </span>
            )}
          </div>
          {run.isError && (
            <div style={{ fontSize: 12, color: 'var(--danger-tx)' }}>
              {tr('请求失败：', 'Request failed: ')}
              {(run.error as Error).message}
            </div>
          )}

          <div style={{ minWidth: 0 }}>
            <div style={LABEL}>
              {tr(
                '外部 MCP 客户端要发的就是这条请求',
                'This is the request an external MCP client would send',
              )}
            </div>
            <pre style={{ ...RESULT_BOX, maxHeight: 180 }}>
              {rpcRequest(tool.name, projectId, args ?? {})}
            </pre>
          </div>
        </div>

        {result && (
          <div className="card" style={{ padding: '14px 16px', display: 'grid', gap: 10, minWidth: 0 }}>
            <div className="row gap8" style={{ alignItems: 'center' }}>
              <strong style={{ fontSize: 13 }}>{tr('返回内容', 'Response')}</strong>
              <span style={{ fontSize: 11.5, color: 'var(--text-3)' }}>
                {tr('外部客户端收到的就是这些', 'This is exactly what a client receives')}
              </span>
            </div>
            <ResultView result={result} />
          </div>
        )}

        {history.length > 0 && (
          <div className="card" style={{ padding: '14px 16px', display: 'grid', gap: 8, minWidth: 0 }}>
            <div className="row gap8" style={{ alignItems: 'center' }}>
              <strong style={{ fontSize: 13 }}>{tr('调用记录', 'Call history')}</strong>
              <span style={{ fontSize: 11.5, color: 'var(--text-3)' }}>
                {tr('点一条可回放入参与结果', 'Click one to replay its arguments and result')}
              </span>
              <button
                className="btn btn-ghost sm"
                style={{ marginLeft: 'auto' }}
                onClick={() => setHistory([])}
              >
                {tr('清空', 'Clear')}
              </button>
            </div>
            <div style={{ display: 'grid', gap: 4 }}>
              {history.map((h) => (
                <button
                  key={h.id}
                  onClick={() => replay(h)}
                  className="row gap8"
                  style={{
                    alignItems: 'center',
                    padding: '5px 8px',
                    borderRadius: 'var(--radius-sm)',
                    border: '0.5px solid var(--border)',
                    background: 'var(--surface-2)',
                    cursor: 'pointer',
                    textAlign: 'left',
                    minWidth: 0,
                  }}
                >
                  <span
                    style={{
                      width: 6,
                      height: 6,
                      borderRadius: '50%',
                      background: h.result.is_error ? 'var(--danger-tx)' : 'var(--ok-tx)',
                      flexShrink: 0,
                    }}
                  />
                  <code style={{ fontFamily: 'var(--mono)', fontSize: 11.5, color: 'var(--text-2)' }}>
                    {h.tool}
                  </code>
                  <span
                    style={{
                      fontSize: 11,
                      color: 'var(--text-3)',
                      fontFamily: 'var(--mono)',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                      minWidth: 0,
                    }}
                  >
                    {JSON.stringify(h.args)}
                  </span>
                  <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-3)', flexShrink: 0 }}>
                    {h.result.duration_ms} ms · {h.at}
                  </span>
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
