/* 市场测试共用 fixture（#700 引入，#708 抽出共享）：手写 ustar 头现场
   生成 .tgz + fetch 替身。零真网、零预生成二进制——想改 fixture 改代码
   即可，diff 可读，顺便当 install.ts 里 tar 解析器的对拍实现。 */
import { createHash } from 'node:crypto'
import { gzipSync } from 'node:zlib'
import type { FetchImpl } from '../../src/index.ts'

export interface TarSpec {
  name: string
  data?: string
  /** '0'=文件 '2'=软链 '5'=目录 …；默认文件。 */
  typeflag?: string
}

export function tarHeader(name: string, size: number, typeflag: string): Buffer {
  const block = Buffer.alloc(512)
  block.write(name, 0, 100, 'utf8')
  block.write('0000644\0', 100) // mode
  block.write('0000000\0', 108) // uid
  block.write('0000000\0', 116) // gid
  block.write(size.toString(8).padStart(11, '0') + '\0', 124)
  block.write('00000000000\0', 136) // mtime
  block.write('        ', 148) // chksum 先占 8 个空格参与求和
  block.write(typeflag, 156)
  block.write('ustar\0', 257)
  block.write('00', 263)
  let sum = 0
  for (const byte of block) sum += byte
  block.write(sum.toString(8).padStart(6, '0') + '\0 ', 148)
  return block
}

export function makeTgz(specs: TarSpec[]): Buffer {
  const blocks: Buffer[] = []
  for (const spec of specs) {
    const data = Buffer.from(spec.data ?? '', 'utf8')
    const typeflag = spec.typeflag ?? '0'
    blocks.push(tarHeader(spec.name, typeflag === '0' ? data.length : 0, typeflag))
    if (typeflag === '0' && data.length) {
      const padded = Buffer.alloc(Math.ceil(data.length / 512) * 512)
      data.copy(padded)
      blocks.push(padded)
    }
  }
  blocks.push(Buffer.alloc(1024)) // 结尾两个零块
  return gzipSync(Buffer.concat(blocks))
}

export const HELLO_ENTRY = "module.exports = { name: 'hello', apply() {} }\n"

/** polaris=null 表示整个字段省略（manifest-missing 用例）。 */
export function helloPkg(
  polaris: Record<string, unknown> | null,
  description = 'A tiny hello plugin',
  version = '1.0.0',
): string {
  const pkg: Record<string, unknown> = { name: 'polaris-plugin-hello', version, description }
  if (polaris !== null) pkg.polaris = polaris
  return JSON.stringify(pkg, null, 2)
}

export const GOOD_POLARIS = {
  kind: 'panel',
  entry: 'index.js',
  description: { zh: '示例面板插件', en: 'An example panel plugin' },
  permissions: { network: false },
  runtime: 'in-process',
  locales: ['zh', 'en'],
}

export function helloTgz(overrides: {
  polaris?: Record<string, unknown> | null
  description?: string
  specs?: TarSpec[]
  version?: string
  entryData?: string
}): Buffer {
  return makeTgz(
    overrides.specs ?? [
      {
        name: 'package/package.json',
        data: helloPkg(
          overrides.polaris === undefined ? GOOD_POLARIS : overrides.polaris,
          overrides.description,
          overrides.version,
        ),
      },
      { name: 'package/index.js', data: overrides.entryData ?? HELLO_ENTRY },
    ],
  )
}

/* ---------- fetch 替身：packument JSON + tarball 二进制 ---------- */

export const REGISTRY = 'https://registry.test'
export const TARBALL_URL = `${REGISTRY}/polaris-plugin-hello/-/polaris-plugin-hello-1.0.0.tgz`

export function registryFetch(
  tgz: Buffer,
  opts: { integrity?: string; version?: string; registryUrl?: string } = {},
): FetchImpl {
  const version = opts.version ?? '1.0.0'
  const registryUrl = opts.registryUrl ?? REGISTRY
  const tarballUrl = `${registryUrl}/polaris-plugin-hello/-/polaris-plugin-hello-${version}.tgz`
  const integrity = opts.integrity ?? `sha512-${createHash('sha512').update(tgz).digest('base64')}`
  return (async (input: unknown) => {
    const url = String(input)
    if (url === `${registryUrl}/polaris-plugin-hello`) {
      return Response.json({
        name: 'polaris-plugin-hello',
        versions: { [version]: { dist: { tarball: tarballUrl, integrity } } },
      })
    }
    if (url === tarballUrl) return new Response(new Uint8Array(tgz))
    return new Response('not found', { status: 404 })
  }) as FetchImpl
}
