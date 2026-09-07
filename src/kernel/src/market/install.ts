/* ============================================================
   安装引擎（#700，市场 PR-5）：npm registry → 校验 → 落盘。

   流程：GET {registry}/{name} 拿 packument → 取 versions[version] 的
   dist.tarball + dist.integrity → 下载 → sha512 复核（不过关的字节
   连解压器都不进）→ 解压到 pluginsDir/<name>/<version>/ → 校验
   polaris manifest + 描述 lint → 算入口 sha256 → 返回 InstallRecord。

   InstallRecord 是纯返回值：落库（PluginMetaStore）与装载前复核的
   接线归 PR-6，本模块保持纯函数化、不依赖 storage 插件。

   tar 解析为什么手写：不新增 npm 依赖是硬约束，而我们只需要读
   node-tar 产出的 npm 发布包——ustar 定长头 + 数据块，几十行内能
   覆盖。刻意收窄的部分见 parseTar 头注。
   本文件必须保持 electron-free（tests/electron-free.test.ts 强制）。
   ============================================================ */

import { createHash } from 'node:crypto'
import { mkdir, readFile, rm, rmdir, writeFile } from 'node:fs/promises'
import { dirname, join, resolve, sep } from 'node:path'
import { gunzipSync } from 'node:zlib'
import type { FetchImpl } from '../sources/contract.ts'
import {
  MarketError,
  isValidPackageName,
  lintDescription,
  validatePolarisManifest,
  type InstallRecord,
} from './contract.ts'

export const NPM_REGISTRY = 'https://registry.npmjs.org'

/* ---------- 极简 tar 解析 ----------

   tar 是 512 字节块的序列：每个成员 = 1 个头块 + ceil(size/512) 个
   数据块，结尾两个全零块。头块（POSIX ustar）的字段是定长偏移：

     偏移   长度  字段
     0      100   name（NUL 结尾）
     124    12    size（八进制 ASCII）
     148    8     chksum（头块字节和，本字段按空格算）
     156    1     typeflag（'0'/NUL=文件 '5'=目录 '1'=硬链 '2'=软链
                  'x'/'g'=pax 扩展头 'L'=GNU longname）
     257    6     magic（"ustar"）
     345    155   prefix（长路径的前半截，name = prefix + '/' + name）

   刻意收窄（v1 单文件 bundle 硬规则下够用，越少解析越少攻击面）：
   - 硬链/软链一律拒绝：软链指向目录外再经它写文件是经典逃逸手法，
     npm 发布包里也根本不该出现链接。
   - pax/longname 扩展头（x/g/L）跳过数据、不应用其路径覆盖：我们
     永远用 ustar 头里的名字做路径校验，覆盖名藏毒也够不到落盘路径；
     代价是超过 100+155 字节的路径会被截断——对插件包不构成限制。 */

interface TarEntry {
  /** 成员路径（prefix + name 拼接后）。 */
  name: string
  typeflag: string
  data: Buffer
}

const BLOCK = 512

function readOctal(block: Buffer, offset: number, length: number): number {
  const raw = block.toString('ascii', offset, offset + length).replace(/[\0 ]+$/, '').trim()
  if (!raw) return 0
  const value = Number.parseInt(raw, 8)
  if (Number.isNaN(value)) throw new MarketError('tar-invalid', `bad octal field at offset ${offset}`)
  return value
}

function readString(block: Buffer, offset: number, length: number): string {
  const end = block.indexOf(0, offset)
  return block.toString('utf8', offset, end === -1 || end > offset + length ? offset + length : end)
}

/** 解析整个 tar buffer 为成员列表。 */
export function parseTar(tar: Buffer): TarEntry[] {
  const entries: TarEntry[] = []
  let offset = 0
  while (offset + BLOCK <= tar.length) {
    const block = tar.subarray(offset, offset + BLOCK)
    if (block.every((byte) => byte === 0)) break // 结尾零块

    // 校验和：头块所有字节求和，chksum 字段本身按 8 个空格计
    const expected = readOctal(block, 148, 8)
    let sum = 0
    for (let i = 0; i < BLOCK; i++) sum += i >= 148 && i < 156 ? 0x20 : block[i]!
    if (sum !== expected) {
      throw new MarketError('tar-invalid', `header checksum mismatch at offset ${offset}`)
    }

    const size = readOctal(block, 124, 12)
    const prefix = readString(block, 345, 155)
    const shortName = readString(block, 0, 100)
    const name = prefix ? `${prefix}/${shortName}` : shortName
    const typeflag = block.toString('ascii', 156, 157)
    const dataStart = offset + BLOCK
    if (dataStart + size > tar.length) {
      throw new MarketError('tar-invalid', `truncated data for ${name}`)
    }
    entries.push({ name, typeflag, data: tar.subarray(dataStart, dataStart + size) })
    offset = dataStart + Math.ceil(size / BLOCK) * BLOCK
  }
  return entries
}

