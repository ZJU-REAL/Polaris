import type { AcpPermissionPolicy, AcpProbeInfo } from '../../lib/api';

/* ============================================================
   「智能体后端」设置页（#836）的纯逻辑：拆参数、环境变量行、slug 去重、文案表。
   文案只存中英两份，tr() 在组件里调用。
   ============================================================ */

export interface ZhEn {
  zh: string;
  en: string;
}

/** 与后端 SLUG_PATTERN 同一条 */
export const SLUG_RE = /^[a-z0-9][a-z0-9_-]*$/;
const ENV_KEY_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** 权限策略：从严到宽。auto 会改文件、跑命令，界面上用警示色。 */
export const POLICY_OPTIONS: { value: AcpPermissionPolicy; label: ZhEn; warn?: boolean }[] = [
  {
    value: 'deny',
    label: { zh: '拒绝所有需要授权的操作', en: 'Refuse anything that needs permission' },
  },
  {
    value: 'read_only',
    label: { zh: '只允许读取和搜索', en: 'Allow reading and searching only' },
  },
  {
    value: 'auto',
    label: { zh: '自动允许（会改文件、跑命令）', en: 'Allow automatically (it may edit files and run commands)' },
    warn: true,
  },
];

/** 模板说明的中文版；后端只给英文。没收录的模板就用后端那句。 */
export const TEMPLATE_DESCRIPTIONS: Record<string, ZhEn> = {
  'claude-code': {
    zh: 'Anthropic 的编程智能体，经官方 ACP 适配器接入。',
    en: "Anthropic's coding agent, through the official ACP adapter.",
  },
  codex: {
    zh: 'OpenAI 的编程智能体，经官方 ACP 适配器接入。',
    en: "OpenAI's coding agent, through the official ACP adapter.",
  },
  gemini: {
    zh: 'Google 的开源智能体，原生支持 ACP。',
    en: "Google's open-source agent; speaks ACP natively.",
  },
  qwen: {
    zh: '通义千问的编程智能体（Gemini CLI 的分支），原生支持 ACP。',
    en: "Qwen's coding agent (a Gemini CLI fork); speaks ACP natively.",
  },
  opencode: {
    zh: '开源智能体，可接多家模型服务。',
    en: 'Open-source agent that works with many model providers.',
  },
  kimi: {
    zh: '月之暗面的命令行智能体，原生支持 ACP。',
    en: "Moonshot's command-line agent; speaks ACP natively.",
  },
};

/** 参数输入框是一整行字符串，按空白拆开。（不处理引号：真有带空格的参数就别用这个框。） */
export function splitArgs(raw: string): string[] {
  return raw.split(/\s+/).filter(Boolean);
}

export function joinCommand(command: string, args: string[] | null | undefined): string {
  return [command, ...(args ?? [])].join(' ');
}

export interface EnvRow {
  key: string;
  value: string;
}

/** 一行环境变量的问题；没问题返回 null。名字留空的行保存时直接丢掉，不算错。 */
export function envRowIssue(row: EnvRow, all: EnvRow[]): ZhEn | null {
  const key = row.key.trim();
  if (!key) return null;
  if (!ENV_KEY_RE.test(key)) {
    return { zh: '名字只能用字母、数字和下划线，且不能以数字开头', en: 'Letters, digits and underscores only; cannot start with a digit' };
  }
  if (all.filter((r) => r.key.trim() === key).length > 1) return { zh: '名字重复了', en: 'Duplicate name' };
  return null;
}

/** 环境变量行 → 提交给后端的 {KEY: VALUE}；名字为空的行丢掉。 */
export function envRowsPayload(rows: EnvRow[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const r of rows) {
    const key = r.key.trim();
    if (key) out[key] = r.value;
  }
  return out;
}

/** 名字 → 一个合法的 slug（小写、非法字符换成连字符）；拼不出来就回退到 fallback。 */
export function slugify(name: string, fallback = 'agent'): string {
  const s = name
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^[-_]+|[-_]+$/g, '')
    .slice(0, 64);
  return SLUG_RE.test(s) ? s : fallback;
}

/** base 被占了就依次试 base-2、base-3……返回第一个没被占的。 */
export function freeSlug(base: string, taken: Iterable<string>): string {
  const used = new Set(taken);
  if (!used.has(base)) return base;
  for (let n = 2; ; n += 1) {
    const candidate = `${base}-${n}`;
    if (!used.has(candidate)) return candidate;
  }
}

/** 探测结果 → 一行行给人看的能力说明。 */
export function probeCapabilities(probe: AcpProbeInfo): ZhEn[] {
  const out: ZhEn[] = [];
  if (probe.load_session) out.push({ zh: '能接着上次的对话继续', en: 'Can resume earlier sessions' });
  if (probe.mcp_http || probe.mcp_sse) out.push({ zh: '能使用 Polaris 的工具（MCP）', en: 'Can use Polaris tools (MCP)' });
  if (probe.prompt_image) out.push({ zh: '能看图片', en: 'Accepts images' });
  return out;
}

/** 后端错误码 → 一句人话；认不出的返回 null，由调用方原样透出。 */
export function acpErrorText(detail: string): ZhEn | null {
  if (detail.startsWith('SLUG_TAKEN')) return { zh: '这个标识已经被用了，换一个', en: 'That ID is already taken — pick another' };
  if (detail.startsWith('BAD_ENV_KEY')) return { zh: '环境变量名不合法', en: 'An environment variable name is invalid' };
  if (detail.startsWith('COMMAND_REQUIRED')) return { zh: '要填启动命令', en: 'A command is required' };
  if (detail.startsWith('BAD_PERMISSION_POLICY')) return { zh: '权限策略不认识', en: 'Unknown permission policy' };
  if (detail.startsWith('UNKNOWN_TEMPLATE')) return { zh: '模板不存在', en: 'Unknown template' };
  return null;
}

/** 探测到的 agent 名称 + 版本，如「Claude Code 1.2.0」。 */
export function probeTitle(probe: AcpProbeInfo): string {
  return [probe.title || probe.name, probe.version].filter(Boolean).join(' ');
}
