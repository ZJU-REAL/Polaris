/* ============================================================
   内置插件注册表（#699，D1/D3）。

   打包态（esbuild + asar）里动态 import 内置插件是 R1 问题的根源，
   所以内置插件全部走 loader.builtins 查表：配置树条目用 cordis:<键>
   specifier，EntryTree.import 命中前缀直接查表返回，零动态 import。

   storage 故意不入表（D3）：loader 树本身就持久化在 storage 的
   SqliteConfigTreeStore 里，storage 是先于 loader 直挂的地基——把它
   放进树等于「树条目负责拉起存树的地方」，自举成环。
   本文件必须保持 electron-free（tests/electron-free.test.ts 强制）。
   ============================================================ */

import { Group, type Loader } from '@deepseek-ai/cordis-plugin-loader'
import { legacyEngine } from './legacy-engine.ts'
import { sources } from './sources.ts'

/**
 * 空探针插件：存在本身就证明 cordis 插件树被真的建起来了（桌面侧
 * kernel.status 的 plugins 计数由它兜底 ≥ 1）。原先定义在 desktop 的
 * kernel.ts，树驱动装载后它必须能按名查表，随 #699 挪进 kernel；
 * desktop 侧改引用归 PR-2。
 */
export const desktopProbe = {
  name: 'desktop-probe',
  apply(): void {
    /* 故意为空：只占一个 registry 名额 */
  },
}

/** 键 = cordis: 后面的 specifier 尾巴，如 cordis:legacy-engine。 */
export const BUILTIN_PLUGINS: Record<string, unknown> = {
  'desktop-probe': desktopProbe,
  'legacy-engine': legacyEngine,
  sources,
}

/** 把内置插件表灌进 loader.builtins。装 SqliteTree 之前调用。 */
export function registerBuiltins(loader: Loader): void {
  Object.assign(loader.builtins, BUILTIN_PLUGINS)
  // 组插件也要可导入：配置树里的组条目（children）序列化成
  // name=cordis:group 的行，Entry 初始化照样按 name 查表拿插件。
  // Group 是 loader 包自带的载体插件，不算 Polaris 内置，故不进上表。
  loader.builtins['group'] = Group
}