/** tar 成员名 → 安装目录内的相对路径；不在目录内即抛 path-traversal。
    npm 包成员统一带一层顶级目录（规范打包是 package/），学
    --strip-components=1 剥掉第一段再校验。 */
function safeRelativePath(memberName: string, destDir: string): string | null {
  if (memberName.includes('\0')) {
    throw new MarketError('path-traversal', `NUL byte in tar member name`)
  }
  const stripped = memberName.split('/').slice(1).join('/')
  if (!stripped) return null // 顶级目录本身，无落盘物
  // 手工归一化：拆段过滤 '.'，见到 '..' 直接拒——比 path.normalize 后
  // 再检查更直白，也不给「归一化后碰巧合法」的花活留缝
  const parts: string[] = []
  for (const part of stripped.split('/')) {
    if (part === '' || part === '.') continue
    if (part === '..') {
      throw new MarketError('path-traversal', `tar member escapes install dir: ${memberName}`)
    }
    parts.push(part)
  }
  if (!parts.length) return null
  const rel = parts.join('/')
  // 双保险：拼出绝对路径后再确认真的落在目标目录内
  const abs = resolve(destDir, rel)
  if (abs !== destDir && !abs.startsWith(destDir + sep)) {
    throw new MarketError('path-traversal', `tar member escapes install dir: ${memberName}`)
  }
  return rel
}

/* ---------- 安装 ---------- */

export interface InstallPluginOptions {
  /** npm 包名。 */
  name: string
  /** 精确版本（索引条目里的 version）。 */
  version: string
  /** registry 基址，默认官方；镜像/私仓可换。 */
  registryUrl?: string
  /** 插件根目录（桌面侧是 userData/plugins），本包不预设位置。 */
  pluginsDir: string
  fetchImpl?: FetchImpl
}

/** registry packument 里用到的字段。 */
interface PackumentVersion {
  dist?: { tarball?: unknown; integrity?: unknown }
}

function sriSha512(integrity: string): string | undefined {
  // SRI 允许空格分隔多个哈希，取 sha512 那个
  return integrity.split(/\s+/).find((part) => part.startsWith('sha512-'))
}

/** 从 npm registry 安装一个插件到 pluginsDir/<name>/<version>/。
    任何校验失败都会清掉已解出的目录——不留「半装状态」给装载器踩。 */
