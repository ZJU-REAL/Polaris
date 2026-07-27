import { useEffect, useMemo, useState, type CSSProperties } from 'react';
import { useMutation } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { tr } from '../../lib/i18n';
import { api, type McpInvokeResult, type McpToolInfo, type McpToolParam } from '../../lib/api';

/* ============================================================
   单个 MCP 工具的试运行面板：填参数 → 运行 → 看外部客户端会收到的内容。
   走 POST /mcp/tools/{name}/invoke，与真实 MCP 调用同一条路径。

   表单控件、参数转换与结果渲染在这里定义并导出，调试台（McpPlayground）复用同一套，
   保证「卡片里试一下」和「调试台里试一下」跑出来的东西逐字一致。
   ============================================================ */

export const RESULT_BOX: CSSProperties = {
  fontFamily: 'var(--mono)',
  fontSize: 11.5,
  lineHeight: 1.55,
  background: 'var(--surface-3)',
  border: '0.5px solid var(--border)',
  borderRadius: 'var(--radius-sm)',
  padding: '8px 10px',
  margin: 0,
  maxHeight: 260,
  overflow: 'auto',
  whiteSpace: 'pre-wrap',
  overflowWrap: 'anywhere',
  color: 'var(--text-2)',
};

/** 把表单里的字符串按参数类型还原成 JSON 值；空串 = 不传该参数。 */
export function coerce(params: McpToolParam[], raw: Record<string, string>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const p of params) {
    const value = (raw[p.name] ?? '').trim();
    if (!value) continue;
    if (p.type === 'boolean') out[p.name] = value === 'true';
    else if (p.type === 'integer' || p.type === 'number') {
      const n = Number(value);
      if (!Number.isNaN(n)) out[p.name] = n;
    } else out[p.name] = value;
  }
  return out;
}

/** 自检给出的样例参数 → 表单初值（数字/布尔转成字符串）。 */
export function toFormValues(args: Record<string, unknown> | null | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(args ?? {})) {
    if (v === null || v === undefined) continue;
    out[k] = typeof v === 'boolean' ? String(v) : String(v);
  }
  return out;
}

export function ParamField({
  param,
  value,
  onChange,
}: {
  param: McpToolParam;
  value: string;
  onChange: (v: string) => void;
}) {
  const hint = param.description ?? '';
  const label = (
    <div style={{ fontSize: 11.5, color: 'var(--text-3)', display: 'flex', gap: 5, alignItems: 'baseline' }}>
      <code style={{ fontFamily: 'var(--mono)', fontWeight: param.required ? 600 : 500, color: 'var(--text-2)' }}>
        {param.name}
        {param.required ? '*' : ''}
      </code>
      {hint && <span style={{ minWidth: 0, overflowWrap: 'anywhere' }}>{hint}</span>}
    </div>
  );

  const options = param.type === 'boolean' ? ['true', 'false'] : param.enum;
  return (
    <label style={{ display: 'grid', gap: 4, minWidth: 0 }}>
      {label}
      {options ? (
        <select className="input mono" style={{ height: 32 }} value={value} onChange={(e) => onChange(e.target.value)}>
          <option value="">{tr('（不传）', '(omit)')}</option>
          {options.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      ) : (
        <input
          className="input mono"
          style={{ height: 32 }}
          type={param.type === 'integer' || param.type === 'number' ? 'number' : 'text'}
          value={value}
          placeholder={param.required ? tr('必填', 'required') : tr('可选', 'optional')}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
    </label>
  );
}

/** 结果区：文本块原样展示，图片块转 data URL 预览。 */
export function ResultView({ result }: { result: McpInvokeResult }) {
  const texts = result.content.filter((b) => b.type === 'text');
  const images = result.content.filter((b) => b.type === 'image' && b.data);
  return (
    <div style={{ display: 'grid', gap: 8, minWidth: 0 }}>
      <div className="row gap8" style={{ fontSize: 11.5 }}>
        <span
          className="pill sm"
          style={
            result.is_error
              ? { background: 'var(--danger-bg)', color: 'var(--danger-tx)' }
              : { background: 'var(--ok-bg)', color: 'var(--ok-tx)' }
          }
        >
          {result.is_error ? tr('调用失败', 'Failed') : tr('调用成功', 'Success')}
        </span>
        <span style={{ color: 'var(--text-3)' }}>{result.duration_ms} ms</span>
        {images.length > 0 && (
          <span style={{ color: 'var(--text-3)' }}>
            {tr('图片', 'Images')} × {images.length}
          </span>
        )}
        {result.truncated && (
          <span style={{ color: 'var(--text-3)' }}>{tr('（内容已截断）', '(truncated)')}</span>
        )}
      </div>
      {texts.map((b, i) => (
        <pre key={i} style={RESULT_BOX}>
          {b.text}
        </pre>
      ))}
      {images.length > 0 && (
        <div className="row wrap gap8">
          {images.map((b, i) => (
            <img
              key={i}
              src={`data:${b.mimeType || 'image/png'};base64,${b.data}`}
              alt={`figure-${i}`}
              style={{
                maxWidth: 180,
                maxHeight: 140,
                objectFit: 'contain',
                borderRadius: 'var(--radius-sm)',
                border: '0.5px solid var(--border)',
                background: 'var(--surface)',
              }}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export function ToolRunner({
  tool,
  projectId,
  sampleArgs,
}: {
  tool: McpToolInfo;
  projectId: string | null;
  /** 自检跑出来的样例参数，用作表单初值（省得手填 uuid）。 */
  sampleArgs?: Record<string, unknown> | null;
}) {
  const [values, setValues] = useState<Record<string, string>>(() => toFormValues(sampleArgs));
  const sampleKey = useMemo(() => JSON.stringify(sampleArgs ?? null), [sampleArgs]);

  // 自检跑完拿到样例参数后，把还空着的表单填上（不覆盖用户已经改过的值）
  useEffect(() => {
    const next = toFormValues(sampleArgs);
    if (!Object.keys(next).length) return;
    setValues((prev) => {
      const merged = { ...prev };
      for (const [k, v] of Object.entries(next)) if (!merged[k]) merged[k] = v;
      return merged;
    });
  }, [sampleKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const run = useMutation({
    mutationFn: () =>
      api.invokeMcpTool(tool.name, {
        project_id: projectId as string,
        arguments: coerce(tool.params, values),
      }),
  });

  return (
    <div
      style={{
        display: 'grid',
        gap: 10,
        minWidth: 0,
        borderTop: '0.5px dashed var(--border)',
        paddingTop: 10,
      }}
    >
      {tool.params.length > 0 && (
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
      )}
      <div className="row gap8" style={{ flexWrap: 'wrap' }}>
        <button
          className="btn btn-primary sm"
          onClick={() => run.mutate()}
          disabled={!projectId || run.isPending}
        >
          <Icon name="play" /> {run.isPending ? tr('运行中…', 'Running…') : tr('运行', 'Run')}
        </button>
        {tool.network && (
          <span style={{ fontSize: 11.5, color: 'var(--warn-tx)' }}>
            {tr('会真的请求外部文献接口', 'Really calls the external literature API')}
          </span>
        )}
        {!projectId && (
          <span style={{ fontSize: 11.5, color: 'var(--text-3)' }}>
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
      {run.data && <ResultView result={run.data} />}
    </div>
  );
}
