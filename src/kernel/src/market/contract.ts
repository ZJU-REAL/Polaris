/* ============================================================
   市场契约（#700，市场 PR-5）：索引条目、polaris manifest、安装记录。

   校验选手写守卫而不是 schemastery：这里的输入是「网络上拉来的不可信
   JSON」，出错时需要指名道姓的字段级诊断（哪个条目、哪个字段、什么
   值），并且要能区分错误类别（MarketError.code）供上层分流处理——
   schemastery 的定位是「配置表单 + 默认值补全」，两个诉求不重合。

   哈希记录（InstallRecord）刻意做成纯数据：install 只负责算出并返回,
   落库（PluginMetaStore）与装载前复核的接线归 PR-6，本模块不依赖
   storage 插件，保持 electron-free 且可独立单测。
   ============================================================ */

/** 七类扩展点（设计报告 §7.1 manifest.kind 的封闭枚举）。 */
export const PLUGIN_KINDS = [
  'datasource',
  'record-kind',
  'runner',
  'agent-tool',
  'workflow',
  'discipline',
  'panel',
] as const
export type PluginKind = (typeof PLUGIN_KINDS)[number]

/** 质量分级（§7.4，学 Home Assistant 的机器可检阶梯）。 */
export const QUALITY_TIERS = ['bronze', 'silver', 'gold', 'platinum'] as const
export type QualityTier = (typeof QUALITY_TIERS)[number]

/** 运行时四档（§7.1）。v1 安装引擎只落盘不启动，档位仅校验合法性。 */
export const PLUGIN_RUNTIMES = ['in-process', 'python-edge', 'subprocess-mcp', 'container'] as const
export type PluginRuntime = (typeof PLUGIN_RUNTIMES)[number]

/** 索引条目里的权限摘要：给市场 UI 如实展示，v1 不 enforcement。 */
export interface MarketPermissions {
  network?: boolean
  filesystem?: boolean
}

/** 官方索引（market/index.json）的单个条目。 */
export interface MarketIndexEntry {
  /** npm 包名（polaris-plugin-* / @scope/polaris-plugin-*）。 */
  name: string
  /** 上架版本；安装时按此精确版本向 registry 解析。 */
  version: string
  kind: PluginKind
  description: string
  publisher: string
  permissions: MarketPermissions
  tier: QualityTier
  /** 治理徽章（official/verified/preview/insecure…），开放枚举。 */
  badges: string[]
}

/** 索引文件的外层包装：schemaVersion 留给不兼容演进用。 */
export interface MarketIndex {
  schemaVersion: number
  plugins: MarketIndexEntry[]
}

/** manifest 里的权限声明：设计报告 §7.1 允许细粒度形态
    （network 白名单数组 / filesystem 范围串），索引摘要是布尔。
    两种形态都收，v1 只展示不执行。 */
export interface ManifestPermissions {
  network?: boolean | string[]
  filesystem?: boolean | string
}

/** package.json 的 `polaris` 字段（§7.1 组件 manifest）。 */
export interface PolarisManifest {
  kind: PluginKind
  /** 入口文件（包内相对路径）。硬规则：必须指向单文件 bundle
      （Obsidian 模式）——kernel 零依赖解析，装完即可 import。 */
  entry: string
  /** 单串或按语言分（{ zh, en }）。上架/安装时逐值过描述 lint。 */
  description?: string | Record<string, string>
  permissions?: ManifestPermissions
  runtime?: PluginRuntime
  /** agent 工具声明（§7.5），v1 只透传不校验内部结构。 */
  tools?: unknown[]
  locales?: string[]
}

/** 安装记录：内容哈希绑定（§7.5-5：审批与信任绑内容哈希而非包名）。
    entry 冗余进记录是刻意的——装载前复核不能回头读包内 package.json
    拿入口路径（篡改者可以同时改 manifest 指向别的文件），必须以安装
    时刻记下的路径为准。 */
export interface InstallRecord {
  name: string
  version: string
  /** 入口文件相对安装目录的路径（安装时刻的 manifest.entry 快照）。 */
  entry: string
  /** tarball 的 SRI 串（`sha512-<base64>`），与 npm dist.integrity 同构。 */
  tarballSha512: string
  /** 入口文件内容的 sha256（hex）。装载前复核对这个值。 */
  entrySha256: string
  /** ISO 8601 时间戳。 */
  installedAt: string
  /** 安装目录绝对路径（pluginsDir/<name>/<version>）。 */
  dir: string
}

/* ---------- 可诊断错误 ---------- */

export type MarketErrorCode =
  | 'index-timeout'
  | 'index-http'
  | 'index-parse'
  | 'index-invalid'
  | 'registry-http'
  | 'registry-invalid'
  | 'version-not-found'
  | 'integrity-unsupported'
  | 'integrity-mismatch'
  | 'tarball-http'
  | 'tar-invalid'
  | 'tar-entry-rejected'
  | 'path-traversal'
  | 'manifest-missing'
  | 'manifest-invalid'
  | 'manifest-entry-missing'
  | 'entry-file-missing'
  | 'description-lint'
  | 'bad-package-name'

