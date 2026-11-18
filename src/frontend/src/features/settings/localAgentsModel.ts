import type { AcpAgentRead, AcpAgentTemplate, AcpPermissionPolicy, AcpProbeInfo } from '../../lib/api';
import type { DotTone } from './settingsUi';

/* ============================================================
   设置 → 本机智能体 的纯逻辑：状态、权限说明、模板说明、参数与环境变量、标识去重。
   文案只存中英两份，tr() 在组件里调用（模块顶层不调 tr）。
   ============================================================ */

export interface ZhEn {
  zh: string;
  en: string;
}

/** 与后端 SLUG_PATTERN 同一条 */
export const SLUG_RE = /^[a-z0-9][a-z0-9_-]*$/;
const ENV_KEY_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** 权限策略：每次询问（默认）→ 只读 → 全部拒绝 → 全部允许。全部允许用警示色。 */
export const POLICY_OPTIONS: { value: AcpPermissionPolicy; label: ZhEn; hint: ZhEn; warn?: boolean }[] = [
  {
    value: 'ask',
    label: { zh: '每次询问', en: 'Ask each time' },
    hint: {
      zh: '改文件、运行命令前先在对话里问你，5 分钟没回答就拒绝。',
      en: 'Asks in the conversation before it edits files or runs commands. No answer in 5 minutes counts as no.',
    },
  },
  {
    value: 'read_only',
    label: { zh: '只允许读取', en: 'Read only' },
    hint: { zh: '可以读文件和搜索，其他操作一律拒绝。', en: 'It can read and search. Everything else is refused.' },
  },
  {
    value: 'deny',
    label: { zh: '全部拒绝', en: 'Refuse all' },
    hint: {
      zh: '需要授权的操作一律拒绝，它只能回答。',
      en: 'Anything that needs permission is refused, so it can only answer.',
    },
  },
  {
    value: 'auto',
    label: { zh: '全部允许', en: 'Allow all' },
    hint: {
      zh: '不经询问就改文件、运行命令。只在你信任它时使用。',
      en: 'Edits files and runs commands without asking. Use only if you trust it.',
    },
    warn: true,
  },
];

/** 新建智能体时预选的策略（后端不传时也是它） */
export const DEFAULT_POLICY: AcpPermissionPolicy = 'ask';

export function policyOption(value: string) {
  return POLICY_OPTIONS.find((o) => o.value === value);
}

/** 模板说明的中文版；后端只给英文。没收录的模板就用后端那句。 */
export const TEMPLATE_DESCRIPTIONS: Record<string, ZhEn> = {
  'claude-code': { zh: 'Anthropic 的编程智能体', en: 'Anthropic’s coding agent' },
  codex: { zh: 'OpenAI 的编程智能体', en: 'OpenAI’s coding agent' },
  pi: { zh: '极简的开源编程智能体，经 pi-acp 接入', en: 'A minimal open-source coding agent, through pi-acp' },
  gemini: { zh: 'Google 的开源智能体', en: 'Google’s open-source agent' },
  qwen: { zh: '通义千问的编程智能体', en: 'Qwen’s coding agent' },
  opencode: { zh: '开源智能体，支持多家模型服务', en: 'Open-source agent that works with many providers' },
  kimi: { zh: '月之暗面的命令行智能体', en: 'Moonshot’s command-line agent' },
};

export function templateDescription(t: Pick<AcpAgentTemplate, 'id' | 'description'>): ZhEn {
  return TEMPLATE_DESCRIPTIONS[t.id] ?? { zh: t.description, en: t.description };
}

/** 动手前不征求同意的智能体（pi）：权限设置对它不起作用 */
export const NO_PROMPTS_NOTE: ZhEn = {
  zh: '它动手前不会征求同意，权限设置对它不起作用。',
  en: 'It doesn’t ask before acting, so the permission setting has no effect on it.',
};

/** 一个已添加的智能体此刻的状态：点 + 一个词。依据是上一次检查连接的结果。 */
export function agentStatus(
  a: Pick<AcpAgentRead, 'enabled' | 'command_found' | 'last_error' | 'error_kind' | 'last_probe'>,
): { tone: DotTone; label: ZhEn } {
  if (!a.enabled) return { tone: 'idle', label: { zh: '已停用', en: 'Off' } };
  if (!a.command_found || a.error_kind === 'not_installed') {
    return { tone: 'warn', label: { zh: '未安装', en: 'Not installed' } };
  }
  if (a.last_error) {
    if (a.error_kind === 'needs_login') return { tone: 'warn', label: { zh: '需要登录', en: 'Needs sign-in' } };
    if (a.error_kind === 'timeout') return { tone: 'err', label: { zh: '没有响应', en: 'No response' } };
    return { tone: 'err', label: { zh: '连接失败', en: 'Couldn’t connect' } };
  }
  if (a.last_probe) return { tone: 'ok', label: { zh: '已连接', en: 'Connected' } };
  return { tone: 'idle', label: { zh: '未检查', en: 'Not checked' } };
}

/** 出问题时该做的事：缺程序给安装命令，没登录给登录命令；没有模板的不给。 */
export function agentFix(
  a: Pick<AcpAgentRead, 'command_found' | 'error_kind' | 'last_error'>,
  template: Pick<AcpAgentTemplate, 'install' | 'login'> | undefined,
): { kind: 'install' | 'login'; command: string } | null {
  if (!template) return null;
  if ((!a.command_found || a.error_kind === 'not_installed') && template.install) {
    return { kind: 'install', command: template.install };
  }
  if (a.last_error && a.error_kind === 'needs_login' && template.login) {
    return { kind: 'login', command: template.login };
  }
  return null;
}

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
    return { zh: '只能用字母、数字和下划线，且不能以数字开头', en: 'Letters, digits and underscores only, not starting with a digit' };
  }
  if (all.filter((r) => r.key.trim() === key).length > 1) return { zh: '名字重复', en: 'Duplicate name' };
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

/** 名字 → 一个合法的标识（小写、非法字符换成连字符）；拼不出来就回退到 fallback。 */
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

/** 检查连接时看到的能力 → 给人看的说明。 */
export function probeCapabilities(probe: AcpProbeInfo): ZhEn[] {
  const out: ZhEn[] = [];
  if (probe.mcp_http) out.push({ zh: '可查你的文献库', en: 'Can search your libraries' });
  if (probe.load_session) out.push({ zh: '可接着上次的对话', en: 'Picks up earlier chats' });
  if (probe.prompt_image) out.push({ zh: '能看图片', en: 'Reads images' });
  return out;
}

/** 检查到的名称 + 版本，如「Claude Code 0.108.1」。 */
export function probeTitle(probe: AcpProbeInfo): string {
  return [probe.title || probe.name, probe.version].filter(Boolean).join(' ');
}

/** 后端错误码 → 一句人话；认不出的返回 null，由调用方走通用错误文案。 */
export function acpErrorText(detail: string): ZhEn | null {
  if (detail.startsWith('SLUG_TAKEN')) return { zh: '这个名称已被使用，请换一个', en: 'That name is taken. Choose another.' };
  if (detail.startsWith('BAD_ENV_KEY')) return { zh: '有环境变量名不正确', en: 'An environment variable name is invalid' };
  if (detail.startsWith('COMMAND_REQUIRED')) return { zh: '请填写启动命令', en: 'Enter a command' };
  if (detail.startsWith('BAD_PERMISSION_POLICY')) return { zh: '无法识别的权限设置', en: 'Unknown permission setting' };
  if (detail.startsWith('UNKNOWN_TEMPLATE')) return { zh: '没有这个智能体模板', en: 'Unknown agent template' };
  return null;
}