export async function installPlugin(options: InstallPluginOptions): Promise<InstallRecord> {
  const { name, version, pluginsDir } = options
  const fetchImpl = options.fetchImpl ?? globalThis.fetch
  const registryUrl = (options.registryUrl ?? NPM_REGISTRY).replace(/\/+$/, '')

  if (!isValidPackageName(name)) {
    throw new MarketError('bad-package-name', `invalid npm package name: ${JSON.stringify(name)}`)
  }
  // version 也会拼进落盘路径，字符集必须收紧到不可能构成路径符号
  if (!/^[0-9A-Za-z.+-]+$/.test(version)) {
    throw new MarketError('path-traversal', `version contains path characters: ${JSON.stringify(version)}`)
  }

  // 1. packument：scoped 包的 / 要转义（registry 的既定形状）
  const packumentUrl = `${registryUrl}/${name.replace('/', '%2f')}`
  const metaRes = await fetchImpl(packumentUrl)
  if (!metaRes.ok) {
    throw new MarketError('registry-http', `registry metadata failed with HTTP ${metaRes.status}: ${packumentUrl}`)
  }
  let packument: { versions?: Record<string, PackumentVersion> }
  try {
    packument = (await metaRes.json()) as typeof packument
  } catch (error) {
    throw new MarketError('registry-invalid', `registry metadata is not valid JSON: ${packumentUrl}`, { cause: error })
  }
  const versionMeta = packument.versions?.[version]
  if (!versionMeta) {
    throw new MarketError('version-not-found', `${name}@${version} not found in registry`)
  }
  const tarballUrl = versionMeta.dist?.tarball
  const integrity = versionMeta.dist?.integrity
  if (typeof tarballUrl !== 'string' || !tarballUrl.length) {
    throw new MarketError('registry-invalid', `${name}@${version} has no dist.tarball`)
  }
  if (typeof integrity !== 'string' || !integrity.length) {
    throw new MarketError('integrity-unsupported', `${name}@${version} has no dist.integrity`)
  }
  const expectedSri = sriSha512(integrity)
  if (!expectedSri) {
    throw new MarketError('integrity-unsupported', `${name}@${version} integrity is not sha512: ${integrity}`)
  }

  // 2. 下载 + integrity 复核：不过关的字节连解压器都不喂
  const tarballRes = await fetchImpl(tarballUrl)
  if (!tarballRes.ok) {
    throw new MarketError('tarball-http', `tarball download failed with HTTP ${tarballRes.status}: ${tarballUrl}`)
  }
  const tarball = Buffer.from(await tarballRes.arrayBuffer())
  const actualSri = `sha512-${createHash('sha512').update(tarball).digest('base64')}`
  if (actualSri !== expectedSri) {
    throw new MarketError(
      'integrity-mismatch',
      `${name}@${version} tarball integrity mismatch: expected ${expectedSri}, got ${actualSri}`,
    )
  }

  // 3. 解压解析（内存中完成全部路径校验，再统一落盘：
  //    路径穿越在写第一个字节之前就会被拒）
  let tar: Buffer
  try {
    tar = gunzipSync(tarball)
  } catch (error) {
    throw new MarketError('tar-invalid', `${name}@${version} tarball is not valid gzip`, { cause: error })
  }
  const destDir = resolve(pluginsDir, ...name.split('/'), version)
  const files: { rel: string; data: Buffer }[] = []
  for (const entry of parseTar(tar)) {
    if (entry.typeflag === '1' || entry.typeflag === '2') {
      throw new MarketError('tar-entry-rejected', `link entry rejected: ${entry.name}`)
    }
    // pax/longname 扩展头与目录项：无落盘物，跳过（见文件头「刻意收窄」）
    if (entry.typeflag !== '0' && entry.typeflag !== '\0' && entry.typeflag !== '') continue
    const rel = safeRelativePath(entry.name, destDir)
    if (rel === null) continue
    files.push({ rel, data: entry.data })
  }

  // 4. 落盘：重装同版本按「覆盖到干净状态」处理，先清后写
  await rm(destDir, { recursive: true, force: true })
  await mkdir(destDir, { recursive: true })
  try {
    for (const file of files) {
      const abs = join(destDir, file.rel)
      await mkdir(dirname(abs), { recursive: true })
      await writeFile(abs, file.data)
    }

    // 5. manifest 校验 + 描述 lint + 入口哈希
    let pkg: unknown
    try {
      pkg = JSON.parse(await readFile(join(destDir, 'package.json'), 'utf8'))
    } catch (error) {
      throw new MarketError('manifest-missing', `${name}@${version} has no readable package.json`, { cause: error })
    }
    const manifest = validatePolarisManifest(pkg)

    // 入口路径同样过穿越防护：manifest 是包作者写的，不可信
    const entryRel = safeRelativePath(`package/${manifest.entry}`, destDir)
    if (entryRel === null) {
      throw new MarketError('manifest-entry-missing', `polaris.entry resolves to nothing: ${manifest.entry}`)
    }
    let entryBytes: Buffer
    try {
      entryBytes = await readFile(join(destDir, entryRel))
    } catch (error) {
      throw new MarketError('entry-file-missing', `polaris.entry not found in package: ${manifest.entry}`, {
        cause: error,
      })
    }

    const descriptions: string[] = []
    if (typeof (pkg as { description?: unknown }).description === 'string') {
      descriptions.push((pkg as { description: string }).description)
    }
    if (typeof manifest.description === 'string') descriptions.push(manifest.description)
    else if (manifest.description) descriptions.push(...Object.values(manifest.description))
    const lintIssues = descriptions.flatMap((text) => lintDescription(text))
    if (lintIssues.length) {
      throw new MarketError('description-lint', `${name}@${version} description rejected: ${lintIssues.join('; ')}`)
    }

    return {
      name,
      version,
      entry: entryRel,
      tarballSha512: actualSri,
      entrySha256: createHash('sha256').update(entryBytes).digest('hex'),
      installedAt: new Date().toISOString(),
      dir: destDir,
    }
  } catch (error) {
    // 校验不过的包不能留在盘上：半装状态会被后续装载器当成已安装
    await rm(destDir, { recursive: true, force: true })
    throw error
  }
}

/** 装载前复核：入口文件当前内容的 sha256 是否仍与安装记录一致。
    读不到文件同样返回 false——「被删了」和「被改了」对装载器是同一
    个答案：不可信，禁用并警告（D4）。 */
export async function verifyEntryHash(record: InstallRecord): Promise<boolean> {
  try {
    const bytes = await readFile(join(record.dir, record.entry))
    return createHash('sha256').update(bytes).digest('hex') === record.entrySha256
  } catch {
    return false
  }
}

export interface UninstallPluginOptions {
  name: string
  pluginsDir: string
}

/** 卸载 = 删掉该包的整个目录（所有版本）。配置树上的启用条目归
    调用方（PR-6）收拾，这里只管盘面。 */
export async function uninstallPlugin(options: UninstallPluginOptions): Promise<void> {
  const { name, pluginsDir } = options
  if (!isValidPackageName(name)) {
    throw new MarketError('bad-package-name', `invalid npm package name: ${JSON.stringify(name)}`)
  }
  await rm(resolve(pluginsDir, ...name.split('/')), { recursive: true, force: true })
  // scoped 包删完顺手收掉空的 @scope 目录；非空会抛错，吞掉即可
  if (name.includes('/')) {
    await rmdir(resolve(pluginsDir, name.split('/')[0]!)).catch(() => {})
  }
}