/** 市场/安装链路的错误：code 供程序分流，message 给人看细节。 */
export class MarketError extends Error {
  readonly code: MarketErrorCode

  constructor(code: MarketErrorCode, message: string, options?: ErrorOptions) {
    super(message, options)
    this.name = 'MarketError'
    this.code = code
  }
}

/* ---------- 手写守卫 ---------- */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((v) => typeof v === 'string')
}

/** npm 包名合法性。除了 npm 规则，这也是路径安全的第一道闸：
    安装目录用包名拼路径，放进来的名字必须不可能构成 ../ 逃逸。 */
export function isValidPackageName(name: unknown): name is string {
  if (typeof name !== 'string' || !name.length || name.length > 214) return false
  return /^(@[a-z0-9][a-z0-9-._~]*\/)?[a-z0-9][a-z0-9-._~]*$/.test(name) && !name.includes('..')
}

function fail(code: MarketErrorCode, where: string, detail: string): never {
  throw new MarketError(code, `${where}: ${detail}`)
}

/** 校验索引单条目；where 用于错误定位（如 `plugins[3]`）。 */
export function validateMarketIndexEntry(value: unknown, where = 'entry'): MarketIndexEntry {
  if (!isRecord(value)) fail('index-invalid', where, 'not an object')
  if (!isValidPackageName(value.name)) {
    fail('index-invalid', where, `invalid npm package name: ${JSON.stringify(value.name)}`)
  }
  if (typeof value.version !== 'string' || !value.version.length) {
    fail('index-invalid', where, 'version must be a non-empty string')
  }
  if (!PLUGIN_KINDS.includes(value.kind as PluginKind)) {
    fail('index-invalid', where, `kind must be one of ${PLUGIN_KINDS.join('|')}, got ${JSON.stringify(value.kind)}`)
  }
  if (typeof value.description !== 'string') fail('index-invalid', where, 'description must be a string')
  if (typeof value.publisher !== 'string' || !value.publisher.length) {
    fail('index-invalid', where, 'publisher must be a non-empty string')
  }
  if (!isRecord(value.permissions)) fail('index-invalid', where, 'permissions must be an object')
  const permissions: MarketPermissions = {}
  for (const key of ['network', 'filesystem'] as const) {
    const flag = value.permissions[key]
    if (flag === undefined) continue
    if (typeof flag !== 'boolean') fail('index-invalid', where, `permissions.${key} must be a boolean`)
    permissions[key] = flag
  }
  if (!QUALITY_TIERS.includes(value.tier as QualityTier)) {
    fail('index-invalid', where, `tier must be one of ${QUALITY_TIERS.join('|')}, got ${JSON.stringify(value.tier)}`)
  }
  if (!isStringArray(value.badges)) fail('index-invalid', where, 'badges must be a string array')
  // 索引描述同样过 lint：投毒描述在「上架面」就该拦住，不等安装
  const issues = lintDescription(value.description)
  if (issues.length) fail('description-lint', where, issues.join('; '))
  return {
    name: value.name,
    version: value.version,
    kind: value.kind as PluginKind,
    description: value.description,
    publisher: value.publisher,
    permissions,
    tier: value.tier as QualityTier,
    badges: [...value.badges],
  }
}

/** 校验索引整体（外层包装 + 逐条目）。 */
export function validateMarketIndex(value: unknown): MarketIndex {
  if (!isRecord(value)) fail('index-invalid', 'index', 'not an object')
  if (value.schemaVersion !== 1) {
    fail('index-invalid', 'index', `unsupported schemaVersion ${JSON.stringify(value.schemaVersion)} (expected 1)`)
  }
  if (!Array.isArray(value.plugins)) fail('index-invalid', 'index', 'plugins must be an array')
  return {
    schemaVersion: 1,
    plugins: value.plugins.map((entry, i) => validateMarketIndexEntry(entry, `plugins[${i}]`)),
  }
}

/** 校验 package.json 的 polaris manifest。缺字段/非法值给出各自可
    诊断的错误码——上层要区分「作者忘写 entry」和「kind 拼错」。 */
export function validatePolarisManifest(pkg: unknown): PolarisManifest {
  if (!isRecord(pkg)) fail('manifest-missing', 'package.json', 'not an object')
  const raw = pkg.polaris
  if (raw === undefined) fail('manifest-missing', 'package.json', 'no `polaris` field')
  if (!isRecord(raw)) fail('manifest-invalid', 'polaris', 'must be an object')
  if (!PLUGIN_KINDS.includes(raw.kind as PluginKind)) {
    fail('manifest-invalid', 'polaris.kind', `must be one of ${PLUGIN_KINDS.join('|')}, got ${JSON.stringify(raw.kind)}`)
  }
  if (typeof raw.entry !== 'string' || !raw.entry.length) {
    fail('manifest-entry-missing', 'polaris.entry', 'must be a non-empty relative path to a single-file bundle')
  }
  const manifest: PolarisManifest = { kind: raw.kind as PluginKind, entry: raw.entry }
  if (raw.description !== undefined) {
    if (typeof raw.description === 'string') {
      manifest.description = raw.description
    } else if (isRecord(raw.description) && Object.values(raw.description).every((v) => typeof v === 'string')) {
      manifest.description = raw.description as Record<string, string>
    } else {
      fail('manifest-invalid', 'polaris.description', 'must be a string or a {locale: string} record')
    }
  }
  if (raw.runtime !== undefined) {
    if (!PLUGIN_RUNTIMES.includes(raw.runtime as PluginRuntime)) {
      fail('manifest-invalid', 'polaris.runtime', `must be one of ${PLUGIN_RUNTIMES.join('|')}, got ${JSON.stringify(raw.runtime)}`)
    }
    manifest.runtime = raw.runtime as PluginRuntime
  }
  if (raw.permissions !== undefined) {
    if (!isRecord(raw.permissions)) fail('manifest-invalid', 'polaris.permissions', 'must be an object')
    const permissions: ManifestPermissions = {}
    const network = raw.permissions.network
    if (network !== undefined) {
      if (typeof network !== 'boolean' && !isStringArray(network)) {
        fail('manifest-invalid', 'polaris.permissions.network', 'must be a boolean or a string array')
      }
      permissions.network = network
    }
    const filesystem = raw.permissions.filesystem
    if (filesystem !== undefined) {
      if (typeof filesystem !== 'boolean' && typeof filesystem !== 'string') {
        fail('manifest-invalid', 'polaris.permissions.filesystem', 'must be a boolean or a string')
      }
      permissions.filesystem = filesystem
    }
    manifest.permissions = permissions
  }
  if (raw.tools !== undefined) {
    if (!Array.isArray(raw.tools)) fail('manifest-invalid', 'polaris.tools', 'must be an array')
    manifest.tools = raw.tools
  }
  if (raw.locales !== undefined) {
    if (!isStringArray(raw.locales)) fail('manifest-invalid', 'polaris.locales', 'must be a string array')
    manifest.locales = raw.locales
  }
  return manifest
}

/* ---------- 描述 lint（§7.5-1，MCPTox 教训） ---------- */

/* 指令性文字黑名单：描述是给人看的说明，出现「对模型下指令」的句式
   即视为投毒尝试（MCPTox：工具描述投毒平均成功率 36.5%，越强的模型
   越顺从恶意元数据）。宁可误杀让作者改措辞，不可漏放。 */
const IMPERATIVE_PATTERNS: { pattern: RegExp; label: string }[] = [
  { pattern: /ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier|preceding)/i, label: 'ignore-previous' },
  { pattern: /disregard\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier|instructions)/i, label: 'disregard-instructions' },
  { pattern: /forget\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier|your)\s+(?:instructions|rules|prompts?)/i, label: 'forget-instructions' },
  { pattern: /you\s+(?:must|should|are\s+required\s+to|have\s+to|will\s+now)\b/i, label: 'you-must' },
  { pattern: /system\s+prompt/i, label: 'system-prompt' },
  { pattern: /do\s+not\s+(?:tell|reveal|mention|disclose|inform)\b/i, label: 'do-not-reveal' },
  { pattern: /(?:^|\s)act\s+as\s+(?:a\s+|an\s+|the\s+)?(?:system|admin|root|developer)/i, label: 'act-as' },
  { pattern: /请?忽略(?:之前|以上|上面|前面|先前|所有)/, label: 'ignore-previous-zh' },
  { pattern: /无视(?:之前|以上|上面|前面|先前|所有)/, label: 'ignore-previous-zh' },
  { pattern: /你(?:必须|应当|现在要)/, label: 'you-must-zh' },
  { pattern: /系统提示词?/, label: 'system-prompt-zh' },
  { pattern: /不要(?:告诉|透露|提及)/, label: 'do-not-reveal-zh' },
]

/* 不可见/方向控制字符：零宽系、bidi 控制、BYTE ORDER MARK、行为分隔、
   interlinear annotation、以及 Unicode Tags 块（U+E0000–E007F，ASCII
   镜像字符，是「肉眼不可见指令」走私的已知载体）。 */
const INVISIBLE_PATTERN =
  /[\u00ad\u200b-\u200f\u2028\u2029\u202a-\u202e\u2060-\u2064\u206a-\u206f\ufeff\ufff9-\ufffb]|[\u{e0000}-\u{e007f}]/u

/** 描述 lint：返回问题列表，空数组即干净。返回而不是抛出，
    方便上架工具一次列全所有问题；安装链路拿到非空即拒。 */
export function lintDescription(text: string): string[] {
  const issues: string[] = []
  for (const { pattern, label } of IMPERATIVE_PATTERNS) {
    const match = pattern.exec(text)
    if (match) issues.push(`imperative text (${label}): ${JSON.stringify(match[0].trim())}`)
  }
  const invisible = INVISIBLE_PATTERN.exec(text)
  if (invisible) {
    const codePoint = invisible[0].codePointAt(0)!.toString(16).toUpperCase().padStart(4, '0')
    issues.push(`invisible unicode: U+${codePoint}`)
  }
  return issues
}
